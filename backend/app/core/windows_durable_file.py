from __future__ import annotations

import ctypes
import hashlib
import json
import math
import ntpath
import os
import secrets
import threading
from collections.abc import Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, NoReturn

from app.core import windows_storage_policy as storage_policy
from app.core.windows_storage_policy import ApprovedPhysicalDirectory


if os.name == "nt":  # pragma: no branch - production and tests run on Windows.
    import pywintypes
    import win32api
    import win32con
    import win32file
else:  # Keep the module importable; the approved-parent gate still fails closed.
    pywintypes = None
    win32api = None
    win32con = None
    win32file = None


_FILE_ATTRIBUTE_DIRECTORY = 0x0010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_FILE_READ_ATTRIBUTES = 0x0080
_ERROR_HANDLE_EOF = 38
_ERROR_FILE_EXISTS = 80
_ERROR_ALREADY_EXISTS = 183
_ERROR_MR_MID_NOT_FOUND = 317
_READ_CHUNK_BYTES = 64 * 1024
_TEMP_CREATE_ATTEMPTS = 32
_DWORD_MAX = 0xFFFFFFFF
_ULONG_MAX = 0xFFFFFFFF
_VOLUME_NAME_GUID = 0x00000001
_FILE_RENAME_INFORMATION_CLASS = 10
_STATUS_SUCCESS = 0
_STATUS_PENDING = 0x00000103
_CLOSE_UNKNOWN_REGISTRY_CAPACITY = 32
_FAILFAST_UNADOPTED_CREATE = 71
_FAILFAST_PENDING_RENAME = 72
_FAILFAST_DETACH_IDENTITY = 73
_FAILFAST_CLOSE_QUARANTINE = 74
_NO_NATIVE_RETURN = object()
_NO_HANDLE_RETURN = object()


_NTSTATUS = ctypes.c_long
_ULONG_PTR = (
    ctypes.c_ulonglong
    if ctypes.sizeof(ctypes.c_void_p) == 8
    else ctypes.c_ulong
)


class _IO_STATUS_BLOCK_STATUS(ctypes.Union):
    _fields_ = [("Status", _NTSTATUS), ("Pointer", ctypes.c_void_p)]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _anonymous_ = ("_status",)
    _fields_ = [
        ("_status", _IO_STATUS_BLOCK_STATUS),
        ("Information", _ULONG_PTR),
    ]


class _FILE_RENAME_REPLACE_UNION(ctypes.Union):
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_ubyte),
        ("_alignment", wintypes.ULONG),
    ]


class _FILE_RENAME_INFORMATION_HEAD(ctypes.Structure):
    _anonymous_ = ("_replace",)
    _fields_ = [
        ("_replace", _FILE_RENAME_REPLACE_UNION),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.ULONG),
        ("FileName", wintypes.WCHAR * 1),
    ]


if os.name == "nt":
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=False)
    _NtSetInformationFile = _ntdll.NtSetInformationFile
    _NtSetInformationFile.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IO_STATUS_BLOCK),
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.c_int,
    ]
    _NtSetInformationFile.restype = _NTSTATUS
    _RtlNtStatusToDosError = _ntdll.RtlNtStatusToDosError
    _RtlNtStatusToDosError.argtypes = [_NTSTATUS]
    _RtlNtStatusToDosError.restype = wintypes.ULONG
else:
    _ntdll = None
    _NtSetInformationFile = None
    _RtlNtStatusToDosError = None


class DurableWriteStage(str, Enum):
    VALIDATE_PARENT = "validate_parent"
    VOLUME_GATE = "volume_gate"
    CREATE_TEMP = "create_temp"
    SET_NONINHERIT = "set_noninherit"
    WRITE_TEMP = "write_temp"
    VERIFY_PARENT_BEFORE_FLUSH = "verify_parent_before_flush"
    FLUSH_PRE_RENAME = "flush_pre_rename"
    SNAPSHOT_PRE_RENAME = "snapshot_pre_rename"
    VERIFY_PARENT_BEFORE_RENAME = "verify_parent_before_rename"
    NATIVE_RENAME = "native_rename"
    VERIFY_PARENT_AFTER_RENAME = "verify_parent_after_rename"
    FLUSH_POST_RENAME = "flush_post_rename"
    REWIND = "rewind"
    VERIFY_TARGET = "verify_target"
    CLOSE = "close"


class DurablePathState(str, Enum):
    CREATE_UNKNOWN = "create_unknown"
    TEMP_CONFIRMED = "temp_confirmed"
    RENAME_IN_FLIGHT_OR_UNKNOWN = "rename_in_flight_or_unknown"
    TARGET_CONFIRMED = "target_confirmed"


class DurableHandleState(str, Enum):
    CREATE_IN_FLIGHT = "create_in_flight"
    OPEN = "open"
    CLOSE_IN_FLIGHT = "close_in_flight"
    CLOSED = "closed"
    CLOSE_UNKNOWN = "close_unknown"


class _CreatePhase(str, Enum):
    NO_HANDLE = "no_handle"
    CALL_IN_FLIGHT = "call_in_flight"
    KNOWN_NO_HANDLE = "known_no_handle"
    RETURNED_UNCOMMITTED = "returned_uncommitted"
    ADOPTED = "adopted"


class _ClosePhase(str, Enum):
    NOT_STARTED = "not_started"
    DETACH_IN_FLIGHT = "detach_in_flight"
    DETACHED_CONFIRMED = "detached_confirmed"
    CLOSE_CALL_IN_FLIGHT = "close_call_in_flight"
    CLOSE_RETURNED_NONE = "close_returned_none"
    CLOSED = "closed"
    CLOSE_UNKNOWN = "close_unknown"


class _NativePendingDecision(str, Enum):
    UNKNOWN = "unknown"
    PENDING = "pending"
    NOT_PENDING = "not_pending"


@dataclass(frozen=True, slots=True)
class DurableFileIdentity:
    volume_serial: int
    file_index: int


@dataclass(frozen=True, slots=True)
class _HandleSnapshot:
    identity: DurableFileIdentity
    attributes: int
    link_count: int
    byte_count: int


@dataclass(frozen=True, slots=True)
class DurableTempOwner:
    """Compatibility view of retained sibling-temp evidence."""

    path: Path
    sha256: str
    byte_count: int
    identity: DurableFileIdentity | None


@dataclass(frozen=True, slots=True)
class DurableEvidenceOwner:
    temp_path: Path
    target_path: Path
    sha256: str
    byte_count: int
    identity: DurableFileIdentity | None
    path_state: DurablePathState
    handle_state: DurableHandleState


@dataclass(frozen=True, slots=True)
class CloseUnknownToken:
    value: str
    path_state: DurablePathState
    handle_state: DurableHandleState


@dataclass(frozen=True, slots=True)
class DurableWriteReceipt:
    path: Path
    sha256: str
    byte_count: int


@dataclass(slots=True)
class _DurableOwner:
    temp_path: Path
    target_path: Path
    sha256: str
    byte_count: int
    path_state: DurablePathState = DurablePathState.CREATE_UNKNOWN
    handle_state: DurableHandleState = DurableHandleState.CREATE_IN_FLIGHT
    handle: Any | None = None
    raw_handle: int | None = None
    identity: DurableFileIdentity | None = None
    pre_snapshot: _HandleSnapshot | None = None
    native_storage: Any | None = None
    native_iosb: _IO_STATUS_BLOCK | None = None
    native_name: bytes | None = None
    failfast_started: bool = False
    create_phase: _CreatePhase = _CreatePhase.NO_HANDLE
    committed_close_token: CloseUnknownToken | None = None
    committed_close_cause: BaseException | None = None
    committed_close_error: DurableWriteError | None = None
    native_pending_decision: _NativePendingDecision = (
        _NativePendingDecision.UNKNOWN
    )
    native_return_observed: bool = False
    committed_native_status: int | None = None
    committed_native_status_error: NativeRenameStatusError | None = None
    committed_native_mapping_cause: BaseException | None = None
    native_status_semantics_committed: bool = False
    committed_native_reconcile_cause: BaseException | None = None
    unadopted_failfast_hook_attempted: bool = False


@dataclass(slots=True)
class DurableHandleAdoptionGuard:
    owner: _DurableOwner
    temporary_handle: Any | None = None
    committed: bool = False

    def hold_returned(self, handle: Any) -> None:
        self.temporary_handle = handle

    def commit(self) -> _DurableOwner:
        handle = self.temporary_handle
        if handle is None:
            raise RuntimeError("CreateFile returned no adoptable handle")
        raw_handle = int(handle)
        if raw_handle == 0:
            raise RuntimeError("CreateFile returned a null handle")
        self.owner.handle = handle
        self.owner.raw_handle = raw_handle
        self.owner.path_state = DurablePathState.TEMP_CONFIRMED
        self.owner.handle_state = DurableHandleState.OPEN
        self.temporary_handle = None
        self.owner.create_phase = _CreatePhase.ADOPTED
        self.committed = True
        return self.owner


