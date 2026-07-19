from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.core import windows_storage_policy as policy
from app.core.windows_storage_policy import (
    ApprovedPhysicalDirectory,
    StoragePolicyError,
    approve_physical_d_write_root,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP_ROOT = REPO_ROOT / ".test-tmp" / "task7-storage-policy"


pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="the production storage policy is Windows-only"
)


@pytest.fixture
def d_test_dir() -> Path:
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TEST_TMP_ROOT / f"case-{os.getpid()}-{os.urandom(8).hex()}"
    path.mkdir()
    try:
        yield path
    finally:
        if path.exists():
            for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if child.is_symlink():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
                else:
                    child.unlink()
            path.rmdir()


@pytest.mark.parametrize(
    "raw_path",
    [
        r"C:\never-touch",
        r"E:\never-touch",
        r"\\server\share\never-touch",
        r"relative\never-touch",
    ],
)
def test_rejects_non_d_and_non_absolute_paths_before_windows_syscalls(
    raw_path: str, monkeypatch: pytest.MonkeyPatch
):
    def unexpected_syscall(*_args, **_kwargs):
        raise AssertionError("a rejected path reached a Windows filesystem syscall")

    monkeypatch.setattr(policy, "_get_drive_type", unexpected_syscall)

    with pytest.raises(StoragePolicyError) as exc_info:
        approve_physical_d_write_root(Path(raw_path))

    assert exc_info.value.code == "storage_path_not_physical_d"


