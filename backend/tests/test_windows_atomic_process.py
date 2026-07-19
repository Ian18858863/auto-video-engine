from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest import mock

import pytest
import win32api
import win32con
import win32event
import win32job
import win32process

from app.pipeline.windows_job_object import WindowsJobObject
from app.pipeline import windows_atomic_process as subject


def _child_environment(work_directory: Path) -> dict[str, str]:
    return {
        "SystemRoot": os.environ["SystemRoot"],
        "WINDIR": os.environ["WINDIR"],
        "TEMP": str(work_directory),
        "TMP": str(work_directory),
        "TMPDIR": str(work_directory),
        "HOME": str(work_directory),
        "USERPROFILE": str(work_directory),
        "LOCALAPPDATA": str(work_directory),
        "APPDATA": str(work_directory),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def _close_launched_process(process: object, job: WindowsJobObject) -> None:
    try:
        if job.active_process_count() != 0:
            job.terminate(0xE0000002)
            assert job.wait_until_empty(5.0)
    finally:
        process.close()  # type: ignore[attr-defined]


def _wait_for_path(path: Path, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.01)


def _wait_for_nonempty_text(path: Path, timeout_seconds: float = 5.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            text = path.read_text(encoding="ascii").strip()
        except (FileNotFoundError, PermissionError):
            text = ""
        if text:
            return text
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for non-empty {path}")
        time.sleep(0.01)


def _assign_test_process_to_outer_job_or_terminate(
    outer_job_handle: object,
    process_handle: object,
) -> None:
    try:
        win32job.AssignProcessToJobObject(outer_job_handle, process_handle)
    except BaseException:
        win32process.TerminateProcess(process_handle, 0x7001)
        if (
            win32event.WaitForSingleObject(process_handle, 5_000)
            != win32event.WAIT_OBJECT_0
        ):
            raise AssertionError("test controller did not exit after Assign failure")
        raise


def _terminate_test_process_if_running(process_handle: object) -> None:
    if (
        win32event.WaitForSingleObject(process_handle, 0)
        == win32event.WAIT_OBJECT_0
    ):
        return
    win32process.TerminateProcess(process_handle, 0x7002)
    if (
        win32event.WaitForSingleObject(process_handle, 5_000)
        != win32event.WAIT_OBJECT_0
    ):
        raise AssertionError("test controller did not exit during fixture cleanup")


def test_outer_job_fixture_terminates_and_waits_when_assign_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int]] = []
    monkeypatch.setattr(
        win32job,
        "AssignProcessToJobObject",
        mock.Mock(side_effect=OSError("assign failed")),
    )
    monkeypatch.setattr(
        win32process,
        "TerminateProcess",
        lambda _handle, code: events.append(("terminate", code)),
    )
    monkeypatch.setattr(
        win32event,
        "WaitForSingleObject",
        lambda _handle, timeout: events.append(("wait", timeout))
        or win32event.WAIT_OBJECT_0,
    )

    with pytest.raises(OSError, match="assign failed"):
        _assign_test_process_to_outer_job_or_terminate(11, 22)

    assert events == [("terminate", 0x7001), ("wait", 5_000)]


def test_real_process_is_in_job_and_cannot_run_before_resume(tmp_path: Path) -> None:
    # When the host is already in an outer Job this also covers supported nested
    # Job creation; on an ordinary Windows runner it still covers atomic launch.
    work_directory = tmp_path / "中文 空格 atomic launch"
    work_directory.mkdir()
    (work_directory / "Program.exe").write_bytes(b"not an executable")
    marker = work_directory / "marker.json"
    application = Path(sys.executable).resolve()
    code = (
        "import json, os, pathlib, sys; "
        "[os.fstat(fd) for fd in (0, 1, 2)]; "
        "pathlib.Path(sys.argv[1]).write_text("
        "json.dumps({'stdio_valid': True}), encoding='utf-8')"
    )
    job = WindowsJobObject.create_unnamed_kill_on_close()
    process = subject.create_suspended_process_in_job(
        application_path=application,
        argv=(str(application), "-I", "-S", "-c", code, str(marker)),
        cwd=work_directory,
        environment=_child_environment(work_directory),
        job_handle=job.handle,
    )
    try:
        assert job.active_process_count() == 1
        assert job.process_ids() == (process.pid,)
        time.sleep(0.15)
        assert not marker.exists()

        assert process.resume() == 1
        assert process.wait_until_exit(5.0)
        assert process.exit_code() == 0
        assert job.wait_until_empty(5.0)
        assert marker.read_text(encoding="utf-8") == '{"stdio_valid": true}'
    finally:
        _close_launched_process(process, job)
        job.close()