@dataclass(slots=True)
class _CloseUnknownEntry:
    token: CloseUnknownToken
    owner: _DurableOwner
    raw_handle: int
    cause: BaseException


class NativeRenameStatusError(OSError):
    def __init__(
        self,
        *,
        signed_status: int,
        mapped_winerror: int,
        mapping_cause: BaseException | None = None,
    ) -> None:
        self.signed_status = signed_status
        self.u32_status = signed_status & _ULONG_MAX
        self.mapped_winerror = mapped_winerror
        self.mapping_cause = mapping_cause
        self.winerror = mapped_winerror
        super().__init__(
            mapped_winerror,
            "native durable rename failed "
            f"(NTSTATUS=0x{self.u32_status:08X}, WinError={mapped_winerror})",
        )


class _NativeRenameInterrupted(RuntimeError):
    def __init__(
        self,
        cause: BaseException,
        reconcile_cause: BaseException | None = None,
    ) -> None:
        super().__init__("native rename return or reconciliation was interrupted")
        self.cause = cause
        self.reconcile_cause = reconcile_cause


class DurableWriteError(RuntimeError):
    """Typed failure carrying state and non-secret recovery evidence."""

    def __init__(
        self,
        *,
        stage: DurableWriteStage,
        target: Path | None,
        evidence_owner: DurableEvidenceOwner | CloseUnknownToken | None,
        cause: BaseException,
        path_state: DurablePathState | None,
        handle_state: DurableHandleState | None,
    ) -> None:
        super().__init__(
            f"durable write failed at {stage.value} ({type(cause).__name__})"
        )
        self.stage = stage
        self.target = target
        self.evidence_owner = evidence_owner
        self.cause = cause
        self.path_state = path_state
        self.handle_state = handle_state

    @property
    def target_path(self) -> Path | None:
        return self.target

    @property
    def temp_owner(self) -> DurableTempOwner | None:
        evidence = self.evidence_owner
        if not isinstance(evidence, DurableEvidenceOwner):
            return None
        if evidence.path_state is DurablePathState.TARGET_CONFIRMED:
            return None
        return DurableTempOwner(
            path=evidence.temp_path,
            sha256=evidence.sha256,
            byte_count=evidence.byte_count,
            identity=evidence.identity,
        )

    @property
    def temp_path(self) -> Path | None:
        owner = self.temp_owner
        return None if owner is None else owner.path


@dataclass(slots=True)
class _DurableWriteLifecycle:
    stage: DurableWriteStage = DurableWriteStage.CREATE_TEMP
    current_owner: _DurableOwner | None = None
    close_error: DurableWriteError | None = None
    close_boundary_cause: BaseException | None = None
    committed_primary: BaseException | None = None
    committed_operation_error: DurableWriteError | None = None
    committed_handoff_error: BaseException | None = None
    typed_handoff_hook_attempted: bool = False

    def __enter__(self) -> _DurableWriteLifecycle:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        primary: BaseException | None,
        _traceback: object,
    ) -> bool:
        try:
            try:
                try:
                    self._capture_exit_entry(primary)
                finally:
                    _before_lifecycle_finalize_call(self)
                    self._finalize_current_owner(primary)
            finally:
                self._last_resort_terminal_owner()
                _after_lifecycle_final_last_resort_before_handoff(self)
        except BaseException as terminalization_cause:
            self.close_boundary_cause = _combine_boundary_causes(
                self.close_boundary_cause, terminalization_cause
            )

        try:
            owner = self.current_owner
            if owner is not None:
                _failfast_if_create_ambiguous(owner)
            if owner is None or owner.create_phase is not _CreatePhase.ADOPTED:
                if owner is not None and owner.failfast_started:
                    assert self.close_boundary_cause is not None
                    raise self.close_boundary_cause
                if self.close_boundary_cause is not None:
                    raise self.close_boundary_cause
                return False
            try:
                prepared_handoff_error = _prepare_lifecycle_result(
                    lifecycle=self,
                    stage=self.stage,
                    owner=owner,
                    primary=primary,
                    close_error=self.close_error,
                    close_boundary_cause=self.close_boundary_cause,
                )
                _after_lifecycle_first_prepare_before_commit(
                    self, prepared_handoff_error
                )
                self.committed_handoff_error = prepared_handoff_error
            except BaseException as preparation_cause:
                self.close_boundary_cause = _combine_boundary_causes(
                    self.close_boundary_cause, preparation_cause
                )
                recovered_handoff_error = _prepare_lifecycle_result(
                    lifecycle=self,
                    stage=self.stage,
                    owner=owner,
                    primary=primary,
                    close_error=self.close_error,
                    close_boundary_cause=self.close_boundary_cause,
                )
                _after_lifecycle_second_prepare_before_commit(
                    self, recovered_handoff_error
                )
                self.committed_handoff_error = recovered_handoff_error
            _after_lifecycle_committed_prepare_before_read(self)
            handoff_error = self.committed_handoff_error
            if handoff_error is None:
                return False
            _before_lifecycle_committed_raise(self, handoff_error)
            if handoff_error is self.committed_operation_error:
                raise handoff_error from primary
            if handoff_error is self.close_error and self.close_error is not None:
                raise handoff_error from self.close_error.cause
            raise handoff_error
        except BaseException as handoff_cause:
            recovery_error = _recover_committed_lifecycle_handoff(
                self, handoff_cause
            )
            if recovery_error is handoff_cause:
                raise
            raise recovery_error from handoff_cause

    def _capture_exit_entry(self, primary: BaseException | None) -> None:
        try:
            self.committed_primary = primary
            owner = self.current_owner
            if owner is None:
                return
            _after_lifecycle_exit_entry(owner, primary)
            if owner.create_phase in {
                _CreatePhase.CALL_IN_FLIGHT,
                _CreatePhase.RETURNED_UNCOMMITTED,
            }:
                _run_unadopted_create_failfast_hook(owner)
                _begin_failfast(
                    owner,
                    _failfast_unadopted_create,
                    _FAILFAST_UNADOPTED_CREATE,
                )
            if owner.create_phase is not _CreatePhase.ADOPTED:
                return
            if primary is not None and owner.failfast_started:
                return
        except BaseException as exc:
            self.close_boundary_cause = _combine_boundary_causes(
                self.close_boundary_cause, exc
            )

    def _finalize_current_owner(self, primary: BaseException | None) -> None:
        try:
            owner = self.current_owner
            if owner is not None:
                _failfast_if_create_ambiguous(owner)
            if (
                owner is None
                or owner.create_phase is not _CreatePhase.ADOPTED
                or owner.failfast_started
            ):
                return
            _failfast_if_native_pending_ambiguous(owner)
            try:
                _before_lifecycle_owner_close(owner, primary)
            except BaseException as exc:
                self.close_boundary_cause = _combine_boundary_causes(
                    self.close_boundary_cause, exc
                )
            try:
                self.close_error = _close_owner(owner)
            except BaseException as exc:
                self.close_boundary_cause = _combine_boundary_causes(
                    self.close_boundary_cause, exc
                )
        finally:
            try:
                _before_lifecycle_force_call(self)
                self._force_terminal_owner()
            finally:
                self._last_resort_terminal_owner()

    def _force_terminal_owner(self) -> None:
        try:
            owner = self.current_owner
            if owner is not None:
                _failfast_if_create_ambiguous(owner)
            if (
                owner is not None
                and owner.create_phase is _CreatePhase.ADOPTED
                and not owner.failfast_started
                and owner.handle_state is DurableHandleState.OPEN
            ):
                _failfast_if_native_pending_ambiguous(owner)
                try:
                    self.close_error = _close_owner(owner)
                except BaseException as exc:
                    self.close_boundary_cause = _combine_boundary_causes(
                        self.close_boundary_cause, exc
                    )
        finally:
            try:
                _before_lifecycle_terminal_owner_read(self)
                self._enforce_terminal_owner()
            finally:
                self._last_resort_terminal_owner()

    def _enforce_terminal_owner(self) -> None:
        owner = self.current_owner
        if owner is not None:
            _failfast_if_create_ambiguous(owner)
        if (
            owner is None
            or owner.create_phase is not _CreatePhase.ADOPTED
            or owner.failfast_started
        ):
            return
        _failfast_if_native_pending_ambiguous(owner)
        if owner.handle_state is DurableHandleState.CLOSE_UNKNOWN:
            if self.close_error is None:
                self.close_error = owner.committed_close_error
            if self.close_error is None:
                _begin_failfast(
                    owner,
                    _failfast_close_quarantine,
                    _FAILFAST_CLOSE_QUARANTINE,
                )
        elif owner.handle_state is not DurableHandleState.CLOSED:
            callback = (
                _failfast_detach_identity
                if owner.handle is not None
                else _failfast_close_quarantine
            )
            exit_code = (
                _FAILFAST_DETACH_IDENTITY
                if owner.handle is not None
                else _FAILFAST_CLOSE_QUARANTINE
            )
            _begin_failfast(owner, callback, exit_code)

    def _last_resort_terminal_owner(self) -> None:
        owner = self.current_owner
        if owner is not None:
            _failfast_if_create_ambiguous(owner)
        if (
            owner is None
            or owner.create_phase is not _CreatePhase.ADOPTED
            or owner.failfast_started
        ):
            return
        _failfast_if_native_pending_ambiguous(owner)
        if owner.handle_state is DurableHandleState.OPEN:
            try:
                self.close_error = _close_owner(owner)
            except BaseException as exc:
                self.close_boundary_cause = _combine_boundary_causes(
                    self.close_boundary_cause, exc
                )
        self._enforce_terminal_owner()


