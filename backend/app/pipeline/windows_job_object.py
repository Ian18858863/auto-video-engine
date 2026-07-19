from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
import threading
import time
from typing import Any


if os.name == "nt":  # pragma: no branch - Task 3A is exercised on Windows.
    import win32api
    import win32con
    import win32job
else:  # Keep imports honest on non-Windows hosts and fail at the API boundary.
    win32api = None
    win32con = None
    win32job = None


class WindowsJobObjectUnsupported(RuntimeError):
    pass


@dataclass(slots=True)
class WindowsJobObject:
    """Explicit single owner for one unnamed Windows Job Object handle."""

    _handle: Any = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    @classmethod
    def create_unnamed_kill_on_close(cls) -> WindowsJobObject:
        _require_windows()
        handle = win32job.CreateJobObject(None, "")
        try:
            win32api.SetHandleInformation(handle, win32con.HANDLE_FLAG_INHERIT, 0)
            limits = win32job.QueryInformationJobObject(
                handle,
                win32job.JobObjectExtendedLimitInformation,
            )
            basic_limits = dict(limits["BasicLimitInformation"])
            breakaway_flags = (
                win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
                | win32job.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
            )
            basic_limits["LimitFlags"] = (
                int(basic_limits["LimitFlags"]) & ~breakaway_flags
            ) | win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            configured_limits = dict(limits)
            configured_limits["BasicLimitInformation"] = basic_limits
            win32job.SetInformationJobObject(
                handle,
                win32job.JobObjectExtendedLimitInformation,
                configured_limits,
            )
            verified_limits = win32job.QueryInformationJobObject(
                handle,
                win32job.JobObjectExtendedLimitInformation,
            )
            verified_flags = int(
                verified_limits["BasicLimitInformation"]["LimitFlags"]
            )
            if not verified_flags & win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE:
                raise RuntimeError("job object kill-on-close configuration was not applied")
            if verified_flags & breakaway_flags:
                raise RuntimeError("job object breakaway flags must remain disabled")
            if win32api.GetHandleInformation(handle) & win32con.HANDLE_FLAG_INHERIT:
                raise RuntimeError("job object handle remained inheritable")
            return cls(_handle=handle)
        except BaseException:
            handle.Close()
            raise

    @property
    def handle(self) -> Any:
        with self._lock:
            self._ensure_open()
            return self._handle

    def active_process_count(self) -> int:
        with self._lock:
            self._ensure_open()
            accounting = win32job.QueryInformationJobObject(
                self._handle,
                win32job.JobObjectBasicAccountingInformation,
            )
            return int(accounting["ActiveProcesses"])

    def process_ids(self) -> tuple[int, ...]:
        with self._lock:
            self._ensure_open()
            process_ids = win32job.QueryInformationJobObject(
                self._handle,
                win32job.JobObjectBasicProcessIdList,
            )
            return tuple(int(process_id) for process_id in process_ids)

    def terminate(self, exit_code: int) -> None:
        if (
            type(exit_code) is not int
            or exit_code < 0
            or exit_code > 0xFFFFFFFF
        ):
            raise ValueError("exit_code must be a Windows unsigned 32-bit exit code")
        with self._lock:
            self._ensure_open()
            native_exit_code = (
                exit_code if exit_code <= 0x7FFFFFFF else exit_code - 0x100000000
            )
            win32job.TerminateJobObject(self._handle, native_exit_code)

    def wait_until_empty(self, timeout_seconds: float) -> bool:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds < 0
        ):
            raise ValueError("timeout_seconds must be a finite non-negative number")
        deadline = time.monotonic() + float(timeout_seconds)
        while True:
            if self.active_process_count() == 0:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._handle.Close()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("job object handle is closed")

    def __enter__(self) -> WindowsJobObject:
        _ = self.handle
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def _require_windows() -> None:
    if os.name != "nt" or win32job is None:
        raise WindowsJobObjectUnsupported("Windows Job Objects are unavailable")


__all__ = ["WindowsJobObject", "WindowsJobObjectUnsupported"]