def test_non_windows_platform_fails_closed(d_test_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(policy, "_platform_is_windows", lambda: False)

    with pytest.raises(StoragePolicyError) as exc_info:
        approve_physical_d_write_root(d_test_dir)

    assert exc_info.value.code == "storage_policy_unsupported"


@pytest.mark.parametrize(
    "raw_path",
    [
        "D:\\safe\\.\\child",
        "D:\\safe\\..\\child",
        "D:\\safe\\trailing.\\child",
        "D:\\safe\\trailing \\child",
        "D:\\safe\\nul\x00tail",
        "D:\\safe\\CON",
        "D:\\safe\\con.txt",
        "D:\\safe\\PRN.log",
        "D:\\safe\\AUX",
        "D:\\safe\\NUL.md",
        "D:\\safe\\COM1",
        "D:\\safe\\com9.txt",
        "D:\\safe\\LPT1",
        "D:\\safe\\lpt9.txt",
        "D:\\safe\\COM¹",
        "D:\\safe\\com².txt",
        "D:\\safe\\COM³.log",
        "D:\\safe\\LPT¹",
        "D:\\safe\\lpt².txt",
        "D:\\safe\\LPT³.log",
        "D:\\safe\\CON .txt",
        "D:\\safe\\COM1 .log",
        "D:\\safe\\LPT¹ .txt",
    ],
)
def test_rejects_ambiguous_windows_components_before_syscalls(
    raw_path: str, monkeypatch: pytest.MonkeyPatch
):
    def unexpected_syscall(*_args, **_kwargs):
        raise AssertionError("an ambiguous path reached a Windows filesystem syscall")

    monkeypatch.setattr(policy, "_get_drive_type", unexpected_syscall)

    with pytest.raises(StoragePolicyError) as exc_info:
        approve_physical_d_write_root(raw_path)  # type: ignore[arg-type]

    assert exc_info.value.code == "storage_path_ambiguous"


@pytest.mark.parametrize("control_code", range(1, 32))
def test_rejects_every_c0_control_character_before_syscalls(
    control_code: int, monkeypatch: pytest.MonkeyPatch
):
    def unexpected_syscall(*_args, **_kwargs):
        raise AssertionError("a control character reached a Windows filesystem syscall")

    monkeypatch.setattr(policy, "_get_drive_type", unexpected_syscall)

    with pytest.raises(StoragePolicyError) as exc_info:
        approve_physical_d_write_root(
            f"D:\\safe\\bad{chr(control_code)}component"
        )

    assert exc_info.value.code == "storage_path_ambiguous"


def test_rejects_non_fixed_d_drive(d_test_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(policy, "_get_drive_type", lambda _root: policy.DRIVE_REMOVABLE)

    with pytest.raises(StoragePolicyError) as exc_info:
        approve_physical_d_write_root(d_test_dir)

    assert exc_info.value.code == "storage_drive_not_fixed"


def test_rejects_subst_or_redirected_d_drive(
    d_test_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        policy,
        "_query_dos_device",
        lambda _drive: (r"\??\C:\redirected-project",),
    )

    with pytest.raises(StoragePolicyError) as exc_info:
        approve_physical_d_write_root(d_test_dir)

    assert exc_info.value.code == "storage_drive_redirected"


@pytest.mark.parametrize("forged_value", ["D:", None])
def test_trusted_system_volume_discovery_ignores_systemdrive_environment(
    forged_value: str | None, monkeypatch: pytest.MonkeyPatch
):
    if forged_value is None:
        monkeypatch.delenv("SystemDrive", raising=False)
    else:
        monkeypatch.setenv("SystemDrive", forged_value)
    monkeypatch.setattr(
        policy.win32api, "GetSystemDirectory", lambda: r"C:\Windows\System32"
    )
    monkeypatch.setattr(
        policy.win32file, "GetVolumePathName", lambda _path: "C:\\"
    )

    assert policy._get_trusted_system_volume_root() == "C:\\"


def test_rejects_d_when_trusted_system_api_reports_d_as_system_volume(
    d_test_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        policy.win32api, "GetSystemDirectory", lambda: r"D:\Windows\System32"
    )
    monkeypatch.setattr(
        policy.win32file, "GetVolumePathName", lambda _path: "D:\\"
    )

    with pytest.raises(StoragePolicyError) as exc_info:
        approve_physical_d_write_root(d_test_dir)

    assert exc_info.value.code == "storage_system_volume_forbidden"


@pytest.mark.parametrize("failure_mode", ["empty", "exception", "empty_root"])
def test_system_volume_discovery_failure_fails_closed(
    d_test_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
):
    if failure_mode == "empty":
        monkeypatch.setattr(policy.win32api, "GetSystemDirectory", lambda: "")
    elif failure_mode == "exception":
        def fail_get_system_directory():
            raise OSError("simulated trusted API failure")

        monkeypatch.setattr(
            policy.win32api, "GetSystemDirectory", fail_get_system_directory
        )
    else:
        monkeypatch.setattr(
            policy.win32api, "GetSystemDirectory", lambda: r"C:\Windows\System32"
        )
        monkeypatch.setattr(
            policy.win32file, "GetVolumePathName", lambda _path: ""
        )

    with pytest.raises(StoragePolicyError) as exc_info:
        approve_physical_d_write_root(d_test_dir)

    assert exc_info.value.code == "storage_system_volume_unknown"


def test_approves_real_d_directory_and_uses_stable_volume_guid_path(d_test_dir: Path):
    approved = approve_physical_d_write_root(d_test_dir)
    try:
        approved.verify()
        assert approved.path == d_test_dir.resolve()
        assert approved.stable_volume_path.startswith(r"\\?\Volume{")
        assert approved.stable_volume_path.endswith(d_test_dir.name)
        assert not approved.stable_volume_path.casefold().startswith(r"\\?\d:")
        assert approved.stable_volume_path.startswith(approved.identity.volume_guid)
        assert approved.identity.volume_serial > 0
        assert approved.identity.file_index > 0
    finally:
        approved.close()


def test_rejects_real_junction_inside_d_test_tree(d_test_dir: Path):
    target = d_test_dir / "junction-target"
    link = d_test_dir / "junction-link"
    target.mkdir()
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    approved = None
    try:
        with pytest.raises(StoragePolicyError) as exc_info:
            approved = approve_physical_d_write_root(link)
        assert exc_info.value.code == "storage_reparse_point_forbidden"
    finally:
        if approved is not None:
            approved.close()
        if link.exists():
            os.rmdir(link)


def test_pinned_directories_block_replacement_but_allow_child_writes(d_test_dir: Path):
    import win32api
    import win32con

    approved = approve_physical_d_write_root(d_test_dir)
    child: ApprovedPhysicalDirectory | None = None
    moved = d_test_dir / "attempt-moved"
    outside_sentinel = d_test_dir.parent / f"outside-{os.urandom(8).hex()}.sentinel"
    try:
        child = approved.create_exclusive_child("attempt")
        child.verify()
        assert win32api.GetHandleInformation(child.handle) & win32con.HANDLE_FLAG_INHERIT == 0

        stable_payload = Path(child.stable_volume_path) / "worker-output.bin"
        with stable_payload.open("xb") as stream:
            stream.write(b"worker-data")
        assert stable_payload.read_bytes() == b"worker-data"
        stable_payload.unlink()
        assert list(child.path.iterdir()) == []

        with pytest.raises(OSError) as rename_error:
            os.rename(child.path, moved)
        assert rename_error.value.winerror in {5, 32}

        with pytest.raises(OSError) as delete_error:
            os.rmdir(child.path)
        assert delete_error.value.winerror in {5, 32}
        assert not outside_sentinel.exists()

        child.close()
        child = None
        os.rmdir(d_test_dir / "attempt")
    finally:
        if child is not None:
            child.close()
        approved.close()
        if moved.exists():
            moved.rmdir()


def test_pinned_child_resists_real_reparse_replacement_attack(d_test_dir: Path):
    import pywintypes
    import win32con
    import win32file
    import winioctlcon

    approved = approve_physical_d_write_root(d_test_dir)
    child: ApprovedPhysicalDirectory | None = None
    outside_target = (
        d_test_dir.parent / f"outside-target-{os.urandom(8).hex()}"
    )
    outside_target.mkdir()
    sentinel = outside_target / "sentinel.txt"
    sentinel.write_text("must-stay-unchanged", encoding="utf-8")
    try:
        child = approved.create_exclusive_child("pinned-attempt")
        original_identity = child.identity
        original_attributes = win32file.GetFileAttributes(str(child.path))
        assert original_attributes & policy._FILE_ATTRIBUTE_REPARSE_POINT == 0

        # FSCTL_SET_REPARSE_POINT requires a write-capable directory handle.
        # The pin must reject that handle before this control code can be sent.
        assert winioctlcon.FSCTL_SET_REPARSE_POINT > 0
        with pytest.raises(pywintypes.error) as write_open_error:
            win32file.CreateFile(
                str(child.path),
                win32con.GENERIC_WRITE,
                win32file.FILE_SHARE_READ
                | win32file.FILE_SHARE_WRITE
                | win32file.FILE_SHARE_DELETE,
                None,
                win32con.OPEN_EXISTING,
                win32file.FILE_FLAG_OPEN_REPARSE_POINT
                | win32file.FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
        assert write_open_error.value.winerror == 32

        with pytest.raises(OSError) as delete_error:
            os.rmdir(child.path)
        assert delete_error.value.winerror in {5, 32}

        replacement = subprocess.run(
            [
                "cmd.exe",
                "/d",
                "/c",
                "mklink",
                "/J",
                str(child.path),
                str(outside_target),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert replacement.returncode != 0

        child.verify()
        assert child.identity == original_identity
        current_attributes = win32file.GetFileAttributes(str(child.path))
        assert current_attributes & policy._FILE_ATTRIBUTE_REPARSE_POINT == 0
        assert sentinel.read_text(encoding="utf-8") == "must-stay-unchanged"
    finally:
        if child is not None:
            child.close()
        approved.close()
        pinned_path = d_test_dir / "pinned-attempt"
        if pinned_path.exists():
            pinned_path.rmdir()
        if sentinel.exists():
            sentinel.unlink()
        if outside_target.exists():
            outside_target.rmdir()


def test_createfile_post_open_failure_closes_exact_handle(
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeHandle:
        def __init__(self):
            self.close_calls = 0

        def Close(self):
            self.close_calls += 1

    handle = FakeHandle()
    monkeypatch.setattr(policy.win32file, "CreateFile", lambda *_args: handle)

    def fail_set_handle_information(*_args):
        raise OSError("simulated SetHandleInformation failure")

    monkeypatch.setattr(
        policy.win32api, "SetHandleInformation", fail_set_handle_information
    )

    with pytest.raises(StoragePolicyError) as exc_info:
        policy._open_directory_handle(Path(r"D:\not-opened"))

    assert exc_info.value.code == "storage_path_unavailable"
    assert handle.close_calls == 1


def test_exclusive_child_rejects_collisions_and_unsafe_names(d_test_dir: Path):
    approved = approve_physical_d_write_root(d_test_dir)
    child: ApprovedPhysicalDirectory | None = None
    try:
        child = approved.create_exclusive_child("attempt-safe")
        with pytest.raises(FileExistsError):
            approved.create_exclusive_child("attempt-safe")

        for unsafe in (
            "",
            ".",
            "..",
            "../escape",
            r"nested\escape",
            "ads:name",
            "trailing.",
            "trailing ",
            "CON",
            "con.txt",
            "PRN.log",
            "AUX",
            "NUL.md",
            "COM1",
            "com9.txt",
            "LPT1",
            "lpt9.txt",
            "COM¹",
            "com².txt",
            "COM³.log",
            "LPT¹",
            "lpt².txt",
            "LPT³.log",
            "CON .txt",
            "COM1 .log",
            "LPT¹ .txt",
        ):
            with pytest.raises((StoragePolicyError, ValueError)):
                approved.create_exclusive_child(unsafe)
    finally:
        if child is not None:
            child.close()
        approved.close()
        attempt = d_test_dir / "attempt-safe"
        if attempt.exists():
            attempt.rmdir()