class _SpyNativeApi:
    def __init__(self, *, fail_at: str = "") -> None:
        self.fail_at = fail_at
        self.events: list[object] = []
        self._nul_index = 0
        self.created_handles: list[int] = []
        self.resume_result = 1
        self.attribute_state = SimpleNamespace(
            pointer=ctypes.c_void_p(555),
            buffer=object(),
        )

    def _fails(self, label: str) -> bool:
        return label in self.fail_at.split("+")

    def get_handle_flags(self, handle: int) -> int:
        self.events.append(("get_handle_flags", handle))
        return 0

    def create_nul_handle(
        self,
        desired_access: int,
        owner_slot: object | None = None,
    ) -> int:
        self._nul_index += 1
        if self.fail_at == f"nul_{self._nul_index}":
            raise OSError(f"nul_{self._nul_index}")
        handle = 100 + self._nul_index
        self.created_handles.append(handle)
        self.events.append(("create_nul", desired_access, handle))
        if owner_slot is not None:
            owner_slot.adopt(handle)  # type: ignore[attr-defined]
        if self._fails(f"nul_get_flags_{self._nul_index}"):
            raise OSError(f"nul_get_flags_{self._nul_index}")
        return handle

    def initialize_attribute_list(
        self,
        attribute_count: int,
        attribute_owner: object | None = None,
    ) -> object:
        self.events.append(("initialize_attributes", attribute_count))
        if self.fail_at == "initialize_attributes":
            raise OSError("initialize_attributes")
        if attribute_owner is not None:
            attribute_owner.adopt_initialized(  # type: ignore[attr-defined]
                self.attribute_state.pointer,
                self.attribute_state.buffer,
            )
        if self._fails("attribute_after_second_init"):
            raise OSError("attribute_after_second_init")
        return self.attribute_state

    def update_attribute(
        self,
        attribute_state: object,
        attribute: int,
        handles: object,
    ) -> None:
        values = tuple(int(value) for value in handles)  # type: ignore[arg-type]
        label = (
            "update_job"
            if attribute == subject.PROC_THREAD_ATTRIBUTE_JOB_LIST
            else "update_handles"
        )
        self.events.append((label, attribute, values, handles, attribute_state))
        if self.fail_at == label:
            raise OSError(label)

    def create_process(self, **kwargs: object) -> tuple[int, int, int, int]:
        self.events.append(("create_process", kwargs))
        if self.fail_at == "nested_denied":
            raise PermissionError(5, "nested Job creation denied")
        if self.fail_at == "create_process":
            raise OSError("create_process")
        process_information = kwargs.get("process_information")
        if process_information is not None:
            process_information.hProcess = 201
            process_information.hThread = 202
            process_information.dwProcessId = 3001
            process_information.dwThreadId = 3002
        if self.fail_at in {
            "create_raw_conversion",
            "create_process_owner",
            "create_thread_owner",
        }:
            raise OSError(self.fail_at)
        return (201, 202, 3001, 3002)

    def verify_process_in_job(
        self,
        process_handle: int,
        job_handle: int,
        pid: int,
    ) -> None:
        self.events.append(("verify_membership", process_handle, job_handle, pid))
        if self.fail_at.startswith("membership"):
            raise OSError("membership")

    def delete_attribute_list(self, state: object) -> None:
        self.events.append(("delete_attributes", state))
        if self._fails("delete_attributes"):
            raise OSError("delete_attributes")

    def close_handle(self, handle: int) -> None:
        self.events.append(("close", handle))
        if self._fails(f"close_{handle}"):
            raise OSError(f"close_{handle}")

    def resume_thread(self, handle: int) -> int:
        self.events.append(("resume", handle))
        return self.resume_result

    def wait_for_handle(self, handle: int, timeout_milliseconds: int) -> bool:
        self.events.append(("wait", handle, timeout_milliseconds))
        if self._fails("membership_wait_unproven"):
            return False
        return True

    def get_exit_code(self, handle: int) -> int:
        self.events.append(("exit_code", handle))
        return 0

    def terminate_job(self, handle: int, exit_code: int) -> None:
        self.events.append(("terminate_job", handle, exit_code))

    def terminate_process(self, handle: int, exit_code: int) -> None:
        self.events.append(("terminate_process", handle, exit_code))

    def active_process_count(self, handle: int) -> int:
        self.events.append(("active_process_count", handle))
        if self._fails("membership_job_query_unproven"):
            raise OSError("job query")
        return 0