def _nonexception_leaves(root: BaseException) -> list[BaseException]:
    pending = [root]
    seen: set[int] = set()
    leaves: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        successors: list[BaseException] = []
        if isinstance(current, BaseExceptionGroup):
            successors.extend(current.exceptions)
        elif not isinstance(current, Exception):
            leaves.append(current)
        for name in (
            "cause",
            "mapping_cause",
            "reconcile_cause",
            "__cause__",
            "__context__",
        ):
            nested = getattr(current, name, None)
            if isinstance(nested, BaseException):
                successors.append(nested)
        pending.extend(reversed(successors))
    return leaves


def _recover_committed_lifecycle_handoff(
    lifecycle: _DurableWriteLifecycle, incoming: BaseException
) -> BaseException:
    committed_handoff = lifecycle.committed_handoff_error
    owner = lifecycle.current_owner

    def contains_identity(root: BaseException, target: BaseException) -> bool:
        pending = [root]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if current is target:
                return True
            if id(current) in seen:
                continue
            seen.add(id(current))
            if isinstance(current, BaseExceptionGroup):
                pending.extend(current.exceptions)
            for name in (
                "cause",
                "mapping_cause",
                "reconcile_cause",
                "__cause__",
                "__context__",
            ):
                nested = getattr(current, name, None)
                if isinstance(nested, BaseException):
                    pending.append(nested)
        return False

    required_evidence: list[BaseException | None] = [
        lifecycle.committed_operation_error,
        lifecycle.close_error,
        lifecycle.close_boundary_cause,
        lifecycle.committed_primary,
    ]
    if owner is not None:
        required_evidence.extend(
            [
                owner.committed_close_error,
                owner.committed_close_cause,
                owner.committed_native_status_error,
                owner.committed_native_mapping_cause,
                owner.committed_native_reconcile_cause,
            ]
        )
    if incoming is committed_handoff and isinstance(incoming, Exception):
        if all(
            evidence is None or contains_identity(incoming, evidence)
            for evidence in required_evidence
        ) and not any(
            _nonexception_leaves(evidence)
            for evidence in required_evidence
            if evidence is not None
        ):
            return incoming

    direct_members: list[BaseException] = []

    def add(error: BaseException | None) -> None:
        if error is None:
            return
        candidates = (
            error.exceptions if isinstance(error, BaseExceptionGroup) else (error,)
        )
        for candidate in candidates:
            if all(candidate is not member for member in direct_members):
                direct_members.append(candidate)

    recovery_cause: BaseException | None = None
    primary = lifecycle.committed_primary
    if (
        lifecycle.committed_operation_error is None
        and primary is not None
        and owner is not None
    ):
        try:
            lifecycle.committed_operation_error = _error_from_owner(
                stage=lifecycle.stage,
                owner=owner,
                cause=primary,
            )
        except BaseException as exc:
            recovery_cause = exc
    if (
        lifecycle.close_error is None
        and owner is not None
        and owner.committed_close_error is not None
    ):
        lifecycle.close_error = owner.committed_close_error
    if (
        owner is not None
        and owner.committed_native_status_error is None
        and owner.committed_native_status is not None
        and (owner.committed_native_status & _ULONG_MAX)
        not in {_STATUS_SUCCESS, _STATUS_PENDING}
        and owner.native_pending_decision is _NativePendingDecision.NOT_PENDING
    ):
        try:
            recovered_status_error = _native_status_error(
                owner.committed_native_status
            )
            owner.committed_native_status_error = recovered_status_error
            owner.committed_native_mapping_cause = (
                recovered_status_error.mapping_cause
            )
        except BaseException as exc:
            recovery_cause = (
                exc
                if recovery_cause is None
                else BaseExceptionGroup(
                    "committed lifecycle evidence recovery failed",
                    [recovery_cause, exc],
                )
            )

    native_evidence: list[BaseException] = []
    if owner is not None:
        native_evidence = [
            evidence
            for evidence in (
                owner.committed_native_status_error,
                owner.committed_native_mapping_cause,
                owner.committed_native_reconcile_cause,
            )
            if evidence is not None
        ]
    missing_native_evidence = (
        [
            evidence
            for evidence in native_evidence
            if not contains_identity(incoming, evidence)
        ]
        if incoming is committed_handoff
        else list(native_evidence)
    )
    native_nonexception_leaves = [
        leaf
        for evidence in native_evidence
        for leaf in _nonexception_leaves(evidence)
    ]
    promote_native_evidence = bool(
        missing_native_evidence or native_nonexception_leaves
    )

    add(committed_handoff)
    add(lifecycle.committed_operation_error)
    add(lifecycle.close_error)
    if owner is not None:
        add(owner.committed_close_error)
        if promote_native_evidence:
            add(owner.committed_native_status_error)
        for evidence in (
            owner.committed_native_mapping_cause,
            owner.committed_native_reconcile_cause,
        ):
            if evidence is not None and (
                any(evidence is missing for missing in missing_native_evidence)
                or _nonexception_leaves(evidence)
            ):
                add(evidence)
    if primary is not None and (
        not isinstance(primary, Exception)
        or (
            incoming is committed_handoff
            and bool(missing_native_evidence)
        )
    ):
        add(primary)
    add(lifecycle.close_boundary_cause)
    add(recovery_cause)
    add(incoming)
    refreshed_evidence = [
        *required_evidence,
        *native_evidence,
        lifecycle.committed_operation_error,
        lifecycle.close_error,
        incoming,
    ]
    for evidence in refreshed_evidence:
        if evidence is None:
            continue
        for leaf in _nonexception_leaves(evidence):
            add(leaf)
    if len(direct_members) == 1:
        return direct_members[0]
    if isinstance(incoming, BaseExceptionGroup) and len(
        direct_members
    ) == len(incoming.exceptions) and all(
        member is incoming_member
        for member, incoming_member in zip(
            direct_members, incoming.exceptions, strict=True
        )
    ):
        return incoming
    return BaseExceptionGroup(
        "committed durable lifecycle handoff was interrupted",
        direct_members,
    )


def _combine_boundary_causes(
    existing: BaseException | None, incoming: BaseException
) -> BaseException:
    if existing is None:
        return incoming
    return BaseExceptionGroup(
        "durable close boundary was interrupted more than once",
        [existing, incoming],
    )


def _prepare_lifecycle_result(
    *,
    lifecycle: _DurableWriteLifecycle,
    stage: DurableWriteStage,
    owner: _DurableOwner,
    primary: BaseException | None,
    close_error: DurableWriteError | None,
    close_boundary_cause: BaseException | None,
) -> BaseException | None:
    if primary is None and close_error is None and close_boundary_cause is None:
        return None

    operation_error = lifecycle.committed_operation_error
    if primary is not None and operation_error is None:
        operation_error = _error_from_owner(
            stage=stage, owner=owner, cause=primary
        )
    lifecycle.committed_primary = primary
    lifecycle.committed_operation_error = operation_error
    if not lifecycle.typed_handoff_hook_attempted:
        lifecycle.typed_handoff_hook_attempted = True
        try:
            _before_lifecycle_typed_handoff(
                owner, primary, operation_error, close_error
            )
        except BaseException as handoff_cause:
            close_boundary_cause = _combine_boundary_causes(
                close_boundary_cause, handoff_cause
            )
            lifecycle.close_boundary_cause = close_boundary_cause
    if close_boundary_cause is None:
        if (
            primary is not None
            and not isinstance(primary, Exception)
            and close_error is None
        ):
            if isinstance(primary, BaseExceptionGroup):
                members = [operation_error, *primary.exceptions]
                handoff_error = BaseExceptionGroup(
                    "durable typed native handoff failed", members
                )
                return handoff_error
            return primary
        if operation_error is not None and close_error is None:
            return operation_error
        if operation_error is None and close_error is not None:
            return close_error

    members: list[BaseException] = []
    if operation_error is not None:
        members.append(operation_error)
        if primary is not None and not isinstance(primary, Exception):
            members.append(primary)
    if close_error is not None:
        members.append(close_error)
        if not isinstance(close_error.cause, Exception):
            members.append(close_error.cause)
    if close_boundary_cause is not None and all(
        close_boundary_cause is not member for member in members
    ):
        members.append(close_boundary_cause)

    if len(members) == 1:
        return members[0]
    return BaseExceptionGroup("durable lifecycle handoff failed", members)


