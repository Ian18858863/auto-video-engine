from __future__ import annotations

import ast
import ctypes
import gc
import importlib
import importlib.util
import inspect
import json
import math
import os
import subprocess
import sys
import textwrap
import threading
import time
import uuid
import weakref
from collections import deque
from collections.abc import Iterator, MutableMapping
from dataclasses import FrozenInstanceError, dataclass, fields as dataclass_fields, is_dataclass, replace
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Callable, NoReturn, get_type_hints

import pytest
from ctypes import wintypes


SUBJECT_NAME = "app.pipeline.windows_overlapped_pipe_io"

ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_PIPE_CONNECTED = 535
ERROR_OPERATION_ABORTED = 995
ERROR_IO_INCOMPLETE = 996
ERROR_IO_PENDING = 997
ERROR_NOT_FOUND = 1168
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
MAX_FINITE_WAIT_MS = 0xFFFFFFFE
MAX_WAIT_SLICE_MS = 250
UINTPTR_MAX = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
FAILFAST_QUARANTINE_EXIT_CODE = 74
FAILFAST_RETURNED_EXIT_CODE = 75

_REAL_PIPE_ACCESS_DUPLEX = 0x00000003
_REAL_FILE_FLAG_OVERLAPPED = 0x40000000
_REAL_PIPE_TYPE_BYTE = 0x00000000
_REAL_PIPE_READMODE_BYTE = 0x00000000
_REAL_PIPE_WAIT = 0x00000000
_REAL_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_REAL_GENERIC_READ = 0x80000000
_REAL_GENERIC_WRITE = 0x40000000
_REAL_OPEN_EXISTING = 3

_QUARANTINE_CHILD_NODE_ENV = "TASK_3B2_QUARANTINE_CHILD_NODE"
_SWEEP_REENTRY_CHILD_NODE_ENV = "TASK_3B2_SWEEP_REENTRY_CHILD_NODE"
_REAL_PIPE_CHILD_NODE_ENV = "TASK_3B2_REAL_PIPE_CHILD_NODE"
_CONCURRENCY_CHILD_NODE_ENV = "TASK_3B2_CONCURRENCY_CHILD_NODE"
_SUBPROCESS_TEMP_ROOT = Path(r"D:\CodexData\tmp")

FRAME = b"\x00\x00\x00\x03abc"

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows overlapped pipe contract"
)


@pytest.fixture(autouse=True)
def _unload_subject_after_each_test():
    sys.modules.pop(SUBJECT_NAME, None)
    yield
    sys.modules.pop(SUBJECT_NAME, None)


@dataclass(frozen=True, slots=True)
class _Outcome:
    ok: bool
    error: int = 0
    byte_count: int = 0
    interrupt: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _EventOutcome:
    handle: int
    error: int = 0
    interrupt: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _WaitOutcome:
    value: int
    error: int = 0
    interrupt: BaseException | None = None


@dataclass(slots=True)
class _ManualClock:
    now: float

    def __call__(self) -> float:
        return self.now


class _ScriptedClock:
    __slots__ = ("now", "steps", "calls")

    def __init__(self, now: float) -> None:
        self.now = now
        self.steps: deque[float | BaseException] = deque()
        self.calls: list[float | BaseException] = []

    def arm_next(self, *steps: float | BaseException) -> None:
        self.steps.extend(steps)

    def __call__(self) -> float:
        step = self.steps.popleft() if self.steps else self.now
        self.calls.append(step)
        if isinstance(step, BaseException):
            raise step
        self.now = float(step)
        return self.now


class _FloatConvertible:
    def __float__(self) -> float:
        return 1.0


def _future_deadline() -> float:
    return time.monotonic() + 5.0


_UNSET = object()


def _integer(value: object) -> int:
    raw = getattr(value, "value", value)
    if raw is None:
        return 0
    return int(raw)


def _pointer_address(value: object) -> int:
    address = ctypes.cast(value, ctypes.c_void_p).value
    assert address is not None
    return int(address)


def _is_null_pointer(value: object) -> bool:
    if value is None:
        return True
    return ctypes.cast(value, ctypes.c_void_p).value is None


def _pointer_snapshot(value: object) -> int:
    return 0 if _is_null_pointer(value) else _pointer_address(value)


def _snapshot_native_call(
    name: str, args: tuple[object, ...]
) -> tuple[int | bool, ...]:
    if name == "CreateEventW":
        return (
            _pointer_snapshot(args[0]),
            bool(_integer(args[1])),
            bool(_integer(args[2])),
            _pointer_snapshot(args[3]),
        )
    if name == "ConnectNamedPipe":
        return (_integer(args[0]), _pointer_snapshot(args[1]))
    if name == "WriteFile":
        return (
            _integer(args[0]),
            _pointer_snapshot(args[1]),
            _integer(args[2]),
            _pointer_snapshot(args[3]),
            _pointer_snapshot(args[4]),
        )
    if name == "GetOverlappedResult":
        return (
            _integer(args[0]),
            _pointer_snapshot(args[1]),
            _pointer_snapshot(args[2]),
            bool(_integer(args[3])),
        )
    if name == "CancelIoEx":
        return (_integer(args[0]), _pointer_snapshot(args[1]))
    if name == "WaitForSingleObject":
        return (_integer(args[0]), _integer(args[1]))
    if name == "CloseHandle":
        return (_integer(args[0]),)
    raise AssertionError(f"unexpected native function: {name}")


class _FakeFunction:
    def __init__(self, kernel: _FakeKernel32, name: str) -> None:
        self.kernel = kernel
        self.name = name
        self.argtypes: object = _UNSET
        self.restype: object = _UNSET
        self.calls: list[tuple[int | bool, ...]] = []

    def __call__(self, *args: object) -> object:
        self.kernel._run_native_enter_hook(self.name)
        self.kernel.native_trace.append(self.name)
        self.calls.append(_snapshot_native_call(self.name, args))
        return self.kernel.dispatch(self.name, args)


class _FakeKernel32:
    def __init__(self) -> None:
        self.load_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.event_handles: deque[int | _EventOutcome] = deque()
        self.connect_outcomes: deque[_Outcome] = deque()
        self.write_outcomes: deque[_Outcome] = deque()
        self.result_outcomes: deque[_Outcome] = deque()
        self.cancel_outcomes: deque[_Outcome] = deque()
        self.wait_results: deque[int | _WaitOutcome] = deque()
        self.close_outcomes: deque[_Outcome] = deque()
        self.overlapped_snapshots: list[tuple[str, int, bytes]] = []
        self.write_payloads: list[bytes] = []
        self.write_buffer_addresses: list[int] = []
        self.write_count_pointers: list[int] = []
        self.created_event_handles: list[int] = []
        self.blockers: dict[str, tuple[threading.Event, threading.Event]] = {}
        self.on_wait: Callable[[int], None] | None = None
        self.on_native_enter: dict[str, Callable[[], None]] = {}
        self.on_native_return: dict[str, Callable[[], None]] = {}
        self.native_trace: list[str] = []

        for name in (
            "ConnectNamedPipe",
            "WriteFile",
            "GetOverlappedResult",
            "CancelIoEx",
            "CreateEventW",
            "WaitForSingleObject",
            "CloseHandle",
        ):
            setattr(self, name, _FakeFunction(self, name))

    def dispatch(self, name: str, args: tuple[object, ...]) -> object:
        self._block_if_requested(name)

        if name == "CreateEventW":
            assert self.event_handles, "test must provide one event handle"
            raw = self.event_handles.popleft()
            outcome = raw if isinstance(raw, _EventOutcome) else _EventOutcome(raw)
            self.created_event_handles.append(outcome.handle)
            ctypes.set_last_error(outcome.error)
            self._raise_if_interrupted(outcome.interrupt)
            self._run_native_return_hook(name)
            return outcome.handle

        if name == "ConnectNamedPipe":
            outcome = self._next(self.connect_outcomes, name)
            pointer = _pointer_address(args[1])
            self.overlapped_snapshots.append(
                ("connect", pointer, ctypes.string_at(pointer, 32))
            )
            ctypes.set_last_error(outcome.error)
            self._raise_if_interrupted(outcome.interrupt)
            self._run_native_return_hook(name)
            return outcome.ok

        if name == "WriteFile":
            outcome = self._next(self.write_outcomes, name)
            byte_count = _integer(args[2])
            self.write_payloads.append(ctypes.string_at(args[1], byte_count))
            self.write_buffer_addresses.append(_pointer_address(args[1]))
            self.write_count_pointers.append(_pointer_snapshot(args[3]))
            pointer = _pointer_address(args[4])
            self.overlapped_snapshots.append(
                ("write", pointer, ctypes.string_at(pointer, 32))
            )
            ctypes.set_last_error(outcome.error)
            self._raise_if_interrupted(outcome.interrupt)
            self._run_native_return_hook(name)
            return outcome.ok

        if name == "GetOverlappedResult":
            outcome = self._next(self.result_outcomes, name)
            target = ctypes.cast(args[2], ctypes.POINTER(wintypes.DWORD))
            target.contents.value = outcome.byte_count
            ctypes.set_last_error(outcome.error)
            self._raise_if_interrupted(outcome.interrupt)
            self._run_native_return_hook(name)
            return outcome.ok

        if name == "CancelIoEx":
            outcome = self._next(self.cancel_outcomes, name)
            ctypes.set_last_error(outcome.error)
            self._raise_if_interrupted(outcome.interrupt)
            self._run_native_return_hook(name)
            return outcome.ok

        if name == "WaitForSingleObject":
            assert self.wait_results, "test must provide one wait result"
            raw = self.wait_results.popleft()
            outcome = raw if isinstance(raw, _WaitOutcome) else _WaitOutcome(raw)
            if self.on_wait is not None and outcome.value == WAIT_TIMEOUT:
                self.on_wait(_integer(args[1]))
            ctypes.set_last_error(outcome.error)
            self._raise_if_interrupted(outcome.interrupt)
            self._run_native_return_hook(name)
            return outcome.value

        if name == "CloseHandle":
            outcome = (
                self.close_outcomes.popleft()
                if self.close_outcomes
                else _Outcome(True)
            )
            ctypes.set_last_error(outcome.error)
            self._raise_if_interrupted(outcome.interrupt)
            self._run_native_return_hook(name)
            return outcome.ok

        raise AssertionError(f"unexpected native function: {name}")

    @staticmethod
    def _next(values: deque[_Outcome], name: str) -> _Outcome:
        assert values, f"test must provide a {name} outcome"
        return values.popleft()

    def _block_if_requested(self, name: str) -> None:
        blocker = self.blockers.get(name)
        if blocker is None:
            return
        entered, release = blocker
        entered.set()
        assert release.wait(5.0), f"test did not release blocked {name}"

    @staticmethod
    def _raise_if_interrupted(interrupt: BaseException | None) -> None:
        if interrupt is not None:
            raise interrupt

    def _run_native_return_hook(self, name: str) -> None:
        callback = self.on_native_return.get(name)
        if callback is not None:
            callback()

    def _run_native_enter_hook(self, name: str) -> None:
        callback = self.on_native_enter.get(name)
        if callback is not None:
            callback()


def _load_subject(
    monkeypatch: pytest.MonkeyPatch, kernel: _FakeKernel32
) -> ModuleType:
    def fake_windll(
        name: str, *args: object, **kwargs: object
    ) -> _FakeKernel32:
        kernel.load_calls.append((name, args, kwargs))
        assert name == "kernel32"
        assert args == ()
        assert kwargs == {"use_last_error": True}
        return kernel

    monkeypatch.setattr(ctypes, "WinDLL", fake_windll)
    importlib.invalidate_caches()
    return importlib.import_module(SUBJECT_NAME)


class _RealPipeOverlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


def _real_pipe_kernel32() -> object:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    kernel.CreateNamedPipeW.restype = wintypes.HANDLE

    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE

    kernel.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
        ctypes.POINTER(_RealPipeOverlapped),
    ]
    kernel.ReadFile.restype = wintypes.BOOL

    kernel.CreateEventW.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel.CreateEventW.restype = wintypes.HANDLE

    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD

    kernel.CancelIoEx.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_RealPipeOverlapped),
    ]
    kernel.CancelIoEx.restype = wintypes.BOOL

    kernel.GetOverlappedResult.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_RealPipeOverlapped),
        wintypes.LPDWORD,
        wintypes.BOOL,
    ]
    kernel.GetOverlappedResult.restype = wintypes.BOOL

    kernel.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    kernel.DisconnectNamedPipe.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


class _RealNamedPipe:
    __slots__ = (
        "kernel",
        "name",
        "server_handle",
        "client_handles",
        "_defer_close_to_process_exit",
    )

    def __init__(self, *, outbound_buffer_bytes: int = 64 * 1024) -> None:
        assert sys.platform == "win32"
        self.kernel = _real_pipe_kernel32()
        self.name = rf"\\.\pipe\codex-task3b2-{os.getpid()}-{uuid.uuid4().hex}"
        raw_handle = self.kernel.CreateNamedPipeW(
            self.name,
            _REAL_PIPE_ACCESS_DUPLEX | _REAL_FILE_FLAG_OVERLAPPED,
            (
                _REAL_PIPE_TYPE_BYTE
                | _REAL_PIPE_READMODE_BYTE
                | _REAL_PIPE_WAIT
                | _REAL_PIPE_REJECT_REMOTE_CLIENTS
            ),
            1,
            outbound_buffer_bytes,
            64 * 1024,
            0,
            None,
        )
        self.server_handle = _integer(raw_handle)
        self.client_handles: list[int] = []
        self._defer_close_to_process_exit = False
        if self.server_handle in {0, UINTPTR_MAX}:
            error = ctypes.get_last_error()
            raise OSError(error, f"CreateNamedPipeW failed for {self.name}")

    def open_client(self) -> int:
        raw_handle = self.kernel.CreateFileW(
            self.name,
            _REAL_GENERIC_READ | _REAL_GENERIC_WRITE,
            0,
            None,
            _REAL_OPEN_EXISTING,
            _REAL_FILE_FLAG_OVERLAPPED,
            None,
        )
        handle = _integer(raw_handle)
        if handle in {0, UINTPTR_MAX}:
            error = ctypes.get_last_error()
            raise OSError(error, f"CreateFileW failed for {self.name}")
        self.client_handles.append(handle)
        return handle

    def defer_close_to_process_exit(self) -> None:
        self._defer_close_to_process_exit = True

    def close(self) -> None:
        if self._defer_close_to_process_exit:
            return

        cleanup_errors: list[OSError] = []
        unclosed_client_handles: list[int] = []
        for handle in self.client_handles:
            if not self.kernel.CloseHandle(handle):
                error = ctypes.get_last_error()
                unclosed_client_handles.append(handle)
                self.defer_close_to_process_exit()
                cleanup_errors.append(
                    OSError(error, f"CloseHandle(client) failed for {self.name}")
                )
        self.client_handles[:] = unclosed_client_handles
        if self.server_handle:
            handle = self.server_handle
            if not self.kernel.DisconnectNamedPipe(handle):
                error = ctypes.get_last_error()
                if error != ERROR_PIPE_NOT_CONNECTED:
                    cleanup_errors.append(
                        OSError(
                            error,
                            f"DisconnectNamedPipe failed for {self.name}",
                        )
                    )
            if not self.kernel.CloseHandle(handle):
                error = ctypes.get_last_error()
                self.defer_close_to_process_exit()
                cleanup_errors.append(
                    OSError(error, f"CloseHandle(server) failed for {self.name}")
                )
            else:
                self.server_handle = 0

        if len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        if cleanup_errors:
            raise ExceptionGroup(
                f"real named-pipe cleanup failed for {self.name}",
                cleanup_errors,
            )


def _load_real_subject() -> ModuleType:
    assert sys.platform == "win32"
    importlib.invalidate_caches()
    return importlib.import_module(SUBJECT_NAME)


def _cleanup_real_owner_before_pipe_close(
    module: ModuleType,
    owner: object | None,
) -> bool:
    if owner is None:
        return True

    active = owner
    seen: set[int] = set()
    for _ in range(16):
        if id(active) in seen:
            return False
        seen.add(id(active))
        if active.state is not module.OverlappedState.TRANSFERRED:
            break
        try:
            active = active.recover_transferred_owner()
        except BaseException:
            return False
    else:
        return False

    safe_states = {
        module.OverlappedState.CLOSED,
        module.OverlappedState.CLOSE_FAILED_QUARANTINED,
        module.OverlappedState.CLOSE_UNKNOWN,
    }
    if active.state in safe_states:
        return True

    if active.state not in {
        module.OverlappedState.NEW,
        module.OverlappedState.DRAINED,
    }:
        try:
            active.cancel_and_drain()
        except BaseException:
            pass

    if active.state in {
        module.OverlappedState.NEW,
        module.OverlappedState.DRAINED,
    }:
        try:
            active.close()
        except BaseException:
            pass

    return active.state in safe_states


def _finalize_real_pipe_owner(
    module: ModuleType,
    owner: object | None,
    pipe: _RealNamedPipe,
) -> None:
    error_in_flight = sys.exception()
    safe_to_close_pipe = _cleanup_real_owner_before_pipe_close(module, owner)
    if safe_to_close_pipe:
        return

    pipe.defer_close_to_process_exit()
    if error_in_flight is None:
        raise AssertionError(
            "real owner could not be proven terminal; "
            "raw pipe handles were deferred to isolated process exit"
        )


def _connect_real_client_before_accept(
    module: ModuleType,
    pipe: _RealNamedPipe,
) -> int:
    client_handle = pipe.open_client()
    connect_owner = None
    try:
        try:
            connect_owner = module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=pipe.server_handle,
                absolute_deadline=time.monotonic() + 5.0,
            )
        except module.OverlappedPipeError as error:
            connect_owner = error.resource_owner
            raise
        _assert_state(module, connect_owner, "DRAINED")
        _assert_completion(
            module,
            connect_owner.completion,
            operation="connect",
            native_outcome="success",
            bytes_transferred=None,
            winerror=None,
        )
        connect_owner.close()
        return client_handle
    finally:
        _finalize_real_pipe_owner(module, connect_owner, pipe)


def _drain_real_pipe_read_after_cancel(
    pipe: _RealNamedPipe,
    client_handle: int,
    overlapped: _RealPipeOverlapped,
    transferred: wintypes.DWORD,
    *,
    timeout_seconds: float = 5.0,
) -> tuple[bool, int | None]:
    event_handle = _integer(overlapped.hEvent)
    if event_handle in {0, UINTPTR_MAX}:
        pipe.defer_close_to_process_exit()
        raise AssertionError("cancelled real read has no valid dedicated event")

    wait_milliseconds = min(
        MAX_FINITE_WAIT_MS,
        max(1, math.ceil(timeout_seconds * 1000.0)),
    )
    wait_result = _integer(
        pipe.kernel.WaitForSingleObject(event_handle, wait_milliseconds)
    )
    if wait_result == WAIT_TIMEOUT:
        pipe.defer_close_to_process_exit()
        raise TimeoutError(
            "cancelled real named-pipe read did not reach its dedicated event "
            f"within {timeout_seconds}s"
        )
    if wait_result == WAIT_FAILED:
        error = ctypes.get_last_error()
        pipe.defer_close_to_process_exit()
        raise OSError(
            error,
            f"WaitForSingleObject(cancelled read) failed for {pipe.name}",
        )
    if wait_result != WAIT_OBJECT_0:
        pipe.defer_close_to_process_exit()
        raise AssertionError(
            "WaitForSingleObject(cancelled read) returned unexpected value "
            f"{wait_result}"
        )

    result_ok = bool(
        pipe.kernel.GetOverlappedResult(
            client_handle,
            ctypes.byref(overlapped),
            ctypes.byref(transferred),
            False,
        )
    )
    result_error = None if result_ok else ctypes.get_last_error() or None
    if not result_ok and result_error in {None, ERROR_IO_INCOMPLETE}:
        pipe.defer_close_to_process_exit()
        raise AssertionError(
            "cancelled real read event signaled without a known terminal result; "
            f"winerror={result_error!r}"
        )
    return result_ok, result_error


def _cancel_and_drain_real_pipe_read(
    pipe: _RealNamedPipe,
    client_handle: int,
    overlapped: _RealPipeOverlapped,
    transferred: wintypes.DWORD,
) -> tuple[bool, int | None, bool, int | None]:
    cancel_ok = bool(
        pipe.kernel.CancelIoEx(client_handle, ctypes.byref(overlapped))
    )
    cancel_error = None if cancel_ok else ctypes.get_last_error() or None
    result_ok, result_error = _drain_real_pipe_read_after_cancel(
        pipe,
        client_handle,
        overlapped,
        transferred,
    )
    return cancel_ok, cancel_error, result_ok, result_error


def _read_real_pipe_exact_with_timeout(
    pipe: _RealNamedPipe,
    client_handle: int,
    byte_count: int,
    *,
    timeout_seconds: float = 3.0,
) -> bytes:
    deadline = time.monotonic() + timeout_seconds
    payload = bytearray()

    while len(payload) < byte_count:
        remaining_bytes = byte_count - len(payload)
        buffer = ctypes.create_string_buffer(remaining_bytes)
        transferred = wintypes.DWORD(0)
        overlapped = _RealPipeOverlapped()
        raw_event = pipe.kernel.CreateEventW(None, True, False, None)
        event_handle = _integer(raw_event)
        if event_handle in {0, UINTPTR_MAX}:
            error = ctypes.get_last_error()
            raise OSError(error, f"CreateEventW failed for {pipe.name}")
        overlapped.hEvent = event_handle
        pending = False
        cancel_attempted = False
        event_close_safe = True

        try:
            pending = True
            read_ok = bool(
                pipe.kernel.ReadFile(
                    client_handle,
                    buffer,
                    remaining_bytes,
                    None,
                    ctypes.byref(overlapped),
                )
            )
            result_ready = read_ok
            if not read_ok:
                error = ctypes.get_last_error()
                if error != ERROR_IO_PENDING:
                    if error:
                        pending = False
                    raise OSError(error, f"ReadFile failed for {pipe.name}")
                remaining_seconds = deadline - time.monotonic()
                wait_milliseconds = (
                    0
                    if remaining_seconds <= 0.0
                    else min(
                        MAX_FINITE_WAIT_MS,
                        max(1, math.ceil(remaining_seconds * 1000.0)),
                    )
                )
                wait_result = _integer(
                    pipe.kernel.WaitForSingleObject(
                        event_handle,
                        wait_milliseconds,
                    )
                )
                if wait_result == WAIT_OBJECT_0:
                    result_ready = True
                else:
                    wait_error = (
                        ctypes.get_last_error() or None
                        if wait_result == WAIT_FAILED
                        else None
                    )
                    cancel_attempted = True
                    try:
                        cancel_status = _cancel_and_drain_real_pipe_read(
                            pipe,
                            client_handle,
                            overlapped,
                            transferred,
                        )
                    except BaseException:
                        pending = False
                        event_close_safe = False
                        pipe.defer_close_to_process_exit()
                        raise
                    else:
                        pending = False
                    if wait_result == WAIT_TIMEOUT:
                        raise TimeoutError(
                            "real named-pipe read exceeded "
                            f"{timeout_seconds}s; cancel/drain={cancel_status!r}"
                        )
                    if wait_result == WAIT_FAILED:
                        raise OSError(
                            wait_error or 0,
                            f"WaitForSingleObject(read) failed for {pipe.name}",
                        )
                    raise AssertionError(
                        "WaitForSingleObject(read) returned unexpected value "
                        f"{wait_result}; cancel/drain={cancel_status!r}"
                    )

            if result_ready:
                result_ok = bool(
                    pipe.kernel.GetOverlappedResult(
                        client_handle,
                        ctypes.byref(overlapped),
                        ctypes.byref(transferred),
                        False,
                    )
                )
                result_error = (
                    None if result_ok else ctypes.get_last_error() or None
                )
                if not result_ok:
                    if result_error not in {None, ERROR_IO_INCOMPLETE}:
                        pending = False
                    raise OSError(
                        result_error or 0,
                        f"GetOverlappedResult(read) failed for {pipe.name}",
                    )
                pending = False

            count = int(transferred.value)
            if count == 0:
                raise AssertionError("real named-pipe read made zero progress")
            if count > remaining_bytes:
                raise AssertionError(
                    "real named-pipe read reported more bytes than requested"
                )
            payload.extend(buffer.raw[:count])
        finally:
            if pending:
                try:
                    if cancel_attempted:
                        _drain_real_pipe_read_after_cancel(
                            pipe,
                            client_handle,
                            overlapped,
                            transferred,
                        )
                    else:
                        cancel_attempted = True
                        _cancel_and_drain_real_pipe_read(
                            pipe,
                            client_handle,
                            overlapped,
                            transferred,
                        )
                    pending = False
                except BaseException:
                    pipe.defer_close_to_process_exit()
                    event_close_safe = False
                    raise
            if event_close_safe and not pipe.kernel.CloseHandle(event_handle):
                error = ctypes.get_last_error()
                raise OSError(
                    error,
                    f"CloseHandle(read event) failed for {pipe.name}",
                )

    return bytes(payload)


def _delegate_real_pipe_node_to_subprocess(
    request: pytest.FixtureRequest,
) -> subprocess.CompletedProcess[str] | None:
    nodeid = f"{Path(__file__).resolve()}::{request.node.name}"
    if os.environ.get(_REAL_PIPE_CHILD_NODE_ENV) == nodeid:
        return None

    executable = Path(sys.executable).resolve()
    assert executable.drive.casefold() == "d:", (
        f"real-pipe watchdog must use D-drive Python, got {executable}"
    )
    backend_root = Path(__file__).resolve().parents[1]
    basetemp = _SUBPROCESS_TEMP_ROOT / f"task-3b2-real-pipe-{uuid.uuid4().hex}"
    environment = os.environ.copy()
    environment.update(
        {
            _REAL_PIPE_CHILD_NODE_ENV: nodeid,
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(_SUBPROCESS_TEMP_ROOT),
            "TMP": str(_SUBPROCESS_TEMP_ROOT),
        }
    )

    try:
        completed = subprocess.run(
            [
                str(executable),
                "-m",
                "pytest",
                "-q",
                "-s",
                "-p",
                "no:cacheprovider",
                nodeid,
                f"--basetemp={basetemp}",
            ],
            cwd=backend_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(
            "real Windows pipe node exceeded the 45-second hard watchdog\n"
            f"nodeid={nodeid}\n"
            f"stdout={error.stdout!r}\n"
            f"stderr={error.stderr!r}"
        )

    assert completed.returncode == 0, (
        "real Windows pipe subprocess failed\n"
        f"nodeid={nodeid}\n"
        f"actual={completed.returncode}\n"
        f"stdout={completed.stdout}\n"
        f"stderr={completed.stderr}"
    )
    return completed


def _delegate_concurrency_node_to_subprocess(
    request: pytest.FixtureRequest,
) -> subprocess.CompletedProcess[str] | None:
    nodeid = f"{Path(__file__).resolve()}::{request.node.name}"
    if os.environ.get(_CONCURRENCY_CHILD_NODE_ENV) == nodeid:
        return None

    executable = Path(sys.executable).resolve()
    assert executable.drive.casefold() == "d:", (
        f"concurrency watchdog must use D-drive Python, got {executable}"
    )
    backend_root = Path(__file__).resolve().parents[1]
    run_token = uuid.uuid4().hex
    basetemp = _SUBPROCESS_TEMP_ROOT / f"task-3b2-concurrency-{run_token}"
    pycache = _SUBPROCESS_TEMP_ROOT / f"task-3b2-concurrency-pycache-{run_token}"
    environment = os.environ.copy()
    environment.update(
        {
            _CONCURRENCY_CHILD_NODE_ENV: nodeid,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(pycache),
            "TEMP": str(_SUBPROCESS_TEMP_ROOT),
            "TMP": str(_SUBPROCESS_TEMP_ROOT),
        }
    )

    try:
        completed = subprocess.run(
            [
                str(executable),
                "-m",
                "pytest",
                "-q",
                "-s",
                "-p",
                "no:cacheprovider",
                nodeid,
                f"--basetemp={basetemp}",
            ],
            cwd=backend_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(
            "concurrency node exceeded the 30-second hard watchdog\n"
            f"nodeid={nodeid}\n"
            f"stdout={error.stdout!r}\n"
            f"stderr={error.stderr!r}"
        )

    assert completed.returncode == 0, (
        "concurrency subprocess failed\n"
        f"nodeid={nodeid}\n"
        f"actual={completed.returncode}\n"
        f"stdout={completed.stdout}\n"
        f"stderr={completed.stderr}"
    )
    return completed


def _delegate_quarantine_node_to_subprocess(
    request: pytest.FixtureRequest,
    *,
    expected_exit_code: int,
) -> subprocess.CompletedProcess[str] | None:
    nodeid = request.node.nodeid
    if os.environ.get(_QUARANTINE_CHILD_NODE_ENV) == nodeid:
        return None

    executable = Path(sys.executable).resolve()
    assert executable.drive.casefold() == "d:", (
        f"quarantine subprocess must use D-drive Python, got {executable}"
    )
    backend_root = Path(__file__).resolve().parents[1]
    basetemp = _SUBPROCESS_TEMP_ROOT / f"task-3b2-quarantine-{uuid.uuid4().hex}"
    environment = os.environ.copy()
    environment.update(
        {
            _QUARANTINE_CHILD_NODE_ENV: nodeid,
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(_SUBPROCESS_TEMP_ROOT),
            "TMP": str(_SUBPROCESS_TEMP_ROOT),
        }
    )
    completed = subprocess.run(
        [
            str(executable),
            "-m",
            "pytest",
            "-q",
            "-s",
            "-p",
            "no:cacheprovider",
            nodeid,
            f"--basetemp={basetemp}",
        ],
        cwd=backend_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert completed.returncode == expected_exit_code, (
        "quarantine subprocess returned the wrong exact exit code\n"
        f"nodeid={nodeid}\n"
        f"expected={expected_exit_code}\n"
        f"actual={completed.returncode}\n"
        f"stdout={completed.stdout}\n"
        f"stderr={completed.stderr}"
    )
    return completed


def _delegate_sweep_reentry_node_to_subprocess(
    request: pytest.FixtureRequest,
) -> subprocess.CompletedProcess[str] | None:
    nodeid = request.node.nodeid
    if os.environ.get(_SWEEP_REENTRY_CHILD_NODE_ENV) == nodeid:
        return None

    executable = Path(sys.executable).resolve()
    assert executable.drive.casefold() == "d:"
    backend_root = Path(__file__).resolve().parents[1]
    basetemp = (
        _SUBPROCESS_TEMP_ROOT
        / f"task-3b2-sweep-reentry-{uuid.uuid4().hex}"
    )
    environment = os.environ.copy()
    environment.update(
        {
            _SWEEP_REENTRY_CHILD_NODE_ENV: nodeid,
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(_SUBPROCESS_TEMP_ROOT),
            "TMP": str(_SUBPROCESS_TEMP_ROOT),
        }
    )

    try:
        completed = subprocess.run(
            [
                str(executable),
                "-m",
                "pytest",
                "-q",
                "-s",
                "-p",
                "no:cacheprovider",
                nodeid,
                f"--basetemp={basetemp}",
            ],
            cwd=backend_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"same-thread operation reentry deadlocked: {error}")

    assert completed.returncode == 0, (
        "reentry subprocess failed\n"
        f"stdout={completed.stdout}\n"
        f"stderr={completed.stderr}"
    )
    return completed


class _RegistryMutationProbe:
    __slots__ = ("fail_after", "interrupt_type", "trace")

    def __init__(
        self,
        *,
        fail_after: str | None = None,
        interrupt_type: type[BaseException] = RuntimeError,
    ) -> None:
        self.fail_after = fail_after
        self.interrupt_type = interrupt_type
        self.trace: list[str] = []

    def after_side_effect(self, label: str) -> None:
        self.trace.append(label)
        if self.fail_after == label:
            raise self.interrupt_type(f"registry {label} side effect committed")


class _DuckRegistryConsumeMarker:
    __slots__ = ("_probe", "_value")

    def __init__(self, value: object, probe: _RegistryMutationProbe) -> None:
        self._value = value
        self._probe = probe

    @property
    def value(self) -> object:
        return self._value

    @value.setter
    def value(self, value: object) -> None:
        self._value = value
        self._probe.after_side_effect("consume")


class _DuckRegistryCell:
    __slots__ = ("public_state",)

    def __init__(self, public_state: object) -> None:
        self.public_state = public_state


class _EqualButDistinctRegistryToken:
    __slots__ = ("_hash",)

    def __init__(self, peer: object) -> None:
        self._hash = hash(peer)

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other: object) -> bool:
        return True


class _EqualButDistinctRegistryCell(_DuckRegistryCell):
    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        return True


class _EqualButDistinctLifecycleValue:
    __slots__ = ("_peer",)

    def __init__(self, peer: object) -> None:
        self._peer = peer

    @property
    def __class__(self) -> type:
        return type(self._peer)

    def __getattr__(self, name: str) -> object:
        return getattr(self._peer, name)

    def __eq__(self, other: object) -> bool:
        return other is self or other is self._peer

    def __hash__(self) -> int:
        return hash(self._peer)


class _DuckRegistryEntry:
    __slots__ = (
        "_probe",
        "_quarantine_flag",
        "_registry_phase",
        "cell",
        "entry_token",
    )

    def __init__(
        self,
        *,
        entry_token: object,
        cell: _DuckRegistryCell,
        registry_phase: object,
        quarantine_flag: bool,
        probe: _RegistryMutationProbe,
    ) -> None:
        self.entry_token = entry_token
        self.cell = cell
        self._registry_phase = registry_phase
        self._quarantine_flag = quarantine_flag
        self._probe = probe

    @property
    def registry_phase(self) -> object:
        return self._registry_phase

    @registry_phase.setter
    def registry_phase(self, value: object) -> None:
        self._registry_phase = value
        self._probe.after_side_effect("phase")

    @property
    def quarantine_flag(self) -> bool:
        return self._quarantine_flag

    @quarantine_flag.setter
    def quarantine_flag(self, value: bool) -> None:
        self._quarantine_flag = value
        self._probe.after_side_effect("flag")

    def set_phase_without_probe(self, value: object) -> None:
        self._registry_phase = value

    def set_flag_without_probe(self, value: bool) -> None:
        self._quarantine_flag = value


class _EqualButDistinctRegistryEntry(_DuckRegistryEntry):
    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        return True


class _DuckRegistryEntries(MutableMapping[object, object]):
    __slots__ = ("_items", "_probe")

    def __init__(
        self,
        *,
        seed: dict[object, object],
        probe: _RegistryMutationProbe,
    ) -> None:
        self._items = dict(seed)
        self._probe = probe

    def __getitem__(self, key: object) -> object:
        return self._items[key]

    def __setitem__(self, key: object, value: object) -> None:
        self._items[key] = value
        self._probe.after_side_effect("insert")

    def __delitem__(self, key: object) -> None:
        del self._items[key]
        self._probe.after_side_effect("delete")

    def __iter__(self) -> Iterator[object]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def install_without_probe(self, key: object, value: object) -> None:
        self._items[key] = value

    def remove_without_probe(self, key: object) -> None:
        self._items.pop(key, None)

    def identity_graph(self) -> tuple[tuple[object, ...], ...]:
        graph: list[tuple[object, ...]] = []
        for token, entry in self._items.items():
            graph.append(
                (
                    id(token),
                    id(entry),
                    id(entry.entry_token),
                    id(entry.cell),
                    _enum_value(entry.registry_phase),
                    entry.quarantine_flag,
                    _enum_value(entry.cell.public_state),
                )
            )
        return tuple(graph)


class _DuckRegistry:
    __slots__ = ("_probe", "_quarantine_count", "entries")

    def __init__(
        self,
        *,
        seed: dict[object, object],
        quarantine_count: int,
        probe: _RegistryMutationProbe,
    ) -> None:
        self.entries = _DuckRegistryEntries(seed=seed, probe=probe)
        self._quarantine_count = quarantine_count
        self._probe = probe

    @property
    def quarantine_count(self) -> int:
        return self._quarantine_count

    @quarantine_count.setter
    def quarantine_count(self, value: int) -> None:
        self._quarantine_count = value
        self._probe.after_side_effect("count")

    def set_count_without_probe(self, value: int) -> None:
        self._quarantine_count = value


class _RegistryTransitionCase:
    __slots__ = ("cell", "entry", "probe", "registry", "token", "transition")

    def __init__(
        self,
        *,
        cell: _DuckRegistryCell,
        entry: _DuckRegistryEntry,
        probe: _RegistryMutationProbe,
        registry: _DuckRegistry,
        token: object,
        transition: object,
    ) -> None:
        self.cell = cell
        self.entry = entry
        self.probe = probe
        self.registry = registry
        self.token = token
        self.transition = transition


def _make_registry_transition_case(
    module: ModuleType,
    mode_name: str,
    *,
    probe: _RegistryMutationProbe | None = None,
) -> _RegistryTransitionCase:
    active_probe = probe or _RegistryMutationProbe()
    defaults = {
        "RESERVE_INSERT": ("EMPTY", "CREATE_EVENT_IN_FLIGHT", False),
        "CLOSED_REMOVE": ("CLOSED", "CLOSED", True),
        "QUARANTINE": ("LIVE", "CLOSE_UNKNOWN", True),
    }
    phase_name, public_state_name, initially_present = defaults[mode_name]
    token = object()
    cell = _DuckRegistryCell(getattr(module.OverlappedState, public_state_name))
    entry = _DuckRegistryEntry(
        entry_token=token,
        cell=cell,
        registry_phase=getattr(module._RegistryPhase, phase_name),
        quarantine_flag=False,
        probe=active_probe,
    )
    registry = _DuckRegistry(
        seed={token: entry} if initially_present else {},
        quarantine_count=0,
        probe=active_probe,
    )
    consume_state = _DuckRegistryConsumeMarker(
        module._RegistryConsumeState.NEVER,
        active_probe,
    )
    transition = module._RegistryTransition(
        mode=getattr(module._RegistryTransitionMode, mode_name),
        entry_token=token,
        expected_entry=entry,
        expected_cell=cell,
        consume_state=consume_state,
    )
    return _RegistryTransitionCase(
        cell=cell,
        entry=entry,
        probe=active_probe,
        registry=registry,
        token=token,
        transition=transition,
    )


def _registry_transition_metadata(transition: object) -> tuple[int, ...]:
    return (
        id(transition.mode),
        id(transition.entry_token),
        id(transition.expected_entry),
        id(transition.expected_cell),
        id(transition.consume_state),
    )


def _registry_graph_snapshot(
    registry: _DuckRegistry,
    transition: object,
) -> tuple[object, ...]:
    expected_entry = transition.expected_entry
    return (
        _registry_transition_metadata(transition),
        _enum_value(transition.consume_state.value),
        registry.entries.identity_graph(),
        registry.quarantine_count,
        id(expected_entry),
        id(expected_entry.entry_token),
        id(expected_entry.cell),
        _enum_value(expected_entry.registry_phase),
        expected_entry.quarantine_flag,
        _enum_value(transition.expected_cell.public_state),
    )


def _equal_but_distinct_registry_entry(
    case: _RegistryTransitionCase,
) -> _EqualButDistinctRegistryEntry:
    return _EqualButDistinctRegistryEntry(
        entry_token=case.token,
        cell=case.cell,
        registry_phase=case.entry.registry_phase,
        quarantine_flag=case.entry.quarantine_flag,
        probe=_RegistryMutationProbe(),
    )


class _TrackedSweepLock:
    __slots__ = ("_lock", "owner_thread")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.owner_thread: int | None = None

    def acquire(
        self,
        blocking: bool = True,
        timeout: float = -1.0,
    ) -> bool:
        acquired = (
            self._lock.acquire(blocking)
            if timeout == -1.0
            else self._lock.acquire(blocking, timeout)
        )
        if acquired:
            assert self.owner_thread is None
            self.owner_thread = threading.get_ident()
        return acquired

    def release(self) -> None:
        assert self.owner_thread == threading.get_ident()
        self.owner_thread = None
        self._lock.release()

    def held_by_current_thread(self) -> bool:
        return self.owner_thread == threading.get_ident()

    def __enter__(self) -> _TrackedSweepLock:
        assert self.acquire()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self.release()


def _arm_next_caller_opcode_exception(
    caller: object,
    fault: BaseException,
) -> None:
    previous_global_trace = sys.gettrace()
    previous_frame_trace = caller.f_trace
    previous_opcode_flag = caller.f_trace_opcodes

    def one_shot_trace(
        frame: object,
        event: str,
        argument: object,
    ) -> object:
        if frame is caller and event == "opcode":
            caller.f_trace = previous_frame_trace
            caller.f_trace_opcodes = previous_opcode_flag
            sys.settrace(previous_global_trace)
            raise fault
        return one_shot_trace

    caller.f_trace = one_shot_trace
    caller.f_trace_opcodes = True
    sys.settrace(one_shot_trace)


class _SweepCellDuck:
    def __init__(
        self,
        *,
        boundary: str | None = None,
        fault: BaseException | None = None,
        contended: bool = False,
        equality_key: object | None = None,
    ) -> None:
        self._underlying = _TrackedSweepLock()
        self._owner_thread: int | None = None
        self._purpose: object | None = None
        self._boundary = boundary
        self._fault = fault
        self._contended = contended
        self._equality_key = equality_key
        self.try_purposes: list[object] = []
        self.release_purposes: list[object] = []
        self.actual_releases = 0
        self.internal_rollbacks = 0
        self.trace: list[str] = []

    def __hash__(self) -> int:
        if self._equality_key is None:
            return object.__hash__(self)
        return hash(("sweep", self._equality_key))

    def __eq__(self, other: object) -> bool:
        if self._equality_key is None:
            return self is other
        return (
            type(other) is type(self)
            and other._equality_key == self._equality_key
        )

    def __getattr__(self, name: str) -> object:
        if name.startswith("__"):
            raise AttributeError(name)
        return self

    def try_acquire(self, purpose: object) -> bool:
        self.try_purposes.append(purpose)
        self.trace.append("try_acquire")

        if self._contended:
            self.trace.append("contended")
            return False

        assert self._underlying.acquire(blocking=False)
        self._owner_thread = threading.get_ident()
        self._purpose = purpose
        self.trace.append("owner_committed")

        if self._boundary == "wrapper-pre-return":
            assert self._fault is not None
            self._owner_thread = None
            self._purpose = None
            self._underlying.release()
            self.internal_rollbacks += 1
            self.trace.append("wrapper_rollback")
            raise self._fault

        if self._boundary == "caller-assignment":
            assert self._fault is not None
            current = inspect.currentframe()
            assert current is not None and current.f_back is not None
            _arm_next_caller_opcode_exception(
                current.f_back,
                self._fault,
            )

        self.trace.append("try_return")
        return True

    def release_if_owned_by_current_thread(
        self,
        purpose: object,
    ) -> None:
        self.release_purposes.append(purpose)
        self.trace.append("release_attempt")

        if (
            self._owner_thread != threading.get_ident()
            or self._purpose is not purpose
        ):
            return

        self._owner_thread = None
        self._purpose = None
        self._underlying.release()
        self.actual_releases += 1
        self.trace.append("released")

    def owned_by_current_thread(self) -> bool:
        return self._owner_thread == threading.get_ident()

    def is_clean(self) -> bool:
        return (
            self._owner_thread is None
            and self._purpose is None
            and self._underlying.owner_thread is None
        )


class _SweepEntryDuck:
    def __init__(
        self,
        *,
        phase: object,
        linked_identity: object,
        boundary: str | None = None,
        fault: BaseException | None = None,
        equality_key: object | None = None,
    ) -> None:
        self._phase = phase
        self._linked_identity = linked_identity
        self._boundary = boundary
        self._fault = fault
        self._equality_key = equality_key
        self._registry: _SweepRegistryDuck | None = None
        self._call_cell: _SweepCellDuck | None = None
        self.after_phase_read: Callable[[], None] | None = None
        self.phase_reads = 0
        self.trace: list[str] = []

    def __eq__(self, other: object) -> bool:
        if self._equality_key is None:
            return self is other
        return (
            type(other) is type(self)
            and other._equality_key == self._equality_key
        )

    def bind_protocol(
        self,
        registry: _SweepRegistryDuck,
        call_cell: _SweepCellDuck,
    ) -> None:
        self._registry = registry
        self._call_cell = call_cell

    def _assert_protocol_locks(self) -> None:
        assert self._registry is not None
        assert self._registry.guard.held_by_current_thread()
        assert self._call_cell is not None
        assert self._call_cell.owned_by_current_thread()

    @property
    def registry_phase(self) -> object:
        self._assert_protocol_locks()
        self.phase_reads += 1
        self.trace.append("phase_enter")

        if self._boundary == "phase-pre":
            assert self._fault is not None
            raise self._fault

        phase = self._phase
        self.trace.append("phase_observed")

        if self.after_phase_read is not None:
            self.after_phase_read()

        if self._boundary == "phase-post":
            assert self._fault is not None
            raise self._fault

        return phase

    def __getattr__(self, name: str) -> object:
        if name.startswith("__"):
            raise AttributeError(name)
        self._assert_protocol_locks()
        return self._linked_identity


class _SweepRegistryDuck(MutableMapping[object, object]):
    def __init__(
        self,
        entries: list[tuple[object, object]],
        *,
        call_cell: _SweepCellDuck,
        boundary: str | None = None,
        fault: BaseException | None = None,
    ) -> None:
        self.guard = _TrackedSweepLock()
        self._backing: dict[object, object] = {}
        for key, value in entries:
            self._backing[key] = value

        self.entries = self
        self.call_cell = call_cell
        self.boundary = boundary
        self.fault = fault
        self.lookup_attempts = 0
        self.delete_attempts = 0
        self.trace: list[str] = []

    def _assert_protocol_locks(self) -> None:
        assert self.guard.held_by_current_thread()
        assert self.call_cell.owned_by_current_thread()

    def __getitem__(self, key: object) -> object:
        self._assert_protocol_locks()
        self.lookup_attempts += 1
        self.trace.append("lookup_enter")

        if self.boundary == "lookup-pre":
            assert self.fault is not None
            raise self.fault

        value = self._backing[key]
        self.trace.append("lookup_observed")

        if self.boundary == "lookup-post":
            assert self.fault is not None
            raise self.fault

        return value

    def __setitem__(self, key: object, value: object) -> None:
        self._assert_protocol_locks()
        self._backing[key] = value

    def __delitem__(self, key: object) -> None:
        self._assert_protocol_locks()
        self.delete_attempts += 1
        self.trace.append("remove_enter")

        if self.boundary == "remove-pre":
            assert self.fault is not None
            raise self.fault

        del self._backing[key]
        self.trace.append("remove_done")

        if self.boundary == "remove-post":
            assert self.fault is not None
            raise self.fault

    def __iter__(self) -> Iterator[object]:
        self._assert_protocol_locks()
        return iter(tuple(self._backing))

    def __len__(self) -> int:
        self._assert_protocol_locks()
        return len(self._backing)

    def peek_identity(self, key: object) -> object | None:
        for stored_key, value in self._backing.items():
            if stored_key is key:
                return value
        return None

    def has_identity_key(self, key: object) -> bool:
        return any(stored_key is key for stored_key in self._backing)

    def replace_direct(
        self,
        key: object,
        value: object,
    ) -> None:
        for stored_key in tuple(self._backing):
            if stored_key is key:
                self._backing[stored_key] = value
                return
        self._backing[key] = value

    def install_direct(
        self,
        key: object,
        value: object,
    ) -> None:
        self._backing[key] = value


def _invoke_sweep(
    module: ModuleType,
    registry: _SweepRegistryDuck,
    token: object,
    entry: _SweepEntryDuck,
    cell: _SweepCellDuck,
) -> object:
    with registry.guard:
        return module._try_sweep_candidate(
            registry,
            token,
            entry,
            cell,
        )


def _assert_sweep_outcome(
    module: ModuleType,
    result: object,
    name: str,
) -> None:
    assert result is getattr(module._SweepResult, name)


def _assert_single_sweep_purpose(
    cell: _SweepCellDuck,
) -> None:
    assert len(cell.try_purposes) == 1
    assert len(cell.release_purposes) == 1
    assert cell.release_purposes[0] is cell.try_purposes[0]

    purpose = cell.try_purposes[0]
    normalized = getattr(
        purpose,
        "name",
        getattr(purpose, "value", purpose),
    )
    assert str(normalized).upper() == "SWEEP"


def _assert_sweep_locks_reusable_from_another_thread(
    registry: _SweepRegistryDuck,
    cell: _SweepCellDuck,
) -> None:
    assert registry.guard.owner_thread is None
    assert cell.is_clean()
    observed: dict[str, bool] = {}

    def probe() -> None:
        got_cell = cell._underlying.acquire(timeout=2.0)
        observed["cell"] = got_cell
        if not got_cell:
            return

        try:
            got_global = registry.guard.acquire(timeout=2.0)
            observed["global"] = got_global
            if got_global:
                registry.guard.release()
        finally:
            cell._underlying.release()

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    thread.join(5.0)

    assert not thread.is_alive()
    assert observed == {
        "cell": True,
        "global": True,
    }


def _call_in_other_thread(
    callback: Callable[[], object],
    *,
    description: str,
    timeout: float = 5.0,
) -> object:
    outcome: dict[str, object] = {}

    def runner() -> None:
        try:
            outcome["value"] = callback()
        except BaseException as error:
            outcome["error"] = error

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout)

    assert not thread.is_alive(), f"{description} blocked for {timeout} seconds"
    if "error" in outcome:
        error = outcome["error"]
        assert isinstance(error, BaseException)
        raise AssertionError(f"{description} raised {error!r}") from error
    assert "value" in outcome
    return outcome["value"]


def _capture_public_registry_fault(
    module: ModuleType,
    callback: Callable[[], object],
    original: BaseException,
    interrupt_type: type[BaseException],
    *,
    reason: str,
    stage: str,
    owner: object | None,
    completion: object | None | type(Ellipsis) = Ellipsis,
) -> BaseException:
    if issubclass(interrupt_type, Exception):
        with pytest.raises(module.OverlappedPipeError) as captured:
            callback()
        handoff: BaseException = captured.value
        assert handoff.__cause__ is original
    else:
        with pytest.raises(BaseExceptionGroup) as captured_group:
            callback()
        group = captured_group.value
        assert type(group) is BaseExceptionGroup
        assert len(group.exceptions) == 2
        primary, handoff = group.exceptions
        assert primary is original
        assert type(handoff) is module.OverlappedPipeError

    _assert_typed_error(
        module,
        handoff,
        reason=reason,
        stage=stage,
        owner=owner,
        winerror=None,
        completion=completion,
    )
    return handoff


def _assert_captured_registry_cell_lock_released(
    module: ModuleType,
    transition: object,
) -> None:
    probe_registry = _DuckRegistry(
        seed={transition.entry_token: transition.expected_entry},
        quarantine_count=0,
        probe=_RegistryMutationProbe(),
    )

    result = _call_in_other_thread(
        lambda: module._try_sweep_candidate(
            probe_registry,
            transition.entry_token,
            transition.expected_entry,
            transition.expected_cell,
        ),
        description="captured RESERVE_INSERT cell sweep probe",
    )

    _assert_sweep_outcome(module, result, "REMOVED")
    assert transition.entry_token not in probe_registry.entries


def _call_registry_commit_with_assignment_probe(
    module: ModuleType,
    real_commit: Callable[[object, object], object],
    registry: object,
    transition: object,
    *,
    fault_label: str | None,
    fault: BaseException,
    mutation_trace: list[str],
) -> object:
    marker = transition.consume_state
    entry = transition.expected_entry
    initial_count = registry.quarantine_count
    assert type(initial_count) is int

    targets: dict[int, tuple[object, dict[str, tuple[str, object]]]] = {
        id(marker): (
            marker,
            {
                "value": (
                    "consume",
                    module._RegistryConsumeState.CONSUMED,
                )
            },
        ),
        id(entry): (
            entry,
            {
                "registry_phase": (
                    "phase",
                    module._RegistryPhase.QUARANTINED,
                ),
                "quarantine_flag": ("flag", True),
            },
        ),
        id(registry): (
            registry,
            {
                "quarantine_count": (
                    "count",
                    initial_count + 1,
                )
            },
        ),
    }

    effective_setters: dict[type, object] = {}
    original_definitions: dict[type, object] = {}
    for target in (marker, entry, registry):
        target_type = type(target)
        if target_type in effective_setters:
            continue
        effective_setters[target_type] = target_type.__setattr__
        original_definitions[target_type] = target_type.__dict__.get(
            "__setattr__",
            _UNSET,
        )

    installed: list[type] = []
    try:
        for target_type, original_setattr in effective_setters.items():
            def traced_setattr(
                instance: object,
                name: str,
                value: object,
                *,
                _original: object = original_setattr,
            ) -> None:
                _original(instance, name, value)
                target_record = targets.get(id(instance))
                if target_record is None or target_record[0] is not instance:
                    return
                mutation = target_record[1].get(name)
                if mutation is None:
                    return

                label, expected_value = mutation
                if label == "count":
                    assert type(value) is int
                    assert value == expected_value
                else:
                    assert value is expected_value
                mutation_trace.append(label)
                if label == fault_label:
                    raise fault

            setattr(target_type, "__setattr__", traced_setattr)
            installed.append(target_type)

        return real_commit(registry, transition)
    finally:
        for target_type in reversed(installed):
            original_definition = original_definitions[target_type]
            if original_definition is _UNSET:
                delattr(target_type, "__setattr__")
            else:
                setattr(
                    target_type,
                    "__setattr__",
                    original_definition,
                )


def _assert_state(module: ModuleType, operation: object, name: str) -> None:
    state = getattr(module.OverlappedState, name)
    assert operation.state is state


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _assert_completion(
    module: ModuleType,
    completion: object,
    *,
    operation: str,
    native_outcome: str,
    bytes_transferred: int | None,
    winerror: int | None,
    deadline_exceeded: bool = False,
    cancel_outcome: str | None = "not_attempted",
    cancel_winerror: int | None = None,
) -> None:
    assert type(completion) is module.Completion
    assert completion.operation is module.OperationKind(operation)
    assert _enum_value(completion.operation) == operation
    assert completion.native_outcome is module.NativeOutcome(native_outcome)
    assert _enum_value(completion.native_outcome) == native_outcome
    assert completion.bytes_transferred == bytes_transferred
    assert completion.winerror == winerror
    assert completion.deadline_exceeded is deadline_exceeded
    if cancel_outcome is None:
        assert completion.cancel_outcome is None
    else:
        assert completion.cancel_outcome is module.CancelOutcome(cancel_outcome)
    assert _enum_value(completion.cancel_outcome) == cancel_outcome
    assert completion.cancel_winerror == cancel_winerror


def _assert_typed_error(
    module: ModuleType,
    error: BaseException,
    *,
    reason: str,
    stage: str,
    owner: object | None | type(Ellipsis) = Ellipsis,
    winerror: int | None | type(Ellipsis) = Ellipsis,
    completion: object | None | type(Ellipsis) = Ellipsis,
) -> None:
    assert type(error) is module.OverlappedPipeError
    assert error.reason is module.FailureReason(reason)
    assert _enum_value(error.reason) == reason
    assert error.stage is module.LifecycleStage(stage)
    assert _enum_value(error.stage) == stage
    expected_winerror = error.winerror if winerror is Ellipsis else winerror
    assert type(error.args) is tuple
    assert len(error.args) == 3
    assert type(error.args[0]) is str
    assert type(error.args[1]) is str
    assert error.args[2] is None or type(error.args[2]) is int
    assert error.args == (reason, stage, expected_winerror)
    assert str(error) == str((reason, stage, expected_winerror))
    if owner is not Ellipsis:
        assert error.resource_owner is owner
    if winerror is not Ellipsis:
        assert error.winerror == winerror
    if completion is not Ellipsis:
        assert error.completion is completion


def _assert_interrupt_group(
    module: ModuleType,
    group: BaseExceptionGroup,
    interrupt_type: type[BaseException],
) -> tuple[BaseException, object]:
    assert type(group) is BaseExceptionGroup
    assert len(group.exceptions) == 2
    primary, handoff = group.exceptions
    assert isinstance(primary, interrupt_type)
    assert type(handoff) is module.OverlappedPipeError
    assert handoff.resource_owner is not None
    return primary, handoff


_INTERRUPTION_TYPES = (RuntimeError, KeyboardInterrupt, SystemExit)


def _capture_outward_interruption(
    module: ModuleType,
    original: BaseException,
    action: Callable[[], object],
) -> tuple[BaseException, object]:
    if isinstance(original, Exception):
        with pytest.raises(module.OverlappedPipeError) as captured:
            action()
        handoff = captured.value
        assert handoff.__cause__ is original
        return original, handoff

    with pytest.raises(BaseExceptionGroup) as captured_group:
        action()
    group = captured_group.value
    assert type(group) is BaseExceptionGroup
    assert len(group.exceptions) == 2
    primary, handoff = group.exceptions
    assert primary is original
    assert type(handoff) is module.OverlappedPipeError
    return primary, handoff


def _patch_last_error_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    interrupt: BaseException,
) -> None:
    calls = 0

    def interrupted_get_last_error() -> int:
        nonlocal calls
        calls += 1
        raise interrupt

    monkeypatch.setattr(ctypes, "get_last_error", interrupted_get_last_error)


def _assert_no_native_calls_after(
    kernel: _FakeKernel32,
    before: dict[str, int],
) -> None:
    for name, count in before.items():
        assert len(getattr(kernel, name).calls) == count, name


def _native_call_counts(kernel: _FakeKernel32) -> dict[str, int]:
    return {
        name: len(getattr(kernel, name).calls)
        for name in (
            "ConnectNamedPipe",
            "WriteFile",
            "GetOverlappedResult",
            "CancelIoEx",
            "CreateEventW",
            "WaitForSingleObject",
            "CloseHandle",
        )
    }


def _forbid_registry_commit_during_input_validation(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
) -> list[tuple[tuple[object, ...], dict[str, object]]]:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def forbidden_commit(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("input validation reached the live registry")

    monkeypatch.setattr(module, "_commit_registry_transition", forbidden_commit)
    return calls


def _assert_native_trace(kernel: _FakeKernel32, *expected: str) -> None:
    assert kernel.native_trace == list(expected)


def _module_strongly_references(
    module: ModuleType,
    target: object,
    *,
    max_depth: int = 5,
) -> bool:
    seen: set[int] = set()

    def contains(value: object, depth: int) -> bool:
        if value is target:
            return True
        if depth <= 0 or isinstance(
            value, (str, bytes, bytearray, int, float, bool, type, ModuleType)
        ):
            return False
        identity = id(value)
        if identity in seen:
            return False
        seen.add(identity)
        if isinstance(value, dict):
            children = [*value.keys(), *value.values()]
        elif isinstance(value, (list, tuple, set, frozenset, deque)):
            children = list(value)
        else:
            try:
                children = gc.get_referents(value)
            except TypeError:
                return False
        return any(contains(child, depth - 1) for child in children)

    return any(contains(value, max_depth) for value in vars(module).values())


def _find_reachable_owner(
    source: object,
    operation_type: type,
    *,
    max_depth: int = 5,
) -> object:
    seen: set[int] = {id(source)}
    level = [source]
    found: list[object] = []
    for _ in range(max_depth):
        next_level: list[object] = []
        for value in level:
            for referent in gc.get_referents(value):
                identity = id(referent)
                if identity in seen:
                    continue
                seen.add(identity)
                if isinstance(referent, operation_type):
                    found.append(referent)
                elif not isinstance(
                    referent,
                    (str, bytes, bytearray, int, float, bool, type, ModuleType),
                ):
                    next_level.append(referent)
        level = next_level
    unique = {id(owner): owner for owner in found}
    assert len(unique) == 1
    return next(iter(unique.values()))


def _reachable_instances(
    source: object,
    subject_type: type,
    *,
    max_depth: int = 8,
) -> tuple[object, ...]:
    seen: set[int] = {id(source)}
    level = [source]
    found: dict[int, object] = {}
    for _ in range(max_depth):
        next_level: list[object] = []
        for value in level:
            for referent in gc.get_referents(value):
                identity = id(referent)
                if identity in seen:
                    continue
                seen.add(identity)
                if isinstance(referent, subject_type):
                    found[identity] = referent
                elif not isinstance(
                    referent,
                    (str, bytes, bytearray, int, float, bool, type, ModuleType),
                ):
                    next_level.append(referent)
        level = next_level
    return tuple(found.values())


def _assert_errors_and_tracebacks_not_cached(
    module: ModuleType,
    owner: object | None,
    *errors: BaseException,
) -> None:
    assert _reachable_instances(owner, BaseException, max_depth=10) == ()
    assert _reachable_instances(owner, TracebackType, max_depth=10) == ()
    assert _reachable_instances(module, BaseException, max_depth=10) == ()
    assert _reachable_instances(module, TracebackType, max_depth=10) == ()
    for error in errors:
        assert all(
            candidate is not error
            for candidate in _reachable_instances(owner, type(error), max_depth=10)
        )
        assert not _module_strongly_references(module, error, max_depth=10)
        traceback = error.__traceback__
        if traceback is None:
            continue
        assert all(
            candidate is not traceback
            for candidate in _reachable_instances(
                owner,
                type(traceback),
                max_depth=10,
            )
        )
        assert not _module_strongly_references(module, traceback, max_depth=10)


def _ctypes_backing_weakrefs(owner: object) -> tuple[weakref.ReferenceType, ...]:
    seen: set[int] = {id(owner)}
    level = [owner]
    backing: dict[int, object] = {}
    for _ in range(8):
        next_level: list[object] = []
        for value in level:
            for referent in gc.get_referents(value):
                identity = id(referent)
                if identity in seen:
                    continue
                seen.add(identity)
                if isinstance(referent, (ctypes.Structure, ctypes.Array)):
                    backing[identity] = referent
                elif not isinstance(
                    referent,
                    (str, bytes, bytearray, int, float, bool, type, ModuleType),
                ):
                    next_level.append(referent)
        level = next_level
    references = tuple(weakref.ref(value) for value in backing.values())
    assert references, "operation must expose ctypes backing through its ownership cell"
    return references


def _method_parameters(subject: object, method_name: str) -> list[inspect.Parameter]:
    signature = inspect.signature(getattr(subject, method_name))
    return list(signature.parameters.values())


def _start_pending_operation(
    *,
    module: ModuleType,
    kernel: _FakeKernel32,
    operation_kind: str,
    pipe_handle: int,
    event_handle: int,
    frame: bytes = FRAME,
    absolute_deadline: float | None = None,
) -> object:
    bound_deadline = (
        _future_deadline() if absolute_deadline is None else absolute_deadline
    )
    kernel.event_handles.append(event_handle)
    if operation_kind == "connect":
        kernel.connect_outcomes.append(_Outcome(False, ERROR_IO_PENDING))
        operation = module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=pipe_handle,
            absolute_deadline=bound_deadline,
        )
    else:
        kernel.write_outcomes.append(_Outcome(False, ERROR_IO_PENDING))
        operation = module.OwnedOverlappedPipeOperation.start_write(
            pipe_handle=pipe_handle,
            immutable_frame=frame,
            absolute_deadline=bound_deadline,
        )
    _assert_state(module, operation, "PENDING")
    return operation


def _start_submit_unknown_operation(
    *,
    module: ModuleType,
    kernel: _FakeKernel32,
    pipe_handle: int,
    event_handle: int,
    absolute_deadline: float,
) -> object:
    kernel.event_handles.append(event_handle)
    kernel.connect_outcomes.append(_Outcome(False, 0))

    with pytest.raises(module.OverlappedPipeError) as captured:
        module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=pipe_handle,
            absolute_deadline=absolute_deadline,
        )

    owner = captured.value.resource_owner
    assert owner is not None
    _assert_typed_error(
        module,
        captured.value,
        reason="native_failure",
        stage="submit",
        owner=owner,
        winerror=None,
        completion=None,
    )
    _assert_state(module, owner, "SUBMIT_UNKNOWN")
    return owner


def _start_result_unknown_operation(
    *,
    module: ModuleType,
    kernel: _FakeKernel32,
    pipe_handle: int,
    event_handle: int,
    absolute_deadline: float,
) -> object:
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=pipe_handle,
        event_handle=event_handle,
        absolute_deadline=absolute_deadline,
    )
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(False, 0))

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.wait_to_deadline()

    _assert_typed_error(
        module,
        captured.value,
        reason="native_failure",
        stage="result",
        owner=operation,
        winerror=None,
        completion=None,
    )
    _assert_state(module, operation, "RESULT_UNKNOWN")
    return operation


def _start_result_required_after_clock_interrupt(
    *,
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    kernel: _FakeKernel32,
    pipe_handle: int,
    event_handle: int,
    absolute_deadline: float,
    frame: bytes = FRAME,
) -> tuple[object, _ScriptedClock]:
    clock = _ScriptedClock(10.0)
    clock.arm_next(10.0, 10.0, KeyboardInterrupt())
    monkeypatch.setattr(module, "_monotonic", clock)
    kernel.event_handles.append(event_handle)
    kernel.write_outcomes.append(_Outcome(True))

    with pytest.raises(BaseExceptionGroup) as captured:
        module.OwnedOverlappedPipeOperation.start_write(
            pipe_handle=pipe_handle,
            immutable_frame=frame,
            absolute_deadline=absolute_deadline,
        )

    _, handoff = _assert_interrupt_group(
        module,
        captured.value,
        KeyboardInterrupt,
    )
    owner = handoff.resource_owner
    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="submit",
        owner=owner,
        winerror=None,
        completion=None,
    )
    _assert_state(module, owner, "RESULT_REQUIRED")
    assert len(clock.calls) == 3
    assert not clock.steps
    assert kernel.GetOverlappedResult.calls == []
    return owner, clock


def _start_cancel_matrix_operation(
    *,
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    kernel: _FakeKernel32,
    state: str,
) -> tuple[object, _ScriptedClock]:
    clock = _ScriptedClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    if state == "PENDING":
        owner = _start_pending_operation(
            module=module,
            kernel=kernel,
            operation_kind="connect",
            pipe_handle=0x3A01,
            event_handle=0x2A01,
            absolute_deadline=100.0,
        )
    elif state == "SUBMIT_UNKNOWN":
        owner = _start_submit_unknown_operation(
            module=module,
            kernel=kernel,
            pipe_handle=0x3A01,
            event_handle=0x2A01,
            absolute_deadline=100.0,
        )
    else:
        assert state == "RESULT_UNKNOWN"
        owner = _start_result_unknown_operation(
            module=module,
            kernel=kernel,
            pipe_handle=0x3A01,
            event_handle=0x2A01,
            absolute_deadline=100.0,
        )
    _assert_state(module, owner, state)
    return owner, clock


def test_overlapped_pipe_module_and_minimum_interface_exist() -> None:
    spec = importlib.util.find_spec(SUBJECT_NAME)
    assert spec is not None, (
        "Task 3B-2 production module is intentionally absent before GREEN: "
        f"{SUBJECT_NAME}"
    )
    module = importlib.import_module(SUBJECT_NAME)

    public_enum_abi = {
        module.OverlappedState: (
            ("CREATE_EVENT_IN_FLIGHT", "create_event_in_flight"),
            ("CREATE_EVENT_UNKNOWN", "create_event_unknown"),
            ("NEW", "new"),
            ("SUBMIT_IN_FLIGHT", "submit_in_flight"),
            ("SUBMIT_UNKNOWN", "submit_unknown"),
            ("PENDING", "pending"),
            ("RESULT_REQUIRED", "result_required"),
            ("RESULT_IN_FLIGHT", "result_in_flight"),
            ("RESULT_UNKNOWN", "result_unknown"),
            ("CANCEL_IN_FLIGHT", "cancel_in_flight"),
            ("CANCEL_UNKNOWN", "cancel_unknown"),
            ("COMPLETED", "completed"),
            ("DRAINED", "drained"),
            ("TRANSFERRED", "transferred"),
            ("CLOSE_IN_FLIGHT", "close_in_flight"),
            ("CLOSE_FAILED_QUARANTINED", "close_failed_quarantined"),
            ("CLOSE_UNKNOWN", "close_unknown"),
            ("CLOSED", "closed"),
        ),
        module.OperationKind: (
            ("CONNECT", "connect"),
            ("WRITE", "write"),
        ),
        module.NativeOutcome: (
            ("SUCCESS", "success"),
            ("CANCELLED", "cancelled"),
            ("FAILURE", "failure"),
        ),
        module.CancelOutcome: (
            ("NOT_ATTEMPTED", "not_attempted"),
            ("ACCEPTED", "accepted"),
            ("NOT_FOUND", "not_found"),
            ("FAILED", "failed"),
            ("UNKNOWN", "unknown"),
        ),
        module.LifecycleStage: (
            ("CREATE_EVENT", "create_event"),
            ("SUBMIT", "submit"),
            ("WAIT", "wait"),
            ("RESULT", "result"),
            ("CANCEL", "cancel"),
            ("DRAIN", "drain"),
            ("TRANSFER", "transfer"),
            ("CLOSE", "close"),
        ),
        module.FailureReason: (
            ("NATIVE_FAILURE", "native_failure"),
            ("BYTE_COUNT_MISMATCH", "byte_count_mismatch"),
            ("DEADLINE_EXCEEDED", "deadline_exceeded"),
            ("DRAIN_PENDING", "drain_pending"),
            ("LIFECYCLE_VIOLATION", "lifecycle_violation"),
            ("OWNERSHIP_TRANSFER_FAILED", "ownership_transfer_failed"),
            ("CLOSE_QUARANTINED", "close_quarantined"),
        ),
    }
    for enum_type, expected_members in public_enum_abi.items():
        assert issubclass(enum_type, str)
        assert issubclass(enum_type, Enum)
        assert list(enum_type.__members__) == [name for name, _ in expected_members]
        for name, expected_value in expected_members:
            member = enum_type.__members__[name]
            assert member.name == name
            assert member.value == expected_value
    assert module.MAX_WAIT_SLICE_MS == MAX_WAIT_SLICE_MS
    assert module.LIVE_OPERATION_REGISTRY_CAPACITY == 256
    assert module.CLOSE_QUARANTINE_CAPACITY == 32
    assert module._MAX_FRAME_BYTES == 0xFFFFFFFF
    assert module._FAILFAST_QUARANTINE_EXIT_CODE == FAILFAST_QUARANTINE_EXIT_CODE
    assert module._FAILFAST_RETURNED_EXIT_CODE == FAILFAST_RETURNED_EXIT_CODE
    assert module._monotonic is time.monotonic
    private_signatures = {
        "_commit_authority_transfer": (
            "cell",
            "source_generation",
            "destination_generation",
        ),
        "_commit_cancel_snapshot": ("cell", "immutable_snapshot"),
        "_commit_wait_transition": ("cell", "immutable_transition"),
        "_commit_registry_transition": ("registry", "immutable_transition"),
        "_try_sweep_candidate": (
            "registry",
            "entry_token",
            "expected_entry",
            "expected_cell",
        ),
        "_failfast_quarantine": (),
    }
    for name, expected_names in private_signatures.items():
        function = getattr(module, name)
        assert callable(function)
        parameters = list(inspect.signature(function).parameters.values())
        assert tuple(parameter.name for parameter in parameters) == expected_names
        assert all(
            parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            and parameter.default is inspect.Parameter.empty
            for parameter in parameters
        )
    assert get_type_hints(module._failfast_quarantine)["return"] is NoReturn
    assert not hasattr(module, "SecretPipeOwner")

    operation_type = module.OwnedOverlappedPipeOperation
    for name in (
        "start_connect",
        "start_write",
        "wait_to_deadline",
        "cancel_and_drain",
        "transfer",
        "recover_transferred_owner",
        "close",
    ):
        assert callable(getattr(operation_type, name))

    connect_parameters = _method_parameters(operation_type, "start_connect")
    assert [parameter.name for parameter in connect_parameters] == [
        "pipe_handle",
        "absolute_deadline",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        and parameter.default is inspect.Parameter.empty
        for parameter in connect_parameters
    )

    write_parameters = _method_parameters(operation_type, "start_write")
    assert [parameter.name for parameter in write_parameters] == [
        "pipe_handle",
        "immutable_frame",
        "absolute_deadline",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        and parameter.default is inspect.Parameter.empty
        for parameter in write_parameters
    )

    for name in (
        "wait_to_deadline",
        "cancel_and_drain",
        "transfer",
        "recover_transferred_owner",
        "close",
    ):
        parameters = _method_parameters(operation_type, name)
        assert [parameter.name for parameter in parameters] == ["self"]

    completion = module.Completion(
        operation=module.OperationKind.CONNECT,
        native_outcome=module.NativeOutcome.SUCCESS,
        bytes_transferred=None,
        winerror=None,
        deadline_exceeded=False,
        cancel_outcome=module.CancelOutcome.NOT_ATTEMPTED,
        cancel_winerror=None,
    )
    assert is_dataclass(completion)
    assert module.Completion.__slots__ == (
        "operation",
        "native_outcome",
        "bytes_transferred",
        "winerror",
        "deadline_exceeded",
        "cancel_outcome",
        "cancel_winerror",
    )
    assert not hasattr(completion, "__dict__")
    with pytest.raises(FrozenInstanceError):
        completion.winerror = 5
    assert issubclass(module.OverlappedPipeError, Exception)
    assert module.OverlappedPipeError.__slots__ == (
        "_reason",
        "_stage",
        "_winerror",
        "_completion",
        "_resource_owner",
    )
    error = module.OverlappedPipeError(
        reason=module.FailureReason.NATIVE_FAILURE,
        stage=module.LifecycleStage.WAIT,
        winerror=ERROR_INVALID_HANDLE,
        completion=completion,
        resource_owner=None,
    )
    _assert_typed_error(
        module,
        error,
        reason="native_failure",
        stage="wait",
        owner=None,
        winerror=ERROR_INVALID_HANDLE,
        completion=completion,
    )
    expected_properties = {
        "reason": module.FailureReason.NATIVE_FAILURE,
        "stage": module.LifecycleStage.WAIT,
        "winerror": ERROR_INVALID_HANDLE,
        "completion": completion,
        "resource_owner": None,
    }
    for name, expected in expected_properties.items():
        descriptor = inspect.getattr_static(module.OverlappedPipeError, name)
        assert type(descriptor) is property
        assert descriptor.fset is None
        assert descriptor.fdel is None
        if name == "winerror":
            assert getattr(error, name) == expected
        else:
            assert getattr(error, name) is expected
        with pytest.raises(AttributeError):
            setattr(error, name, expected)


def test_overlapped_abi_bindings_and_zero_initialized_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(0x1001)
    kernel.connect_outcomes.append(_Outcome(True))
    module = _load_subject(monkeypatch, kernel)

    assert kernel.load_calls == [("kernel32", (), {"use_last_error": True})]
    for name in (
        "ConnectNamedPipe",
        "WriteFile",
        "GetOverlappedResult",
        "CancelIoEx",
        "CreateEventW",
        "WaitForSingleObject",
        "CloseHandle",
    ):
        function = getattr(kernel, name)
        assert function.argtypes is not _UNSET
        assert function.restype is not _UNSET

    assert kernel.ConnectNamedPipe.restype is wintypes.BOOL
    assert kernel.WriteFile.restype is wintypes.BOOL
    assert kernel.GetOverlappedResult.restype is wintypes.BOOL
    assert kernel.CancelIoEx.restype is wintypes.BOOL
    assert kernel.CreateEventW.restype is wintypes.HANDLE
    assert kernel.WaitForSingleObject.restype is wintypes.DWORD
    assert kernel.CloseHandle.restype is wintypes.BOOL
    assert len(kernel.WriteFile.argtypes) == 5
    assert len(kernel.GetOverlappedResult.argtypes) == 4
    assert len(kernel.CancelIoEx.argtypes) == 2
    assert len(kernel.CreateEventW.argtypes) == 4
    assert len(kernel.WaitForSingleObject.argtypes) == 2
    assert len(kernel.CloseHandle.argtypes) == 1

    connect_argtypes = kernel.ConnectNamedPipe.argtypes
    assert len(connect_argtypes) == 2
    assert ctypes.sizeof(connect_argtypes[0]) == ctypes.sizeof(ctypes.c_void_p) == 8
    overlapped_type = connect_argtypes[1]._type_
    assert issubclass(overlapped_type, ctypes.Structure)
    assert ctypes.sizeof(overlapped_type) == 32
    assert ctypes.alignment(overlapped_type) == 8
    field_names = [field[0] for field in overlapped_type._fields_]
    assert len(field_names) == 4
    assert field_names[:2] == ["Internal", "InternalHigh"]
    assert field_names[-1] == "hEvent"
    assert [getattr(overlapped_type, name).offset for name in field_names] == [
        0,
        8,
        16,
        24,
    ]
    assert overlapped_type._fields_[0][1] is ctypes.c_size_t
    assert overlapped_type._fields_[1][1] is ctypes.c_size_t
    offset_union_type = overlapped_type._fields_[2][1]
    assert issubclass(offset_union_type, ctypes.Union)
    union_fields = list(offset_union_type._fields_)
    assert len(union_fields) == 2
    pointer_arms = [field for field in union_fields if field[0] == "Pointer"]
    assert pointer_arms == [("Pointer", wintypes.LPVOID)]
    offset_arms = [field for field in union_fields if field[0] != "Pointer"]
    assert len(offset_arms) == 1
    offset_arm_name, offset_structure_type = offset_arms[0]
    assert issubclass(offset_structure_type, ctypes.Structure)
    assert list(offset_structure_type._fields_) == [
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
    ]
    assert ctypes.sizeof(offset_structure_type) == 8
    assert ctypes.alignment(offset_structure_type) == 4
    assert offset_structure_type.Offset.offset == 0
    assert offset_structure_type.OffsetHigh.offset == 4
    assert ctypes.sizeof(offset_union_type) == 8
    assert ctypes.alignment(offset_union_type) == 8
    assert getattr(offset_union_type, offset_arm_name).offset == 0
    assert offset_union_type.Pointer.offset == 0
    assert overlapped_type._fields_[3][1] is wintypes.HANDLE
    assert ctypes.sizeof(wintypes.BOOL) == ctypes.sizeof(wintypes.DWORD) == 4
    assert kernel.CreateEventW.argtypes[1:3] == [wintypes.BOOL, wintypes.BOOL]
    assert kernel.CreateEventW.argtypes[3] is wintypes.LPCWSTR
    assert {
        name
        for name, value in vars(kernel).items()
        if isinstance(value, _FakeFunction)
    } == {
        "ConnectNamedPipe",
        "WriteFile",
        "GetOverlappedResult",
        "CancelIoEx",
        "CreateEventW",
        "WaitForSingleObject",
        "CloseHandle",
    }

    operation = module.OwnedOverlappedPipeOperation.start_connect(
        pipe_handle=0x2001,
        absolute_deadline=_future_deadline(),
    )
    _assert_state(module, operation, "DRAINED")
    _assert_completion(
        module,
        operation.completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
    )
    create_args = kernel.CreateEventW.calls[0]
    assert _is_null_pointer(create_args[0])
    assert bool(create_args[1]) is True
    assert bool(create_args[2]) is False
    assert _is_null_pointer(create_args[3])
    _, _, snapshot = kernel.overlapped_snapshots[0]
    assert snapshot[:24] == b"\x00" * 24
    assert int.from_bytes(snapshot[24:32], "little") == 0x1001
    operation.close()


@pytest.mark.parametrize(
    ("connect_outcome", "connect_mode"),
    [
        (_Outcome(True, error=ERROR_ACCESS_DENIED), "synchronous"),
        (_Outcome(False, error=ERROR_PIPE_CONNECTED), "already-connected"),
        (_Outcome(False, error=ERROR_IO_PENDING), "pending"),
        (_Outcome(False, error=ERROR_ACCESS_DENIED), "known-failure"),
        (_Outcome(False, error=0), "submit-unknown"),
    ],
    ids=[
        "true-ignores-stale-error",
        "error-pipe-connected",
        "error-io-pending",
        "known-nonzero-error",
        "false-zero",
    ],
)
def test_connect_native_outcome_matrix_keeps_bytes_nonsemantic(
    monkeypatch: pytest.MonkeyPatch,
    connect_outcome: _Outcome,
    connect_mode: str,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(0x1101)
    kernel.connect_outcomes.append(connect_outcome)
    if connect_mode == "pending":
        kernel.wait_results.append(_WaitOutcome(WAIT_OBJECT_0, ERROR_ACCESS_DENIED))
        # GetOverlappedResult does not define a meaningful connect byte count.
        kernel.result_outcomes.append(
            _Outcome(
                True,
                error=ERROR_ACCESS_DENIED,
                byte_count=0xA5A5A5A5,
            )
        )
    module = _load_subject(monkeypatch, kernel)

    if connect_mode in {"known-failure", "submit-unknown"}:
        with pytest.raises(module.OverlappedPipeError) as captured:
            module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=0x2101,
                absolute_deadline=_future_deadline(),
            )
        error = captured.value
        operation = error.resource_owner
        assert type(operation) is module.OwnedOverlappedPipeOperation
    else:
        operation = module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x2101,
            absolute_deadline=_future_deadline(),
        )
        assert type(operation) is module.OwnedOverlappedPipeOperation

    assert _integer(kernel.ConnectNamedPipe.calls[0][0]) == 0x2101
    assert len(kernel.overlapped_snapshots) == 1
    assert kernel.overlapped_snapshots[0][0] == "connect"
    assert kernel.overlapped_snapshots[0][1] != 0

    if connect_mode == "pending":
        _assert_state(module, operation, "PENDING")
        assert operation.completion is None
        completion = operation.wait_to_deadline()
        assert len(kernel.WaitForSingleObject.calls) == 1
        assert len(kernel.GetOverlappedResult.calls) == 1
    elif connect_mode in {"synchronous", "already-connected"}:
        assert kernel.WaitForSingleObject.calls == []
        assert kernel.GetOverlappedResult.calls == []
        completion = operation.completion
    elif connect_mode == "known-failure":
        completion = operation.completion
        assert error.completion is completion
        _assert_typed_error(
            module,
            error,
            reason="native_failure",
            stage="submit",
            owner=operation,
            winerror=ERROR_ACCESS_DENIED,
            completion=completion,
        )
        _assert_completion(
            module,
            completion,
            operation="connect",
            native_outcome="failure",
            bytes_transferred=None,
            winerror=ERROR_ACCESS_DENIED,
        )
        _assert_state(module, operation, "DRAINED")
    else:
        assert connect_mode == "submit-unknown"
        _assert_typed_error(
            module,
            error,
            reason="native_failure",
            stage="submit",
            owner=operation,
            winerror=None,
            completion=None,
        )
        assert operation.completion is None
        _assert_state(module, operation, "SUBMIT_UNKNOWN")
        assert kernel.WaitForSingleObject.calls == []
        assert kernel.GetOverlappedResult.calls == []
        assert kernel.CloseHandle.calls == []
        _assert_native_trace(kernel, "CreateEventW", "ConnectNamedPipe")
        return

    if connect_mode in {"synchronous", "already-connected", "pending"}:
        assert completion is operation.completion
        _assert_completion(
            module,
            completion,
            operation="connect",
            native_outcome="success",
            bytes_transferred=None,
            winerror=None,
        )
        _assert_state(module, operation, "DRAINED")

    expected_trace = ["CreateEventW", "ConnectNamedPipe"]
    if connect_mode == "pending":
        expected_trace.extend(["WaitForSingleObject", "GetOverlappedResult"])
    _assert_native_trace(kernel, *expected_trace)
    operation.close()
    operation.close()
    assert [_integer(call[0]) for call in kernel.CloseHandle.calls] == [0x1101]


@pytest.mark.parametrize(
    "write_mode",
    ["synchronous", "pending"],
    ids=["synchronous", "error-io-pending"],
)
def test_write_two_native_outcomes_and_exact_result(
    monkeypatch: pytest.MonkeyPatch,
    write_mode: str,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(0x1201)
    if write_mode == "synchronous":
        kernel.write_outcomes.append(_Outcome(True))
    else:
        kernel.write_outcomes.append(_Outcome(False, ERROR_IO_PENDING))
        kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=len(FRAME)))
    module = _load_subject(monkeypatch, kernel)

    operation = module.OwnedOverlappedPipeOperation.start_write(
        pipe_handle=0x2201,
        immutable_frame=FRAME,
        absolute_deadline=_future_deadline(),
    )
    assert kernel.write_payloads == [FRAME]
    assert kernel.write_count_pointers == [0]
    if write_mode == "pending":
        _assert_state(module, operation, "PENDING")
        completion = operation.wait_to_deadline()
        assert len(kernel.WaitForSingleObject.calls) == 1
    else:
        assert kernel.WaitForSingleObject.calls == []
        completion = operation.completion
    assert len(kernel.GetOverlappedResult.calls) == 1
    result_call = kernel.GetOverlappedResult.calls[0]
    assert _integer(result_call[0]) == 0x2201
    assert _pointer_address(result_call[1]) == kernel.overlapped_snapshots[0][1]
    assert bool(result_call[3]) is False
    _assert_completion(
        module,
        completion,
        operation="write",
        native_outcome="success",
        bytes_transferred=len(FRAME),
        winerror=None,
    )
    _assert_state(module, operation, "DRAINED")
    operation.close()


@pytest.mark.parametrize(
    ("result_outcome", "expected_reason", "expected_state"),
    [
        (
            _Outcome(False, ERROR_ACCESS_DENIED),
            "native_failure",
            "DRAINED",
        ),
        (
            _Outcome(False, 0),
            "native_failure",
            "RESULT_UNKNOWN",
        ),
        (
            _Outcome(True, byte_count=len(FRAME) - 1),
            "byte_count_mismatch",
            "DRAINED",
        ),
    ],
    ids=["native-failure", "false-zero", "byte-count-mismatch"],
)
def test_sync_write_result_required_error_matrix_uses_exactly_one_gor(
    monkeypatch: pytest.MonkeyPatch,
    result_outcome: _Outcome,
    expected_reason: str,
    expected_state: str,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(0x1202)
    kernel.write_outcomes.append(_Outcome(True))
    kernel.result_outcomes.append(result_outcome)
    module = _load_subject(monkeypatch, kernel)

    with pytest.raises(module.OverlappedPipeError) as captured:
        module.OwnedOverlappedPipeOperation.start_write(
            pipe_handle=0x2202,
            immutable_frame=FRAME,
            absolute_deadline=_future_deadline(),
        )

    error = captured.value
    owner = error.resource_owner
    assert owner is not None
    expected_winerror = (
        ERROR_ACCESS_DENIED
        if result_outcome.error == ERROR_ACCESS_DENIED
        else None
    )
    _assert_typed_error(
        module,
        error,
        reason=expected_reason,
        stage="result",
        owner=owner,
        winerror=expected_winerror,
        completion=error.completion,
    )
    if expected_state == "DRAINED":
        expected_native_outcome = (
            "failure" if expected_reason == "native_failure" else "success"
        )
        expected_bytes = (
            None if expected_native_outcome == "failure" else len(FRAME) - 1
        )
        _assert_completion(
            module,
            error.completion,
            operation="write",
            native_outcome=expected_native_outcome,
            bytes_transferred=expected_bytes,
            winerror=expected_winerror,
        )
    else:
        assert error.completion is None
    _assert_state(module, owner, expected_state)
    assert kernel.write_payloads == [FRAME]
    assert kernel.write_count_pointers == [0]
    _assert_native_trace(
        kernel,
        "CreateEventW",
        "WriteFile",
        "GetOverlappedResult",
    )
    assert len(kernel.GetOverlappedResult.calls) == 1
    assert kernel.WaitForSingleObject.calls == []
    assert kernel.CancelIoEx.calls == []
    if expected_state == "DRAINED":
        owner.close()
    else:
        before = _native_call_counts(kernel)
        with pytest.raises(module.OverlappedPipeError):
            owner.close()
        _assert_no_native_calls_after(kernel, before)
        pointer = kernel.overlapped_snapshots[0][1]
        kernel.result_outcomes.append(_Outcome(True, byte_count=len(FRAME)))
        completion = owner.wait_to_deadline()
        _assert_completion(
            module,
            completion,
            operation="write",
            native_outcome="success",
            bytes_transferred=len(FRAME),
            winerror=None,
        )
        _assert_state(module, owner, "DRAINED")
        assert kernel.WaitForSingleObject.calls == []
        assert kernel.CancelIoEx.calls == []
        assert [
            (_integer(call[0]), _pointer_address(call[1]), bool(call[3]))
            for call in kernel.GetOverlappedResult.calls
        ] == [(0x2202, pointer, False), (0x2202, pointer, False)]
        owner.close()


@pytest.mark.parametrize(
    (
        "result_outcome",
        "expected_native_outcome",
        "expected_bytes",
        "expected_winerror",
    ),
    [
        (_Outcome(True, byte_count=len(FRAME)), "success", len(FRAME), None),
        (_Outcome(True, byte_count=len(FRAME) - 1), "success", len(FRAME) - 1, None),
        (_Outcome(False, ERROR_ACCESS_DENIED), "failure", None, ERROR_ACCESS_DENIED),
        (
            _Outcome(False, ERROR_OPERATION_ABORTED),
            "cancelled",
            None,
            ERROR_OPERATION_ABORTED,
        ),
    ],
    ids=["success-exact", "success-bad-byte-count", "failure", "cancelled"],
)
def test_sync_write_terminal_at_post_submit_deadline_keeps_submit_origin(
    monkeypatch: pytest.MonkeyPatch,
    result_outcome: _Outcome,
    expected_native_outcome: str,
    expected_bytes: int | None,
    expected_winerror: int | None,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    kernel.event_handles.append(0x1203)
    kernel.write_outcomes.append(_Outcome(True))
    kernel.result_outcomes.append(result_outcome)
    kernel.on_native_return["WriteFile"] = lambda: setattr(clock, "now", 20.0)

    with pytest.raises(module.OverlappedPipeError) as captured:
        module.OwnedOverlappedPipeOperation.start_write(
            pipe_handle=0x2203,
            immutable_frame=FRAME,
            absolute_deadline=20.0,
        )

    error = captured.value
    owner = error.resource_owner
    completion = error.completion
    assert owner is not None
    _assert_typed_error(
        module,
        error,
        reason="deadline_exceeded",
        stage="submit",
        owner=owner,
        winerror=expected_winerror,
        completion=completion,
    )
    _assert_completion(
        module,
        completion,
        operation="write",
        native_outcome=expected_native_outcome,
        bytes_transferred=expected_bytes,
        winerror=expected_winerror,
        deadline_exceeded=True,
    )
    _assert_state(module, owner, "DRAINED")
    _assert_native_trace(
        kernel,
        "CreateEventW",
        "WriteFile",
        "GetOverlappedResult",
    )
    assert len(kernel.GetOverlappedResult.calls) == 1
    assert kernel.WaitForSingleObject.calls == []
    assert kernel.CancelIoEx.calls == []
    owner.close()


@pytest.mark.parametrize(
    "reported_count",
    [0, len(FRAME) - 1, len(FRAME) + 1],
    ids=["zero", "partial", "over-count"],
)
def test_write_rejects_every_nonexact_terminal_count(
    monkeypatch: pytest.MonkeyPatch,
    reported_count: int,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="write",
        pipe_handle=0x2301,
        event_handle=0x1301,
    )
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=reported_count))

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.wait_to_deadline()

    error = captured.value
    _assert_typed_error(
        module,
        error,
        reason="byte_count_mismatch",
        stage="result",
        owner=operation,
    )
    _assert_completion(
        module,
        error.completion,
        operation="write",
        native_outcome="success",
        bytes_transferred=reported_count,
        winerror=None,
    )
    assert len(kernel.GetOverlappedResult.calls) == 1
    _assert_state(module, operation, "DRAINED")
    operation.close()
    assert [_integer(call[0]) for call in kernel.CloseHandle.calls] == [0x1301]


@pytest.mark.parametrize("operation_kind", ["connect", "write"])
def test_timeout_requests_exact_cancel_and_drains_aborted_operation(
    monkeypatch: pytest.MonkeyPatch,
    operation_kind: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(100.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind=operation_kind,
        pipe_handle=0x2401,
        event_handle=0x1401,
        absolute_deadline=100.5,
    )
    start_pointer = kernel.overlapped_snapshots[0][1]
    kernel.on_wait = lambda milliseconds: setattr(
        clock, "now", clock.now + milliseconds / 1000.0
    )
    kernel.wait_results.extend([WAIT_TIMEOUT, WAIT_TIMEOUT])
    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.wait_to_deadline()

    error = captured.value
    _assert_typed_error(
        module,
        error,
        reason="deadline_exceeded",
        stage="wait",
        owner=operation,
    )
    _assert_completion(
        module,
        error.completion,
        operation=operation_kind,
        native_outcome="cancelled",
        bytes_transferred=None,
        winerror=ERROR_OPERATION_ABORTED,
        deadline_exceeded=True,
        cancel_outcome="accepted",
        cancel_winerror=None,
    )
    assert len(kernel.CancelIoEx.calls) == 1
    cancel_call = kernel.CancelIoEx.calls[0]
    assert _integer(cancel_call[0]) == 0x2401
    assert _pointer_address(cancel_call[1]) == start_pointer
    assert [_integer(call[1]) for call in kernel.WaitForSingleObject.calls] == [
        MAX_WAIT_SLICE_MS,
        MAX_WAIT_SLICE_MS,
    ]
    result_call = kernel.GetOverlappedResult.calls[0]
    assert _integer(result_call[0]) == 0x2401
    assert _pointer_address(result_call[1]) == start_pointer
    _assert_state(module, operation, "DRAINED")
    operation.close()


@pytest.mark.parametrize("operation_kind", ["connect", "write"])
def test_cancel_error_not_found_still_observes_terminal_result(
    monkeypatch: pytest.MonkeyPatch,
    operation_kind: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind=operation_kind,
        pipe_handle=0x2501,
        event_handle=0x1501,
    )
    start_pointer = kernel.overlapped_snapshots[0][1]
    terminal_count = (
        len(FRAME) if operation_kind == "write" else 0xA5A5A5A5
    )
    kernel.cancel_outcomes.append(_Outcome(False, ERROR_NOT_FOUND))
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=terminal_count))

    completion = operation.cancel_and_drain()

    assert len(kernel.CancelIoEx.calls) == 1
    assert len(kernel.WaitForSingleObject.calls) == 1
    assert len(kernel.GetOverlappedResult.calls) == 1
    assert _pointer_address(kernel.CancelIoEx.calls[0][1]) == start_pointer
    assert _pointer_address(kernel.GetOverlappedResult.calls[0][1]) == start_pointer
    _assert_state(module, operation, "DRAINED")
    _assert_completion(
        module,
        completion,
        operation=operation_kind,
        native_outcome="success",
        bytes_transferred=len(FRAME) if operation_kind == "write" else None,
        winerror=None,
        cancel_outcome="not_found",
        cancel_winerror=ERROR_NOT_FOUND,
    )
    operation.close()


@pytest.mark.parametrize("terminal_mode", ["direct", "cancelled"])
def test_terminal_operation_event_cleanup_is_idempotent_and_exact_once(
    monkeypatch: pytest.MonkeyPatch,
    terminal_mode: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    if terminal_mode == "direct":
        kernel.event_handles.append(0x1601)
        kernel.connect_outcomes.append(_Outcome(True))
        operation = module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x2601,
            absolute_deadline=_future_deadline(),
        )
    else:
        operation = _start_pending_operation(
            module=module,
            kernel=kernel,
            operation_kind="connect",
            pipe_handle=0x2601,
            event_handle=0x1601,
        )
        kernel.cancel_outcomes.append(_Outcome(True))
        kernel.wait_results.append(WAIT_OBJECT_0)
        kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))
        operation.cancel_and_drain()

    completion = operation.completion
    assert completion is not None
    operation.close()
    after_close = _native_call_counts(kernel)
    assert operation.wait_to_deadline() is completion
    assert operation.cancel_and_drain() is completion
    operation.close()

    _assert_state(module, operation, "CLOSED")
    assert operation.completion is completion
    _assert_no_native_calls_after(kernel, after_close)
    assert [_integer(call[0]) for call in kernel.CloseHandle.calls] == [0x1601]


@pytest.mark.parametrize("operation_kind", ["connect", "write"])
def test_start_native_failure_returns_terminal_owner_without_auto_close(
    monkeypatch: pytest.MonkeyPatch,
    operation_kind: str,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(0x1801)
    if operation_kind == "connect":
        kernel.connect_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
    else:
        kernel.write_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
    module = _load_subject(monkeypatch, kernel)

    with pytest.raises(module.OverlappedPipeError) as captured:
        if operation_kind == "connect":
            module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=0x2801,
                absolute_deadline=_future_deadline(),
            )
        else:
            module.OwnedOverlappedPipeOperation.start_write(
                pipe_handle=0x2801,
                immutable_frame=FRAME,
                absolute_deadline=_future_deadline(),
            )

    error = captured.value
    owner = error.resource_owner
    assert owner is not None
    _assert_typed_error(
        module,
        error,
        reason="native_failure",
        stage="submit",
        owner=owner,
        winerror=ERROR_ACCESS_DENIED,
    )
    _assert_state(module, owner, "DRAINED")
    _assert_completion(
        module,
        error.completion,
        operation=operation_kind,
        native_outcome="failure",
        bytes_transferred=None,
        winerror=ERROR_ACCESS_DENIED,
    )
    assert kernel.CloseHandle.calls == []
    owner.close()
    _assert_state(module, owner, "CLOSED")
    assert [_integer(call[0]) for call in kernel.CloseHandle.calls] == [0x1801]
    after_close = _native_call_counts(kernel)
    for action in (owner.wait_to_deadline, owner.cancel_and_drain):
        with pytest.raises(module.OverlappedPipeError) as replay:
            action()
        assert replay.value is not error
        _assert_typed_error(
            module,
            replay.value,
            reason="native_failure",
            stage="submit",
            owner=owner,
            winerror=ERROR_ACCESS_DENIED,
            completion=error.completion,
        )
    _assert_no_native_calls_after(kernel, after_close)


def test_two_pending_writes_keep_handle_event_overlapped_and_buffer_disjoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.extend([0x1901, 0x1902])
    kernel.write_outcomes.extend(
        [
            _Outcome(False, ERROR_IO_PENDING),
            _Outcome(False, ERROR_IO_PENDING),
        ]
    )
    module = _load_subject(monkeypatch, kernel)
    first_frame = FRAME
    second_frame = b"\x00\x00\x00\x05vwxyz"

    first = module.OwnedOverlappedPipeOperation.start_write(
        pipe_handle=0x2901,
        immutable_frame=first_frame,
        absolute_deadline=_future_deadline(),
    )
    second = module.OwnedOverlappedPipeOperation.start_write(
        pipe_handle=0x2902,
        immutable_frame=second_frame,
        absolute_deadline=_future_deadline(),
    )
    write_pointers = [item[1] for item in kernel.overlapped_snapshots]
    assert len(set(write_pointers)) == 2
    assert len(set(kernel.write_buffer_addresses)) == 2
    assert kernel.write_payloads == [first_frame, second_frame]
    assert kernel.write_count_pointers == [0, 0]
    assert kernel.created_event_handles == [0x1901, 0x1902]
    assert [
        int.from_bytes(snapshot[24:32], "little")
        for _, _, snapshot in kernel.overlapped_snapshots
    ] == [0x1901, 0x1902]
    assert all(
        snapshot[:24] == b"\x00" * 24
        for _, _, snapshot in kernel.overlapped_snapshots
    )
    assert set(write_pointers).isdisjoint(kernel.write_buffer_addresses)

    def owned_buffer_reference(
        owner: object,
        expected_address: int,
    ) -> weakref.ReferenceType:
        matches = [
            value
            for value in _reachable_instances(owner, ctypes.Array)
            if ctypes.addressof(value) == expected_address
        ]
        assert len(matches) == 1
        return weakref.ref(matches[0])

    def assert_live_buffer(
        reference: weakref.ReferenceType,
        expected_address: int,
        expected_payload: bytes,
    ) -> None:
        buffer = reference()
        assert buffer is not None
        assert ctypes.addressof(buffer) == expected_address
        assert ctypes.string_at(buffer, len(expected_payload)) == expected_payload

    first_buffer = owned_buffer_reference(first, kernel.write_buffer_addresses[0])
    second_buffer = owned_buffer_reference(second, kernel.write_buffer_addresses[1])
    gc.collect()
    assert_live_buffer(first_buffer, kernel.write_buffer_addresses[0], first_frame)
    assert_live_buffer(second_buffer, kernel.write_buffer_addresses[1], second_frame)

    kernel.wait_results.extend([WAIT_OBJECT_0, WAIT_OBJECT_0])
    kernel.result_outcomes.extend(
        [
            _Outcome(True, byte_count=len(second_frame)),
            _Outcome(True, byte_count=len(first_frame)),
        ]
    )
    second.wait_to_deadline()
    gc.collect()
    assert_live_buffer(first_buffer, kernel.write_buffer_addresses[0], first_frame)
    assert_live_buffer(second_buffer, kernel.write_buffer_addresses[1], second_frame)
    first.wait_to_deadline()
    gc.collect()
    assert_live_buffer(first_buffer, kernel.write_buffer_addresses[0], first_frame)
    assert_live_buffer(second_buffer, kernel.write_buffer_addresses[1], second_frame)

    assert [_integer(call[0]) for call in kernel.WaitForSingleObject.calls] == [
        0x1902,
        0x1901,
    ]
    assert [_integer(call[0]) for call in kernel.GetOverlappedResult.calls] == [
        0x2902,
        0x2901,
    ]
    assert [
        _pointer_address(call[1]) for call in kernel.GetOverlappedResult.calls
    ] == [write_pointers[1], write_pointers[0]]
    _assert_state(module, first, "DRAINED")
    _assert_state(module, second, "DRAINED")
    first.close()
    second.close()
    assert sorted(_integer(call[0]) for call in kernel.CloseHandle.calls) == [
        0x1901,
        0x1902,
    ]


def test_exact_native_signatures_and_source_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(0x1A01)
    kernel.connect_outcomes.append(_Outcome(True))
    module = _load_subject(monkeypatch, kernel)

    handle_type, overlapped_pointer = kernel.ConnectNamedPipe.argtypes
    assert handle_type is wintypes.HANDLE
    assert ctypes.sizeof(handle_type) == ctypes.sizeof(ctypes.c_void_p)
    assert issubclass(overlapped_pointer._type_, ctypes.Structure)
    assert kernel.ConnectNamedPipe.argtypes == [handle_type, overlapped_pointer]
    assert kernel.ConnectNamedPipe.restype is wintypes.BOOL
    assert kernel.WriteFile.argtypes == [
        handle_type,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
        overlapped_pointer,
    ]
    assert kernel.WriteFile.restype is wintypes.BOOL
    assert kernel.GetOverlappedResult.argtypes == [
        handle_type,
        overlapped_pointer,
        wintypes.LPDWORD,
        wintypes.BOOL,
    ]
    assert kernel.GetOverlappedResult.restype is wintypes.BOOL
    assert kernel.CancelIoEx.argtypes == [handle_type, overlapped_pointer]
    assert kernel.CancelIoEx.restype is wintypes.BOOL
    assert kernel.CreateEventW.argtypes == [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    assert kernel.CreateEventW.restype is wintypes.HANDLE
    assert kernel.WaitForSingleObject.argtypes == [handle_type, wintypes.DWORD]
    assert kernel.WaitForSingleObject.restype is wintypes.DWORD
    assert kernel.CloseHandle.argtypes == [handle_type]
    assert kernel.CloseHandle.restype is wintypes.BOOL

    source_path = Path(inspect.getsourcefile(module) or "")
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed_plain_imports = {"ctypes", "math", "os", "threading", "time"}
    allowed_from_imports = {
        "__future__": {"annotations"},
        "ctypes": {"wintypes"},
        "dataclasses": {"dataclass"},
        "enum": {"Enum"},
        "typing": {"NoReturn"},
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(
                alias.name in allowed_plain_imports and alias.asname is None
                for alias in node.names
            ), "production imports must stay inside the native-module allowlist"
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module in allowed_from_imports
            assert all(
                alias.name in allowed_from_imports[node.module]
                and alias.asname is None
                for alias in node.names
            ), "from-import escaped the exact production allowlist"

    win_dll_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ctypes"
        and node.func.attr == "WinDLL"
    ]
    assert len(win_dll_calls) == 1
    win_dll_call = win_dll_calls[0]
    assert len(win_dll_call.args) == 1
    assert isinstance(win_dll_call.args[0], ast.Constant)
    assert win_dll_call.args[0].value == "kernel32"
    assert len(win_dll_call.keywords) == 1
    assert win_dll_call.keywords[0].arg == "use_last_error"
    assert isinstance(win_dll_call.keywords[0].value, ast.Constant)
    assert win_dll_call.keywords[0].value.value is True

    ctypes_imports = [
        (node, alias)
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "ctypes"
    ]
    assert len(ctypes_imports) == 1
    ctypes_import_node, ctypes_import_alias = ctypes_imports[0]
    assert ctypes_import_node in tree.body
    assert ctypes_import_alias.asname is None
    assert not any(
        isinstance(node, ast.Import)
        and any(
            alias.name == "_ctypes"
            or alias.name.startswith("importlib")
            or (alias.name.startswith("ctypes.") and alias.name != "ctypes")
            for alias in node.names
        )
        for node in ast.walk(tree)
    )
    banned_loader_names = {
        "CDLL",
        "OleDLL",
        "PyDLL",
        "LibraryLoader",
        "LoadLibrary",
        "CFUNCTYPE",
        "WINFUNCTYPE",
        "cdll",
        "oledll",
        "pydll",
        "windll",
        "pythonapi",
        "_dlopen",
        "__dict__",
        "__getattribute__",
        "c_bool",
    }
    assert not any(
        isinstance(node, ast.ImportFrom)
        and (
            node.module == "_ctypes"
            or (node.module or "").startswith("importlib")
            or (
                node.module == "ctypes"
                and any(
                    alias.name == "*" or alias.name in banned_loader_names | {"WinDLL"}
                    for alias in node.names
                )
            )
        )
        for node in ast.walk(tree)
    )
    loader_attributes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in banned_loader_names | {"WinDLL"}
    ]
    assert loader_attributes == [win_dll_call.func]
    assert not any(
        isinstance(node, ast.Attribute)
        and node.attr
        in {
            "__base__",
            "__bases__",
            "__builtins__",
            "__class__",
            "__closure__",
            "__code__",
            "__func__",
            "__globals__",
            "__import__",
            "__loader__",
            "__mro__",
            "__reduce__",
            "__reduce_ex__",
            "__self__",
            "__spec__",
            "__subclasses__",
            "__wrapped__",
            "_current_frames",
            "_getframe",
            "_sys",
            "ag_frame",
            "attrgetter",
            "cr_frame",
            "create_module",
            "currentframe",
            "delattr",
            "dir",
            "exec_module",
            "f_back",
            "f_builtins",
            "f_globals",
            "f_locals",
            "find_spec",
            "getattr",
            "getattr_static",
            "gi_frame",
            "hasattr",
            "load_module",
            "methodcaller",
            "module_from_spec",
            "modules",
            "setattr",
            "spec_from_file_location",
            "spec_from_loader",
            "sys",
            "tb_frame",
            "vars",
        }
        for node in ast.walk(tree)
    ), "reflective import/module lookup is forbidden in production"
    assert not any(
        isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id
        in {
            "__builtins__",
            "__import__",
            "__loader__",
            "__package__",
            "__spec__",
            "compile",
            "delattr",
            "dir",
            "eval",
            "exec",
            "getattr",
            "globals",
            "hasattr",
            "locals",
            "setattr",
            "vars",
        }
        for node in ast.walk(tree)
    ), "reflective import builtins are forbidden in production"
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {
            "__import__",
            "compile",
            "eval",
            "exec",
            "globals",
            "locals",
        }
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "hasattr", "setattr", "vars", "dir"}
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "ctypes"
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "ctypes"
        for node in ast.walk(tree)
    )

    win_dll_assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and node.value is win_dll_call
    ]
    assert len(win_dll_assignments) == 1
    assignment = win_dll_assignments[0]
    targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
    assert len(targets) == 1
    assert isinstance(targets[0], ast.Name)
    kernel_name = targets[0].id
    parent_by_child = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    ctypes_loads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id == "ctypes"
    ]
    assert ctypes_loads
    assert all(
        isinstance(parent_by_child[node], ast.Attribute)
        and parent_by_child[node].value is node
        for node in ctypes_loads
    ), "ctypes module must not escape through an alias or dynamic access"
    max_frame_assignments: list[tuple[ast.AST, ast.AST, ast.AST]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_MAX_FRAME_BYTES":
                    max_frame_assignments.append((node, target, node.value))
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_MAX_FRAME_BYTES"
            and node.value is not None
        ):
            max_frame_assignments.append((node, node.target, node.value))
    assert len(max_frame_assignments) == 1
    _, max_frame_target, max_frame_value = max_frame_assignments[0]
    assert isinstance(max_frame_value, ast.Constant)
    assert type(max_frame_value.value) is int
    assert max_frame_value.value == 0xFFFFFFFF
    max_frame_name_mutations = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "_MAX_FRAME_BYTES"
        and isinstance(node.ctx, (ast.Store, ast.Del))
    ]
    assert max_frame_name_mutations == [max_frame_target]
    assert not any(
        isinstance(node, ast.Attribute)
        and node.attr == "_MAX_FRAME_BYTES"
        and isinstance(node.ctx, (ast.Store, ast.Del))
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"setattr", "delattr"}
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "_MAX_FRAME_BYTES"
        for node in ast.walk(tree)
    )
    environment_entry_names = {
        "environ",
        "environb",
        "getenv",
        "getenvb",
        "putenv",
        "unsetenv",
    }
    assert not any(
        isinstance(node, ast.Attribute) and node.attr in environment_entry_names
        for node in ast.walk(tree)
    ), "the fixed DWORD frame limit must not depend on process environment"
    assert not any(
        isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in environment_entry_names
        for node in ast.walk(tree)
    ), "environment access must not be aliased before frame validation"
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "os"
        and any(
            alias.name == "*" or alias.name in environment_entry_names
            for alias in node.names
        )
        for node in ast.walk(tree)
    )
    kernel_loads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id == kernel_name
    ]
    assert kernel_loads
    assert all(
        isinstance(parent_by_child[node], ast.Attribute)
        and parent_by_child[node].value is node
        for node in kernel_loads
    ), "kernel32 object must not escape through an alias or dynamic access"
    allowed_bindings = {
        "ConnectNamedPipe",
        "WriteFile",
        "GetOverlappedResult",
        "CancelIoEx",
        "CreateEventW",
        "WaitForSingleObject",
        "CloseHandle",
    }
    native_binding_attributes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in allowed_bindings
    ]
    assert {node.attr for node in native_binding_attributes} == allowed_bindings
    assert all(
        isinstance(node.value, ast.Name) and node.value.id == kernel_name
        for node in native_binding_attributes
    ), "every native binding must come from the unique kernel32 WinDLL"
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "setattr", "vars", "dir"}
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == kernel_name
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == kernel_name
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "errcheck"
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "errcheck"
        for node in ast.walk(tree)
    )
    for banned in (
        "pywintypes.OVERLAPPED",
        "win32file.CancelIo",
        "ctypes.PyDLL",
        ".errcheck",
        "weakref.finalize",
        "atexit.register",
        "def __del__",
    ):
        assert banned not in source
    assert "CancelIo(" not in source
    assert set(vars(kernel)).isdisjoint({"ResetEvent", "OpenEventW", "Sleep"})

    failfast_definitions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_failfast_quarantine"
    ]
    assert len(failfast_definitions) == 1
    failfast_definition = failfast_definitions[0]
    assert failfast_definition.decorator_list == []
    assert len(failfast_definition.body) == 1
    failfast_statement = failfast_definition.body[0]
    assert isinstance(failfast_statement, ast.Expr)
    failfast_call = failfast_statement.value
    assert isinstance(failfast_call, ast.Call)
    assert isinstance(failfast_call.func, ast.Attribute)
    assert isinstance(failfast_call.func.value, ast.Name)
    assert failfast_call.func.value.id == "os"
    assert failfast_call.func.attr == "_exit"
    assert len(failfast_call.args) == 1
    assert failfast_call.keywords == []
    failfast_exit_code = failfast_call.args[0]
    assert (
        isinstance(failfast_exit_code, ast.Name)
        and failfast_exit_code.id == "_FAILFAST_QUARANTINE_EXIT_CODE"
    ) or (
        isinstance(failfast_exit_code, ast.Constant)
        and type(failfast_exit_code.value) is int
        and failfast_exit_code.value == FAILFAST_QUARANTINE_EXIT_CODE
    )

    operation = module.OwnedOverlappedPipeOperation.start_connect(
        pipe_handle=0x2A01,
        absolute_deadline=_future_deadline(),
    )
    operation.close()


def test_commit_primitives_have_source_pure_auditable_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_subject(monkeypatch, _FakeKernel32())
    source_path = Path(inspect.getsourcefile(module) or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    primitive_names = {
        "_commit_authority_transfer",
        "_commit_cancel_snapshot",
        "_commit_wait_transition",
        "_commit_registry_transition",
    }

    for primitive_name in primitive_names:
        module_level_definitions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == primitive_name
        ]
        same_named_definitions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == primitive_name
        ]
        assert len(module_level_definitions) == 1
        assert same_named_definitions == module_level_definitions, (
            f"{primitive_name} must be the unique module-level FunctionDef"
        )
        definition = module_level_definitions[0]
        assert definition.decorator_list == []
        parent_by_child = {
            child: parent
            for parent in ast.walk(definition)
            for child in ast.iter_child_nodes(parent)
        }

        violations: list[str] = []
        for statement in definition.body:
            for node in ast.walk(statement):
                if isinstance(node, ast.Call):
                    violation = "call"
                elif isinstance(
                    node,
                    (
                        ast.ListComp,
                        ast.SetComp,
                        ast.DictComp,
                        ast.GeneratorExp,
                    ),
                ):
                    violation = "comprehension"
                elif isinstance(
                    node,
                    (
                        ast.Lambda,
                        ast.FunctionDef,
                        ast.AsyncFunctionDef,
                        ast.ClassDef,
                    ),
                ):
                    violation = "nested definition"
                elif isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await)):
                    violation = "yield/await"
                elif isinstance(
                    node,
                    (
                        ast.Assert,
                        ast.AsyncFor,
                        ast.AsyncWith,
                        ast.AugAssign,
                        ast.For,
                        ast.Global,
                        ast.Import,
                        ast.ImportFrom,
                        ast.Match,
                        ast.NamedExpr,
                        ast.Nonlocal,
                        ast.Raise,
                        ast.Try,
                        ast.TryStar,
                        ast.UnaryOp,
                        ast.While,
                        ast.With,
                    ),
                ):
                    violation = "implicit call, exception, or hidden mutation"
                elif isinstance(node, ast.JoinedStr):
                    violation = "formatted allocation"
                elif isinstance(node, ast.Starred):
                    violation = "starred unpack allocation"
                elif isinstance(node, ast.Delete):
                    allowed_registry_delete = (
                        primitive_name == "_commit_registry_transition"
                        and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Subscript)
                        and isinstance(node.targets[0].ctx, ast.Del)
                        and isinstance(node.targets[0].value, ast.Attribute)
                        and isinstance(node.targets[0].value.value, ast.Name)
                        and node.targets[0].value.value.id == "registry"
                        and node.targets[0].value.attr == "entries"
                        and isinstance(node.targets[0].slice, ast.Name)
                        and isinstance(node.targets[0].slice.ctx, ast.Load)
                        and node.targets[0].slice.id == "entry_token"
                    )
                    if allowed_registry_delete:
                        continue
                    violation = "non-protocol delete"
                elif isinstance(node, ast.BinOp):
                    allowed_count_increment = (
                        primitive_name == "_commit_registry_transition"
                        and isinstance(node.op, ast.Add)
                        and isinstance(node.left, ast.Attribute)
                        and isinstance(node.left.value, ast.Name)
                        and node.left.value.id == "registry"
                        and node.left.attr == "quarantine_count"
                        and isinstance(node.right, ast.Constant)
                        and type(node.right.value) is int
                        and node.right.value == 1
                    )
                    if allowed_count_increment:
                        continue
                    violation = "non-protocol binary operation"
                elif isinstance(node, (ast.Dict, ast.Set, ast.List, ast.Tuple)):
                    violation = "explicit container expression"
                else:
                    continue
                violations.append(
                    f"{source_path.name}:{getattr(node, 'lineno', 0)} "
                    f"{type(node).__name__} ({violation})"
                )

        count_stores = [
            node
            for node in ast.walk(definition)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id == "registry"
            and node.attr == "quarantine_count"
        ]
        if primitive_name == "_commit_registry_transition":
            assert len(count_stores) == 1
            count_store = count_stores[0]
            count_assignment = parent_by_child[count_store]
            assert isinstance(count_assignment, ast.Assign)
            assert count_assignment.targets == [count_store]
            count_increment = count_assignment.value
            assert isinstance(count_increment, ast.BinOp)
            assert isinstance(count_increment.op, ast.Add)
            assert isinstance(count_increment.left, ast.Attribute)
            assert isinstance(count_increment.left.value, ast.Name)
            assert count_increment.left.value.id == "registry"
            assert count_increment.left.attr == "quarantine_count"
            assert isinstance(count_increment.right, ast.Constant)
            assert type(count_increment.right.value) is int
            assert count_increment.right.value == 1
            matching_increments = [
                node
                for node in ast.walk(definition)
                if isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Add)
                and isinstance(node.left, ast.Attribute)
                and isinstance(node.left.value, ast.Name)
                and node.left.value.id == "registry"
                and node.left.attr == "quarantine_count"
                and isinstance(node.right, ast.Constant)
                and type(node.right.value) is int
                and node.right.value == 1
            ]
            assert matching_increments == [count_increment]
        else:
            assert count_stores == []

        # Only the frozen registry count increment is allowed to allocate an
        # unavoidable replacement int; it is not a scratch business object.
        assert violations == [], (
            f"{primitive_name} must only commit its prebuilt transition: {violations}"
        )


@pytest.mark.parametrize("operation_kind", ["connect", "write"])
def test_successful_native_returns_never_read_stale_last_error(
    monkeypatch: pytest.MonkeyPatch,
    operation_kind: str,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(_EventOutcome(0x1A02, error=ERROR_ACCESS_DENIED))
    kernel.close_outcomes.append(_Outcome(True, error=ERROR_ACCESS_DENIED))
    if operation_kind == "connect":
        kernel.connect_outcomes.append(_Outcome(True, error=ERROR_ACCESS_DENIED))
    else:
        kernel.write_outcomes.append(_Outcome(True, error=ERROR_ACCESS_DENIED))
        kernel.result_outcomes.append(
            _Outcome(
                True,
                error=ERROR_ACCESS_DENIED,
                byte_count=len(FRAME),
            )
        )
    module = _load_subject(monkeypatch, kernel)
    reads: list[str] = []

    def forbid_last_error_read() -> int:
        reads.append(kernel.native_trace[-1])
        raise AssertionError("successful native return read stale last-error")

    monkeypatch.setattr(ctypes, "get_last_error", forbid_last_error_read)
    if operation_kind == "connect":
        operation = module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x2A02,
            absolute_deadline=_future_deadline(),
        )
    else:
        operation = module.OwnedOverlappedPipeOperation.start_write(
            pipe_handle=0x2A02,
            immutable_frame=FRAME,
            absolute_deadline=_future_deadline(),
        )
    _assert_state(module, operation, "DRAINED")
    operation.close()
    assert reads == []


def test_cancel_true_and_wait_object_zero_ignore_stale_last_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x2A03,
        event_handle=0x1A03,
    )
    kernel.cancel_outcomes.append(_Outcome(True, error=ERROR_ACCESS_DENIED))
    kernel.wait_results.append(_WaitOutcome(WAIT_OBJECT_0, ERROR_ACCESS_DENIED))
    kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))
    kernel.close_outcomes.append(_Outcome(True, error=ERROR_ACCESS_DENIED))
    kernel.native_trace.clear()
    reads: list[str] = []

    def guarded_last_error_read() -> int:
        native_name = kernel.native_trace[-1]
        reads.append(native_name)
        if native_name == "GetOverlappedResult":
            return ERROR_OPERATION_ABORTED
        raise AssertionError(f"{native_name} success read stale last-error")

    monkeypatch.setattr(ctypes, "get_last_error", guarded_last_error_read)
    completion = operation.cancel_and_drain()
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="cancelled",
        bytes_transferred=None,
        winerror=ERROR_OPERATION_ABORTED,
        cancel_outcome="accepted",
        cancel_winerror=None,
    )
    operation.close()
    assert reads == ["GetOverlappedResult"]


def test_pipe_connected_false_reads_last_error_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(_EventOutcome(0x1A04, error=ERROR_ACCESS_DENIED))
    kernel.connect_outcomes.append(_Outcome(False, ERROR_PIPE_CONNECTED))
    kernel.close_outcomes.append(_Outcome(True, error=ERROR_ACCESS_DENIED))
    module = _load_subject(monkeypatch, kernel)
    reads: list[str] = []

    def exact_pipe_connected_last_error() -> int:
        native_name = kernel.native_trace[-1]
        reads.append(native_name)
        if native_name == "ConnectNamedPipe":
            return ERROR_PIPE_CONNECTED
        raise AssertionError(f"{native_name} unexpectedly read last-error")

    monkeypatch.setattr(ctypes, "get_last_error", exact_pipe_connected_last_error)
    operation = module.OwnedOverlappedPipeOperation.start_connect(
        pipe_handle=0x2A04,
        absolute_deadline=_future_deadline(),
    )
    _assert_state(module, operation, "DRAINED")
    operation.close()
    assert reads == ["ConnectNamedPipe"]


def test_wait_timeout_and_later_success_never_read_stale_last_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x2A05,
        event_handle=0x1A05,
        absolute_deadline=100.0,
    )
    kernel.wait_results.extend(
        [
            _WaitOutcome(WAIT_TIMEOUT, ERROR_ACCESS_DENIED),
            _WaitOutcome(WAIT_OBJECT_0, ERROR_ACCESS_DENIED),
        ]
    )
    kernel.result_outcomes.append(
        _Outcome(True, error=ERROR_ACCESS_DENIED, byte_count=0xA205)
    )
    kernel.native_trace.clear()
    reads: list[str] = []

    def forbid_last_error_read() -> int:
        reads.append(kernel.native_trace[-1])
        raise AssertionError("nonfailure wait/result read stale last-error")

    monkeypatch.setattr(ctypes, "get_last_error", forbid_last_error_read)
    completion = operation.wait_to_deadline()
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
    )
    _assert_native_trace(
        kernel,
        "WaitForSingleObject",
        "WaitForSingleObject",
        "GetOverlappedResult",
    )
    operation.close()
    assert reads == []


@pytest.mark.parametrize(
    ("pipe_handle", "expected_exception"),
    [
        (True, TypeError),
        (0, ValueError),
        (-1, ValueError),
        (-2, ValueError),
        (UINTPTR_MAX, ValueError),
        (UINTPTR_MAX + 1, ValueError),
        (1.0, TypeError),
        ("123", TypeError),
    ],
    ids=[
        "bool",
        "null",
        "negative-invalid-value",
        "negative",
        "positive-invalid-value",
        "overflow",
        "float",
        "string",
    ],
)
def test_invalid_pipe_handle_rejected_before_event_create(
    monkeypatch: pytest.MonkeyPatch,
    pipe_handle: object,
    expected_exception: type[Exception],
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    registry_commits = _forbid_registry_commit_during_input_validation(
        monkeypatch,
        module,
    )

    with pytest.raises(expected_exception) as captured:
        module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=pipe_handle,
            absolute_deadline=_future_deadline(),
        )

    assert type(captured.value) is expected_exception
    assert _native_call_counts(kernel) == {
        name: 0 for name in _native_call_counts(kernel)
    }
    assert registry_commits == []


@pytest.mark.parametrize(
    ("frame", "expected_exception"),
    [
        (b"", ValueError),
        (bytearray(b"x"), TypeError),
        (memoryview(b"x"), TypeError),
        ("x", TypeError),
        (type("B", (bytes,), {})(b"x"), TypeError),
    ],
    ids=["empty", "bytearray", "memoryview", "string", "bytes-subclass"],
)
def test_invalid_frame_rejected_before_event_create(
    monkeypatch: pytest.MonkeyPatch,
    frame: object,
    expected_exception: type[Exception],
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    registry_commits = _forbid_registry_commit_during_input_validation(
        monkeypatch,
        module,
    )

    with pytest.raises(expected_exception) as captured:
        module.OwnedOverlappedPipeOperation.start_write(
            pipe_handle=0x2B01,
            immutable_frame=frame,
            absolute_deadline=_future_deadline(),
        )

    assert type(captured.value) is expected_exception
    assert _native_call_counts(kernel) == {
        name: 0 for name in _native_call_counts(kernel)
    }
    assert registry_commits == []


def test_frame_dword_boundary_is_locally_probed_without_allocating_four_gib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    assert module._MAX_FRAME_BYTES == 0xFFFFFFFF

    with monkeypatch.context() as boundary_patch:
        boundary_patch.setattr(module, "_MAX_FRAME_BYTES", 8)
        real_registry_commit = module._commit_registry_transition
        registry_commits: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def recording_registry_commit(*args: object, **kwargs: object) -> object:
            registry_commits.append((args, kwargs))
            return real_registry_commit(*args, **kwargs)

        boundary_patch.setattr(
            module,
            "_commit_registry_transition",
            recording_registry_commit,
        )
        with pytest.raises(ValueError) as captured:
            module.OwnedOverlappedPipeOperation.start_write(
                pipe_handle=0x2B02,
                immutable_frame=b"x" * 9,
                absolute_deadline=_future_deadline(),
            )
        assert type(captured.value) is ValueError
        assert _native_call_counts(kernel) == {
            name: 0 for name in _native_call_counts(kernel)
        }
        assert registry_commits == []
        boundary_patch.setattr(
            module,
            "_commit_registry_transition",
            real_registry_commit,
        )

        kernel.event_handles.append(0x1B02)
        kernel.write_outcomes.append(_Outcome(True))
        kernel.result_outcomes.append(_Outcome(True, byte_count=8))
        operation = module.OwnedOverlappedPipeOperation.start_write(
            pipe_handle=0x2B02,
            immutable_frame=b"y" * 8,
            absolute_deadline=_future_deadline(),
        )

        _assert_state(module, operation, "DRAINED")
        _assert_completion(
            module,
            operation.completion,
            operation="write",
            native_outcome="success",
            bytes_transferred=8,
            winerror=None,
        )
        assert kernel.write_payloads == [b"y" * 8]
        _assert_native_trace(
            kernel,
            "CreateEventW",
            "WriteFile",
            "GetOverlappedResult",
        )

    assert module._MAX_FRAME_BYTES == 0xFFFFFFFF
    operation.close()


@pytest.mark.parametrize(
    ("deadline", "expected_exception"),
    [
        (True, TypeError),
        (math.nan, ValueError),
        (math.inf, ValueError),
        (-math.inf, ValueError),
        ("later", TypeError),
        ("1.0", TypeError),
        (Decimal("1.0"), TypeError),
        (_FloatConvertible(), TypeError),
        (10**10_000, ValueError),
    ],
    ids=[
        "bool",
        "nan",
        "positive-inf",
        "negative-inf",
        "string",
        "convertible-string",
        "decimal",
        "custom-float",
        "huge-int",
    ],
)
def test_invalid_deadline_rejected_without_wait_or_cancel(
    monkeypatch: pytest.MonkeyPatch,
    deadline: object,
    expected_exception: type[Exception],
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    registry_commits = _forbid_registry_commit_during_input_validation(
        monkeypatch,
        module,
    )

    with pytest.raises(expected_exception) as captured:
        module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x2C01,
            absolute_deadline=deadline,
        )

    assert type(captured.value) is expected_exception
    assert _native_call_counts(kernel) == {
        name: 0 for name in _native_call_counts(kernel)
    }
    assert registry_commits == []


def test_finite_integer_deadline_is_normalized_and_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    kernel.event_handles.append(0x1C01)
    kernel.connect_outcomes.append(_Outcome(True))

    operation = module.OwnedOverlappedPipeOperation.start_connect(
        pipe_handle=0x2C01,
        absolute_deadline=11,
    )

    _assert_state(module, operation, "DRAINED")
    _assert_completion(
        module,
        operation.completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
    )
    _assert_native_trace(kernel, "CreateEventW", "ConnectNamedPipe")
    operation.close()


@pytest.mark.parametrize(
    ("event_error", "expected_winerror"),
    [(0, None), (ERROR_ACCESS_DENIED, ERROR_ACCESS_DENIED)],
    ids=["false-zero", "known-nonzero-error"],
)
def test_create_event_known_null_has_no_resource_owner(
    monkeypatch: pytest.MonkeyPatch,
    event_error: int,
    expected_winerror: int | None,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(_EventOutcome(0, event_error))
    module = _load_subject(monkeypatch, kernel)

    with pytest.raises(module.OverlappedPipeError) as captured:
        module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x2D01,
            absolute_deadline=_future_deadline(),
        )

    _assert_typed_error(
        module,
        captured.value,
        reason="native_failure",
        stage="create_event",
        owner=None,
        winerror=expected_winerror,
        completion=None,
    )
    assert kernel.CreateEventW.calls == [(0, True, False, 0)]
    assert kernel.ConnectNamedPipe.calls == []
    assert kernel.CloseHandle.calls == []
    _assert_native_trace(kernel, "CreateEventW")


@pytest.mark.parametrize(
    "interrupt_type",
    [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit],
)
def test_create_event_return_registration_unknown_preserves_one_public_owner(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_type: type[BaseException],
) -> None:
    delegated = _delegate_quarantine_node_to_subprocess(
        request,
        expected_exit_code=0,
    )
    if delegated is not None:
        return

    kernel = _FakeKernel32()
    original = interrupt_type(f"CreateEventW {interrupt_type.__name__}")
    kernel.event_handles.append(_EventOutcome(0x1D01, interrupt=original))
    module = _load_subject(monkeypatch, kernel)
    real_commit = module._commit_registry_transition
    registry_commits: list[tuple[object, object, object]] = []
    reserve_context: dict[str, object] = {}

    def backing_objects(active: object) -> dict[int, object]:
        references = _ctypes_backing_weakrefs(active)
        assert references
        values = [reference() for reference in references]
        assert all(value is not None for value in values)
        return {id(value): value for value in values}

    def assert_same_backing(active: object) -> None:
        current = backing_objects(active)
        original_backing = reserve_context["backing"]
        assert isinstance(original_backing, dict)
        assert current.keys() == original_backing.keys()
        assert all(
            current[identity] is expected
            for identity, expected in original_backing.items()
        )

    def capture_registry_commit(
        registry: object,
        transition: object,
    ) -> object:
        mode = _registry_status(transition.mode)
        assert mode in {"RESERVE_INSERT", "QUARANTINE"}

        if not reserve_context:
            assert transition.mode is module._RegistryTransitionMode.RESERVE_INSERT
            result = real_commit(registry, transition)
            _assert_registry_transition_result(module, result, "COMMITTED")
            entry = transition.expected_entry
            cell = transition.expected_cell
            token = transition.entry_token
            assert registry.entries[token] is entry
            reserve_context.update(
                registry=registry,
                token=token,
                entry=entry,
                cell=cell,
                backing=backing_objects(cell),
            )
        else:
            assert transition.mode is module._RegistryTransitionMode.QUARANTINE
            assert registry is reserve_context["registry"]
            assert transition.entry_token is reserve_context["token"]
            assert transition.expected_entry is reserve_context["entry"]
            assert transition.expected_cell is reserve_context["cell"]
            assert (
                registry.entries[transition.entry_token]
                is transition.expected_entry
            )
            assert_same_backing(transition.expected_cell)
            result = real_commit(registry, transition)
            _assert_registry_transition_result(module, result, "COMMITTED")
            assert (
                registry.entries[transition.entry_token]
                is transition.expected_entry
            )
            assert_same_backing(transition.expected_cell)

        registry_commits.append((registry, transition, result))
        return result

    monkeypatch.setattr(
        module,
        "_commit_registry_transition",
        capture_registry_commit,
    )

    if issubclass(interrupt_type, Exception):
        with pytest.raises(module.OverlappedPipeError) as captured_error:
            module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=0x2D02,
                absolute_deadline=_future_deadline(),
            )
        primary = original
        handoff = captured_error.value
        outward_error: BaseException = handoff
        assert handoff.__cause__ is original
    else:
        with pytest.raises(BaseExceptionGroup) as captured_group:
            module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=0x2D02,
                absolute_deadline=_future_deadline(),
            )
        primary, handoff = _assert_interrupt_group(
            module,
            captured_group.value,
            interrupt_type,
        )
        outward_error = captured_group.value
    assert primary is original
    owner = handoff.resource_owner
    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="create_event",
        owner=owner,
        winerror=None,
        completion=None,
    )
    _assert_state(module, owner, "CREATE_EVENT_UNKNOWN")
    assert owner.completion is None
    assert [
        _registry_status(transition.mode)
        for _, transition, _ in registry_commits
    ] == [
        "RESERVE_INSERT",
        "QUARANTINE",
    ]
    assert len(registry_commits) == 2
    reserve_registry, reserve_transition, reserve_result = registry_commits[0]
    quarantine_registry, quarantine_transition, quarantine_result = registry_commits[1]
    _assert_registry_transition_result(module, reserve_result, "COMMITTED")
    _assert_registry_transition_result(module, quarantine_result, "COMMITTED")
    assert quarantine_registry is reserve_registry
    assert quarantine_transition.entry_token is reserve_transition.entry_token
    assert (
        quarantine_transition.expected_entry
        is reserve_transition.expected_entry
    )
    assert quarantine_transition.expected_cell is reserve_transition.expected_cell
    assert (
        reserve_registry.entries[reserve_transition.entry_token]
        is reserve_transition.expected_entry
    )
    assert reserve_transition.expected_cell is reserve_context["cell"]
    assert_same_backing(owner)
    assert kernel.created_event_handles == [0x1D01]
    assert kernel.ConnectNamedPipe.calls == []
    assert kernel.CloseHandle.calls == []

    def assert_controls_reject_without_native(active: object) -> None:
        state_before = active.state
        before = _native_call_counts(kernel)
        for action, stage in (
            (active.wait_to_deadline, "wait"),
            (active.cancel_and_drain, "drain"),
            (active.close, "close"),
        ):
            with pytest.raises(module.OverlappedPipeError) as rejected:
                action()
            _assert_typed_error(
                module,
                rejected.value,
                reason="lifecycle_violation",
                stage=stage,
                owner=active,
                winerror=None,
                completion=None,
            )
        assert active.state is state_before
        assert active.completion is None
        _assert_no_native_calls_after(kernel, before)
        assert kernel.CancelIoEx.calls == []
        assert kernel.WaitForSingleObject.calls == []
        assert kernel.GetOverlappedResult.calls == []
        assert kernel.CloseHandle.calls == []

    assert_controls_reject_without_native(owner)
    before_transfer = _native_call_counts(kernel)
    destination = owner.transfer()
    assert destination is not owner
    assert owner.recover_transferred_owner() is destination
    _assert_state(module, owner, "TRANSFERRED")
    _assert_state(module, destination, "CREATE_EVENT_UNKNOWN")
    assert destination.completion is None
    assert_same_backing(destination)
    assert (
        reserve_registry.entries[reserve_transition.entry_token]
        is reserve_transition.expected_entry
    )
    assert reserve_transition.expected_cell is quarantine_transition.expected_cell
    assert len(registry_commits) == 2
    _assert_no_native_calls_after(kernel, before_transfer)
    assert_controls_reject_without_native(destination)
    before_second_transfer = _native_call_counts(kernel)
    destination2 = destination.transfer()
    assert destination2 is not destination
    assert owner.recover_transferred_owner() is destination
    assert destination.recover_transferred_owner() is destination2
    _assert_state(module, destination, "TRANSFERRED")
    _assert_state(module, destination2, "CREATE_EVENT_UNKNOWN")
    assert destination2.completion is None
    assert_same_backing(destination2)
    assert (
        reserve_registry.entries[reserve_transition.entry_token]
        is reserve_transition.expected_entry
    )
    assert reserve_transition.expected_cell is quarantine_transition.expected_cell
    assert len(registry_commits) == 2
    _assert_no_native_calls_after(kernel, before_second_transfer)
    assert_controls_reject_without_native(destination2)
    _assert_errors_and_tracebacks_not_cached(
        module,
        destination2,
        primary,
        outward_error,
        handoff,
    )


@pytest.mark.parametrize("operation_kind", ["connect", "write"])
@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_submit_unknown_preserves_exact_owner_without_resubmit_or_close(
    monkeypatch: pytest.MonkeyPatch,
    operation_kind: str,
    interrupt_type: type[BaseException],
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(0x1E01)
    outcome = _Outcome(False, ERROR_IO_PENDING, interrupt=interrupt_type())
    if operation_kind == "connect":
        kernel.connect_outcomes.append(outcome)
    else:
        kernel.write_outcomes.append(outcome)
    module = _load_subject(monkeypatch, kernel)

    with pytest.raises(BaseExceptionGroup) as captured:
        if operation_kind == "connect":
            module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=0x2E01,
                absolute_deadline=_future_deadline(),
            )
        else:
            module.OwnedOverlappedPipeOperation.start_write(
                pipe_handle=0x2E01,
                immutable_frame=FRAME,
                absolute_deadline=_future_deadline(),
            )

    _, handoff = _assert_interrupt_group(module, captured.value, interrupt_type)
    owner = handoff.resource_owner
    _assert_state(module, owner, "SUBMIT_UNKNOWN")
    before = _native_call_counts(kernel)
    with pytest.raises(module.OverlappedPipeError):
        owner.close()
    _assert_no_native_calls_after(kernel, before)


@pytest.mark.parametrize("operation_kind", ["connect", "write"])
def test_submit_last_error_capture_interrupt_becomes_submit_unknown(
    monkeypatch: pytest.MonkeyPatch,
    operation_kind: str,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(0x1F01)
    if operation_kind == "connect":
        kernel.connect_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
    else:
        kernel.write_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
    module = _load_subject(monkeypatch, kernel)
    _patch_last_error_interrupt(monkeypatch, KeyboardInterrupt())

    with pytest.raises(BaseExceptionGroup) as captured:
        if operation_kind == "connect":
            module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=0x2F01,
                absolute_deadline=_future_deadline(),
            )
        else:
            module.OwnedOverlappedPipeOperation.start_write(
                pipe_handle=0x2F01,
                immutable_frame=FRAME,
                absolute_deadline=_future_deadline(),
            )

    _, handoff = _assert_interrupt_group(module, captured.value, KeyboardInterrupt)
    _assert_state(module, handoff.resource_owner, "SUBMIT_UNKNOWN")
    assert kernel.CloseHandle.calls == []


@pytest.mark.parametrize(
    "result_mode",
    ["success", "incomplete-then-success", "nonterminal-error"],
)
def test_submit_unknown_uses_only_same_pointer_observation_and_never_resubmits(
    monkeypatch: pytest.MonkeyPatch,
    result_mode: str,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(0x1F02)
    kernel.write_outcomes.append(
        _Outcome(False, ERROR_IO_PENDING, interrupt=KeyboardInterrupt())
    )
    module = _load_subject(monkeypatch, kernel)
    if result_mode == "nonterminal-error":
        clock = _ManualClock(100.0)
        monkeypatch.setattr(module, "_monotonic", clock)
        bound_deadline = 100.5
    else:
        clock = None
        bound_deadline = _future_deadline()
    with pytest.raises(BaseExceptionGroup) as captured:
        module.OwnedOverlappedPipeOperation.start_write(
            pipe_handle=0x2F02,
            immutable_frame=FRAME,
            absolute_deadline=bound_deadline,
        )
    _, handoff = _assert_interrupt_group(module, captured.value, KeyboardInterrupt)
    operation = handoff.resource_owner
    pointer = kernel.overlapped_snapshots[0][1]
    if result_mode == "success":
        kernel.wait_results.append(WAIT_OBJECT_0)
        kernel.result_outcomes.append(_Outcome(True, byte_count=len(FRAME)))
    elif result_mode == "incomplete-then-success":
        kernel.wait_results.extend([WAIT_OBJECT_0, WAIT_OBJECT_0])
        kernel.result_outcomes.extend(
            [
                _Outcome(False, ERROR_IO_INCOMPLETE),
                _Outcome(True, byte_count=len(FRAME)),
            ]
        )
    else:
        assert clock is not None
        kernel.on_wait = lambda milliseconds: setattr(
            clock, "now", clock.now + milliseconds / 1000.0
        )
        kernel.wait_results.extend([WAIT_TIMEOUT, WAIT_TIMEOUT, WAIT_TIMEOUT])
        kernel.result_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
        kernel.cancel_outcomes.append(_Outcome(False, ERROR_NOT_FOUND))

    if result_mode == "nonterminal-error":
        with pytest.raises(module.OverlappedPipeError) as result_error:
            operation.wait_to_deadline()
        _assert_typed_error(
            module,
            result_error.value,
            reason="drain_pending",
            stage="drain",
            owner=operation,
            winerror=None,
            completion=None,
        )
        _assert_state(module, operation, "SUBMIT_UNKNOWN")
        assert [_integer(call[1]) for call in kernel.WaitForSingleObject.calls] == [
            MAX_WAIT_SLICE_MS,
            MAX_WAIT_SLICE_MS,
            0,
        ]
        assert len(kernel.CancelIoEx.calls) == 1
        assert _pointer_address(kernel.CancelIoEx.calls[0][1]) == pointer
        assert len(kernel.GetOverlappedResult.calls) == 1
    else:
        completion = operation.wait_to_deadline()
        assert completion.native_outcome is module.NativeOutcome.SUCCESS

    assert len(kernel.WriteFile.calls) == 1
    assert all(
        _pointer_address(call[1]) == pointer
        for call in kernel.GetOverlappedResult.calls
    )
    if result_mode == "nonterminal-error":
        assert operation.completion is None
    else:
        operation.close()


@pytest.mark.parametrize("boundary", ["create", "result", "cancel", "close"])
def test_false_return_last_error_capture_interrupt_is_never_misclassified(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    if boundary == "close":
        delegated = _delegate_quarantine_node_to_subprocess(
            request,
            expected_exit_code=0,
        )
        if delegated is not None:
            return

    kernel = _FakeKernel32()
    if boundary == "create":
        kernel.event_handles.append(_EventOutcome(0, ERROR_ACCESS_DENIED))
    else:
        kernel.event_handles.append(0x1F03)
        if boundary == "close":
            kernel.connect_outcomes.append(_Outcome(True))
        else:
            kernel.connect_outcomes.append(_Outcome(False, ERROR_IO_PENDING))
    module = _load_subject(monkeypatch, kernel)
    if boundary == "result":
        operation = module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x2F03,
            absolute_deadline=_future_deadline(),
        )
        kernel.wait_results.append(WAIT_OBJECT_0)
        kernel.result_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
    elif boundary == "cancel":
        operation = module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x2F03,
            absolute_deadline=_future_deadline(),
        )
        kernel.cancel_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
    elif boundary == "close":
        operation = module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x2F03,
            absolute_deadline=_future_deadline(),
        )
        kernel.close_outcomes.append(_Outcome(False, ERROR_INVALID_HANDLE))
    else:
        operation = None
    _patch_last_error_interrupt(monkeypatch, KeyboardInterrupt())

    with pytest.raises(BaseExceptionGroup) as captured:
        if boundary == "create":
            module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=0x2F03,
                absolute_deadline=_future_deadline(),
            )
        elif boundary == "result":
            operation.wait_to_deadline()
        elif boundary == "cancel":
            operation.cancel_and_drain()
        else:
            operation.close()

    if boundary == "create":
        group = captured.value
        assert isinstance(group.exceptions[0], KeyboardInterrupt)
        assert type(group.exceptions[1]) is module.OverlappedPipeError
        assert group.exceptions[1].resource_owner is None
        assert kernel.CloseHandle.calls == []
        return

    _, handoff = _assert_interrupt_group(module, captured.value, KeyboardInterrupt)
    owner = handoff.resource_owner
    assert owner is not None
    expected_state = {
        "result": "RESULT_UNKNOWN",
        "cancel": "CANCEL_UNKNOWN",
        "close": "CLOSE_UNKNOWN",
    }[boundary]
    _assert_state(module, owner, expected_state)
    assert owner.completion is None or boundary == "close"
    if boundary == "close":
        assert len(kernel.CloseHandle.calls) == 1


_NATIVE_INTERRUPTION_BOUNDARIES = [
    pytest.param("create-return", id="create-return"),
    pytest.param("create-last-error", id="create-last-error"),
    pytest.param("submit-connect-return", id="connect-return"),
    pytest.param("submit-connect-last-error", id="connect-last-error"),
    pytest.param("submit-write-return", id="write-return"),
    pytest.param("submit-write-last-error", id="write-last-error"),
    pytest.param("wait-enter", id="wait-enter"),
    pytest.param("wait-return", id="wait-return"),
    pytest.param("wait-last-error", id="wait-last-error"),
    pytest.param("gor-last-error", id="gor-last-error"),
    pytest.param("cancel-return", id="cancel-return"),
    pytest.param("cancel-last-error", id="cancel-last-error"),
    pytest.param("close-return", id="close-return"),
    pytest.param("close-last-error", id="close-last-error"),
]


@pytest.mark.parametrize("boundary", _NATIVE_INTERRUPTION_BOUNDARIES)
def test_native_interruption_boundaries_deliver_three_exception_classes_without_caching(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    if boundary in {"create-return", "close-return", "close-last-error"}:
        delegated = _delegate_quarantine_node_to_subprocess(
            request,
            expected_exit_code=0,
        )
        if delegated is not None:
            return

    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    real_get_last_error = ctypes.get_last_error

    for offset, interrupt_type in enumerate(_INTERRUPTION_TYPES):
        original = interrupt_type(f"{boundary}-{interrupt_type.__name__}")
        event_handle = 0x2400 + offset
        pipe_handle = 0x3400 + offset
        operation: object | None = None
        uses_last_error = boundary.endswith("last-error")
        kernel.native_trace.clear()

        if boundary == "create-return":
            kernel.event_handles.append(
                _EventOutcome(event_handle, interrupt=original)
            )

            def action() -> object:
                return module.OwnedOverlappedPipeOperation.start_connect(
                    pipe_handle=pipe_handle,
                    absolute_deadline=_future_deadline(),
                )

            expected_stage = "create_event"
            expected_state = "CREATE_EVENT_UNKNOWN"
            expected_trace = ("CreateEventW",)
        elif boundary == "create-last-error":
            kernel.event_handles.append(_EventOutcome(0, ERROR_ACCESS_DENIED))

            def action() -> object:
                return module.OwnedOverlappedPipeOperation.start_connect(
                    pipe_handle=pipe_handle,
                    absolute_deadline=_future_deadline(),
                )

            expected_stage = "create_event"
            expected_state = None
            expected_trace = ("CreateEventW",)
        elif boundary.startswith("submit-"):
            operation_kind = "write" if "-write-" in boundary else "connect"
            outcome = (
                _Outcome(False, ERROR_IO_PENDING, interrupt=original)
                if boundary.endswith("return")
                else _Outcome(False, ERROR_ACCESS_DENIED)
            )
            kernel.event_handles.append(event_handle)
            if operation_kind == "write":
                kernel.write_outcomes.append(outcome)

                def action() -> object:
                    return module.OwnedOverlappedPipeOperation.start_write(
                        pipe_handle=pipe_handle,
                        immutable_frame=FRAME,
                        absolute_deadline=_future_deadline(),
                    )

                expected_trace = ("CreateEventW", "WriteFile")
            else:
                kernel.connect_outcomes.append(outcome)

                def action() -> object:
                    return module.OwnedOverlappedPipeOperation.start_connect(
                        pipe_handle=pipe_handle,
                        absolute_deadline=_future_deadline(),
                    )

                expected_trace = ("CreateEventW", "ConnectNamedPipe")
            expected_stage = "submit"
            expected_state = "SUBMIT_UNKNOWN"
        elif boundary.startswith("wait-"):
            operation = _start_pending_operation(
                module=module,
                kernel=kernel,
                operation_kind="connect",
                pipe_handle=pipe_handle,
                event_handle=event_handle,
            )
            kernel.native_trace.clear()
            if boundary == "wait-enter":

                def interrupt_wait_enter() -> None:
                    raise original

                kernel.on_native_enter["WaitForSingleObject"] = (
                    interrupt_wait_enter
                )
                expected_trace = ()
            elif boundary == "wait-return":
                kernel.wait_results.append(
                    _WaitOutcome(WAIT_OBJECT_0, interrupt=original)
                )
                expected_trace = ("WaitForSingleObject",)
            else:
                kernel.wait_results.append(
                    _WaitOutcome(WAIT_FAILED, ERROR_INVALID_HANDLE)
                )
                expected_trace = ("WaitForSingleObject",)
            action = operation.wait_to_deadline
            expected_stage = "wait"
            expected_state = "PENDING"
        elif boundary == "gor-last-error":
            operation = _start_pending_operation(
                module=module,
                kernel=kernel,
                operation_kind="connect",
                pipe_handle=pipe_handle,
                event_handle=event_handle,
            )
            kernel.wait_results.append(WAIT_OBJECT_0)
            kernel.result_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
            kernel.native_trace.clear()
            action = operation.wait_to_deadline
            expected_stage = "result"
            expected_state = "RESULT_UNKNOWN"
            expected_trace = (
                "WaitForSingleObject",
                "GetOverlappedResult",
            )
        elif boundary.startswith("cancel-"):
            operation = _start_pending_operation(
                module=module,
                kernel=kernel,
                operation_kind="connect",
                pipe_handle=pipe_handle,
                event_handle=event_handle,
            )
            kernel.cancel_outcomes.append(
                _Outcome(True, interrupt=original)
                if boundary == "cancel-return"
                else _Outcome(False, ERROR_ACCESS_DENIED)
            )
            kernel.native_trace.clear()
            action = operation.cancel_and_drain
            expected_stage = "cancel"
            expected_state = "CANCEL_UNKNOWN"
            expected_trace = ("CancelIoEx",)
        else:
            assert boundary.startswith("close-")
            kernel.event_handles.append(event_handle)
            kernel.connect_outcomes.append(_Outcome(True))
            operation = module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=pipe_handle,
                absolute_deadline=_future_deadline(),
            )
            _assert_state(module, operation, "DRAINED")
            kernel.close_outcomes.append(
                _Outcome(True, interrupt=original)
                if boundary == "close-return"
                else _Outcome(False, ERROR_INVALID_HANDLE)
            )
            kernel.native_trace.clear()
            action = operation.close
            expected_stage = "close"
            expected_state = "CLOSE_UNKNOWN"
            expected_trace = ("CloseHandle",)

        if uses_last_error:
            _patch_last_error_interrupt(monkeypatch, original)
        try:
            primary, handoff = _capture_outward_interruption(
                module,
                original,
                action,
            )
        finally:
            if uses_last_error:
                monkeypatch.setattr(
                    ctypes,
                    "get_last_error",
                    real_get_last_error,
                )
            if boundary == "wait-enter":
                kernel.on_native_enter.pop("WaitForSingleObject", None)

        owner = handoff.resource_owner
        if boundary == "create-last-error":
            assert owner is None
        elif operation is not None:
            assert owner is operation
        else:
            assert owner is not None

        if boundary.startswith("close-"):
            _assert_typed_error(
                module,
                handoff,
                reason="close_quarantined",
                stage=expected_stage,
                owner=owner,
                winerror=None,
                completion=owner.completion,
            )
        else:
            _assert_typed_error(
                module,
                handoff,
                reason="native_failure",
                stage=expected_stage,
                owner=owner,
                winerror=None,
                completion=None,
            )
        if expected_state is not None:
            _assert_state(module, owner, expected_state)
        _assert_native_trace(kernel, *expected_trace)
        _assert_errors_and_tracebacks_not_cached(
            module,
            owner,
            primary,
            handoff,
        )

        if boundary.startswith("submit-"):
            submit_name = (
                "WriteFile" if "-write-" in boundary else "ConnectNamedPipe"
            )
            submit_count = len(getattr(kernel, submit_name).calls)
            kernel.wait_results.append(WAIT_OBJECT_0)
            kernel.result_outcomes.append(
                _Outcome(
                    True,
                    byte_count=(
                        len(FRAME) if submit_name == "WriteFile" else 0xA001
                    ),
                )
            )
            kernel.native_trace.clear()
            owner.wait_to_deadline()
            assert len(getattr(kernel, submit_name).calls) == submit_count
            _assert_native_trace(
                kernel,
                "WaitForSingleObject",
                "GetOverlappedResult",
            )
            owner.close()
        elif boundary.startswith("wait-"):
            kernel.wait_results.append(WAIT_OBJECT_0)
            kernel.result_outcomes.append(_Outcome(True, byte_count=0xA002))
            kernel.native_trace.clear()
            owner.wait_to_deadline()
            _assert_native_trace(
                kernel,
                "WaitForSingleObject",
                "GetOverlappedResult",
            )
            owner.close()
        elif boundary == "gor-last-error":
            kernel.result_outcomes.append(_Outcome(True, byte_count=0xA003))
            kernel.native_trace.clear()
            owner.wait_to_deadline()
            _assert_native_trace(kernel, "GetOverlappedResult")
            owner.close()
        elif boundary.startswith("cancel-"):
            cancel_count = len(kernel.CancelIoEx.calls)
            kernel.wait_results.append(WAIT_OBJECT_0)
            kernel.result_outcomes.append(_Outcome(True, byte_count=0xA004))
            kernel.native_trace.clear()
            owner.cancel_and_drain()
            assert len(kernel.CancelIoEx.calls) == cancel_count
            _assert_native_trace(
                kernel,
                "WaitForSingleObject",
                "GetOverlappedResult",
            )
            owner.close()
        elif boundary.startswith("close-"):
            before = _native_call_counts(kernel)
            with pytest.raises(module.OverlappedPipeError):
                owner.close()
            _assert_no_native_calls_after(kernel, before)


_CLOCK_INTERRUPTION_BOUNDARIES = [
    pytest.param("entry", id="entry-clock"),
    pytest.param("pre-submit", id="event-to-submit-clock"),
    pytest.param("post-submit", id="post-submit-clock"),
    pytest.param("pre-wait", id="pre-wait-clock"),
    pytest.param("post-cancel", id="post-cancel-clock"),
]


@pytest.mark.parametrize("boundary", _CLOCK_INTERRUPTION_BOUNDARIES)
def test_clock_interruption_boundaries_deliver_three_exception_classes_without_caching(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)

    for offset, interrupt_type in enumerate(_INTERRUPTION_TYPES):
        original = interrupt_type(f"{boundary}-{interrupt_type.__name__}")
        clock = _ScriptedClock(10.0)
        monkeypatch.setattr(module, "_monotonic", clock)
        event_handle = 0x2500 + offset
        pipe_handle = 0x3500 + offset
        operation: object | None = None
        original_cancel_commit: Callable[..., object] | None = None
        kernel.native_trace.clear()

        if boundary == "entry":
            clock.arm_next(original)

            def action() -> object:
                return module.OwnedOverlappedPipeOperation.start_connect(
                    pipe_handle=pipe_handle,
                    absolute_deadline=100.0,
                )

            expected_owner = None
            expected_state = None
            expected_stage = "submit"
            expected_trace = ()
        elif boundary == "pre-submit":
            clock.arm_next(10.0, original)
            kernel.event_handles.append(event_handle)

            def action() -> object:
                return module.OwnedOverlappedPipeOperation.start_connect(
                    pipe_handle=pipe_handle,
                    absolute_deadline=100.0,
                )

            expected_owner = Ellipsis
            expected_state = "NEW"
            expected_stage = "submit"
            expected_trace = ("CreateEventW",)
        elif boundary == "post-submit":
            clock.arm_next(10.0, 10.0, original)
            kernel.event_handles.append(event_handle)
            kernel.write_outcomes.append(_Outcome(True))

            def action() -> object:
                return module.OwnedOverlappedPipeOperation.start_write(
                    pipe_handle=pipe_handle,
                    immutable_frame=FRAME,
                    absolute_deadline=100.0,
                )

            expected_owner = Ellipsis
            expected_state = "RESULT_REQUIRED"
            expected_stage = "submit"
            expected_trace = ("CreateEventW", "WriteFile")
        elif boundary == "pre-wait":
            operation = _start_pending_operation(
                module=module,
                kernel=kernel,
                operation_kind="connect",
                pipe_handle=pipe_handle,
                event_handle=event_handle,
                absolute_deadline=100.0,
            )
            clock.calls.clear()
            clock.arm_next(original)
            kernel.native_trace.clear()
            action = operation.wait_to_deadline
            expected_owner = operation
            expected_state = "PENDING"
            expected_stage = "wait"
            expected_trace = ()
        else:
            assert boundary == "post-cancel"
            operation = _start_pending_operation(
                module=module,
                kernel=kernel,
                operation_kind="connect",
                pipe_handle=pipe_handle,
                event_handle=event_handle,
                absolute_deadline=100.0,
            )
            clock.calls.clear()
            original_cancel_commit = module._commit_cancel_snapshot

            def commit_then_arm_clock(
                cell: object,
                immutable_snapshot: object,
            ) -> object:
                assert original_cancel_commit is not None
                result = original_cancel_commit(cell, immutable_snapshot)
                clock.arm_next(original)
                return result

            monkeypatch.setattr(
                module,
                "_commit_cancel_snapshot",
                commit_then_arm_clock,
            )
            kernel.cancel_outcomes.append(_Outcome(True))
            kernel.native_trace.clear()
            action = operation.cancel_and_drain
            expected_owner = operation
            expected_state = "PENDING"
            expected_stage = "cancel"
            expected_trace = ("CancelIoEx",)

        try:
            primary, handoff = _capture_outward_interruption(
                module,
                original,
                action,
            )
        finally:
            if original_cancel_commit is not None:
                monkeypatch.setattr(
                    module,
                    "_commit_cancel_snapshot",
                    original_cancel_commit,
                )

        owner = handoff.resource_owner
        if expected_owner is None:
            assert owner is None
        elif expected_owner is Ellipsis:
            assert owner is not None
        else:
            assert owner is expected_owner
        _assert_typed_error(
            module,
            handoff,
            reason="native_failure",
            stage=expected_stage,
            owner=owner,
            winerror=None,
            completion=None,
        )
        if expected_state is not None:
            _assert_state(module, owner, expected_state)
        _assert_native_trace(kernel, *expected_trace)
        clock.calls.clear()
        assert not clock.steps
        _assert_errors_and_tracebacks_not_cached(
            module,
            owner,
            primary,
            handoff,
        )

        if boundary == "pre-submit":
            owner.close()
        elif boundary == "post-submit":
            kernel.result_outcomes.append(
                _Outcome(True, byte_count=len(FRAME))
            )
            kernel.native_trace.clear()
            owner.wait_to_deadline()
            _assert_native_trace(kernel, "GetOverlappedResult")
            owner.close()
        elif boundary == "pre-wait":
            kernel.wait_results.append(WAIT_OBJECT_0)
            kernel.result_outcomes.append(_Outcome(True, byte_count=0xA101))
            kernel.native_trace.clear()
            owner.wait_to_deadline()
            _assert_native_trace(
                kernel,
                "WaitForSingleObject",
                "GetOverlappedResult",
            )
            owner.close()
        elif boundary == "post-cancel":
            cancel_count = len(kernel.CancelIoEx.calls)
            kernel.wait_results.append(WAIT_OBJECT_0)
            kernel.result_outcomes.append(_Outcome(True, byte_count=0xA102))
            kernel.native_trace.clear()
            owner.cancel_and_drain()
            assert len(kernel.CancelIoEx.calls) == cancel_count
            _assert_native_trace(
                kernel,
                "WaitForSingleObject",
                "GetOverlappedResult",
            )
            owner.close()


@pytest.mark.parametrize(
    ("result_mode", "expected_outcome", "expected_error", "raises_error"),
    [
        ("success", "success", None, False),
        ("incomplete-then-success", "success", None, False),
        ("aborted", "cancelled", ERROR_OPERATION_ABORTED, False),
        ("failure", "failure", ERROR_ACCESS_DENIED, True),
    ],
)
@pytest.mark.parametrize("operation_kind", ["connect", "write"])
def test_get_overlapped_result_terminal_matrix(
    monkeypatch: pytest.MonkeyPatch,
    result_mode: str,
    expected_outcome: str,
    expected_error: int | None,
    raises_error: bool,
    operation_kind: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind=operation_kind,
        pipe_handle=0x3001,
        event_handle=0x2001,
    )
    terminal_count = len(FRAME) if operation_kind == "write" else 0xDEADBEEF
    if result_mode == "incomplete-then-success":
        kernel.wait_results.extend([WAIT_OBJECT_0, WAIT_OBJECT_0])
        kernel.result_outcomes.extend(
            [
                _Outcome(False, ERROR_IO_INCOMPLETE),
                _Outcome(True, byte_count=terminal_count),
            ]
        )
    else:
        kernel.wait_results.append(WAIT_OBJECT_0)
        if result_mode == "success":
            kernel.result_outcomes.append(_Outcome(True, byte_count=terminal_count))
        elif result_mode == "aborted":
            kernel.result_outcomes.append(
                _Outcome(False, ERROR_OPERATION_ABORTED, byte_count=0)
            )
        else:
            kernel.result_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))

    if raises_error:
        with pytest.raises(module.OverlappedPipeError) as captured:
            operation.wait_to_deadline()
        error = captured.value
        _assert_typed_error(
            module,
            error,
            reason="native_failure",
            stage="result",
            owner=operation,
            winerror=expected_error,
        )
        completion = error.completion
    else:
        completion = operation.wait_to_deadline()
        assert completion is operation.completion

    _assert_completion(
        module,
        completion,
        operation=operation_kind,
        native_outcome=expected_outcome,
        bytes_transferred=(
            terminal_count
            if operation_kind == "write" and expected_outcome == "success"
            else None
        ),
        winerror=expected_error,
    )
    _assert_state(module, operation, "DRAINED")
    operation.close()


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_result_unknown_is_live_transferable_and_never_closable(
    monkeypatch: pytest.MonkeyPatch,
    interrupt_type: type[BaseException],
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="write",
        pipe_handle=0x3002,
        event_handle=0x2002,
    )
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(
        _Outcome(True, byte_count=len(FRAME), interrupt=interrupt_type())
    )

    with pytest.raises(BaseExceptionGroup) as captured:
        operation.wait_to_deadline()

    _, handoff = _assert_interrupt_group(module, captured.value, interrupt_type)
    assert handoff.resource_owner is operation
    _assert_state(module, operation, "RESULT_UNKNOWN")
    assert operation.completion is None
    before = _native_call_counts(kernel)
    with pytest.raises(module.OverlappedPipeError):
        operation.close()
    _assert_no_native_calls_after(kernel, before)
    destination = operation.transfer()
    _assert_state(module, operation, "TRANSFERRED")
    assert operation.recover_transferred_owner() is destination


@pytest.mark.parametrize(
    ("wait_outcome", "reason", "winerror"),
    [
        (_WaitOutcome(WAIT_FAILED, ERROR_INVALID_HANDLE), "native_failure", ERROR_INVALID_HANDLE),
        (_WaitOutcome(0x80, ERROR_ACCESS_DENIED), "lifecycle_violation", None),
    ],
    ids=["wait-failed", "unexpected-value"],
)
def test_wait_failure_does_not_claim_terminal_or_auto_cancel(
    monkeypatch: pytest.MonkeyPatch,
    wait_outcome: _WaitOutcome,
    reason: str,
    winerror: int | None,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3003,
        event_handle=0x2003,
    )
    kernel.wait_results.append(wait_outcome)
    if wait_outcome.value != WAIT_FAILED:
        monkeypatch.setattr(
            ctypes,
            "get_last_error",
            lambda: pytest.fail("unexpected wait value must ignore stale last-error"),
        )

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.wait_to_deadline()

    _assert_typed_error(
        module,
        captured.value,
        reason=reason,
        stage="wait",
        owner=operation,
        winerror=winerror,
        completion=None,
    )
    assert kernel.CancelIoEx.calls == []
    assert kernel.GetOverlappedResult.calls == []
    assert operation.completion is None
    before = _native_call_counts(kernel)
    with pytest.raises(module.OverlappedPipeError):
        operation.close()
    _assert_no_native_calls_after(kernel, before)


@pytest.mark.parametrize(
    ("cancel_mode", "terminal_mode", "cancel_outcome", "cancel_winerror"),
    [
        ("accepted", "aborted", "accepted", None),
        ("not-found", "success", "not_found", ERROR_NOT_FOUND),
        ("failed", "aborted", "failed", ERROR_ACCESS_DENIED),
    ],
)
@pytest.mark.parametrize("operation_kind", ["connect", "write"])
def test_cancel_outcomes_all_continue_to_exact_terminal_observation(
    monkeypatch: pytest.MonkeyPatch,
    cancel_mode: str,
    terminal_mode: str,
    cancel_outcome: str,
    cancel_winerror: int | None,
    operation_kind: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind=operation_kind,
        pipe_handle=0x3004,
        event_handle=0x2004,
    )
    pointer = kernel.overlapped_snapshots[0][1]
    if cancel_mode == "accepted":
        kernel.cancel_outcomes.append(_Outcome(True))
    elif cancel_mode == "not-found":
        kernel.cancel_outcomes.append(_Outcome(False, ERROR_NOT_FOUND))
    else:
        kernel.cancel_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
    kernel.wait_results.append(WAIT_OBJECT_0)
    if terminal_mode == "success":
        kernel.result_outcomes.append(
            _Outcome(
                True,
                byte_count=len(FRAME) if operation_kind == "write" else 0xCAFEBABE,
            )
        )
    else:
        kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))

    completion = operation.cancel_and_drain()

    _assert_completion(
        module,
        completion,
        operation=operation_kind,
        native_outcome="success" if terminal_mode == "success" else "cancelled",
        bytes_transferred=(
            len(FRAME)
            if operation_kind == "write" and terminal_mode == "success"
            else None
        ),
        winerror=None if terminal_mode == "success" else ERROR_OPERATION_ABORTED,
        cancel_outcome=cancel_outcome,
        cancel_winerror=cancel_winerror,
    )
    assert len(kernel.CancelIoEx.calls) == 1
    assert _pointer_address(kernel.CancelIoEx.calls[0][1]) == pointer
    assert _pointer_address(kernel.GetOverlappedResult.calls[0][1]) == pointer
    operation.close()


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_cancel_unknown_is_attempted_once_and_still_drains(
    monkeypatch: pytest.MonkeyPatch,
    interrupt_type: type[BaseException],
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3005,
        event_handle=0x2005,
    )
    kernel.cancel_outcomes.append(_Outcome(True, interrupt=interrupt_type()))

    with pytest.raises(BaseExceptionGroup) as captured:
        operation.cancel_and_drain()

    _, handoff = _assert_interrupt_group(module, captured.value, interrupt_type)
    assert handoff.resource_owner is operation
    _assert_state(module, operation, "CANCEL_UNKNOWN")
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))

    completion = operation.cancel_and_drain()

    assert len(kernel.CancelIoEx.calls) == 1
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="cancelled",
        bytes_transferred=None,
        winerror=ERROR_OPERATION_ABORTED,
        cancel_outcome="unknown",
        cancel_winerror=None,
    )
    operation.close()


@pytest.mark.parametrize(
    "ambiguity_boundary",
    ["native-return", "last-error-capture", "snapshot-before", "snapshot-after"],
    ids=["native-return", "last-error-capture", "snapshot-before", "snapshot-after"],
)
def test_wait_carrier_survives_cancel_return_and_snapshot_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
    ambiguity_boundary: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_result_unknown_operation(
        module=module,
        kernel=kernel,
        pipe_handle=0x3A06,
        event_handle=0x2A06,
        absolute_deadline=20.0,
    )
    pointer = kernel.overlapped_snapshots[0][1]
    original_commit = module._commit_cancel_snapshot
    real_get_last_error = ctypes.get_last_error
    original = KeyboardInterrupt(f"{ambiguity_boundary} interrupted")

    if ambiguity_boundary == "native-return":
        kernel.cancel_outcomes.append(_Outcome(True, interrupt=original))
    elif ambiguity_boundary == "last-error-capture":
        kernel.cancel_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
        _patch_last_error_interrupt(monkeypatch, original)
    else:
        def interrupting_commit(*args: object, **kwargs: object) -> object:
            if ambiguity_boundary == "snapshot-before":
                raise original
            result = original_commit(*args, **kwargs)
            raise original

        monkeypatch.setattr(
            module,
            "_commit_cancel_snapshot",
            interrupting_commit,
        )
        kernel.cancel_outcomes.append(_Outcome(True))

    kernel.native_trace.clear()
    before = _native_call_counts(kernel)

    with pytest.raises(BaseExceptionGroup) as captured:
        operation.cancel_and_drain()

    primary, handoff = _assert_interrupt_group(
        module,
        captured.value,
        KeyboardInterrupt,
    )
    assert primary is original
    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="cancel",
        owner=operation,
        winerror=None,
        completion=None,
    )
    expected_state = (
        "PENDING" if ambiguity_boundary == "snapshot-after" else "CANCEL_UNKNOWN"
    )
    _assert_state(module, operation, expected_state)
    _assert_native_trace(kernel, "CancelIoEx")
    assert len(kernel.CancelIoEx.calls) == before["CancelIoEx"] + 1
    assert len(kernel.WaitForSingleObject.calls) == before["WaitForSingleObject"]
    assert len(kernel.GetOverlappedResult.calls) == before["GetOverlappedResult"]
    monkeypatch.setattr(module, "_commit_cancel_snapshot", original_commit)
    monkeypatch.setattr(ctypes, "get_last_error", real_get_last_error)
    _assert_errors_and_tracebacks_not_cached(
        module,
        operation,
        primary,
        handoff,
    )

    kernel.result_outcomes.append(_Outcome(True, byte_count=0xA006))
    kernel.on_native_return["GetOverlappedResult"] = lambda: setattr(
        clock,
        "now",
        20.0,
    )
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as resumed:
        operation.cancel_and_drain()

    completion = resumed.value.completion
    _assert_typed_error(
        module,
        resumed.value,
        reason="deadline_exceeded",
        stage="wait",
        owner=operation,
        winerror=None,
        completion=completion,
    )
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
        deadline_exceeded=True,
        cancel_outcome=(
            "accepted" if ambiguity_boundary == "snapshot-after" else "unknown"
        ),
        cancel_winerror=None,
    )
    _assert_state(module, operation, "DRAINED")
    _assert_native_trace(kernel, "GetOverlappedResult")
    assert len(kernel.CancelIoEx.calls) == 1
    assert len(kernel.WaitForSingleObject.calls) == before["WaitForSingleObject"]
    result_call = kernel.GetOverlappedResult.calls[-1]
    assert _integer(result_call[0]) == 0x3A06
    assert _pointer_address(result_call[1]) == pointer
    assert bool(result_call[3]) is False
    operation.close()


def test_result_unknown_wait_carrier_clears_on_996_before_ordinary_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_result_unknown_operation(
        module=module,
        kernel=kernel,
        pipe_handle=0x3A07,
        event_handle=0x2A07,
        absolute_deadline=100.0,
    )
    pointer = kernel.overlapped_snapshots[0][1]
    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.result_outcomes.extend(
        [
            _Outcome(False, ERROR_IO_INCOMPLETE),
            _Outcome(True, byte_count=0xA007),
        ]
    )
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.native_trace.clear()

    completion = operation.cancel_and_drain()

    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
        cancel_outcome="accepted",
        cancel_winerror=None,
    )
    _assert_state(module, operation, "DRAINED")
    _assert_native_trace(
        kernel,
        "CancelIoEx",
        "GetOverlappedResult",
        "WaitForSingleObject",
        "GetOverlappedResult",
    )
    assert len(kernel.CancelIoEx.calls) == 1
    assert kernel.WaitForSingleObject.calls[-1] == (0x2A07, MAX_WAIT_SLICE_MS)
    assert len(kernel.GetOverlappedResult.calls) == 3
    for result_call in kernel.GetOverlappedResult.calls[-2:]:
        assert _integer(result_call[0]) == 0x3A07
        assert _pointer_address(result_call[1]) == pointer
        assert bool(result_call[3]) is False
    operation.close()


def test_wait_timeout_before_absolute_deadline_only_starts_another_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(100.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3006,
        event_handle=0x2006,
        absolute_deadline=101.0,
    )
    kernel.on_wait = lambda milliseconds: setattr(
        clock, "now", clock.now + milliseconds / 1000.0
    )
    kernel.wait_results.extend([WAIT_TIMEOUT, WAIT_OBJECT_0])
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xFFFFFFFF))

    completion = operation.wait_to_deadline()

    assert [_integer(call[1]) for call in kernel.WaitForSingleObject.calls] == [
        MAX_WAIT_SLICE_MS,
        MAX_WAIT_SLICE_MS,
    ]
    assert kernel.CancelIoEx.calls == []
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
    )
    operation.close()


@pytest.mark.parametrize(
    ("start", "deadline", "expected_wait_ms"),
    [(10.0, 9.0, None), (10.0, 10.0001, 1)],
    ids=["already-expired", "sub-millisecond"],
)
def test_expired_and_submillisecond_deadlines_never_become_infinite_waits(
    monkeypatch: pytest.MonkeyPatch,
    start: float,
    deadline: float,
    expected_wait_ms: int | None,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(start)
    monkeypatch.setattr(module, "_monotonic", clock)
    if expected_wait_ms is None:
        with pytest.raises(module.OverlappedPipeError) as captured:
            module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=0x3007,
                absolute_deadline=deadline,
            )
        assert captured.value.reason is module.FailureReason.DEADLINE_EXCEEDED
        assert captured.value.resource_owner is None
        assert _native_call_counts(kernel) == {
            name: 0 for name in _native_call_counts(kernel)
        }
        return

    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3007,
        event_handle=0x2007,
        absolute_deadline=deadline,
    )
    kernel.wait_results.append(WAIT_TIMEOUT)
    kernel.on_wait = lambda _milliseconds: setattr(clock, "now", deadline)
    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.wait_to_deadline()

    waits = [_integer(call[1]) for call in kernel.WaitForSingleObject.calls]
    assert waits == [expected_wait_ms]
    assert all(value != 0xFFFFFFFF for value in waits)
    assert captured.value.reason is module.FailureReason.DEADLINE_EXCEEDED
    operation.close()


def test_sleep_jump_is_detected_after_one_bounded_wait_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(100.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3008,
        event_handle=0x2008,
        absolute_deadline=110.0,
    )
    kernel.on_wait = lambda _milliseconds: setattr(clock, "now", 10_000.0)
    kernel.wait_results.append(WAIT_TIMEOUT)
    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))

    with pytest.raises(module.OverlappedPipeError):
        operation.wait_to_deadline()

    assert [_integer(call[1]) for call in kernel.WaitForSingleObject.calls] == [
        MAX_WAIT_SLICE_MS
    ]
    assert len(kernel.CancelIoEx.calls) == 1
    operation.close()


def test_deadline_without_terminal_returns_live_drain_pending_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(48.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="write",
        pipe_handle=0x3009,
        event_handle=0x2009,
        absolute_deadline=49.0,
    )
    clock.now = 50.0
    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.result_outcomes.append(_Outcome(False, ERROR_IO_INCOMPLETE))

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.wait_to_deadline()

    error = captured.value
    _assert_typed_error(
        module,
        error,
        reason="drain_pending",
        stage="drain",
        owner=operation,
        completion=None,
    )
    assert operation.completion is None
    assert kernel.WaitForSingleObject.calls == []
    before = _native_call_counts(kernel)
    with pytest.raises(module.OverlappedPipeError):
        operation.close()
    _assert_no_native_calls_after(kernel, before)


def test_failed_cancel_at_deadline_polls_without_recancel_and_keeps_owner_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(48.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="write",
        pipe_handle=0x300A,
        event_handle=0x200A,
        absolute_deadline=49.0,
    )
    pointer = kernel.overlapped_snapshots[0][1]
    clock.now = 50.0
    kernel.cancel_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
    kernel.wait_results.extend([WAIT_TIMEOUT, WAIT_TIMEOUT])
    kernel.result_outcomes.extend(
        [
            _Outcome(False, ERROR_IO_INCOMPLETE),
            _Outcome(False, ERROR_IO_INCOMPLETE),
        ]
    )
    kernel.native_trace.clear()

    errors: list[BaseException] = []
    for action in (operation.wait_to_deadline, operation.cancel_and_drain):
        with pytest.raises(module.OverlappedPipeError) as captured:
            action()
        errors.append(captured.value)
        _assert_typed_error(
            module,
            captured.value,
            reason="drain_pending",
            stage="drain",
            owner=operation,
            winerror=ERROR_ACCESS_DENIED,
            completion=None,
        )
        _assert_state(module, operation, "PENDING")
        assert operation.completion is None

    assert errors[1] is not errors[0]
    assert len(kernel.CancelIoEx.calls) == 1
    assert kernel.WaitForSingleObject.calls == [(0x200A, 0), (0x200A, 0)]
    assert len(kernel.GetOverlappedResult.calls) == 2
    for result_call in kernel.GetOverlappedResult.calls:
        assert _integer(result_call[0]) == 0x300A
        assert _pointer_address(result_call[1]) == pointer
        assert bool(result_call[3]) is False
    _assert_native_trace(
        kernel,
        "CancelIoEx",
        "WaitForSingleObject",
        "GetOverlappedResult",
        "WaitForSingleObject",
        "GetOverlappedResult",
    )
    before_close = _native_call_counts(kernel)
    with pytest.raises(module.OverlappedPipeError) as close_rejected:
        operation.close()
    _assert_typed_error(
        module,
        close_rejected.value,
        reason="lifecycle_violation",
        stage="close",
        owner=operation,
        winerror=None,
        completion=None,
    )
    _assert_no_native_calls_after(kernel, before_close)


def test_deadline_success_race_preserves_both_facts_and_replays_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(18.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="write",
        pipe_handle=0x3010,
        event_handle=0x2010,
        absolute_deadline=19.0,
    )
    clock.now = 20.0
    kernel.cancel_outcomes.append(_Outcome(False, ERROR_NOT_FOUND))
    kernel.result_outcomes.append(_Outcome(True, byte_count=len(FRAME)))

    with pytest.raises(module.OverlappedPipeError) as first:
        operation.wait_to_deadline()

    completion = first.value.completion
    _assert_completion(
        module,
        completion,
        operation="write",
        native_outcome="success",
        bytes_transferred=len(FRAME),
        winerror=None,
        deadline_exceeded=True,
        cancel_outcome="not_found",
        cancel_winerror=ERROR_NOT_FOUND,
    )
    before = _native_call_counts(kernel)
    with pytest.raises(module.OverlappedPipeError) as second:
        operation.wait_to_deadline()
    _assert_no_native_calls_after(kernel, before)
    assert second.value.reason is module.FailureReason.DEADLINE_EXCEEDED
    assert second.value.completion is completion
    operation.close()


def test_transfer_moves_authority_to_distinct_owner_and_source_only_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    source = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3101,
        event_handle=0x2101,
    )

    destination = source.transfer()

    assert destination is not source
    _assert_state(module, source, "TRANSFERRED")
    _assert_state(module, destination, "PENDING")
    assert source.recover_transferred_owner() is destination
    assert source.recover_transferred_owner() is destination
    final_destination = destination.transfer()
    _assert_state(module, destination, "TRANSFERRED")
    assert source.recover_transferred_owner() is destination
    assert destination.recover_transferred_owner() is final_destination
    before = _native_call_counts(kernel)
    for action, stage in (
        (source.wait_to_deadline, "wait"),
        (source.cancel_and_drain, "drain"),
        (source.close, "close"),
        (source.transfer, "transfer"),
    ):
        with pytest.raises(module.OverlappedPipeError) as captured:
            action()
        _assert_typed_error(
            module,
            captured.value,
            reason="lifecycle_violation",
            stage=stage,
            owner=final_destination,
            winerror=None,
            completion=None,
        )
    _assert_no_native_calls_after(kernel, before)

    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))
    completion = final_destination.cancel_and_drain()
    assert completion.native_outcome is module.NativeOutcome.CANCELLED
    final_destination.close()


@pytest.mark.parametrize("owner_state", ["DRAINED", "NEW", "CLOSED"])
def test_recover_transferred_owner_rejects_non_tombstone_without_native_call(
    monkeypatch: pytest.MonkeyPatch,
    owner_state: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    if owner_state == "NEW":
        clock = _ManualClock(10.0)
        monkeypatch.setattr(module, "_monotonic", clock)
        kernel.event_handles.append(0x2102)
        kernel.on_native_return["CreateEventW"] = lambda: setattr(
            clock,
            "now",
            20.0,
        )
        with pytest.raises(module.OverlappedPipeError) as presubmit:
            module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=0x3102,
                absolute_deadline=20.0,
            )
        operation = presubmit.value.resource_owner
        assert operation is not None
        completion = None
    else:
        kernel.event_handles.append(0x2102)
        kernel.connect_outcomes.append(_Outcome(True))
        operation = module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x3102,
            absolute_deadline=_future_deadline(),
        )
        completion = operation.completion
        assert completion is not None
        if owner_state == "CLOSED":
            operation.close()

    _assert_state(module, operation, owner_state)
    assert operation.completion is completion
    before = _native_call_counts(kernel)

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.recover_transferred_owner()

    _assert_typed_error(
        module,
        captured.value,
        reason="lifecycle_violation",
        stage="transfer",
        owner=operation,
        winerror=None,
        completion=completion,
    )
    _assert_no_native_calls_after(kernel, before)
    if owner_state != "CLOSED":
        operation.close()


def _registry_status(value: object) -> str:
    return str(getattr(value, "value", value))


def _assert_registry_transition_result(
    module: ModuleType,
    result: object,
    expected_name: str,
) -> None:
    assert result is getattr(module._RegistryTransitionResult, expected_name)


def _assert_registry_commit_target(
    module: ModuleType,
    case: _RegistryTransitionCase,
    mode_name: str,
) -> None:
    assert _enum_value(case.transition.consume_state.value) == "CONSUMED"
    if mode_name == "RESERVE_INSERT":
        assert case.registry.entries[case.token] is case.entry
        assert _enum_value(case.entry.registry_phase) == "EMPTY"
        assert case.entry.quarantine_flag is False
        assert case.registry.quarantine_count == 0
    elif mode_name == "CLOSED_REMOVE":
        assert case.token not in case.registry.entries
        assert _enum_value(case.entry.registry_phase) == "CLOSED"
        assert case.entry.quarantine_flag is False
        assert case.registry.quarantine_count == 0
    else:
        assert mode_name == "QUARANTINE"
        assert case.registry.entries[case.token] is case.entry
        assert _enum_value(case.entry.registry_phase) == "QUARANTINED"
        assert case.entry.quarantine_flag is True
        assert case.registry.quarantine_count == 1


def _expected_registry_trace(mode_name: str) -> list[str]:
    return {
        "RESERVE_INSERT": ["consume", "insert"],
        "CLOSED_REMOVE": ["consume", "delete"],
        "QUARANTINE": ["consume", "phase", "flag", "count"],
    }[mode_name]


def _forbid_registry_primitive_escape(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
) -> None:
    def forbidden_clock() -> float:
        raise AssertionError("registry primitive sampled the clock")

    def forbidden_failfast() -> None:
        raise AssertionError("registry primitive called fail-fast")

    monkeypatch.setattr(module, "_monotonic", forbidden_clock)
    monkeypatch.setattr(module, "_failfast_quarantine", forbidden_failfast)


def test_registry_business_fields_are_confined_to_frozen_primitives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_subject(monkeypatch, _FakeKernel32())
    source_path = Path(inspect.getsourcefile(module) or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    parent_by_child = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }

    def enclosing_function_name(node: ast.AST) -> str | None:
        current = node
        while current in parent_by_child:
            current = parent_by_child[current]
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current.name
        return None

    def assigned_value(node: ast.Attribute) -> ast.AST | None:
        parent = parent_by_child.get(node)
        if isinstance(parent, ast.Assign):
            return parent.value
        if isinstance(parent, ast.AnnAssign):
            return parent.value
        if isinstance(parent, ast.AugAssign):
            return None
        return None

    def phase_value_name(node: ast.Attribute) -> str | None:
        value = assigned_value(node)
        return value.attr if isinstance(value, ast.Attribute) else None

    def is_direct_self_attribute(node: ast.Attribute) -> bool:
        return isinstance(node.value, ast.Name) and node.value.id == "self"

    commit_name = "_commit_registry_transition"
    sweep_name = "_try_sweep_candidate"
    violations: list[str] = []
    safe_entries_readers = {"len", "list", "tuple"}

    def shadowed_entries_reader(node: ast.AST) -> str | None:
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            candidate = node.id
        elif isinstance(node, ast.arg):
            candidate = node.arg
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            candidate = node.name
        elif isinstance(node, ast.ExceptHandler):
            candidate = node.name
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            candidate = node.name
        elif isinstance(node, ast.MatchMapping):
            candidate = node.rest
        elif isinstance(node, ast.alias):
            candidate = node.asname or node.name.split(".", 1)[0]
        else:
            candidate = None
        return candidate if candidate in safe_entries_readers else None

    for node in ast.walk(tree):
        shadowed_name = shadowed_entries_reader(node)
        if shadowed_name is not None:
            violations.append(
                f"{source_path.name}:{getattr(node, 'lineno', 0)} "
                f"shadowed trusted registry.entries reader {shadowed_name}"
            )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__new__":
            violations.append(
                f"{source_path.name}:{getattr(node, 'lineno', 0)} "
                "custom __new__ could replay sensitive __init__ on a live object"
            )
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and node.id in {"__init__", "__new__"}
        ):
            violations.append(
                f"{source_path.name}:{getattr(node, 'lineno', 0)} "
                f"rebound sensitive constructor hook {node.id}"
            )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr in {"__init__", "__new__"}:
            violations.append(
                f"{source_path.name}:{getattr(node, 'lineno', 0)} "
                f"explicit constructor hook access {node.attr} is forbidden"
            )
            continue
        if node.attr == "public_state" and isinstance(
            node.ctx,
            (ast.Store, ast.Del),
        ):
            violations.append(
                f"{source_path.name}:{getattr(node, 'lineno', 0)} "
                "public_state must be a derived read-only view"
            )
            continue
        if node.attr == "entries":
            function_name = enclosing_function_name(node)
            location = f"{source_path.name}:{getattr(node, 'lineno', 0)}"
            parent = parent_by_child.get(node)
            if isinstance(node.ctx, ast.Store):
                if function_name != "__init__" or not is_direct_self_attribute(node):
                    violations.append(
                        f"{location} registry.entries was not initialized on direct self"
                    )
                continue
            if isinstance(node.ctx, ast.Del):
                violations.append(
                    f"{location} registry.entries attribute was deleted"
                )
                continue
            if isinstance(parent, ast.Subscript) and parent.value is node:
                if isinstance(parent.ctx, (ast.Store, ast.Del)) and function_name not in {
                    commit_name,
                    sweep_name,
                }:
                    violations.append(
                        f"{location} registry.entries mutation escaped primitives"
                    )
                continue
            if isinstance(parent, ast.Compare):
                safe_membership = any(
                    comparator is node
                    and isinstance(parent.ops[index], (ast.In, ast.NotIn))
                    for index, comparator in enumerate(parent.comparators)
                )
                if not safe_membership:
                    violations.append(
                        f"{location} registry.entries escaped through a comparison"
                    )
                continue
            if isinstance(parent, ast.Call):
                safe_reader = (
                    isinstance(parent.func, ast.Name)
                    and parent.func.id in safe_entries_readers
                    and parent.args == [node]
                    and parent.keywords == []
                )
                if not safe_reader:
                    violations.append(
                        f"{location} registry.entries escaped through a call"
                    )
                continue
            violations.append(
                f"{location} registry.entries escaped through "
                f"{type(parent).__name__ if parent is not None else 'module root'}"
            )
            continue
        if node.attr not in {
            "registry_phase",
            "quarantine_flag",
            "quarantine_count",
        }:
            continue

        function_name = enclosing_function_name(node)
        location = f"{source_path.name}:{getattr(node, 'lineno', 0)}"

        if node.attr in {"quarantine_flag", "quarantine_count"}:
            if isinstance(node.ctx, ast.Del):
                violations.append(
                    f"{location} deleted protected field {node.attr}"
                )
                continue
            if (
                function_name == "__init__"
                and isinstance(node.ctx, ast.Store)
                and is_direct_self_attribute(node)
            ):
                continue
            if function_name != commit_name:
                violations.append(
                    f"{location} {node.attr} escaped {commit_name}"
                )
            continue

        assert node.attr == "registry_phase"
        if isinstance(node.ctx, ast.Load):
            if function_name not in {commit_name, sweep_name}:
                violations.append(
                    f"{location} registry_phase load escaped primitives"
                )
            continue

        if isinstance(node.ctx, ast.Store):
            if function_name == "__init__" and is_direct_self_attribute(node):
                continue

            phase_name = phase_value_name(node)
            if function_name == commit_name:
                if phase_name != "QUARANTINED":
                    violations.append(
                        f"{location} registry primitive wrote {phase_name!r}"
                    )
            elif function_name == sweep_name:
                violations.append(
                    f"{location} sweep primitive mutated registry_phase"
                )
            elif phase_name not in {"LIVE", "CLOSED"}:
                violations.append(
                    f"{location} lifecycle wrote unsafe phase {phase_name!r}"
                )
            continue

        violations.append(
            f"{location} unsupported registry_phase context "
            f"{type(node.ctx).__name__}"
        )

    dynamic_accessors = {
        "getattr",
        "setattr",
        "hasattr",
        "delattr",
        "__getattribute__",
        "__setattr__",
        "__delattr__",
        "vars",
        "dir",
    }
    sensitive_names = {
        "registry_phase",
        "quarantine_flag",
        "quarantine_count",
        "public_state",
        "entries",
    }
    sensitive_aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            aliases_sensitive_name = (
                isinstance(value, ast.Constant) and value.value in sensitive_names
            ) or (isinstance(value, ast.Name) and value.id in sensitive_aliases)
            if not aliases_sensitive_name:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in sensitive_aliases:
                    sensitive_aliases.add(target.id)
                    changed = True

    def is_sensitive_name_expression(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Constant) and node.value in sensitive_names
        ) or (isinstance(node, ast.Name) and node.id in sensitive_aliases)

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in dynamic_accessors
        ):
            violations.append(
                f"{source_path.name}:{node.lineno} dynamic accessor "
                f"{node.id} is forbidden in production"
            )
            continue
        if isinstance(node, ast.Attribute) and node.attr in {
            "__getattribute__",
            "__setattr__",
            "__delattr__",
            "__dict__",
        }:
            violations.append(
                f"{source_path.name}:{node.lineno} dynamic attribute "
                f"{node.attr} is forbidden in production"
            )
            continue
        if not isinstance(node, ast.Call):
            continue
        accessor_name: str | None = None
        if isinstance(node.func, ast.Name):
            accessor_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            accessor_name = node.func.attr
        if accessor_name == "vars":
            violations.append(
                f"{source_path.name}:{node.lineno} vars bypassed registry AST oracle"
            )
            continue
        if accessor_name not in dynamic_accessors:
            continue
        if any(is_sensitive_name_expression(argument) for argument in node.args):
            violations.append(
                f"{source_path.name}:{node.lineno} dynamic "
                f"{accessor_name} bypassed registry AST oracle"
            )

    assert violations == [], "\n".join(violations)


def test_real_registry_cell_public_state_is_read_only_and_follows_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    real_commit = module._commit_registry_transition
    captured_reserve: list[object] = []

    def capture_reserve_transition(
        registry: object,
        transition: object,
    ) -> object:
        if _enum_value(transition.mode) == "RESERVE_INSERT":
            captured_reserve.append(transition)
        return real_commit(registry, transition)

    monkeypatch.setattr(
        module,
        "_commit_registry_transition",
        capture_reserve_transition,
    )
    source = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3A41,
        event_handle=0x2A41,
    )
    assert len(captured_reserve) == 1
    cell = captured_reserve[0].expected_cell
    descriptor = inspect.getattr_static(type(cell), "public_state")
    assert type(descriptor) is property
    assert descriptor.fset is None
    assert descriptor.fdel is None
    slots = getattr(type(cell), "__slots__", ())
    normalized_slots = (slots,) if isinstance(slots, str) else tuple(slots)
    assert "public_state" not in normalized_slots
    assert cell.public_state is module.OverlappedState.PENDING
    with pytest.raises(AttributeError):
        cell.public_state = module.OverlappedState.NEW

    destination = source.transfer()
    _assert_state(module, source, "TRANSFERRED")
    _assert_state(module, destination, "PENDING")
    assert cell.public_state is module.OverlappedState.PENDING
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xA441))
    completion = destination.wait_to_deadline()
    assert destination.completion is completion
    _assert_state(module, destination, "DRAINED")
    assert cell.public_state is module.OverlappedState.DRAINED
    destination.close()
    _assert_state(module, destination, "CLOSED")
    assert cell.public_state is module.OverlappedState.CLOSED


def test_registry_transition_private_test_abi_is_exact_frozen_and_slotted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_subject(monkeypatch, _FakeKernel32())

    exact_string_enums = {
        module._RegistryTransitionMode: [
            "RESERVE_INSERT",
            "CLOSED_REMOVE",
            "QUARANTINE",
        ],
        module._RegistryTransitionResult: [
            "MISMATCH",
            "COMMITTED",
            "ALREADY_CONSUMED",
        ],
        module._RegistryConsumeState: ["NEVER", "CONSUMED"],
        module._RegistryPhase: ["EMPTY", "LIVE", "QUARANTINED", "CLOSED"],
    }
    for enum_type, expected_names in exact_string_enums.items():
        assert issubclass(enum_type, Enum)
        assert list(enum_type.__members__) == expected_names
        assert [member.name for member in enum_type.__members__.values()] == expected_names
        assert [member.value for member in enum_type.__members__.values()] == expected_names
    assert issubclass(module._SweepResult, Enum)
    assert list(module._SweepResult.__members__) == ["REMOVED", "SKIPPED"]
    assert [member.name for member in module._SweepResult.__members__.values()] == [
        "REMOVED",
        "SKIPPED",
    ]
    assert module._RegistryTransition.__slots__ == (
        "mode",
        "entry_token",
        "expected_entry",
        "expected_cell",
        "consume_state",
    )
    assert module._RegistryConsumeMarker.__slots__ == ("value",)
    assert is_dataclass(module._RegistryConsumeMarker)
    marker_fields = dataclass_fields(module._RegistryConsumeMarker)
    assert [field.name for field in marker_fields] == ["value"]
    assert get_type_hints(module._RegistryConsumeMarker) == {
        "value": module._RegistryConsumeState,
    }

    marker = module._RegistryConsumeMarker(
        value=module._RegistryConsumeState.NEVER,
    )
    assert not hasattr(marker, "__dict__")
    case = _make_registry_transition_case(module, "RESERVE_INSERT")
    transition = case.transition
    assert is_dataclass(transition)
    assert not hasattr(transition, "__dict__")
    metadata_before = _registry_transition_metadata(transition)
    with pytest.raises(FrozenInstanceError):
        transition.expected_cell = object()
    assert _registry_transition_metadata(transition) == metadata_before


@pytest.mark.parametrize(
    "mode_name",
    ["RESERVE_INSERT", "CLOSED_REMOVE", "QUARANTINE"],
)
def test_registry_transition_phase_and_public_state_source_matrix_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    mode_name: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    _forbid_registry_primitive_escape(monkeypatch, module)
    phase_names = ["EMPTY", "LIVE", "QUARANTINED", "CLOSED"]
    public_state_names = [state.name for state in module.OverlappedState]

    for phase_name in phase_names:
        for public_state_name in public_state_names:
            case = _make_registry_transition_case(module, mode_name)
            case.entry.set_phase_without_probe(
                getattr(module._RegistryPhase, phase_name)
            )
            case.cell.public_state = getattr(
                module.OverlappedState,
                public_state_name,
            )
            before = _registry_graph_snapshot(case.registry, case.transition)
            before_native = _native_call_counts(kernel)

            result = module._commit_registry_transition(
                case.registry,
                case.transition,
            )

            legal = (
                mode_name == "RESERVE_INSERT"
                and phase_name == "EMPTY"
                and public_state_name == "CREATE_EVENT_IN_FLIGHT"
            ) or (
                mode_name == "CLOSED_REMOVE"
                and phase_name == "CLOSED"
                and public_state_name == "CLOSED"
            ) or (
                mode_name == "QUARANTINE"
                and (
                    (
                        phase_name == "EMPTY"
                        and public_state_name == "CREATE_EVENT_UNKNOWN"
                    )
                    or (
                        phase_name == "LIVE"
                        and public_state_name
                        in {"CLOSE_FAILED_QUARANTINED", "CLOSE_UNKNOWN"}
                    )
                )
            )
            if legal:
                _assert_registry_transition_result(module, result, "COMMITTED")
                assert case.probe.trace == _expected_registry_trace(mode_name)
                _assert_registry_commit_target(module, case, mode_name)
            else:
                _assert_registry_transition_result(module, result, "MISMATCH")
                assert _registry_graph_snapshot(
                    case.registry,
                    case.transition,
                ) == before
                assert case.probe.trace == []
                assert _enum_value(case.transition.consume_state.value) == "NEVER"
            _assert_no_native_calls_after(kernel, before_native)


_REGISTRY_SOURCE_GUARD_CASES = [
    pytest.param("RESERVE_INSERT", "wrong-token", id="reserve-wrong-token"),
    pytest.param("RESERVE_INSERT", "wrong-cell", id="reserve-wrong-cell"),
    pytest.param("RESERVE_INSERT", "token-present", id="reserve-token-present"),
    pytest.param("CLOSED_REMOVE", "wrong-token", id="closed-wrong-token"),
    pytest.param("CLOSED_REMOVE", "wrong-cell", id="closed-wrong-cell"),
    pytest.param("CLOSED_REMOVE", "wrong-entry", id="closed-wrong-entry"),
    pytest.param("CLOSED_REMOVE", "mapping-aba", id="closed-mapping-aba"),
    pytest.param("CLOSED_REMOVE", "token-absent", id="closed-token-absent"),
    pytest.param("QUARANTINE", "wrong-token", id="quarantine-wrong-token"),
    pytest.param("QUARANTINE", "wrong-cell", id="quarantine-wrong-cell"),
    pytest.param("QUARANTINE", "wrong-entry", id="quarantine-wrong-entry"),
    pytest.param("QUARANTINE", "mapping-aba", id="quarantine-mapping-aba"),
    pytest.param("QUARANTINE", "token-absent", id="quarantine-token-absent"),
    pytest.param("QUARANTINE", "flag-already-set", id="quarantine-flag-set"),
    pytest.param("QUARANTINE", "capacity-full", id="quarantine-capacity-full"),
]


@pytest.mark.parametrize(("mode_name", "guard_case"), _REGISTRY_SOURCE_GUARD_CASES)
def test_registry_transition_identity_and_one_shot_guards_are_zero_mutation(
    monkeypatch: pytest.MonkeyPatch,
    mode_name: str,
    guard_case: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    _forbid_registry_primitive_escape(monkeypatch, module)
    case = _make_registry_transition_case(module, mode_name)
    transition = case.transition

    if guard_case == "wrong-token":
        equal_token = _EqualButDistinctRegistryToken(case.token)
        assert equal_token == case.token
        assert equal_token is not case.token
        transition = replace(transition, entry_token=equal_token)
    elif guard_case == "wrong-cell":
        equal_cell = _EqualButDistinctRegistryCell(case.cell.public_state)
        assert equal_cell == case.cell
        assert equal_cell is not case.cell
        transition = replace(
            transition,
            expected_cell=equal_cell,
        )
    elif guard_case == "token-present":
        case.registry.entries.install_without_probe(case.token, case.entry)
    elif guard_case == "wrong-entry":
        fresh = _equal_but_distinct_registry_entry(case)
        assert fresh == case.entry
        assert fresh is not case.entry
        transition = replace(
            transition,
            expected_entry=fresh,
            expected_cell=case.cell,
        )
    elif guard_case == "mapping-aba":
        replacement = _equal_but_distinct_registry_entry(case)
        assert replacement == case.entry
        assert replacement is not case.entry
        case.registry.entries.install_without_probe(
            case.token,
            replacement,
        )
    elif guard_case == "token-absent":
        case.registry.entries.remove_without_probe(case.token)
    elif guard_case == "flag-already-set":
        case.entry.set_flag_without_probe(True)
    else:
        assert guard_case == "capacity-full"
        case.registry.set_count_without_probe(32)

    before = _registry_graph_snapshot(case.registry, transition)
    metadata_before = _registry_transition_metadata(transition)
    before_native = _native_call_counts(kernel)

    result = module._commit_registry_transition(case.registry, transition)

    _assert_registry_transition_result(module, result, "MISMATCH")
    assert _registry_graph_snapshot(case.registry, transition) == before
    assert _registry_transition_metadata(transition) == metadata_before
    assert _enum_value(transition.consume_state.value) == "NEVER"
    assert case.probe.trace == []
    _assert_no_native_calls_after(kernel, before_native)


_REGISTRY_COMMIT_CASES = [
    pytest.param("RESERVE_INSERT", "default", id="reserve"),
    pytest.param("CLOSED_REMOVE", "default", id="closed-remove"),
    pytest.param(
        "QUARANTINE",
        "empty-create-unknown",
        id="quarantine-create-unknown",
    ),
    pytest.param(
        "QUARANTINE",
        "live-close-failed",
        id="quarantine-close-failed",
    ),
    pytest.param(
        "QUARANTINE",
        "live-close-unknown",
        id="quarantine-close-unknown",
    ),
]


@pytest.mark.parametrize(("mode_name", "source_variant"), _REGISTRY_COMMIT_CASES)
def test_registry_transition_commit_replay_and_consumed_aba_are_exact(
    monkeypatch: pytest.MonkeyPatch,
    mode_name: str,
    source_variant: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    _forbid_registry_primitive_escape(monkeypatch, module)
    case = _make_registry_transition_case(module, mode_name)
    if source_variant == "empty-create-unknown":
        case.entry.set_phase_without_probe(module._RegistryPhase.EMPTY)
        case.cell.public_state = module.OverlappedState.CREATE_EVENT_UNKNOWN
    elif source_variant == "live-close-failed":
        case.cell.public_state = module.OverlappedState.CLOSE_FAILED_QUARANTINED
    elif source_variant == "live-close-unknown":
        case.cell.public_state = module.OverlappedState.CLOSE_UNKNOWN
    else:
        assert source_variant == "default"

    metadata_before = _registry_transition_metadata(case.transition)
    before_native = _native_call_counts(kernel)
    result = module._commit_registry_transition(
        case.registry,
        case.transition,
    )

    _assert_registry_transition_result(module, result, "COMMITTED")
    assert case.probe.trace == _expected_registry_trace(mode_name)
    assert _registry_transition_metadata(case.transition) == metadata_before
    _assert_registry_commit_target(module, case, mode_name)
    committed = _registry_graph_snapshot(case.registry, case.transition)
    trace_after_commit = list(case.probe.trace)

    replay = module._commit_registry_transition(
        case.registry,
        case.transition,
    )

    _assert_registry_transition_result(module, replay, "ALREADY_CONSUMED")
    assert _registry_graph_snapshot(case.registry, case.transition) == committed
    assert case.probe.trace == trace_after_commit

    fresh = _equal_but_distinct_registry_entry(case)
    assert fresh == case.entry
    assert fresh is not case.entry
    case.registry.entries.install_without_probe(case.token, fresh)
    aba_before = _registry_graph_snapshot(case.registry, case.transition)
    replay_after_aba = module._commit_registry_transition(
        case.registry,
        case.transition,
    )

    _assert_registry_transition_result(
        module,
        replay_after_aba,
        "ALREADY_CONSUMED",
    )
    assert case.registry.entries[case.token] is fresh
    assert _registry_graph_snapshot(case.registry, case.transition) == aba_before
    assert case.probe.trace == trace_after_commit
    _assert_no_native_calls_after(kernel, before_native)


_REGISTRY_PARTIAL_STEPS = [
    pytest.param("RESERVE_INSERT", "consume", id="reserve-after-consume"),
    pytest.param("RESERVE_INSERT", "insert", id="reserve-after-insert"),
    pytest.param("CLOSED_REMOVE", "consume", id="closed-after-consume"),
    pytest.param("CLOSED_REMOVE", "delete", id="closed-after-delete"),
    pytest.param("QUARANTINE", "consume", id="quarantine-after-consume"),
    pytest.param("QUARANTINE", "phase", id="quarantine-after-phase"),
    pytest.param("QUARANTINE", "flag", id="quarantine-after-flag"),
    pytest.param("QUARANTINE", "count", id="quarantine-after-count"),
]


@pytest.mark.parametrize("interrupt_type", [RuntimeError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize(("mode_name", "fault_step"), _REGISTRY_PARTIAL_STEPS)
def test_registry_transition_consume_first_partial_steps_never_replay_or_hit_aba(
    monkeypatch: pytest.MonkeyPatch,
    mode_name: str,
    fault_step: str,
    interrupt_type: type[BaseException],
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    _forbid_registry_primitive_escape(monkeypatch, module)
    probe = _RegistryMutationProbe(
        fail_after=fault_step,
        interrupt_type=interrupt_type,
    )
    case = _make_registry_transition_case(module, mode_name, probe=probe)
    metadata_before = _registry_transition_metadata(case.transition)
    before_native = _native_call_counts(kernel)
    full_trace = _expected_registry_trace(mode_name)
    expected_prefix = full_trace[: full_trace.index(fault_step) + 1]

    with pytest.raises(interrupt_type) as captured:
        module._commit_registry_transition(
            case.registry,
            case.transition,
        )

    assert type(captured.value) is interrupt_type
    assert case.probe.trace == expected_prefix
    assert _registry_transition_metadata(case.transition) == metadata_before
    assert _enum_value(case.transition.consume_state.value) == "CONSUMED"
    if mode_name == "RESERVE_INSERT":
        assert (case.registry.entries.get(case.token) is case.entry) is (
            fault_step == "insert"
        )
        assert _enum_value(case.entry.registry_phase) == "EMPTY"
        assert case.entry.quarantine_flag is False
        assert case.registry.quarantine_count == 0
    elif mode_name == "CLOSED_REMOVE":
        assert (case.registry.entries.get(case.token) is case.entry) is (
            fault_step == "consume"
        )
        assert _enum_value(case.entry.registry_phase) == "CLOSED"
        assert case.entry.quarantine_flag is False
        assert case.registry.quarantine_count == 0
    else:
        assert mode_name == "QUARANTINE"
        assert case.registry.entries[case.token] is case.entry
        phase_committed = fault_step in {"phase", "flag", "count"}
        flag_committed = fault_step in {"flag", "count"}
        count_committed = fault_step == "count"
        assert _enum_value(case.entry.registry_phase) == (
            "QUARANTINED" if phase_committed else "LIVE"
        )
        assert case.entry.quarantine_flag is flag_committed
        assert case.registry.quarantine_count == (1 if count_committed else 0)
    _assert_no_native_calls_after(kernel, before_native)

    probe.fail_after = None
    partial = _registry_graph_snapshot(case.registry, case.transition)
    trace_after_fault = list(case.probe.trace)
    replay = module._commit_registry_transition(
        case.registry,
        case.transition,
    )
    _assert_registry_transition_result(module, replay, "ALREADY_CONSUMED")
    assert _registry_graph_snapshot(case.registry, case.transition) == partial
    assert case.probe.trace == trace_after_fault

    fresh = _equal_but_distinct_registry_entry(case)
    assert fresh == case.entry
    assert fresh is not case.entry
    case.registry.entries.install_without_probe(case.token, fresh)
    aba_before = _registry_graph_snapshot(case.registry, case.transition)
    replay_after_aba = module._commit_registry_transition(
        case.registry,
        case.transition,
    )
    _assert_registry_transition_result(
        module,
        replay_after_aba,
        "ALREADY_CONSUMED",
    )
    assert case.registry.entries[case.token] is fresh
    assert _registry_graph_snapshot(case.registry, case.transition) == aba_before
    assert case.probe.trace == trace_after_fault
    _assert_no_native_calls_after(kernel, before_native)


@pytest.mark.parametrize("fault_position", ["pre", "post"])
@pytest.mark.parametrize(
    "interrupt_type",
    [RuntimeError, KeyboardInterrupt, SystemExit],
)
def test_public_reserve_insert_faults_release_both_locks_and_remain_sweepable(
    monkeypatch: pytest.MonkeyPatch,
    fault_position: str,
    interrupt_type: type[BaseException],
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    real_commit = module._commit_registry_transition
    real_sweep = module._try_sweep_candidate
    original = interrupt_type(
        f"RESERVE_INSERT {fault_position} interrupted"
    )
    captured_transitions: list[object] = []
    fault_delivered = False

    def faulting_commit(
        registry: object,
        transition: object,
    ) -> object:
        nonlocal fault_delivered
        if (
            _registry_status(transition.mode) != "RESERVE_INSERT"
            or fault_delivered
        ):
            return real_commit(registry, transition)

        fault_delivered = True
        captured_transitions.append(transition)
        if fault_position == "pre":
            raise original

        result = real_commit(registry, transition)
        _assert_registry_transition_result(module, result, "COMMITTED")
        raise original

    monkeypatch.setattr(
        module,
        "_commit_registry_transition",
        faulting_commit,
    )

    handoff = _capture_public_registry_fault(
        module,
        lambda: module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x3B31,
            absolute_deadline=_future_deadline(),
        ),
        original,
        interrupt_type,
        reason="lifecycle_violation",
        stage="create_event",
        owner=None,
        completion=None,
    )

    assert fault_delivered is True
    assert len(captured_transitions) == 1
    transition = captured_transitions[0]
    assert _registry_status(transition.consume_state.value) == (
        "NEVER" if fault_position == "pre" else "CONSUMED"
    )
    assert _native_call_counts(kernel) == {
        name: 0 for name in _native_call_counts(kernel)
    }

    monkeypatch.setattr(
        module,
        "_commit_registry_transition",
        real_commit,
    )
    _assert_errors_and_tracebacks_not_cached(
        module,
        None,
        original,
        handoff,
    )
    _assert_captured_registry_cell_lock_released(module, transition)

    public_sweep_results: list[str] = []

    def recording_sweep(
        registry: object,
        entry_token: object,
        expected_entry: object,
        expected_cell: object,
    ) -> object:
        result = real_sweep(
            registry,
            entry_token,
            expected_entry,
            expected_cell,
        )
        if expected_entry is transition.expected_entry:
            _assert_sweep_outcome(module, result, "REMOVED")
            public_sweep_results.append(_registry_status(result))
        return result

    monkeypatch.setattr(
        module,
        "_try_sweep_candidate",
        recording_sweep,
    )
    kernel.event_handles.append(0x2B31)
    kernel.connect_outcomes.append(_Outcome(True))

    def public_followup() -> object:
        followup = module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x3B32,
            absolute_deadline=_future_deadline(),
        )
        _assert_state(module, followup, "DRAINED")
        followup.close()
        _assert_state(module, followup, "CLOSED")
        return followup

    _call_in_other_thread(
        public_followup,
        description="public reserve follow-up after registry fault",
    )

    assert public_sweep_results == (
        [] if fault_position == "pre" else ["REMOVED"]
    )
    assert len(kernel.CreateEventW.calls) == 1
    assert len(kernel.ConnectNamedPipe.calls) == 1
    assert len(kernel.CloseHandle.calls) == 1


@pytest.mark.parametrize(
    "interrupt_type",
    [RuntimeError, KeyboardInterrupt, SystemExit],
)
def test_public_reserve_post_commit_faults_over_capacity_never_exhaust_registry(
    monkeypatch: pytest.MonkeyPatch,
    interrupt_type: type[BaseException],
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    real_commit = module._commit_registry_transition
    real_sweep = module._try_sweep_candidate
    transitions: list[object] = []
    tracked_entries: dict[int, object] = {}
    removed_entries: list[object] = []
    sweep_results: list[str] = []
    inject_fault = True
    current_fault: BaseException | None = None

    def recording_sweep(
        registry: object,
        entry_token: object,
        expected_entry: object,
        expected_cell: object,
    ) -> object:
        result = real_sweep(
            registry,
            entry_token,
            expected_entry,
            expected_cell,
        )
        if tracked_entries.get(id(expected_entry)) is expected_entry:
            _assert_sweep_outcome(module, result, "REMOVED")
            removed_entries.append(expected_entry)
            sweep_results.append(_registry_status(result))
        return result

    def faulting_commit(
        registry: object,
        transition: object,
    ) -> object:
        if (
            _registry_status(transition.mode) != "RESERVE_INSERT"
            or not inject_fault
        ):
            return real_commit(registry, transition)

        assert current_fault is not None
        result = real_commit(registry, transition)
        _assert_registry_transition_result(module, result, "COMMITTED")
        transitions.append(transition)
        tracked_entries[id(transition.expected_entry)] = (
            transition.expected_entry
        )
        raise current_fault

    monkeypatch.setattr(module, "_try_sweep_candidate", recording_sweep)
    monkeypatch.setattr(
        module,
        "_commit_registry_transition",
        faulting_commit,
    )

    for index in range(300):
        current_fault = interrupt_type(
            f"RESERVE_INSERT post interruption {index}"
        )
        _capture_public_registry_fault(
            module,
            lambda index=index: (
                module.OwnedOverlappedPipeOperation.start_connect(
                    pipe_handle=0x4000 + index,
                    absolute_deadline=_future_deadline(),
                )
            ),
            current_fault,
            interrupt_type,
            reason="lifecycle_violation",
            stage="create_event",
            owner=None,
            completion=None,
        )

    assert len(transitions) == 300
    assert all(
        _registry_status(transition.consume_state.value) == "CONSUMED"
        for transition in transitions
    )
    assert _native_call_counts(kernel) == {
        name: 0 for name in _native_call_counts(kernel)
    }

    inject_fault = False
    current_fault = None
    kernel.event_handles.append(0x2B40)
    kernel.connect_outcomes.append(_Outcome(True))

    def final_followup() -> object:
        operation = module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x3B40,
            absolute_deadline=_future_deadline(),
        )
        operation.close()
        return operation

    operation = _call_in_other_thread(
        final_followup,
        description="301st public reserve after post-commit interruptions",
    )
    _assert_state(module, operation, "CLOSED")

    assert len(removed_entries) == 300
    assert len({id(entry) for entry in removed_entries}) == 300
    assert set(map(id, removed_entries)) == set(tracked_entries)
    assert sweep_results == ["REMOVED"] * 300
    assert len(kernel.CreateEventW.calls) == 1
    assert len(kernel.ConnectNamedPipe.calls) == 1
    assert len(kernel.CloseHandle.calls) == 1


@pytest.mark.parametrize("interrupt_type", _INTERRUPTION_TYPES)
@pytest.mark.parametrize("fault_position", ["before", "after"])
@pytest.mark.parametrize("owner_state", ["PENDING", "DRAINED"])
def test_transfer_commit_boundary_always_exposes_the_unique_authority_owner(
    monkeypatch: pytest.MonkeyPatch,
    fault_position: str,
    interrupt_type: type[BaseException],
    owner_state: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    source = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3103,
        event_handle=0x2103,
    )
    if owner_state == "DRAINED":
        kernel.wait_results.append(WAIT_OBJECT_0)
        kernel.result_outcomes.append(_Outcome(True, byte_count=0xA201))
        frozen_completion = source.wait_to_deadline()
    else:
        frozen_completion = None
    _assert_state(module, source, owner_state)
    assert source.completion is frozen_completion
    original_commit = module._commit_authority_transfer
    original = interrupt_type(f"transfer-{fault_position}")

    def faulting_commit(*args: object, **kwargs: object) -> object:
        if fault_position == "before":
            raise original
        result = original_commit(*args, **kwargs)
        raise original

    monkeypatch.setattr(module, "_commit_authority_transfer", faulting_commit)
    primary, handoff = _capture_outward_interruption(
        module,
        original,
        source.transfer,
    )
    active = handoff.resource_owner
    assert active is not None
    _assert_typed_error(
        module,
        handoff,
        reason="ownership_transfer_failed",
        stage="transfer",
        owner=active,
        winerror=None,
        completion=frozen_completion,
    )
    if fault_position == "before":
        assert active is source
        assert source.state is not module.OverlappedState.TRANSFERRED
        with pytest.raises(module.OverlappedPipeError):
            source.recover_transferred_owner()
    else:
        _assert_state(module, source, "TRANSFERRED")
        assert active is source.recover_transferred_owner()

    monkeypatch.setattr(
        module,
        "_commit_authority_transfer",
        original_commit,
    )
    _assert_errors_and_tracebacks_not_cached(
        module,
        active,
        primary,
        handoff,
    )
    if owner_state == "PENDING":
        kernel.wait_results.append(WAIT_OBJECT_0)
        kernel.result_outcomes.append(_Outcome(True, byte_count=0xA201))
        frozen_completion = active.wait_to_deadline()
    else:
        before_replay = _native_call_counts(kernel)
        assert active.wait_to_deadline() is frozen_completion
        assert active.cancel_and_drain() is frozen_completion
        _assert_no_native_calls_after(kernel, before_replay)
    _assert_state(module, active, "DRAINED")
    assert active.completion is frozen_completion
    active.close()


@pytest.mark.parametrize(
    "later_state",
    ["transferred", "closed", "quarantined"],
)
def test_transfer_chain_recovery_forever_returns_each_direct_successor(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    later_state: str,
) -> None:
    if later_state == "quarantined":
        delegated = _delegate_quarantine_node_to_subprocess(
            request,
            expected_exit_code=0,
        )
        if delegated is not None:
            return

    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    source_a = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3110,
        event_handle=0x2110,
    )

    successor_b = source_a.transfer()
    successor_c = successor_b.transfer()

    _assert_state(module, source_a, "TRANSFERRED")
    _assert_state(module, successor_b, "TRANSFERRED")
    assert source_a.recover_transferred_owner() is successor_b
    assert successor_b.recover_transferred_owner() is successor_c

    final_authority: object | None = None
    if later_state == "transferred":
        final_authority = successor_c.transfer()
        _assert_state(module, successor_c, "TRANSFERRED")
        assert successor_c.recover_transferred_owner() is final_authority
    else:
        kernel.cancel_outcomes.append(_Outcome(True))
        kernel.wait_results.append(WAIT_OBJECT_0)
        kernel.result_outcomes.append(
            _Outcome(False, ERROR_OPERATION_ABORTED)
        )
        completion = successor_c.cancel_and_drain()
        assert completion.native_outcome is module.NativeOutcome.CANCELLED
        _assert_state(module, successor_c, "DRAINED")

        if later_state == "closed":
            successor_c.close()
            _assert_state(module, successor_c, "CLOSED")
        else:
            kernel.close_outcomes.append(
                _Outcome(False, ERROR_INVALID_HANDLE)
            )
            with pytest.raises(module.OverlappedPipeError) as quarantined:
                successor_c.close()
            _assert_typed_error(
                module,
                quarantined.value,
                reason="close_quarantined",
                stage="close",
                owner=successor_c,
                winerror=ERROR_INVALID_HANDLE,
            )
            _assert_state(
                module,
                successor_c,
                "CLOSE_FAILED_QUARANTINED",
            )

    before_recovery = _native_call_counts(kernel)
    assert source_a.recover_transferred_owner() is successor_b
    assert source_a.recover_transferred_owner() is successor_b
    assert successor_b.recover_transferred_owner() is successor_c
    assert successor_b.recover_transferred_owner() is successor_c
    _assert_no_native_calls_after(kernel, before_recovery)

    if final_authority is not None:
        kernel.cancel_outcomes.append(_Outcome(True))
        kernel.wait_results.append(WAIT_OBJECT_0)
        kernel.result_outcomes.append(
            _Outcome(False, ERROR_OPERATION_ABORTED)
        )
        final_authority.cancel_and_drain()
        final_authority.close()

        after_final_close = _native_call_counts(kernel)
        assert source_a.recover_transferred_owner() is successor_b
        assert successor_b.recover_transferred_owner() is successor_c
        assert successor_c.recover_transferred_owner() is final_authority
        _assert_no_native_calls_after(kernel, after_final_close)


def test_transfer_epilogue_second_interruption_preserves_recoverable_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    source = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3111,
        event_handle=0x2111,
    )
    real_commit = module._commit_authority_transfer
    second = SystemExit(29)

    class _InterruptDuringTransferEpilogue(KeyboardInterrupt):
        __slots__ = ("_armed", "_second", "triggered")

        def __init__(self) -> None:
            super().__init__("transfer commit result became outward-unknown")
            self._armed = True
            self._second = second
            self.triggered = False

        def __getattribute__(self, name: str) -> object:
            if name == "__cause__" and object.__getattribute__(
                self,
                "_armed",
            ):
                object.__setattr__(self, "_armed", False)
                object.__setattr__(self, "triggered", True)
                raise object.__getattribute__(self, "_second")
            return KeyboardInterrupt.__getattribute__(self, name)

    first = _InterruptDuringTransferEpilogue()
    commit_calls = 0

    def commit_then_interrupt(*args: object, **kwargs: object) -> object:
        nonlocal commit_calls
        commit_calls += 1
        real_commit(*args, **kwargs)
        raise first

    monkeypatch.setattr(
        module,
        "_commit_authority_transfer",
        commit_then_interrupt,
    )

    with pytest.raises(BaseException) as interrupted:
        source.transfer()

    assert first.triggered is True
    assert commit_calls == 1
    monkeypatch.setattr(
        module,
        "_commit_authority_transfer",
        real_commit,
    )

    before_recovery = _native_call_counts(kernel)
    successor = source.recover_transferred_owner()
    assert source.recover_transferred_owner() is successor
    _assert_state(module, source, "TRANSFERRED")
    _assert_state(module, successor, "PENDING")
    _assert_no_native_calls_after(kernel, before_recovery)
    _assert_errors_and_tracebacks_not_cached(
        module,
        successor,
        first,
        second,
        interrupted.value,
    )

    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(
        _Outcome(False, ERROR_OPERATION_ABORTED)
    )
    successor.cancel_and_drain()
    successor.close()


@pytest.mark.parametrize("close_mode", ["false", "return-unknown"])
def test_close_failure_or_unknown_is_quarantined_and_never_retried(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    close_mode: str,
) -> None:
    delegated = _delegate_quarantine_node_to_subprocess(
        request,
        expected_exit_code=0,
    )
    if delegated is not None:
        return

    kernel = _FakeKernel32()
    kernel.event_handles.append(0x2201)
    kernel.connect_outcomes.append(_Outcome(True))
    if close_mode == "false":
        kernel.close_outcomes.append(_Outcome(False, ERROR_INVALID_HANDLE))
    else:
        kernel.close_outcomes.append(_Outcome(True, interrupt=KeyboardInterrupt()))
    module = _load_subject(monkeypatch, kernel)
    operation = module.OwnedOverlappedPipeOperation.start_connect(
        pipe_handle=0x3201,
        absolute_deadline=_future_deadline(),
    )
    completion = operation.completion
    assert completion is not None
    _assert_state(module, operation, "DRAINED")
    expected_winerror = (
        ERROR_INVALID_HANDLE if close_mode == "false" else None
    )

    if close_mode == "false":
        with pytest.raises(module.OverlappedPipeError) as captured:
            operation.close()
        error = captured.value
        _assert_typed_error(
            module,
            error,
            reason="close_quarantined",
            stage="close",
            owner=operation,
            winerror=ERROR_INVALID_HANDLE,
            completion=completion,
        )
        _assert_state(module, operation, "CLOSE_FAILED_QUARANTINED")
    else:
        with pytest.raises(BaseExceptionGroup) as captured_group:
            operation.close()
        primary, handoff = _assert_interrupt_group(
            module, captured_group.value, KeyboardInterrupt
        )
        _assert_typed_error(
            module,
            handoff,
            reason="close_quarantined",
            stage="close",
            owner=operation,
            winerror=None,
            completion=completion,
        )
        _assert_state(module, operation, "CLOSE_UNKNOWN")
        _assert_errors_and_tracebacks_not_cached(
            module,
            operation,
            primary,
            captured_group.value,
            handoff,
        )

    assert len(kernel.CloseHandle.calls) == 1
    before = _native_call_counts(kernel)
    assert operation.wait_to_deadline() is completion
    assert operation.cancel_and_drain() is completion
    _assert_no_native_calls_after(kernel, before)
    with pytest.raises(module.OverlappedPipeError) as repeated_source:
        operation.close()
    _assert_typed_error(
        module,
        repeated_source.value,
        reason="close_quarantined",
        stage="close",
        owner=operation,
        winerror=expected_winerror,
        completion=completion,
    )
    _assert_no_native_calls_after(kernel, before)
    destination = operation.transfer()
    _assert_state(module, operation, "TRANSFERRED")
    assert operation.recover_transferred_owner() is destination
    assert destination.completion is completion
    assert destination.wait_to_deadline() is completion
    assert destination.cancel_and_drain() is completion
    _assert_no_native_calls_after(kernel, before)
    before = _native_call_counts(kernel)
    with pytest.raises(module.OverlappedPipeError) as repeated_destination:
        destination.close()
    _assert_typed_error(
        module,
        repeated_destination.value,
        reason="close_quarantined",
        stage="close",
        owner=destination,
        winerror=expected_winerror,
        completion=completion,
    )
    _assert_no_native_calls_after(kernel, before)


@pytest.mark.parametrize(
    ("failfast_mode", "expected_exit_code"),
    [
        ("quarantine", FAILFAST_QUARANTINE_EXIT_CODE),
        ("returned", FAILFAST_RETURNED_EXIT_CODE),
    ],
    ids=["quarantine-exit-74", "returned-guard-exit-75"],
)
def test_close_quarantine_33rd_entry_uses_exact_failfast_exit_code(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    failfast_mode: str,
    expected_exit_code: int,
) -> None:
    delegated = _delegate_quarantine_node_to_subprocess(
        request,
        expected_exit_code=expected_exit_code,
    )
    if delegated is not None:
        if failfast_mode == "quarantine":
            assert "TASK_3B2_CLOSE_CALLS=33" in delegated.stdout
        else:
            assert "TASK_3B2_FAILFAST_STUB_RETURNED" in delegated.stdout
            assert "TASK_3B2_FAILFAST_CALLER_RETURNED" not in delegated.stdout
        return

    kernel = _FakeKernel32()
    kernel.event_handles.extend(0x2300 + index for index in range(33))
    kernel.connect_outcomes.extend(_Outcome(True) for _ in range(33))
    kernel.close_outcomes.extend(
        _Outcome(False, ERROR_INVALID_HANDLE) for _ in range(33)
    )
    module = _load_subject(monkeypatch, kernel)

    def observed_failfast() -> None:
        if failfast_mode == "quarantine":
            print(
                f"TASK_3B2_CLOSE_CALLS={len(kernel.CloseHandle.calls)}",
                flush=True,
            )
            os._exit(FAILFAST_QUARANTINE_EXIT_CODE)
        print("TASK_3B2_FAILFAST_STUB_RETURNED", flush=True)

    monkeypatch.setattr(module, "_failfast_quarantine", observed_failfast)
    owners: list[object] = []
    for index in range(32):
        operation = module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x3300 + index,
            absolute_deadline=_future_deadline(),
        )
        owners.append(operation)
        with pytest.raises(module.OverlappedPipeError):
            operation.close()

    last = module.OwnedOverlappedPipeOperation.start_connect(
        pipe_handle=0x33FF,
        absolute_deadline=_future_deadline(),
    )
    last.close()
    print("TASK_3B2_FAILFAST_CALLER_RETURNED", flush=True)
    pytest.fail("quarantine fail-fast path returned to Python")


def test_real_failfast_quarantine_seam_exits_with_exact_process_code(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegated = _delegate_quarantine_node_to_subprocess(
        request,
        expected_exit_code=FAILFAST_QUARANTINE_EXIT_CODE,
    )
    if delegated is not None:
        assert "TASK_3B2_REAL_FAILFAST_SEAM_ENTER" in delegated.stdout
        assert "TASK_3B2_REAL_FAILFAST_SEAM_RETURNED" not in delegated.stdout
        assert "TASK_3B2_REAL_FAILFAST_FINALLY_RAN" not in delegated.stdout
        return

    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    print("TASK_3B2_REAL_FAILFAST_SEAM_ENTER", flush=True)
    try:
        module._failfast_quarantine()
        print("TASK_3B2_REAL_FAILFAST_SEAM_RETURNED", flush=True)
    finally:
        print("TASK_3B2_REAL_FAILFAST_FINALLY_RAN", flush=True)
        os._exit(91)


def test_live_registry_capacity_is_checked_before_257th_create_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.extend(0x4000 + index for index in range(256))
    kernel.connect_outcomes.extend(
        _Outcome(False, ERROR_IO_PENDING) for _ in range(256)
    )
    module = _load_subject(monkeypatch, kernel)

    assert module._MAX_FRAME_BYTES == 0xFFFFFFFF
    with monkeypatch.context() as boundary_patch:
        boundary_patch.setattr(module, "_MAX_FRAME_BYTES", 8)
        for index in range(300):
            with pytest.raises(ValueError):
                module.OwnedOverlappedPipeOperation.start_write(
                    pipe_handle=0x4800 + index,
                    immutable_frame=b"z" * 9,
                    absolute_deadline=_future_deadline(),
                )
    assert module._MAX_FRAME_BYTES == 0xFFFFFFFF
    assert _native_call_counts(kernel) == {
        name: 0 for name in _native_call_counts(kernel)
    }

    owners = [
        module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x5000 + index,
            absolute_deadline=_future_deadline(),
        )
        for index in range(256)
    ]

    with pytest.raises(module.OverlappedPipeError) as captured:
        module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x5FFF,
            absolute_deadline=_future_deadline(),
        )

    _assert_typed_error(
        module,
        captured.value,
        reason="lifecycle_violation",
        stage="create_event",
        owner=None,
        winerror=None,
        completion=None,
    )
    assert len(kernel.CreateEventW.calls) == 256
    assert len(kernel.ConnectNamedPipe.calls) == 256
    assert all(owner.completion is None for owner in owners)


def test_known_null_create_releases_reservation_for_later_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.extend(
        [_EventOutcome(0, ERROR_ACCESS_DENIED) for _ in range(300)]
    )
    kernel.event_handles.append(0x6001)
    kernel.connect_outcomes.append(_Outcome(True))
    module = _load_subject(monkeypatch, kernel)

    for index in range(300):
        with pytest.raises(module.OverlappedPipeError) as captured:
            module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=0x6100 + index,
                absolute_deadline=_future_deadline(),
            )
        assert captured.value.resource_owner is None

    operation = module.OwnedOverlappedPipeOperation.start_connect(
        pipe_handle=0x6FFF,
        absolute_deadline=_future_deadline(),
    )
    assert len(kernel.CreateEventW.calls) == 301
    operation.close()


def test_exact_closed_operations_release_live_registry_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.extend(0x7000 + index for index in range(300))
    kernel.connect_outcomes.extend(_Outcome(True) for _ in range(300))
    module = _load_subject(monkeypatch, kernel)

    for index in range(300):
        operation = module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x8000 + index,
            absolute_deadline=_future_deadline(),
        )
        operation.close()

    assert len(kernel.CreateEventW.calls) == 300
    assert len(kernel.CloseHandle.calls) == 300


@pytest.mark.parametrize("terminal", [False, True], ids=["pending", "drained"])
def test_live_registry_keeps_backing_owner_alive_without_gc_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    terminal: bool,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    if terminal:
        kernel.event_handles.append(0x2401)
        kernel.write_outcomes.append(_Outcome(True))
        kernel.result_outcomes.append(_Outcome(True, byte_count=len(FRAME)))
        operation = module.OwnedOverlappedPipeOperation.start_write(
            pipe_handle=0x3401,
            immutable_frame=FRAME,
            absolute_deadline=_future_deadline(),
        )
    else:
        operation = _start_pending_operation(
            module=module,
            kernel=kernel,
            operation_kind="write",
            pipe_handle=0x3401,
            event_handle=0x2401,
        )
    backing_refs = _ctypes_backing_weakrefs(operation)
    del operation
    gc.collect()

    assert all(reference() is not None for reference in backing_refs)
    assert kernel.CloseHandle.calls == []


def test_transferred_aliases_can_be_dropped_without_freeing_active_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    source = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="write",
        pipe_handle=0x3402,
        event_handle=0x2402,
    )
    backing_refs = _ctypes_backing_weakrefs(source)
    destination = source.transfer()
    del source
    del destination
    gc.collect()

    assert all(reference() is not None for reference in backing_refs)
    assert kernel.CloseHandle.calls == []


def test_normal_closed_releases_ctypes_backing_after_transfer_and_keeps_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.extend([0x2403, 0x2404])
    kernel.connect_outcomes.append(_Outcome(True))
    kernel.write_outcomes.append(_Outcome(True))
    kernel.result_outcomes.append(_Outcome(True, byte_count=len(FRAME)))
    module = _load_subject(monkeypatch, kernel)

    connect_source = module.OwnedOverlappedPipeOperation.start_connect(
        pipe_handle=0x3403,
        absolute_deadline=_future_deadline(),
    )
    write_source = module.OwnedOverlappedPipeOperation.start_write(
        pipe_handle=0x3404,
        immutable_frame=FRAME,
        absolute_deadline=_future_deadline(),
    )
    connect_owner = connect_source.transfer()
    write_owner = write_source.transfer()

    _assert_state(module, connect_source, "TRANSFERRED")
    _assert_state(module, write_source, "TRANSFERRED")
    _assert_state(module, connect_owner, "DRAINED")
    _assert_state(module, write_owner, "DRAINED")
    assert connect_source.recover_transferred_owner() is connect_owner
    assert write_source.recover_transferred_owner() is write_owner

    connect_backing = _ctypes_backing_weakrefs(connect_owner)
    write_backing = _ctypes_backing_weakrefs(write_owner)
    connect_overlapped_address = kernel.overlapped_snapshots[0][1]
    write_overlapped_address = kernel.overlapped_snapshots[1][1]
    write_buffer_address = kernel.write_buffer_addresses[0]

    def backing_contains_address(
        references: tuple[weakref.ReferenceType, ...],
        backing_type: type[ctypes.Structure] | type[ctypes.Array],
        address: int,
    ) -> bool:
        return any(
            isinstance(value := reference(), backing_type)
            and ctypes.addressof(value) == address
            for reference in references
        )

    assert backing_contains_address(
        connect_backing,
        ctypes.Structure,
        connect_overlapped_address,
    )
    assert backing_contains_address(
        write_backing,
        ctypes.Structure,
        write_overlapped_address,
    )
    assert backing_contains_address(
        write_backing,
        ctypes.Array,
        write_buffer_address,
    )

    connect_completion = connect_owner.completion
    write_completion = write_owner.completion
    assert connect_completion is not None
    assert write_completion is not None
    connect_owner.close()
    write_owner.close()
    after_close = _native_call_counts(kernel)

    _assert_state(module, connect_owner, "CLOSED")
    _assert_state(module, write_owner, "CLOSED")
    gc.collect()
    assert all(reference() is None for reference in connect_backing)
    assert all(reference() is None for reference in write_backing)
    assert connect_owner.wait_to_deadline() is connect_completion
    assert connect_owner.cancel_and_drain() is connect_completion
    assert write_owner.wait_to_deadline() is write_completion
    assert write_owner.cancel_and_drain() is write_completion
    connect_owner.close()
    write_owner.close()
    _assert_no_native_calls_after(kernel, after_close)
    assert [_integer(call[0]) for call in kernel.CloseHandle.calls] == [
        0x2403,
        0x2404,
    ]
    assert all(
        borrowed_pipe not in {_integer(call[0]) for call in kernel.CloseHandle.calls}
        for borrowed_pipe in (0x3403, 0x3404)
    )

    _assert_completion(
        module,
        connect_completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
    )
    _assert_completion(
        module,
        write_completion,
        operation="write",
        native_outcome="success",
        bytes_transferred=len(FRAME),
        winerror=None,
    )
    _assert_no_native_calls_after(kernel, after_close)


def test_normal_exception_with_nonexception_cause_preserves_both_and_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(0x2501)
    wrapper = RuntimeError("native wrapper failed")
    interrupt = KeyboardInterrupt("native interruption")
    wrapper.__cause__ = interrupt
    kernel.connect_outcomes.append(
        _Outcome(False, ERROR_IO_PENDING, interrupt=wrapper)
    )
    module = _load_subject(monkeypatch, kernel)

    with pytest.raises(BaseExceptionGroup) as captured:
        module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x3501,
            absolute_deadline=_future_deadline(),
        )

    outer = captured.value
    assert type(outer) is BaseExceptionGroup
    assert len(outer.exceptions) == 3
    root, preserved_wrapper, handoff = outer.exceptions
    assert root is interrupt
    assert preserved_wrapper is wrapper
    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="submit",
        owner=handoff.resource_owner,
        winerror=None,
        completion=None,
    )
    assert handoff.resource_owner is not None
    assert FRAME.hex() not in str(captured.value)
    _assert_errors_and_tracebacks_not_cached(
        module,
        handoff.resource_owner,
        interrupt,
        wrapper,
        outer,
        handoff,
    )


def test_exception_graph_is_cause_first_identity_deduplicated_and_cycle_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(0x2503)
    first_root = KeyboardInterrupt("cause root")
    second_root = SystemExit(17)
    cause_branch = RuntimeError("cause branch")
    duplicate_branch = RuntimeError("duplicate branch")
    context_branch = RuntimeError("context branch")
    wrapper = RuntimeError("native wrapper failed")
    wrapper.__cause__ = cause_branch
    wrapper.__context__ = context_branch
    cause_branch.__cause__ = first_root
    cause_branch.__context__ = duplicate_branch
    duplicate_branch.__cause__ = first_root
    duplicate_branch.__context__ = second_root
    context_branch.__cause__ = second_root
    context_branch.__context__ = wrapper
    kernel.connect_outcomes.append(
        _Outcome(False, ERROR_IO_PENDING, interrupt=wrapper)
    )
    module = _load_subject(monkeypatch, kernel)

    with pytest.raises(BaseExceptionGroup) as captured:
        module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x3503,
            absolute_deadline=_future_deadline(),
        )

    outer = captured.value
    assert type(outer) is BaseExceptionGroup
    assert len(outer.exceptions) == 4
    root_one, root_two, preserved_wrapper, handoff = outer.exceptions
    assert root_one is first_root
    assert root_two is second_root
    assert preserved_wrapper is wrapper
    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="submit",
        owner=handoff.resource_owner,
        winerror=None,
        completion=None,
    )
    assert handoff.resource_owner is not None
    _assert_errors_and_tracebacks_not_cached(
        module,
        handoff.resource_owner,
        first_root,
        second_root,
        cause_branch,
        duplicate_branch,
        context_branch,
        wrapper,
        outer,
        handoff,
    )


def test_multiple_baseexception_leaves_keep_order_and_owner_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(0x2502)
    primary = BaseExceptionGroup(
        "native interruption",
        [KeyboardInterrupt(), SystemExit(9)],
    )
    kernel.connect_outcomes.append(
        _Outcome(False, ERROR_IO_PENDING, interrupt=primary)
    )
    module = _load_subject(monkeypatch, kernel)

    with pytest.raises(BaseExceptionGroup) as captured:
        module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x3502,
            absolute_deadline=_future_deadline(),
        )

    outer = captured.value
    assert type(outer) is BaseExceptionGroup
    assert len(outer.exceptions) == 2
    assert outer.exceptions[0] is primary
    handoff = outer.exceptions[1]
    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="submit",
        owner=handoff.resource_owner,
        winerror=None,
        completion=None,
    )
    assert handoff.resource_owner is not None
    _assert_errors_and_tracebacks_not_cached(
        module,
        handoff.resource_owner,
        *primary.exceptions,
        primary,
        outer,
        handoff,
    )


def test_same_owner_wait_and_transfer_decide_after_cell_lock_is_released(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    delegated = _delegate_concurrency_node_to_subprocess(request)
    if delegated is not None:
        return

    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3601,
        event_handle=0x2601,
    )
    entered = threading.Event()
    release = threading.Event()
    public_transfer_entered = threading.Event()
    wait_result_committed = threading.Event()
    authority_commit_entered = threading.Event()
    waiter_finished = threading.Event()
    mover_finished = threading.Event()
    kernel.blockers["WaitForSingleObject"] = (entered, release)
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xABCD))
    outcomes: dict[str, object] = {}
    operation_type = module.OwnedOverlappedPipeOperation
    real_public_transfer = operation_type.transfer
    real_wait_commit = module._commit_wait_transition
    real_authority_commit = module._commit_authority_transfer

    def entered_public_transfer(owner: object) -> object:
        public_transfer_entered.set()
        return real_public_transfer(owner)

    def recording_wait_commit(
        cell: object,
        immutable_transition: object,
    ) -> object:
        result = real_wait_commit(cell, immutable_transition)
        if (
            _enum_value(immutable_transition.mode) == "RESULT_HANDOFF"
            and _enum_value(result) == "COMMITTED"
        ):
            wait_result_committed.set()
        return result

    def ordered_authority_commit(*args: object, **kwargs: object) -> object:
        assert public_transfer_entered.is_set()
        assert wait_result_committed.is_set()
        authority_commit_entered.set()
        return real_authority_commit(*args, **kwargs)

    monkeypatch.setattr(operation_type, "transfer", entered_public_transfer)
    monkeypatch.setattr(module, "_commit_wait_transition", recording_wait_commit)
    monkeypatch.setattr(
        module,
        "_commit_authority_transfer",
        ordered_authority_commit,
    )

    def waiter() -> None:
        try:
            outcomes["wait"] = operation.wait_to_deadline()
        except BaseException as error:
            outcomes["wait"] = error
        finally:
            waiter_finished.set()

    def mover() -> None:
        try:
            outcomes["transfer"] = operation.transfer()
        except BaseException as error:
            outcomes["transfer"] = error
        finally:
            mover_finished.set()

    wait_thread = threading.Thread(target=waiter)
    move_thread = threading.Thread(target=mover)
    wait_thread.start()
    try:
        assert entered.wait(2.0), "wait thread did not enter WaitForSingleObject"
        move_thread.start()
        assert public_transfer_entered.wait(2.0), "public transfer call did not enter"
    finally:
        release.set()
    assert wait_result_committed.wait(2.0), "wait result handoff did not commit"
    assert authority_commit_entered.wait(2.0), "authority transfer commit did not enter"
    assert waiter_finished.wait(2.0), "wait thread did not finish"
    assert mover_finished.wait(2.0), "transfer thread did not finish"
    wait_thread.join()
    move_thread.join()
    completion = outcomes["wait"]
    destination = outcomes["transfer"]
    assert type(completion) is module.Completion
    assert type(destination) is module.OwnedOverlappedPipeOperation
    _assert_state(module, operation, "TRANSFERRED")
    _assert_state(module, destination, "DRAINED")
    assert operation.recover_transferred_owner() is destination
    assert destination.completion is completion
    assert len(kernel.WaitForSingleObject.calls) == 1
    assert len(kernel.GetOverlappedResult.calls) == 1
    assert kernel.CancelIoEx.calls == []
    destination.close()
    assert [_integer(call[0]) for call in kernel.CloseHandle.calls] == [0x2601]


@pytest.mark.parametrize("concurrent_action", ["cancel", "close"])
def test_same_owner_wait_serializes_cancel_and_close(
    monkeypatch: pytest.MonkeyPatch,
    concurrent_action: str,
    request: pytest.FixtureRequest,
) -> None:
    delegated = _delegate_concurrency_node_to_subprocess(request)
    if delegated is not None:
        return

    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3602,
        event_handle=0x2602,
    )
    wait_entered = threading.Event()
    release_wait = threading.Event()
    action_public_entered = threading.Event()
    wait_public_finished = threading.Event()
    wait_terminal_committed = threading.Event()
    action_native_entered = threading.Event()
    waiter_finished = threading.Event()
    action_finished = threading.Event()
    kernel.blockers["WaitForSingleObject"] = (wait_entered, release_wait)
    if concurrent_action == "cancel":
        kernel.wait_results.extend(
            [
                _WaitOutcome(WAIT_FAILED, ERROR_INVALID_HANDLE),
                WAIT_OBJECT_0,
            ]
        )
        kernel.cancel_outcomes.append(_Outcome(True))
        kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))
    else:
        kernel.wait_results.append(WAIT_OBJECT_0)
        kernel.result_outcomes.append(_Outcome(True, byte_count=0xABCE))
    outcomes: dict[str, object] = {}
    operation_type = module.OwnedOverlappedPipeOperation
    real_public_wait = operation_type.wait_to_deadline
    real_wait_commit = module._commit_wait_transition

    def tracked_public_wait(owner: object) -> object:
        try:
            return real_public_wait(owner)
        finally:
            wait_public_finished.set()

    def recording_wait_commit(
        cell: object,
        immutable_transition: object,
    ) -> object:
        result = real_wait_commit(cell, immutable_transition)
        if (
            _enum_value(immutable_transition.mode) == "RESULT_HANDOFF"
            and _enum_value(result) == "COMMITTED"
        ):
            wait_terminal_committed.set()
        return result

    monkeypatch.setattr(operation_type, "wait_to_deadline", tracked_public_wait)
    monkeypatch.setattr(module, "_commit_wait_transition", recording_wait_commit)

    if concurrent_action == "cancel":
        real_public_action = operation_type.cancel_and_drain

        def tracked_public_action(owner: object) -> object:
            action_public_entered.set()
            return real_public_action(owner)

        def require_finished_wait_before_cancel() -> None:
            assert wait_public_finished.is_set()
            action_native_entered.set()

        monkeypatch.setattr(
            operation_type,
            "cancel_and_drain",
            tracked_public_action,
        )
        kernel.on_native_enter["CancelIoEx"] = require_finished_wait_before_cancel
    else:
        real_public_action = operation_type.close

        def tracked_public_action(owner: object) -> object:
            action_public_entered.set()
            return real_public_action(owner)

        def require_terminal_commit_before_close() -> None:
            assert wait_terminal_committed.is_set()
            action_native_entered.set()

        monkeypatch.setattr(operation_type, "close", tracked_public_action)
        kernel.on_native_enter["CloseHandle"] = require_terminal_commit_before_close

    def waiter() -> None:
        try:
            outcomes["wait"] = operation.wait_to_deadline()
        except BaseException as error:
            outcomes["wait"] = error
        finally:
            waiter_finished.set()

    def concurrent_caller() -> None:
        try:
            if concurrent_action == "cancel":
                outcomes["action"] = operation.cancel_and_drain()
            else:
                outcomes["action"] = operation.close()
        except BaseException as error:
            outcomes["action"] = error
        finally:
            action_finished.set()

    wait_thread = threading.Thread(target=waiter)
    action_thread = threading.Thread(target=concurrent_caller)
    wait_thread.start()
    try:
        assert wait_entered.wait(2.0), "wait thread did not enter WaitForSingleObject"
        action_thread.start()
        assert action_public_entered.wait(2.0), (
            f"public {concurrent_action} call did not enter"
        )
    finally:
        release_wait.set()
    if concurrent_action == "cancel":
        assert wait_public_finished.wait(2.0), (
            "WAIT_FAILED public wait call did not finish"
        )
    else:
        assert wait_terminal_committed.wait(2.0), (
            "successful wait result did not reach terminal commit"
        )
    assert action_native_entered.wait(2.0), (
        f"{concurrent_action} native boundary did not enter"
    )
    assert waiter_finished.wait(2.0), "wait thread did not finish"
    assert action_finished.wait(2.0), f"{concurrent_action} thread did not finish"
    wait_thread.join()
    action_thread.join()

    if concurrent_action == "cancel":
        _assert_typed_error(
            module,
            outcomes["wait"],
            reason="native_failure",
            stage="wait",
            owner=operation,
            winerror=ERROR_INVALID_HANDLE,
            completion=None,
        )
        completion = outcomes["action"]
        _assert_completion(
            module,
            completion,
            operation="connect",
            native_outcome="cancelled",
            bytes_transferred=None,
            winerror=ERROR_OPERATION_ABORTED,
            cancel_outcome="accepted",
            cancel_winerror=None,
        )
        assert len(kernel.WaitForSingleObject.calls) == 2
        assert len(kernel.GetOverlappedResult.calls) == 1
        assert len(kernel.CancelIoEx.calls) == 1
        _assert_state(module, operation, "DRAINED")
        operation.close()
    else:
        completion = outcomes["wait"]
        assert type(completion) is module.Completion
        assert outcomes["action"] is None
        assert len(kernel.WaitForSingleObject.calls) == 1
        assert len(kernel.GetOverlappedResult.calls) == 1
        assert kernel.CancelIoEx.calls == []
        _assert_state(module, operation, "CLOSED")
    assert [_integer(call[0]) for call in kernel.CloseHandle.calls] == [0x2602]


def test_blocked_native_wait_does_not_hold_global_registry_lock(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    delegated = _delegate_concurrency_node_to_subprocess(request)
    if delegated is not None:
        return

    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    blocked = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3603,
        event_handle=0x2603,
    )
    wait_entered = threading.Event()
    release_wait = threading.Event()
    other_reserve_committed = threading.Event()
    other_connect_entered = threading.Event()
    release_other_connect = threading.Event()
    blocked_finished = threading.Event()
    other_finished = threading.Event()
    kernel.blockers["WaitForSingleObject"] = (wait_entered, release_wait)
    kernel.blockers["ConnectNamedPipe"] = (
        other_connect_entered,
        release_other_connect,
    )
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xABCF))
    outcomes: dict[str, object] = {}
    other_thread_identity: int | None = None
    real_registry_commit = module._commit_registry_transition

    def recording_registry_commit(
        registry: object,
        immutable_transition: object,
    ) -> object:
        result = real_registry_commit(registry, immutable_transition)
        if (
            threading.get_ident() == other_thread_identity
            and _enum_value(immutable_transition.mode) == "RESERVE_INSERT"
            and _enum_value(result) == "COMMITTED"
        ):
            other_reserve_committed.set()
        return result

    def require_other_reserve_before_connect() -> None:
        if threading.get_ident() == other_thread_identity:
            assert other_reserve_committed.is_set()
            other_connect_entered.set()

    monkeypatch.setattr(
        module,
        "_commit_registry_transition",
        recording_registry_commit,
    )
    kernel.on_native_enter["ConnectNamedPipe"] = require_other_reserve_before_connect

    def blocked_waiter() -> None:
        try:
            outcomes["blocked"] = blocked.wait_to_deadline()
        except BaseException as error:
            outcomes["blocked"] = error
        finally:
            blocked_finished.set()

    def start_other_owner() -> None:
        nonlocal other_thread_identity
        other_thread_identity = threading.get_ident()
        try:
            outcomes["other"] = _start_pending_operation(
                module=module,
                kernel=kernel,
                operation_kind="connect",
                pipe_handle=0x3604,
                event_handle=0x2604,
            )
        except BaseException as error:
            outcomes["other"] = error
        finally:
            other_finished.set()

    wait_thread = threading.Thread(target=blocked_waiter)
    other_thread = threading.Thread(target=start_other_owner)
    wait_thread.start()
    try:
        assert wait_entered.wait(2.0), "first owner did not enter blocked native wait"
        other_thread.start()
        assert other_reserve_committed.wait(2.0), (
            "second owner did not commit RESERVE_INSERT while first owner waited"
        )
        assert other_connect_entered.wait(2.0), (
            "second owner did not enter ConnectNamedPipe while first owner waited"
        )
    finally:
        release_other_connect.set()
        release_wait.set()
    assert blocked_finished.wait(2.0), "first owner wait thread did not finish"
    assert other_finished.wait(2.0), "second owner start thread did not finish"
    wait_thread.join()
    other_thread.join()
    blocked_completion = outcomes["blocked"]
    other = outcomes["other"]
    assert type(blocked_completion) is module.Completion
    assert type(other) is module.OwnedOverlappedPipeOperation
    _assert_state(module, blocked, "DRAINED")
    _assert_state(module, other, "PENDING")
    assert len(kernel.CreateEventW.calls) == 2
    assert len(kernel.ConnectNamedPipe.calls) == 2
    blocked.close()
    kernel.blockers.pop("WaitForSingleObject")
    kernel.blockers.pop("ConnectNamedPipe")
    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))
    other_completion = other.cancel_and_drain()
    _assert_completion(
        module,
        other_completion,
        operation="connect",
        native_outcome="cancelled",
        bytes_transferred=None,
        winerror=ERROR_OPERATION_ABORTED,
        cancel_outcome="accepted",
        cancel_winerror=None,
    )
    other.close()
    assert sorted(_integer(call[0]) for call in kernel.CloseHandle.calls) == [
        0x2603,
        0x2604,
    ]


def test_concurrent_double_transfer_commits_one_authority_only(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    delegated = _delegate_concurrency_node_to_subprocess(request)
    if delegated is not None:
        return

    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    source = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3605,
        event_handle=0x2605,
    )
    operation_type = module.OwnedOverlappedPipeOperation
    real_public_transfer = operation_type.transfer
    real_transfer = module._commit_authority_transfer
    public_transfer_barrier = threading.Barrier(2)
    first_commit_entered = threading.Event()
    release_first_commit = threading.Event()
    transfers_finished = [threading.Event(), threading.Event()]
    commit_calls = 0
    outcomes: list[object] = []

    def synchronized_public_transfer(owner: object) -> object:
        try:
            public_transfer_barrier.wait(timeout=5.0)
        except threading.BrokenBarrierError as error:
            raise AssertionError(
                "both public transfer calls must enter before authority commit"
            ) from error
        return real_public_transfer(owner)

    def blocking_transfer_commit(*args: object, **kwargs: object) -> object:
        nonlocal commit_calls
        commit_calls += 1
        assert commit_calls == 1
        first_commit_entered.set()
        assert release_first_commit.wait(5.0), (
            "test did not release the first authority commit"
        )
        return real_transfer(*args, **kwargs)

    monkeypatch.setattr(
        operation_type,
        "transfer",
        synchronized_public_transfer,
    )
    monkeypatch.setattr(
        module,
        "_commit_authority_transfer",
        blocking_transfer_commit,
    )

    def run_transfer(index: int) -> None:
        try:
            outcomes.append(source.transfer())
        except BaseException as error:
            outcomes.append(error)
        finally:
            transfers_finished[index].set()

    first_thread = threading.Thread(target=run_transfer, args=(0,))
    second_thread = threading.Thread(target=run_transfer, args=(1,))
    first_thread.start()
    second_thread.start()
    try:
        assert first_commit_entered.wait(2.0), (
            "neither synchronized public transfer reached the authority commit"
        )
    finally:
        release_first_commit.set()
    assert commit_calls == 1
    assert transfers_finished[0].wait(2.0), "first transfer thread did not finish"
    assert transfers_finished[1].wait(2.0), "second transfer thread did not finish"
    first_thread.join()
    second_thread.join()
    destinations = [
        value
        for value in outcomes
        if type(value) is module.OwnedOverlappedPipeOperation
    ]
    rejections = [
        value for value in outcomes if type(value) is module.OverlappedPipeError
    ]
    assert len(destinations) == 1
    assert len(rejections) == 1
    destination = destinations[0]
    _assert_typed_error(
        module,
        rejections[0],
        reason="lifecycle_violation",
        stage="transfer",
        owner=destination,
        winerror=None,
        completion=None,
    )
    assert source.recover_transferred_owner() is destination
    _assert_state(module, source, "TRANSFERRED")
    _assert_state(module, destination, "PENDING")
    assert commit_calls == 1
    assert kernel.CancelIoEx.calls == []
    assert kernel.WaitForSingleObject.calls == []
    assert kernel.GetOverlappedResult.calls == []
    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))
    destination.cancel_and_drain()
    destination.close()


@pytest.mark.parametrize(
    "result_outcome",
    [
        _Outcome(True, byte_count=0xDEAD),
        _Outcome(False, ERROR_OPERATION_ABORTED),
        _Outcome(False, ERROR_ACCESS_DENIED),
        _Outcome(False, ERROR_IO_INCOMPLETE),
    ],
    ids=[
        "gor-true",
        "gor-operation-aborted",
        "gor-native-failure",
        "gor-io-incomplete",
    ],
)
def test_unproven_submit_unknown_terminal_like_gor_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    result_outcome: _Outcome,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_submit_unknown_operation(
        module=module,
        kernel=kernel,
        pipe_handle=0x3B01,
        event_handle=0x2B01,
        absolute_deadline=11.0,
    )
    pointer = kernel.overlapped_snapshots[0][1]
    clock.now = 12.0
    kernel.cancel_outcomes.append(_Outcome(False, ERROR_NOT_FOUND))
    kernel.wait_results.append(WAIT_TIMEOUT)
    kernel.result_outcomes.append(result_outcome)
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.cancel_and_drain()

    _assert_typed_error(
        module,
        captured.value,
        reason="drain_pending",
        stage="drain",
        owner=operation,
        winerror=None,
        completion=None,
    )
    _assert_state(module, operation, "SUBMIT_UNKNOWN")
    assert operation.completion is None
    _assert_native_trace(
        kernel,
        "CancelIoEx",
        "WaitForSingleObject",
        "GetOverlappedResult",
    )
    assert kernel.WaitForSingleObject.calls == [(0x2B01, 0)]
    assert len(kernel.CancelIoEx.calls) == 1
    assert kernel.CloseHandle.calls == []
    result_call = kernel.GetOverlappedResult.calls[0]
    assert _integer(result_call[0]) == 0x3B01
    assert _pointer_address(result_call[1]) == pointer
    assert bool(result_call[3]) is False


def test_unproven_submit_unknown_gor_false_zero_enters_result_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_submit_unknown_operation(
        module=module,
        kernel=kernel,
        pipe_handle=0x3B11,
        event_handle=0x2B11,
        absolute_deadline=11.0,
    )
    pointer = kernel.overlapped_snapshots[0][1]
    clock.now = 12.0
    kernel.cancel_outcomes.append(_Outcome(False, ERROR_NOT_FOUND))
    kernel.wait_results.append(WAIT_TIMEOUT)
    kernel.result_outcomes.append(_Outcome(False, 0))
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.cancel_and_drain()

    _assert_typed_error(
        module,
        captured.value,
        reason="native_failure",
        stage="result",
        owner=operation,
        winerror=None,
        completion=None,
    )
    _assert_state(module, operation, "RESULT_UNKNOWN")
    assert operation.completion is None
    _assert_native_trace(
        kernel,
        "CancelIoEx",
        "WaitForSingleObject",
        "GetOverlappedResult",
    )
    assert kernel.WaitForSingleObject.calls == [(0x2B11, 0)]
    assert len(kernel.CancelIoEx.calls) == 1
    assert kernel.CloseHandle.calls == []
    result_call = kernel.GetOverlappedResult.calls[0]
    assert _integer(result_call[0]) == 0x3B11
    assert _pointer_address(result_call[1]) == pointer
    assert bool(result_call[3]) is False


def test_late_event_signal_after_drain_pending_terminalizes_without_recancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_submit_unknown_operation(
        module=module,
        kernel=kernel,
        pipe_handle=0x3B02,
        event_handle=0x2B02,
        absolute_deadline=11.0,
    )
    pointer = kernel.overlapped_snapshots[0][1]
    clock.now = 12.0
    kernel.cancel_outcomes.append(_Outcome(False, ERROR_NOT_FOUND))
    kernel.wait_results.append(WAIT_TIMEOUT)
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xBEEF))
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as first:
        operation.cancel_and_drain()

    _assert_typed_error(
        module,
        first.value,
        reason="drain_pending",
        stage="drain",
        owner=operation,
        winerror=None,
        completion=None,
    )
    _assert_state(module, operation, "SUBMIT_UNKNOWN")
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xCAFE))

    with pytest.raises(module.OverlappedPipeError) as second:
        operation.cancel_and_drain()

    completion = second.value.completion
    _assert_typed_error(
        module,
        second.value,
        reason="deadline_exceeded",
        stage="drain",
        owner=operation,
        winerror=None,
        completion=completion,
    )
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
        deadline_exceeded=True,
        cancel_outcome="not_found",
        cancel_winerror=ERROR_NOT_FOUND,
    )
    _assert_state(module, operation, "DRAINED")
    _assert_native_trace(
        kernel,
        "CancelIoEx",
        "WaitForSingleObject",
        "GetOverlappedResult",
        "WaitForSingleObject",
        "GetOverlappedResult",
    )
    assert kernel.WaitForSingleObject.calls == [(0x2B02, 0), (0x2B02, 0)]
    assert len(kernel.CancelIoEx.calls) == 1
    assert [
        _pointer_address(call[1]) for call in kernel.GetOverlappedResult.calls
    ] == [pointer, pointer]
    operation.close()


def test_exact_cancel_true_establishes_submission_proof_without_event_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_submit_unknown_operation(
        module=module,
        kernel=kernel,
        pipe_handle=0x3B03,
        event_handle=0x2B03,
        absolute_deadline=11.0,
    )
    pointer = kernel.overlapped_snapshots[0][1]
    clock.now = 12.0
    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xABCD))
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.cancel_and_drain()

    completion = captured.value.completion
    _assert_typed_error(
        module,
        captured.value,
        reason="deadline_exceeded",
        stage="drain",
        owner=operation,
        winerror=None,
        completion=completion,
    )
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
        deadline_exceeded=True,
        cancel_outcome="accepted",
        cancel_winerror=None,
    )
    _assert_state(module, operation, "DRAINED")
    _assert_native_trace(kernel, "CancelIoEx", "GetOverlappedResult")
    assert kernel.WaitForSingleObject.calls == []
    assert len(kernel.CancelIoEx.calls) == 1
    result_call = kernel.GetOverlappedResult.calls[0]
    assert _integer(result_call[0]) == 0x3B03
    assert _pointer_address(result_call[1]) == pointer
    assert bool(result_call[3]) is False
    operation.close()


def test_gor_false_zero_enters_result_unknown_and_reobserves_same_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3B04,
        event_handle=0x2B04,
        absolute_deadline=100.0,
    )
    pointer = kernel.overlapped_snapshots[0][1]
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(False, 0))
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.wait_to_deadline()

    _assert_typed_error(
        module,
        captured.value,
        reason="native_failure",
        stage="result",
        owner=operation,
        winerror=None,
        completion=None,
    )
    _assert_state(module, operation, "RESULT_UNKNOWN")
    kernel.result_outcomes.append(_Outcome(True, byte_count=0x1234))

    completion = operation.wait_to_deadline()

    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
    )
    _assert_state(module, operation, "DRAINED")
    _assert_native_trace(
        kernel,
        "WaitForSingleObject",
        "GetOverlappedResult",
        "GetOverlappedResult",
    )
    assert len(kernel.WaitForSingleObject.calls) == 1
    assert kernel.CancelIoEx.calls == []
    assert [
        (_integer(call[0]), _pointer_address(call[1]), bool(call[3]))
        for call in kernel.GetOverlappedResult.calls
    ] == [(0x3B04, pointer, False), (0x3B04, pointer, False)]
    operation.close()


def test_result_required_start_incomplete_returns_pending_without_second_gor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(0x2C01)
    kernel.write_outcomes.append(_Outcome(True))
    kernel.result_outcomes.append(_Outcome(False, ERROR_IO_INCOMPLETE))
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)

    operation = module.OwnedOverlappedPipeOperation.start_write(
        pipe_handle=0x3C01,
        immutable_frame=FRAME,
        absolute_deadline=100.0,
    )

    pointer = kernel.overlapped_snapshots[0][1]
    _assert_state(module, operation, "PENDING")
    assert operation.completion is None
    _assert_native_trace(
        kernel,
        "CreateEventW",
        "WriteFile",
        "GetOverlappedResult",
    )
    assert len(kernel.GetOverlappedResult.calls) == 1
    assert kernel.WaitForSingleObject.calls == []
    assert kernel.CancelIoEx.calls == []
    assert kernel.write_count_pointers == [0]
    result_call = kernel.GetOverlappedResult.calls[0]
    assert _integer(result_call[0]) == 0x3C01
    assert _pointer_address(result_call[1]) == pointer
    assert bool(result_call[3]) is False


def test_result_required_gor_incomplete_clock_interrupt_persists_pending_then_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation, clock = _start_result_required_after_clock_interrupt(
        monkeypatch=monkeypatch,
        module=module,
        kernel=kernel,
        pipe_handle=0x3C02,
        event_handle=0x2C02,
        absolute_deadline=100.0,
    )
    pointer = kernel.overlapped_snapshots[0][1]
    kernel.result_outcomes.append(_Outcome(False, ERROR_IO_INCOMPLETE))
    kernel.native_trace.clear()
    before_interrupt = _native_call_counts(kernel)
    clock.arm_next(10.0, KeyboardInterrupt())

    with pytest.raises(BaseExceptionGroup) as captured:
        operation.wait_to_deadline()

    _, handoff = _assert_interrupt_group(
        module,
        captured.value,
        KeyboardInterrupt,
    )
    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="result",
        owner=operation,
        winerror=None,
        completion=None,
    )
    _assert_state(module, operation, "PENDING")
    assert operation.completion is None
    _assert_native_trace(kernel, "GetOverlappedResult")
    assert len(kernel.GetOverlappedResult.calls) == (
        before_interrupt["GetOverlappedResult"] + 1
    )
    assert len(kernel.WaitForSingleObject.calls) == before_interrupt[
        "WaitForSingleObject"
    ]
    assert len(kernel.CancelIoEx.calls) == before_interrupt["CancelIoEx"]
    assert len(kernel.WriteFile.calls) == before_interrupt["WriteFile"]
    assert not clock.steps
    first_result_call = kernel.GetOverlappedResult.calls[-1]
    assert _integer(first_result_call[0]) == 0x3C02
    assert _pointer_address(first_result_call[1]) == pointer
    assert bool(first_result_call[3]) is False

    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=len(FRAME)))
    kernel.native_trace.clear()
    before_resume = _native_call_counts(kernel)

    completion = operation.wait_to_deadline()

    _assert_completion(
        module,
        completion,
        operation="write",
        native_outcome="success",
        bytes_transferred=len(FRAME),
        winerror=None,
    )
    _assert_state(module, operation, "DRAINED")
    _assert_native_trace(kernel, "WaitForSingleObject", "GetOverlappedResult")
    assert kernel.WaitForSingleObject.calls == [(0x2C02, MAX_WAIT_SLICE_MS)]
    assert kernel.CancelIoEx.calls == []
    assert len(kernel.WriteFile.calls) == 1
    assert len(kernel.GetOverlappedResult.calls) == (
        before_resume["GetOverlappedResult"] + 1
    )
    assert [
        (_integer(call[0]), _pointer_address(call[1]), bool(call[3]))
        for call in kernel.GetOverlappedResult.calls[-2:]
    ] == [(0x3C02, pointer, False), (0x3C02, pointer, False)]
    operation.close()


def test_result_required_cancel_resume_incomplete_redispatches_to_single_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation, _ = _start_result_required_after_clock_interrupt(
        monkeypatch=monkeypatch,
        module=module,
        kernel=kernel,
        pipe_handle=0x3C03,
        event_handle=0x2C03,
        absolute_deadline=100.0,
    )
    pointer = kernel.overlapped_snapshots[0][1]
    kernel.result_outcomes.extend(
        [
            _Outcome(False, ERROR_IO_INCOMPLETE),
            _Outcome(False, ERROR_OPERATION_ABORTED),
        ]
    )
    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.native_trace.clear()

    completion = operation.cancel_and_drain()

    _assert_completion(
        module,
        completion,
        operation="write",
        native_outcome="cancelled",
        bytes_transferred=None,
        winerror=ERROR_OPERATION_ABORTED,
        cancel_outcome="accepted",
        cancel_winerror=None,
    )
    _assert_state(module, operation, "DRAINED")
    _assert_native_trace(
        kernel,
        "GetOverlappedResult",
        "CancelIoEx",
        "WaitForSingleObject",
        "GetOverlappedResult",
    )
    assert len(kernel.CancelIoEx.calls) == 1
    assert len(kernel.WriteFile.calls) == 1
    assert [
        (_integer(call[0]), _pointer_address(call[1]), bool(call[3]))
        for call in kernel.GetOverlappedResult.calls
    ] == [(0x3C03, pointer, False), (0x3C03, pointer, False)]
    operation.close()


def test_result_required_transfer_destination_performs_exactly_one_gor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    source, _ = _start_result_required_after_clock_interrupt(
        monkeypatch=monkeypatch,
        module=module,
        kernel=kernel,
        pipe_handle=0x3C04,
        event_handle=0x2C04,
        absolute_deadline=100.0,
    )
    pointer = kernel.overlapped_snapshots[0][1]
    before_transfer = _native_call_counts(kernel)
    transfer_trace = tuple(kernel.native_trace)

    destination = source.transfer()

    _assert_no_native_calls_after(kernel, before_transfer)
    assert tuple(kernel.native_trace) == transfer_trace
    assert len(kernel.CloseHandle.calls) == before_transfer["CloseHandle"]
    _assert_state(module, source, "TRANSFERRED")
    _assert_state(module, destination, "RESULT_REQUIRED")
    assert source.recover_transferred_owner() is destination
    kernel.native_trace.clear()
    kernel.result_outcomes.append(_Outcome(True, byte_count=len(FRAME)))
    before = _native_call_counts(kernel)

    completion = destination.wait_to_deadline()

    _assert_completion(
        module,
        completion,
        operation="write",
        native_outcome="success",
        bytes_transferred=len(FRAME),
        winerror=None,
    )
    _assert_state(module, destination, "DRAINED")
    _assert_native_trace(kernel, "GetOverlappedResult")
    assert len(kernel.GetOverlappedResult.calls) == before["GetOverlappedResult"] + 1
    assert len(kernel.WaitForSingleObject.calls) == before["WaitForSingleObject"]
    assert len(kernel.CancelIoEx.calls) == before["CancelIoEx"]
    assert len(kernel.WriteFile.calls) == before["WriteFile"]
    result_call = kernel.GetOverlappedResult.calls[-1]
    assert _integer(result_call[0]) == 0x3C04
    assert _pointer_address(result_call[1]) == pointer
    assert bool(result_call[3]) is False
    destination.close()


@pytest.mark.parametrize(
    "resume_now",
    [10.0, 20.0],
    ids=["resume-before-deadline", "resume-at-deadline"],
)
def test_completed_transfer_destination_classifies_without_native_reobservation(
    monkeypatch: pytest.MonkeyPatch,
    resume_now: float,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ScriptedClock(10.0)
    clock.arm_next(10.0, 10.0, 10.0, KeyboardInterrupt())
    monkeypatch.setattr(module, "_monotonic", clock)
    kernel.event_handles.append(0x2C05)
    kernel.write_outcomes.append(_Outcome(True))
    kernel.result_outcomes.append(_Outcome(True, byte_count=len(FRAME)))

    with pytest.raises(BaseExceptionGroup) as captured:
        module.OwnedOverlappedPipeOperation.start_write(
            pipe_handle=0x3C05,
            immutable_frame=FRAME,
            absolute_deadline=20.0,
        )

    _, handoff = _assert_interrupt_group(
        module,
        captured.value,
        KeyboardInterrupt,
    )
    owner = handoff.resource_owner
    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="result",
        owner=owner,
        winerror=None,
        completion=None,
    )
    _assert_state(module, owner, "COMPLETED")
    assert owner.completion is None
    assert not clock.steps
    _assert_native_trace(
        kernel,
        "CreateEventW",
        "WriteFile",
        "GetOverlappedResult",
    )
    pointer = kernel.overlapped_snapshots[0][1]
    result_call = kernel.GetOverlappedResult.calls[0]
    assert _integer(result_call[0]) == 0x3C05
    assert _pointer_address(result_call[1]) == pointer
    assert bool(result_call[3]) is False

    before_transfer = _native_call_counts(kernel)
    transfer_trace = tuple(kernel.native_trace)
    destination = owner.transfer()

    _assert_no_native_calls_after(kernel, before_transfer)
    assert tuple(kernel.native_trace) == transfer_trace
    assert len(kernel.CloseHandle.calls) == before_transfer["CloseHandle"]
    _assert_state(module, owner, "TRANSFERRED")
    assert owner.recover_transferred_owner() is destination
    _assert_state(module, destination, "COMPLETED")
    clock.now = resume_now
    kernel.native_trace.clear()
    before = _native_call_counts(kernel)

    if resume_now < 20.0:
        completion = destination.cancel_and_drain()
    else:
        with pytest.raises(module.OverlappedPipeError) as resumed:
            destination.cancel_and_drain()
        completion = resumed.value.completion
        _assert_typed_error(
            module,
            resumed.value,
            reason="deadline_exceeded",
            stage="submit",
            owner=destination,
            winerror=None,
            completion=completion,
        )

    _assert_completion(
        module,
        completion,
        operation="write",
        native_outcome="success",
        bytes_transferred=len(FRAME),
        winerror=None,
        deadline_exceeded=resume_now >= 20.0,
    )
    _assert_state(module, destination, "DRAINED")
    assert destination.completion is completion
    _assert_native_trace(kernel)
    _assert_no_native_calls_after(kernel, before)
    destination.close()


@pytest.mark.parametrize("interrupt_type", _INTERRUPTION_TYPES)
@pytest.mark.parametrize(
    "initial_state",
    ["PENDING", "SUBMIT_UNKNOWN", "RESULT_UNKNOWN"],
    ids=["pending", "submit-unknown", "result-unknown"],
)
@pytest.mark.parametrize(
    ("cancel_mode", "cancel_native", "cancel_outcome", "cancel_winerror"),
    [
        ("accepted", _Outcome(True), "accepted", None),
        ("not-found", _Outcome(False, ERROR_NOT_FOUND), "not_found", ERROR_NOT_FOUND),
        ("failed", _Outcome(False, ERROR_ACCESS_DENIED), "failed", ERROR_ACCESS_DENIED),
        ("unknown", _Outcome(False, 0), "unknown", None),
    ],
    ids=["accepted", "not-found", "failed", "unknown"],
)
def test_post_cancel_clock_interrupt_preserves_resume_and_carried_origin(
    monkeypatch: pytest.MonkeyPatch,
    interrupt_type: type[BaseException],
    initial_state: str,
    cancel_mode: str,
    cancel_native: _Outcome,
    cancel_outcome: str,
    cancel_winerror: int | None,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation, clock = _start_cancel_matrix_operation(
        monkeypatch=monkeypatch,
        module=module,
        kernel=kernel,
        state=initial_state,
    )
    pointer = kernel.overlapped_snapshots[0][1]
    original_commit = module._commit_cancel_snapshot
    original = interrupt_type(
        f"post-cancel-clock-{initial_state}-{cancel_mode}"
    )
    commit_calls = 0

    def commit_then_interrupt(*args: object, **kwargs: object) -> object:
        nonlocal commit_calls
        commit_calls += 1
        result = original_commit(*args, **kwargs)
        clock.arm_next(original)
        return result

    monkeypatch.setattr(module, "_commit_cancel_snapshot", commit_then_interrupt)
    kernel.cancel_outcomes.append(cancel_native)
    kernel.native_trace.clear()
    before = _native_call_counts(kernel)

    primary, handoff = _capture_outward_interruption(
        module,
        original,
        operation.cancel_and_drain,
    )
    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="cancel",
        owner=operation,
        winerror=None,
        completion=None,
    )
    expected_stable_state = (
        "PENDING"
        if cancel_mode == "accepted"
        else "CANCEL_UNKNOWN"
        if cancel_mode == "unknown"
        else initial_state
    )
    resume_state = "PENDING" if cancel_mode == "accepted" else initial_state
    has_carried_result_origin = initial_state == "RESULT_UNKNOWN"
    _assert_state(module, operation, expected_stable_state)
    assert operation.completion is None
    _assert_native_trace(kernel, "CancelIoEx")
    assert len(kernel.CancelIoEx.calls) == before["CancelIoEx"] + 1
    assert len(kernel.WaitForSingleObject.calls) == before["WaitForSingleObject"]
    assert len(kernel.GetOverlappedResult.calls) == before["GetOverlappedResult"]
    assert len(kernel.CloseHandle.calls) == before["CloseHandle"]
    clock.calls.clear()
    monkeypatch.setattr(module, "_commit_cancel_snapshot", original_commit)
    _assert_errors_and_tracebacks_not_cached(
        module,
        operation,
        primary,
        handoff,
    )

    if not has_carried_result_origin and resume_state in {"PENDING", "SUBMIT_UNKNOWN"}:
        kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xD00D))
    if has_carried_result_origin:
        kernel.on_native_return["GetOverlappedResult"] = lambda: setattr(
            clock,
            "now",
            100.0,
        )

    if has_carried_result_origin:
        with pytest.raises(module.OverlappedPipeError) as resumed:
            operation.cancel_and_drain()
        completion = resumed.value.completion
        _assert_typed_error(
            module,
            resumed.value,
            reason="deadline_exceeded",
            stage="wait",
            owner=operation,
            winerror=None,
            completion=completion,
        )
    else:
        completion = operation.cancel_and_drain()

    expected_resume_trace = (
        ["GetOverlappedResult"]
        if has_carried_result_origin or resume_state == "RESULT_UNKNOWN"
        else ["WaitForSingleObject", "GetOverlappedResult"]
    )
    _assert_native_trace(kernel, "CancelIoEx", *expected_resume_trace)
    if not has_carried_result_origin and resume_state == "PENDING":
        assert kernel.WaitForSingleObject.calls[-1] == (0x2A01, MAX_WAIT_SLICE_MS)
    elif not has_carried_result_origin and resume_state == "SUBMIT_UNKNOWN":
        assert kernel.WaitForSingleObject.calls[-1] == (0x2A01, 0)
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
        deadline_exceeded=has_carried_result_origin,
        cancel_outcome=cancel_outcome,
        cancel_winerror=cancel_winerror,
    )
    _assert_state(module, operation, "DRAINED")
    assert commit_calls == 1
    assert len(kernel.CancelIoEx.calls) == 1
    result_call = kernel.GetOverlappedResult.calls[-1]
    assert _integer(result_call[0]) == 0x3A01
    assert _pointer_address(result_call[1]) == pointer
    assert bool(result_call[3]) is False
    operation.close()


@pytest.mark.parametrize("interrupt_type", _INTERRUPTION_TYPES)
@pytest.mark.parametrize(
    "commit_boundary",
    ["before", "after"],
    ids=["before-commit", "after-commit"],
)
def test_cancel_snapshot_commit_pre_and_post_interruption_are_monotonic(
    monkeypatch: pytest.MonkeyPatch,
    commit_boundary: str,
    interrupt_type: type[BaseException],
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_submit_unknown_operation(
        module=module,
        kernel=kernel,
        pipe_handle=0x3D01,
        event_handle=0x2D01,
        absolute_deadline=100.0,
    )
    pointer = kernel.overlapped_snapshots[0][1]
    original_commit = module._commit_cancel_snapshot
    original = interrupt_type(f"cancel-snapshot-{commit_boundary}")
    commit_calls = 0

    def interrupting_commit(*args: object, **kwargs: object) -> object:
        nonlocal commit_calls
        commit_calls += 1
        if commit_boundary == "before":
            raise original
        result = original_commit(*args, **kwargs)
        raise original

    monkeypatch.setattr(module, "_commit_cancel_snapshot", interrupting_commit)
    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.native_trace.clear()
    before = _native_call_counts(kernel)

    primary, handoff = _capture_outward_interruption(
        module,
        original,
        operation.cancel_and_drain,
    )
    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="cancel",
        owner=operation,
        winerror=None,
        completion=None,
    )
    expected_state = "CANCEL_UNKNOWN" if commit_boundary == "before" else "PENDING"
    _assert_state(module, operation, expected_state)
    assert operation.completion is None
    _assert_native_trace(kernel, "CancelIoEx")
    assert len(kernel.CancelIoEx.calls) == before["CancelIoEx"] + 1
    assert len(kernel.WaitForSingleObject.calls) == before["WaitForSingleObject"]
    assert len(kernel.GetOverlappedResult.calls) == before["GetOverlappedResult"]
    assert len(kernel.CloseHandle.calls) == before["CloseHandle"]

    monkeypatch.setattr(
        module,
        "_commit_cancel_snapshot",
        original_commit,
    )
    _assert_errors_and_tracebacks_not_cached(
        module,
        operation,
        primary,
        handoff,
    )
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xFACE))

    completion = operation.cancel_and_drain()

    _assert_native_trace(
        kernel,
        "CancelIoEx",
        "WaitForSingleObject",
        "GetOverlappedResult",
    )
    expected_wait_ms = 0 if commit_boundary == "before" else MAX_WAIT_SLICE_MS
    assert kernel.WaitForSingleObject.calls[-1] == (0x2D01, expected_wait_ms)
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
        cancel_outcome="unknown" if commit_boundary == "before" else "accepted",
        cancel_winerror=None,
    )
    _assert_state(module, operation, "DRAINED")
    assert commit_calls == 1
    assert len(kernel.CancelIoEx.calls) == 1
    result_call = kernel.GetOverlappedResult.calls[-1]
    assert _integer(result_call[0]) == 0x3D01
    assert _pointer_address(result_call[1]) == pointer
    assert bool(result_call[3]) is False
    operation.close()


@pytest.mark.parametrize(
    "initial_state",
    ["PENDING", "SUBMIT_UNKNOWN", "RESULT_UNKNOWN"],
    ids=["pending", "submit-unknown", "result-unknown"],
)
@pytest.mark.parametrize(
    ("cancel_native", "cancel_outcome", "cancel_winerror"),
    [
        pytest.param(_Outcome(True), "accepted", None, id="accepted"),
        pytest.param(
            _Outcome(False, ERROR_NOT_FOUND),
            "not_found",
            ERROR_NOT_FOUND,
            id="not-found",
        ),
        pytest.param(
            _Outcome(False, ERROR_ACCESS_DENIED),
            "failed",
            ERROR_ACCESS_DENIED,
            id="failed",
        ),
        pytest.param(_Outcome(False, 0), "unknown", None, id="unknown"),
    ],
)
def test_cancel_matrix_uses_one_snapshot_cell_and_contiguous_transition_chain(
    monkeypatch: pytest.MonkeyPatch,
    initial_state: str,
    cancel_native: _Outcome,
    cancel_outcome: str,
    cancel_winerror: int | None,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation, _clock = _start_cancel_matrix_operation(
        monkeypatch=monkeypatch,
        module=module,
        kernel=kernel,
        state=initial_state,
    )
    pointer = kernel.overlapped_snapshots[0][1]
    real_cancel_commit = module._commit_cancel_snapshot
    real_wait_transition = module._commit_wait_transition
    cancel_cells: list[object] = []
    cancel_targets: list[object] = []
    transition_cells: list[object] = []
    transitions: list[object] = []

    def capture_cancel_target(
        cell: object,
        immutable_snapshot: object,
    ) -> object:
        cancel_cells.append(cell)
        cancel_targets.append(immutable_snapshot)
        return real_cancel_commit(cell, immutable_snapshot)

    def capture_transition_chain(
        cell: object,
        immutable_transition: object,
    ) -> object:
        transition_cells.append(cell)
        transitions.append(immutable_transition)
        return real_wait_transition(cell, immutable_transition)

    monkeypatch.setattr(module, "_commit_cancel_snapshot", capture_cancel_target)
    monkeypatch.setattr(
        module,
        "_commit_wait_transition",
        capture_transition_chain,
    )
    kernel.cancel_outcomes.append(cancel_native)

    if initial_state == "RESULT_UNKNOWN":
        kernel.result_outcomes.append(
            _Outcome(False, ERROR_IO_INCOMPLETE)
        )
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xD002))
    kernel.native_trace.clear()

    completion = operation.cancel_and_drain()

    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
        cancel_outcome=cancel_outcome,
        cancel_winerror=cancel_winerror,
    )
    assert len(cancel_targets) == 1
    assert len(cancel_cells) == 1
    assert transitions
    assert all(
        transition_cell is cancel_cells[0]
        for transition_cell in transition_cells
    )
    assert cancel_targets[0] is transitions[0].expected_snapshot
    assert all(
        previous.target_snapshot is following.expected_snapshot
        for previous, following in zip(
            transitions,
            transitions[1:],
            strict=False,
        )
    )

    transition_modes = [
        str(getattr(transition.mode, "value", transition.mode))
        for transition in transitions
    ]
    if initial_state == "RESULT_UNKNOWN":
        assert transition_modes == [
            "RESULT_HANDOFF",
            "OBSERVATION_COMMIT",
            "OBJECT_DISPATCH",
            "RESULT_HANDOFF",
        ]
        _assert_native_trace(
            kernel,
            "CancelIoEx",
            "GetOverlappedResult",
            "WaitForSingleObject",
            "GetOverlappedResult",
        )
    else:
        assert transition_modes == [
            "OBSERVATION_COMMIT",
            "OBJECT_DISPATCH",
            "RESULT_HANDOFF",
        ]
        _assert_native_trace(
            kernel,
            "CancelIoEx",
            "WaitForSingleObject",
            "GetOverlappedResult",
        )

    assert len(kernel.CancelIoEx.calls) == 1
    assert _pointer_address(kernel.CancelIoEx.calls[0][1]) == pointer
    expected_result_calls = 2 if initial_state == "RESULT_UNKNOWN" else 1
    for result_call in kernel.GetOverlappedResult.calls[-expected_result_calls:]:
        assert _pointer_address(result_call[1]) == pointer
    _assert_state(module, operation, "DRAINED")
    operation.close()


@pytest.mark.parametrize("operation_kind", ["connect", "write"])
@pytest.mark.parametrize(
    "absolute_deadline",
    [9.0, 10.0],
    ids=["past", "equal"],
)
def test_start_deadline_equal_or_past_rejects_before_any_side_effect(
    monkeypatch: pytest.MonkeyPatch,
    operation_kind: str,
    absolute_deadline: float,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    real_registry_commit = module._commit_registry_transition
    registry_commits: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def recording_registry_commit(*args: object, **kwargs: object) -> object:
        registry_commits.append((args, kwargs))
        return real_registry_commit(*args, **kwargs)

    monkeypatch.setattr(
        module,
        "_commit_registry_transition",
        recording_registry_commit,
    )

    with pytest.raises(module.OverlappedPipeError) as captured:
        if operation_kind == "connect":
            module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=0x3E01,
                absolute_deadline=absolute_deadline,
            )
        else:
            module.OwnedOverlappedPipeOperation.start_write(
                pipe_handle=0x3E01,
                immutable_frame=FRAME,
                absolute_deadline=absolute_deadline,
            )

    _assert_typed_error(
        module,
        captured.value,
        reason="deadline_exceeded",
        stage="submit",
        owner=None,
        winerror=None,
        completion=None,
    )
    assert registry_commits == []
    _assert_native_trace(kernel)
    assert _native_call_counts(kernel) == {
        name: 0 for name in _native_call_counts(kernel)
    }


def test_presubmit_deadline_after_event_keeps_new_closable_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    kernel.event_handles.append(0x2E01)
    kernel.on_native_return["CreateEventW"] = lambda: setattr(
        clock, "now", 20.0
    )

    with pytest.raises(module.OverlappedPipeError) as captured:
        module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x3E02,
            absolute_deadline=20.0,
        )

    owner = captured.value.resource_owner
    assert owner is not None
    _assert_typed_error(
        module,
        captured.value,
        reason="deadline_exceeded",
        stage="submit",
        owner=owner,
        winerror=None,
        completion=None,
    )
    _assert_state(module, owner, "NEW")
    assert owner.completion is None
    _assert_native_trace(kernel, "CreateEventW")
    assert kernel.ConnectNamedPipe.calls == []
    assert kernel.CancelIoEx.calls == []
    assert kernel.GetOverlappedResult.calls == []
    before = _native_call_counts(kernel)

    with pytest.raises(module.OverlappedPipeError) as invalid_wait:
        owner.wait_to_deadline()

    _assert_typed_error(
        module,
        invalid_wait.value,
        reason="lifecycle_violation",
        stage="wait",
        owner=owner,
        winerror=None,
        completion=None,
    )
    _assert_no_native_calls_after(kernel, before)

    with pytest.raises(module.OverlappedPipeError) as invalid_cancel:
        owner.cancel_and_drain()

    _assert_typed_error(
        module,
        invalid_cancel.value,
        reason="lifecycle_violation",
        stage="drain",
        owner=owner,
        winerror=None,
        completion=None,
    )
    _assert_no_native_calls_after(kernel, before)
    owner.close()
    _assert_state(module, owner, "CLOSED")
    assert [_integer(call[0]) for call in kernel.CloseHandle.calls] == [0x2E01]
    after_close = _native_call_counts(kernel)
    for action, stage in (
        (owner.wait_to_deadline, "wait"),
        (owner.cancel_and_drain, "drain"),
        (owner.transfer, "transfer"),
    ):
        with pytest.raises(module.OverlappedPipeError) as rejected:
            action()
        _assert_typed_error(
            module,
            rejected.value,
            reason="lifecycle_violation",
            stage=stage,
            owner=owner,
            winerror=None,
            completion=None,
        )
    _assert_no_native_calls_after(kernel, after_close)


@pytest.mark.parametrize(
    ("remaining", "expected_wait_ms"),
    [
        (0.0001, 1),
        (0.2, 200),
        (0.2491, 250),
        (0.25, 250),
        (sys.float_info.max, 250),
    ],
    ids=["minimum-one-ms", "two-hundred-ms", "ceil-to-250", "exact-250", "max-finite"],
)
def test_wait_slice_ceil_clamp_and_max_finite(
    monkeypatch: pytest.MonkeyPatch,
    remaining: float,
    expected_wait_ms: int,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    absolute_deadline = (
        sys.float_info.max if remaining == sys.float_info.max else 10.0 + remaining
    )
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3E03,
        event_handle=0x2E03,
        absolute_deadline=absolute_deadline,
    )
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xCAFE))
    kernel.native_trace.clear()

    completion = operation.wait_to_deadline()

    assert kernel.WaitForSingleObject.calls == [(0x2E03, expected_wait_ms)]
    assert expected_wait_ms != 0xFFFFFFFF
    _assert_native_trace(kernel, "WaitForSingleObject", "GetOverlappedResult")
    assert kernel.CancelIoEx.calls == []
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
    )
    operation.close()


def test_spurious_early_timeouts_resample_remaining_without_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ScriptedClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3E04,
        event_handle=0x2E04,
        absolute_deadline=10.25,
    )
    start_clock_calls = len(clock.calls)
    wait_entry_clock_calls: list[int] = []
    kernel.on_native_enter["WaitForSingleObject"] = lambda: (
        wait_entry_clock_calls.append(len(clock.calls))
    )
    early_times = iter([10.0625, 10.125])
    kernel.on_wait = lambda _milliseconds: setattr(clock, "now", next(early_times))
    kernel.wait_results.extend(
        [
            _WaitOutcome(WAIT_TIMEOUT, ERROR_ACCESS_DENIED),
            _WaitOutcome(WAIT_TIMEOUT, ERROR_ACCESS_DENIED),
            _WaitOutcome(WAIT_OBJECT_0, ERROR_ACCESS_DENIED),
        ]
    )
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xBEEF))
    kernel.native_trace.clear()

    def forbid_stale_last_error_read() -> int:
        raise AssertionError("successful wait/result paths must ignore stale last-error")

    monkeypatch.setattr(ctypes, "get_last_error", forbid_stale_last_error_read)

    completion = operation.wait_to_deadline()

    assert [_integer(call[1]) for call in kernel.WaitForSingleObject.calls] == [
        250,
        188,
        125,
    ]
    assert wait_entry_clock_calls == [
        start_clock_calls + 1,
        start_clock_calls + 3,
        start_clock_calls + 5,
    ]
    _assert_native_trace(
        kernel,
        "WaitForSingleObject",
        "WaitForSingleObject",
        "WaitForSingleObject",
        "GetOverlappedResult",
    )
    assert kernel.CancelIoEx.calls == []
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
    )
    operation.close()


def test_post_submit_deadline_sample_has_submit_origin_and_terminal_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    kernel.event_handles.append(0x2E05)
    kernel.connect_outcomes.append(_Outcome(False, ERROR_IO_PENDING))
    kernel.cancel_outcomes.append(_Outcome(False, ERROR_NOT_FOUND))
    kernel.result_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
    kernel.on_native_return["ConnectNamedPipe"] = lambda: setattr(
        clock, "now", 20.0
    )

    with pytest.raises(module.OverlappedPipeError) as captured:
        module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x3E05,
            absolute_deadline=20.0,
        )

    owner = captured.value.resource_owner
    completion = captured.value.completion
    _assert_typed_error(
        module,
        captured.value,
        reason="deadline_exceeded",
        stage="submit",
        owner=owner,
        winerror=ERROR_ACCESS_DENIED,
        completion=completion,
    )
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="failure",
        bytes_transferred=None,
        winerror=ERROR_ACCESS_DENIED,
        deadline_exceeded=True,
        cancel_outcome="not_found",
        cancel_winerror=ERROR_NOT_FOUND,
    )
    _assert_state(module, owner, "DRAINED")
    _assert_native_trace(
        kernel,
        "CreateEventW",
        "ConnectNamedPipe",
        "CancelIoEx",
        "GetOverlappedResult",
    )
    assert kernel.WaitForSingleObject.calls == []
    owner.close()


@pytest.mark.parametrize(
    (
        "result_outcome",
        "expected_native_outcome",
        "expected_bytes",
        "expected_winerror",
    ),
    [
        (_Outcome(True, byte_count=len(FRAME)), "success", len(FRAME), None),
        (_Outcome(True, byte_count=len(FRAME) - 1), "success", len(FRAME) - 1, None),
        (_Outcome(False, ERROR_ACCESS_DENIED), "failure", None, ERROR_ACCESS_DENIED),
        (
            _Outcome(False, ERROR_OPERATION_ABORTED),
            "cancelled",
            None,
            ERROR_OPERATION_ABORTED,
        ),
    ],
    ids=["success-exact", "success-bad-byte-count", "failure", "cancelled"],
)
def test_post_wait_deadline_sample_has_wait_origin_and_terminal_priority(
    monkeypatch: pytest.MonkeyPatch,
    result_outcome: _Outcome,
    expected_native_outcome: str,
    expected_bytes: int | None,
    expected_winerror: int | None,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ScriptedClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="write",
        pipe_handle=0x3E06,
        event_handle=0x2E06,
        absolute_deadline=20.0,
    )
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(result_outcome)
    wait_return_clock_calls: list[int] = []

    def mark_wait_return() -> None:
        wait_return_clock_calls.append(len(clock.calls))
        clock.now = 20.0

    def require_post_wait_sample_before_result() -> None:
        assert wait_return_clock_calls
        assert len(clock.calls) == wait_return_clock_calls[0] + 1

    kernel.on_native_return["WaitForSingleObject"] = mark_wait_return
    kernel.on_native_enter[
        "GetOverlappedResult"
    ] = require_post_wait_sample_before_result
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.wait_to_deadline()

    completion = captured.value.completion
    _assert_typed_error(
        module,
        captured.value,
        reason="deadline_exceeded",
        stage="wait",
        owner=operation,
        winerror=expected_winerror,
        completion=completion,
    )
    _assert_completion(
        module,
        completion,
        operation="write",
        native_outcome=expected_native_outcome,
        bytes_transferred=expected_bytes,
        winerror=expected_winerror,
        deadline_exceeded=True,
    )
    _assert_state(module, operation, "DRAINED")
    _assert_native_trace(kernel, "WaitForSingleObject", "GetOverlappedResult")
    assert len(kernel.WaitForSingleObject.calls) == 1
    assert len(kernel.GetOverlappedResult.calls) == 1
    assert kernel.CancelIoEx.calls == []
    operation.close()


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_post_wait_clock_baseexception_precedes_result_interpretation(
    monkeypatch: pytest.MonkeyPatch,
    interrupt_type: type[BaseException],
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ScriptedClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3E16,
        event_handle=0x2E16,
        absolute_deadline=100.0,
    )
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.on_native_return["WaitForSingleObject"] = lambda: clock.arm_next(
        interrupt_type()
    )
    kernel.native_trace.clear()

    with pytest.raises(BaseExceptionGroup) as interrupted:
        operation.wait_to_deadline()

    _, handoff = _assert_interrupt_group(
        module,
        interrupted.value,
        interrupt_type,
    )
    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="wait",
        owner=operation,
        winerror=None,
        completion=None,
    )
    _assert_state(module, operation, "PENDING")
    assert operation.completion is None
    _assert_native_trace(kernel, "WaitForSingleObject")
    assert kernel.GetOverlappedResult.calls == []
    assert kernel.CancelIoEx.calls == []
    kernel.on_native_return.pop("WaitForSingleObject")
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xE016))
    before_waits = len(kernel.WaitForSingleObject.calls)
    kernel.native_trace.clear()

    completion = operation.wait_to_deadline()

    assert len(kernel.WaitForSingleObject.calls) == before_waits
    _assert_native_trace(kernel, "GetOverlappedResult")
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
    )
    _assert_state(module, operation, "DRAINED")
    operation.close()


@pytest.mark.parametrize(
    "observation_mode",
    ["object", "timeout", "failed", "unexpected"],
)
@pytest.mark.parametrize(
    "interrupt_type",
    [RuntimeError, KeyboardInterrupt, SystemExit],
)
@pytest.mark.parametrize(
    "transfer_owner",
    [False, True],
    ids=["same-facade", "transferred-destination"],
)
def test_committed_wait_observation_precedes_method_switch_after_interruption(
    monkeypatch: pytest.MonkeyPatch,
    observation_mode: str,
    interrupt_type: type[BaseException],
    transfer_owner: bool,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    real_wait_transition = module._commit_wait_transition
    transition_modes: list[str] = []

    def recording_wait_transition(
        cell: object,
        immutable_transition: object,
    ) -> object:
        mode = getattr(immutable_transition, "mode")
        transition_modes.append(str(getattr(mode, "value", mode)))
        return real_wait_transition(cell, immutable_transition)

    monkeypatch.setattr(
        module,
        "_commit_wait_transition",
        recording_wait_transition,
    )
    clock = _ScriptedClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    source = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3E17,
        event_handle=0x2E17,
        absolute_deadline=100.0,
    )
    wait_outcomes = {
        "object": _WaitOutcome(WAIT_OBJECT_0, ERROR_ACCESS_DENIED),
        "timeout": _WaitOutcome(WAIT_TIMEOUT, ERROR_ACCESS_DENIED),
        "failed": _WaitOutcome(WAIT_FAILED, ERROR_INVALID_HANDLE),
        "unexpected": _WaitOutcome(0x80, ERROR_ACCESS_DENIED),
    }
    original = interrupt_type("post-Wait clock interrupted")
    kernel.wait_results.append(wait_outcomes[observation_mode])
    kernel.on_native_return["WaitForSingleObject"] = lambda: clock.arm_next(
        original
    )
    kernel.native_trace.clear()

    if issubclass(interrupt_type, Exception):
        with pytest.raises(module.OverlappedPipeError) as interrupted:
            source.wait_to_deadline()
        handoff = interrupted.value
        assert handoff.__cause__ is original
    else:
        with pytest.raises(BaseExceptionGroup) as interrupted_group:
            source.wait_to_deadline()
        primary, handoff = _assert_interrupt_group(
            module,
            interrupted_group.value,
            interrupt_type,
        )
        assert primary is original

    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="wait",
        owner=source,
        winerror=None,
        completion=None,
    )
    _assert_state(module, source, "PENDING")
    _assert_native_trace(kernel, "WaitForSingleObject")
    assert kernel.GetOverlappedResult.calls == []
    assert kernel.CancelIoEx.calls == []
    active = source.transfer() if transfer_owner else source
    if transfer_owner:
        _assert_state(module, source, "TRANSFERRED")
        assert source.recover_transferred_owner() is active
    _assert_state(module, active, "PENDING")
    assert transition_modes == ["OBSERVATION_COMMIT"]
    traceback = original.__traceback__
    assert traceback is not None
    clock.calls.clear()
    assert original not in _reachable_instances(active, interrupt_type, max_depth=10)
    assert traceback not in _reachable_instances(active, type(traceback), max_depth=10)
    assert _reachable_instances(
        active,
        module.OverlappedPipeError,
        max_depth=10,
    ) == ()
    assert not _module_strongly_references(module, original, max_depth=10)
    assert not _module_strongly_references(module, traceback, max_depth=10)
    kernel.on_native_return.pop("WaitForSingleObject")
    kernel.native_trace.clear()
    resume_clock_calls = len(clock.calls)

    if observation_mode == "object":
        clock.arm_next(100.0)
        kernel.result_outcomes.append(_Outcome(True, byte_count=0xE117))

        def require_resample_before_result() -> None:
            assert len(clock.calls) == resume_clock_calls + 1
            assert not clock.steps
            assert transition_modes == [
                "OBSERVATION_COMMIT",
                "OBJECT_DISPATCH",
            ]

        kernel.on_native_enter[
            "GetOverlappedResult"
        ] = require_resample_before_result

        with pytest.raises(module.OverlappedPipeError) as resumed:
            active.cancel_and_drain()

        completion = resumed.value.completion
        _assert_typed_error(
            module,
            resumed.value,
            reason="deadline_exceeded",
            stage="wait",
            owner=active,
            winerror=None,
            completion=completion,
        )
        _assert_completion(
            module,
            completion,
            operation="connect",
            native_outcome="success",
            bytes_transferred=None,
            winerror=None,
            deadline_exceeded=True,
        )
        _assert_native_trace(kernel, "GetOverlappedResult")
        assert transition_modes == [
            "OBSERVATION_COMMIT",
            "OBJECT_DISPATCH",
            "RESULT_HANDOFF",
        ]
        assert kernel.WaitForSingleObject.calls == [(0x2E17, MAX_WAIT_SLICE_MS)]
        assert kernel.CancelIoEx.calls == []
    elif observation_mode == "timeout":
        clock.arm_next(10.0)

        def require_resample_before_cancel() -> None:
            assert len(clock.calls) == resume_clock_calls + 1
            assert not clock.steps
            assert transition_modes == [
                "OBSERVATION_COMMIT",
                "NONOBJECT_HANDOFF",
            ]

        kernel.on_native_enter["CancelIoEx"] = require_resample_before_cancel
        kernel.cancel_outcomes.append(_Outcome(True))
        kernel.wait_results.append(WAIT_OBJECT_0)
        kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))

        completion = active.cancel_and_drain()

        _assert_completion(
            module,
            completion,
            operation="connect",
            native_outcome="cancelled",
            bytes_transferred=None,
            winerror=ERROR_OPERATION_ABORTED,
            cancel_outcome="accepted",
            cancel_winerror=None,
        )
        _assert_native_trace(
            kernel,
            "CancelIoEx",
            "WaitForSingleObject",
            "GetOverlappedResult",
        )
    else:
        clock.arm_next(10.0)
        before = _native_call_counts(kernel)

        with pytest.raises(module.OverlappedPipeError) as resumed:
            active.cancel_and_drain()

        assert len(clock.calls) == resume_clock_calls + 1
        assert not clock.steps
        assert transition_modes == [
            "OBSERVATION_COMMIT",
            "NONOBJECT_HANDOFF",
            "NONOBJECT_HANDOFF",
        ]

        _assert_typed_error(
            module,
            resumed.value,
            reason=(
                "native_failure" if observation_mode == "failed" else "lifecycle_violation"
            ),
            stage="wait",
            owner=active,
            winerror=(ERROR_INVALID_HANDLE if observation_mode == "failed" else None),
            completion=None,
        )
        _assert_no_native_calls_after(kernel, before)
        _assert_state(module, active, "PENDING")
        kernel.cancel_outcomes.append(_Outcome(True))
        kernel.wait_results.append(WAIT_OBJECT_0)
        kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))
        kernel.native_trace.clear()
        completion = active.cancel_and_drain()
        _assert_completion(
            module,
            completion,
            operation="connect",
            native_outcome="cancelled",
            bytes_transferred=None,
            winerror=ERROR_OPERATION_ABORTED,
            cancel_outcome="accepted",
            cancel_winerror=None,
        )
        _assert_native_trace(
            kernel,
            "CancelIoEx",
            "WaitForSingleObject",
            "GetOverlappedResult",
        )

    _assert_state(module, active, "DRAINED")
    active.close()


@pytest.mark.parametrize(
    "committed_nonobject",
    ["timeout", "failed", "unexpected"],
)
def test_committed_nonobject_recovery_resample_honors_crossed_deadline(
    monkeypatch: pytest.MonkeyPatch,
    committed_nonobject: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ScriptedClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    source = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3E18,
        event_handle=0x2E18,
        absolute_deadline=20.0,
    )
    wait_outcomes = {
        "timeout": _WaitOutcome(WAIT_TIMEOUT, ERROR_ACCESS_DENIED),
        "failed": _WaitOutcome(WAIT_FAILED, ERROR_INVALID_HANDLE),
        "unexpected": _WaitOutcome(0x80, ERROR_ACCESS_DENIED),
    }
    original = KeyboardInterrupt("post-Wait clock interrupted")
    kernel.wait_results.append(wait_outcomes[committed_nonobject])
    kernel.on_native_return["WaitForSingleObject"] = lambda: clock.arm_next(
        original
    )
    kernel.native_trace.clear()

    with pytest.raises(BaseExceptionGroup) as interrupted:
        source.wait_to_deadline()

    primary, handoff = _assert_interrupt_group(
        module,
        interrupted.value,
        KeyboardInterrupt,
    )
    assert primary is original
    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="wait",
        owner=source,
        winerror=None,
        completion=None,
    )
    _assert_state(module, source, "PENDING")
    _assert_native_trace(kernel, "WaitForSingleObject")
    assert kernel.CancelIoEx.calls == []
    assert kernel.GetOverlappedResult.calls == []
    clock.calls.clear()
    kernel.on_native_return.pop("WaitForSingleObject")
    destination = source.transfer()
    assert source.recover_transferred_owner() is destination
    clock.arm_next(20.0)
    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xE118))
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as resumed:
        destination.cancel_and_drain()

    completion = resumed.value.completion
    _assert_typed_error(
        module,
        resumed.value,
        reason="deadline_exceeded",
        stage="drain",
        owner=destination,
        winerror=None,
        completion=completion,
    )
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
        deadline_exceeded=True,
        cancel_outcome="accepted",
        cancel_winerror=None,
    )
    _assert_state(module, destination, "DRAINED")
    _assert_native_trace(kernel, "CancelIoEx", "GetOverlappedResult")
    assert len(kernel.WaitForSingleObject.calls) == 1
    assert len(kernel.CancelIoEx.calls) == 1
    destination.close()


@pytest.mark.parametrize(
    "target_mode",
    [
        "OBSERVATION_COMMIT",
        "NONOBJECT_HANDOFF",
        "OBJECT_DISPATCH",
        "RESULT_HANDOFF",
    ],
)
@pytest.mark.parametrize(
    "altered_field",
    ["mode", "expected_snapshot", "target_snapshot"],
    ids=["equal-mode", "equal-source", "equal-target"],
)
def test_wait_transition_rejects_equal_but_distinct_mode_source_and_target(
    monkeypatch: pytest.MonkeyPatch,
    target_mode: str,
    altered_field: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3E1D,
        event_handle=0x2E1D,
        absolute_deadline=100.0,
    )

    if target_mode == "NONOBJECT_HANDOFF":
        kernel.wait_results.extend([WAIT_TIMEOUT, WAIT_OBJECT_0])
    else:
        kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xE11D))

    real_transition = module._commit_wait_transition
    injected = False
    mismatch_statuses: list[str] = []
    commit_statuses: list[str] = []

    def status_value(value: object) -> str:
        return str(getattr(value, "value", value))

    def forbidden_clock() -> float:
        raise AssertionError("_commit_wait_transition sampled the clock")

    def identity_guarding_transition(
        cell: object,
        immutable_transition: object,
    ) -> object:
        nonlocal injected
        mode = immutable_transition.mode
        mode_value = str(getattr(mode, "value", mode))
        if mode_value != target_mode or injected:
            return real_transition(cell, immutable_transition)

        original_value = getattr(
            immutable_transition,
            altered_field,
        )
        equal_but_distinct = _EqualButDistinctLifecycleValue(
            original_value
        )
        assert equal_but_distinct is not original_value
        assert equal_but_distinct == original_value
        assert original_value == equal_but_distinct

        mismatched = replace(
            immutable_transition,
            **{altered_field: equal_but_distinct},
        )
        before_native = _native_call_counts(kernel)
        active_clock = module._monotonic
        setattr(module, "_monotonic", forbidden_clock)
        try:
            mismatch = real_transition(cell, mismatched)
        finally:
            setattr(module, "_monotonic", active_clock)

        mismatch_statuses.append(status_value(mismatch))
        assert mismatch_statuses[-1] == "MISMATCH"
        _assert_no_native_calls_after(kernel, before_native)

        injected = True
        committed = real_transition(cell, immutable_transition)
        commit_statuses.append(status_value(committed))
        assert commit_statuses[-1] == "COMMITTED"
        return committed

    monkeypatch.setattr(
        module,
        "_commit_wait_transition",
        identity_guarding_transition,
    )
    kernel.native_trace.clear()

    completion = operation.wait_to_deadline()

    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
    )
    assert injected is True
    assert mismatch_statuses == ["MISMATCH"]
    assert commit_statuses == ["COMMITTED"]
    assert kernel.CancelIoEx.calls == []
    assert len(kernel.WaitForSingleObject.calls) == (
        2 if target_mode == "NONOBJECT_HANDOFF" else 1
    )
    assert len(kernel.GetOverlappedResult.calls) == 1
    _assert_state(module, operation, "DRAINED")
    operation.close()


@pytest.mark.parametrize("fault_position", ["before", "after"])
@pytest.mark.parametrize(
    ("target_mode", "interrupt_type"),
    [
        pytest.param("OBSERVATION_COMMIT", RuntimeError, id="observe-exception"),
        pytest.param("OBSERVATION_COMMIT", KeyboardInterrupt, id="observe-keyboard"),
        pytest.param("OBSERVATION_COMMIT", SystemExit, id="observe-system-exit"),
        pytest.param("NONOBJECT_HANDOFF", RuntimeError, id="timeout-exception"),
        pytest.param("NONOBJECT_HANDOFF", KeyboardInterrupt, id="timeout-keyboard"),
        pytest.param("NONOBJECT_HANDOFF", SystemExit, id="timeout-system-exit"),
        pytest.param("OBJECT_DISPATCH", RuntimeError, id="dispatch-exception"),
        pytest.param("OBJECT_DISPATCH", KeyboardInterrupt, id="dispatch-keyboard"),
        pytest.param("OBJECT_DISPATCH", SystemExit, id="dispatch-system-exit"),
        pytest.param("RESULT_HANDOFF", RuntimeError, id="handoff-exception"),
        pytest.param("RESULT_HANDOFF", KeyboardInterrupt, id="handoff-keyboard"),
        pytest.param("RESULT_HANDOFF", SystemExit, id="handoff-system-exit"),
    ],
)
def test_wait_transition_commits_are_atomic_replayable_and_transfer_safe(
    monkeypatch: pytest.MonkeyPatch,
    target_mode: str,
    interrupt_type: type[BaseException],
    fault_position: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    source = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3E1A,
        event_handle=0x2E1A,
        absolute_deadline=100.0,
    )
    active = source.transfer()
    assert source.recover_transferred_owner() is active

    if target_mode == "NONOBJECT_HANDOFF":
        kernel.wait_results.extend([WAIT_TIMEOUT, WAIT_OBJECT_0])
        kernel.cancel_outcomes.append(_Outcome(True))
        kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))
    else:
        kernel.wait_results.append(WAIT_OBJECT_0)
        if target_mode == "OBSERVATION_COMMIT" and fault_position == "before":
            kernel.wait_results.append(WAIT_OBJECT_0)
        kernel.result_outcomes.append(_Outcome(True, byte_count=0xE11A))
        if target_mode == "RESULT_HANDOFF" and fault_position == "before":
            kernel.result_outcomes.append(_Outcome(True, byte_count=0xE11A))

    real_transition = module._commit_wait_transition
    original = interrupt_type(f"{target_mode} {fault_position} interrupted")
    target_attempts: list[object] = []
    mismatch_statuses: list[str] = []
    commit_statuses: list[str] = []
    replay_statuses: list[str] = []
    fault_delivered = False

    def status_value(value: object) -> str:
        return str(getattr(value, "value", value))

    def forbidden_clock_sample() -> float:
        raise AssertionError("_commit_wait_transition sampled the clock")

    def call_real_transition(
        cell: object,
        immutable_transition: object,
    ) -> object:
        before_native = _native_call_counts(kernel)
        active_clock = module._monotonic
        setattr(module, "_monotonic", forbidden_clock_sample)
        try:
            result = real_transition(cell, immutable_transition)
        finally:
            setattr(module, "_monotonic", active_clock)
        _assert_no_native_calls_after(kernel, before_native)
        return result

    def faulting_transition(
        cell: object,
        immutable_transition: object,
    ) -> object:
        nonlocal fault_delivered
        mode = getattr(immutable_transition, "mode")
        mode_value = str(getattr(mode, "value", mode))
        if mode_value != target_mode:
            return call_real_transition(cell, immutable_transition)

        target_attempts.append(immutable_transition)
        assert is_dataclass(immutable_transition)
        assert not hasattr(immutable_transition, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(immutable_transition, "expected_snapshot", object())
        if not fault_delivered:
            mismatched = replace(
                immutable_transition,
                expected_snapshot=object(),
            )
            mismatch_statuses.append(
                status_value(call_real_transition(cell, mismatched))
            )
            fault_delivered = True
            if fault_position == "before":
                raise original
            committed = call_real_transition(cell, immutable_transition)
            commit_statuses.append(status_value(committed))
            replay_statuses.append(
                status_value(call_real_transition(cell, immutable_transition))
            )
            raise original

        committed = call_real_transition(cell, immutable_transition)
        commit_statuses.append(status_value(committed))
        replay_statuses.append(
            status_value(call_real_transition(cell, immutable_transition))
        )
        return committed

    monkeypatch.setattr(module, "_commit_wait_transition", faulting_transition)
    kernel.native_trace.clear()

    if issubclass(interrupt_type, Exception):
        with pytest.raises(module.OverlappedPipeError) as interrupted:
            active.wait_to_deadline()
        handoff = interrupted.value
        assert handoff.__cause__ is original
    else:
        with pytest.raises(BaseExceptionGroup) as interrupted_group:
            active.wait_to_deadline()
        primary, handoff = _assert_interrupt_group(
            module,
            interrupted_group.value,
            interrupt_type,
        )
        assert primary is original

    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="result" if target_mode == "RESULT_HANDOFF" else "wait",
        owner=active,
        winerror=None,
        completion=None,
    )
    assert mismatch_statuses == ["MISMATCH"]
    assert commit_statuses == ([] if fault_position == "before" else ["COMMITTED"])
    assert replay_statuses == ([] if fault_position == "before" else ["ALREADY_COMMITTED"])
    assert len(target_attempts) == 1
    assert len(kernel.WaitForSingleObject.calls) == 1
    assert len(kernel.GetOverlappedResult.calls) == (
        1 if target_mode == "RESULT_HANDOFF" else 0
    )
    assert kernel.CancelIoEx.calls == []
    monkeypatch.setattr(
        module,
        "_commit_wait_transition",
        real_transition,
    )
    _assert_errors_and_tracebacks_not_cached(
        module,
        active,
        original,
        handoff,
    )
    monkeypatch.setattr(
        module,
        "_commit_wait_transition",
        faulting_transition,
    )
    kernel.native_trace.clear()

    completion = (
        active.wait_to_deadline()
        if target_mode == "OBSERVATION_COMMIT" and fault_position == "before"
        else active.cancel_and_drain()
    )

    assert mismatch_statuses == ["MISMATCH"]
    assert commit_statuses == ["COMMITTED"]
    assert replay_statuses == ["ALREADY_COMMITTED"]
    assert len(target_attempts) == (2 if fault_position == "before" else 1)
    if target_mode == "NONOBJECT_HANDOFF":
        _assert_completion(
            module,
            completion,
            operation="connect",
            native_outcome="cancelled",
            bytes_transferred=None,
            winerror=ERROR_OPERATION_ABORTED,
            cancel_outcome="accepted",
            cancel_winerror=None,
        )
        _assert_native_trace(
            kernel,
            "CancelIoEx",
            "WaitForSingleObject",
            "GetOverlappedResult",
        )
    else:
        _assert_completion(
            module,
            completion,
            operation="connect",
            native_outcome="success",
            bytes_transferred=None,
            winerror=None,
        )
        if target_mode == "OBSERVATION_COMMIT" and fault_position == "before":
            _assert_native_trace(kernel, "WaitForSingleObject", "GetOverlappedResult")
        elif target_mode == "RESULT_HANDOFF" and fault_position == "after":
            _assert_native_trace(kernel)
        else:
            _assert_native_trace(kernel, "GetOverlappedResult")

    assert len(kernel.WaitForSingleObject.calls) == (
        2
        if target_mode in {"NONOBJECT_HANDOFF", "OBSERVATION_COMMIT"}
        and (target_mode == "NONOBJECT_HANDOFF" or fault_position == "before")
        else 1
    )
    assert len(kernel.GetOverlappedResult.calls) == (
        2
        if target_mode == "RESULT_HANDOFF" and fault_position == "before"
        else 1
    )
    assert len(kernel.CancelIoEx.calls) == (
        1 if target_mode == "NONOBJECT_HANDOFF" else 0
    )
    _assert_state(module, active, "DRAINED")
    active.close()


@pytest.mark.parametrize(
    ("wait_outcome", "reason", "winerror"),
    [
        pytest.param(
            _WaitOutcome(WAIT_FAILED, ERROR_INVALID_HANDLE),
            "native_failure",
            ERROR_INVALID_HANDLE,
            id="wait-failed",
        ),
        pytest.param(
            _WaitOutcome(0x80, ERROR_ACCESS_DENIED),
            "lifecycle_violation",
            None,
            id="unexpected",
        ),
    ],
)
@pytest.mark.parametrize(
    "resume_case",
    [
        "pending",
        "submit-unknown",
        "cancel-unknown-pending",
        "cancel-unknown-submit",
    ],
)
def test_pending_wait_outward_record_survives_transfer_and_is_acked_once(
    monkeypatch: pytest.MonkeyPatch,
    wait_outcome: _WaitOutcome,
    reason: str,
    winerror: int | None,
    resume_case: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    starts_submit_unknown = resume_case in {
        "submit-unknown",
        "cancel-unknown-submit",
    }
    if starts_submit_unknown:
        source = _start_submit_unknown_operation(
            module=module,
            kernel=kernel,
            pipe_handle=0x3E1B,
            event_handle=0x2E1B,
            absolute_deadline=100.0,
        )
    else:
        source = _start_pending_operation(
            module=module,
            kernel=kernel,
            operation_kind="connect",
            pipe_handle=0x3E1B,
            event_handle=0x2E1B,
            absolute_deadline=100.0,
        )
    cancel_already_attempted = resume_case.startswith("cancel-unknown")
    expected_resume_state = (
        "CANCEL_UNKNOWN"
        if cancel_already_attempted
        else "SUBMIT_UNKNOWN"
        if starts_submit_unknown
        else "PENDING"
    )
    if cancel_already_attempted:
        kernel.cancel_outcomes.append(
            _Outcome(True, interrupt=KeyboardInterrupt())
        )
        with pytest.raises(BaseExceptionGroup) as cancel_interrupted:
            source.cancel_and_drain()
        _assert_interrupt_group(
            module,
            cancel_interrupted.value,
            KeyboardInterrupt,
        )
        _assert_state(module, source, "CANCEL_UNKNOWN")

    kernel.wait_results.append(wait_outcome)
    real_transition = module._commit_wait_transition
    original = RuntimeError("NONOBJECT_HANDOFF post-commit interrupted")
    transition_modes: list[str] = []
    observation_transitions: list[object] = []
    nonobject_transitions: list[object] = []
    commit_statuses: list[str] = []
    replay_statuses: list[str] = []
    stale_observation_statuses: list[str] = []
    nonobject_commits = 0

    def fault_after_wait_handoff(
        cell: object,
        immutable_transition: object,
    ) -> object:
        nonlocal nonobject_commits
        mode = getattr(immutable_transition, "mode")
        mode_value = str(getattr(mode, "value", mode))
        transition_modes.append(mode_value)
        if mode_value == "OBSERVATION_COMMIT" and not observation_transitions:
            observation_transitions.append(immutable_transition)
        committed = real_transition(cell, immutable_transition)
        commit_statuses.append(str(getattr(committed, "value", committed)))
        if mode_value == "NONOBJECT_HANDOFF":
            nonobject_transitions.append(immutable_transition)
            nonobject_commits += 1
            if nonobject_commits == 1:
                replay = real_transition(cell, immutable_transition)
                replay_statuses.append(str(getattr(replay, "value", replay)))
                raise original
            if nonobject_commits == 2:
                stale = real_transition(cell, observation_transitions[0])
                stale_observation_statuses.append(
                    str(getattr(stale, "value", stale))
                )
            replay = real_transition(cell, immutable_transition)
            replay_statuses.append(str(getattr(replay, "value", replay)))
        return committed

    monkeypatch.setattr(
        module,
        "_commit_wait_transition",
        fault_after_wait_handoff,
    )
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as interrupted:
        source.wait_to_deadline()

    assert interrupted.value.__cause__ is original
    _assert_typed_error(
        module,
        interrupted.value,
        reason="native_failure",
        stage="wait",
        owner=source,
        winerror=None,
        completion=None,
    )
    assert transition_modes == ["OBSERVATION_COMMIT", "NONOBJECT_HANDOFF"]
    assert commit_statuses == ["COMMITTED", "COMMITTED"]
    assert replay_statuses == ["ALREADY_COMMITTED"]
    assert stale_observation_statuses == []
    _assert_state(module, source, expected_resume_state)
    _assert_native_trace(kernel, "WaitForSingleObject")
    monkeypatch.setattr(module, "_commit_wait_transition", real_transition)
    _assert_errors_and_tracebacks_not_cached(
        module,
        source,
        original,
        interrupted.value,
    )
    monkeypatch.setattr(
        module,
        "_commit_wait_transition",
        fault_after_wait_handoff,
    )
    destination = source.transfer()
    assert source.recover_transferred_owner() is destination
    before = _native_call_counts(kernel)
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as delivered:
        destination.cancel_and_drain()

    _assert_typed_error(
        module,
        delivered.value,
        reason=reason,
        stage="wait",
        owner=destination,
        winerror=winerror,
        completion=None,
    )
    assert delivered.value.__cause__ is None
    assert delivered.value.__context__ is None
    assert transition_modes == [
        "OBSERVATION_COMMIT",
        "NONOBJECT_HANDOFF",
        "NONOBJECT_HANDOFF",
    ]
    assert commit_statuses == ["COMMITTED", "COMMITTED", "COMMITTED"]
    assert replay_statuses == ["ALREADY_COMMITTED", "ALREADY_COMMITTED"]
    assert stale_observation_statuses == ["MISMATCH"]
    assert len(observation_transitions) == 1
    assert len(nonobject_transitions) == 2
    observation = observation_transitions[0]
    handoff_transition, ack_transition = nonobject_transitions
    assert observation.target_snapshot is handoff_transition.expected_snapshot
    assert handoff_transition.target_snapshot is ack_transition.expected_snapshot
    assert ack_transition.target_snapshot is not ack_transition.expected_snapshot
    assert ack_transition.target_snapshot is not observation.expected_snapshot
    assert ack_transition.target_snapshot is not observation.target_snapshot
    assert ack_transition.target_snapshot is not handoff_transition.target_snapshot
    _assert_no_native_calls_after(kernel, before)
    _assert_native_trace(kernel)
    _assert_state(module, destination, expected_resume_state)

    if resume_case == "cancel-unknown-pending":
        clock.now = 100.0
        kernel.wait_results.append(WAIT_TIMEOUT)
        kernel.result_outcomes.append(_Outcome(True, byte_count=0x600D))
        before_cancels = len(kernel.CancelIoEx.calls)
        kernel.native_trace.clear()

        with pytest.raises(module.OverlappedPipeError) as terminal:
            destination.cancel_and_drain()

        completion = terminal.value.completion
        _assert_typed_error(
            module,
            terminal.value,
            reason="deadline_exceeded",
            stage="drain",
            owner=destination,
            winerror=None,
            completion=completion,
        )
        _assert_completion(
            module,
            completion,
            operation="connect",
            native_outcome="success",
            bytes_transferred=None,
            winerror=None,
            deadline_exceeded=True,
            cancel_outcome="unknown",
            cancel_winerror=None,
        )
        assert len(kernel.CancelIoEx.calls) == before_cancels
        assert kernel.WaitForSingleObject.calls[-1] == (0x2E1B, 0)
        _assert_native_trace(
            kernel,
            "WaitForSingleObject",
            "GetOverlappedResult",
        )
        _assert_state(module, destination, "DRAINED")
        destination.close()
        return

    if resume_case == "cancel-unknown-submit":
        clock.now = 100.0
        kernel.wait_results.append(WAIT_TIMEOUT)
        kernel.result_outcomes.append(_Outcome(True, byte_count=0xBAD0))
        before_cancels = len(kernel.CancelIoEx.calls)
        kernel.native_trace.clear()

        with pytest.raises(module.OverlappedPipeError) as still_unproven:
            destination.cancel_and_drain()

        _assert_typed_error(
            module,
            still_unproven.value,
            reason="drain_pending",
            stage="drain",
            owner=destination,
            winerror=None,
            completion=None,
        )
        assert destination.completion is None
        assert len(kernel.CancelIoEx.calls) == before_cancels
        assert kernel.WaitForSingleObject.calls[-1] == (0x2E1B, 0)
        _assert_native_trace(
            kernel,
            "WaitForSingleObject",
            "GetOverlappedResult",
        )

        kernel.wait_results.append(WAIT_TIMEOUT)
        kernel.result_outcomes.append(_Outcome(True, byte_count=0xBAD1))
        kernel.native_trace.clear()

        with pytest.raises(module.OverlappedPipeError) as still_unproven_again:
            destination.cancel_and_drain()

        _assert_typed_error(
            module,
            still_unproven_again.value,
            reason="drain_pending",
            stage="drain",
            owner=destination,
            winerror=None,
            completion=None,
        )
        assert destination.completion is None
        _assert_state(module, destination, "CANCEL_UNKNOWN")
        assert len(kernel.CancelIoEx.calls) == before_cancels
        assert kernel.WaitForSingleObject.calls[-1] == (0x2E1B, 0)
        _assert_native_trace(
            kernel,
            "WaitForSingleObject",
            "GetOverlappedResult",
        )

        kernel.wait_results.append(WAIT_OBJECT_0)
        kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))
        kernel.native_trace.clear()

        with pytest.raises(module.OverlappedPipeError) as terminal:
            destination.cancel_and_drain()

        completion = terminal.value.completion
        _assert_typed_error(
            module,
            terminal.value,
            reason="deadline_exceeded",
            stage="drain",
            owner=destination,
            winerror=None,
            completion=completion,
        )
        _assert_completion(
            module,
            completion,
            operation="connect",
            native_outcome="cancelled",
            bytes_transferred=None,
            winerror=ERROR_OPERATION_ABORTED,
            deadline_exceeded=True,
            cancel_outcome="unknown",
            cancel_winerror=None,
        )
        assert len(kernel.CancelIoEx.calls) == before_cancels
        assert kernel.WaitForSingleObject.calls[-1] == (0x2E1B, 0)
        _assert_native_trace(
            kernel,
            "WaitForSingleObject",
            "GetOverlappedResult",
        )
        _assert_state(module, destination, "DRAINED")
        destination.close()
        return

    if not cancel_already_attempted:
        kernel.cancel_outcomes.append(_Outcome(True))
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))
    kernel.native_trace.clear()

    completion = destination.cancel_and_drain()

    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="cancelled",
        bytes_transferred=None,
        winerror=ERROR_OPERATION_ABORTED,
        cancel_outcome=("unknown" if cancel_already_attempted else "accepted"),
        cancel_winerror=None,
    )
    assert transition_modes.count("NONOBJECT_HANDOFF") == 2
    _assert_native_trace(
        kernel,
        *(
            ("WaitForSingleObject", "GetOverlappedResult")
            if cancel_already_attempted
            else ("CancelIoEx", "WaitForSingleObject", "GetOverlappedResult")
        ),
    )
    expected_wait_ms = (
        0 if resume_case == "cancel-unknown-submit" else MAX_WAIT_SLICE_MS
    )
    assert kernel.WaitForSingleObject.calls[-1] == (0x2E1B, expected_wait_ms)
    _assert_state(module, destination, "DRAINED")
    destination.close()


@pytest.mark.parametrize(
    "interrupt_type",
    [RuntimeError, KeyboardInterrupt, SystemExit],
)
def test_pending_wait_ack_postcommit_interruption_never_replays_old_wait_leaf(
    monkeypatch: pytest.MonkeyPatch,
    interrupt_type: type[BaseException],
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3E1C,
        event_handle=0x2E1C,
        absolute_deadline=100.0,
    )
    kernel.wait_results.append(_WaitOutcome(WAIT_FAILED, ERROR_INVALID_HANDLE))
    real_transition = module._commit_wait_transition
    original = interrupt_type("NONOBJECT_HANDOFF ACK post-commit interrupted")
    transition_modes: list[str] = []
    transitions: list[object] = []
    commit_statuses: list[str] = []
    nonobject_commits = 0

    def commit_ack_then_interrupt(
        cell: object,
        immutable_transition: object,
    ) -> object:
        nonlocal nonobject_commits
        mode = getattr(immutable_transition, "mode")
        mode_value = str(getattr(mode, "value", mode))
        transition_modes.append(mode_value)
        transitions.append(immutable_transition)
        committed = real_transition(cell, immutable_transition)
        commit_statuses.append(str(getattr(committed, "value", committed)))
        if mode_value == "NONOBJECT_HANDOFF":
            nonobject_commits += 1
            if nonobject_commits == 2:
                raise original
        return committed

    monkeypatch.setattr(
        module,
        "_commit_wait_transition",
        commit_ack_then_interrupt,
    )
    kernel.native_trace.clear()

    if issubclass(interrupt_type, Exception):
        with pytest.raises(module.OverlappedPipeError) as interrupted:
            operation.wait_to_deadline()
        handoff = interrupted.value
        assert handoff.__cause__ is original
    else:
        with pytest.raises(BaseExceptionGroup) as interrupted_group:
            operation.wait_to_deadline()
        primary, handoff = _assert_interrupt_group(
            module,
            interrupted_group.value,
            interrupt_type,
        )
        assert primary is original

    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="wait",
        owner=operation,
        winerror=None,
        completion=None,
    )
    assert transition_modes == [
        "OBSERVATION_COMMIT",
        "NONOBJECT_HANDOFF",
        "NONOBJECT_HANDOFF",
    ]
    assert commit_statuses == ["COMMITTED", "COMMITTED", "COMMITTED"]
    observation, wait_handoff, ack = transitions
    assert observation.target_snapshot is wait_handoff.expected_snapshot
    assert wait_handoff.target_snapshot is ack.expected_snapshot
    assert ack.target_snapshot is not ack.expected_snapshot
    assert ack.target_snapshot is not observation.expected_snapshot
    assert ack.target_snapshot is not observation.target_snapshot
    assert ack.target_snapshot is not wait_handoff.target_snapshot
    _assert_state(module, operation, "PENDING")
    _assert_native_trace(kernel, "WaitForSingleObject")
    monkeypatch.setattr(module, "_commit_wait_transition", real_transition)
    _assert_errors_and_tracebacks_not_cached(
        module,
        operation,
        original,
        handoff,
    )

    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xE11C))
    kernel.native_trace.clear()

    completion = operation.wait_to_deadline()

    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
    )
    _assert_state(module, operation, "DRAINED")
    _assert_native_trace(kernel, "WaitForSingleObject", "GetOverlappedResult")
    assert kernel.CancelIoEx.calls == []
    operation.close()


@pytest.mark.parametrize("gor_boundary", ["enter", "return"])
@pytest.mark.parametrize(
    "interrupt_type",
    [RuntimeError, KeyboardInterrupt, SystemExit],
)
def test_object_observation_result_ambiguity_stays_direct_and_keeps_wait_origin(
    monkeypatch: pytest.MonkeyPatch,
    gor_boundary: str,
    interrupt_type: type[BaseException],
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ScriptedClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    source = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3E19,
        event_handle=0x2E19,
        absolute_deadline=20.0,
    )
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.on_native_return["WaitForSingleObject"] = lambda: clock.arm_next(
        KeyboardInterrupt()
    )

    with pytest.raises(BaseExceptionGroup):
        source.wait_to_deadline()

    destination = source.transfer()
    _assert_state(module, destination, "PENDING")
    kernel.on_native_return.pop("WaitForSingleObject")
    clock.calls.clear()
    original = interrupt_type(f"GOR {gor_boundary} interrupted")
    if gor_boundary == "enter":
        def interrupt_result_enter() -> None:
            raise original

        kernel.on_native_enter["GetOverlappedResult"] = interrupt_result_enter
    else:
        kernel.result_outcomes.append(
            _Outcome(True, byte_count=0xE119, interrupt=original)
        )
    clock.arm_next(10.0)
    kernel.native_trace.clear()

    primary, handoff = _capture_outward_interruption(
        module,
        original,
        destination.cancel_and_drain,
    )

    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="result",
        owner=destination,
        winerror=None,
        completion=None,
    )
    _assert_state(module, destination, "RESULT_UNKNOWN")
    assert kernel.WaitForSingleObject.calls == [(0x2E19, MAX_WAIT_SLICE_MS)]
    assert kernel.CancelIoEx.calls == []
    if gor_boundary == "enter":
        _assert_native_trace(kernel)
        kernel.on_native_enter.pop("GetOverlappedResult")
    else:
        _assert_native_trace(kernel, "GetOverlappedResult")

    clock.calls.clear()
    _assert_errors_and_tracebacks_not_cached(
        module,
        destination,
        primary,
        handoff,
    )
    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.result_outcomes.append(_Outcome(False, 0))
    before_waits = len(kernel.WaitForSingleObject.calls)
    before_cancels = len(kernel.CancelIoEx.calls)
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as still_ambiguous:
        destination.cancel_and_drain()

    _assert_typed_error(
        module,
        still_ambiguous.value,
        reason="native_failure",
        stage="result",
        owner=destination,
        winerror=None,
        completion=None,
    )
    _assert_state(module, destination, "RESULT_UNKNOWN")
    assert len(kernel.WaitForSingleObject.calls) == before_waits
    assert len(kernel.CancelIoEx.calls) == before_cancels + 1
    _assert_native_trace(kernel, "CancelIoEx", "GetOverlappedResult")

    kernel.on_native_return["GetOverlappedResult"] = lambda: setattr(
        clock, "now", 20.0
    )
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xE119))
    before_waits = len(kernel.WaitForSingleObject.calls)
    before_cancels = len(kernel.CancelIoEx.calls)
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as terminal:
        destination.cancel_and_drain()

    completion = terminal.value.completion
    _assert_typed_error(
        module,
        terminal.value,
        reason="deadline_exceeded",
        stage="wait",
        owner=destination,
        winerror=None,
        completion=completion,
    )
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
        deadline_exceeded=True,
        cancel_outcome="accepted",
        cancel_winerror=None,
    )
    assert len(kernel.WaitForSingleObject.calls) == before_waits
    assert len(kernel.CancelIoEx.calls) == before_cancels
    _assert_native_trace(kernel, "GetOverlappedResult")
    _assert_state(module, destination, "DRAINED")
    destination.close()


@pytest.mark.parametrize(
    (
        "result_outcome",
        "expected_native_outcome",
        "expected_bytes",
        "expected_winerror",
    ),
    [
        (_Outcome(True, byte_count=len(FRAME)), "success", len(FRAME), None),
        (_Outcome(True, byte_count=len(FRAME) - 1), "success", len(FRAME) - 1, None),
        (_Outcome(False, ERROR_ACCESS_DENIED), "failure", None, ERROR_ACCESS_DENIED),
        (
            _Outcome(False, ERROR_OPERATION_ABORTED),
            "cancelled",
            None,
            ERROR_OPERATION_ABORTED,
        ),
    ],
    ids=["success-exact", "success-bad-byte-count", "failure", "cancelled"],
)
def test_post_cancel_deadline_sample_has_drain_origin_and_terminal_priority(
    monkeypatch: pytest.MonkeyPatch,
    result_outcome: _Outcome,
    expected_native_outcome: str,
    expected_bytes: int | None,
    expected_winerror: int | None,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="write",
        pipe_handle=0x3E07,
        event_handle=0x2E07,
        absolute_deadline=20.0,
    )
    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.result_outcomes.append(result_outcome)
    kernel.on_native_return["CancelIoEx"] = lambda: setattr(clock, "now", 20.0)
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.cancel_and_drain()

    completion = captured.value.completion
    _assert_typed_error(
        module,
        captured.value,
        reason="deadline_exceeded",
        stage="drain",
        owner=operation,
        winerror=expected_winerror,
        completion=completion,
    )
    _assert_completion(
        module,
        completion,
        operation="write",
        native_outcome=expected_native_outcome,
        bytes_transferred=expected_bytes,
        winerror=expected_winerror,
        deadline_exceeded=True,
        cancel_outcome="accepted",
        cancel_winerror=None,
    )
    _assert_state(module, operation, "DRAINED")
    _assert_native_trace(kernel, "CancelIoEx", "GetOverlappedResult")
    assert len(kernel.CancelIoEx.calls) == 1
    assert len(kernel.GetOverlappedResult.calls) == 1
    assert kernel.WaitForSingleObject.calls == []
    operation.close()


def test_post_result_deadline_sample_preserves_wait_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="write",
        pipe_handle=0x3E08,
        event_handle=0x2E08,
        absolute_deadline=20.0,
    )
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=len(FRAME)))
    kernel.on_native_return["GetOverlappedResult"] = lambda: setattr(
        clock, "now", 20.0
    )
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.wait_to_deadline()

    completion = captured.value.completion
    _assert_typed_error(
        module,
        captured.value,
        reason="deadline_exceeded",
        stage="wait",
        owner=operation,
        winerror=None,
        completion=completion,
    )
    _assert_completion(
        module,
        completion,
        operation="write",
        native_outcome="success",
        bytes_transferred=len(FRAME),
        winerror=None,
        deadline_exceeded=True,
    )
    _assert_state(module, operation, "DRAINED")
    _assert_native_trace(kernel, "WaitForSingleObject", "GetOverlappedResult")
    assert kernel.CancelIoEx.calls == []
    operation.close()


@pytest.mark.parametrize(
    "wait_outcome",
    [
        _WaitOutcome(WAIT_FAILED, ERROR_INVALID_HANDLE),
        _WaitOutcome(0x80, ERROR_ACCESS_DENIED),
    ],
    ids=["wait-failed", "unexpected-value"],
)
def test_wait_failure_or_unexpected_value_yields_to_crossed_deadline(
    monkeypatch: pytest.MonkeyPatch,
    wait_outcome: _WaitOutcome,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ScriptedClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3E18,
        event_handle=0x2E18,
        absolute_deadline=11.0,
    )
    wait_return_clock_calls: list[int] = []

    def cross_deadline_after_wait() -> None:
        wait_return_clock_calls.append(len(clock.calls))
        clock.now = 12.0

    def require_post_wait_sample_before_cancel() -> None:
        assert wait_return_clock_calls
        assert len(clock.calls) > wait_return_clock_calls[0]

    kernel.on_native_return["WaitForSingleObject"] = cross_deadline_after_wait
    kernel.on_native_enter["CancelIoEx"] = require_post_wait_sample_before_cancel
    kernel.wait_results.append(wait_outcome)
    kernel.cancel_outcomes.append(_Outcome(True))
    kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.wait_to_deadline()

    completion = captured.value.completion
    _assert_typed_error(
        module,
        captured.value,
        reason="deadline_exceeded",
        stage="wait",
        owner=operation,
        winerror=ERROR_OPERATION_ABORTED,
        completion=completion,
    )
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="cancelled",
        bytes_transferred=None,
        winerror=ERROR_OPERATION_ABORTED,
        deadline_exceeded=True,
        cancel_outcome="accepted",
        cancel_winerror=None,
    )
    _assert_state(module, operation, "DRAINED")
    _assert_native_trace(
        kernel,
        "WaitForSingleObject",
        "CancelIoEx",
        "GetOverlappedResult",
    )
    assert len(kernel.CancelIoEx.calls) == 1
    operation.close()


def test_sticky_unproven_followups_only_poll_and_never_recancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_submit_unknown_operation(
        module=module,
        kernel=kernel,
        pipe_handle=0x3E09,
        event_handle=0x2E09,
        absolute_deadline=11.0,
    )
    clock.now = 12.0
    kernel.cancel_outcomes.append(_Outcome(False, ERROR_NOT_FOUND))
    kernel.wait_results.extend([WAIT_TIMEOUT, WAIT_TIMEOUT, WAIT_OBJECT_0])
    kernel.result_outcomes.extend(
        [
            _Outcome(True, byte_count=0xAAAA),
            _Outcome(False, ERROR_IO_INCOMPLETE),
            _Outcome(True, byte_count=0xBBBB),
        ]
    )
    kernel.native_trace.clear()

    for _ in range(2):
        with pytest.raises(module.OverlappedPipeError) as pending:
            operation.cancel_and_drain()
        _assert_typed_error(
            module,
            pending.value,
            reason="drain_pending",
            stage="drain",
            owner=operation,
            winerror=None,
            completion=None,
        )
        _assert_state(module, operation, "SUBMIT_UNKNOWN")
        assert operation.completion is None

    with pytest.raises(module.OverlappedPipeError) as terminal:
        operation.cancel_and_drain()

    completion = terminal.value.completion
    _assert_typed_error(
        module,
        terminal.value,
        reason="deadline_exceeded",
        stage="drain",
        owner=operation,
        winerror=None,
        completion=completion,
    )
    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
        deadline_exceeded=True,
        cancel_outcome="not_found",
        cancel_winerror=ERROR_NOT_FOUND,
    )
    assert kernel.WaitForSingleObject.calls == [
        (0x2E09, 0),
        (0x2E09, 0),
        (0x2E09, 0),
    ]
    assert len(kernel.CancelIoEx.calls) == 1
    _assert_native_trace(
        kernel,
        "CancelIoEx",
        "WaitForSingleObject",
        "GetOverlappedResult",
        "WaitForSingleObject",
        "GetOverlappedResult",
        "WaitForSingleObject",
        "GetOverlappedResult",
    )
    _assert_state(module, operation, "DRAINED")
    operation.close()


def test_sync_write_cancelled_result_returns_drained_owner_and_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(0x2E0A)
    kernel.write_outcomes.append(_Outcome(True))
    kernel.result_outcomes.append(_Outcome(False, ERROR_OPERATION_ABORTED))
    module = _load_subject(monkeypatch, kernel)

    operation = module.OwnedOverlappedPipeOperation.start_write(
        pipe_handle=0x3E0A,
        immutable_frame=FRAME,
        absolute_deadline=_future_deadline(),
    )

    completion = operation.completion
    _assert_state(module, operation, "DRAINED")
    _assert_completion(
        module,
        completion,
        operation="write",
        native_outcome="cancelled",
        bytes_transferred=None,
        winerror=ERROR_OPERATION_ABORTED,
    )
    _assert_native_trace(
        kernel,
        "CreateEventW",
        "WriteFile",
        "GetOverlappedResult",
    )
    assert kernel.WaitForSingleObject.calls == []
    assert kernel.CancelIoEx.calls == []
    before = _native_call_counts(kernel)
    assert operation.wait_to_deadline() is completion
    assert operation.cancel_and_drain() is completion
    _assert_no_native_calls_after(kernel, before)
    operation.close()


@pytest.mark.parametrize(
    "terminal_mode",
    ["native-failure", "byte-mismatch", "deadline"],
)
def test_terminal_error_replay_is_fresh_and_uses_current_authority_owner(
    monkeypatch: pytest.MonkeyPatch,
    terminal_mode: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)

    if terminal_mode == "native-failure":
        kernel.event_handles.append(0x2E0B)
        kernel.connect_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
        with pytest.raises(module.OverlappedPipeError) as initial:
            module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=0x3E0B,
                absolute_deadline=_future_deadline(),
            )
        reason = "native_failure"
        stage = "submit"
        winerror = ERROR_ACCESS_DENIED
    elif terminal_mode == "byte-mismatch":
        owner = _start_pending_operation(
            module=module,
            kernel=kernel,
            operation_kind="write",
            pipe_handle=0x3E0B,
            event_handle=0x2E0B,
        )
        kernel.wait_results.append(WAIT_OBJECT_0)
        kernel.result_outcomes.append(_Outcome(True, byte_count=len(FRAME) - 1))
        with pytest.raises(module.OverlappedPipeError) as initial:
            owner.wait_to_deadline()
        reason = "byte_count_mismatch"
        stage = "result"
        winerror = None
    else:
        clock = _ManualClock(10.0)
        monkeypatch.setattr(module, "_monotonic", clock)
        owner = _start_pending_operation(
            module=module,
            kernel=kernel,
            operation_kind="connect",
            pipe_handle=0x3E0B,
            event_handle=0x2E0B,
            absolute_deadline=11.0,
        )
        clock.now = 12.0
        kernel.cancel_outcomes.append(_Outcome(False, ERROR_NOT_FOUND))
        kernel.result_outcomes.append(_Outcome(True, byte_count=0xCCCC))
        with pytest.raises(module.OverlappedPipeError) as initial:
            owner.wait_to_deadline()
        reason = "deadline_exceeded"
        stage = "wait"
        winerror = None

    source = initial.value.resource_owner
    completion = initial.value.completion
    assert source is not None
    _assert_typed_error(
        module,
        initial.value,
        reason=reason,
        stage=stage,
        owner=source,
        winerror=winerror,
        completion=completion,
    )
    _assert_state(module, source, "DRAINED")
    assert type(completion) is module.Completion
    assert source.completion is completion
    if terminal_mode == "native-failure":
        _assert_completion(
            module,
            completion,
            operation="connect",
            native_outcome="failure",
            bytes_transferred=None,
            winerror=ERROR_ACCESS_DENIED,
        )
    elif terminal_mode == "byte-mismatch":
        _assert_completion(
            module,
            completion,
            operation="write",
            native_outcome="success",
            bytes_transferred=len(FRAME) - 1,
            winerror=None,
        )
    else:
        _assert_completion(
            module,
            completion,
            operation="connect",
            native_outcome="success",
            bytes_transferred=None,
            winerror=None,
            deadline_exceeded=True,
            cancel_outcome="not_found",
            cancel_winerror=ERROR_NOT_FOUND,
        )

    def assert_no_cached_error_or_traceback(
        owner: object,
        *known_errors: BaseException,
    ) -> None:
        traceback = initial.value.__traceback__
        assert traceback is not None
        traceback_type = type(traceback)
        assert _reachable_instances(
            owner,
            module.OverlappedPipeError,
            max_depth=10,
        ) == ()
        assert _reachable_instances(
            owner,
            traceback_type,
            max_depth=10,
        ) == ()
        assert _reachable_instances(
            module,
            module.OverlappedPipeError,
            max_depth=10,
        ) == ()
        assert _reachable_instances(
            module,
            traceback_type,
            max_depth=10,
        ) == ()
        for error in (initial.value, *known_errors):
            assert not _module_strongly_references(
                module,
                error,
                max_depth=10,
            )

    assert_no_cached_error_or_traceback(source)
    before_replay = _native_call_counts(kernel)

    with pytest.raises(module.OverlappedPipeError) as same_owner_replay:
        source.cancel_and_drain()

    assert same_owner_replay.value is not initial.value
    _assert_typed_error(
        module,
        same_owner_replay.value,
        reason=reason,
        stage=stage,
        owner=source,
        winerror=winerror,
        completion=completion,
    )
    assert_no_cached_error_or_traceback(source, same_owner_replay.value)
    _assert_no_native_calls_after(kernel, before_replay)
    destination = source.transfer()
    _assert_no_native_calls_after(kernel, before_replay)
    assert source.recover_transferred_owner() is destination
    _assert_state(module, destination, "DRAINED")

    with pytest.raises(module.OverlappedPipeError) as destination_replay:
        destination.wait_to_deadline()

    assert destination_replay.value is not initial.value
    assert destination_replay.value is not same_owner_replay.value
    _assert_typed_error(
        module,
        destination_replay.value,
        reason=reason,
        stage=stage,
        owner=destination,
        winerror=winerror,
        completion=completion,
    )
    assert destination.completion is completion
    assert_no_cached_error_or_traceback(
        destination,
        same_owner_replay.value,
        destination_replay.value,
    )
    _assert_no_native_calls_after(kernel, before_replay)
    destination.close()


@pytest.mark.parametrize("origin", ["submit", "drain"])
def test_submit_and_drain_terminal_deadlines_replay_across_method_and_transfer(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)

    if origin == "submit":
        kernel.event_handles.append(0x2E1B)
        kernel.write_outcomes.append(_Outcome(True))
        kernel.result_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
        kernel.on_native_return["WriteFile"] = lambda: setattr(clock, "now", 20.0)
        with pytest.raises(module.OverlappedPipeError) as initial:
            module.OwnedOverlappedPipeOperation.start_write(
                pipe_handle=0x3E1B,
                immutable_frame=FRAME,
                absolute_deadline=20.0,
            )
        stage = "submit"
        cancel_outcome = "not_attempted"
        same_owner_method = "cancel_and_drain"
        destination_method = "wait_to_deadline"
        _assert_native_trace(
            kernel,
            "CreateEventW",
            "WriteFile",
            "GetOverlappedResult",
        )
        assert kernel.CancelIoEx.calls == []
        assert kernel.WaitForSingleObject.calls == []
    else:
        source = _start_pending_operation(
            module=module,
            kernel=kernel,
            operation_kind="write",
            pipe_handle=0x3E1B,
            event_handle=0x2E1B,
            absolute_deadline=20.0,
        )
        kernel.cancel_outcomes.append(_Outcome(True))
        kernel.result_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
        kernel.on_native_return["CancelIoEx"] = lambda: setattr(clock, "now", 20.0)
        kernel.native_trace.clear()
        with pytest.raises(module.OverlappedPipeError) as initial:
            source.cancel_and_drain()
        stage = "drain"
        cancel_outcome = "accepted"
        same_owner_method = "wait_to_deadline"
        destination_method = "cancel_and_drain"
        _assert_native_trace(kernel, "CancelIoEx", "GetOverlappedResult")
        assert len(kernel.CancelIoEx.calls) == 1
        assert kernel.WaitForSingleObject.calls == []

    source = initial.value.resource_owner
    completion = initial.value.completion
    assert source is not None
    _assert_typed_error(
        module,
        initial.value,
        reason="deadline_exceeded",
        stage=stage,
        owner=source,
        winerror=ERROR_ACCESS_DENIED,
        completion=completion,
    )
    _assert_completion(
        module,
        completion,
        operation="write",
        native_outcome="failure",
        bytes_transferred=None,
        winerror=ERROR_ACCESS_DENIED,
        deadline_exceeded=True,
        cancel_outcome=cancel_outcome,
        cancel_winerror=None,
    )
    _assert_state(module, source, "DRAINED")
    assert len(kernel.GetOverlappedResult.calls) == 1
    before_replay = _native_call_counts(kernel)

    with pytest.raises(module.OverlappedPipeError) as opposite_method_replay:
        getattr(source, same_owner_method)()

    assert opposite_method_replay.value is not initial.value
    _assert_typed_error(
        module,
        opposite_method_replay.value,
        reason="deadline_exceeded",
        stage=stage,
        owner=source,
        winerror=ERROR_ACCESS_DENIED,
        completion=completion,
    )
    _assert_no_native_calls_after(kernel, before_replay)
    destination = source.transfer()
    assert source.recover_transferred_owner() is destination
    _assert_state(module, source, "TRANSFERRED")
    _assert_state(module, destination, "DRAINED")
    _assert_no_native_calls_after(kernel, before_replay)

    with pytest.raises(module.OverlappedPipeError) as transferred_replay:
        getattr(destination, destination_method)()

    assert transferred_replay.value is not initial.value
    assert transferred_replay.value is not opposite_method_replay.value
    _assert_typed_error(
        module,
        transferred_replay.value,
        reason="deadline_exceeded",
        stage=stage,
        owner=destination,
        winerror=ERROR_ACCESS_DENIED,
        completion=completion,
    )
    assert destination.completion is completion
    _assert_no_native_calls_after(kernel, before_replay)
    destination.close()


def test_wait_failed_is_nonterminal_and_later_wait_can_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3E0C,
        event_handle=0x2E0C,
        absolute_deadline=100.0,
    )
    kernel.wait_results.append(_WaitOutcome(WAIT_FAILED, ERROR_INVALID_HANDLE))
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as failed:
        operation.wait_to_deadline()

    _assert_typed_error(
        module,
        failed.value,
        reason="native_failure",
        stage="wait",
        owner=operation,
        winerror=ERROR_INVALID_HANDLE,
        completion=None,
    )
    _assert_state(module, operation, "PENDING")
    assert operation.completion is None
    assert kernel.CancelIoEx.calls == []
    assert kernel.GetOverlappedResult.calls == []
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xDDDD))

    completion = operation.wait_to_deadline()

    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
    )
    _assert_state(module, operation, "DRAINED")
    _assert_native_trace(
        kernel,
        "WaitForSingleObject",
        "WaitForSingleObject",
        "GetOverlappedResult",
    )
    assert kernel.CancelIoEx.calls == []
    operation.close()


@pytest.mark.parametrize("interrupt_type", _INTERRUPTION_TYPES)
def test_post_gor_clock_baseexception_preserves_wait_origin_across_method_switch(
    monkeypatch: pytest.MonkeyPatch,
    interrupt_type: type[BaseException],
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ScriptedClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="write",
        pipe_handle=0x3E0D,
        event_handle=0x2E0D,
        absolute_deadline=20.0,
    )
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=len(FRAME)))
    original = interrupt_type("post-GOR clock interrupted")
    kernel.on_native_return["GetOverlappedResult"] = lambda: clock.arm_next(
        original
    )
    kernel.native_trace.clear()

    primary, handoff = _capture_outward_interruption(
        module,
        original,
        operation.wait_to_deadline,
    )
    _assert_typed_error(
        module,
        handoff,
        reason="native_failure",
        stage="result",
        owner=operation,
        winerror=None,
        completion=None,
    )
    _assert_state(module, operation, "COMPLETED")
    assert operation.completion is None
    _assert_native_trace(kernel, "WaitForSingleObject", "GetOverlappedResult")
    clock.calls.clear()
    kernel.on_native_return.pop("GetOverlappedResult")
    _assert_errors_and_tracebacks_not_cached(
        module,
        operation,
        primary,
        handoff,
    )
    before = _native_call_counts(kernel)
    kernel.native_trace.clear()
    clock.arm_next(20.0)

    with pytest.raises(module.OverlappedPipeError) as resumed:
        operation.cancel_and_drain()

    completion = resumed.value.completion
    _assert_typed_error(
        module,
        resumed.value,
        reason="deadline_exceeded",
        stage="wait",
        owner=operation,
        winerror=None,
        completion=completion,
    )
    _assert_completion(
        module,
        completion,
        operation="write",
        native_outcome="success",
        bytes_transferred=len(FRAME),
        winerror=None,
        deadline_exceeded=True,
    )
    _assert_state(module, operation, "DRAINED")
    _assert_native_trace(kernel)
    _assert_no_native_calls_after(kernel, before)
    operation.close()


@pytest.mark.parametrize(
    "fault_type",
    [RuntimeError, KeyboardInterrupt, SystemExit],
    ids=["exception", "keyboard-interrupt", "system-exit"],
)
@pytest.mark.parametrize(
    ("boundary", "phase_name", "entry_must_remain"),
    [
        ("wrapper-pre-return", "EMPTY", True),
        ("caller-assignment", "CLOSED", True),
        ("lookup-pre", "LIVE", True),
        ("lookup-post", "QUARANTINED", True),
        ("phase-pre", "EMPTY", True),
        ("phase-post", "CLOSED", True),
        ("remove-pre", "EMPTY", True),
        ("remove-post", "CLOSED", False),
    ],
)
def test_sweep_fault_boundaries_release_both_locks(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    phase_name: str,
    entry_must_remain: bool,
    fault_type: type[BaseException],
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    fault = fault_type(f"sweep-{boundary}")

    cell = _SweepCellDuck(
        boundary=(
            boundary
            if boundary in {"wrapper-pre-return", "caller-assignment"}
            else None
        ),
        fault=fault,
    )
    token = cell
    entry = _SweepEntryDuck(
        phase=getattr(module._RegistryPhase, phase_name),
        linked_identity=cell,
        boundary=(
            boundary if boundary in {"phase-pre", "phase-post"} else None
        ),
        fault=fault,
    )
    registry = _SweepRegistryDuck(
        [(token, entry)],
        call_cell=cell,
        boundary=(
            boundary
            if boundary
            in {"lookup-pre", "lookup-post", "remove-pre", "remove-post"}
            else None
        ),
        fault=fault,
    )
    entry.bind_protocol(registry, cell)

    before = _native_call_counts(kernel)
    monkeypatch.setattr(
        module,
        "_monotonic",
        lambda: pytest.fail("sweep sampled the clock"),
    )
    monkeypatch.setattr(
        module,
        "_failfast_quarantine",
        lambda: pytest.fail("sweep called fail-fast"),
    )

    previous_trace = sys.gettrace()
    try:
        with pytest.raises(fault_type) as captured:
            _invoke_sweep(
                module,
                registry,
                token,
                entry,
                cell,
            )
    finally:
        sys.settrace(previous_trace)

    assert captured.value is fault

    if entry_must_remain:
        assert registry.peek_identity(token) is entry
    else:
        assert not registry.has_identity_key(token)

    if boundary in {"wrapper-pre-return", "caller-assignment"}:
        assert registry.lookup_attempts == 0
        assert entry.phase_reads == 0
        assert registry.delete_attempts == 0
    elif boundary in {"lookup-pre", "lookup-post"}:
        assert registry.lookup_attempts == 1
        assert entry.phase_reads == 0
        assert registry.delete_attempts == 0
    elif boundary in {"phase-pre", "phase-post"}:
        assert registry.lookup_attempts >= 1
        assert entry.phase_reads == 1
        assert registry.delete_attempts == 0
    else:
        assert registry.lookup_attempts >= 1
        assert entry.phase_reads == 1
        assert registry.delete_attempts == 1

    assert cell.internal_rollbacks == (
        1 if boundary == "wrapper-pre-return" else 0
    )
    assert cell.actual_releases == (
        0 if boundary == "wrapper-pre-return" else 1
    )
    _assert_single_sweep_purpose(cell)
    _assert_sweep_locks_reusable_from_another_thread(
        registry,
        cell,
    )
    assert _native_call_counts(kernel) == before


def test_sweep_only_removes_exact_empty_or_closed_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    before = _native_call_counts(kernel)

    monkeypatch.setattr(
        module,
        "_monotonic",
        lambda: pytest.fail("sweep sampled the clock"),
    )
    monkeypatch.setattr(
        module,
        "_failfast_quarantine",
        lambda: pytest.fail("sweep called fail-fast"),
    )

    by_outcome: dict[str, list[object]] = {
        "REMOVED": [],
        "SKIPPED": [],
    }

    for phase_name, expected_outcome in (
        ("EMPTY", "REMOVED"),
        ("CLOSED", "REMOVED"),
        ("LIVE", "SKIPPED"),
        ("QUARANTINED", "SKIPPED"),
    ):
        cell = _SweepCellDuck()
        token = cell
        entry = _SweepEntryDuck(
            phase=getattr(module._RegistryPhase, phase_name),
            linked_identity=cell,
        )
        registry = _SweepRegistryDuck(
            [(token, entry)],
            call_cell=cell,
        )
        entry.bind_protocol(registry, cell)

        result = _invoke_sweep(
            module,
            registry,
            token,
            entry,
            cell,
        )

        _assert_sweep_outcome(module, result, expected_outcome)
        by_outcome[expected_outcome].append(result)
        assert entry.phase_reads == 1
        assert registry.delete_attempts == (
            1 if expected_outcome == "REMOVED" else 0
        )

        if expected_outcome == "REMOVED":
            assert not registry.has_identity_key(token)
        else:
            assert registry.peek_identity(token) is entry

        assert cell.internal_rollbacks == 0
        assert cell.actual_releases == 1
        _assert_single_sweep_purpose(cell)
        _assert_sweep_locks_reusable_from_another_thread(
            registry,
            cell,
        )

    assert by_outcome["REMOVED"][0] is by_outcome["REMOVED"][1]
    assert by_outcome["SKIPPED"][0] is by_outcome["SKIPPED"][1]
    assert by_outcome["REMOVED"][0] is not by_outcome["SKIPPED"][0]
    assert _native_call_counts(kernel) == before


def test_sweep_failed_try_acquire_skips_without_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    cell = _SweepCellDuck(contended=True)
    token = cell
    entry = _SweepEntryDuck(
        phase=module._RegistryPhase.EMPTY,
        linked_identity=cell,
    )
    registry = _SweepRegistryDuck(
        [(token, entry)],
        call_cell=cell,
    )
    entry.bind_protocol(registry, cell)

    result = _invoke_sweep(
        module,
        registry,
        token,
        entry,
        cell,
    )

    _assert_sweep_outcome(module, result, "SKIPPED")
    assert registry.peek_identity(token) is entry
    assert registry.lookup_attempts == 0
    assert entry.phase_reads == 0
    assert registry.delete_attempts == 0
    assert cell.actual_releases == 0
    _assert_single_sweep_purpose(cell)
    _assert_sweep_locks_reusable_from_another_thread(
        registry,
        cell,
    )


def test_sweep_rechecks_identity_after_phase_before_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    cell = _SweepCellDuck()
    token = cell
    expected = _SweepEntryDuck(
        phase=module._RegistryPhase.EMPTY,
        linked_identity=cell,
        equality_key="same-entry",
    )
    replacement = _SweepEntryDuck(
        phase=module._RegistryPhase.LIVE,
        linked_identity=cell,
        equality_key="same-entry",
    )
    assert replacement == expected
    assert replacement is not expected
    registry = _SweepRegistryDuck(
        [(token, expected)],
        call_cell=cell,
    )
    expected.bind_protocol(registry, cell)
    replacement.bind_protocol(registry, cell)
    expected.after_phase_read = lambda: registry.replace_direct(
        token,
        replacement,
    )

    result = _invoke_sweep(
        module,
        registry,
        token,
        expected,
        cell,
    )

    _assert_sweep_outcome(module, result, "SKIPPED")
    assert registry.peek_identity(token) is replacement
    assert expected.phase_reads == 1
    assert replacement.phase_reads == 0
    assert registry.delete_attempts == 0
    assert cell.actual_releases == 1
    _assert_single_sweep_purpose(cell)
    _assert_sweep_locks_reusable_from_another_thread(
        registry,
        cell,
    )


def test_sweep_remove_after_fault_never_reinserts_or_deletes_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    fault = KeyboardInterrupt("remove-after")
    cell = _SweepCellDuck()
    token = cell
    expected = _SweepEntryDuck(
        phase=module._RegistryPhase.CLOSED,
        linked_identity=cell,
        equality_key="same-entry",
    )
    replacement = _SweepEntryDuck(
        phase=module._RegistryPhase.LIVE,
        linked_identity=cell,
        equality_key="same-entry",
    )
    assert replacement == expected
    assert replacement is not expected
    registry = _SweepRegistryDuck(
        [(token, expected)],
        call_cell=cell,
        boundary="remove-post",
        fault=fault,
    )
    expected.bind_protocol(registry, cell)
    replacement.bind_protocol(registry, cell)

    with pytest.raises(KeyboardInterrupt) as captured:
        _invoke_sweep(
            module,
            registry,
            token,
            expected,
            cell,
        )

    assert captured.value is fault
    assert not registry.has_identity_key(token)
    assert expected.phase_reads == 1
    assert registry.delete_attempts == 1

    registry.boundary = None
    registry.fault = None
    registry.install_direct(token, replacement)

    result = _invoke_sweep(
        module,
        registry,
        token,
        expected,
        cell,
    )

    _assert_sweep_outcome(module, result, "SKIPPED")
    assert registry.peek_identity(token) is replacement
    assert expected.phase_reads == 1
    assert replacement.phase_reads == 0
    assert registry.delete_attempts == 1
    assert cell.actual_releases == 2
    assert len(cell.try_purposes) == 2
    assert len(cell.release_purposes) == 2
    assert all(
        attempted is released
        for attempted, released in zip(
            cell.try_purposes,
            cell.release_purposes,
            strict=True,
        )
    )
    _assert_sweep_locks_reusable_from_another_thread(
        registry,
        cell,
    )


@pytest.mark.parametrize("mismatch", ["token", "entry", "cell"])
def test_sweep_identity_mismatch_never_deletes_current_entry(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    actual_cell = _SweepCellDuck(
        equality_key="same" if mismatch in {"token", "cell"} else None,
    )
    stored_token: object = actual_cell
    call_token: object = actual_cell
    call_cell = actual_cell
    expected = _SweepEntryDuck(
        phase=module._RegistryPhase.EMPTY,
        linked_identity=actual_cell,
        equality_key="same-entry" if mismatch == "entry" else None,
    )
    current = expected

    if mismatch == "token":
        call_token = _SweepCellDuck(equality_key="same")
        assert call_token == stored_token
        assert call_token is not stored_token
    elif mismatch == "entry":
        current = _SweepEntryDuck(
            phase=module._RegistryPhase.LIVE,
            linked_identity=actual_cell,
            equality_key="same-entry",
        )
        assert current == expected
        assert current is not expected
    else:
        call_cell = _SweepCellDuck(equality_key="same")
        assert call_cell == actual_cell
        assert call_cell is not actual_cell

    registry = _SweepRegistryDuck(
        [(stored_token, current)],
        call_cell=call_cell,
    )
    expected.bind_protocol(registry, call_cell)
    if current is not expected:
        current.bind_protocol(registry, call_cell)

    result = _invoke_sweep(
        module,
        registry,
        call_token,
        expected,
        call_cell,
    )

    _assert_sweep_outcome(module, result, "SKIPPED")
    assert registry.peek_identity(stored_token) is current
    assert registry.delete_attempts == 0
    assert expected.phase_reads == 0
    if current is not expected:
        assert current.phase_reads == 0

    assert call_cell.actual_releases == 1
    _assert_single_sweep_purpose(call_cell)
    _assert_sweep_locks_reusable_from_another_thread(
        registry,
        call_cell,
    )


def test_same_thread_ordinary_reentry_is_rejected_without_poisoning_cell_lock(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegated = _delegate_sweep_reentry_node_to_subprocess(request)
    if delegated is not None:
        assert "TASK_3B2_SWEEP_REENTRY_OK" in delegated.stdout
        return

    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3F01,
        event_handle=0x2F01,
    )
    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xD101))
    rejected: list[BaseException] = []
    returned: list[object] = []

    def attempt_same_thread_reentry() -> None:
        kernel.on_native_enter.pop(
            "WaitForSingleObject",
            None,
        )
        before = _native_call_counts(kernel)

        for _ in range(2):
            try:
                returned.append(operation.transfer())
            except BaseException as error:
                rejected.append(error)

            assert returned == []
            _assert_state(module, operation, "PENDING")
            assert _native_call_counts(kernel) == before

    kernel.on_native_enter[
        "WaitForSingleObject"
    ] = attempt_same_thread_reentry
    kernel.native_trace.clear()

    completion = operation.wait_to_deadline()

    assert returned == []
    assert len(rejected) == 2
    assert type(rejected[0]) is type(rejected[1])
    assert rejected[0].args == rejected[1].args
    _assert_state(module, operation, "DRAINED")
    assert operation.completion is completion
    _assert_native_trace(
        kernel,
        "WaitForSingleObject",
        "GetOverlappedResult",
    )

    successor = operation.transfer()
    _assert_state(module, operation, "TRANSFERRED")
    assert operation.recover_transferred_owner() is successor
    _assert_state(module, successor, "DRAINED")
    successor.close()
    _assert_state(module, successor, "CLOSED")
    print("TASK_3B2_SWEEP_REENTRY_OK", flush=True)


def test_real_pending_connect_reaches_terminal_after_client_arrives(
    request: pytest.FixtureRequest,
) -> None:
    delegated = _delegate_real_pipe_node_to_subprocess(request)
    if delegated is not None:
        return

    module = _load_real_subject()
    pipe = _RealNamedPipe()
    owner = None
    try:
        try:
            owner = module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=pipe.server_handle,
                absolute_deadline=time.monotonic() + 5.0,
            )
        except module.OverlappedPipeError as error:
            owner = error.resource_owner
            raise
        _assert_state(module, owner, "PENDING")
        assert owner.completion is None

        pipe.open_client()
        completion = owner.wait_to_deadline()

        _assert_completion(
            module,
            completion,
            operation="connect",
            native_outcome="success",
            bytes_transferred=None,
            winerror=None,
        )
        _assert_state(module, owner, "DRAINED")
        owner.close()
    finally:
        _finalize_real_pipe_owner(module, owner, pipe)
        pipe.close()


def test_real_client_before_accept_exercises_pipe_connected_path(
    request: pytest.FixtureRequest,
) -> None:
    delegated = _delegate_real_pipe_node_to_subprocess(request)
    if delegated is not None:
        return

    module = _load_real_subject()
    pipe = _RealNamedPipe()
    try:
        client_handle = _connect_real_client_before_accept(module, pipe)
        assert client_handle not in {0, UINTPTR_MAX}
    finally:
        pipe.close()


def test_real_connected_pipe_synchronously_writes_exact_seven_bytes(
    request: pytest.FixtureRequest,
) -> None:
    delegated = _delegate_real_pipe_node_to_subprocess(request)
    if delegated is not None:
        return

    module = _load_real_subject()
    pipe = _RealNamedPipe()
    owner = None
    try:
        client_handle = _connect_real_client_before_accept(module, pipe)
        try:
            owner = module.OwnedOverlappedPipeOperation.start_write(
                pipe_handle=pipe.server_handle,
                immutable_frame=FRAME,
                absolute_deadline=time.monotonic() + 5.0,
            )
        except module.OverlappedPipeError as error:
            owner = error.resource_owner
            raise

        _assert_state(module, owner, "DRAINED")
        completion = owner.completion
        _assert_completion(
            module,
            completion,
            operation="write",
            native_outcome="success",
            bytes_transferred=7,
            winerror=None,
        )
        assert (
            _read_real_pipe_exact_with_timeout(pipe, client_handle, 7)
            == FRAME
        )
        owner.close()
    finally:
        _finalize_real_pipe_owner(module, owner, pipe)
        pipe.close()


def test_real_256k_pending_write_cancel_drains_exact_native_terminal(
    request: pytest.FixtureRequest,
) -> None:
    delegated = _delegate_real_pipe_node_to_subprocess(request)
    if delegated is not None:
        return

    module = _load_real_subject()
    pipe = _RealNamedPipe(outbound_buffer_bytes=4096)
    owner = None
    try:
        client_handle = _connect_real_client_before_accept(module, pipe)
        frame = b"x" * (256 * 1024)
        try:
            owner = module.OwnedOverlappedPipeOperation.start_write(
                pipe_handle=pipe.server_handle,
                immutable_frame=frame,
                absolute_deadline=time.monotonic() + 10.0,
            )
        except module.OverlappedPipeError as error:
            owner = error.resource_owner
            raise
        _assert_state(module, owner, "PENDING")

        completion = owner.cancel_and_drain()

        _assert_state(module, owner, "DRAINED")
        assert type(completion) is module.Completion
        assert completion.operation is module.OperationKind.WRITE
        assert completion.deadline_exceeded is False
        assert completion.cancel_outcome in {
            module.CancelOutcome.ACCEPTED,
            module.CancelOutcome.NOT_FOUND,
        }
        if completion.cancel_outcome is module.CancelOutcome.ACCEPTED:
            assert completion.cancel_winerror is None
        else:
            assert completion.cancel_winerror == ERROR_NOT_FOUND

        if completion.native_outcome is module.NativeOutcome.CANCELLED:
            assert completion.bytes_transferred is None
            assert completion.winerror == ERROR_OPERATION_ABORTED
        else:
            assert completion.native_outcome is module.NativeOutcome.SUCCESS
            assert completion.bytes_transferred == len(frame)
            assert completion.winerror is None

            assert _read_real_pipe_exact_with_timeout(
                pipe,
                client_handle,
                len(frame),
            ) == frame

        owner.close()
    finally:
        _finalize_real_pipe_owner(module, owner, pipe)
        pipe.close()


def test_real_two_pending_connects_complete_in_reverse_without_crosstalk(
    request: pytest.FixtureRequest,
) -> None:
    delegated = _delegate_real_pipe_node_to_subprocess(request)
    if delegated is not None:
        return

    module = _load_real_subject()
    first_pipe = _RealNamedPipe()
    second_pipe = _RealNamedPipe()
    first = None
    second = None
    try:
        try:
            first = module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=first_pipe.server_handle,
                absolute_deadline=time.monotonic() + 20.0,
            )
        except module.OverlappedPipeError as error:
            first = error.resource_owner
            raise
        try:
            second = module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=second_pipe.server_handle,
                absolute_deadline=time.monotonic() + 20.0,
            )
        except module.OverlappedPipeError as error:
            second = error.resource_owner
            raise
        _assert_state(module, first, "PENDING")
        _assert_state(module, second, "PENDING")
        assert first_pipe.server_handle != second_pipe.server_handle
        assert first_pipe.name != second_pipe.name

        second_client = second_pipe.open_client()
        second_completion = second.wait_to_deadline()
        _assert_completion(
            module,
            second_completion,
            operation="connect",
            native_outcome="success",
            bytes_transferred=None,
            winerror=None,
        )
        _assert_state(module, second, "DRAINED")

        _assert_state(module, first, "PENDING")
        assert first.completion is None

        first_client = first_pipe.open_client()
        first_completion = first.wait_to_deadline()
        _assert_completion(
            module,
            first_completion,
            operation="connect",
            native_outcome="success",
            bytes_transferred=None,
            winerror=None,
        )
        _assert_state(module, first, "DRAINED")
        assert first_client != second_client

        first.close()
        second.close()
    finally:
        _finalize_real_pipe_owner(module, first, first_pipe)
        _finalize_real_pipe_owner(module, second, second_pipe)
        first_pipe.close()
        second_pipe.close()


def test_write_false_zero_is_submit_unknown_and_never_terminalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.append(0x2F20)
    kernel.write_outcomes.append(_Outcome(False, 0))
    module = _load_subject(monkeypatch, kernel)

    with pytest.raises(module.OverlappedPipeError) as captured:
        module.OwnedOverlappedPipeOperation.start_write(
            pipe_handle=0x3F20,
            immutable_frame=FRAME,
            absolute_deadline=_future_deadline(),
        )

    owner = captured.value.resource_owner
    _assert_typed_error(
        module,
        captured.value,
        reason="native_failure",
        stage="submit",
        owner=owner,
        winerror=None,
        completion=None,
    )
    _assert_state(module, owner, "SUBMIT_UNKNOWN")
    assert owner.completion is None
    assert kernel.write_payloads == [FRAME]
    assert kernel.write_count_pointers == [0]
    _assert_native_trace(kernel, "CreateEventW", "WriteFile")
    assert kernel.WaitForSingleObject.calls == []
    assert kernel.GetOverlappedResult.calls == []
    assert kernel.CancelIoEx.calls == []
    assert kernel.CloseHandle.calls == []


def test_wait_failed_zero_is_nonterminal_and_uses_none_winerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    operation = _start_pending_operation(
        module=module,
        kernel=kernel,
        operation_kind="connect",
        pipe_handle=0x3F21,
        event_handle=0x2F21,
    )
    kernel.wait_results.append(_WaitOutcome(WAIT_FAILED, 0))
    real_get_last_error = ctypes.get_last_error
    last_error_calls = 0

    def counted_get_last_error() -> int:
        nonlocal last_error_calls
        last_error_calls += 1
        return real_get_last_error()

    monkeypatch.setattr(ctypes, "get_last_error", counted_get_last_error)
    kernel.native_trace.clear()

    with pytest.raises(module.OverlappedPipeError) as captured:
        operation.wait_to_deadline()

    _assert_typed_error(
        module,
        captured.value,
        reason="native_failure",
        stage="wait",
        owner=operation,
        winerror=None,
        completion=None,
    )
    _assert_state(module, operation, "PENDING")
    assert operation.completion is None
    assert last_error_calls == 1
    _assert_native_trace(kernel, "WaitForSingleObject")
    assert kernel.GetOverlappedResult.calls == []
    assert kernel.CancelIoEx.calls == []

    kernel.wait_results.append(WAIT_OBJECT_0)
    kernel.result_outcomes.append(_Outcome(True, byte_count=0xF021))
    completion = operation.wait_to_deadline()

    _assert_completion(
        module,
        completion,
        operation="connect",
        native_outcome="success",
        bytes_transferred=None,
        winerror=None,
    )
    assert last_error_calls == 1
    _assert_native_trace(
        kernel,
        "WaitForSingleObject",
        "WaitForSingleObject",
        "GetOverlappedResult",
    )
    operation.close()


@pytest.mark.parametrize("terminal_kind", ["return", "raise"])
def test_close_false_zero_quarantines_once_and_preserves_terminal_replay(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    terminal_kind: str,
) -> None:
    delegated = _delegate_quarantine_node_to_subprocess(
        request,
        expected_exit_code=0,
    )
    if delegated is not None:
        return

    kernel = _FakeKernel32()
    kernel.event_handles.append(0x2F22)
    if terminal_kind == "return":
        kernel.connect_outcomes.append(_Outcome(True))
    else:
        kernel.connect_outcomes.append(_Outcome(False, ERROR_ACCESS_DENIED))
    kernel.close_outcomes.append(_Outcome(False, 0))
    module = _load_subject(monkeypatch, kernel)

    if terminal_kind == "return":
        source = module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x3F22,
            absolute_deadline=_future_deadline(),
        )
        completion = source.completion
    else:
        with pytest.raises(module.OverlappedPipeError) as initial:
            module.OwnedOverlappedPipeOperation.start_connect(
                pipe_handle=0x3F22,
                absolute_deadline=_future_deadline(),
            )
        source = initial.value.resource_owner
        completion = initial.value.completion

    assert source is not None
    _assert_state(module, source, "DRAINED")
    real_get_last_error = ctypes.get_last_error
    last_error_calls = 0

    def counted_get_last_error() -> int:
        nonlocal last_error_calls
        last_error_calls += 1
        return real_get_last_error()

    monkeypatch.setattr(ctypes, "get_last_error", counted_get_last_error)

    with pytest.raises(module.OverlappedPipeError) as close_error:
        source.close()

    _assert_typed_error(
        module,
        close_error.value,
        reason="close_quarantined",
        stage="close",
        owner=source,
        winerror=None,
        completion=completion,
    )
    _assert_state(module, source, "CLOSE_FAILED_QUARANTINED")
    assert last_error_calls == 1
    assert len(kernel.CloseHandle.calls) == 1
    before = _native_call_counts(kernel)

    def assert_terminal_replay(active: object) -> None:
        if terminal_kind == "return":
            assert active.wait_to_deadline() is completion
            assert active.cancel_and_drain() is completion
        else:
            replayed: list[BaseException] = []
            for action in (
                active.wait_to_deadline,
                active.cancel_and_drain,
            ):
                with pytest.raises(module.OverlappedPipeError) as replay:
                    action()
                replayed.append(replay.value)
                _assert_typed_error(
                    module,
                    replay.value,
                    reason="native_failure",
                    stage="submit",
                    owner=active,
                    winerror=ERROR_ACCESS_DENIED,
                    completion=completion,
                )
            assert replayed[0] is not replayed[1]

    assert_terminal_replay(source)
    _assert_no_native_calls_after(kernel, before)

    destination = source.transfer()
    assert source.recover_transferred_owner() is destination
    _assert_state(module, destination, "CLOSE_FAILED_QUARANTINED")
    assert_terminal_replay(destination)

    with pytest.raises(module.OverlappedPipeError) as repeated_close:
        destination.close()

    _assert_typed_error(
        module,
        repeated_close.value,
        reason="close_quarantined",
        stage="close",
        owner=destination,
        winerror=None,
        completion=completion,
    )
    assert last_error_calls == 1
    _assert_no_native_calls_after(kernel, before)


@pytest.mark.parametrize(
    "live_state",
    ["RESULT_REQUIRED", "COMPLETED", "CANCEL_UNKNOWN"],
)
def test_close_rejects_every_live_state_that_still_requires_recovery(
    monkeypatch: pytest.MonkeyPatch,
    live_state: str,
) -> None:
    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)

    if live_state == "RESULT_REQUIRED":
        owner, _ = _start_result_required_after_clock_interrupt(
            monkeypatch=monkeypatch,
            module=module,
            kernel=kernel,
            pipe_handle=0x3F23,
            event_handle=0x2F23,
            absolute_deadline=100.0,
        )
    elif live_state == "COMPLETED":
        clock = _ScriptedClock(10.0)
        clock.arm_next(10.0, 10.0, 10.0, KeyboardInterrupt())
        monkeypatch.setattr(module, "_monotonic", clock)
        kernel.event_handles.append(0x2F23)
        kernel.write_outcomes.append(_Outcome(True))
        kernel.result_outcomes.append(_Outcome(True, byte_count=len(FRAME)))
        with pytest.raises(BaseExceptionGroup) as interrupted:
            module.OwnedOverlappedPipeOperation.start_write(
                pipe_handle=0x3F23,
                immutable_frame=FRAME,
                absolute_deadline=100.0,
            )
        _, handoff = _assert_interrupt_group(
            module,
            interrupted.value,
            KeyboardInterrupt,
        )
        owner = handoff.resource_owner
    else:
        owner = _start_pending_operation(
            module=module,
            kernel=kernel,
            operation_kind="connect",
            pipe_handle=0x3F23,
            event_handle=0x2F23,
            absolute_deadline=_future_deadline(),
        )
        kernel.cancel_outcomes.append(
            _Outcome(True, interrupt=KeyboardInterrupt())
        )
        with pytest.raises(BaseExceptionGroup):
            owner.cancel_and_drain()

    assert owner is not None
    _assert_state(module, owner, live_state)
    assert owner.completion is None
    before = _native_call_counts(kernel)

    with pytest.raises(module.OverlappedPipeError) as rejected:
        owner.close()

    _assert_typed_error(
        module,
        rejected.value,
        reason="lifecycle_violation",
        stage="close",
        owner=owner,
        winerror=None,
        completion=None,
    )
    _assert_state(module, owner, live_state)
    _assert_no_native_calls_after(kernel, before)

    if live_state == "RESULT_REQUIRED":
        kernel.result_outcomes.append(_Outcome(True, byte_count=len(FRAME)))
        completion = owner.wait_to_deadline()
        _assert_completion(
            module,
            completion,
            operation="write",
            native_outcome="success",
            bytes_transferred=len(FRAME),
            winerror=None,
        )
    elif live_state == "COMPLETED":
        completion = owner.wait_to_deadline()
        _assert_completion(
            module,
            completion,
            operation="write",
            native_outcome="success",
            bytes_transferred=len(FRAME),
            winerror=None,
        )
    else:
        kernel.wait_results.append(WAIT_OBJECT_0)
        kernel.result_outcomes.append(
            _Outcome(False, ERROR_OPERATION_ABORTED)
        )
        completion = owner.cancel_and_drain()
        _assert_completion(
            module,
            completion,
            operation="connect",
            native_outcome="cancelled",
            bytes_transferred=None,
            winerror=ERROR_OPERATION_ABORTED,
            cancel_outcome="unknown",
            cancel_winerror=None,
        )

    _assert_state(module, owner, "DRAINED")
    owner.close()


@pytest.mark.parametrize(
    ("close_mode", "expected_state"),
    [
        ("false", "CLOSE_FAILED_QUARANTINED"),
        ("false-zero", "CLOSE_FAILED_QUARANTINED"),
        ("return-unknown", "CLOSE_UNKNOWN"),
    ],
)
def test_new_close_quarantine_without_terminal_record_allows_only_transfer(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    close_mode: str,
    expected_state: str,
) -> None:
    delegated = _delegate_quarantine_node_to_subprocess(
        request,
        expected_exit_code=0,
    )
    if delegated is not None:
        return

    kernel = _FakeKernel32()
    module = _load_subject(monkeypatch, kernel)
    clock = _ManualClock(10.0)
    monkeypatch.setattr(module, "_monotonic", clock)
    kernel.event_handles.append(0x2F24)
    kernel.on_native_return["CreateEventW"] = lambda: setattr(
        clock,
        "now",
        20.0,
    )

    with pytest.raises(module.OverlappedPipeError) as presubmit_deadline:
        module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x3F24,
            absolute_deadline=20.0,
        )

    source = presubmit_deadline.value.resource_owner
    assert source is not None
    _assert_state(module, source, "NEW")
    real_get_last_error = ctypes.get_last_error
    last_error_calls = 0

    def counted_get_last_error() -> int:
        nonlocal last_error_calls
        last_error_calls += 1
        return real_get_last_error()

    monkeypatch.setattr(ctypes, "get_last_error", counted_get_last_error)
    if close_mode in {"false", "false-zero"}:
        close_winerror = ERROR_INVALID_HANDLE if close_mode == "false" else 0
        kernel.close_outcomes.append(_Outcome(False, close_winerror))
        with pytest.raises(module.OverlappedPipeError) as closed:
            source.close()
        close_error = closed.value
        expected_winerror = close_winerror or None
    else:
        kernel.close_outcomes.append(
            _Outcome(True, interrupt=KeyboardInterrupt())
        )
        with pytest.raises(BaseExceptionGroup) as closed_group:
            source.close()
        _, close_error = _assert_interrupt_group(
            module,
            closed_group.value,
            KeyboardInterrupt,
        )
        expected_winerror = None

    _assert_typed_error(
        module,
        close_error,
        reason="close_quarantined",
        stage="close",
        owner=source,
        winerror=expected_winerror,
        completion=None,
    )
    _assert_state(module, source, expected_state)
    assert len(kernel.CloseHandle.calls) == 1
    expected_last_error_calls = 1 if close_mode != "return-unknown" else 0
    assert last_error_calls == expected_last_error_calls

    def assert_only_transfer_is_legal(active: object) -> None:
        state_before = active.state
        before = _native_call_counts(kernel)
        for action, stage in (
            (active.wait_to_deadline, "wait"),
            (active.cancel_and_drain, "drain"),
        ):
            with pytest.raises(module.OverlappedPipeError) as rejected:
                action()
            _assert_typed_error(
                module,
                rejected.value,
                reason="lifecycle_violation",
                stage=stage,
                owner=active,
                winerror=None,
                completion=None,
            )

        with pytest.raises(module.OverlappedPipeError) as repeated_close:
            active.close()
        _assert_typed_error(
            module,
            repeated_close.value,
            reason="close_quarantined",
            stage="close",
            owner=active,
            winerror=expected_winerror,
            completion=None,
        )
        assert active.state is state_before
        _assert_no_native_calls_after(kernel, before)

    assert_only_transfer_is_legal(source)
    destination = source.transfer()
    assert source.recover_transferred_owner() is destination
    _assert_state(module, source, "TRANSFERRED")
    _assert_state(module, destination, expected_state)
    assert_only_transfer_is_legal(destination)
    assert len(kernel.CloseHandle.calls) == 1
    assert last_error_calls == expected_last_error_calls


@pytest.mark.parametrize("fault_position", ["pre", "post"])
@pytest.mark.parametrize(
    "interrupt_type",
    [RuntimeError, KeyboardInterrupt, SystemExit],
)
def test_public_closed_remove_fault_keeps_closed_owner_idempotent_and_sweepable(
    monkeypatch: pytest.MonkeyPatch,
    fault_position: str,
    interrupt_type: type[BaseException],
) -> None:
    kernel = _FakeKernel32()
    kernel.event_handles.extend([0x2B41, 0x2B42])
    kernel.connect_outcomes.extend([_Outcome(True), _Outcome(True)])
    module = _load_subject(monkeypatch, kernel)
    owner = module.OwnedOverlappedPipeOperation.start_connect(
        pipe_handle=0x3B41,
        absolute_deadline=_future_deadline(),
    )
    _assert_state(module, owner, "DRAINED")
    completion = owner.completion

    real_commit = module._commit_registry_transition
    real_sweep = module._try_sweep_candidate
    original = interrupt_type(
        f"CLOSED_REMOVE {fault_position} interrupted"
    )
    target_attempts: list[object] = []
    target_transition: object | None = None

    def faulting_commit(
        registry: object,
        transition: object,
    ) -> object:
        nonlocal target_transition
        if (
            _registry_status(transition.mode) != "CLOSED_REMOVE"
            or target_transition is not None
        ):
            return real_commit(registry, transition)

        target_transition = transition
        target_attempts.append(transition)
        if fault_position == "pre":
            raise original

        result = real_commit(registry, transition)
        _assert_registry_transition_result(module, result, "COMMITTED")
        raise original

    monkeypatch.setattr(
        module,
        "_commit_registry_transition",
        faulting_commit,
    )

    handoff = _capture_public_registry_fault(
        module,
        owner.close,
        original,
        interrupt_type,
        reason="native_failure",
        stage="close",
        owner=owner,
        completion=completion,
    )

    assert target_transition is not None
    assert target_attempts == [target_transition]
    assert _registry_status(target_transition.consume_state.value) == (
        "NEVER" if fault_position == "pre" else "CONSUMED"
    )
    _assert_state(module, owner, "CLOSED")
    assert len(kernel.CloseHandle.calls) == 1

    monkeypatch.setattr(
        module,
        "_commit_registry_transition",
        real_commit,
    )
    _assert_errors_and_tracebacks_not_cached(
        module,
        owner,
        original,
        handoff,
    )

    public_sweep_results: list[str] = []

    def recording_sweep(
        registry: object,
        entry_token: object,
        expected_entry: object,
        expected_cell: object,
    ) -> object:
        result = real_sweep(
            registry,
            entry_token,
            expected_entry,
            expected_cell,
        )
        if expected_entry is target_transition.expected_entry:
            _assert_sweep_outcome(module, result, "REMOVED")
            public_sweep_results.append(_registry_status(result))
        return result

    monkeypatch.setattr(
        module,
        "_try_sweep_candidate",
        recording_sweep,
    )

    def public_probe() -> object:
        owner.close()
        owner.close()
        followup = module.OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=0x3B42,
            absolute_deadline=_future_deadline(),
        )
        followup.close()
        return followup

    followup = _call_in_other_thread(
        public_probe,
        description="CLOSED owner idempotence and next public start",
    )

    _assert_state(module, owner, "CLOSED")
    _assert_state(module, followup, "CLOSED")
    assert target_attempts == [target_transition]
    assert public_sweep_results == (
        ["REMOVED"] if fault_position == "pre" else []
    )
    assert len(kernel.CreateEventW.calls) == 2
    assert len(kernel.ConnectNamedPipe.calls) == 2
    assert len(kernel.CloseHandle.calls) == 2


_PUBLIC_QUARANTINE_FAULT_CASES = [
    pytest.param("pre", "exit-74", FAILFAST_QUARANTINE_EXIT_CODE, id="pre"),
    pytest.param(
        "consume",
        "exit-74",
        FAILFAST_QUARANTINE_EXIT_CODE,
        id="after-consume",
    ),
    pytest.param(
        "phase",
        "exit-74",
        FAILFAST_QUARANTINE_EXIT_CODE,
        id="after-phase",
    ),
    pytest.param(
        "flag",
        "exit-74",
        FAILFAST_QUARANTINE_EXIT_CODE,
        id="after-flag",
    ),
    pytest.param(
        "count",
        "exit-74",
        FAILFAST_QUARANTINE_EXIT_CODE,
        id="after-count",
    ),
    pytest.param("post", "exit-74", FAILFAST_QUARANTINE_EXIT_CODE, id="post"),
    pytest.param(
        "post",
        "return",
        FAILFAST_RETURNED_EXIT_CODE,
        id="post-stub-returned",
    ),
]


@pytest.mark.parametrize(
    ("commit_boundary", "failfast_mode", "expected_exit_code"),
    _PUBLIC_QUARANTINE_FAULT_CASES,
)
@pytest.mark.parametrize(
    "interrupt_type",
    [RuntimeError, KeyboardInterrupt, SystemExit],
)
def test_public_quarantine_faults_release_locks_before_exact_process_exit(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    commit_boundary: str,
    failfast_mode: str,
    expected_exit_code: int,
    interrupt_type: type[BaseException],
) -> None:
    trace_by_boundary = {
        "pre": [],
        "consume": ["consume"],
        "phase": ["consume", "phase"],
        "flag": ["consume", "phase", "flag"],
        "count": ["consume", "phase", "flag", "count"],
        "post": ["consume", "phase", "flag", "count"],
    }
    expected_trace = trace_by_boundary[commit_boundary]
    delegated = _delegate_quarantine_node_to_subprocess(
        request,
        expected_exit_code=expected_exit_code,
    )
    if delegated is not None:
        assert "TASK_3B2_QUARANTINE_COMMIT_ATTEMPTS=1" in delegated.stdout
        assert "TASK_3B2_QUARANTINE_ERROR_CACHE=clean" in delegated.stdout
        assert "TASK_3B2_QUARANTINE_LOCK_PROBE=ok" in delegated.stdout
        assert (
            "TASK_3B2_QUARANTINE_TRACE=" + json.dumps(expected_trace)
        ) in delegated.stdout
        assert (
            "TASK_3B2_QUARANTINE_FAULT=" + interrupt_type.__name__
        ) in delegated.stdout
        if failfast_mode == "return":
            assert "TASK_3B2_FAILFAST_STUB_RETURNED" in delegated.stdout
            assert "TASK_3B2_FAILFAST_CALLER_RETURNED" not in delegated.stdout
            assert "TASK_3B2_FAILFAST_CALLER_RAISED" not in delegated.stdout
        else:
            assert "TASK_3B2_FAILFAST_STUB_RETURNED" not in delegated.stdout
        return

    kernel = _FakeKernel32()
    kernel.event_handles.extend([0x2B50, 0x2B51])
    kernel.connect_outcomes.extend([_Outcome(True), _Outcome(True)])
    kernel.close_outcomes.append(_Outcome(False, ERROR_INVALID_HANDLE))
    module = _load_subject(monkeypatch, kernel)
    real_commit = module._commit_registry_transition
    reserve_records: list[tuple[object, object]] = []

    def capture_initial_reserve(
        registry: object,
        transition: object,
    ) -> object:
        result = real_commit(registry, transition)
        if _registry_status(transition.mode) == "RESERVE_INSERT":
            _assert_registry_transition_result(module, result, "COMMITTED")
            reserve_records.append((registry, transition))
        return result

    monkeypatch.setattr(
        module,
        "_commit_registry_transition",
        capture_initial_reserve,
    )
    owner = module.OwnedOverlappedPipeOperation.start_connect(
        pipe_handle=0x3B50,
        absolute_deadline=_future_deadline(),
    )
    assert len(reserve_records) == 1
    original_registry, reserve_transition = reserve_records[0]
    original_entry = reserve_transition.expected_entry
    original_cell = reserve_transition.expected_cell
    original_token = reserve_transition.entry_token
    assert original_registry.entries[original_token] is original_entry
    original_completion = owner.completion
    assert original_completion is not None

    def backing_identities(active: object) -> frozenset[int]:
        references = _ctypes_backing_weakrefs(active)
        values = [reference() for reference in references]
        assert all(value is not None for value in values)
        return frozenset(id(value) for value in values)

    original_backing = backing_identities(owner)

    real_failfast = module._failfast_quarantine
    original = interrupt_type(
        f"QUARANTINE {commit_boundary} interrupted"
    )
    mutation_trace: list[str] = []
    fault_seen: list[bool] = []
    commit_statuses: list[str] = []
    captured_transitions: list[object] = []
    commit_attempts = 0

    def faulting_commit(
        registry: object,
        transition: object,
    ) -> object:
        nonlocal commit_attempts
        commit_attempts += 1
        if commit_attempts != 1:
            raise AssertionError(
                "registry transition retried after QUARANTINE became unknown"
            )
        if _registry_status(transition.mode) != "QUARANTINE":
            raise AssertionError(
                "first intercepted registry transition was not QUARANTINE"
            )
        if registry is not original_registry:
            raise AssertionError("public quarantine replaced the live registry")
        if transition.entry_token is not original_token:
            raise AssertionError("public quarantine replaced the entry token")
        if transition.expected_entry is not original_entry:
            raise AssertionError("public quarantine copied the registry entry")
        if transition.expected_cell is not original_cell:
            raise AssertionError("public quarantine copied the ownership cell")
        if registry.entries[original_token] is not original_entry:
            raise AssertionError("public quarantine rebound the live mapping")
        if owner.completion is not original_completion:
            raise AssertionError("public quarantine replaced terminal evidence")
        if backing_identities(owner) != original_backing:
            raise AssertionError("public quarantine replaced ctypes backing")

        if captured_transitions:
            raise AssertionError("QUARANTINE transition was attempted twice")
        captured_transitions.append(transition)
        if commit_boundary == "pre":
            fault_seen.append(True)
            raise original

        try:
            result = _call_registry_commit_with_assignment_probe(
                module,
                real_commit,
                registry,
                transition,
                fault_label=(
                    commit_boundary
                    if commit_boundary in {"consume", "phase", "flag", "count"}
                    else None
                ),
                fault=original,
                mutation_trace=mutation_trace,
            )
        except BaseException as error:
            if registry.entries[original_token] is not original_entry:
                raise AssertionError(
                    "partial quarantine commit rebound the live mapping"
                ) from error
            if backing_identities(owner) != original_backing:
                raise AssertionError(
                    "partial quarantine commit replaced ctypes backing"
                ) from error
            if error is original:
                fault_seen.append(True)
            raise

        if commit_boundary == "post":
            if result is not module._RegistryTransitionResult.COMMITTED:
                raise AssertionError(
                    "QUARANTINE primitive returned a foreign COMMITTED result"
                )
            commit_statuses.append(_registry_status(result))
            if registry.entries[original_token] is not original_entry:
                raise AssertionError("post quarantine commit rebound the live mapping")
            fault_seen.append(True)
            raise original

        raise AssertionError(
            f"QUARANTINE {commit_boundary} fault was not delivered"
        )

    monkeypatch.setattr(
        module,
        "_commit_registry_transition",
        faulting_commit,
    )

    def child_abort(exit_code: int, marker: str) -> None:
        print(marker, flush=True)
        os._exit(exit_code)

    def failfast_stub() -> None:
        setattr(module, "_commit_registry_transition", real_commit)
        setattr(module, "_failfast_quarantine", real_failfast)
        try:
            _assert_errors_and_tracebacks_not_cached(
                module,
                owner,
                original,
            )
        except BaseException as audit_error:
            child_abort(
                80,
                "TASK_3B2_QUARANTINE_ERROR_CACHE="
                f"{type(audit_error).__name__}",
            )
        print("TASK_3B2_QUARANTINE_ERROR_CACHE=clean", flush=True)
        if original_registry.entries[original_token] is not original_entry:
            child_abort(91, "TASK_3B2_QUARANTINE_ENTRY_IDENTITY=changed")
        if owner.completion is not original_completion:
            child_abort(92, "TASK_3B2_QUARANTINE_COMPLETION_IDENTITY=changed")
        if backing_identities(owner) != original_backing:
            child_abort(93, "TASK_3B2_QUARANTINE_BACKING_IDENTITY=changed")

        print(
            f"TASK_3B2_QUARANTINE_COMMIT_ATTEMPTS={commit_attempts}",
            flush=True,
        )
        if commit_attempts != 1:
            child_abort(
                81,
                "TASK_3B2_QUARANTINE_BAD_COMMIT_ATTEMPTS="
                f"{commit_attempts}",
            )
        assert commit_attempts == 1

        if mutation_trace != expected_trace:
            child_abort(
                82,
                "TASK_3B2_QUARANTINE_BAD_TRACE="
                + json.dumps(mutation_trace),
            )
        if fault_seen != [True]:
            child_abort(
                83,
                f"TASK_3B2_QUARANTINE_BAD_FAULT_SEEN={fault_seen!r}",
            )
        if len(captured_transitions) != 1:
            child_abort(
                84,
                "TASK_3B2_QUARANTINE_BAD_TRANSITION_COUNT="
                f"{len(captured_transitions)}",
            )
        if commit_boundary == "post" and commit_statuses != ["COMMITTED"]:
            child_abort(
                85,
                f"TASK_3B2_QUARANTINE_BAD_STATUS={commit_statuses!r}",
            )

        expected_marker = (
            "NEVER" if commit_boundary == "pre" else "CONSUMED"
        )
        actual_marker = _registry_status(
            captured_transitions[0].consume_state.value
        )
        if actual_marker != expected_marker:
            child_abort(
                86,
                "TASK_3B2_QUARANTINE_BAD_MARKER=" + actual_marker,
            )

        probe_result: dict[str, object] = {}

        def public_lock_probe() -> None:
            try:
                if backing_identities(owner) != original_backing:
                    raise AssertionError("source backing changed before transfer")
                destination = owner.transfer()
                if owner.recover_transferred_owner() is not destination:
                    raise AssertionError(
                        "transfer recovery returned the wrong destination"
                    )
                if destination.completion is not original_completion:
                    raise AssertionError("transfer replaced terminal evidence")
                if backing_identities(destination) != original_backing:
                    raise AssertionError("transfer copied ctypes backing")

                followup = module.OwnedOverlappedPipeOperation.start_connect(
                    pipe_handle=0x3B51,
                    absolute_deadline=_future_deadline(),
                )
                if followup.state is not module.OverlappedState.DRAINED:
                    raise AssertionError(
                        f"follow-up state was {followup.state!r}"
                    )
                followup.close()
                if followup.state is not module.OverlappedState.CLOSED:
                    raise AssertionError(
                        f"follow-up close state was {followup.state!r}"
                    )
                probe_result["ok"] = True
            except BaseException as error:
                probe_result["error"] = repr(error)

        thread = threading.Thread(target=public_lock_probe, daemon=True)
        thread.start()
        thread.join(5.0)
        if thread.is_alive():
            child_abort(87, "TASK_3B2_QUARANTINE_LOCK_PROBE=blocked")
        if probe_result != {"ok": True}:
            child_abort(
                88,
                f"TASK_3B2_QUARANTINE_LOCK_PROBE={probe_result!r}",
            )
        if commit_attempts != 1:
            child_abort(
                89,
                "TASK_3B2_QUARANTINE_COMMIT_ATTEMPTS_CHANGED="
                f"{commit_attempts}",
            )
        if len(kernel.CloseHandle.calls) != 2:
            child_abort(
                90,
                "TASK_3B2_QUARANTINE_BAD_CLOSE_COUNT="
                f"{len(kernel.CloseHandle.calls)}",
            )

        print(
            "TASK_3B2_QUARANTINE_TRACE=" + json.dumps(mutation_trace),
            flush=True,
        )
        print(
            "TASK_3B2_QUARANTINE_FAULT=" + interrupt_type.__name__,
            flush=True,
        )
        print("TASK_3B2_QUARANTINE_LOCK_PROBE=ok", flush=True)

        if failfast_mode == "exit-74":
            os._exit(FAILFAST_QUARANTINE_EXIT_CODE)

        setattr(module, "_commit_registry_transition", faulting_commit)
        print("TASK_3B2_FAILFAST_STUB_RETURNED", flush=True)

    monkeypatch.setattr(module, "_failfast_quarantine", failfast_stub)

    try:
        owner.close()
    except BaseException as unexpected:
        print(
            "TASK_3B2_FAILFAST_CALLER_RAISED="
            f"{type(unexpected).__name__}",
            flush=True,
        )
        os._exit(98)

    print("TASK_3B2_FAILFAST_CALLER_RETURNED", flush=True)
    os._exit(99)
