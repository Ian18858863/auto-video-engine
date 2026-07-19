from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from enum import Enum
import math
import ntpath
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Mapping, Sequence


CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES = 0x00000100

PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D

_ERROR_INSUFFICIENT_BUFFER = 122
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_HANDLE_FLAG_INHERIT = 0x00000001
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_STILL_ACTIVE = 259
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_JOB_PID_CAPACITY = 64

_SIZE_T = ctypes.c_size_t
_DWORD_PTR = ctypes.c_size_t


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _STARTUPINFOW),
        ("lpAttributeList", wintypes.LPVOID),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _JOB_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _JOB_BASIC_PROCESS_ID_LIST(ctypes.Structure):
    _fields_ = [
        ("NumberOfAssignedProcesses", wintypes.DWORD),
        ("NumberOfProcessIdsInList", wintypes.DWORD),
        ("ProcessIdList", _DWORD_PTR * _JOB_PID_CAPACITY),
    ]


class WindowsAtomicProcessUnsupported(RuntimeError):
    pass


class LaunchResourceState(str, Enum):
    PREPARED = "prepared"
    PROCESS_CREATED_UNVERIFIED = "process_created_unverified"
    MEMBERSHIP_PROVEN = "membership_proven"
    CONTAINMENT_PROVEN = "containment_proven"
    TRANSFERRED_TO_PROCESS = "transferred_to_process"
    TRANSFERRED_FOR_QUARANTINE = "transferred_for_quarantine"
    CLOSED = "closed"


@dataclass(slots=True)
class _OwnedNativeHandle:
    _api: Any = field(repr=False)
    _value: int = field(default=0, init=False, repr=False)
    _live: bool = field(default=False, init=False, repr=False)
    _close_failed: bool = field(default=False, init=False, repr=False)
    _lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
    )

    def adopt(self, value: int) -> None:
        with self._lock:
            if self._live or self._value:
                raise RuntimeError("native handle slot already owns a handle")
            if type(value) is not int or value <= 0 or value == _INVALID_HANDLE_VALUE:
                raise ValueError("cannot adopt an invalid native handle")
            self._value = value
            self._live = True
            self._close_failed = False

    @property
    def value(self) -> int:
        with self._lock:
            if not self._live:
                raise RuntimeError("native handle is not live")
            return self._value

    @property
    def live(self) -> bool:
        with self._lock:
            return self._live

    @property
    def close_failed(self) -> bool:
        with self._lock:
            return self._close_failed

    def close(self) -> None:
        with self._lock:
            if not self._live:
                return
            try:
                self._api.close_handle(self._value)
            except BaseException:
                self._close_failed = True
                raise
            self._live = False
            self._close_failed = False

    def Close(self) -> None:
        self.close()

    def __int__(self) -> int:
        return self.value


@dataclass(slots=True)
class _OwnedAttributeList:
    pointer: ctypes.c_void_p | None = field(default=None, init=False)
    buffer: object | None = field(default=None, init=False, repr=False)
    initialized: bool = field(default=False, init=False)
    delete_failed: bool = field(default=False, init=False)

    def prepare_buffer(self, pointer: ctypes.c_void_p, buffer: object) -> None:
        if self.pointer is not None or self.buffer is not None or self.initialized:
            raise RuntimeError("attribute-list owner is already prepared")
        self.pointer = pointer
        self.buffer = buffer

    def mark_initialized(self) -> None:
        if self.pointer is None or self.buffer is None:
            raise RuntimeError("attribute-list buffer was not prepared")
        self.initialized = True

    def adopt_initialized(self, pointer: ctypes.c_void_p, buffer: object) -> None:
        self.prepare_buffer(pointer, buffer)
        # Native initialization has already succeeded. Record that ownership
        # before invoking any hook that tests or callers may replace.
        self.initialized = True
        self.delete_failed = False
        self.mark_initialized()

    def delete(self, native_api: Any) -> None:
        if not self.initialized:
            self.pointer = None
            self.buffer = None
            self.delete_failed = False
            return
        try:
            native_api.delete_attribute_list(self)
        except BaseException:
            self.delete_failed = True
            raise
        self.initialized = False
        self.delete_failed = False
        self.pointer = None
        self.buffer = None


