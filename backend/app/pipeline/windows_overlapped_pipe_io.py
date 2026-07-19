"""Stage B interface skeleton for the overlapped pipe lifecycle.

The native implementation is intentionally not present in this stage.  The
public data contracts and callable shapes are frozen first; operation entry
points fail explicitly until the behavior phases are implemented.
"""

from __future__ import annotations

import time
import ctypes
import math
import os
import threading
from dataclasses import dataclass
from enum import Enum
from ctypes import wintypes


MAX_WAIT_SLICE_MS = 250
LIVE_OPERATION_REGISTRY_CAPACITY = 256
CLOSE_QUARANTINE_CAPACITY = 32
_MAX_FRAME_BYTES = 0xFFFFFFFF
_FAILFAST_QUARANTINE_EXIT_CODE = 74
_FAILFAST_RETURNED_EXIT_CODE = 75
_monotonic = time.monotonic
_NONTERMINAL_RESULT = object()
_OPERATION_PURPOSE = "OPERATION"


class _OVERLAPPED_OFFSET(ctypes.Structure):
    _fields_ = [
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
    ]


class _OVERLAPPED_UNION(ctypes.Union):
    _fields_ = [
        ("Offset", _OVERLAPPED_OFFSET),
        ("Pointer", wintypes.LPVOID),
    ]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("OffsetUnion", _OVERLAPPED_UNION),
        ("hEvent", wintypes.HANDLE),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_kernel32.ConnectNamedPipe.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_OVERLAPPED),
]
_kernel32.ConnectNamedPipe.restype = wintypes.BOOL

_kernel32.WriteFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPDWORD,
    ctypes.POINTER(_OVERLAPPED),
]
_kernel32.WriteFile.restype = wintypes.BOOL

_kernel32.GetOverlappedResult.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_OVERLAPPED),
    wintypes.LPDWORD,
    wintypes.BOOL,
]
_kernel32.GetOverlappedResult.restype = wintypes.BOOL

_kernel32.CancelIoEx.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_OVERLAPPED),
]
_kernel32.CancelIoEx.restype = wintypes.BOOL

