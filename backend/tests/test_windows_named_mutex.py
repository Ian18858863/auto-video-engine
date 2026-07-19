from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core import windows_named_mutex as mutex_module
from app.core.windows_named_mutex import (
    WindowsMutexBusy,
    WindowsNamedMutex,
    build_mutex_name,
    current_user_sid_string,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
TEST_TMP_ROOT = REPO_ROOT / ".test-tmp" / "task7-named-mutex"


pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="named mutex enforcement is Windows-only"
)


def _child_env() -> dict[str, str]:
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    env["TEMP"] = str(TEST_TMP_ROOT)
    env["TMP"] = str(TEST_TMP_ROOT)
    env["TMPDIR"] = str(TEST_TMP_ROOT)
    return env


def _start_mutex_owner(name: str) -> subprocess.Popen[str]:
    code = r"""
import json
import sys
import time
from app.core.windows_named_mutex import WindowsNamedMutex

mutex = WindowsNamedMutex.try_acquire(sys.argv[1])
print(json.dumps({"state": "acquired", "pid": __import__("os").getpid()}), flush=True)
time.sleep(120)
mutex.release()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, name],
        cwd=BACKEND_ROOT,
        env=_child_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        close_fds=True,
    )
    assert process.stdout is not None
    line = process.stdout.readline()
    assert line, process.stderr.read() if process.stderr else "owner exited silently"
    assert json.loads(line)["state"] == "acquired"
    return process


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def test_name_factory_is_deterministic_scoped_and_bound_to_current_sid():
    first = build_mutex_name("material-step", "database|pipeline|material")
    again = build_mutex_name("material-step", "database|pipeline|material")
    registry = build_mutex_name("asset-registry", "database|pipeline|material")

    assert first == again
    assert first != registry
    assert first.startswith(
        f"Global\\AutoVideoEngine.material-step.{current_user_sid_string()}."
    )
    assert len(first.rsplit(".", 1)[1]) == 64
    assert set(first.rsplit(".", 1)[1]) <= set("0123456789abcdef")


def test_owner_lease_namespace_is_available_and_path_scoped():
    name = build_mutex_name(
        "material-submission-owner",
        r"D:\CodexData\Temp\auto-video-engine\submissions.sqlite3",
    )
    same = build_mutex_name(
        "material-submission-owner",
        r"D:\CodexData\Temp\auto-video-engine\submissions.sqlite3",
    )
    other = build_mutex_name(
        "material-submission-owner",
        r"D:\CodexData\Temp\auto-video-engine\other.sqlite3",
    )
    assert name == same
    assert name != other
    assert ".material-submission-owner." in name


def test_script_version_namespace_is_project_scoped():
    first = build_mutex_name("script-version", r"D:\projects\project-a|17")
    same = build_mutex_name("script-version", r"D:\projects\project-a|17")
    other = build_mutex_name("script-version", r"D:\projects\project-a|18")

    assert first == same
    assert first != other
    assert ".script-version." in first


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "user-controlled",
        r"Local\AutoVideoEngine.material-step.S-1-5-21.bad",
        r"Global\AutoVideoEngine.material-step.S-1-5-18." + "0" * 64,
        r"Global\AutoVideoEngine.unknown.S-1-5-21-1." + "0" * 64,
    ],
)
def test_try_acquire_rejects_names_not_created_for_current_user(unsafe_name: str):
    with pytest.raises(ValueError):
        WindowsNamedMutex.try_acquire(unsafe_name)


def test_two_processes_cannot_acquire_same_mutex_at_once():
    name = build_mutex_name("material-step", f"competition-{os.urandom(16).hex()}")
    owner = _start_mutex_owner(name)
    try:
        with pytest.raises(WindowsMutexBusy) as exc_info:
            WindowsNamedMutex.try_acquire(name)
        assert exc_info.value.status_code == 409
    finally:
        _stop_process(owner)


def test_waiter_open_before_owner_death_observes_real_abandonment():
    name = build_mutex_name("material-step", f"abandoned-{os.urandom(16).hex()}")
    owner = _start_mutex_owner(name)
    waiter = WindowsNamedMutex.open_waiter(name)
    try:
        owner.kill()
        owner.wait(timeout=10)
        waiter.acquire(timeout_ms=5_000)
        assert waiter.was_abandoned is True
    finally:
        waiter.release()
        _stop_process(owner)


def test_mutex_handle_is_non_inheritable_and_dacl_is_user_plus_system_only():
    import win32api
    import win32con
    import win32security

    name = build_mutex_name("asset-registry", f"security-{os.urandom(16).hex()}")
    mutex = WindowsNamedMutex.try_acquire(name)
    try:
        flags = win32api.GetHandleInformation(mutex.handle)
        assert flags & win32con.HANDLE_FLAG_INHERIT == 0

        descriptor = win32security.GetKernelObjectSecurity(
            mutex.handle,
            win32security.OWNER_SECURITY_INFORMATION
            | win32security.DACL_SECURITY_INFORMATION,
        )
        owner_sid = win32security.ConvertSidToStringSid(
            descriptor.GetSecurityDescriptorOwner()
        )
        assert owner_sid == current_user_sid_string()
        dacl = descriptor.GetSecurityDescriptorDacl()
        assert dacl is not None
        actual_sids = {
            win32security.ConvertSidToStringSid(dacl.GetAce(index)[2])
            for index in range(dacl.GetAceCount())
        }
        assert actual_sids == {current_user_sid_string(), "S-1-5-18"}
    finally:
        mutex.release()


def test_rejects_squatted_mutex_owned_by_another_principal(
    monkeypatch: pytest.MonkeyPatch,
):
    import win32con
    import win32security

    user_sid = win32security.ConvertStringSidToSid(current_user_sid_string())
    system_sid = win32security.ConvertStringSidToSid("S-1-5-18")
    other_owner = win32security.ConvertStringSidToSid("S-1-5-32-545")
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(win32con.ACL_REVISION, 0x001F0001, user_sid)
    dacl.AddAccessAllowedAce(win32con.ACL_REVISION, 0x001F0001, system_sid)
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(other_owner, False)
    descriptor.SetSecurityDescriptorDacl(True, dacl, False)
    monkeypatch.setattr(
        mutex_module.win32security,
        "GetKernelObjectSecurity",
        lambda _handle, _flags: descriptor,
    )

    with pytest.raises(PermissionError, match="owner"):
        mutex_module._verify_restricted_dacl(object())


def test_non_owner_thread_cannot_release_or_close_owned_mutex():
    name = build_mutex_name("material-step", f"thread-owner-{time.time_ns()}")
    mutex = WindowsNamedMutex.try_acquire(name)
    original_handle = int(mutex.handle)
    wrong_thread_errors: list[BaseException] = []

    def wrong_release():
        try:
            mutex.release()
        except BaseException as exc:
            wrong_thread_errors.append(exc)

    wrong_thread = threading.Thread(target=wrong_release)
    wrong_thread.start()
    wrong_thread.join(timeout=10)
    assert not wrong_thread.is_alive()
    assert len(wrong_thread_errors) == 1
    assert isinstance(wrong_thread_errors[0], RuntimeError)
    assert int(mutex.handle) == original_handle

    busy_results: list[object] = []

    def contend_while_owned():
        try:
            contender = WindowsNamedMutex.try_acquire(name)
        except BaseException as exc:
            busy_results.append(exc)
        else:
            busy_results.append(contender)

    busy_thread = threading.Thread(target=contend_while_owned)
    busy_thread.start()
    busy_thread.join(timeout=10)
    assert not busy_thread.is_alive()
    assert len(busy_results) == 1
    assert isinstance(busy_results[0], WindowsMutexBusy)

    mutex.release()

    success_results: list[object] = []

    def contend_after_release():
        try:
            contender = WindowsNamedMutex.try_acquire(name)
            success_results.append("acquired")
            contender.release()
        except BaseException as exc:
            success_results.append(exc)

    success_thread = threading.Thread(target=contend_after_release)
    success_thread.start()
    success_thread.join(timeout=10)
    assert not success_thread.is_alive()
    assert success_results == ["acquired"]


def test_numeric_mutex_handle_is_not_available_to_spawned_child():
    name = build_mutex_name("asset-registry", f"inherit-{os.urandom(16).hex()}")
    mutex = WindowsNamedMutex.try_acquire(name)
    code = r"""
import sys
import win32event

print("ready", flush=True)
sys.stdin.readline()
try:
    result = win32event.WaitForSingleObject(int(sys.argv[1]), 1000)
except Exception:
    raise SystemExit(0)
if result not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED_0):
    raise SystemExit(0)
try:
    win32event.ReleaseMutex(int(sys.argv[1]))
except Exception:
    raise SystemExit(0)
raise SystemExit(17)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(int(mutex.handle))],
        cwd=BACKEND_ROOT,
        env=_child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        close_fds=False,
    )
    try:
        assert process.stdout is not None
        assert process.stdin is not None
        assert process.stdout.readline().strip() == "ready"
        mutex.release()
        process.stdin.write("go\n")
        process.stdin.flush()
        assert process.wait(timeout=10) == 0
    finally:
        mutex.release()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_release_is_idempotent():
    name = build_mutex_name("asset-registry", f"release-{time.time_ns()}")
    mutex = WindowsNamedMutex.try_acquire(name)

    mutex.release()
    mutex.release()

    reacquired = WindowsNamedMutex.try_acquire(name)
    reacquired.release()