class _NativeWindowsApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsAtomicProcessUnsupported(
                "atomic Windows process creation is unavailable"
            )
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32 = kernel32

        self._initialize_attributes = kernel32.InitializeProcThreadAttributeList
        self._initialize_attributes.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_SIZE_T),
        ]
        self._initialize_attributes.restype = wintypes.BOOL

        self._update_attribute = kernel32.UpdateProcThreadAttribute
        self._update_attribute.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            _DWORD_PTR,
            wintypes.LPVOID,
            _SIZE_T,
            wintypes.LPVOID,
            ctypes.POINTER(_SIZE_T),
        ]
        self._update_attribute.restype = wintypes.BOOL

        self._delete_attributes = kernel32.DeleteProcThreadAttributeList
        self._delete_attributes.argtypes = [wintypes.LPVOID]
        self._delete_attributes.restype = None

        self._create_process = kernel32.CreateProcessW
        self._create_process.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.POINTER(_SECURITY_ATTRIBUTES),
            ctypes.POINTER(_SECURITY_ATTRIBUTES),
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPCWSTR,
            ctypes.POINTER(_STARTUPINFOW),
            ctypes.POINTER(_PROCESS_INFORMATION),
        ]
        self._create_process.restype = wintypes.BOOL

        self._create_file = kernel32.CreateFileW
        self._create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_SECURITY_ATTRIBUTES),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._create_file.restype = wintypes.HANDLE

        self._get_handle_information = kernel32.GetHandleInformation
        self._get_handle_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._get_handle_information.restype = wintypes.BOOL

        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL

        self._resume_thread = kernel32.ResumeThread
        self._resume_thread.argtypes = [wintypes.HANDLE]
        self._resume_thread.restype = wintypes.DWORD

        self._wait_for_single_object = kernel32.WaitForSingleObject
        self._wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._wait_for_single_object.restype = wintypes.DWORD

        self._get_exit_code_process = kernel32.GetExitCodeProcess
        self._get_exit_code_process.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._get_exit_code_process.restype = wintypes.BOOL

        self._terminate_job = kernel32.TerminateJobObject
        self._terminate_job.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._terminate_job.restype = wintypes.BOOL

        self._terminate_process = kernel32.TerminateProcess
        self._terminate_process.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._terminate_process.restype = wintypes.BOOL

        self._is_process_in_job = kernel32.IsProcessInJob
        self._is_process_in_job.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        self._is_process_in_job.restype = wintypes.BOOL

        self._query_job_information = kernel32.QueryInformationJobObject
        self._query_job_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._query_job_information.restype = wintypes.BOOL

    def get_handle_flags(self, handle: int) -> int:
        flags = wintypes.DWORD()
        if not self._get_handle_information(
            wintypes.HANDLE(handle), ctypes.byref(flags)
        ):
            _raise_last_windows_error()
        return int(flags.value)

    def create_nul_handle(
        self,
        desired_access: int,
        owner_slot: _OwnedNativeHandle,
    ) -> int:
        security_attributes = _SECURITY_ATTRIBUTES(
            nLength=ctypes.sizeof(_SECURITY_ATTRIBUTES),
            lpSecurityDescriptor=None,
            bInheritHandle=True,
        )
        raw_handle = self._create_file(
            "NUL",
            desired_access,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            ctypes.byref(security_attributes),
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        raw_value = _raw_handle_value(raw_handle)
        if raw_value <= 0 or raw_value == _INVALID_HANDLE_VALUE:
            _raise_last_windows_error()
        # CreateFileW has returned a live OS resource. Adopt the validated raw
        # value before any conversion or inspection that can fail.
        owner_slot.adopt(raw_value)
        handle = _handle_to_int(raw_handle)
        if handle != raw_value:
            raise RuntimeError("NUL handle conversion diverged from the owned raw value")
        if not self.get_handle_flags(handle) & _HANDLE_FLAG_INHERIT:
            raise RuntimeError("NUL standard handle was not inheritable")
        return handle

    def initialize_attribute_list(
        self,
        attribute_count: int,
        attribute_owner: _OwnedAttributeList,
    ) -> _OwnedAttributeList:
        size = _SIZE_T()
        ctypes.set_last_error(0)
        first_result = self._initialize_attributes(
            None,
            attribute_count,
            0,
            ctypes.byref(size),
        )
        first_error = ctypes.get_last_error()
        if first_result or first_error != _ERROR_INSUFFICIENT_BUFFER or size.value == 0:
            if first_error:
                raise ctypes.WinError(first_error)
            raise RuntimeError("attribute-list size query returned an invalid result")

        buffer = ctypes.create_string_buffer(size.value)
        pointer = ctypes.cast(buffer, ctypes.c_void_p)
        attribute_owner.prepare_buffer(pointer, buffer)
        if not self._initialize_attributes(
            pointer,
            attribute_count,
            0,
            ctypes.byref(size),
        ):
            _raise_last_windows_error()
        # The OS resource now exists. Make it cleanup-visible before the
        # fallible registration hook runs.
        attribute_owner.initialized = True
        attribute_owner.delete_failed = False
        attribute_owner.mark_initialized()
        return attribute_owner

    def update_attribute(
        self,
        attribute_state: _OwnedAttributeList,
        attribute: int,
        handles: ctypes.Array[Any],
    ) -> None:
        if not self._update_attribute(
            attribute_state.pointer,
            0,
            attribute,
            ctypes.cast(handles, ctypes.c_void_p),
            ctypes.sizeof(handles),
            None,
            None,
        ):
            _raise_last_windows_error()

    def create_process(
        self,
        *,
        application_name: str,
        command_line: ctypes.Array[Any],
        cwd: str,
        environment_block: ctypes.Array[Any],
        creation_flags: int,
        inherit_handles: bool,
        startup_info: _STARTUPINFOEXW,
        process_information: _PROCESS_INFORMATION,
    ) -> None:
        startup_pointer = ctypes.cast(
            ctypes.byref(startup_info),
            ctypes.POINTER(_STARTUPINFOW),
        )
        if not self._create_process(
            application_name,
            command_line,
            None,
            None,
            inherit_handles,
            creation_flags,
            ctypes.cast(environment_block, ctypes.c_void_p),
            cwd,
            startup_pointer,
            ctypes.byref(process_information),
        ):
            _raise_last_windows_error()

    def delete_attribute_list(self, state: _OwnedAttributeList) -> None:
        self._delete_attributes(state.pointer)

    def close_handle(self, handle: int) -> None:
        if not self._close_handle(wintypes.HANDLE(handle)):
            _raise_last_windows_error()

    def resume_thread(self, handle: int) -> int:
        result = int(self._resume_thread(wintypes.HANDLE(handle)))
        if result == 0xFFFFFFFF:
            _raise_last_windows_error()
        return result

    def wait_for_handle(self, handle: int, timeout_milliseconds: int) -> bool:
        result = int(
            self._wait_for_single_object(
                wintypes.HANDLE(handle),
                timeout_milliseconds,
            )
        )
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        _raise_last_windows_error()

    def get_exit_code(self, handle: int) -> int:
        exit_code = wintypes.DWORD()
        if not self._get_exit_code_process(
            wintypes.HANDLE(handle), ctypes.byref(exit_code)
        ):
            _raise_last_windows_error()
        return int(exit_code.value)

    def terminate_job(self, handle: int, exit_code: int) -> None:
        if not self._terminate_job(wintypes.HANDLE(handle), exit_code):
            _raise_last_windows_error()

    def terminate_process(self, handle: int, exit_code: int) -> None:
        if not self._terminate_process(wintypes.HANDLE(handle), exit_code):
            _raise_last_windows_error()

    def active_process_count(self, handle: int) -> int:
        accounting = _JOB_BASIC_ACCOUNTING_INFORMATION()
        returned_length = wintypes.DWORD()
        if not self._query_job_information(
            wintypes.HANDLE(handle),
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            ctypes.byref(returned_length),
        ):
            _raise_last_windows_error()
        return int(accounting.ActiveProcesses)

    def process_ids(self, handle: int) -> tuple[int, ...]:
        process_list = _JOB_BASIC_PROCESS_ID_LIST()
        returned_length = wintypes.DWORD()
        if not self._query_job_information(
            wintypes.HANDLE(handle),
            _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
            ctypes.byref(process_list),
            ctypes.sizeof(process_list),
            ctypes.byref(returned_length),
        ):
            _raise_last_windows_error()
        count = int(process_list.NumberOfProcessIdsInList)
        if count > _JOB_PID_CAPACITY:
            raise RuntimeError("Job PID query exceeded the fixed verification capacity")
        return tuple(int(process_list.ProcessIdList[index]) for index in range(count))

    def verify_process_in_job(
        self,
        process_handle: int,
        job_handle: int,
        pid: int,
    ) -> None:
        is_member = wintypes.BOOL()
        if not self._is_process_in_job(
            wintypes.HANDLE(process_handle),
            wintypes.HANDLE(job_handle),
            ctypes.byref(is_member),
        ):
            _raise_last_windows_error()
        process_ids = self.process_ids(job_handle)
        if not is_member.value or process_ids != (pid,):
            raise RuntimeError(
                "suspended root process was not the sole member of the requested Job"
            )
        if self.active_process_count(job_handle) != 1:
            raise RuntimeError("suspended Job active-process count was not exactly one")


@dataclass(slots=True)
class LaunchResourceOwner:
    """Stable, transferable owner created before the first native launch call."""

    _api: Any = field(repr=False)
    job_handle: int = field(repr=False)
    process_information: _PROCESS_INFORMATION = field(
        default_factory=_PROCESS_INFORMATION,
        init=False,
        repr=False,
    )
    state: LaunchResourceState = field(
        default=LaunchResourceState.PREPARED,
        init=False,
    )
    membership_proven: bool = field(default=False, init=False)
    root_signaled: bool = field(default=False, init=False)
    job_empty_proven: bool = field(default=False, init=False)
    pid: int = field(default=0, init=False)
    tid: int = field(default=0, init=False)
    stdin_handle: _OwnedNativeHandle = field(init=False, repr=False)
    stdout_handle: _OwnedNativeHandle = field(init=False, repr=False)
    stderr_handle: _OwnedNativeHandle = field(init=False, repr=False)
    process_handle: _OwnedNativeHandle = field(init=False, repr=False)
    thread_handle: _OwnedNativeHandle = field(init=False, repr=False)
    attribute_list: _OwnedAttributeList = field(init=False, repr=False)
    suspended_process: SuspendedJobProcess = field(init=False, repr=False)
    resource_error_envelope: WindowsAtomicProcessResourceError = field(
        init=False,
        repr=False,
    )
    containment_error_envelope: WindowsAtomicProcessContainmentError = field(
        init=False,
        repr=False,
    )
    emergency_error_envelope: WindowsAtomicProcessResourceError = field(
        init=False,
        repr=False,
    )
    _handoff_transferred: bool = field(default=False, init=False, repr=False)
    command_line: object | None = field(default=None, init=False, repr=False)
    environment_block: object | None = field(default=None, init=False, repr=False)
    job_handle_array: object | None = field(default=None, init=False, repr=False)
    standard_handle_array: object | None = field(default=None, init=False, repr=False)
    startup_info: object | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.stdin_handle = _OwnedNativeHandle(self._api)
        self.stdout_handle = _OwnedNativeHandle(self._api)
        self.stderr_handle = _OwnedNativeHandle(self._api)
        self.process_handle = _OwnedNativeHandle(self._api)
        self.thread_handle = _OwnedNativeHandle(self._api)
        self.attribute_list = _OwnedAttributeList()
        self.suspended_process = SuspendedJobProcess(self)
        self.resource_error_envelope = WindowsAtomicProcessResourceError(
            "atomic launch resource handoff is not configured",
            resource_owner=self,
        )
        self.containment_error_envelope = WindowsAtomicProcessContainmentError(
            "atomic launch containment handoff is not configured",
            resource_owner=self,
        )
        self.emergency_error_envelope = WindowsAtomicProcessResourceError(
            "atomic launch failed while preparing its resource handoff",
            resource_owner=self,
        )

    @property
    def process_created(self) -> bool:
        return bool(
            self.process_handle.live
            or self.thread_handle.live
            or _raw_handle_value(self.process_information.hProcess)
            or _raw_handle_value(self.process_information.hThread)
        )

    @property
    def attribute_list_live(self) -> bool:
        return self.attribute_list.initialized

    def synchronize_process_information(self) -> None:
        self.pid = int(self.process_information.dwProcessId)
        self.tid = int(self.process_information.dwThreadId)
        raw_process = _handle_to_int(self.process_information.hProcess)
        if raw_process:
            if not self.process_handle.live:
                self.process_handle.adopt(raw_process)
            elif self.process_handle.value != raw_process:
                raise RuntimeError("process handle owner diverged from PROCESS_INFORMATION")
            self.process_information.hProcess = None
        raw_thread = _handle_to_int(self.process_information.hThread)
        if raw_thread:
            if not self.thread_handle.live:
                self.thread_handle.adopt(raw_thread)
            elif self.thread_handle.value != raw_thread:
                raise RuntimeError("thread handle owner diverged from PROCESS_INFORMATION")
            self.process_information.hThread = None
        if raw_process or raw_thread:
            self.state = LaunchResourceState.PROCESS_CREATED_UNVERIFIED

    def mark_membership_proven(self) -> None:
        self.membership_proven = True
        self.state = LaunchResourceState.MEMBERSHIP_PROVEN

    def mark_containment_proven(self) -> None:
        self.state = LaunchResourceState.CONTAINMENT_PROVEN

    def mark_transferred_to_process(self) -> None:
        if self._handoff_transferred:
            raise RuntimeError("launch resources were already transferred")
        self._handoff_transferred = True
        self.state = LaunchResourceState.TRANSFERRED_TO_PROCESS

    def transfer_to_quarantine(self) -> LaunchResourceOwner:
        if self._handoff_transferred:
            raise RuntimeError("launch resources were already transferred")
        self._handoff_transferred = True
        self.state = LaunchResourceState.TRANSFERRED_FOR_QUARANTINE
        return self

    def live_handle_values(self) -> tuple[int, ...]:
        ordered = (
            self.stdin_handle,
            self.stdout_handle,
            self.stderr_handle,
            self.process_handle,
            self.thread_handle,
        )
        values = [handle.value for handle in ordered if handle.live]
        raw_process = _raw_handle_value(self.process_information.hProcess)
        raw_thread = _raw_handle_value(self.process_information.hThread)
        for raw in (raw_process, raw_thread):
            if raw and raw not in values:
                values.append(raw)
        return tuple(values)

    def cleanup_temporaries_stop_on_failure(self) -> list[BaseException]:
        errors = self._cleanup_stop_on_failure(include_process_handles=False)
        if not errors:
            self._release_launch_buffers()
        return errors

    def cleanup_all_stop_on_failure(self) -> list[BaseException]:
        try:
            self.synchronize_process_information()
        except BaseException as exc:
            return [exc]
        errors = self._cleanup_stop_on_failure(include_process_handles=True)
        if not errors:
            self._release_launch_buffers()
        return errors

    def _cleanup_stop_on_failure(
        self,
        *,
        include_process_handles: bool,
    ) -> list[BaseException]:
        try:
            self.attribute_list.delete(self._api)
        except BaseException as exc:
            return [exc]
        handles: list[_OwnedNativeHandle] = [
            self.stderr_handle,
            self.stdout_handle,
            self.stdin_handle,
        ]
        if include_process_handles:
            handles.extend([self.thread_handle, self.process_handle])
        for handle in handles:
            try:
                handle.close()
            except BaseException as exc:
                return [exc]
        self._refresh_closed_state()
        return []

    def retry_failed_launch_cleanup(self) -> bool:
        if self.process_created and self.state not in {
            LaunchResourceState.CONTAINMENT_PROVEN,
            LaunchResourceState.CLOSED,
        }:
            containment_errors = _contain_failed_launch(self)
            if containment_errors or self.state is not LaunchResourceState.CONTAINMENT_PROVEN:
                return False
        return not self.cleanup_all_stop_on_failure() and self.state is LaunchResourceState.CLOSED

    def _refresh_closed_state(self) -> None:
        if (
            not self.attribute_list.initialized
            and not self.live_handle_values()
            and not _raw_handle_value(self.process_information.hProcess)
            and not _raw_handle_value(self.process_information.hThread)
        ):
            self.state = LaunchResourceState.CLOSED

    def _release_launch_buffers(self) -> None:
        self.command_line = None
        self.environment_block = None
        self.job_handle_array = None
        self.standard_handle_array = None
        self.startup_info = None


class WindowsAtomicProcessResourceError(RuntimeError):
    """A launch error carrying the pre-existing owner of every live resource."""

    def __init__(
        self,
        message: str,
        *,
        resource_owner: LaunchResourceOwner,
        cause: BaseException | None = None,
        cleanup_errors: Sequence[BaseException] = (),
    ) -> None:
        self.resource_owner = resource_owner
        self.process = resource_owner.suspended_process
        self.cause = cause
        self.cleanup_errors = tuple(cleanup_errors)
        super().__init__(message)

    def configure(
        self,
        message: str,
        *,
        cause: BaseException,
        cleanup_error_groups: Sequence[Sequence[BaseException]],
    ) -> None:
        cleanup_errors: list[BaseException] = []
        for group in cleanup_error_groups:
            cleanup_errors.extend(group)
        self.cause = cause
        self.cleanup_errors = tuple(cleanup_errors)
        RuntimeError.__init__(self, message)


class WindowsAtomicProcessContainmentError(WindowsAtomicProcessResourceError):
    """Post-create failure whose Job/root termination is not fully proven."""


def _configure_error_envelope(
    envelope: WindowsAtomicProcessResourceError,
    message: str,
    *,
    cause: BaseException,
    cleanup_error_groups: Sequence[Sequence[BaseException]],
) -> None:
    envelope.configure(
        message,
        cause=cause,
        cleanup_error_groups=cleanup_error_groups,
    )


def _raise_prebuilt_resource_error(
    owner: LaunchResourceOwner,
    message: str,
    *,
    cause: BaseException,
    cleanup_error_groups: Sequence[Sequence[BaseException]],
    containment: bool = False,
) -> None:
    envelope = (
        owner.containment_error_envelope
        if containment
        else owner.resource_error_envelope
    )
    try:
        _configure_error_envelope(
            envelope,
            message,
            cause=cause,
            cleanup_error_groups=cleanup_error_groups,
        )
    except BaseException:
        raise owner.emergency_error_envelope from None
    raise envelope from cause


@dataclass(slots=True)
class SuspendedJobProcess:
    _resources: LaunchResourceOwner = field(repr=False)
    _resume_attempted: bool = field(default=False, init=False, repr=False)
    _resume_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    @property
    def process_handle(self) -> _OwnedNativeHandle:
        return self._resources.process_handle

    @property
    def thread_handle(self) -> _OwnedNativeHandle:
        return self._resources.thread_handle

    @property
    def pid(self) -> int:
        return self._resources.pid

    @property
    def tid(self) -> int:
        return self._resources.tid

    def resume(self) -> int:
        with self._resume_lock:
            if self._resume_attempted:
                raise RuntimeError("ResumeThread was already attempted for this process")
            self._resume_attempted = True
            previous_suspend_count = int(
                self._resources._api.resume_thread(self.thread_handle.value)
            )
            if previous_suspend_count != 1:
                raise RuntimeError(
                    "ResumeThread previous suspend count must be exactly 1"
                )
            return previous_suspend_count

    def wait_until_exit(self, timeout_seconds: float) -> bool:
        timeout_milliseconds = _timeout_to_milliseconds(timeout_seconds)
        signaled = bool(
            self._resources._api.wait_for_handle(
                self.process_handle.value,
                timeout_milliseconds,
            )
        )
        if signaled:
            self._resources.root_signaled = True
        return signaled

    def exit_code(self) -> int:
        return int(self._resources._api.get_exit_code(self.process_handle.value))

    def close_thread_handle(self) -> None:
        self.thread_handle.close()

    def close_process_handle(self) -> None:
        self.process_handle.close()

    def close(self) -> None:
        errors: list[BaseException] = []
        for handle in (self.thread_handle, self.process_handle):
            try:
                handle.close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("failed to close launched-process handles", errors)
        self._resources._refresh_closed_state()


def _new_launch_resource_owner(
    native_api: Any,
    job_handle: int,
) -> LaunchResourceOwner:
    return LaunchResourceOwner(native_api, job_handle)


_NATIVE_API: Any = _NativeWindowsApi() if os.name == "nt" else None


def create_suspended_process_in_job(
    *,
    application_path: Path,
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    job_handle: object,
) -> SuspendedJobProcess:
    if _NATIVE_API is None:
        raise WindowsAtomicProcessUnsupported(
            "atomic Windows process creation is unavailable"
        )

    application_name, cwd_text = _validate_paths(application_path, cwd)
    command_line = _build_command_line(application_name, argv)
    environment_block = _build_environment_block(environment)
    borrowed_job_handle = _coerce_handle(job_handle, "job_handle")
    owner = _new_launch_resource_owner(_NATIVE_API, borrowed_job_handle)
    owner.command_line = command_line
    owner.environment_block = environment_block
    try:
        if _NATIVE_API.get_handle_flags(borrowed_job_handle) & _HANDLE_FLAG_INHERIT:
            raise ValueError("job_handle must be non-inheritable")

        _NATIVE_API.create_nul_handle(_GENERIC_READ, owner.stdin_handle)
        _NATIVE_API.create_nul_handle(_GENERIC_WRITE, owner.stdout_handle)
        _NATIVE_API.create_nul_handle(_GENERIC_WRITE, owner.stderr_handle)
        standard_handles = (
            owner.stdin_handle.value,
            owner.stdout_handle.value,
            owner.stderr_handle.value,
        )
        if len(set(standard_handles)) != 3:
            raise RuntimeError("standard NUL handles must be three distinct handles")

        _NATIVE_API.initialize_attribute_list(2, owner.attribute_list)
        owner.job_handle_array = (wintypes.HANDLE * 1)(borrowed_job_handle)
        owner.standard_handle_array = (wintypes.HANDLE * 3)(
            *standard_handles,
        )
        _NATIVE_API.update_attribute(
            owner.attribute_list,
            PROC_THREAD_ATTRIBUTE_JOB_LIST,
            owner.job_handle_array,
        )
        _NATIVE_API.update_attribute(
            owner.attribute_list,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            owner.standard_handle_array,
        )

        startup_info = _STARTUPINFOEXW()
        startup_info.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        startup_info.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        startup_info.StartupInfo.hStdInput = wintypes.HANDLE(standard_handles[0])
        startup_info.StartupInfo.hStdOutput = wintypes.HANDLE(standard_handles[1])
        startup_info.StartupInfo.hStdError = wintypes.HANDLE(standard_handles[2])
        startup_info.lpAttributeList = owner.attribute_list.pointer
        owner.startup_info = startup_info

        creation_flags = (
            CREATE_SUSPENDED
            | CREATE_NO_WINDOW
            | CREATE_UNICODE_ENVIRONMENT
            | EXTENDED_STARTUPINFO_PRESENT
        )
        _NATIVE_API.create_process(
            application_name=application_name,
            command_line=owner.command_line,
            cwd=cwd_text,
            environment_block=owner.environment_block,
            creation_flags=creation_flags,
            inherit_handles=True,
            startup_info=startup_info,
            process_information=owner.process_information,
        )
        owner.synchronize_process_information()
        _NATIVE_API.verify_process_in_job(
            owner.process_handle.value,
            borrowed_job_handle,
            owner.pid,
        )
        owner.mark_membership_proven()

        cleanup_errors = owner.cleanup_temporaries_stop_on_failure()
        if cleanup_errors:
            raise cleanup_errors[0]
        owner.mark_transferred_to_process()
        return owner.suspended_process
    except BaseException as primary_error:
        synchronization_errors: list[BaseException] = []
        try:
            owner.synchronize_process_information()
        except BaseException as exc:
            synchronization_errors.append(exc)
        if owner.process_created:
            containment_errors = _contain_failed_launch(owner)
            if owner.state is not LaunchResourceState.CONTAINMENT_PROVEN:
                _raise_prebuilt_resource_error(
                    owner,
                    "atomic launch failed and containment is unproven",
                    cause=primary_error,
                    cleanup_error_groups=(
                        synchronization_errors,
                        containment_errors,
                    ),
                    containment=True,
                )
            try:
                cleanup_errors = owner.cleanup_all_stop_on_failure()
            except BaseException as cleanup_error:
                _raise_prebuilt_resource_error(
                    owner,
                    "atomic launch containment succeeded but native cleanup failed",
                    cause=primary_error,
                    cleanup_error_groups=(
                        synchronization_errors,
                        containment_errors,
                        (cleanup_error,),
                    ),
                )
            if cleanup_errors:
                _raise_prebuilt_resource_error(
                    owner,
                    "atomic launch containment succeeded but native cleanup is incomplete",
                    cause=primary_error,
                    cleanup_error_groups=(
                        synchronization_errors,
                        containment_errors,
                        cleanup_errors,
                    ),
                )
            raise
        try:
            cleanup_errors = owner.cleanup_all_stop_on_failure()
        except BaseException as cleanup_error:
            _raise_prebuilt_resource_error(
                owner,
                "atomic launch failed before CreateProcess and native cleanup failed",
                cause=primary_error,
                cleanup_error_groups=(
                    synchronization_errors,
                    (cleanup_error,),
                ),
            )
        if cleanup_errors:
            _raise_prebuilt_resource_error(
                owner,
                "atomic launch failed before CreateProcess and cleanup is incomplete",
                cause=primary_error,
                cleanup_error_groups=(synchronization_errors, cleanup_errors),
            )
        raise


def _contain_failed_launch(owner: LaunchResourceOwner) -> list[BaseException]:
    errors: list[BaseException] = []
    try:
        owner._api.terminate_job(owner.job_handle, 0xE0000003)
    except BaseException as exc:
        errors.append(exc)

    root_handle: int | None = None
    try:
        root_handle = _root_handle_value(owner)
    except BaseException as exc:
        errors.append(exc)
    if root_handle is None:
        errors.append(RuntimeError("CreateProcess returned no root process handle"))
    else:
        if not owner.membership_proven:
            try:
                owner._api.terminate_process(root_handle, 0xE0000003)
            except BaseException as exc:
                errors.append(exc)
        try:
            owner.root_signaled = bool(
                owner._api.wait_for_handle(root_handle, 5_000)
            )
        except BaseException as exc:
            errors.append(exc)

    try:
        owner.job_empty_proven = _wait_until_job_empty(
            owner._api,
            owner.job_handle,
        )
    except BaseException as exc:
        errors.append(exc)
    if owner.root_signaled and owner.job_empty_proven:
        owner.mark_containment_proven()
    return errors


def _wait_until_job_empty(
    native_api: Any,
    job_handle: int,
    *,
    timeout_seconds: float = 5.0,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if native_api.active_process_count(job_handle) == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _root_handle_value(owner: LaunchResourceOwner) -> int | None:
    if owner.process_handle.live:
        return owner.process_handle.value
    raw = _handle_to_int(owner.process_information.hProcess)
    return raw or None


def _validate_paths(application_path: Path, cwd: Path) -> tuple[str, str]:
    application = _validate_absolute_d_path(
        application_path,
        label="application_path",
        expect_file=True,
    )
    working_directory = _validate_absolute_d_path(
        cwd,
        label="cwd",
        expect_file=False,
    )
    return str(application), str(working_directory)


def _validate_absolute_d_path(
    value: Path,
    *,
    label: str,
    expect_file: bool,
) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{label} must be a Path")
    raw = str(value)
    drive, tail = ntpath.splitdrive(raw)
    if (
        "\x00" in raw
        or raw.startswith(("\\\\", "//"))
        or drive.casefold() != "d:"
        or not tail.startswith(("\\", "/"))
    ):
        raise ValueError(f"{label} must be an absolute path on approved D:")
    try:
        resolved = value.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} does not exist") from exc
    resolved_drive, _resolved_tail = ntpath.splitdrive(str(resolved))
    if resolved_drive.casefold() != "d:":
        raise ValueError(f"{label} must resolve to approved D:")
    if expect_file and not resolved.is_file():
        raise ValueError(f"{label} must be an existing file")
    if not expect_file and not resolved.is_dir():
        raise ValueError(f"{label} must be an existing directory")
    return resolved


def _build_command_line(
    application_name: str,
    argv: Sequence[str],
) -> ctypes.Array[Any]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv:
        raise ValueError("argv must be a non-empty sequence of strings")
    arguments = tuple(argv)
    if any(type(argument) is not str or "\x00" in argument for argument in arguments):
        raise ValueError("argv must contain only NUL-free strings")
    if ntpath.normcase(ntpath.normpath(arguments[0])) != ntpath.normcase(
        ntpath.normpath(application_name)
    ):
        raise ValueError("argv[0] must exactly identify application_path")
    command_line_text = subprocess.list2cmdline(arguments)
    if len(command_line_text) >= 32_767:
        raise ValueError("Windows command line exceeds the supported length")
    return ctypes.create_unicode_buffer(command_line_text)


def _build_environment_block(environment: Mapping[str, str]) -> ctypes.Array[Any]:
    if not isinstance(environment, Mapping):
        raise TypeError("environment must be a mapping")
    entries: list[tuple[str, str]] = []
    folded_names: set[str] = set()
    for name, value in environment.items():
        if (
            type(name) is not str
            or not name
            or "=" in name
            or "\x00" in name
            or type(value) is not str
            or "\x00" in value
        ):
            raise ValueError("environment must contain valid NUL-free name/value strings")
        try:
            name.encode("utf-16-le", errors="strict")
            value.encode("utf-16-le", errors="strict")
        except UnicodeError as exc:
            raise ValueError("environment contains invalid Unicode") from exc
        folded_name = name.casefold()
        if folded_name in folded_names:
            raise ValueError("environment contains duplicate case-insensitive names")
        folded_names.add(folded_name)
        entries.append((name, value))
    entries.sort(key=lambda item: item[0].casefold())
    block_text = "\x00".join(f"{name}={value}" for name, value in entries) + "\x00"
    return ctypes.create_unicode_buffer(block_text)


def _coerce_handle(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a valid Windows handle")
    try:
        handle = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a valid Windows handle") from exc
    if handle <= 0 or handle == _INVALID_HANDLE_VALUE:
        raise ValueError(f"{label} must be a valid Windows handle")
    return handle


def _timeout_to_milliseconds(timeout_seconds: float) -> int:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds < 0
    ):
        raise ValueError("timeout_seconds must be a finite non-negative number")
    milliseconds = math.ceil(float(timeout_seconds) * 1000.0)
    return min(milliseconds, 0xFFFFFFFE)


def _handle_to_int(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    raw_value = getattr(value, "value", value)
    return int(raw_value)


def _raw_handle_value(value: object) -> int:
    """Read ctypes HANDLE storage without using the fallible conversion hook."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    raw_value = getattr(value, "value", None)
    return raw_value if isinstance(raw_value, int) else 0


def _raise_last_windows_error() -> None:
    error = ctypes.get_last_error()
    raise ctypes.WinError(error)


__all__ = [
    "LaunchResourceOwner",
    "LaunchResourceState",
    "SuspendedJobProcess",
    "WindowsAtomicProcessContainmentError",
    "WindowsAtomicProcessResourceError",
    "WindowsAtomicProcessUnsupported",
    "create_suspended_process_in_job",
]