def test_native_spy_receives_exact_two_attributes_and_only_three_nul_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Path(sys.executable).resolve()
    spy = _SpyNativeApi()
    monkeypatch.setattr(subject, "_NATIVE_API", spy, raising=False)

    process = subject.create_suspended_process_in_job(
        application_path=application,
        argv=(str(application), "-I", "-S", "-c", "pass"),
        cwd=tmp_path,
        environment={"UNICODE_VALUE": "中文 空格"},
        job_handle=99,
    )
    create_event = next(event for event in spy.events if event[0] == "create_process")
    create_arguments = create_event[1]
    updates = [event for event in spy.events if event[0] in {"update_job", "update_handles"}]

    assert ("initialize_attributes", 2) in spy.events
    assert ("verify_membership", 201, 99, 3001) in spy.events
    assert [(event[1], event[2]) for event in updates] == [
        (subject.PROC_THREAD_ATTRIBUTE_JOB_LIST, (99,)),
        (subject.PROC_THREAD_ATTRIBUTE_HANDLE_LIST, (101, 102, 103)),
    ]
    assert len({101, 102, 103}) == 3
    assert create_arguments["inherit_handles"] is True
    assert create_arguments["creation_flags"] == (
        subject.CREATE_SUSPENDED
        | subject.CREATE_NO_WINDOW
        | subject.CREATE_UNICODE_ENVIRONMENT
        | subject.EXTENDED_STARTUPINFO_PRESENT
    )
    startup = create_arguments["startup_info"].StartupInfo
    assert startup.dwFlags & subject.STARTF_USESTDHANDLES
    assert tuple(
        int(handle)
        for handle in (startup.hStdInput, startup.hStdOutput, startup.hStdError)
    ) == (101, 102, 103)
    assert isinstance(create_arguments["command_line"], ctypes.Array)
    assert isinstance(create_arguments["environment_block"], ctypes.Array)
    assert create_arguments["environment_block"].value.startswith("UNICODE_VALUE=中文 空格")
    cleanup_events = [
        event for event in spy.events if event[0] in {"delete_attributes", "close"}
    ]
    assert cleanup_events[0][0] == "delete_attributes"
    assert cleanup_events[0][1] is process._resources.attribute_list
    assert [event[:2] for event in cleanup_events[1:4]] == [
        ("close", 103),
        ("close", 102),
        ("close", 101),
    ]
    assert not any(event[0] == "close" and event[1] in {99, 201, 202} for event in spy.events)

    process.close()
    assert [event for event in spy.events if event[0] == "close"][-2:] == [
        ("close", 202),
        ("close", 201),
    ]


@pytest.mark.parametrize(
    ("fail_at", "expected_created", "attribute_was_initialized"),
    [
        ("nul_1", (), False),
        ("nul_2", (101,), False),
        ("nul_3", (101, 102), False),
        ("initialize_attributes", (101, 102, 103), False),
        ("update_job", (101, 102, 103), True),
        ("update_handles", (101, 102, 103), True),
        ("create_process", (101, 102, 103), True),
    ],
)
def test_native_failures_clean_every_created_resource_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_at: str,
    expected_created: tuple[int, ...],
    attribute_was_initialized: bool,
) -> None:
    application = Path(sys.executable).resolve()
    spy = _SpyNativeApi(fail_at=fail_at)
    monkeypatch.setattr(subject, "_NATIVE_API", spy, raising=False)

    with pytest.raises(OSError, match=fail_at):
        subject.create_suspended_process_in_job(
            application_path=application,
            argv=(str(application), "-c", "pass"),
            cwd=tmp_path,
            environment={},
            job_handle=99,
        )

    closed = [event[1] for event in spy.events if event[0] == "close"]
    assert closed == list(reversed(expected_created))
    assert len(closed) == len(set(closed))
    assert sum(event[0] == "delete_attributes" for event in spy.events) == int(
        attribute_was_initialized
    )
    assert not any(event[0] == "terminate_job" for event in spy.events)


def test_invalid_application_fails_before_any_native_resource_is_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _SpyNativeApi()
    monkeypatch.setattr(subject, "_NATIVE_API", spy, raising=False)

    with pytest.raises(ValueError, match="absolute.*D"):
        subject.create_suspended_process_in_job(
            application_path=Path("C:/forbidden/python.exe"),
            argv=("C:/forbidden/python.exe", "-c", "pass"),
            cwd=tmp_path,
            environment={},
            job_handle=99,
        )

    assert spy.events == []


def test_aggregate_owner_factory_runs_before_any_native_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Path(sys.executable).resolve()
    spy = _SpyNativeApi()
    monkeypatch.setattr(subject, "_NATIVE_API", spy)
    monkeypatch.setattr(
        subject,
        "_new_launch_resource_owner",
        mock.Mock(side_effect=RuntimeError("aggregate owner construction")),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="aggregate owner construction"):
        subject.create_suspended_process_in_job(
            application_path=application,
            argv=(str(application), "-c", "pass"),
            cwd=tmp_path,
            environment={},
            job_handle=99,
        )

    assert spy.events == []


def test_owner_prebuilds_all_stable_handoff_envelopes() -> None:
    owner = subject._new_launch_resource_owner(_SpyNativeApi(), 99)

    assert owner.resource_error_envelope.resource_owner is owner
    assert owner.containment_error_envelope.resource_owner is owner
    assert owner.emergency_error_envelope.resource_owner is owner
    assert isinstance(
        owner.containment_error_envelope,
        subject.WindowsAtomicProcessContainmentError,
    )
    assert str(owner.emergency_error_envelope) == (
        "atomic launch failed while preparing its resource handoff"
    )