_CLOSE_UNKNOWN_REGISTRY_LOCK = threading.Lock()
_CLOSE_UNKNOWN_REGISTRY: dict[str, _CloseUnknownEntry] = {}


def _after_create_return_before_attach(_handle: Any) -> None:
    """Semantic fault-injection point; production is deliberately a no-op."""


def _after_owner_adopted_before_operations(_owner: _DurableOwner) -> None:
    """Semantic fault-injection point after adoption and before operations."""


def _before_unadopted_create_failfast_call(_owner: _DurableOwner) -> None:
    """Semantic fault-injection point before ambiguous-create failfast."""


def _after_lifecycle_exit_entry(
    _owner: _DurableOwner, _primary: BaseException | None
) -> None:
    """Semantic fault-injection point at durable lifecycle exit entry."""


def _after_lifecycle_final_last_resort_before_handoff(
    _lifecycle: _DurableWriteLifecycle,
) -> None:
    """Semantic fault-injection point after final terminal ownership recovery."""


def _before_lifecycle_owner_close(
    _owner: _DurableOwner, _primary: BaseException | None
) -> None:
    """Semantic fault-injection point before lifecycle-owned exact close."""


def _before_lifecycle_finalize_call(_lifecycle: _DurableWriteLifecycle) -> None:
    """Semantic fault-injection point before lifecycle finalization."""


def _before_lifecycle_force_call(_lifecycle: _DurableWriteLifecycle) -> None:
    """Semantic fault-injection point before the nested terminal force call."""


def _before_lifecycle_terminal_owner_read(
    _lifecycle: _DurableWriteLifecycle,
) -> None:
    """Semantic fault-injection point before terminal owner state is read."""


def _before_lifecycle_typed_handoff(
    _owner: _DurableOwner,
    _primary: BaseException | None,
    _operation_error: DurableWriteError | None,
    _close_error: DurableWriteError | None,
) -> None:
    """Semantic fault-injection point after typed lifecycle evidence is prepared."""


def _before_lifecycle_committed_raise(
    _lifecycle: _DurableWriteLifecycle, _error: BaseException
) -> None:
    """Semantic fault-injection point immediately before the committed raise."""


def _after_lifecycle_committed_prepare_before_read(
    _lifecycle: _DurableWriteLifecycle,
) -> None:
    """Semantic fault-injection point after prepare and before committed read."""


def _after_lifecycle_first_prepare_before_commit(
    _lifecycle: _DurableWriteLifecycle, _error: BaseException | None
) -> None:
    """Semantic fault-injection point after first prepare and before commit."""


def _after_lifecycle_second_prepare_before_commit(
    _lifecycle: _DurableWriteLifecycle, _error: BaseException | None
) -> None:
    """Semantic fault-injection point after retry prepare and before commit."""


def _after_native_return_before_status_commit(
    _owner: _DurableOwner, _status: int
) -> None:
    """Semantic fault-injection point after native return registration."""


def _before_native_pending_decision(
    _owner: _DurableOwner, _returned_status: object, _iosb: _IO_STATUS_BLOCK
) -> None:
    """Semantic fault-injection point before the native pending decision."""


def _before_native_pending_settle_call(
    _owner: _DurableOwner, _returned_status: object
) -> None:
    """Semantic fault-injection point before pending settlement is entered."""


def _before_native_status_handoff(
    _owner: _DurableOwner, _status_error: NativeRenameStatusError
) -> None:
    """Semantic fault-injection point before a prepared status is handed off."""


def _before_native_status_throw(
    _owner: _DurableOwner, _status_error: NativeRenameStatusError
) -> None:
    """Semantic fault-injection point before the first native-status throw."""


def _before_native_status_final_raise(
    _owner: _DurableOwner,
    _status_error: NativeRenameStatusError,
    _reconcile_cause: BaseException | None,
) -> None:
    """Semantic fault-injection point before the committed status leaves native code."""


def _after_native_pending_settled_before_status_handoff(
    _owner: _DurableOwner,
    _status_error: NativeRenameStatusError | None,
    _captured_cause: BaseException | None,
) -> None:
    """Semantic fault-injection point after pending settles and before status handoff."""


def _after_close_return_before_state_commit(
    _owner: _DurableOwner, _raw_handle: int
) -> None:
    """Semantic fault-injection point after CloseHandle returned None."""


def _after_close_terminal_commit_before_return(
    _owner: _DurableOwner, _raw_handle: int
) -> None:
    """Semantic fault-injection point after CLOSED is terminally committed."""


def _after_detach_identity_before_close(
    _owner: _DurableOwner, _raw_handle: int
) -> None:
    """Semantic fault-injection point after Detach identity is confirmed."""


def _before_close_detach_identity(_owner: _DurableOwner) -> None:
    """Semantic fault-injection point after close ownership starts, before Detach."""


def _after_close_exception_before_quarantine(
    _owner: _DurableOwner, _raw_handle: int, _cause: BaseException
) -> None:
    """Semantic fault-injection point before close-unknown quarantine."""


def _before_close_quarantine_lock(_owner: _DurableOwner) -> None:
    """Semantic fault-injection point before quarantine lock acquisition."""


def _after_close_quarantine_lock(_owner: _DurableOwner) -> None:
    """Semantic fault-injection point while the quarantine lock is held."""


def _after_close_quarantine_insert(_owner: _DurableOwner) -> None:
    """Semantic fault-injection point after strong-reference insertion."""


def _before_close_quarantine_state_commit(_owner: _DurableOwner) -> None:
    """Semantic fault-injection point before CLOSE_UNKNOWN state commit."""


def _before_close_cause_capture(
    _owner: _DurableOwner, _raw_handle: int | None, _cause: BaseException
) -> None:
    """Semantic fault-injection point before a close result is captured."""


def _before_close_quarantine_handoff(
    _owner: _DurableOwner, _raw_handle: int, _cause: BaseException
) -> None:
    """Semantic fault-injection point before close-unknown quarantine handoff."""


def _after_close_unknown_commit_before_typed_handoff(
    _owner: _DurableOwner,
    _token: CloseUnknownToken,
    _close_error: DurableWriteError,
) -> None:
    """Semantic fault-injection point after close-unknown evidence commits."""


def _failfast_unadopted_create(_owner: _DurableOwner) -> NoReturn:
    os._exit(_FAILFAST_UNADOPTED_CREATE)


def _failfast_pending_native_rename(_owner: _DurableOwner) -> NoReturn:
    os._exit(_FAILFAST_PENDING_RENAME)


def _failfast_detach_identity(_owner: _DurableOwner) -> NoReturn:
    os._exit(_FAILFAST_DETACH_IDENTITY)


def _failfast_close_quarantine(_owner: _DurableOwner) -> NoReturn:
    os._exit(_FAILFAST_CLOSE_QUARANTINE)


def _before_failfast_flag(_owner: _DurableOwner, _exit_code: int) -> None:
    """Semantic fault-injection point before failfast ownership is marked."""


def _before_failfast_callback(
    _owner: _DurableOwner, _callback: Any, _exit_code: int
) -> None:
    """Semantic fault-injection point before a failfast callback is entered."""


