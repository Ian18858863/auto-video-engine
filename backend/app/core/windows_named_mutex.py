from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any


if os.name == "nt":  # pragma: no branch - imports are exercised on Windows CI.
    import pywintypes
    import win32api
    import win32con
    import win32event
    import win32security
else:  # Keep the module importable and fail closed at the API boundary.
    pywintypes = None
    win32api = None
    win32con = None
    win32event = None
    win32security = None


_MUTEX_MODIFY_STATE = 0x0001
_MUTEX_ALL_ACCESS = 0x001F0001
_READ_CONTROL = 0x00020000
_ALLOWED_NAMESPACES = frozenset(
    {
        "material-step",
        "asset-registry",
        "material-submission-owner",
        "script-version",
    }
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class WindowsMutexBusy(RuntimeError):
    status_code = 409

    def __init__(self, name: str):
        super().__init__(f"named mutex is already owned: {name}")
        self.name = name


class WindowsMutexUnsupported(RuntimeError):
    pass


def current_user_sid_string() -> str:
    _require_windows()
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        sid, _attributes = win32security.GetTokenInformation(
            token, win32security.TokenUser
        )
        return str(win32security.ConvertSidToStringSid(sid))
    finally:
        token.Close()


def build_mutex_name(namespace: str, key: str) -> str:
    if namespace not in _ALLOWED_NAMESPACES:
        raise ValueError(f"unsupported mutex namespace: {namespace}")
    if not isinstance(key, str) or not key:
        raise ValueError("mutex key must be a non-empty string")
    digest = hashlib.sha256(
        f"AutoVideoEngine\x00{namespace}\x00{key}".encode("utf-8")
    ).hexdigest()
    return f"Global\\AutoVideoEngine.{namespace}.{current_user_sid_string()}.{digest}"


@dataclass(slots=True)
class WindowsNamedMutex:
    name: str
    _handle: Any = field(repr=False)
    _acquired: bool = field(default=False, repr=False)
    _was_abandoned: bool = field(default=False, repr=False)
    _closed: bool = field(default=False, repr=False)
    _owner_thread: threading.Thread | None = field(default=None, repr=False)
    _owner_native_id: int | None = field(default=None, repr=False)

    @classmethod
    def try_acquire(
        cls,
        name: str,
        *,
        timeout_ms: int = 0,
    ) -> WindowsNamedMutex:
        _validate_generated_name(name)
        _require_windows()
        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or timeout_ms < 0
        ):
            raise ValueError("timeout_ms must be a non-negative integer")
        handle = win32event.CreateMutex(_restricted_security_attributes(), False, name)
        instance = cls(name=name, _handle=handle)
        try:
            _make_handle_non_inheritable(handle)
            _verify_restricted_dacl(handle)
            instance.acquire(timeout_ms=timeout_ms)
            return instance
        except Exception:
            instance.release()
            raise

    @classmethod
    def open_waiter(cls, name: str) -> WindowsNamedMutex:
        """Open an existing object before owner death so abandonment is observable."""

        _validate_generated_name(name)
        _require_windows()
        handle = win32event.OpenMutex(
            win32con.SYNCHRONIZE | _MUTEX_MODIFY_STATE | _READ_CONTROL,
            False,
            name,
        )
        instance = cls(name=name, _handle=handle)
        try:
            _make_handle_non_inheritable(handle)
            _verify_restricted_dacl(handle)
            return instance
        except Exception:
            instance.release()
            raise

    @property
    def handle(self) -> Any:
        if self._closed:
            raise RuntimeError("named mutex handle is closed")
        return self._handle

    @property
    def was_abandoned(self) -> bool:
        return self._was_abandoned

    def assert_owned(self) -> None:
        """Prove this wrapper still owns the mutex on the current thread."""

        if self._closed or not self._acquired:
            raise RuntimeError("named mutex is not owned")
        if threading.current_thread() is not self._owner_thread:
            raise RuntimeError("named mutex is owned by another thread")

    def acquire(self, timeout_ms: int = 0) -> None:
        if self._closed:
            raise RuntimeError("named mutex handle is closed")
        if self._acquired:
            raise RuntimeError("named mutex is already acquired by this wrapper")
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms < 0:
            raise ValueError("timeout_ms must be a non-negative integer")

        result = win32event.WaitForSingleObject(self._handle, timeout_ms)
        if result == win32event.WAIT_TIMEOUT:
            raise WindowsMutexBusy(self.name)
        if result == win32event.WAIT_OBJECT_0:
            self._acquired = True
            self._was_abandoned = False
            self._owner_thread = threading.current_thread()
            self._owner_native_id = threading.get_native_id()
            return
        if result == win32event.WAIT_ABANDONED_0:
            self._acquired = True
            self._was_abandoned = True
            self._owner_thread = threading.current_thread()
            self._owner_native_id = threading.get_native_id()
            return
        raise RuntimeError(f"unexpected mutex wait result: {result}")

    def release(self) -> None:
        if self._closed:
            return
        if self._acquired:
            if threading.current_thread() is not self._owner_thread:
                raise RuntimeError("named mutex can only be released by its owner thread")
            win32event.ReleaseMutex(self._handle)
            self._acquired = False
            self._owner_thread = None
            self._owner_native_id = None
        self._handle.Close()
        self._closed = True

    def __enter__(self) -> WindowsNamedMutex:
        if not self._acquired:
            raise RuntimeError("named mutex context entered without ownership")
        if threading.current_thread() is not self._owner_thread:
            raise RuntimeError("named mutex context entered by a non-owner thread")
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()