def test_error_configuration_or_cleanup_hook_failure_raises_prebuilt_emergency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Path(sys.executable).resolve()
    spy = _SpyNativeApi(fail_at="nul_get_flags_1")
    captured: dict[str, object] = {}
    original_factory = subject._new_launch_resource_owner

    def capture_owner(native_api: object, job_handle: int) -> object:
        owner = original_factory(native_api, job_handle)
        captured["owner"] = owner
        return owner

    monkeypatch.setattr(subject, "_NATIVE_API", spy)
    monkeypatch.setattr(subject, "_new_launch_resource_owner", capture_owner)
    monkeypatch.setattr(
        subject.LaunchResourceOwner,
        "cleanup_all_stop_on_failure",
        mock.Mock(side_effect=OSError("cleanup hook failed")),
    )
    monkeypatch.setattr(
        subject,
        "_configure_error_envelope",
        mock.Mock(side_effect=OSError("error configuration failed")),
        raising=False,
    )

    with pytest.raises(subject.WindowsAtomicProcessResourceError) as caught:
        subject.create_suspended_process_in_job(
            application_path=application,
            argv=(str(application), "-c", "pass"),
            cwd=tmp_path,
            environment={},
            job_handle=99,
        )

    owner = captured["owner"]
    assert caught.value is owner.emergency_error_envelope
    assert caught.value.resource_owner is owner
    assert caught.value.cleanup_errors == ()


def test_native_create_nul_registers_raw_before_fallible_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_handle = 777
    native = object.__new__(subject._NativeWindowsApi)
    native._create_file = mock.Mock(return_value=raw_handle)
    native._close_handle = mock.Mock(return_value=False)
    owner_slot = subject._OwnedNativeHandle(native)
    monkeypatch.setattr(
        subject,
        "_handle_to_int",
        mock.Mock(side_effect=OSError("persistent raw-handle conversion failure")),
    )

    with pytest.raises(OSError, match="persistent raw-handle conversion failure"):
        native.create_nul_handle(subject._GENERIC_READ, owner_slot)

    assert native._close_handle.call_count == 0
    assert owner_slot.live is True
    assert owner_slot.value == raw_handle


def test_failed_raw_nul_close_returns_typed_retryable_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Path(sys.executable).resolve()
    raw_handle = 777
    native = object.__new__(subject._NativeWindowsApi)
    native._create_file = mock.Mock(return_value=raw_handle)
    native._close_handle = mock.Mock(return_value=False)
    native.get_handle_flags = mock.Mock(return_value=0)
    original = subject._handle_to_int

    def fail_raw_conversion(value: object) -> int:
        if getattr(value, "value", value) == raw_handle:
            raise OSError("persistent raw-handle conversion failure")
        return original(value)

    monkeypatch.setattr(subject, "_NATIVE_API", native)
    monkeypatch.setattr(subject, "_handle_to_int", fail_raw_conversion)

    with pytest.raises(subject.WindowsAtomicProcessResourceError) as caught:
        subject.create_suspended_process_in_job(
            application_path=application,
            argv=(str(application), "-c", "pass"),
            cwd=tmp_path,
            environment={},
            job_handle=99,
        )

    owner = caught.value.resource_owner
    assert owner.live_handle_values() == (raw_handle,)
    assert native._close_handle.call_count == 1

    native._close_handle.return_value = True
    assert owner.retry_failed_launch_cleanup() is True
    assert native._close_handle.call_count == 2
    assert owner.state is subject.LaunchResourceState.CLOSED


@pytest.mark.parametrize(
    ("fail_at", "expected_live_handles", "attribute_live"),
    [
        ("nul_get_flags_1+close_101", (101,), False),
        ("attribute_after_second_init+delete_attributes", (101, 102, 103), True),
    ],
)
def test_precreate_cleanup_failure_retains_transferable_owner_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_at: str,
    expected_live_handles: tuple[int, ...],
    attribute_live: bool,
) -> None:
    application = Path(sys.executable).resolve()
    spy = _SpyNativeApi(fail_at=fail_at)
    monkeypatch.setattr(subject, "_NATIVE_API", spy)

    with pytest.raises(subject.WindowsAtomicProcessResourceError) as caught:
        subject.create_suspended_process_in_job(
            application_path=application,
            argv=(str(application), "-c", "pass"),
            cwd=tmp_path,
            environment={},
            job_handle=99,
        )

    owner = caught.value.resource_owner
    assert owner.live_handle_values() == expected_live_handles
    assert owner.attribute_list_live is attribute_live
    assert owner.process_created is False

    spy.fail_at = ""
    assert owner.retry_failed_launch_cleanup() is True
    assert owner.live_handle_values() == ()
    assert owner.attribute_list_live is False
    assert owner.state is subject.LaunchResourceState.CLOSED


@pytest.mark.parametrize(
    "fail_at",
    ["create_raw_conversion", "create_process_owner", "create_thread_owner"],
)
def test_create_success_conversion_and_owner_failures_contain_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_at: str,
) -> None:
    application = Path(sys.executable).resolve()
    spy = _SpyNativeApi(fail_at=fail_at)
    monkeypatch.setattr(subject, "_NATIVE_API", spy)

    with pytest.raises(OSError, match=fail_at):
        subject.create_suspended_process_in_job(
            application_path=application,
            argv=(str(application), "-c", "pass"),
            cwd=tmp_path,
            environment={},
            job_handle=99,
        )

    labels = [event[0] for event in spy.events]
    assert labels.index("terminate_job") < labels.index("terminate_process")
    assert labels.index("terminate_process") < labels.index("wait")
    assert labels.index("wait") < labels.index("active_process_count")
    assert labels.index("active_process_count") < labels.index("delete_attributes")
    assert labels.index("wait") < max(
        index
        for index, event in enumerate(spy.events)
        if event[:2] == ("close", 201)
    )
    assert ("close", 202) in spy.events
    assert ("close", 201) in spy.events