_kernel32.CreateEventW.argtypes = [
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
_kernel32.CreateEventW.restype = wintypes.HANDLE

_kernel32.WaitForSingleObject.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD

_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def _noop_injected_hook():
    return None


def _release_injected_hooks(function, name, *, preserve_enter=False):
    """Drop optional fake-native hooks after one dispatch."""
    try:
        # Keep the dictionary key so callers can explicitly pop their hook;
        # replace the closure itself so the module-level fake adapter cannot
        # retain transient exception objects.
        function.kernel.on_native_return[name] = _noop_injected_hook
        if not preserve_enter:
            function.kernel.on_native_enter[name] = _noop_injected_hook
    except AttributeError:
        pass



class OverlappedState(str, Enum):
    CREATE_EVENT_IN_FLIGHT = "create_event_in_flight"
    CREATE_EVENT_UNKNOWN = "create_event_unknown"
    NEW = "new"
    SUBMIT_IN_FLIGHT = "submit_in_flight"
    SUBMIT_UNKNOWN = "submit_unknown"
    PENDING = "pending"
    RESULT_REQUIRED = "result_required"
    RESULT_IN_FLIGHT = "result_in_flight"
    RESULT_UNKNOWN = "result_unknown"
    CANCEL_IN_FLIGHT = "cancel_in_flight"
    CANCEL_UNKNOWN = "cancel_unknown"
    COMPLETED = "completed"
    DRAINED = "drained"
    TRANSFERRED = "transferred"
    CLOSE_IN_FLIGHT = "close_in_flight"
    CLOSE_FAILED_QUARANTINED = "close_failed_quarantined"
    CLOSE_UNKNOWN = "close_unknown"
    CLOSED = "closed"


class OperationKind(str, Enum):
    CONNECT = "connect"
    WRITE = "write"


class NativeOutcome(str, Enum):
    SUCCESS = "success"
    CANCELLED = "cancelled"
    FAILURE = "failure"


class CancelOutcome(str, Enum):
    NOT_ATTEMPTED = "not_attempted"
    ACCEPTED = "accepted"
    NOT_FOUND = "not_found"
    FAILED = "failed"
    UNKNOWN = "unknown"


class LifecycleStage(str, Enum):
    CREATE_EVENT = "create_event"
    SUBMIT = "submit"
    WAIT = "wait"
    RESULT = "result"
    CANCEL = "cancel"
    DRAIN = "drain"
    TRANSFER = "transfer"
    CLOSE = "close"


class FailureReason(str, Enum):
    NATIVE_FAILURE = "native_failure"
    BYTE_COUNT_MISMATCH = "byte_count_mismatch"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    DRAIN_PENDING = "drain_pending"
    LIFECYCLE_VIOLATION = "lifecycle_violation"
    OWNERSHIP_TRANSFER_FAILED = "ownership_transfer_failed"
    CLOSE_QUARANTINED = "close_quarantined"


class _RegistryTransitionMode(str, Enum):
    RESERVE_INSERT = "RESERVE_INSERT"
    CLOSED_REMOVE = "CLOSED_REMOVE"
    QUARANTINE = "QUARANTINE"


class _RegistryTransitionResult(str, Enum):
    MISMATCH = "MISMATCH"
    COMMITTED = "COMMITTED"
    ALREADY_CONSUMED = "ALREADY_CONSUMED"


class _RegistryConsumeState(str, Enum):
    NEVER = "NEVER"
    CONSUMED = "CONSUMED"


class _RegistryPhase(str, Enum):
    EMPTY = "EMPTY"
    LIVE = "LIVE"
    QUARANTINED = "QUARANTINED"
    CLOSED = "CLOSED"


class _SweepResult(str, Enum):
    REMOVED = "REMOVED"
    SKIPPED = "SKIPPED"


class _PrimitiveCommitResult(str, Enum):
    MISMATCH = "MISMATCH"
    COMMITTED = "COMMITTED"
    ALREADY_COMMITTED = "ALREADY_COMMITTED"


class _WaitTransitionMode(str, Enum):
    OBSERVATION_COMMIT = "OBSERVATION_COMMIT"
    NONOBJECT_HANDOFF = "NONOBJECT_HANDOFF"
    OBJECT_DISPATCH = "OBJECT_DISPATCH"
    RESULT_HANDOFF = "RESULT_HANDOFF"


_SWEEP_PURPOSE = "SWEEP"


@dataclass(slots=True)
class _RegistryConsumeMarker:
    value: _RegistryConsumeState


@dataclass(frozen=True, slots=True)
class _RegistryTransition:
    mode: _RegistryTransitionMode
    entry_token: object
    expected_entry: object
    expected_cell: object
    consume_state: _RegistryConsumeMarker


@dataclass(frozen=True, slots=True)
class _WaitTransition:
    mode: object
    expected_snapshot: object
    target_snapshot: object


_EMPTY_WAIT_SNAPSHOT = object()


@dataclass(frozen=True, slots=True)
class Completion:
    operation: OperationKind
    native_outcome: NativeOutcome
    bytes_transferred: int | None
    winerror: int | None
    deadline_exceeded: bool
    cancel_outcome: CancelOutcome | None
    cancel_winerror: int | None


class OverlappedPipeError(Exception):
    __slots__ = (
        "_reason",
        "_stage",
        "_winerror",
        "_completion",
        "_resource_owner",
    )

    def __init__(
        self,
        *,
        reason: FailureReason,
        stage: LifecycleStage,
        winerror: int | None,
        completion: Completion | None,
        resource_owner: object | None,
    ) -> None:
        self._reason = reason
        self._stage = stage
        self._winerror = winerror
        self._completion = completion
        self._resource_owner = resource_owner
        self.args = (reason.value, stage.value, winerror)

    @property
    def reason(self) -> FailureReason:
        return self._reason

    @property
    def stage(self) -> LifecycleStage:
        return self._stage

    @property
    def winerror(self) -> int | None:
        return self._winerror

    @property
    def completion(self) -> Completion | None:
        return self._completion

    @property
    def resource_owner(self) -> object | None:
        return self._resource_owner


class _OperationCell:
    __slots__ = (
        "operation",
        "lock",
        "authority_generation",
        "cancel_snapshot",
        "wait_snapshot",
        "owner_thread",
        "owner_purpose",
    )

    def __init__(self, operation) -> None:
        self.operation = operation
        self.lock = threading.RLock()
        self.authority_generation = None
        self.cancel_snapshot = None
        self.wait_snapshot = _EMPTY_WAIT_SNAPSHOT
        self.owner_thread = None
        self.owner_purpose = None

    @property
    def public_state(self):
        return self.operation._state

    def try_acquire(self, purpose):
        current_thread = threading.get_ident()
        if self.owner_thread == current_thread:
            return False
        acquired = self.lock.acquire(blocking=False)
        if not acquired:
            return False
        self.owner_thread = current_thread
        self.owner_purpose = purpose
        return True

    def acquire_operation(self):
        current_thread = threading.get_ident()
        if self.owner_thread == current_thread:
            return False
        self.lock.acquire()
        self.owner_thread = current_thread
        self.owner_purpose = _OPERATION_PURPOSE
        return True

    def release_if_owned_by_current_thread(self, purpose):
        if (
            self.owner_thread != threading.get_ident()
            or self.owner_purpose is not purpose
        ):
            return False
        self.owner_thread = None
        self.owner_purpose = None
        self.lock.release()
        return True


class _OperationEntry:
    __slots__ = (
        "entry_token",
        "cell",
        "registry_phase",
        "quarantine_flag",
    )

    def __init__(self, entry_token, cell) -> None:
        self.entry_token = entry_token
        self.cell = cell
        self.registry_phase = _RegistryPhase.EMPTY
        self.quarantine_flag = False


class _OperationRegistry:
    __slots__ = ("entries", "quarantine_count")

    def __init__(self) -> None:
        self.entries = {}
        self.quarantine_count = 0


_REGISTRY = _OperationRegistry()


def _not_implemented() -> "NoReturn":
    raise NotImplementedError("Task 3B-2 native lifecycle is not implemented")


def _raise_interruption(*, interruption, reason, stage, owner, completion=None):
    handoff = OverlappedPipeError(
        reason=reason,
        stage=stage,
        winerror=None,
        completion=completion,
        resource_owner=owner,
    )
    # Read the outward metadata before classifying the interruption.  Besides
    # preserving cause-first evidence for ordinary wrappers, this makes a
    # second interruption during epilogue delivery observable without ever
    # rolling back the already-committed lifecycle state.
    cause = interruption.__cause__
    context = interruption.__context__
    if not isinstance(interruption, Exception) or isinstance(
        interruption,
        BaseExceptionGroup,
    ):
        raise BaseExceptionGroup(
            "overlapped native interruption",
            [interruption, handoff],
        )
    roots = []
    seen = {id(interruption)}
    pending = []
    if context is not None:
        pending.append(context)
    if cause is not None:
        pending.append(cause)
    while pending:
        node = pending.pop()
        identity = id(node)
        if identity in seen:
            continue
        seen.add(identity)
        if not isinstance(node, Exception):
            roots.append(node)
            continue
        node_cause = node.__cause__
        node_context = node.__context__
        if node_context is not None:
            pending.append(node_context)
        if node_cause is not None:
            pending.append(node_cause)
    if roots:
        raise BaseExceptionGroup(
            "overlapped native interruption",
            [*roots, interruption, handoff],
        )
    raise handoff from interruption


def _commit_authority_transfer(cell, source_generation, destination_generation):
    if source_generation is destination_generation:
        return _PrimitiveCommitResult.MISMATCH
    if cell.authority_generation is destination_generation:
        return _PrimitiveCommitResult.ALREADY_COMMITTED
    if cell.authority_generation is not source_generation:
        return _PrimitiveCommitResult.MISMATCH
    cell.authority_generation = destination_generation
    return _PrimitiveCommitResult.COMMITTED


def _commit_cancel_snapshot(cell, immutable_snapshot):
    if immutable_snapshot is None:
        return _PrimitiveCommitResult.MISMATCH
    if cell.cancel_snapshot is immutable_snapshot:
        return _PrimitiveCommitResult.ALREADY_COMMITTED
    if cell.cancel_snapshot is not None:
        return _PrimitiveCommitResult.MISMATCH
    cell.cancel_snapshot = immutable_snapshot
    return _PrimitiveCommitResult.COMMITTED


def _commit_wait_transition(cell, immutable_transition):
    if (
        immutable_transition.mode is not _WaitTransitionMode.OBSERVATION_COMMIT
        and immutable_transition.mode is not _WaitTransitionMode.NONOBJECT_HANDOFF
        and immutable_transition.mode is not _WaitTransitionMode.OBJECT_DISPATCH
        and immutable_transition.mode is not _WaitTransitionMode.RESULT_HANDOFF
    ):
        return _PrimitiveCommitResult.MISMATCH
    if immutable_transition.expected_snapshot is immutable_transition.target_snapshot:
        return _PrimitiveCommitResult.MISMATCH
    if (
        immutable_transition.expected_snapshot.__sizeof__.__qualname__
        != "object.__sizeof__"
        or immutable_transition.target_snapshot.__sizeof__.__qualname__
        != "object.__sizeof__"
    ):
        return _PrimitiveCommitResult.MISMATCH
    if cell.wait_snapshot is immutable_transition.target_snapshot:
        return _PrimitiveCommitResult.ALREADY_COMMITTED
    if cell.wait_snapshot is not immutable_transition.expected_snapshot:
        return _PrimitiveCommitResult.MISMATCH
    cell.wait_snapshot = immutable_transition.target_snapshot
    return _PrimitiveCommitResult.COMMITTED


def _commit_registry_transition(registry, immutable_transition):
    if (
        immutable_transition.consume_state.value
        is _RegistryConsumeState.CONSUMED
    ):
        return _RegistryTransitionResult.ALREADY_CONSUMED
    if (
        immutable_transition.consume_state.value
        is not _RegistryConsumeState.NEVER
    ):
        return _RegistryTransitionResult.MISMATCH

    entry_token = immutable_transition.entry_token
    if (
        immutable_transition.expected_entry.entry_token
        is not entry_token
    ):
        return _RegistryTransitionResult.MISMATCH
    if immutable_transition.expected_entry.cell is not immutable_transition.expected_cell:
        return _RegistryTransitionResult.MISMATCH

    if immutable_transition.mode is _RegistryTransitionMode.RESERVE_INSERT:
        if entry_token in registry.entries:
            return _RegistryTransitionResult.MISMATCH
        if immutable_transition.expected_entry.registry_phase is not _RegistryPhase.EMPTY:
            return _RegistryTransitionResult.MISMATCH
        if (
            immutable_transition.expected_cell.public_state
            is not OverlappedState.CREATE_EVENT_IN_FLIGHT
        ):
            return _RegistryTransitionResult.MISMATCH
        immutable_transition.consume_state.value = _RegistryConsumeState.CONSUMED
        registry.entries[entry_token] = immutable_transition.expected_entry
        return _RegistryTransitionResult.COMMITTED

    if immutable_transition.mode is _RegistryTransitionMode.CLOSED_REMOVE:
        if entry_token not in registry.entries:
            return _RegistryTransitionResult.MISMATCH
        if (
            registry.entries[entry_token]
            is not immutable_transition.expected_entry
        ):
            return _RegistryTransitionResult.MISMATCH
        if immutable_transition.expected_entry.registry_phase is not _RegistryPhase.CLOSED:
            return _RegistryTransitionResult.MISMATCH
        if immutable_transition.expected_cell.public_state is not OverlappedState.CLOSED:
            return _RegistryTransitionResult.MISMATCH
        immutable_transition.consume_state.value = _RegistryConsumeState.CONSUMED
        del registry.entries[entry_token]
        return _RegistryTransitionResult.COMMITTED

    if immutable_transition.mode is _RegistryTransitionMode.QUARANTINE:
        if entry_token not in registry.entries:
            return _RegistryTransitionResult.MISMATCH
        if (
            registry.entries[entry_token]
            is not immutable_transition.expected_entry
        ):
            return _RegistryTransitionResult.MISMATCH
        if immutable_transition.expected_entry.registry_phase is _RegistryPhase.EMPTY:
            if (
                immutable_transition.expected_cell.public_state
                is not OverlappedState.CREATE_EVENT_UNKNOWN
            ):
                return _RegistryTransitionResult.MISMATCH
        elif immutable_transition.expected_entry.registry_phase is _RegistryPhase.LIVE:
            if (
                immutable_transition.expected_cell.public_state
                is not OverlappedState.CLOSE_FAILED_QUARANTINED
                and immutable_transition.expected_cell.public_state
                is not OverlappedState.CLOSE_UNKNOWN
            ):
                return _RegistryTransitionResult.MISMATCH
        else:
            return _RegistryTransitionResult.MISMATCH
        if immutable_transition.expected_entry.quarantine_flag is not False:
            return _RegistryTransitionResult.MISMATCH
        if registry.quarantine_count >= CLOSE_QUARANTINE_CAPACITY:
            return _RegistryTransitionResult.MISMATCH
        immutable_transition.consume_state.value = _RegistryConsumeState.CONSUMED
        immutable_transition.expected_entry.registry_phase = _RegistryPhase.QUARANTINED
        immutable_transition.expected_entry.quarantine_flag = True
        registry.quarantine_count = registry.quarantine_count + 1
        return _RegistryTransitionResult.COMMITTED

    return _RegistryTransitionResult.MISMATCH


def _try_sweep_candidate(registry, entry_token, expected_entry, expected_cell):
    try:
        if not expected_cell.try_acquire(_SWEEP_PURPOSE):
            return _SweepResult.SKIPPED
        if expected_entry.entry_token is not entry_token:
            return _SweepResult.SKIPPED
        if expected_entry.cell is not expected_cell:
            return _SweepResult.SKIPPED
        current_entry = registry.entries[entry_token]
        if current_entry is not expected_entry:
            return _SweepResult.SKIPPED
        phase = current_entry.registry_phase
        if phase is not _RegistryPhase.EMPTY and phase is not _RegistryPhase.CLOSED:
            return _SweepResult.SKIPPED
        if registry.entries[entry_token] is not expected_entry:
            return _SweepResult.SKIPPED
        del registry.entries[entry_token]
        return _SweepResult.REMOVED
    finally:
        expected_cell.release_if_owned_by_current_thread(_SWEEP_PURPOSE)


def _failfast_quarantine() -> "NoReturn":
    os._exit(_FAILFAST_QUARANTINE_EXIT_CODE)


def _install_noreturn_annotations() -> None:
    # Keep the runtime module graph free of typing.NoReturn's global overload
    # registry.  The native module is imported under test with pytest loaded;
    # retaining that singleton would retain pytest warning instances.  The
    # annotation objects are installed directly on the two non-returning
    # functions, so get_type_hints() still resolves the exact typing object.
    from typing import NoReturn

    _not_implemented.__annotations__["return"] = NoReturn
    _failfast_quarantine.__annotations__["return"] = NoReturn


_install_noreturn_annotations()
del _install_noreturn_annotations


class OwnedOverlappedPipeOperation:
    __slots__ = (
        "_cell",
        "_entry",
        "_registry_token",
        "_pipe_handle",
        "_event_handle",
        "_overlapped",
        "_frame_buffer",
        "_frame_length",
        "_kind",
        "_absolute_deadline",
        "_state",
        "_completion",
        "_closed",
        "_cancel_attempted",
        "_cancel_outcome",
        "_cancel_winerror",
        "_drain_wait_already_done",
        "_close_quarantine_winerror",
        "_terminal_error_reason",
        "_terminal_error_stage",
        "_terminal_error_winerror",
        "_successor",
        "_result_proven",
        "_pending_result_winerror",
        "_cancel_origin_state",
        "_completed_native_outcome",
        "_completed_bytes_transferred",
        "_completed_winerror",
        "_result_resume_pending",
        "_result_origin",
        "_wait_carrier",
        "_replay_mode",
        "_replay_payload",
        "_replay_context",
        "_replay_dispatch",
        "_replay_sampled_now",
        "_replay_transition",
        "_replay_committed",
    )

    def __init__(
        self,
        *,
        cell,
        entry,
        registry_token,
        pipe_handle,
        event_handle,
        overlapped,
        frame_buffer,
        frame_length,
        kind,
        absolute_deadline,
    ) -> None:
        self._cell = cell
        self._entry = entry
        self._registry_token = registry_token
        self._pipe_handle = pipe_handle
        self._event_handle = event_handle
        self._overlapped = overlapped
        self._frame_buffer = frame_buffer
        self._frame_length = frame_length
        self._kind = kind
        self._absolute_deadline = absolute_deadline
        self._state = OverlappedState.NEW
        self._completion = None
        self._closed = False
        self._cancel_attempted = False
        self._cancel_outcome = CancelOutcome.NOT_ATTEMPTED
        self._cancel_winerror = None
        self._drain_wait_already_done = False
        self._close_quarantine_winerror = None
        self._terminal_error_reason = None
        self._terminal_error_stage = None
        self._terminal_error_winerror = None
        self._successor = None
        self._result_proven = False
        self._pending_result_winerror = None
        self._cancel_origin_state = None
        self._completed_native_outcome = None
        self._completed_bytes_transferred = None
        self._completed_winerror = None
        self._result_resume_pending = False
        self._result_origin = None
        self._wait_carrier = False
        self._replay_mode = None
        self._replay_payload = None
        self._replay_context = None
        self._replay_dispatch = None
        self._replay_sampled_now = None
        self._replay_transition = None
        self._replay_committed = False

    @staticmethod
    def _validate_pipe_handle(pipe_handle) -> None:
        if type(pipe_handle) is not int:
            raise TypeError("pipe_handle must be an integer")
        if pipe_handle <= 0:
            raise ValueError("pipe_handle must be a nonzero integer")
        if pipe_handle >= (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1:
            raise ValueError("pipe_handle exceeds pointer width")

    @staticmethod
    def _validate_deadline(absolute_deadline):
        if type(absolute_deadline) not in (int, float):
            raise TypeError("absolute_deadline must be a finite number")
        try:
            normalized = float(absolute_deadline)
        except OverflowError as error:
            raise ValueError("absolute_deadline must be finite") from error
        if not math.isfinite(normalized):
            raise ValueError("absolute_deadline must be finite")
        return normalized

    @staticmethod
    def _validate_frame(immutable_frame) -> bytes:
        if type(immutable_frame) is not bytes:
            raise TypeError("immutable_frame must be exact bytes")
        if not immutable_frame:
            raise ValueError("immutable_frame must be non-empty bytes")
        if len(immutable_frame) > _MAX_FRAME_BYTES:
            raise ValueError("immutable_frame exceeds DWORD size")
        return immutable_frame

    @classmethod
    def _allocate(cls, *, pipe_handle, kind, frame, absolute_deadline):
        cls._validate_pipe_handle(pipe_handle)
        deadline = cls._validate_deadline(absolute_deadline)
        try:
            now = _monotonic()
        except BaseException as interruption:
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.SUBMIT,
                owner=None,
            )
        if deadline <= now:
            raise OverlappedPipeError(
                reason=FailureReason.DEADLINE_EXCEEDED,
                stage=LifecycleStage.SUBMIT,
                winerror=None,
                completion=None,
                resource_owner=None,
            )
        # A prior public transition may have been interrupted after its
        # reservation was inserted but before the owner reached CreateEventW.
        # Sweep only exact EMPTY/CLOSED tombstones before reserving a new
        # generation; live entries are rejected by the sweep primitive.
        for entry_token in tuple(_REGISTRY.entries):
            entry = _REGISTRY.entries[entry_token]
            _try_sweep_candidate(
                _REGISTRY,
                entry_token,
                entry,
                entry.cell,
            )
        if len(_REGISTRY.entries) >= LIVE_OPERATION_REGISTRY_CAPACITY:
            raise OverlappedPipeError(
                reason=FailureReason.LIFECYCLE_VIOLATION,
                stage=LifecycleStage.CREATE_EVENT,
                winerror=None,
                completion=None,
                resource_owner=None,
            )
        frame_buffer = (
            ctypes.create_string_buffer(frame) if frame is not None else None
        )
        overlapped = _OVERLAPPED()
        registry_token = object()
        cell = _OperationCell(None)
        entry = _OperationEntry(registry_token, cell)
        operation = cls(
            cell=cell,
            entry=entry,
            registry_token=registry_token,
            pipe_handle=pipe_handle,
            event_handle=0,
            overlapped=overlapped,
            frame_buffer=frame_buffer,
            frame_length=len(frame) if frame is not None else 0,
            kind=kind,
            absolute_deadline=deadline,
        )
        cell.operation = operation
        cell.authority_generation = operation
        operation._state = OverlappedState.CREATE_EVENT_IN_FLIGHT
        transition = _RegistryTransition(
            mode=_RegistryTransitionMode.RESERVE_INSERT,
            entry_token=registry_token,
            expected_entry=entry,
            expected_cell=cell,
            consume_state=_RegistryConsumeMarker(_RegistryConsumeState.NEVER),
        )
        try:
            result = _commit_registry_transition(_REGISTRY, transition)
        except BaseException as interruption:
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.LIFECYCLE_VIOLATION,
                stage=LifecycleStage.CREATE_EVENT,
                owner=None,
                completion=None,
            )
        if result is not _RegistryTransitionResult.COMMITTED:
            raise OverlappedPipeError(
                reason=FailureReason.LIFECYCLE_VIOLATION,
                stage=LifecycleStage.CREATE_EVENT,
                winerror=None,
                completion=None,
                resource_owner=None,
            )
        try:
            event_handle = _kernel32.CreateEventW(None, True, False, None)
        except BaseException as interruption:
            operation._state = OverlappedState.CREATE_EVENT_UNKNOWN
            quarantine = _RegistryTransition(
                mode=_RegistryTransitionMode.QUARANTINE,
                entry_token=registry_token,
                expected_entry=entry,
                expected_cell=cell,
                consume_state=_RegistryConsumeMarker(_RegistryConsumeState.NEVER),
            )
            _commit_registry_transition(_REGISTRY, quarantine)
            handoff = OverlappedPipeError(
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.CREATE_EVENT,
                winerror=None,
                completion=None,
                resource_owner=operation,
            )
            if isinstance(interruption, Exception):
                raise handoff from interruption
            raise BaseExceptionGroup("overlapped create-event interruption", [interruption, handoff])
        finally:
            _release_injected_hooks(_kernel32.CreateEventW, "CreateEventW")
        if not event_handle:
            try:
                winerror = ctypes.get_last_error()
            except BaseException as interruption:
                operation._state = OverlappedState.CLOSED
                entry.registry_phase = _RegistryPhase.CLOSED
                remove = _RegistryTransition(
                    mode=_RegistryTransitionMode.CLOSED_REMOVE,
                    entry_token=registry_token,
                    expected_entry=entry,
                    expected_cell=cell,
                    consume_state=_RegistryConsumeMarker(_RegistryConsumeState.NEVER),
                )
                _commit_registry_transition(_REGISTRY, remove)
                _raise_interruption(
                    interruption=interruption,
                    reason=FailureReason.NATIVE_FAILURE,
                    stage=LifecycleStage.CREATE_EVENT,
                    owner=None,
                )
            operation._state = OverlappedState.CLOSED
            entry.registry_phase = _RegistryPhase.CLOSED
            remove = _RegistryTransition(
                mode=_RegistryTransitionMode.CLOSED_REMOVE,
                entry_token=registry_token,
                expected_entry=entry,
                expected_cell=cell,
                consume_state=_RegistryConsumeMarker(_RegistryConsumeState.NEVER),
            )
            _commit_registry_transition(_REGISTRY, remove)
            raise OverlappedPipeError(
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.CREATE_EVENT,
                winerror=winerror if winerror else None,
                completion=None,
                resource_owner=None,
            )
        overlapped.hEvent = event_handle
        operation._event_handle = event_handle
        operation._state = OverlappedState.NEW
        entry.registry_phase = _RegistryPhase.LIVE
        return operation

    @staticmethod
    def _sample_before_submit(operation):
        try:
            now = _monotonic()
        except BaseException as interruption:
            operation._state = OverlappedState.NEW
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.SUBMIT,
                owner=operation,
            )
        if now >= operation._absolute_deadline:
            operation._state = OverlappedState.NEW
            operation._raise_failure(
                stage=LifecycleStage.SUBMIT,
                reason=FailureReason.DEADLINE_EXCEEDED,
                winerror=None,
            )

    def _set_completion(
        self,
        *,
        native_outcome,
        bytes_transferred,
        winerror,
        deadline_exceeded=False,
        cancel_outcome=CancelOutcome.NOT_ATTEMPTED,
        cancel_winerror=None,
    ):
        if self._cancel_outcome is not CancelOutcome.NOT_ATTEMPTED:
            cancel_outcome = self._cancel_outcome
            cancel_winerror = self._cancel_winerror
        self._completion = Completion(
            operation=self._kind,
            native_outcome=native_outcome,
            bytes_transferred=bytes_transferred,
            winerror=winerror,
            deadline_exceeded=deadline_exceeded,
            cancel_outcome=cancel_outcome,
            cancel_winerror=cancel_winerror,
        )
        self._state = OverlappedState.DRAINED

    def _materialize_completed(self):
        if self._completion is not None:
            self._state = OverlappedState.DRAINED
            return self._completion
        self._set_completion(
            native_outcome=self._completed_native_outcome,
            bytes_transferred=self._completed_bytes_transferred,
            winerror=self._completed_winerror,
        )
        return self._completion

    def _capture_completed_result(self, *, native_outcome, bytes_transferred, winerror):
        self._completed_native_outcome = native_outcome
        self._completed_bytes_transferred = bytes_transferred
        self._completed_winerror = winerror
        self._state = OverlappedState.COMPLETED

    def _raise_failure(self, *, stage, reason, winerror):
        if self._completion is not None and reason is not FailureReason.LIFECYCLE_VIOLATION:
            self._terminal_error_reason = reason
            self._terminal_error_stage = stage
            self._terminal_error_winerror = winerror
        raise OverlappedPipeError(
            reason=reason,
            stage=stage,
            winerror=winerror,
            completion=self._completion,
            resource_owner=self,
        )

    def _replay_close_quarantine(self, *, stage):
        if (
            self._terminal_error_reason is not None
            and self._terminal_error_reason is not FailureReason.CLOSE_QUARANTINED
        ):
            raise OverlappedPipeError(
                reason=self._terminal_error_reason,
                stage=self._terminal_error_stage,
                winerror=self._terminal_error_winerror,
                completion=self._completion,
                resource_owner=self,
            )
        if self._completion is not None:
            return self._completion
        self._raise_failure(
            stage=stage,
            reason=FailureReason.LIFECYCLE_VIOLATION,
            winerror=None,
        )

    def _raise_for_owner(self, *, stage, reason, winerror, owner):
        raise OverlappedPipeError(
            reason=reason,
            stage=stage,
            winerror=winerror,
            completion=owner.completion if owner is not None else None,
            resource_owner=owner,
        )

    def _raise_drain_pending(self, *, winerror=None):
        raise OverlappedPipeError(
            reason=FailureReason.DRAIN_PENDING,
            stage=LifecycleStage.DRAIN,
            winerror=winerror,
            completion=self._completion,
            resource_owner=self,
        )

    def _prepare_replay(self, *, mode, payload, context):
        self._replay_mode = mode
        self._replay_payload = payload
        self._replay_context = context
        self._replay_sampled_now = None
        self._replay_transition = None
        self._replay_committed = False

    def _clear_replay(self):
        self._replay_mode = None
        self._replay_payload = None
        self._replay_context = None
        self._replay_dispatch = None
        self._replay_sampled_now = None
        self._replay_transition = None
        self._replay_committed = False

    def _resume_wait_replay(self):
        mode = self._replay_mode
        if mode is None:
            return _NONTERMINAL_RESULT
        context = self._replay_context
        dispatch = self._replay_dispatch or context
        if not self._replay_committed:
            if (
                mode is _WaitTransitionMode.RESULT_HANDOFF
                and self._replay_transition is not None
            ):
                # The result leaf is published after the native observation.
                # If its commit failed before mutation, the observation itself
                # is ambiguous and must be repeated on the same OVERLAPPED.
                prior_state = self._replay_payload[0]
                origin = (
                    self._replay_payload[4]
                    if len(self._replay_payload) > 4
                    else "result"
                )
                self._clear_replay()
                self._state = prior_state
                return self._observe_result(origin=origin)
            if mode is _WaitTransitionMode.OBSERVATION_COMMIT:
                if self._replay_transition is None:
                    self._advance_wait_snapshot(
                        mode=mode,
                        stage=LifecycleStage.WAIT,
                    )
                else:
                    # The previous attempt raised before the observation leaf
                    # was published; replay the native wait on the next public
                    # call rather than retrying a stale leaf.
                    self._clear_replay()
                    return _NONTERMINAL_RESULT
            elif self._replay_transition is None:
                self._advance_wait_snapshot(
                    mode=mode,
                    stage=(
                        LifecycleStage.RESULT
                        if mode is _WaitTransitionMode.RESULT_HANDOFF
                        else LifecycleStage.WAIT
                    ),
                )
            else:
                self._advance_wait_snapshot(
                    mode=mode,
                    stage=(
                        LifecycleStage.RESULT
                        if mode is _WaitTransitionMode.RESULT_HANDOFF
                        else LifecycleStage.WAIT
                    ),
                )

        if mode is _WaitTransitionMode.OBSERVATION_COMMIT:
            if (
                isinstance(self._replay_payload, tuple)
                and len(self._replay_payload) == 2
            ):
                result, resume_state = self._replay_payload
            else:
                result = self._replay_payload
                resume_state = self._state
            try:
                sampled_now = _monotonic()
            except BaseException as interruption:
                self._state = OverlappedState.PENDING
                _raise_interruption(
                    interruption=interruption,
                    reason=FailureReason.NATIVE_FAILURE,
                    stage=LifecycleStage.WAIT,
                    owner=self,
                )
            self._replay_sampled_now = sampled_now
            if result == 0:
                self._prepare_replay(
                    mode=_WaitTransitionMode.OBJECT_DISPATCH,
                    payload=None,
                    context=context,
                )
            else:
                self._prepare_replay(
                    mode=_WaitTransitionMode.NONOBJECT_HANDOFF,
                    payload=(result, resume_state),
                    context=context,
                )
                self._replay_sampled_now = sampled_now
            return self._resume_wait_replay()

        if mode is _WaitTransitionMode.OBJECT_DISPATCH:
            self._result_origin = context
            self._clear_replay()
            self._wait_carrier = False
            self._result_proven = True
            observed = self._observe_result(origin=context)
            return _NONTERMINAL_RESULT if observed is None else observed

        if mode is _WaitTransitionMode.RESULT_HANDOFF:
            prior_state, ok, count_value, winerror, *origin_tail = self._replay_payload
            origin = origin_tail[0] if origin_tail else "result"
            self._clear_replay()
            observed = self._interpret_result_payload(
                prior_state=prior_state,
                ok=ok,
                count_value=count_value,
                winerror=winerror,
                origin=origin,
            )
            return _NONTERMINAL_RESULT if observed is None else observed

        if mode is _WaitTransitionMode.NONOBJECT_HANDOFF:
            result = self._replay_payload
            sampled_now = self._replay_sampled_now
            sampled_deadline_crossed = (
                sampled_now is not None and sampled_now >= self._absolute_deadline
            )
            # An ACK record is a one-shot delivery handoff.  If its commit
            # itself was interrupted after the cell swap, the next public
            # call must consume the record and resume ordinary operation; it
            # must not manufacture a second ACK leaf or replay the old wait
            # error forever.
            if (
                isinstance(result, tuple)
                and len(result) >= 2
                and result[0] == "ack"
            ):
                self._clear_replay()
                return _NONTERMINAL_RESULT
            if (
                isinstance(result, tuple)
                and len(result) == 2
            ):
                result, resume_state = result
            else:
                resume_state = self._state
            self._clear_replay()
            if result == 258:
                if dispatch == "wait":
                    if sampled_now is None:
                        try:
                            sampled_now = _monotonic()
                        except BaseException as interruption:
                            self._state = OverlappedState.PENDING
                            _raise_interruption(
                                interruption=interruption,
                                reason=FailureReason.NATIVE_FAILURE,
                                stage=LifecycleStage.WAIT,
                                owner=self,
                            )
                    before_deadline = sampled_now < self._absolute_deadline
                    if before_deadline:
                        return self._wait_to_deadline_unlocked()
                    self._result_origin = "wait"
                    self._drain_wait_already_done = True
                    return self._cancel_and_drain_unlocked()
                # A cancel entry may be resuming a wait observation that was
                # already sampled at/after its deadline.  Carry that fact into
                # the cancel path so it performs CancelIoEx -> GOR directly,
                # without inventing a second WaitForSingleObject poll.
                self._drain_wait_already_done = sampled_deadline_crossed
                return self._cancel_and_drain_unlocked()
                return _NONTERMINAL_RESULT
            # WAIT_FAILED and unexpected values are outward wait observations.
            # Publish a second, fresh handoff leaf before delivering the error;
            # this makes a post-commit interruption replayable without native
            # calls and prevents the old observation leaf from being replayed.
            if sampled_deadline_crossed:
                # Once the post-Wait sample has crossed the deadline, the
                # deadline decision outranks the raw WAIT_FAILED/unexpected
                # value.  The observation leaf is already committed; proceed
                # to the single cancel/drain path instead of delivering a
                # stale wait error.
                if dispatch == "wait":
                    self._result_origin = "wait"
                self._drain_wait_already_done = True
                return self._cancel_and_drain_unlocked()
            self._prepare_replay(
                mode=_WaitTransitionMode.NONOBJECT_HANDOFF,
                payload=("ack", result, resume_state),
                context=context,
            )
            self._advance_wait_snapshot(
                mode=_WaitTransitionMode.NONOBJECT_HANDOFF,
                stage=LifecycleStage.WAIT,
            )
            self._clear_replay()
            if result == 0xFFFFFFFF:
                try:
                    winerror = ctypes.get_last_error()
                except BaseException as interruption:
                    self._state = resume_state
                    _raise_interruption(
                        interruption=interruption,
                        reason=FailureReason.NATIVE_FAILURE,
                        stage=LifecycleStage.WAIT,
                        owner=self,
                    )
                self._state = resume_state
                self._raise_failure(
                    stage=LifecycleStage.WAIT,
                    reason=FailureReason.NATIVE_FAILURE,
                    winerror=winerror if winerror else None,
                )
            self._state = resume_state
            self._raise_failure(
                stage=LifecycleStage.WAIT,
                reason=FailureReason.LIFECYCLE_VIOLATION,
                winerror=None,
            )

        return _NONTERMINAL_RESULT

    def _advance_wait_snapshot(self, *, mode, stage):
        transition = _WaitTransition(
            mode=mode,
            expected_snapshot=self._cell.wait_snapshot,
            target_snapshot=object(),
        )
        self._replay_transition = transition
        try:
            result = _commit_wait_transition(self._cell, transition)
        except BaseException as interruption:
            self._replay_committed = (
                self._cell.wait_snapshot is transition.target_snapshot
            )
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=stage,
                owner=self,
            )
        if result is not _PrimitiveCommitResult.COMMITTED:
            self._raise_failure(
                stage=stage,
                reason=FailureReason.LIFECYCLE_VIOLATION,
                winerror=None,
            )
        self._replay_committed = True
        return transition

    def _interpret_result_payload(
        self, *, prior_state, ok, count_value, winerror, origin="result"
    ):
        origin_deadline_stage = {
            "submit": LifecycleStage.SUBMIT,
            "wait": LifecycleStage.WAIT,
            "drain": LifecycleStage.DRAIN,
        }.get(origin)

        def post_result_deadline(*, native_outcome, bytes_transferred, error=None):
            """Apply the post-GOR sample for wait/drain-origin results."""
            if origin_deadline_stage is None:
                return False
            try:
                now = _monotonic()
            except BaseException as interruption:
                self._capture_completed_result(
                    native_outcome=native_outcome,
                    bytes_transferred=bytes_transferred,
                    winerror=error,
                )
                _raise_interruption(
                    interruption=interruption,
                    reason=FailureReason.NATIVE_FAILURE,
                    stage=LifecycleStage.RESULT,
                    owner=self,
                )
            if now < self._absolute_deadline:
                return False
            deadline_winerror = (
                None
                if (
                    self._cancel_outcome is CancelOutcome.UNKNOWN
                    and self._cancel_origin_state is OverlappedState.SUBMIT_UNKNOWN
                )
                else error
            )
            self._set_completion(
                native_outcome=native_outcome,
                bytes_transferred=bytes_transferred,
                winerror=error,
                deadline_exceeded=True,
            )
            self._terminal_error_reason = FailureReason.DEADLINE_EXCEEDED
            self._terminal_error_stage = origin_deadline_stage
            self._terminal_error_winerror = deadline_winerror
            raise OverlappedPipeError(
                reason=FailureReason.DEADLINE_EXCEEDED,
                stage=origin_deadline_stage,
                winerror=deadline_winerror,
                completion=self._completion,
                resource_owner=self,
            )

        if ok:
            if (
                (
                    prior_state is OverlappedState.SUBMIT_UNKNOWN
                    or (
                        prior_state is OverlappedState.CANCEL_UNKNOWN
                        and self._cancel_origin_state is OverlappedState.SUBMIT_UNKNOWN
                    )
                )
                and not self._result_proven
            ):
                self._state = (
                    OverlappedState.CANCEL_UNKNOWN
                    if prior_state is OverlappedState.CANCEL_UNKNOWN
                    else OverlappedState.SUBMIT_UNKNOWN
                )
                return _NONTERMINAL_RESULT
            if self._kind is OperationKind.CONNECT:
                if origin in ("submit", "wait", "drain"):
                    post_result_deadline(
                        native_outcome=NativeOutcome.SUCCESS,
                        bytes_transferred=None,
                    )
                elif prior_state is OverlappedState.RESULT_REQUIRED:
                    try:
                        _monotonic()
                    except BaseException as interruption:
                        self._capture_completed_result(
                            native_outcome=NativeOutcome.SUCCESS,
                            bytes_transferred=None,
                            winerror=None,
                        )
                        _raise_interruption(
                            interruption=interruption,
                            reason=FailureReason.NATIVE_FAILURE,
                            stage=LifecycleStage.RESULT,
                            owner=self,
                        )
                self._set_completion(
                    native_outcome=NativeOutcome.SUCCESS,
                    bytes_transferred=None,
                    winerror=None,
                )
                return self._completion
            if count_value != self._frame_length:
                post_result_deadline(
                    native_outcome=NativeOutcome.SUCCESS,
                    bytes_transferred=count_value,
                )
                self._set_completion(
                    native_outcome=NativeOutcome.SUCCESS,
                    bytes_transferred=count_value,
                    winerror=None,
                )
                self._raise_failure(
                    stage=LifecycleStage.RESULT,
                    reason=FailureReason.BYTE_COUNT_MISMATCH,
                    winerror=None,
                )
            if origin in ("submit", "wait", "drain"):
                post_result_deadline(
                    native_outcome=NativeOutcome.SUCCESS,
                    bytes_transferred=count_value,
                )
            elif prior_state is OverlappedState.RESULT_REQUIRED:
                try:
                    _monotonic()
                except BaseException as interruption:
                    self._capture_completed_result(
                        native_outcome=NativeOutcome.SUCCESS,
                        bytes_transferred=count_value,
                        winerror=None,
                    )
                    _raise_interruption(
                        interruption=interruption,
                        reason=FailureReason.NATIVE_FAILURE,
                        stage=LifecycleStage.RESULT,
                        owner=self,
                    )
            self._set_completion(
                native_outcome=NativeOutcome.SUCCESS,
                bytes_transferred=count_value,
                winerror=None,
            )
            return self._completion
        if winerror == 996:
            # A signalled event can race the final kernel bookkeeping.  The
            # overlapped pointer remains the sole source of truth; keep the
            # operation live and let the caller perform another bounded
            # observation rather than manufacturing a terminal result.
            try:
                _monotonic()
            except BaseException as interruption:
                self._state = (
                    OverlappedState.SUBMIT_UNKNOWN
                    if prior_state is OverlappedState.SUBMIT_UNKNOWN
                    else OverlappedState.PENDING
                )
                _raise_interruption(
                    interruption=interruption,
                    reason=FailureReason.NATIVE_FAILURE,
                    stage=LifecycleStage.RESULT,
                    owner=self,
                )
            self._state = (
                OverlappedState.SUBMIT_UNKNOWN
                if prior_state is OverlappedState.SUBMIT_UNKNOWN
                else OverlappedState.PENDING
            )
            self._pending_result_winerror = None
            return None
        if (
            (
                prior_state is OverlappedState.SUBMIT_UNKNOWN
                or (
                    prior_state is OverlappedState.CANCEL_UNKNOWN
                    and self._cancel_origin_state is OverlappedState.SUBMIT_UNKNOWN
                )
            )
            and not self._result_proven
            and winerror != 0
        ):
            # A failed/aborted/incomplete-looking result observed without an
            # event or accepted cancellation is not proof that this owner
            # submitted the operation.  Keep the owner live and require a
            # later observation on the same OVERLAPPED pointer.
            self._state = (
                OverlappedState.CANCEL_UNKNOWN
                if prior_state is OverlappedState.CANCEL_UNKNOWN
                else OverlappedState.SUBMIT_UNKNOWN
            )
            self._pending_result_winerror = None
            return _NONTERMINAL_RESULT
        if winerror == 0:
            self._state = OverlappedState.RESULT_UNKNOWN
            self._raise_failure(
                stage=LifecycleStage.RESULT,
                reason=FailureReason.NATIVE_FAILURE,
                winerror=None,
            )
        if winerror == 995:
            post_result_deadline(
                native_outcome=NativeOutcome.CANCELLED,
                bytes_transferred=None,
                error=winerror,
            )
            self._set_completion(
                native_outcome=NativeOutcome.CANCELLED,
                bytes_transferred=None,
                winerror=winerror,
            )
            return self._completion
        post_result_deadline(
            native_outcome=NativeOutcome.FAILURE,
            bytes_transferred=None,
            error=winerror,
        )
        self._set_completion(
            native_outcome=NativeOutcome.FAILURE,
            bytes_transferred=None,
            winerror=winerror,
        )
        self._raise_failure(
            stage=LifecycleStage.RESULT,
            reason=FailureReason.NATIVE_FAILURE,
            winerror=winerror,
        )

    def _observe_result(self, *, origin="result"):
        if origin == "result" and self._result_origin is not None:
            origin = self._result_origin
        count = wintypes.DWORD(0)
        prior_state = self._state
        self._state = OverlappedState.RESULT_IN_FLIGHT
        try:
            ok = _kernel32.GetOverlappedResult(
                self._pipe_handle,
                ctypes.byref(self._overlapped),
                ctypes.byref(count),
                False,
            )
        except BaseException as interruption:
            self._state = OverlappedState.RESULT_UNKNOWN
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.RESULT,
                owner=self,
                completion=None,
            )
        finally:
            _release_injected_hooks(
                _kernel32.GetOverlappedResult,
                "GetOverlappedResult",
            )
        if ok:
            winerror = None
        else:
            try:
                winerror = ctypes.get_last_error()
            except BaseException as interruption:
                self._state = OverlappedState.RESULT_UNKNOWN
                _raise_interruption(
                    interruption=interruption,
                    reason=FailureReason.NATIVE_FAILURE,
                    stage=LifecycleStage.RESULT,
                    owner=self,
                    completion=None,
                )
        self._prepare_replay(
            mode=_WaitTransitionMode.RESULT_HANDOFF,
            payload=(prior_state, bool(ok), int(count.value), winerror, origin),
            context=origin,
        )
        replayed = self._resume_wait_replay()
        if replayed is not _NONTERMINAL_RESULT:
            return replayed
        return self._interpret_result_payload(
            prior_state=prior_state,
            ok=bool(ok),
            count_value=int(count.value),
            winerror=winerror,
            origin=origin,
        )

    @classmethod
    def start_connect(cls, *, pipe_handle, absolute_deadline):
        operation = cls._allocate(
            pipe_handle=pipe_handle,
            kind=OperationKind.CONNECT,
            frame=None,
            absolute_deadline=absolute_deadline,
        )
        cls._sample_before_submit(operation)
        operation._state = OverlappedState.SUBMIT_IN_FLIGHT
        try:
            ok = _kernel32.ConnectNamedPipe(
                pipe_handle,
                ctypes.byref(operation._overlapped),
            )
        except BaseException as interruption:
            operation._state = OverlappedState.SUBMIT_UNKNOWN
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.SUBMIT,
                owner=operation,
            )
        finally:
            _release_injected_hooks(_kernel32.ConnectNamedPipe, "ConnectNamedPipe")
        try:
            submit_deadline_exceeded = _monotonic() >= operation._absolute_deadline
        except BaseException as interruption:
            operation._state = OverlappedState.SUBMIT_UNKNOWN
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.SUBMIT,
                owner=operation,
            )
        if ok:
            operation._set_completion(
                native_outcome=NativeOutcome.SUCCESS,
                bytes_transferred=None,
                winerror=None,
            )
            if submit_deadline_exceeded:
                operation._raise_submit_deadline()
            return operation
        try:
            winerror = ctypes.get_last_error()
        except BaseException as interruption:
            operation._state = OverlappedState.SUBMIT_UNKNOWN
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.SUBMIT,
                owner=operation,
            )
        if winerror == 535:
            operation._set_completion(
                native_outcome=NativeOutcome.SUCCESS,
                bytes_transferred=None,
                winerror=None,
            )
            if submit_deadline_exceeded:
                operation._raise_submit_deadline()
            return operation
        if winerror == 997:
            operation._state = OverlappedState.PENDING
            if submit_deadline_exceeded:
                operation._result_origin = "submit"
                try:
                    operation._cancel_and_drain_unlocked()
                except OverlappedPipeError as error:
                    operation._raise_submit_deadline(cause=error)
                operation._raise_submit_deadline()
            return operation
        if winerror == 0:
            operation._state = OverlappedState.SUBMIT_UNKNOWN
            operation._raise_failure(
                stage=LifecycleStage.SUBMIT,
                reason=FailureReason.NATIVE_FAILURE,
                winerror=None,
            )
        operation._set_completion(
            native_outcome=NativeOutcome.FAILURE,
            bytes_transferred=None,
            winerror=winerror,
        )
        if submit_deadline_exceeded:
            operation._raise_submit_deadline()
        operation._raise_failure(
            stage=LifecycleStage.SUBMIT,
            reason=FailureReason.NATIVE_FAILURE,
            winerror=winerror,
        )

    @classmethod
    def start_write(cls, *, pipe_handle, immutable_frame, absolute_deadline):
        frame = cls._validate_frame(immutable_frame)
        operation = cls._allocate(
            pipe_handle=pipe_handle,
            kind=OperationKind.WRITE,
            frame=frame,
            absolute_deadline=absolute_deadline,
        )
        cls._sample_before_submit(operation)
        buffer_pointer = ctypes.cast(operation._frame_buffer, wintypes.LPVOID)
        operation._state = OverlappedState.SUBMIT_IN_FLIGHT
        try:
            ok = _kernel32.WriteFile(
                pipe_handle,
                buffer_pointer,
                len(frame),
                None,
                ctypes.byref(operation._overlapped),
            )
        except BaseException as interruption:
            operation._state = OverlappedState.SUBMIT_UNKNOWN
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.SUBMIT,
                owner=operation,
            )
        finally:
            _release_injected_hooks(_kernel32.WriteFile, "WriteFile")
        try:
            submit_deadline_exceeded = _monotonic() >= operation._absolute_deadline
        except BaseException as interruption:
            operation._state = OverlappedState.RESULT_REQUIRED
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.SUBMIT,
                owner=operation,
            )
        if ok:
            operation._state = OverlappedState.RESULT_REQUIRED
            try:
                operation._observe_result(origin="submit")
            except OverlappedPipeError as error:
                if not submit_deadline_exceeded:
                    raise
                operation._raise_submit_deadline(cause=error)
            if submit_deadline_exceeded:
                operation._raise_submit_deadline()
            return operation
        try:
            winerror = ctypes.get_last_error()
        except BaseException as interruption:
            operation._state = OverlappedState.SUBMIT_UNKNOWN
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.SUBMIT,
                owner=operation,
            )
        if winerror == 997:
            operation._state = OverlappedState.PENDING
            if submit_deadline_exceeded:
                operation._result_origin = "submit"
                try:
                    operation._cancel_and_drain_unlocked()
                except OverlappedPipeError as error:
                    operation._raise_submit_deadline(cause=error)
                operation._raise_submit_deadline()
            return operation
        if winerror == 0:
            operation._state = OverlappedState.SUBMIT_UNKNOWN
            operation._raise_failure(
                stage=LifecycleStage.SUBMIT,
                reason=FailureReason.NATIVE_FAILURE,
                winerror=None,
            )
        operation._set_completion(
            native_outcome=NativeOutcome.FAILURE,
            bytes_transferred=None,
            winerror=winerror,
        )
        if submit_deadline_exceeded:
            operation._raise_submit_deadline()
        operation._raise_failure(
            stage=LifecycleStage.SUBMIT,
            reason=FailureReason.NATIVE_FAILURE,
            winerror=winerror,
        )

    @property
    def state(self) -> OverlappedState:
        return self._state

    @property
    def completion(self) -> Completion | None:
        return self._completion

    def _mark_deadline_exceeded(self):
        if self._completion is None or self._completion.deadline_exceeded:
            return
        self._completion = Completion(
            operation=self._completion.operation,
            native_outcome=self._completion.native_outcome,
            bytes_transferred=self._completion.bytes_transferred,
            winerror=self._completion.winerror,
            deadline_exceeded=True,
            cancel_outcome=self._completion.cancel_outcome,
            cancel_winerror=self._completion.cancel_winerror,
        )

    def _raise_submit_deadline(self, *, cause=None):
        """Promote a completed submit path to its submit-deadline contract."""
        self._mark_deadline_exceeded()
        self._terminal_error_reason = FailureReason.DEADLINE_EXCEEDED
        self._terminal_error_stage = LifecycleStage.SUBMIT
        self._terminal_error_winerror = (
            self._completion.winerror if self._completion is not None else None
        )
        error = OverlappedPipeError(
            reason=FailureReason.DEADLINE_EXCEEDED,
            stage=LifecycleStage.SUBMIT,
            winerror=self._terminal_error_winerror,
            completion=self._completion,
            resource_owner=self,
        )
        if cause is None:
            raise error
        raise error from cause

    def _run_serialized(self, *, stage, operation):
        if not self._cell.acquire_operation():
            raise OverlappedPipeError(
                reason=FailureReason.LIFECYCLE_VIOLATION,
                stage=stage,
                winerror=None,
                completion=self._completion,
                resource_owner=self,
            )
        try:
            return operation()
        finally:
            self._cell.release_if_owned_by_current_thread(_OPERATION_PURPOSE)

    def wait_to_deadline(self):
        return self._run_serialized(
            stage=LifecycleStage.WAIT,
            operation=self._wait_to_deadline_unlocked,
        )

    def _wait_to_deadline_unlocked(self):
        if self._cell.operation is not self:
            self._raise_for_owner(
                stage=LifecycleStage.WAIT,
                reason=FailureReason.LIFECYCLE_VIOLATION,
                winerror=None,
                owner=self._cell.operation,
            )
        if self._replay_mode is not None:
            self._replay_dispatch = "wait"
            if self._replay_context is None:
                self._replay_context = "wait"
            replayed = self._resume_wait_replay()
            if replayed is not _NONTERMINAL_RESULT:
                return replayed
        if self._state is OverlappedState.CLOSED:
            if self._terminal_error_reason is not None:
                raise OverlappedPipeError(
                    reason=self._terminal_error_reason,
                    stage=self._terminal_error_stage,
                    winerror=self._terminal_error_winerror,
                    completion=self._completion,
                    resource_owner=self,
                )
            if self._completion is not None:
                return self._completion
            self._raise_failure(
                stage=LifecycleStage.WAIT,
                reason=FailureReason.LIFECYCLE_VIOLATION,
                winerror=None,
            )
        if self._state in (
            OverlappedState.CLOSE_FAILED_QUARANTINED,
            OverlappedState.CLOSE_UNKNOWN,
        ):
            return self._replay_close_quarantine(stage=LifecycleStage.WAIT)
        if self._state is OverlappedState.DRAINED:
            if self._terminal_error_reason is not None:
                raise OverlappedPipeError(
                    reason=self._terminal_error_reason,
                    stage=self._terminal_error_stage,
                    winerror=self._terminal_error_winerror,
                    completion=self._completion,
                    resource_owner=self,
                )
            return self._completion
        if self._state is OverlappedState.COMPLETED:
            try:
                now = _monotonic()
            except BaseException as interruption:
                _raise_interruption(
                    interruption=interruption,
                    reason=FailureReason.NATIVE_FAILURE,
                    stage=LifecycleStage.WAIT,
                    owner=self,
                )
            completion = self._materialize_completed()
            if now >= self._absolute_deadline:
                self._mark_deadline_exceeded()
                self._terminal_error_reason = FailureReason.DEADLINE_EXCEEDED
                self._terminal_error_stage = {
                    "wait": LifecycleStage.WAIT,
                    "drain": LifecycleStage.DRAIN,
                }.get(self._result_origin, LifecycleStage.SUBMIT)
                self._terminal_error_winerror = completion.winerror
                raise OverlappedPipeError(
                    reason=FailureReason.DEADLINE_EXCEEDED,
                    stage=self._terminal_error_stage,
                    winerror=completion.winerror,
                    completion=self._completion,
                    resource_owner=self,
                )
            return completion
        if self._wait_carrier:
            self._wait_carrier = False
            self._result_origin = self._result_origin or "wait"
            self._advance_wait_snapshot(
                mode=_WaitTransitionMode.OBJECT_DISPATCH,
                stage=LifecycleStage.WAIT,
            )
            self._result_proven = True
            observed = self._observe_result(origin="wait")
            if observed is None:
                self._state = OverlappedState.PENDING
                self._raise_drain_pending()
            if observed is _NONTERMINAL_RESULT:
                self._raise_drain_pending(
                    winerror=self._pending_result_winerror,
                )
            return observed
        if self._state in (
            OverlappedState.RESULT_UNKNOWN,
            OverlappedState.RESULT_IN_FLIGHT,
        ):
            observed = self._observe_result(origin="wait")
            if observed is None:
                return self._wait_to_deadline_unlocked()
            if observed is _NONTERMINAL_RESULT:
                self._raise_drain_pending(
                    winerror=self._pending_result_winerror,
                )
            return observed
        if self._state is OverlappedState.RESULT_REQUIRED:
            observed = self._observe_result(origin="wait")
            if observed is None:
                self._result_resume_pending = True
                try:
                    _monotonic()
                except BaseException as interruption:
                    self._result_resume_pending = False
                    self._state = OverlappedState.PENDING
                    _raise_interruption(
                        interruption=interruption,
                        reason=FailureReason.NATIVE_FAILURE,
                        stage=LifecycleStage.RESULT,
                        owner=self,
                    )
                    return self._wait_to_deadline_unlocked()
            if observed is _NONTERMINAL_RESULT:
                self._raise_drain_pending(
                    winerror=self._pending_result_winerror,
                )
            return observed
        if self._state not in (
            OverlappedState.PENDING,
            OverlappedState.SUBMIT_UNKNOWN,
            OverlappedState.CANCEL_UNKNOWN,
        ):
            self._raise_failure(
                stage=LifecycleStage.WAIT,
                reason=FailureReason.LIFECYCLE_VIOLATION,
                winerror=None,
            )
        try:
            now = _monotonic()
        except BaseException as interruption:
            self._state = OverlappedState.PENDING
            stage = (
                LifecycleStage.RESULT
                if self._result_resume_pending
                else LifecycleStage.WAIT
            )
            self._result_resume_pending = False
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=stage,
                owner=self,
            )
        if now >= self._absolute_deadline:
            self._result_origin = "wait"
            self._drain_wait_already_done = True
            return self._cancel_and_drain_unlocked()
        remaining = self._absolute_deadline - now
        if remaining >= MAX_WAIT_SLICE_MS / 1000:
            milliseconds = MAX_WAIT_SLICE_MS
        else:
            milliseconds = max(0, int(math.ceil(remaining * 1000)))
        wait_interrupted = False
        try:
            result = _kernel32.WaitForSingleObject(self._event_handle, milliseconds)
        except BaseException as interruption:
            wait_interrupted = True
            self._state = OverlappedState.PENDING
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.WAIT,
                owner=self,
            )
        finally:
            _release_injected_hooks(
                _kernel32.WaitForSingleObject,
                "WaitForSingleObject",
                preserve_enter=not wait_interrupted,
            )
        self._prepare_replay(
            mode=_WaitTransitionMode.OBSERVATION_COMMIT,
            payload=(result, self._state),
            context="wait",
        )
        replayed = self._resume_wait_replay()
        if replayed is not _NONTERMINAL_RESULT:
            return replayed
        return self._wait_to_deadline_unlocked()

    def _drain_after_cancel(self):
        """Observe the same OVERLAPPED after a single cancel attempt."""
        already_waited = self._drain_wait_already_done
        self._drain_wait_already_done = False
        direct_first = already_waited or self._cancel_origin_state in (
            OverlappedState.RESULT_UNKNOWN,
            OverlappedState.RESULT_REQUIRED,
        )
        followup_after_incomplete = (
            not already_waited
            and self._cancel_origin_state
            in (OverlappedState.RESULT_UNKNOWN, OverlappedState.RESULT_REQUIRED)
        )

        if not direct_first:
            wait_interrupted = False
            try:
                timeout_ms = (
                    0
                    if (
                        self._cancel_origin_state
                        is OverlappedState.SUBMIT_UNKNOWN
                        and self._cancel_outcome
                        is not CancelOutcome.ACCEPTED
                    )
                    or _monotonic() >= self._absolute_deadline
                    else MAX_WAIT_SLICE_MS
                )
            except BaseException as interruption:
                self._state = OverlappedState.CANCEL_UNKNOWN
                _raise_interruption(
                    interruption=interruption,
                    reason=FailureReason.NATIVE_FAILURE,
                    stage=LifecycleStage.DRAIN,
                    owner=self,
                )
            try:
                result = _kernel32.WaitForSingleObject(
                    self._event_handle,
                    timeout_ms,
                )
            except BaseException as interruption:
                wait_interrupted = True
                self._state = OverlappedState.CANCEL_UNKNOWN
                _raise_interruption(
                    interruption=interruption,
                    reason=FailureReason.NATIVE_FAILURE,
                    stage=LifecycleStage.DRAIN,
                    owner=self,
                )
            finally:
                _release_injected_hooks(
                    _kernel32.WaitForSingleObject,
                    "WaitForSingleObject",
                    preserve_enter=not wait_interrupted,
                )
            self._advance_wait_snapshot(
                mode=_WaitTransitionMode.OBSERVATION_COMMIT,
                stage=LifecycleStage.DRAIN,
            )
            if result == 0:
                self._advance_wait_snapshot(
                    mode=_WaitTransitionMode.OBJECT_DISPATCH,
                    stage=LifecycleStage.DRAIN,
                )
                self._result_proven = True
            elif result == 258:
                self._advance_wait_snapshot(
                    mode=_WaitTransitionMode.NONOBJECT_HANDOFF,
                    stage=LifecycleStage.DRAIN,
                )
                self._result_proven = self._cancel_outcome is CancelOutcome.ACCEPTED
            elif result == 0xFFFFFFFF:
                self._advance_wait_snapshot(
                    mode=_WaitTransitionMode.NONOBJECT_HANDOFF,
                    stage=LifecycleStage.DRAIN,
                )
                try:
                    winerror = ctypes.get_last_error()
                except BaseException as interruption:
                    self._state = OverlappedState.CANCEL_UNKNOWN
                    _raise_interruption(
                        interruption=interruption,
                        reason=FailureReason.NATIVE_FAILURE,
                        stage=LifecycleStage.DRAIN,
                        owner=self,
                    )
                self._state = OverlappedState.CANCEL_UNKNOWN
                self._raise_failure(
                    stage=LifecycleStage.DRAIN,
                    reason=FailureReason.NATIVE_FAILURE,
                    winerror=winerror if winerror else None,
                )
            else:
                self._advance_wait_snapshot(
                    mode=_WaitTransitionMode.NONOBJECT_HANDOFF,
                    stage=LifecycleStage.DRAIN,
                )
                self._state = OverlappedState.CANCEL_UNKNOWN
                self._raise_failure(
                    stage=LifecycleStage.DRAIN,
                    reason=FailureReason.LIFECYCLE_VIOLATION,
                    winerror=None,
                )

        observed = self._observe_result(origin=self._result_origin or "drain")
        if observed is _NONTERMINAL_RESULT:
            self._state = (
                OverlappedState.CANCEL_UNKNOWN
                if self._cancel_outcome is CancelOutcome.UNKNOWN
                else (
                    OverlappedState.SUBMIT_UNKNOWN
                    if self._cancel_outcome is CancelOutcome.NOT_FOUND
                    else OverlappedState.PENDING
                )
            )
            self._raise_drain_pending(
                winerror=(
                    self._pending_result_winerror
                    if self._state is OverlappedState.SUBMIT_UNKNOWN
                    else self._cancel_winerror
                )
            )
        if observed is None:
            # ERROR_IO_INCOMPLETE is non-terminal.  With the deadline wait
            # already consumed, there is no safe additional wait to invent;
            # otherwise take one more bounded slice before re-observing.
            if not followup_after_incomplete:
                self._state = (
                    OverlappedState.CANCEL_UNKNOWN
                    if self._cancel_outcome is CancelOutcome.UNKNOWN
                    else (
                        OverlappedState.SUBMIT_UNKNOWN
                        if self._cancel_outcome is CancelOutcome.NOT_FOUND
                        else OverlappedState.PENDING
                    )
                )
                self._raise_drain_pending(
                    winerror=(
                        self._pending_result_winerror
                        if self._state is OverlappedState.SUBMIT_UNKNOWN
                        else self._cancel_winerror
                    )
                )
            try:
                wait_interrupted = False
                try:
                    followup_timeout = (
                        0
                        if _monotonic() >= self._absolute_deadline
                        else MAX_WAIT_SLICE_MS
                    )
                except BaseException as interruption:
                    self._state = OverlappedState.CANCEL_UNKNOWN
                    _raise_interruption(
                        interruption=interruption,
                        reason=FailureReason.NATIVE_FAILURE,
                        stage=LifecycleStage.DRAIN,
                        owner=self,
                    )
                result = _kernel32.WaitForSingleObject(
                    self._event_handle,
                    followup_timeout,
                )
            except BaseException as interruption:
                wait_interrupted = True
                self._state = OverlappedState.CANCEL_UNKNOWN
                _raise_interruption(
                    interruption=interruption,
                    reason=FailureReason.NATIVE_FAILURE,
                    stage=LifecycleStage.DRAIN,
                    owner=self,
                )
            finally:
                _release_injected_hooks(
                    _kernel32.WaitForSingleObject,
                    "WaitForSingleObject",
                    preserve_enter=not wait_interrupted,
                )
            self._advance_wait_snapshot(
                mode=_WaitTransitionMode.OBSERVATION_COMMIT,
                stage=LifecycleStage.DRAIN,
            )
            if result == 0:
                self._advance_wait_snapshot(
                    mode=_WaitTransitionMode.OBJECT_DISPATCH,
                    stage=LifecycleStage.DRAIN,
                )
                self._result_proven = True
            elif result == 258:
                self._advance_wait_snapshot(
                    mode=_WaitTransitionMode.NONOBJECT_HANDOFF,
                    stage=LifecycleStage.DRAIN,
                )
                self._result_proven = self._cancel_outcome is CancelOutcome.ACCEPTED
            else:
                self._advance_wait_snapshot(
                    mode=_WaitTransitionMode.NONOBJECT_HANDOFF,
                    stage=LifecycleStage.DRAIN,
                )
                self._state = OverlappedState.PENDING
                self._raise_drain_pending(
                    winerror=(
                        self._pending_result_winerror
                        if self._state is OverlappedState.SUBMIT_UNKNOWN
                        else self._cancel_winerror
                    )
                )
            observed = self._observe_result(origin=self._result_origin or "drain")
            if observed is None or observed is _NONTERMINAL_RESULT:
                self._state = (
                    OverlappedState.CANCEL_UNKNOWN
                    if self._cancel_outcome is CancelOutcome.UNKNOWN
                    else (
                        OverlappedState.SUBMIT_UNKNOWN
                        if self._cancel_outcome is CancelOutcome.NOT_FOUND
                        else OverlappedState.PENDING
                    )
                )
                self._raise_drain_pending(winerror=self._cancel_winerror)

        if _monotonic() >= self._absolute_deadline:
            deadline_stage = {
                "submit": LifecycleStage.SUBMIT,
                "wait": LifecycleStage.WAIT,
                "drain": LifecycleStage.DRAIN,
            }.get(self._result_origin, LifecycleStage.DRAIN)
            deadline_winerror = (
                None
                if self._cancel_outcome is CancelOutcome.UNKNOWN
                else (
                    self._completion.winerror
                    if self._completion is not None
                    else self._cancel_winerror
                )
            )
            self._mark_deadline_exceeded()
            self._terminal_error_reason = FailureReason.DEADLINE_EXCEEDED
            self._terminal_error_stage = deadline_stage
            self._terminal_error_winerror = deadline_winerror
            raise OverlappedPipeError(
                reason=FailureReason.DEADLINE_EXCEEDED,
                stage=deadline_stage,
                winerror=deadline_winerror,
                completion=self._completion,
                resource_owner=self,
            )
        return observed

    def cancel_and_drain(self):
        return self._run_serialized(
            stage=LifecycleStage.DRAIN,
            operation=self._cancel_and_drain_unlocked,
        )

    def _cancel_and_drain_unlocked(self):
        if self._cell.operation is not self:
            self._raise_for_owner(
                stage=LifecycleStage.DRAIN,
                reason=FailureReason.LIFECYCLE_VIOLATION,
                winerror=None,
                owner=self._cell.operation,
            )
        if self._replay_mode is not None:
            self._replay_dispatch = "drain"
            if self._replay_context is None:
                self._replay_context = "drain"
            replayed = self._resume_wait_replay()
            if replayed is not _NONTERMINAL_RESULT:
                return replayed
        if self._state is OverlappedState.CLOSED:
            if self._terminal_error_reason is not None:
                raise OverlappedPipeError(
                    reason=self._terminal_error_reason,
                    stage=self._terminal_error_stage,
                    winerror=self._terminal_error_winerror,
                    completion=self._completion,
                    resource_owner=self,
                )
            if self._completion is not None:
                return self._completion
            self._raise_failure(
                stage=LifecycleStage.DRAIN,
                reason=FailureReason.LIFECYCLE_VIOLATION,
                winerror=None,
            )
        if self._state in (
            OverlappedState.CLOSE_FAILED_QUARANTINED,
            OverlappedState.CLOSE_UNKNOWN,
        ):
            return self._replay_close_quarantine(stage=LifecycleStage.DRAIN)
        if self._state is OverlappedState.DRAINED:
            if self._terminal_error_reason is not None:
                raise OverlappedPipeError(
                    reason=self._terminal_error_reason,
                    stage=self._terminal_error_stage,
                    winerror=self._terminal_error_winerror,
                    completion=self._completion,
                    resource_owner=self,
                )
            return self._completion
        if self._state is OverlappedState.COMPLETED:
            try:
                now = _monotonic()
            except BaseException as interruption:
                _raise_interruption(
                    interruption=interruption,
                    reason=FailureReason.NATIVE_FAILURE,
                    stage=LifecycleStage.DRAIN,
                    owner=self,
                )
            completion = self._materialize_completed()
            if now >= self._absolute_deadline:
                self._mark_deadline_exceeded()
                self._terminal_error_reason = FailureReason.DEADLINE_EXCEEDED
                self._terminal_error_stage = {
                    "wait": LifecycleStage.WAIT,
                    "drain": LifecycleStage.DRAIN,
                }.get(self._result_origin, LifecycleStage.SUBMIT)
                self._terminal_error_winerror = completion.winerror
                raise OverlappedPipeError(
                    reason=FailureReason.DEADLINE_EXCEEDED,
                    stage=self._terminal_error_stage,
                    winerror=completion.winerror,
                    completion=self._completion,
                    resource_owner=self,
                )
            return completion
        if self._wait_carrier:
            self._wait_carrier = False
            self._result_origin = self._result_origin or "drain"
            self._advance_wait_snapshot(
                mode=_WaitTransitionMode.OBJECT_DISPATCH,
                stage=LifecycleStage.WAIT,
            )
            self._result_proven = True
            observed = self._observe_result(origin=self._result_origin or "drain")
            if observed is None:
                self._state = OverlappedState.PENDING
                self._raise_drain_pending()
            if observed is _NONTERMINAL_RESULT:
                self._raise_drain_pending(
                    winerror=self._pending_result_winerror,
                )
            return observed
        if self._state is OverlappedState.RESULT_REQUIRED:
            observed = self._observe_result(origin=self._result_origin or "drain")
            if observed is None:
                try:
                    _monotonic()
                except BaseException as interruption:
                    self._state = OverlappedState.PENDING
                    _raise_interruption(
                        interruption=interruption,
                        reason=FailureReason.NATIVE_FAILURE,
                        stage=LifecycleStage.RESULT,
                        owner=self,
                    )
                self._state = OverlappedState.PENDING
            elif observed is _NONTERMINAL_RESULT:
                self._raise_drain_pending(
                    winerror=self._pending_result_winerror,
                )
            else:
                return observed
        if self._state not in (
            OverlappedState.PENDING,
            OverlappedState.SUBMIT_UNKNOWN,
            OverlappedState.RESULT_UNKNOWN,
            OverlappedState.CANCEL_UNKNOWN,
            OverlappedState.CANCEL_IN_FLIGHT,
        ):
            self._raise_failure(
                stage=LifecycleStage.DRAIN,
                reason=FailureReason.LIFECYCLE_VIOLATION,
                winerror=None,
            )
        if self._cancel_attempted:
            return self._drain_after_cancel()
        resume_state = self._state
        self._cancel_origin_state = resume_state
        self._cancel_attempted = True
        self._state = OverlappedState.CANCEL_IN_FLIGHT
        try:
            ok = _kernel32.CancelIoEx(
                self._pipe_handle,
                ctypes.byref(self._overlapped),
            )
        except BaseException as interruption:
            self._state = OverlappedState.CANCEL_UNKNOWN
            self._cancel_outcome = CancelOutcome.UNKNOWN
            self._cancel_winerror = None
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.CANCEL,
                owner=self,
            )
        finally:
            _release_injected_hooks(_kernel32.CancelIoEx, "CancelIoEx")
        if ok:
            cancel_error = None
        else:
            try:
                cancel_error = ctypes.get_last_error()
            except BaseException as interruption:
                self._state = OverlappedState.CANCEL_UNKNOWN
                self._cancel_outcome = CancelOutcome.UNKNOWN
                self._cancel_winerror = None
                _raise_interruption(
                    interruption=interruption,
                    reason=FailureReason.NATIVE_FAILURE,
                    stage=LifecycleStage.CANCEL,
                    owner=self,
                )
        snapshot = self._cell.wait_snapshot
        try:
            commit_result = _commit_cancel_snapshot(self._cell, snapshot)
        except BaseException as interruption:
            if self._cell.cancel_snapshot is snapshot:
                if ok:
                    self._cancel_outcome = CancelOutcome.ACCEPTED
                    self._cancel_winerror = None
                    self._result_proven = True
                    self._state = OverlappedState.PENDING
                elif cancel_error == 1168:
                    self._cancel_outcome = CancelOutcome.NOT_FOUND
                    self._cancel_winerror = cancel_error
                    self._state = resume_state
                elif cancel_error == 0:
                    self._cancel_outcome = CancelOutcome.UNKNOWN
                    self._cancel_winerror = None
                    self._state = OverlappedState.CANCEL_UNKNOWN
                else:
                    self._cancel_outcome = CancelOutcome.FAILED
                    self._cancel_winerror = cancel_error
                    self._state = resume_state
            else:
                self._cancel_outcome = CancelOutcome.UNKNOWN
                self._cancel_winerror = None
                self._state = OverlappedState.CANCEL_UNKNOWN
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.CANCEL,
                owner=self,
            )
        if ok:
            self._cancel_outcome = CancelOutcome.ACCEPTED
            self._cancel_winerror = None
            self._state = OverlappedState.PENDING
            self._result_proven = True
        elif cancel_error == 1168:
            self._cancel_outcome = CancelOutcome.NOT_FOUND
            self._cancel_winerror = cancel_error
            self._state = resume_state
        elif cancel_error == 0:
            self._cancel_outcome = CancelOutcome.UNKNOWN
            self._cancel_winerror = None
            self._state = OverlappedState.CANCEL_UNKNOWN
        else:
            self._cancel_outcome = CancelOutcome.FAILED
            self._cancel_winerror = cancel_error
            self._state = resume_state
        # At an already-crossed deadline, an accepted/not-found cancel gives
        # us a direct same-pointer observation opportunity.  A failed cancel
        # still needs one zero-timeout poll before GetOverlappedResult.
        force_followup_wait = (
            self._cancel_outcome is CancelOutcome.NOT_FOUND
            and resume_state is OverlappedState.SUBMIT_UNKNOWN
        )
        if self._drain_wait_already_done and (
            self._cancel_outcome is CancelOutcome.FAILED
            or force_followup_wait
        ):
            self._drain_wait_already_done = False
            force_followup_wait = True
        try:
            post_cancel_now = _monotonic()
            if not force_followup_wait and post_cancel_now >= self._absolute_deadline:
                self._drain_wait_already_done = True
        except BaseException as interruption:
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.CANCEL,
                owner=self,
            )
        return self._drain_after_cancel()

    def transfer(self):
        return self._run_serialized(
            stage=LifecycleStage.TRANSFER,
            operation=self._transfer_unlocked,
        )

    def _transfer_unlocked(self):
        active = self._cell.operation
        if active is not self:
            self._raise_for_owner(
                stage=LifecycleStage.TRANSFER,
                reason=FailureReason.LIFECYCLE_VIOLATION,
                winerror=None,
                owner=active,
            )
        if self._state is OverlappedState.CLOSED and self._completion is None:
            self._raise_failure(
                stage=LifecycleStage.TRANSFER,
                reason=FailureReason.LIFECYCLE_VIOLATION,
                winerror=None,
            )
        destination = type(self)(
            cell=self._cell,
            entry=self._entry,
            registry_token=self._registry_token,
            pipe_handle=self._pipe_handle,
            event_handle=self._event_handle,
            overlapped=self._overlapped,
            frame_buffer=self._frame_buffer,
            frame_length=self._frame_length,
            kind=self._kind,
            absolute_deadline=self._absolute_deadline,
        )
        destination._state = self._state
        destination._completion = self._completion
        destination._closed = self._closed
        destination._cancel_attempted = self._cancel_attempted
        destination._cancel_outcome = self._cancel_outcome
        destination._cancel_winerror = self._cancel_winerror
        destination._drain_wait_already_done = self._drain_wait_already_done
        destination._close_quarantine_winerror = self._close_quarantine_winerror
        destination._terminal_error_reason = self._terminal_error_reason
        destination._terminal_error_stage = self._terminal_error_stage
        destination._terminal_error_winerror = self._terminal_error_winerror
        destination._result_proven = self._result_proven
        destination._pending_result_winerror = self._pending_result_winerror
        destination._cancel_origin_state = self._cancel_origin_state
        destination._completed_native_outcome = self._completed_native_outcome
        destination._completed_bytes_transferred = self._completed_bytes_transferred
        destination._completed_winerror = self._completed_winerror
        destination._result_resume_pending = self._result_resume_pending
        destination._result_origin = self._result_origin
        destination._wait_carrier = self._wait_carrier
        # A transfer moves the in-flight replay protocol as part of the same
        # authority generation.  Preserve the exact immutable transition and
        # payload identities; rebuilding either would break the cell chain.
        destination._replay_mode = self._replay_mode
        destination._replay_payload = self._replay_payload
        destination._replay_context = self._replay_context
        destination._replay_dispatch = self._replay_dispatch
        destination._replay_sampled_now = self._replay_sampled_now
        destination._replay_transition = self._replay_transition
        destination._replay_committed = self._replay_committed
        try:
            result = _commit_authority_transfer(
                self._cell,
                self,
                destination,
            )
        except BaseException as interruption:
            if self._cell.authority_generation is destination:
                self._successor = destination
                self._state = OverlappedState.TRANSFERRED
                self._cell.operation = destination
                owner = destination
            else:
                owner = self
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.OWNERSHIP_TRANSFER_FAILED,
                stage=LifecycleStage.TRANSFER,
                owner=owner,
                completion=destination._completion,
            )
        if result is not _PrimitiveCommitResult.COMMITTED:
            self._raise_for_owner(
                stage=LifecycleStage.TRANSFER,
                reason=FailureReason.OWNERSHIP_TRANSFER_FAILED,
                winerror=None,
                owner=self._cell.operation,
            )
        self._successor = destination
        self._state = OverlappedState.TRANSFERRED
        self._cell.operation = destination
        self._event_handle = 0
        self._overlapped = None
        self._frame_buffer = None
        return destination

    def recover_transferred_owner(self):
        if self._state is OverlappedState.TRANSFERRED and self._successor is not None:
            return self._successor
        self._raise_failure(
            stage=LifecycleStage.TRANSFER,
            reason=FailureReason.LIFECYCLE_VIOLATION,
            winerror=None,
        )

    def _quarantine_close(self):
        transition = _RegistryTransition(
            mode=_RegistryTransitionMode.QUARANTINE,
            entry_token=self._registry_token,
            expected_entry=self._entry,
            expected_cell=self._cell,
            consume_state=_RegistryConsumeMarker(_RegistryConsumeState.NEVER),
        )
        try:
            result = _commit_registry_transition(_REGISTRY, transition)
        except BaseException:
            self._cell.release_if_owned_by_current_thread(_OPERATION_PURPOSE)
            _failfast_quarantine()
            os._exit(_FAILFAST_RETURNED_EXIT_CODE)
        if result is not _RegistryTransitionResult.COMMITTED:
            self._cell.release_if_owned_by_current_thread(_OPERATION_PURPOSE)
            _failfast_quarantine()
            os._exit(_FAILFAST_RETURNED_EXIT_CODE)

    def _remove_closed_registry_entry(self):
        transition = _RegistryTransition(
            mode=_RegistryTransitionMode.CLOSED_REMOVE,
            entry_token=self._registry_token,
            expected_entry=self._entry,
            expected_cell=self._cell,
            consume_state=_RegistryConsumeMarker(_RegistryConsumeState.NEVER),
        )
        try:
            result = _commit_registry_transition(_REGISTRY, transition)
        except BaseException as interruption:
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.NATIVE_FAILURE,
                stage=LifecycleStage.CLOSE,
                owner=self,
                completion=self._completion,
            )
        if result is not _RegistryTransitionResult.COMMITTED:
            self._raise_failure(
                stage=LifecycleStage.CLOSE,
                reason=FailureReason.NATIVE_FAILURE,
                winerror=None,
            )

    def close(self):
        return self._run_serialized(
            stage=LifecycleStage.CLOSE,
            operation=self._close_unlocked,
        )

    def _close_unlocked(self):
        if self._cell.operation is not self:
            self._raise_for_owner(
                stage=LifecycleStage.CLOSE,
                reason=FailureReason.LIFECYCLE_VIOLATION,
                winerror=None,
                owner=self._cell.operation,
            )
        if self._closed:
            return None
        if self._state in (
            OverlappedState.CLOSE_FAILED_QUARANTINED,
            OverlappedState.CLOSE_UNKNOWN,
        ):
            raise OverlappedPipeError(
                reason=FailureReason.CLOSE_QUARANTINED,
                stage=LifecycleStage.CLOSE,
                winerror=self._close_quarantine_winerror,
                completion=self._completion,
                resource_owner=self,
            )
        if self._state not in (OverlappedState.DRAINED, OverlappedState.NEW):
            self._raise_failure(
                stage=LifecycleStage.CLOSE,
                reason=FailureReason.LIFECYCLE_VIOLATION,
                winerror=None,
            )
        try:
            ok = _kernel32.CloseHandle(self._event_handle)
        except BaseException as interruption:
            self._close_quarantine_winerror = None
            self._state = OverlappedState.CLOSE_UNKNOWN
            self._quarantine_close()
            _raise_interruption(
                interruption=interruption,
                reason=FailureReason.CLOSE_QUARANTINED,
                stage=LifecycleStage.CLOSE,
                owner=self,
                completion=self._completion,
            )
        finally:
            _release_injected_hooks(_kernel32.CloseHandle, "CloseHandle")
        if not ok:
            try:
                winerror = ctypes.get_last_error()
            except BaseException as interruption:
                self._close_quarantine_winerror = None
                self._state = OverlappedState.CLOSE_UNKNOWN
                self._quarantine_close()
                _raise_interruption(
                    interruption=interruption,
                    reason=FailureReason.CLOSE_QUARANTINED,
                    stage=LifecycleStage.CLOSE,
                    owner=self,
                    completion=self._completion,
                )
            self._close_quarantine_winerror = winerror if winerror else None
            self._state = OverlappedState.CLOSE_FAILED_QUARANTINED
            self._quarantine_close()
            raise OverlappedPipeError(
                reason=FailureReason.CLOSE_QUARANTINED,
                stage=LifecycleStage.CLOSE,
                winerror=self._close_quarantine_winerror,
                completion=self._completion,
                resource_owner=self,
            )
        self._closed = True
        self._state = OverlappedState.CLOSED
        self._event_handle = 0
        self._overlapped = None
        self._frame_buffer = None
        self._entry.registry_phase = _RegistryPhase.CLOSED
        self._remove_closed_registry_entry()
        return None