def _validate_generated_name(name: str) -> None:
    if not isinstance(name, str):
        raise ValueError("mutex name must be a string")
    parts = name.split(".")
    if len(parts) != 4:
        raise ValueError("mutex name does not match the generated format")
    prefix, namespace, sid, digest = parts
    if prefix != "Global\\AutoVideoEngine" or namespace not in _ALLOWED_NAMESPACES:
        raise ValueError("mutex name has an invalid namespace")
    if sid != current_user_sid_string() or not _HASH_RE.fullmatch(digest):
        raise ValueError("mutex name is not bound to the current user")


def _restricted_security_attributes() -> Any:
    _require_windows()
    user_sid = _current_user_sid()
    system_sid = win32security.CreateWellKnownSid(
        win32security.WinLocalSystemSid, None
    )
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(win32con.ACL_REVISION, _MUTEX_ALL_ACCESS, user_sid)
    dacl.AddAccessAllowedAce(win32con.ACL_REVISION, _MUTEX_ALL_ACCESS, system_sid)

    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(user_sid, False)
    descriptor.SetSecurityDescriptorDacl(True, dacl, False)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    attributes.bInheritHandle = False
    return attributes


def _verify_restricted_dacl(handle: Any) -> None:
    descriptor = win32security.GetKernelObjectSecurity(
        handle,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION,
    )
    owner_sid = descriptor.GetSecurityDescriptorOwner()
    if owner_sid is None:
        raise PermissionError("named mutex has no owner")
    owner_sid_string = str(win32security.ConvertSidToStringSid(owner_sid))
    if owner_sid_string not in {current_user_sid_string(), "S-1-5-18"}:
        raise PermissionError("named mutex owner is not current-user or SYSTEM")
    dacl = descriptor.GetSecurityDescriptorDacl()
    if dacl is None:
        raise PermissionError("named mutex has a null DACL")

    expected_sids = {current_user_sid_string(), "S-1-5-18"}
    actual_sids: set[str] = set()
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        (ace_type, _ace_flags), access_mask, sid = ace
        if ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE:
            raise PermissionError("named mutex DACL contains a non-allow ACE")
        if access_mask != _MUTEX_ALL_ACCESS:
            raise PermissionError("named mutex DACL contains an unexpected access mask")
        actual_sids.add(str(win32security.ConvertSidToStringSid(sid)))
    if actual_sids != expected_sids or dacl.GetAceCount() != 2:
        raise PermissionError("named mutex DACL is not current-user plus SYSTEM only")


def _current_user_sid() -> Any:
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        sid, _attributes = win32security.GetTokenInformation(
            token, win32security.TokenUser
        )
        return sid
    finally:
        token.Close()


def _make_handle_non_inheritable(handle: Any) -> None:
    win32api.SetHandleInformation(handle, win32con.HANDLE_FLAG_INHERIT, 0)


def _require_windows() -> None:
    if os.name != "nt" or win32event is None:
        raise WindowsMutexUnsupported("Windows named mutex is unavailable")