@pytest.mark.parametrize(
    "fail_at",
    ["membership_wait_unproven", "membership_job_query_unproven"],
)
def test_unproven_post_create_containment_transfers_all_live_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_at: str,
) -> None:
    application = Path(sys.executable).resolve()
    spy = _SpyNativeApi(fail_at=fail_at)
    monkeypatch.setattr(subject, "_NATIVE_API", spy)

    with pytest.raises(subject.WindowsAtomicProcessContainmentError) as caught:
        subject.create_suspended_process_in_job(
            application_path=application,
            argv=(str(application), "-c", "pass"),
            cwd=tmp_path,
            environment={},
            job_handle=99,
        )

    owner = caught.value.resource_owner
    assert owner.process_created is True
    assert owner.membership_proven is False
    assert owner.live_handle_values() == (101, 102, 103, 201, 202)
    assert owner.attribute_list_live is True
    assert not any(event[0] in {"delete_attributes", "close"} for event in spy.events)
    assert ("terminate_process", 201, 0xE0000003) in spy.events
    assert ("terminate_job", 99, 0xE0000003) in spy.events
    assert "WindowsAtomicProcessContainmentError" in subject.__all__

    transferred = owner.transfer_to_quarantine()
    assert transferred is owner
    assert owner.state is subject.LaunchResourceState.TRANSFERRED_FOR_QUARANTINE


def test_persistent_process_information_conversion_failure_returns_typed_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Path(sys.executable).resolve()
    spy = _SpyNativeApi(fail_at="create_raw_conversion")
    monkeypatch.setattr(subject, "_NATIVE_API", spy)
    original = subject._handle_to_int

    def fail_process_information_conversion(value: object) -> int:
        raw = getattr(value, "value", value)
        if raw in {201, 202}:
            raise OSError("persistent PI conversion failure")
        return original(value)

    monkeypatch.setattr(subject, "_handle_to_int", fail_process_information_conversion)

    with pytest.raises(subject.WindowsAtomicProcessContainmentError) as caught:
        subject.create_suspended_process_in_job(
            application_path=application,
            argv=(str(application), "-c", "pass"),
            cwd=tmp_path,
            environment={},
            job_handle=99,
        )

    owner = caught.value.resource_owner
    assert owner.process_created is True
    assert owner.live_handle_values() == (101, 102, 103, 201, 202)
    assert owner.attribute_list_live is True
    assert not any(event[0] in {"delete_attributes", "close"} for event in spy.events)


def test_second_attribute_init_success_precedes_fallible_registration_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Path(sys.executable).resolve()
    spy = _SpyNativeApi()
    monkeypatch.setattr(subject, "_NATIVE_API", spy)
    monkeypatch.setattr(
        subject._OwnedAttributeList,
        "mark_initialized",
        mock.Mock(side_effect=OSError("attribute registration hook")),
    )

    with pytest.raises(OSError, match="attribute registration hook"):
        subject.create_suspended_process_in_job(
            application_path=application,
            argv=(str(application), "-c", "pass"),
            cwd=tmp_path,
            environment={},
            job_handle=99,
        )

    assert sum(event[0] == "delete_attributes" for event in spy.events) == 1
    assert [event for event in spy.events if event[0] == "close"] == [
        ("close", 103),
        ("close", 102),
        ("close", 101),
    ]


def test_cleanup_adopts_raw_process_information_before_marking_owner_closed() -> None:
    spy = _SpyNativeApi()
    owner = subject._new_launch_resource_owner(spy, 99)
    owner.process_information.hProcess = 201
    owner.process_information.hThread = 202
    owner.process_information.dwProcessId = 3001
    owner.process_information.dwThreadId = 3002
    owner.state = subject.LaunchResourceState.CONTAINMENT_PROVEN
    owner.root_signaled = True
    owner.job_empty_proven = True

    assert owner.cleanup_all_stop_on_failure() == []

    assert [event for event in spy.events if event[0] == "close"] == [
        ("close", 202),
        ("close", 201),
    ]
    assert not owner.process_information.hProcess
    assert not owner.process_information.hThread
    assert owner.process_created is False
    assert owner.state is subject.LaunchResourceState.CLOSED


def test_quarantine_transfer_is_single_use() -> None:
    owner = subject._new_launch_resource_owner(_SpyNativeApi(), 99)

    assert owner.transfer_to_quarantine() is owner
    with pytest.raises(RuntimeError, match="already transferred"):
        owner.transfer_to_quarantine()