def write_durable_bytes(
    *,
    parent: ApprovedPhysicalDirectory,
    filename: str,
    payload: bytes,
    replace: bool,
    max_bytes: int,
) -> DurableWriteReceipt:
    _validate_bytes_arguments(
        filename=filename,
        payload=payload,
        replace=replace,
        max_bytes=max_bytes,
    )
    _verify_parent_without_owner(parent)
    target = Path(parent.stable_volume_path) / filename
    maximum_component_length = _query_volume_gate(parent=parent, target=target)
    name_utf16le = _validate_native_basename(
        filename, maximum_component_length=maximum_component_length
    )
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    lifecycle = _DurableWriteLifecycle()
    with lifecycle:
        owner = _create_and_adopt(
            lifecycle=lifecycle,
            parent=parent,
            target=target,
            sha256=expected_sha256,
            byte_count=len(payload),
        )
        _after_owner_adopted_before_operations(owner)

        stage = DurableWriteStage.SET_NONINHERIT
        lifecycle.stage = stage
        _set_and_verify_noninherit(owner)

        stage = DurableWriteStage.WRITE_TEMP
        lifecycle.stage = stage
        _write_exact(owner, payload)

        stage = DurableWriteStage.VERIFY_PARENT_BEFORE_FLUSH
        lifecycle.stage = stage
        parent.verify()

        stage = DurableWriteStage.FLUSH_PRE_RENAME
        lifecycle.stage = stage
        win32file.FlushFileBuffers(owner.handle)

        stage = DurableWriteStage.SNAPSHOT_PRE_RENAME
        lifecycle.stage = stage
        snapshot = _snapshot_handle(owner.handle)
        _verify_pre_rename_snapshot(
            snapshot=snapshot,
            parent=parent,
            expected_byte_count=len(payload),
        )
        owner.pre_snapshot = snapshot
        owner.identity = snapshot.identity

        stage = DurableWriteStage.VERIFY_PARENT_BEFORE_RENAME
        lifecycle.stage = stage
        parent.verify()

        stage = DurableWriteStage.NATIVE_RENAME
        lifecycle.stage = stage
        _native_sibling_rename(
            owner=owner,
            basename=filename,
            name_utf16le=name_utf16le,
            replace=replace,
        )

        stage = DurableWriteStage.VERIFY_PARENT_AFTER_RENAME
        lifecycle.stage = stage
        parent.verify()

        stage = DurableWriteStage.FLUSH_POST_RENAME
        lifecycle.stage = stage
        win32file.FlushFileBuffers(owner.handle)

        stage = DurableWriteStage.REWIND
        lifecycle.stage = stage
        position = win32file.SetFilePointer(
            owner.handle, 0, win32file.FILE_BEGIN
        )
        if position != 0:
            raise OSError(f"durable source rewind returned {position!r}")

        stage = DurableWriteStage.VERIFY_TARGET
        lifecycle.stage = stage
        _verify_renamed_handle(
            owner=owner,
            parent=parent,
            expected_sha256=expected_sha256,
            expected_byte_count=len(payload),
            max_bytes=max_bytes,
        )
    return DurableWriteReceipt(
        path=target,
        sha256=expected_sha256,
        byte_count=len(payload),
    )


def write_durable_json(
    *,
    parent: ApprovedPhysicalDirectory,
    filename: str,
    payload: Mapping[str, object],
    replace: bool,
    max_bytes: int,
) -> DurableWriteReceipt:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    normalized = _normalize_json_value(payload, active=set())
    encoded = json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8", "strict")
    return write_durable_bytes(
        parent=parent,
        filename=filename,
        payload=encoded,
        replace=replace,
        max_bytes=max_bytes,
    )


def _normalize_json_value(
    value: object,
    *,
    active: set[int],
) -> object:
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return value
    if type(value) is str:
        try:
            value.encode("utf-8", "strict")
        except UnicodeEncodeError:
            raise ValueError("JSON string contains invalid Unicode") from None
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic JSON mapping")
        active.add(identity)
        try:
            result: dict[str, object] = {}
            for key, child in value.items():
                if type(key) is not str:
                    raise TypeError("JSON mapping keys must be strings")
                try:
                    key.encode("utf-8", "strict")
                except UnicodeEncodeError:
                    raise ValueError("JSON key contains invalid Unicode") from None
                result[key] = _normalize_json_value(
                    child,
                    active=active,
                )
            return result
        finally:
            active.remove(identity)
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic JSON sequence")
        active.add(identity)
        try:
            return [
                _normalize_json_value(child, active=active) for child in value
            ]
        finally:
            active.remove(identity)
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def _validate_bytes_arguments(
    *, filename: str, payload: bytes, replace: bool, max_bytes: int
) -> None:
    if type(filename) is not str:
        raise TypeError("filename must be a string")
    try:
        storage_policy._validate_child_name(filename)
    except Exception as exc:
        raise ValueError("filename must be one ordinary Windows file name") from exc
    try:
        filename.encode("utf-16-le", "strict")
    except UnicodeEncodeError as exc:
        raise ValueError("filename contains an unpaired surrogate") from exc
    if type(payload) is not bytes:
        raise TypeError("payload must be exact bytes")
    if type(replace) is not bool:
        raise TypeError("replace must be a bool")
    if type(max_bytes) is not int:
        raise TypeError("max_bytes must be an integer")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if len(payload) > min(max_bytes, _DWORD_MAX):
        raise ValueError("payload exceeds durable byte limit")


def _raise_typed_boundary_error(
    typed_error: DurableWriteError, cause: BaseException
) -> NoReturn:
    nonexception_leaves = _nonexception_leaves(cause)
    if not nonexception_leaves and isinstance(cause, Exception):
        raise typed_error from cause
    raise BaseExceptionGroup(
        "typed boundary error and non-Exception interruption",
        [typed_error, *nonexception_leaves],
    ) from cause


def _verify_parent_without_owner(parent: ApprovedPhysicalDirectory) -> None:
    if not isinstance(parent, ApprovedPhysicalDirectory):
        raise TypeError("parent must be an ApprovedPhysicalDirectory")
    try:
        parent.verify()
        drive, tail = ntpath.splitdrive(str(parent.path))
        if drive.casefold() != "d:" or not tail.startswith(("\\", "/")):
            raise ValueError("approved parent is not an absolute D path")
        stable = parent.stable_volume_path.replace("/", "\\").casefold()
        volume_guid = parent.identity.volume_guid.replace("/", "\\").casefold()
        if not stable.startswith(volume_guid):
            raise ValueError("approved parent stable path left its pinned volume")
    except BaseException as exc:
        typed_error = DurableWriteError(
            stage=DurableWriteStage.VALIDATE_PARENT,
            target=None,
            evidence_owner=None,
            cause=exc,
            path_state=None,
            handle_state=None,
        )
        _raise_typed_boundary_error(typed_error, exc)


def _query_volume_gate(
    *, parent: ApprovedPhysicalDirectory, target: Path
) -> int:
    try:
        information = win32api.GetVolumeInformation(parent.identity.volume_guid)
        if not isinstance(information, tuple) or len(information) < 5:
            raise RuntimeError("GetVolumeInformation returned an invalid tuple")
        volume_serial = information[1]
        maximum_component_length = information[2]
        filesystem_name = information[4]
        if type(volume_serial) is not int:
            raise RuntimeError("volume serial is not an integer")
        if volume_serial != parent.identity.volume_serial:
            raise RuntimeError("approved volume serial changed")
        if filesystem_name != "NTFS":
            raise RuntimeError("durable native rename requires NTFS")
        if (
            type(maximum_component_length) is not int
            or maximum_component_length <= 0
            or maximum_component_length > _ULONG_MAX
        ):
            raise RuntimeError("volume maximum component length is invalid")
        return maximum_component_length
    except BaseException as exc:
        typed_error = DurableWriteError(
            stage=DurableWriteStage.VOLUME_GATE,
            target=target,
            evidence_owner=None,
            cause=exc,
            path_state=None,
            handle_state=None,
        )
        _raise_typed_boundary_error(typed_error, exc)


def _validate_native_basename(
    basename: str, *, maximum_component_length: int
) -> bytes:
    try:
        storage_policy._validate_child_name(basename)
        encoded = basename.encode("utf-16-le", "strict")
    except Exception as exc:
        raise ValueError("filename is not a strict native basename") from exc
    code_units = len(encoded) // 2
    if (
        not encoded
        or len(encoded) % 2 != 0
        or len(encoded) > _ULONG_MAX
        or code_units > maximum_component_length
    ):
        raise ValueError("filename exceeds the approved NTFS component limit")
    if any(character in basename for character in ("/", "\\", ":", "\x00")):
        raise ValueError("filename is not one ordinary basename")
    return encoded


