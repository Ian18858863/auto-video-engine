from __future__ import annotations

import ntpath
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar


DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3

_FILE_READ_ATTRIBUTES = 0x0080
_FILE_LIST_DIRECTORY = 0x0001
_FILE_ATTRIBUTE_DIRECTORY = 0x0010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_VOLUME_NAME_GUID = 0x00000001
_DOS_DEVICE_BASENAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
    | {f"com{index}" for index in "¹²³"}
    | {f"lpt{index}" for index in "¹²³"}
)


if os.name == "nt":  # pragma: no branch - imports are exercised on Windows CI.
    import win32api
    import win32con
    import win32file
else:  # Keep importable so callers receive a fail-closed domain error.
    win32api = None
    win32con = None
    win32file = None


class StoragePolicyError(RuntimeError):
    """A fail-closed rejection from the physical write-root policy."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    volume_guid: str
    volume_serial: int
    file_index: int


@dataclass(slots=True)
class ApprovedPhysicalDirectory:
    path: Path
    stable_volume_path: str
    identity: DirectoryIdentity
    _handle: Any | None = field(default=None, repr=False)
    _tree: PinnedDirectoryTree | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def handle(self) -> Any:
        self._ensure_open()
        if self._tree is not None:
            return self._tree.directories[-1].handle
        if self._handle is None:
            raise StoragePolicyError("storage_pin_closed", "directory handle is closed")
        return self._handle

    def verify(self) -> None:
        self._ensure_open()
        if self._tree is not None:
            self._tree.verify()
            pinned = self._tree.directories[-1]
            if (
                pinned.path != self.path
                or _normcase(pinned.stable_volume_path)
                != _normcase(self.stable_volume_path)
                or pinned.identity != self.identity
            ):
                raise StoragePolicyError(
                    "storage_identity_changed", "approved directory metadata diverged"
                )
            return
        _verify_component(self)

    def create_exclusive_child(self, name: str) -> ApprovedPhysicalDirectory:
        self.verify()
        _validate_child_name(name)
        stable_child = ntpath.join(self.stable_volume_path, name)
        visible_child = self.path / name
        os.mkdir(stable_child)
        try:
            return _approve_existing_directory(visible_child, ApprovedPhysicalDirectory)
        except Exception:
            # Do not mutate a path whose post-create identity could not be pinned.
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._tree is not None:
            self._tree.close()
            return
        _close_handle(self._handle)
        self._handle = None

    def _ensure_open(self) -> None:
        if self._closed:
            raise StoragePolicyError("storage_pin_closed", "directory pin is closed")

    def __enter__(self) -> ApprovedPhysicalDirectory:
        self.verify()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


@dataclass(slots=True)
class PinnedDirectoryTree:
    directories: tuple[ApprovedPhysicalDirectory, ...]
    _closed: bool = field(default=False, init=False, repr=False)

    def verify(self) -> None:
        if self._closed:
            raise StoragePolicyError("storage_pin_closed", "directory tree is closed")
        if not self.directories:
            raise StoragePolicyError("storage_pin_empty", "directory tree has no handles")
        expected_guid = self.directories[0].identity.volume_guid
        for directory in self.directories:
            directory.verify()
            if _normcase(directory.identity.volume_guid) != _normcase(expected_guid):
                raise StoragePolicyError(
                    "storage_volume_changed", "pinned directory crossed a volume boundary"
                )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for directory in reversed(self.directories):
            directory.close()


@dataclass(slots=True)
class ApprovedPhysicalWriteRoot(ApprovedPhysicalDirectory):
    pass


_ApprovedDirectoryT = TypeVar(
    "_ApprovedDirectoryT", bound=ApprovedPhysicalDirectory
)


def approve_physical_d_write_root(path: Path | str) -> ApprovedPhysicalWriteRoot:
    """Approve and pin an existing, non-reparse directory on physical D:."""

    return _approve_existing_directory(path, ApprovedPhysicalWriteRoot)


def _approve_existing_directory(
    path: Path | str, result_type: type[_ApprovedDirectoryT]
) -> _ApprovedDirectoryT:
    if not _platform_is_windows():
        raise StoragePolicyError(
            "storage_policy_unsupported", "physical D policy requires Windows"
        )

    visible_path = _validate_and_normalize_d_path(path)
    drive_root = "D:\\"

    if _get_drive_type(drive_root) != DRIVE_FIXED:
        raise StoragePolicyError(
            "storage_drive_not_fixed", "D: is not a fixed local volume"
        )

    dos_targets = _query_dos_device("D:")
    if _dos_device_is_redirected(dos_targets):
        raise StoragePolicyError(
            "storage_drive_redirected", "D: is SUBST, UNC, or otherwise redirected"
        )

    volume_guid = _normalize_volume_guid(_get_volume_guid(drive_root))
    try:
        system_volume_root = _get_trusted_system_volume_root()
        system_guid = _normalize_volume_guid(
            _get_volume_guid(system_volume_root)
        )
    except StoragePolicyError as exc:
        if exc.code == "storage_system_volume_unknown":
            raise
        raise StoragePolicyError(
            "storage_system_volume_unknown",
            "cannot establish the trusted Windows system volume",
        ) from exc
    except Exception as exc:
        raise StoragePolicyError(
            "storage_system_volume_unknown",
            "cannot establish the trusted Windows system volume",
        ) from exc
    if _normcase(system_guid) == _normcase(volume_guid):
        raise StoragePolicyError(
            "storage_system_volume_forbidden",
            "D: resolves to the Windows system volume",
        )

    tree = _pin_directory_chain(visible_path, volume_guid)
    try:
        tree.verify()
        final = tree.directories[-1]
        return result_type(
            path=visible_path,
            stable_volume_path=final.stable_volume_path,
            identity=final.identity,
            _tree=tree,
        )
    except Exception:
        tree.close()
        raise


def _platform_is_windows() -> bool:
    return os.name == "nt"


def _validate_and_normalize_d_path(path: Path | str) -> Path:
    raw_path = os.fspath(path)
    if not isinstance(raw_path, str) or not raw_path:
        raise StoragePolicyError(
            "storage_path_not_physical_d", "path must be a non-empty string"
        )

    drive, tail = ntpath.splitdrive(raw_path)
    if (
        raw_path.startswith(("\\\\", "//"))
        or drive.casefold() != "d:"
        or not tail.startswith(("\\", "/"))
    ):
        raise StoragePolicyError(
            "storage_path_not_physical_d",
            "path must be an absolute drive-letter path on D:",
        )

    raw_components = [
        item for item in tail.replace("/", "\\").split("\\") if item
    ]
    if any(_component_is_ambiguous(item) for item in raw_components):
        raise StoragePolicyError(
            "storage_path_ambiguous",
            "path contains dot traversal, reserved, or lossy Windows components",
        )

    normalized_text = ntpath.normpath(raw_path)
    normalized_drive, normalized_tail = ntpath.splitdrive(normalized_text)
    components = [item for item in normalized_tail.replace("/", "\\").split("\\") if item]
    if normalized_drive.casefold() != "d:" or any(":" in item for item in components):
        raise StoragePolicyError(
            "storage_path_not_physical_d", "alternate streams are forbidden"
        )
    return Path(normalized_text)


def _get_drive_type(root: str) -> int:
    _require_win32()
    return int(win32file.GetDriveType(root))


def _query_dos_device(drive: str) -> tuple[str, ...]:
    _require_win32()
    raw = win32file.QueryDosDevice(drive)
    if isinstance(raw, str):
        return tuple(part for part in raw.split("\x00") if part)
    return tuple(str(part) for part in raw)


def _get_volume_guid(root: str) -> str:
    _require_win32()
    return str(win32file.GetVolumeNameForVolumeMountPoint(root))


def _get_trusted_system_volume_root() -> str:
    _require_win32()
    try:
        system_directory = win32api.GetSystemDirectory()
        if not isinstance(system_directory, str) or not system_directory:
            raise ValueError("GetSystemDirectory returned no path")
        system_drive, system_tail = ntpath.splitdrive(system_directory)
        if (
            system_directory.startswith(("\\\\", "//"))
            or len(system_drive) != 2
            or system_drive[1] != ":"
            or not system_tail.startswith(("\\", "/"))
        ):
            raise ValueError("GetSystemDirectory returned an invalid local path")

        volume_root = win32file.GetVolumePathName(system_directory)
        if not isinstance(volume_root, str) or not volume_root:
            raise ValueError("GetVolumePathName returned no root")
        volume_drive, volume_tail = ntpath.splitdrive(volume_root)
        if (
            volume_root.startswith(("\\\\", "//"))
            or len(volume_drive) != 2
            or volume_drive[1] != ":"
            or not volume_tail.startswith(("\\", "/"))
        ):
            raise ValueError("GetVolumePathName returned an invalid local root")
        return volume_root
    except StoragePolicyError:
        raise
    except Exception as exc:
        raise StoragePolicyError(
            "storage_system_volume_unknown",
            "trusted Windows system-volume APIs failed",
        ) from exc


def _dos_device_is_redirected(targets: object) -> bool:
    if isinstance(targets, str):
        values = tuple(part for part in targets.split("\x00") if part)
    else:
        values = tuple(str(part) for part in targets)
    if not values:
        return True
    for value in values:
        lowered = value.casefold()
        if lowered.startswith(
            ("\\??\\", "\\\\?\\", "\\device\\mup", "\\device\\lanmanredirector")
        ):
            return True
        if not lowered.startswith("\\device\\harddiskvolume"):
            return True
    return False


def _normalize_volume_guid(value: str) -> str:
    normalized = value.replace("/", "\\")
    if not normalized.endswith("\\"):
        normalized += "\\"
    if not normalized.casefold().startswith("\\\\?\\volume{"):
        raise StoragePolicyError(
            "storage_volume_guid_invalid", "volume did not return a GUID path"
        )
    return normalized


def _pin_directory_chain(path: Path, volume_guid: str) -> PinnedDirectoryTree:
    components = _directory_chain(path)
    pinned: list[ApprovedPhysicalDirectory] = []
    try:
        for component_path in components:
            handle = _open_directory_handle(component_path)
            try:
                stable_path, identity = _inspect_directory_handle(handle, volume_guid)
                component = ApprovedPhysicalDirectory(
                    path=component_path,
                    stable_volume_path=stable_path,
                    identity=identity,
                    _handle=handle,
                )
                component.verify()
            except Exception:
                _close_handle(handle)
                raise
            pinned.append(component)
        return PinnedDirectoryTree(tuple(pinned))
    except Exception:
        for component in reversed(pinned):
            component.close()
        raise


def _directory_chain(path: Path) -> tuple[Path, ...]:
    drive, tail = ntpath.splitdrive(str(path))
    current = Path(f"{drive}\\")
    result = [current]
    for part in (item for item in tail.replace("/", "\\").split("\\") if item):
        current = current / part
        result.append(current)
    return tuple(result)


def _open_directory_handle(path: Path) -> Any:
    _require_win32()
    handle = None
    try:
        handle = win32file.CreateFile(
            str(path),
            _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES,
            win32file.FILE_SHARE_READ,
            None,
            win32con.OPEN_EXISTING,
            win32file.FILE_FLAG_OPEN_REPARSE_POINT
            | win32file.FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        win32api.SetHandleInformation(handle, win32con.HANDLE_FLAG_INHERIT, 0)
        return handle
    except Exception as exc:
        _close_handle(handle)
        raise StoragePolicyError(
            "storage_path_unavailable", f"cannot pin directory: {path}"
        ) from exc
    except BaseException:
        _close_handle(handle)
        raise


def _inspect_directory_handle(
    handle: Any, expected_volume_guid: str
) -> tuple[str, DirectoryIdentity]:
    _require_win32()
    info = win32file.GetFileInformationByHandle(handle)
    attributes = int(info[0])
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise StoragePolicyError(
            "storage_reparse_point_forbidden", "directory is a reparse point"
        )
    if not attributes & _FILE_ATTRIBUTE_DIRECTORY:
        raise StoragePolicyError("storage_not_directory", "path is not a directory")

    stable_path = str(
        win32file.GetFinalPathNameByHandle(handle, _VOLUME_NAME_GUID)
    ).replace("/", "\\")
    if not _path_is_on_volume(stable_path, expected_volume_guid):
        raise StoragePolicyError(
            "storage_volume_changed", "final handle path crossed the approved D volume"
        )

    file_index = (int(info[8]) << 32) | int(info[9])
    identity = DirectoryIdentity(
        volume_guid=expected_volume_guid,
        volume_serial=int(info[4]),
        file_index=file_index,
    )
    return stable_path, identity


def _verify_component(directory: ApprovedPhysicalDirectory) -> None:
    stable_path, identity = _inspect_directory_handle(
        directory.handle, directory.identity.volume_guid
    )
    if identity != directory.identity or _normcase(stable_path) != _normcase(
        directory.stable_volume_path
    ):
        raise StoragePolicyError(
            "storage_identity_changed", f"directory identity changed: {directory.path}"
        )


def _path_is_on_volume(path: str, volume_guid: str) -> bool:
    normalized_path = path.replace("/", "\\")
    normalized_guid = _normalize_volume_guid(volume_guid)
    path_casefold = normalized_path.casefold()
    guid_casefold = normalized_guid.casefold()
    return path_casefold == guid_casefold.rstrip("\\") or path_casefold.startswith(
        guid_casefold
    )


def _validate_child_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or any(character in name for character in ("\\", "/"))
        or _component_is_ambiguous(name)
    ):
        raise StoragePolicyError(
            "storage_child_name_invalid", "child must be one safe path component"
        )


def _component_is_ambiguous(component: str) -> bool:
    if (
        component in {".", ".."}
        or component.endswith((" ", "."))
        or any(ord(character) < 32 for character in component)
        or any(character in component for character in (":", '"', "<", ">", "|", "?", "*"))
    ):
        return True
    basename = component.split(".", 1)[0].rstrip(" .").casefold()
    return basename in _DOS_DEVICE_BASENAMES


def _normcase(value: str) -> str:
    return ntpath.normcase(value.rstrip("\\"))


def _close_handle(handle: Any | None) -> None:
    if handle is None:
        return
    try:
        handle.Close()
    except OSError:
        pass


def _require_win32() -> None:
    if not _platform_is_windows() or win32file is None:
        raise StoragePolicyError(
            "storage_policy_unsupported", "pywin32 storage APIs unavailable"
        )