def test_nested_create_access_denied_fails_closed_without_breakaway_or_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Path(sys.executable).resolve()
    spy = _SpyNativeApi(fail_at="nested_denied")
    monkeypatch.setattr(subject, "_NATIVE_API", spy)

    with pytest.raises(PermissionError, match="nested Job creation denied"):
        subject.create_suspended_process_in_job(
            application_path=application,
            argv=(str(application), "-c", "pass"),
            cwd=tmp_path,
            environment={},
            job_handle=99,
        )

    create_arguments = next(
        event[1] for event in spy.events if event[0] == "create_process"
    )
    assert not create_arguments["creation_flags"] & 0x01000000  # BREAKAWAY_FROM_JOB
    assert [event for event in spy.events if event[0] == "close"] == [
        ("close", 103),
        ("close", 102),
        ("close", 101),
    ]
    assert not hasattr(subject, "AssignProcessToJobObject")


def test_post_create_cleanup_failure_terminates_job_and_closes_returned_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Path(sys.executable).resolve()
    spy = _SpyNativeApi(fail_at="delete_attributes")
    monkeypatch.setattr(subject, "_NATIVE_API", spy)

    with pytest.raises(subject.WindowsAtomicProcessResourceError) as caught:
        subject.create_suspended_process_in_job(
            application_path=application,
            argv=(str(application), "-c", "pass"),
            cwd=tmp_path,
            environment={},
            job_handle=99,
        )

    owner = caught.value.resource_owner
    labels = [event[0] for event in spy.events]
    first_delete = labels.index("delete_attributes")
    terminate = labels.index("terminate_job")
    assert first_delete < terminate
    assert not any(label == "close" for label in labels[first_delete + 1 : terminate])
    assert labels.index("wait") < labels.index("active_process_count")
    assert owner.attribute_list_live is True
    assert owner.live_handle_values() == (101, 102, 103, 201, 202)

    spy.fail_at = ""
    assert owner.retry_failed_launch_cleanup() is True
    assert owner.attribute_list_live is False
    assert owner.state is subject.LaunchResourceState.CLOSED
    assert labels.index("wait") < max(
        index
        for index, event in enumerate(spy.events)
        if event[:2] == ("close", 201)
    )
    closes = [event for event in spy.events if event[0] == "close"]
    assert closes == [
        ("close", 103),
        ("close", 102),
        ("close", 101),
        ("close", 202),
        ("close", 201),
    ]
    assert len(closes) == len({event[1] for event in closes})


def test_resume_is_single_use_and_requires_previous_suspend_count_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Path(sys.executable).resolve()
    spy = _SpyNativeApi()
    monkeypatch.setattr(subject, "_NATIVE_API", spy)
    process = subject.create_suspended_process_in_job(
        application_path=application,
        argv=(str(application), "-c", "pass"),
        cwd=tmp_path,
        environment={},
        job_handle=99,
    )
    try:
        spy.resume_result = 0
        with pytest.raises(RuntimeError, match="suspend count.*1"):
            process.resume()
        with pytest.raises(RuntimeError, match="already attempted"):
            process.resume()
        assert [event for event in spy.events if event[0] == "resume"] == [
            ("resume", 202)
        ]
    finally:
        process.close()


def test_root_exit_does_not_hide_grandchild_and_termination_prevents_late_write(
    tmp_path: Path,
) -> None:
    work_directory = tmp_path / "grandchild containment"
    work_directory.mkdir()
    pid_marker = work_directory / "grandchild.pid"
    late_marker = work_directory / "late-write.txt"
    application = Path(sys.executable).resolve()
    grandchild_code = (
        "import pathlib, sys, time; "
        "time.sleep(1.0); pathlib.Path(sys.argv[1]).write_text('late', encoding='utf-8')"
    )
    root_code = (
        "import pathlib, subprocess, sys; "
        "child=subprocess.Popen([sys.executable, '-I', '-S', '-c', sys.argv[1], sys.argv[2]]); "
        "pathlib.Path(sys.argv[3]).write_text(str(child.pid), encoding='ascii')"
    )
    job = WindowsJobObject.create_unnamed_kill_on_close()
    process = subject.create_suspended_process_in_job(
        application_path=application,
        argv=(
            str(application),
            "-I",
            "-S",
            "-c",
            root_code,
            grandchild_code,
            str(late_marker),
            str(pid_marker),
        ),
        cwd=work_directory,
        environment=_child_environment(work_directory),
        job_handle=job.handle,
    )
    try:
        assert process.resume() == 1
        assert process.wait_until_exit(5.0)
        grandchild_pid = int(_wait_for_nonempty_text(pid_marker))

        remaining_pids = job.process_ids()
        assert job.active_process_count() == len(remaining_pids)
        assert remaining_pids
        assert process.pid not in remaining_pids
        # The venv redirector can make the PID returned by Popen a short-lived
        # launcher. The invariant is the still-live descendant tree, not one PID.
        assert grandchild_pid > 0

        job.terminate(0xE0000004)
        assert job.wait_until_empty(5.0)
        time.sleep(1.15)
        assert not late_marker.exists()
    finally:
        _close_launched_process(process, job)
        job.close()