def _create_and_adopt(
    *,
    lifecycle: _DurableWriteLifecycle,
    parent: ApprovedPhysicalDirectory,
    target: Path,
    sha256: str,
    byte_count: int,
) -> _DurableOwner:
    last_collision: BaseException | None = None
    for _attempt in range(_TEMP_CREATE_ATTEMPTS):
        temp_path = Path(parent.stable_volume_path) / (
            f".durable-write-{secrets.token_hex(24)}.tmp"
        )
        owner = _DurableOwner(
            temp_path=temp_path,
            target_path=target,
            sha256=sha256,
            byte_count=byte_count,
        )
        lifecycle.current_owner = owner
        guard = DurableHandleAdoptionGuard(owner)
        returned_handle: object = _NO_HANDLE_RETURN
        owner.create_phase = _CreatePhase.CALL_IN_FLIGHT
        try:
            returned_handle = win32file.CreateFile(
                str(temp_path),
                win32con.GENERIC_READ
                | win32con.GENERIC_WRITE
                | win32con.DELETE
                | _FILE_READ_ATTRIBUTES
                | win32con.SYNCHRONIZE,
                win32file.FILE_SHARE_READ,
                None,
                win32con.CREATE_NEW,
                win32con.FILE_ATTRIBUTE_NORMAL
                | win32file.FILE_FLAG_OPEN_REPARSE_POINT
                | win32file.FILE_FLAG_WRITE_THROUGH,
                None,
            )
            owner.create_phase = _CreatePhase.RETURNED_UNCOMMITTED
            guard.hold_returned(returned_handle)
            _after_create_return_before_attach(returned_handle)
            return guard.commit()
        except pywintypes.error as exc:
            if returned_handle is _NO_HANDLE_RETURN:
                owner.create_phase = _CreatePhase.KNOWN_NO_HANDLE
            if _winerror(exc) in {_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}:
                if _nonexception_leaves(exc):
                    typed_error = _error_from_owner(
                        stage=DurableWriteStage.CREATE_TEMP,
                        owner=owner,
                        cause=exc,
                        include_unconfirmed=False,
                    )
                    _raise_typed_boundary_error(typed_error, exc)
                last_collision = exc
                continue
            typed_error = _error_from_owner(
                stage=DurableWriteStage.CREATE_TEMP,
                owner=owner,
                cause=exc,
                include_unconfirmed=False,
            )
            _raise_typed_boundary_error(typed_error, exc)
        finally:
            if owner.create_phase in {
                _CreatePhase.CALL_IN_FLIGHT,
                _CreatePhase.RETURNED_UNCOMMITTED,
            }:
                owner.path_state = DurablePathState.CREATE_UNKNOWN
                owner.handle_state = DurableHandleState.CREATE_IN_FLIGHT
                _run_unadopted_create_failfast_hook(owner)
                _begin_failfast(
                    owner,
                    _failfast_unadopted_create,
                    _FAILFAST_UNADOPTED_CREATE,
                )

    assert last_collision is not None
    owner = _DurableOwner(
        temp_path=Path(parent.stable_volume_path) / ".durable-write-collision.tmp",
        target_path=target,
        sha256=sha256,
        byte_count=byte_count,
    )
    typed_error = _error_from_owner(
        stage=DurableWriteStage.CREATE_TEMP,
        owner=owner,
        cause=last_collision,
        include_unconfirmed=False,
    )
    _raise_typed_boundary_error(typed_error, last_collision)


def _set_and_verify_noninherit(owner: _DurableOwner) -> None:
    win32api.SetHandleInformation(
        owner.handle, win32con.HANDLE_FLAG_INHERIT, 0
    )
    flags = win32api.GetHandleInformation(owner.handle)
    if int(flags) & win32con.HANDLE_FLAG_INHERIT:
        raise OSError("durable source handle remained inheritable")


def _write_exact(owner: _DurableOwner, payload: bytes) -> None:
    result = win32file.WriteFile(owner.handle, payload)
    if not isinstance(result, tuple) or len(result) != 2:
        raise OSError("WriteFile returned an invalid result")
    status, written = result
    if status not in (0, None) or int(written) != len(payload):
        raise OSError(
            f"short durable WriteFile result: status={status!r}, bytes={written!r}"
        )


def _snapshot_handle(handle: object) -> _HandleSnapshot:
    information = win32file.GetFileInformationByHandle(handle)
    attributes = int(information[0])
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise RuntimeError("durable source is a reparse point")
    if attributes & _FILE_ATTRIBUTE_DIRECTORY:
        raise RuntimeError("durable source is a directory")
    identity = DurableFileIdentity(
        volume_serial=int(information[4]),
        file_index=(int(information[8]) << 32) | int(information[9]),
    )
    return _HandleSnapshot(
        identity=identity,
        attributes=attributes,
        byte_count=(int(information[5]) << 32) | int(information[6]),
        link_count=int(information[7]),
    )


def _verify_pre_rename_snapshot(
    *,
    snapshot: _HandleSnapshot,
    parent: ApprovedPhysicalDirectory,
    expected_byte_count: int,
) -> None:
    if snapshot.identity.volume_serial != parent.identity.volume_serial:
        raise RuntimeError("durable source escaped the approved volume")
    if snapshot.identity.file_index == 0:
        raise RuntimeError("durable source file identity is zero")
    if snapshot.link_count != 1:
        raise RuntimeError("durable source link count is not one")
    if snapshot.byte_count != expected_byte_count:
        raise RuntimeError("durable source size differs from WriteFile count")


def _build_native_rename_storage(
    *, name_utf16le: bytes, replace: bool
) -> tuple[Any, _IO_STATUS_BLOCK, int]:
    allocation_length = ctypes.sizeof(_FILE_RENAME_INFORMATION_HEAD) + len(
        name_utf16le
    )
    if allocation_length > _ULONG_MAX:
        raise ValueError("native rename buffer exceeds ULONG")
    storage = ctypes.create_string_buffer(allocation_length)
    head = _FILE_RENAME_INFORMATION_HEAD.from_buffer(storage)
    head.ReplaceIfExists = 1 if replace else 0
    head.RootDirectory = None
    head.FileNameLength = len(name_utf16le)
    ctypes.memmove(
        ctypes.addressof(storage) + _FILE_RENAME_INFORMATION_HEAD.FileName.offset,
        name_utf16le,
        len(name_utf16le),
    )
    iosb = _IO_STATUS_BLOCK()
    iosb.Status = _STATUS_PENDING
    iosb.Information = 0
    return storage, iosb, allocation_length


def _native_sibling_rename(
    *,
    owner: _DurableOwner,
    basename: str,
    name_utf16le: bytes,
    replace: bool,
) -> None:
    del basename  # Validation and UTF-16 freezing occurred before file creation.
    storage, iosb, allocation_length = _build_native_rename_storage(
        name_utf16le=name_utf16le,
        replace=replace,
    )
    owner.native_storage = storage
    owner.native_iosb = iosb
    owner.native_name = name_utf16le
    owner.path_state = DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
    returned_status: object = _NO_NATIVE_RETURN
    status: int | None = None
    status_error: NativeRenameStatusError | None = None
    status_reconcile_cause: BaseException | None = None
    captured_cause: BaseException | None = None
    try:
        try:
            returned_status = _NtSetInformationFile(
                owner.raw_handle,
                ctypes.byref(iosb),
                ctypes.c_void_p(ctypes.addressof(storage)),
                allocation_length,
                _FILE_RENAME_INFORMATION_CLASS,
            )
            owner.native_return_observed = True
            status = _signed_ntstatus(returned_status)
            if (status & _ULONG_MAX) == _STATUS_PENDING:
                return
            _after_native_return_before_status_commit(owner, status)
            status_error = _commit_native_status(owner, status)
            status_reconcile_cause = _apply_native_status_semantics(owner)
            if status == _STATUS_SUCCESS:
                return
            assert status_error is not None
            _before_native_status_throw(owner, status_error)
            raise status_error
        except BaseException as exc:
            if owner.failfast_started:
                raise
            captured_cause = exc
    finally:
        _before_native_pending_settle_call(owner, returned_status)
        _settle_native_pending(owner, returned_status)

    if status_error is None:
        status_error = owner.committed_native_status_error
    status_reconcile_cause = _apply_native_status_semantics(
        owner,
        reconcile_interrupted_success=(
            owner.committed_native_status == _STATUS_SUCCESS
            and not owner.native_status_semantics_committed
        ),
    )
    _after_native_pending_settled_before_status_handoff(
        owner, status_error, captured_cause
    )
    assert captured_cause is not None
    if status_error is captured_cause:
        owner.committed_native_status_error = status_error
        try:
            _before_native_status_handoff(owner, status_error)
        except BaseException as handoff_cause:
            handoff_members: list[BaseException] = [status_error, handoff_cause]
            if status_reconcile_cause is not None:
                handoff_members.append(status_reconcile_cause)
            raise BaseExceptionGroup(
                "prepared native status handoff was interrupted",
                handoff_members,
            ) from handoff_cause
        final_status_error: BaseException | None = None
        try:
            _before_native_status_final_raise(
                owner, status_error, status_reconcile_cause
            )
            if status_reconcile_cause is not None:
                final_status_error = _NativeRenameInterrupted(
                    status_error, status_reconcile_cause
                )
            else:
                final_status_error = status_error
            raise final_status_error
        except BaseException as final_raise_cause:
            if final_raise_cause is final_status_error:
                raise
            final_members: list[BaseException] = [status_error]
            if status_reconcile_cause is not None:
                final_members.append(status_reconcile_cause)
            if all(
                final_raise_cause is not member for member in final_members
            ):
                final_members.append(final_raise_cause)
            raise BaseExceptionGroup(
                "committed native status final raise was interrupted",
                final_members,
            ) from final_raise_cause

    if status_error is not None:
        handoff_members: list[BaseException] = [status_error, captured_cause]
        if status_reconcile_cause is not None:
            handoff_members.append(status_reconcile_cause)
        raise BaseExceptionGroup(
            "prepared native status and status-throw interruption",
            handoff_members,
        ) from captured_cause
    reconcile_cause = _apply_native_status_semantics(owner)
    if not isinstance(captured_cause, Exception):
        if reconcile_cause is None:
            raise captured_cause
        interruption_members = (
            list(captured_cause.exceptions)
            if isinstance(captured_cause, BaseExceptionGroup)
            else [captured_cause]
        )
        if all(reconcile_cause is not member for member in interruption_members):
            interruption_members.append(reconcile_cause)
        raise BaseExceptionGroup(
            "native non-Exception interruption and reconciliation evidence",
            interruption_members,
        ) from captured_cause
    if reconcile_cause is not None:
        raise _NativeRenameInterrupted(
            captured_cause, reconcile_cause
        ) from captured_cause
    raise _NativeRenameInterrupted(captured_cause) from captured_cause


def _commit_native_status(
    owner: _DurableOwner, status_value: int
) -> NativeRenameStatusError | None:
    signed_status = _signed_ntstatus(status_value)
    committed_status = owner.committed_native_status
    if committed_status is not None and committed_status != signed_status:
        raise RuntimeError("native status changed after it was committed")
    owner.committed_native_status = signed_status

    u32_status = signed_status & _ULONG_MAX
    status_error = owner.committed_native_status_error
    if u32_status in {_STATUS_SUCCESS, _STATUS_PENDING}:
        if status_error is not None:
            raise RuntimeError("successful or pending native status has an error")
        return None

    if signed_status < 0:
        reconcile_cause = _apply_native_status_semantics(owner)
        if reconcile_cause is not None:
            raise RuntimeError("negative native status unexpectedly reconciled")

    if status_error is None:
        status_error = _native_status_error(signed_status)
        owner.committed_native_status_error = status_error
    elif (
        status_error.signed_status != signed_status
        or status_error.u32_status != u32_status
    ):
        raise RuntimeError("native status error disagrees with committed status")
    mapping_cause = status_error.mapping_cause
    if (
        owner.committed_native_mapping_cause is not None
        and owner.committed_native_mapping_cause is not mapping_cause
    ):
        raise RuntimeError("native status mapping cause changed after commit")
    owner.committed_native_mapping_cause = mapping_cause
    return status_error


def _apply_native_status_semantics(
    owner: _DurableOwner,
    *,
    reconcile_interrupted_success: bool = False,
) -> BaseException | None:
    if owner.native_status_semantics_committed:
        return owner.committed_native_reconcile_cause

    status = owner.committed_native_status
    if not owner.native_return_observed:
        reconcile_cause = _try_reconcile_owner_location(owner)
    elif status is None:
        raise RuntimeError("observed native return has no committed status")
    elif status == _STATUS_SUCCESS and not reconcile_interrupted_success:
        owner.path_state = DurablePathState.TARGET_CONFIRMED
        reconcile_cause = None
    elif status < 0:
        owner.path_state = DurablePathState.TEMP_CONFIRMED
        reconcile_cause = None
    else:
        reconcile_cause = _try_reconcile_owner_location(owner)

    owner.committed_native_reconcile_cause = reconcile_cause
    owner.native_status_semantics_committed = True
    return reconcile_cause


def _settle_native_pending(
    owner: _DurableOwner, returned_status: object
) -> None:
    try:
        owner.native_pending_decision = _NativePendingDecision.UNKNOWN
        iosb = owner.native_iosb
        if iosb is None:
            raise RuntimeError("native pending decision lost its IO status block")
        _before_native_pending_decision(owner, returned_status, iosb)
        if returned_status is _NO_NATIVE_RETURN:
            owner.native_return_observed = False
            status_value = _signed_ntstatus(iosb.Status)
        else:
            owner.native_return_observed = True
            status_value = _signed_ntstatus(returned_status)
            _commit_native_status(owner, status_value)
        owner.native_pending_decision = (
            _NativePendingDecision.PENDING
            if (status_value & _ULONG_MAX) == _STATUS_PENDING
            else _NativePendingDecision.NOT_PENDING
        )
    finally:
        if owner.native_pending_decision is not _NativePendingDecision.NOT_PENDING:
            _begin_failfast(
                owner,
                _failfast_pending_native_rename,
                _FAILFAST_PENDING_RENAME,
            )


def _failfast_if_native_pending_ambiguous(owner: _DurableOwner) -> None:
    if owner.native_iosb is None:
        return
    status = owner.committed_native_status
    status_error = owner.committed_native_status_error
    ambiguous = owner.native_pending_decision is not _NativePendingDecision.NOT_PENDING
    if not ambiguous and not owner.native_return_observed:
        ambiguous = status is not None or status_error is not None
    elif not ambiguous and status is None:
        ambiguous = True
    elif not ambiguous:
        assert status is not None
        u32_status = status & _ULONG_MAX
        ambiguous = (
            u32_status == _STATUS_PENDING
            or (u32_status == _STATUS_SUCCESS and status_error is not None)
            or (
                u32_status != _STATUS_SUCCESS
                and (
                    status_error is None
                    or status_error.signed_status != status
                    or status_error.u32_status != u32_status
                )
            )
        )
    if ambiguous:
        _begin_failfast(
            owner,
            _failfast_pending_native_rename,
            _FAILFAST_PENDING_RENAME,
        )


def _failfast_if_create_ambiguous(owner: _DurableOwner) -> None:
    if owner.create_phase in {
        _CreatePhase.CALL_IN_FLIGHT,
        _CreatePhase.RETURNED_UNCOMMITTED,
    }:
        _begin_failfast(
            owner,
            _failfast_unadopted_create,
            _FAILFAST_UNADOPTED_CREATE,
        )


def _run_unadopted_create_failfast_hook(owner: _DurableOwner) -> None:
    if not owner.unadopted_failfast_hook_attempted:
        owner.unadopted_failfast_hook_attempted = True
        _before_unadopted_create_failfast_call(owner)


def _try_reconcile_owner_location(owner: _DurableOwner) -> BaseException | None:
    try:
        snapshot = _snapshot_handle(owner.handle)
        if owner.identity is not None and snapshot.identity != owner.identity:
            raise RuntimeError("native reconcile identity changed")
        final_path = win32file.GetFinalPathNameByHandle(
            owner.handle, _VOLUME_NAME_GUID
        )
        normalized = _norm_windows_path(final_path)
        if normalized == _norm_windows_path(owner.target_path):
            owner.path_state = DurablePathState.TARGET_CONFIRMED
        elif normalized == _norm_windows_path(owner.temp_path):
            owner.path_state = DurablePathState.TEMP_CONFIRMED
        else:
            owner.path_state = DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
            raise RuntimeError("native reconcile path matched neither candidate")
        return None
    except BaseException as exc:
        owner.path_state = DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
        return exc


def _verify_renamed_handle(
    *,
    owner: _DurableOwner,
    parent: ApprovedPhysicalDirectory,
    expected_sha256: str,
    expected_byte_count: int,
    max_bytes: int,
) -> None:
    if owner.path_state is not DurablePathState.TARGET_CONFIRMED:
        raise RuntimeError("durable target location is not confirmed")
    if owner.pre_snapshot is None or owner.identity is None:
        raise RuntimeError("pre-rename source identity was not frozen")
    snapshot = _snapshot_handle(owner.handle)
    if snapshot.identity != owner.pre_snapshot.identity:
        raise RuntimeError("renamed handle identity differs from source")
    if snapshot.identity.volume_serial != parent.identity.volume_serial:
        raise RuntimeError("renamed handle escaped the approved volume")
    if snapshot.link_count != 1:
        raise RuntimeError("renamed handle link count is not one")
    if snapshot.byte_count != expected_byte_count:
        raise RuntimeError("renamed handle size differs from expected bytes")
    final_path = win32file.GetFinalPathNameByHandle(
        owner.handle, _VOLUME_NAME_GUID
    )
    if _norm_windows_path(final_path) != _norm_windows_path(owner.target_path):
        raise RuntimeError("renamed handle path differs from frozen target")
    actual_sha256, actual_byte_count = _bounded_hash_from_handle(
        handle=owner.handle,
        max_bytes=max_bytes,
    )
    if (
        actual_byte_count != expected_byte_count
        or actual_sha256 != expected_sha256
    ):
        raise RuntimeError("renamed handle bytes differ from frozen payload")


def _bounded_hash_from_handle(
    *, handle: object, max_bytes: int
) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    while byte_count <= max_bytes:
        remaining = max_bytes + 1 - byte_count
        read_size = min(_READ_CHUNK_BYTES, remaining)
        try:
            status, chunk = win32file.ReadFile(handle, read_size)
        except pywintypes.error as exc:
            if _winerror(exc) == _ERROR_HANDLE_EOF:
                if _nonexception_leaves(exc):
                    raise
                break
            raise
        if status not in (0, None):
            raise OSError(f"ReadFile returned status {status!r}")
        if not chunk:
            break
        digest.update(chunk)
        byte_count += len(chunk)
        if byte_count > max_bytes:
            raise ValueError("destination exceeds max_bytes")
    return digest.hexdigest(), byte_count