def test_parallel_launches_exclude_inheritable_canaries_and_job_handles(
    tmp_path: Path,
) -> None:
    application = Path(sys.executable).resolve()
    canaries = [win32event.CreateEvent(None, True, False, None) for _ in range(2)]
    for canary in canaries:
        win32api.SetHandleInformation(
            canary,
            win32con.HANDLE_FLAG_INHERIT,
            win32con.HANDLE_FLAG_INHERIT,
        )
        assert win32api.GetHandleInformation(canary) & win32con.HANDLE_FLAG_INHERIT
    jobs = [WindowsJobObject.create_unnamed_kill_on_close() for _ in range(2)]
    processes: list[object] = []
    child_code = (
        "import ctypes, json, pathlib, sys; "
        "from ctypes import wintypes; k=ctypes.WinDLL('kernel32', use_last_error=True); "
        "handles=[int(raw) for raw in sys.argv[1:-1]]; "
        "se=k.SetEvent; se.argtypes=[wintypes.HANDLE]; se.restype=wintypes.BOOL; "
        "event_calls=[bool(se(wintypes.HANDLE(raw))) for raw in handles[:2]]; "
        "q=k.QueryInformationJobObject; q.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]; q.restype=wintypes.BOOL; "
        "job_queries=[bool(q(wintypes.HANDLE(raw),1,buf,ctypes.sizeof(buf),ctypes.byref(length))) for raw in handles[2:] for buf in [ctypes.create_string_buffer(128)] for length in [wintypes.DWORD()]]; "
        "pathlib.Path(sys.argv[-1]).write_text(json.dumps({'event_calls':event_calls,'job_queries':job_queries}), encoding='utf-8')"
    )

    def launch(index: int) -> object:
        work_directory = tmp_path / f"parallel-{index}"
        work_directory.mkdir()
        marker = work_directory / "inheritance.json"
        excluded = tuple(int(handle) for handle in [*canaries, *(job.handle for job in jobs)])
        return subject.create_suspended_process_in_job(
            application_path=application,
            argv=(
                str(application),
                "-I",
                "-S",
                "-c",
                child_code,
                *(str(handle) for handle in excluded),
                str(marker),
            ),
            cwd=work_directory,
            environment=_child_environment(work_directory),
            job_handle=jobs[index].handle,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            processes = list(executor.map(launch, range(2)))
        for index, process in enumerate(processes):
            assert jobs[index].process_ids() == (process.pid,)
            assert process.resume() == 1
        for index, process in enumerate(processes):
            assert process.wait_until_exit(5.0)
            assert jobs[index].wait_until_empty(5.0)
            marker = tmp_path / f"parallel-{index}" / "inheritance.json"
            inheritance = json.loads(marker.read_text(encoding="utf-8"))
            assert inheritance["job_queries"] == [False, False]
            assert all(
                win32event.WaitForSingleObject(canary, 0) == win32event.WAIT_TIMEOUT
                for canary in canaries
            )
    finally:
        for index, process in enumerate(processes):
            _close_launched_process(process, jobs[index])
        for job in jobs:
            job.close()
        for canary in canaries:
            canary.Close()


def test_controller_exit_closes_last_job_handle_and_kills_worker(
    tmp_path: Path,
) -> None:
    work_directory = tmp_path / "owner-exit"
    work_directory.mkdir()
    ready = work_directory / "ready.pid"
    release = work_directory / "release-controller"
    late_marker = work_directory / "late-owner-write.txt"
    backend_root = Path(__file__).resolve().parents[1]
    application = Path(sys.executable).resolve()
    controller_code = (
        "import os, pathlib, sys, time; "
        "sys.path.insert(0, sys.argv[1]); "
        "from app.pipeline.windows_job_object import WindowsJobObject; "
        "from app.pipeline.windows_atomic_process import create_suspended_process_in_job; "
        "work=pathlib.Path(sys.argv[2]); ready=pathlib.Path(sys.argv[3]); "
        "release=pathlib.Path(sys.argv[4]); late=pathlib.Path(sys.argv[5]); app=pathlib.Path(sys.argv[6]); "
        "job=WindowsJobObject.create_unnamed_kill_on_close(); "
        "env={'SystemRoot':os.environ['SystemRoot'],'WINDIR':os.environ['WINDIR'],"
        "'TEMP':str(work),'TMP':str(work),'TMPDIR':str(work),'HOME':str(work),"
        "'USERPROFILE':str(work),'LOCALAPPDATA':str(work),'APPDATA':str(work),"
        "'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'}; "
        "code=\"import pathlib,sys,time; time.sleep(1.0); pathlib.Path(sys.argv[1]).write_text('late',encoding='utf-8')\"; "
        "child=create_suspended_process_in_job(application_path=app,argv=(str(app),'-I','-S','-c',code,str(late)),cwd=work,environment=env,job_handle=job.handle); "
        "assert child.resume()==1; "
        "f=ready.open('w',encoding='ascii'); f.write(str(child.pid)); f.flush(); os.fsync(f.fileno()); f.close(); "
        "deadline=time.monotonic()+5.0; "
        "exec(\"while not release.exists() and time.monotonic() < deadline:\\n    time.sleep(0.01)\"); "
        "os._exit(0)"
    )
    controller = subprocess.Popen(
        [
            str(application),
            "-c",
            controller_code,
            str(backend_root),
            str(work_directory),
            str(ready),
            str(release),
            str(late_marker),
            str(application),
        ],
        cwd=backend_root,
        env=_child_environment(work_directory),
        close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    worker_handle = None
    try:
        worker_pid = int(_wait_for_nonempty_text(ready))
        worker_handle = win32api.OpenProcess(
            win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            worker_pid,
        )
        release.write_text("exit", encoding="ascii")
        assert controller.wait(timeout=5.0) == 0
        assert (
            win32event.WaitForSingleObject(worker_handle, 5_000)
            == win32event.WAIT_OBJECT_0
        )
        time.sleep(1.15)
        assert not late_marker.exists()
    finally:
        if controller.poll() is None:
            controller.kill()
            controller.wait(timeout=5.0)
        if worker_handle is not None:
            worker_handle.Close()


def test_real_restricted_outer_job_never_allows_an_escaped_child(
    tmp_path: Path,
) -> None:
    work_directory = tmp_path / "restricted outer job"
    work_directory.mkdir()
    result_marker = work_directory / "nested-result.txt"
    unexpected_child_marker = work_directory / "unexpected-child.txt"
    backend_root = Path(__file__).resolve().parents[1]
    application = Path(sys.executable).resolve()
    controller_code = (
        "import pathlib, sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "from app.pipeline.windows_job_object import WindowsJobObject; "
        "from app.pipeline.windows_atomic_process import create_suspended_process_in_job; "
        "work=pathlib.Path(sys.argv[2]); result=pathlib.Path(sys.argv[3]); "
        "child_marker=pathlib.Path(sys.argv[4]); app=pathlib.Path(sys.argv[5]); "
        "job=WindowsJobObject.create_unnamed_kill_on_close(); "
        "env={'SystemRoot':__import__('os').environ['SystemRoot'],'WINDIR':__import__('os').environ['WINDIR'],"
        "'TEMP':str(work),'TMP':str(work),'TMPDIR':str(work),'HOME':str(work),"
        "'USERPROFILE':str(work),'LOCALAPPDATA':str(work),'APPDATA':str(work),"
        "'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'}; "
        "code=\"import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('escaped',encoding='utf-8')\"; "
        "exec(\"try:\\n"
        "    child=create_suspended_process_in_job(application_path=app,argv=(str(app),'-I','-S','-c',code,str(child_marker)),cwd=work,environment=env,job_handle=job.handle)\\n"
        "except BaseException as exc:\\n"
        "    result.write_text(type(exc).__name__+':'+str(exc),encoding='utf-8')\\n"
        "else:\\n"
        "    result.write_text('nested_supported',encoding='utf-8')\\n"
        "    job.terminate(0xE0000005)\\n"
        "    job.wait_until_empty(5.0)\\n"
        "    child.close()\")"
    )
    outer_job = WindowsJobObject.create_unnamed_kill_on_close()
    win32job.SetInformationJobObject(
        outer_job.handle,
        win32job.JobObjectBasicUIRestrictions,
        {"UIRestrictionsClass": win32job.JOB_OBJECT_UILIMIT_HANDLES},
    )
    process_handle = None
    thread_handle = None
    try:
        command_line = subprocess.list2cmdline(
            [
                str(application),
                "-c",
                controller_code,
                str(backend_root),
                str(work_directory),
                str(result_marker),
                str(unexpected_child_marker),
                str(application),
            ]
        )
        process_handle, thread_handle, _pid, _tid = win32process.CreateProcess(
            str(application),
            command_line,
            None,
            None,
            False,
            win32con.CREATE_SUSPENDED
            | win32con.CREATE_NO_WINDOW
            | win32con.CREATE_UNICODE_ENVIRONMENT,
            _child_environment(work_directory),
            str(backend_root),
            win32process.STARTUPINFO(),
        )
        # Test-only suspended assignment constructs the hostile outer-job fixture.
        # Production launch code contains no AssignProcessToJobObject fallback.
        _assign_test_process_to_outer_job_or_terminate(
            outer_job.handle,
            process_handle,
        )
        assert win32process.ResumeThread(thread_handle) == 1
        assert (
            win32event.WaitForSingleObject(process_handle, 5_000)
            == win32event.WAIT_OBJECT_0
        )
        _wait_for_path(result_marker)
        result = result_marker.read_text(encoding="utf-8")

        assert (
            result == "nested_supported"
            or "denied" in result.casefold()
            or "error 5" in result.casefold()
        )
        assert not unexpected_child_marker.exists()
        assert outer_job.wait_until_empty(5.0)
    finally:
        if outer_job.active_process_count() != 0:
            outer_job.terminate(0xE0000006)
            assert outer_job.wait_until_empty(5.0)
        if thread_handle is not None:
            thread_handle.Close()
        if process_handle is not None:
            _terminate_test_process_if_running(process_handle)
            process_handle.Close()
        outer_job.close()