def _close_owner(owner: _DurableOwner) -> DurableWriteError | None:
    if owner.handle_state is not DurableHandleState.OPEN or owner.handle is None:
        return _error_from_owner(
            stage=DurableWriteStage.CLOSE,
            owner=owner,
            cause=RuntimeError("durable owner reached close without one open handle"),
        )
    close_phase = _ClosePhase.NOT_STARTED
    detached_raw: int | None = None
    close_cause: BaseException | None = None
    try:
        try:
            owner.handle_state = DurableHandleState.CLOSE_IN_FLIGHT
            _before_close_detach_identity(owner)
            frozen_raw = int(owner.handle)
            if frozen_raw == 0 or owner.raw_handle != frozen_raw:
                raise RuntimeError("frozen raw handle identity is invalid")
            close_phase = _ClosePhase.DETACH_IN_FLIGHT
            detached_raw = int(owner.handle.Detach())
            if detached_raw == 0 or detached_raw != frozen_raw:
                raise RuntimeError("Detach returned a different raw handle identity")
            owner.handle = None
            owner.raw_handle = detached_raw
            close_phase = _ClosePhase.DETACHED_CONFIRMED
            _after_detach_identity_before_close(owner, detached_raw)
            close_phase = _ClosePhase.CLOSE_CALL_IN_FLIGHT
            result = win32api.CloseHandle(detached_raw)
            if result is not None:
                raise RuntimeError("CloseHandle returned a non-None result")
            close_phase = _ClosePhase.CLOSE_RETURNED_NONE
            _after_close_return_before_state_commit(owner, detached_raw)
            owner.handle_state = DurableHandleState.CLOSED
            close_phase = _ClosePhase.CLOSED
            _after_close_terminal_commit_before_return(owner, detached_raw)
            return None
        except BaseException as exc:
            if owner.failfast_started:
                raise
            if (
                close_phase is _ClosePhase.CLOSED
                and owner.handle_state is DurableHandleState.CLOSED
            ):
                raise
            _before_close_cause_capture(owner, detached_raw, exc)
            close_cause = exc
    finally:
        if close_phase in {
            _ClosePhase.NOT_STARTED,
            _ClosePhase.DETACH_IN_FLIGHT,
        }:
            _begin_failfast(
                owner,
                _failfast_detach_identity,
                _FAILFAST_DETACH_IDENTITY,
            )
        if close_phase is _ClosePhase.DETACHED_CONFIRMED:
            _begin_failfast(
                owner,
                _failfast_close_quarantine,
                _FAILFAST_CLOSE_QUARANTINE,
            )
        if close_phase in {
            _ClosePhase.CLOSE_CALL_IN_FLIGHT,
            _ClosePhase.CLOSE_RETURNED_NONE,
        }:
            if close_cause is None or detached_raw is None:
                _begin_failfast(
                    owner,
                    _failfast_close_quarantine,
                    _FAILFAST_CLOSE_QUARANTINE,
                )
            try:
                try:
                    _after_close_exception_before_quarantine(
                        owner, detached_raw, close_cause
                    )
                except BaseException as transition_cause:
                    close_cause = BaseExceptionGroup(
                        "close result and quarantine transition both interrupted",
                        [close_cause, transition_cause],
                    )
                _before_close_quarantine_handoff(
                    owner, detached_raw, close_cause
                )
                token = _quarantine_close_unknown(
                    owner=owner,
                    raw_handle=detached_raw,
                    cause=close_cause,
                )
                if owner.committed_close_token is not token:
                    _begin_failfast(
                        owner,
                        _failfast_close_quarantine,
                        _FAILFAST_CLOSE_QUARANTINE,
                    )
                close_error = owner.committed_close_error
                if close_error is None:
                    _begin_failfast(
                        owner,
                        _failfast_close_quarantine,
                        _FAILFAST_CLOSE_QUARANTINE,
                    )
                _after_close_unknown_commit_before_typed_handoff(
                    owner, token, close_error
                )
                return owner.committed_close_error
            finally:
                if owner.handle_state is not DurableHandleState.CLOSE_UNKNOWN:
                    _begin_failfast(
                        owner,
                        _failfast_close_quarantine,
                        _FAILFAST_CLOSE_QUARANTINE,
                    )


def _quarantine_close_unknown(
    *, owner: _DurableOwner, raw_handle: int, cause: BaseException
) -> CloseUnknownToken:
    try:
        _before_close_quarantine_lock(owner)
        with _CLOSE_UNKNOWN_REGISTRY_LOCK:
            _after_close_quarantine_lock(owner)
            if len(_CLOSE_UNKNOWN_REGISTRY) >= _CLOSE_UNKNOWN_REGISTRY_CAPACITY:
                _begin_failfast(
                    owner,
                    _failfast_close_quarantine,
                    _FAILFAST_CLOSE_QUARANTINE,
                )
            token = CloseUnknownToken(
                value=secrets.token_hex(24),
                path_state=owner.path_state,
                handle_state=DurableHandleState.CLOSE_UNKNOWN,
            )
            if token.value in _CLOSE_UNKNOWN_REGISTRY:
                _begin_failfast(
                    owner,
                    _failfast_close_quarantine,
                    _FAILFAST_CLOSE_QUARANTINE,
                )
            entry = _CloseUnknownEntry(
                token=token,
                owner=owner,
                raw_handle=raw_handle,
                cause=cause,
            )
            _CLOSE_UNKNOWN_REGISTRY[token.value] = entry
            _after_close_quarantine_insert(owner)
            close_error = DurableWriteError(
                stage=DurableWriteStage.CLOSE,
                target=owner.target_path,
                evidence_owner=token,
                cause=cause,
                path_state=owner.path_state,
                handle_state=DurableHandleState.CLOSE_UNKNOWN,
            )
            owner.committed_close_token = token
            owner.committed_close_cause = cause
            owner.committed_close_error = close_error
            _before_close_quarantine_state_commit(owner)
            owner.handle_state = DurableHandleState.CLOSE_UNKNOWN
            return token
    except BaseException:
        if owner.failfast_started:
            raise
        if (
            owner.handle_state is DurableHandleState.CLOSE_UNKNOWN
            and owner.committed_close_token is not None
            and owner.committed_close_cause is cause
            and owner.committed_close_error is not None
        ):
            raise
        _begin_failfast(
            owner,
            _failfast_close_quarantine,
            _FAILFAST_CLOSE_QUARANTINE,
        )


def _error_from_owner(
    *,
    stage: DurableWriteStage,
    owner: _DurableOwner,
    cause: BaseException,
    include_unconfirmed: bool = True,
) -> DurableWriteError:
    evidence: DurableEvidenceOwner | None
    if include_unconfirmed:
        evidence = DurableEvidenceOwner(
            temp_path=owner.temp_path,
            target_path=owner.target_path,
            sha256=owner.sha256,
            byte_count=owner.byte_count,
            identity=owner.identity,
            path_state=owner.path_state,
            handle_state=owner.handle_state,
        )
    else:
        evidence = None
    return DurableWriteError(
        stage=stage,
        target=owner.target_path,
        evidence_owner=evidence,
        cause=cause,
        path_state=owner.path_state,
        handle_state=owner.handle_state,
    )


def _native_status_error(status: int) -> NativeRenameStatusError:
    mapping_cause: BaseException | None = None
    try:
        mapped = int(_RtlNtStatusToDosError(status))
    except BaseException as exc:
        mapped = _ERROR_MR_MID_NOT_FOUND
        mapping_cause = exc
    return NativeRenameStatusError(
        signed_status=status,
        mapped_winerror=mapped,
        mapping_cause=mapping_cause,
    )


def _signed_ntstatus(value: object) -> int:
    return ctypes.c_int32(int(value) & _ULONG_MAX).value


def _winerror(error: BaseException) -> int | None:
    value = getattr(error, "winerror", None)
    if value is not None:
        return int(value)
    if error.args and isinstance(error.args[0], int):
        return int(error.args[0])
    return None


def _norm_windows_path(value: object) -> str:
    return ntpath.normcase(ntpath.normpath(str(value).replace("/", "\\")))


def _begin_failfast(
    owner: _DurableOwner,
    callback: Any,
    fallback_exit_code: int,
) -> NoReturn:
    try:
        _before_failfast_flag(owner, fallback_exit_code)
        owner.failfast_started = True
        _before_failfast_callback(owner, callback, fallback_exit_code)
        callback(owner)
    finally:
        os._exit(fallback_exit_code)
