"""Durable, route-free metadata store for future material submissions.

This module deliberately stores only serializable submission metadata.  It is
not an owner-lane registry and it never stores a ``Future``, SQLAlchemy
session, mutex, runner, publisher, or retained recovery state.  A later
coordinator can use it to make an asynchronous ``202`` boundary safe, but the
legacy material route is not wired here.

The sidecar is a file-backed SQLite database on D:.  Each operation uses a
short transaction with ``synchronous=FULL`` and DELETE journaling.  State
transitions are compare-and-set operations so a stale callback cannot silently
overwrite newer terminal evidence.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import json
import ntpath
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterator, Literal, Mapping
from uuid import UUID

from app.pipeline.material_execution_api import serialize_material_execution_response
from app.pipeline.material_execution_api_context import (
    MaterialExecutionSubmissionContext,
)
from app.pipeline.material_execution_application import (
    MaterialExecutionCommand,
    MaterialExecutionResponse,
    MaterialExecutionLaneKey,
    MaterialRecoveryTicket,
)
from app.pipeline.material_execution_coordinator import MaterialCompletionReceipt


SCHEMA_VERSION = 2
MAX_ERROR_DETAIL_CHARS = 2000
MAX_ERROR_CODE_CHARS = 64
MAX_JSON_BYTES = 1024 * 1024
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OWNER_LEASE_END_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OWNER_LEASE_ACQUISITION_KINDS = frozenset(
    {"initial", "clean_handoff", "abandoned_mutex", "exclusive_reacquire"}
)
_DOS_DEVICE_BASENAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_OPERATIONS = frozenset(
    {
        "official_capture",
        "pexels",
        "pixabay",
        "coverr",
        "info_motion",
        "stock_candidates",
    }
)
SubmissionState = Literal[
    "accepted",
    "dispatching",
    "running",
    "succeeded",
    "failed",
    "blocked",
    "reconciling",
    "unknown",
]
_STATES = frozenset(
    {
        "accepted",
        "dispatching",
        "running",
        "succeeded",
        "failed",
        "blocked",
        "reconciling",
        "unknown",
    }
)
_TRANSITIONS: dict[str, frozenset[str]] = {
    "accepted": frozenset(
        {"dispatching", "succeeded", "failed", "blocked"}
    ),
    # A callback can win the CAS before the enqueue/state write is durable.
    # These terminal edges are intentional crash-window adoption edges, not
    # permission to skip the owner lane during normal dispatch.
    "dispatching": frozenset(
        {"running", "succeeded", "failed", "blocked", "unknown"}
    ),
    "running": frozenset({"succeeded", "failed", "blocked", "unknown"}),
    "blocked": frozenset({"reconciling"}),
    "reconciling": frozenset({"succeeded", "failed", "blocked", "unknown"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    # Unknown is never retried automatically.  It can only be explicitly
    # adopted (with a typed recovery ticket) or closed as a failed attempt.
    "unknown": frozenset({"failed", "reconciling"}),
}


class MaterialExecutionSubmissionError(RuntimeError):
    """Base class for submission-store failures."""


class MaterialExecutionSubmissionPathError(MaterialExecutionSubmissionError):
    """The sidecar path is not a safe physical D: file."""


class MaterialExecutionSubmissionSchemaError(MaterialExecutionSubmissionError):
    """The sidecar schema or a stored row cannot be trusted."""


class MaterialExecutionSubmissionIdentityConflict(MaterialExecutionSubmissionError):
    """A submission ID or metadata belongs to another command identity."""


class MaterialExecutionSubmissionStateConflict(MaterialExecutionSubmissionError):
    """A compare-and-set observed a state other than its expected state."""

    def __init__(self, message: str, *, current_state: str | None = None) -> None:
        super().__init__(message)
        self.current_state = current_state


class MaterialExecutionSubmissionTransitionError(MaterialExecutionSubmissionError):
    """A requested state transition or its metadata is invalid."""


class MaterialExecutionSubmissionNotFound(MaterialExecutionSubmissionError):
    """The requested submission ID is absent."""


@dataclass(frozen=True, slots=True)
class MaterialExecutionOwnerToken:
    """The only credential accepted for an owned submission mutation.

    The epoch is allocated transactionally after a process-wide Windows mutex
    has been acquired.  UUIDs remain useful for audit, but are not liveness
    evidence and are never sufficient on their own.
    """

    owner_instance_id: str
    owner_epoch: int

    def __post_init__(self) -> None:
        _validate_uuid(self.owner_instance_id, "owner_instance_id")
        if type(self.owner_epoch) is not int or self.owner_epoch <= 0:
            raise ValueError("owner_epoch must be a positive integer")


@dataclass(frozen=True, slots=True)
class MaterialExecutionOwnerLeaseCapability:
    """Process-local capability for mutating one live owner epoch.

    The durable token is intentionally not enough for an owner-bound CAS.  The
    opaque nonce is held only by the exact store/lease instance that acquired
    the mutex, so another process that merely reads ``owner_instance_id`` and
    ``owner_epoch`` from SQLite cannot replay the mutation.
    """

    owner_instance_id: str
    owner_epoch: int
    _nonce: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class MaterialExecutionOwnerLeaseRecord:
    """Durable audit row for one exclusive submission owner epoch."""

    schema_version: int
    owner_epoch: int
    owner_instance_id: str
    mutex_name: str
    acquired_at: str
    ended_at: str | None = None
    end_reason: str | None = None
    acquisition_kind: str = "initial"
    process_id: int | None = None
    holder_thread_id: int | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise MaterialExecutionSubmissionSchemaError(
                f"unsupported owner lease schema version: {self.schema_version}"
            )
        if type(self.owner_epoch) is not int or self.owner_epoch <= 0:
            raise MaterialExecutionSubmissionSchemaError("owner lease epoch is invalid")
        _validate_uuid(self.owner_instance_id, "owner_instance_id")
        if (
            type(self.mutex_name) is not str
            or not self.mutex_name
            or len(self.mutex_name) > 260
        ):
            raise MaterialExecutionSubmissionSchemaError("owner lease mutex name is invalid")
        if type(self.acquired_at) is not str or not self.acquired_at:
            raise MaterialExecutionSubmissionSchemaError("owner lease acquired_at is invalid")
        try:
            acquired_at = datetime.fromisoformat(self.acquired_at)
        except (TypeError, ValueError) as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "owner lease acquired_at is invalid"
            ) from exc
        if acquired_at.tzinfo is None or acquired_at.utcoffset() is None:
            raise MaterialExecutionSubmissionSchemaError(
                "owner lease acquired_at must include a timezone"
            )
        if self.ended_at is not None:
            if type(self.ended_at) is not str or not self.ended_at:
                raise MaterialExecutionSubmissionSchemaError("owner lease ended_at is invalid")
            try:
                ended_at = datetime.fromisoformat(self.ended_at)
            except (TypeError, ValueError) as exc:
                raise MaterialExecutionSubmissionSchemaError(
                    "owner lease ended_at is invalid"
                ) from exc
            if ended_at.tzinfo is None or ended_at.utcoffset() is None:
                raise MaterialExecutionSubmissionSchemaError(
                    "owner lease ended_at must include a timezone"
                )
            if ended_at < acquired_at:
                raise MaterialExecutionSubmissionSchemaError(
                    "owner lease ended_at precedes acquired_at"
                )
        if self.end_reason is not None and (
            type(self.end_reason) is not str
            or _OWNER_LEASE_END_REASON_RE.fullmatch(self.end_reason) is None
        ):
            raise MaterialExecutionSubmissionSchemaError("owner lease end_reason is invalid")
        if (self.ended_at is None) != (self.end_reason is None):
            raise MaterialExecutionSubmissionSchemaError(
                "owner lease end metadata is incomplete"
            )
        if type(self.acquisition_kind) is not str or self.acquisition_kind not in _OWNER_LEASE_ACQUISITION_KINDS:
            raise MaterialExecutionSubmissionSchemaError("owner lease acquisition kind is invalid")
        for value, label in (
            (self.process_id, "process_id"),
            (self.holder_thread_id, "holder_thread_id"),
        ):
            if value is not None and (type(value) is not int or value <= 0):
                raise MaterialExecutionSubmissionSchemaError(f"owner lease {label} is invalid")


@dataclass(frozen=True, slots=True)
class MaterialExecutionOwnerLeaseActivation:
    """Result of one mutex-backed, transactionally fenced activation."""

    token: MaterialExecutionOwnerToken
    record: MaterialExecutionOwnerLeaseRecord
    acquisition_kind: str
    was_abandoned: bool
    fenced_submissions: tuple["MaterialExecutionSubmissionRecord", ...] = ()
    capability: MaterialExecutionOwnerLeaseCapability | None = field(
        default=None, repr=False, compare=False
    )


def _is_reparse_point(path: Path) -> bool:
    """Return whether a path is a symlink or Windows reparse point."""

    try:
        if path.is_symlink():
            return True
        attributes = int(getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0))
    except OSError as exc:
        raise MaterialExecutionSubmissionPathError(
            f"cannot inspect submission path: {path}"
        ) from exc
    return bool(attributes & 0x0400)


def _normalise_sidecar_path(value: Path | str) -> Path:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value.strip():
        path = Path(value)
    else:
        raise MaterialExecutionSubmissionPathError("submission path must be a Path")
    _validate_lexical_d_path(str(path))
    if not path.is_absolute():
        raise MaterialExecutionSubmissionPathError(
            "submission path must be an absolute D: file"
        )
    # Inspect the lexical path before resolving it.  Resolving first would
    # silently follow a symlink/reparse point and defeat the physical-root
    # policy.
    probe = path
    while True:
        # ``Path.exists()`` follows links and returns false for a dangling
        # link.  ``lexists`` lets us reject both live and dangling reparse
        # points before ``resolve`` has a chance to follow them.
        if os.path.lexists(str(probe)) and _is_reparse_point(probe):
            raise MaterialExecutionSubmissionPathError(
                "submission path contains a reparse point"
            )
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise MaterialExecutionSubmissionPathError(
            "submission path cannot be resolved safely"
        ) from exc
    if resolved.drive.upper() != "D:":
        raise MaterialExecutionSubmissionPathError(
            "submission sidecar must remain on D:"
        )
    if resolved.name in {"", ".", ".."}:
        raise MaterialExecutionSubmissionPathError("submission path must name a file")
    return resolved


def _validate_lexical_d_path(raw_path: str) -> None:
    """Reject ambiguous Windows names before any path resolution occurs."""

    drive, tail = ntpath.splitdrive(raw_path)
    if drive.casefold() != "d:" or not tail.startswith(("\\", "/")):
        raise MaterialExecutionSubmissionPathError(
            "submission sidecar must be an absolute local D: path"
        )
    if raw_path.startswith(("\\\\", "//")):
        raise MaterialExecutionSubmissionPathError(
            "UNC submission paths are not allowed"
        )
    components = [part for part in tail.replace("/", "\\").split("\\") if part]
    for component in components:
        basename = component.split(".", 1)[0].rstrip(" .").casefold()
        if (
            component in {".", ".."}
            or component.endswith((" ", "."))
            or ":" in component
            or any(ord(character) < 32 for character in component)
            or any(character in component for character in ('"', "<", ">", "|", "?", "*"))
            or basename in _DOS_DEVICE_BASENAMES
        ):
            raise MaterialExecutionSubmissionPathError(
                "submission path contains an ambiguous Windows component"
            )


def _stat_identity(path: Path) -> tuple[int, int, int, int]:
    """Return a no-follow identity fence for a path component."""

    try:
        stat_result = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise MaterialExecutionSubmissionPathError(
            f"cannot inspect submission path identity: {path}"
        ) from exc
    return (
        int(getattr(stat_result, "st_dev", 0)),
        int(getattr(stat_result, "st_ino", 0)),
        int(getattr(stat_result, "st_nlink", 1)),
        int(getattr(stat_result, "st_file_attributes", 0)),
    )


def _runtime_identity(path: Path) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Capture the sidecar and its immediate parent without following links."""

    if not os.path.lexists(str(path)):
        raise MaterialExecutionSubmissionPathError(
            "submission sidecar disappeared during an operation"
        )
    if _is_reparse_point(path) or not path.is_file():
        raise MaterialExecutionSubmissionPathError(
            "submission sidecar is no longer a regular physical file"
        )
    file_identity = _stat_identity(path)
    if file_identity[2] != 1:
        raise MaterialExecutionSubmissionPathError(
            "submission sidecar must have one hard link"
        )
    parent = path.parent
    if _is_reparse_point(parent) or not parent.is_dir():
        raise MaterialExecutionSubmissionPathError(
            "submission sidecar parent is no longer a physical directory"
        )
    return file_identity, _stat_identity(parent)


def _validate_uuid(value: Any, label: str) -> str:
    if type(value) is not str or _UUID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical lowercase UUID")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a canonical lowercase UUID") from exc
    return value


def _validate_task_id(value: Any, label: str = "material_task_id") -> str:
    if type(value) is not str or _TASK_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is not a safe immutable identifier")
    return value


def _validate_sha256(value: Any, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _clone_json(value: Any, *, label: str, max_bytes: int = MAX_JSON_BYTES) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > max_bytes:
            raise ValueError(f"{label} exceeds the JSON size limit")
        return json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ValueError) and "size limit" in str(exc):
            raise
        raise TypeError(f"{label} must be JSON-safe") from exc


def _encode_json(value: Any, *, label: str) -> str:
    cloned = _clone_json(value, label=label)
    try:
        encoded = json.dumps(
            cloned,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} must be JSON-safe") from exc
    return encoded


def _decode_json(value: Any, *, label: str) -> Any:
    if type(value) is not str:
        raise MaterialExecutionSubmissionSchemaError(f"{label} is not JSON text")
    if len(value.encode("utf-8", "strict")) > MAX_JSON_BYTES:
        raise MaterialExecutionSubmissionSchemaError(f"{label} exceeds the JSON size limit")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MaterialExecutionSubmissionSchemaError(f"{label} is invalid JSON") from exc
    try:
        return _clone_json(parsed, label=label)
    except (TypeError, ValueError) as exc:
        raise MaterialExecutionSubmissionSchemaError(f"{label} is not JSON-safe") from exc


def _timestamp(previous: str | None = None) -> str:
    value = datetime.now(timezone.utc)
    if previous:
        try:
            old = datetime.fromisoformat(previous)
            if value <= old:
                value = old + timedelta(microseconds=1)
        except (TypeError, ValueError):
            pass
    return value.isoformat(timespec="microseconds")


def _expected_owner_mutex_name(database_path: Path) -> str | None:
    """Return the path-bound owner mutex name when Windows can prove it."""

    if os.name != "nt":
        return None
    try:
        from app.core.windows_named_mutex import build_mutex_name

        physical = os.path.realpath(os.path.abspath(os.fspath(database_path)))
        physical = physical.replace("/", "\\").casefold()
        return build_mutex_name("material-submission-owner", physical)
    except (ImportError, RuntimeError, ValueError) as exc:
        raise MaterialExecutionSubmissionSchemaError(
            "cannot derive the submission owner mutex name"
        ) from exc


@dataclass(frozen=True, slots=True)
class MaterialExecutionSubmissionErrorMetadata:
    """Bounded, JSON-only error evidence safe to expose to a status API."""

    code: str
    detail: str
    retryable: bool = False
    recovery_required: bool = False
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.code) is not str or _ERROR_CODE_RE.fullmatch(self.code) is None:
            raise ValueError("error code is not a safe identifier")
        if type(self.detail) is not str:
            raise TypeError("error detail must be a string")
        if len(self.detail) > MAX_ERROR_DETAIL_CHARS:
            raise ValueError("error detail exceeds bounded limit")
        if type(self.retryable) is not bool or type(self.recovery_required) is not bool:
            raise TypeError("error flags must be booleans")
        if not isinstance(self.data, Mapping):
            raise TypeError("error data must be a mapping")
        cloned = _clone_json(dict(self.data), label="error data")
        object.__setattr__(self, "data", cloned)

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "retryable": self.retryable,
            "recovery_required": self.recovery_required,
            "data": _clone_json(dict(self.data), label="error data"),
        }

    @classmethod
    def from_json(cls, value: Any) -> "MaterialExecutionSubmissionErrorMetadata":
        if not isinstance(value, Mapping):
            raise MaterialExecutionSubmissionSchemaError("error metadata is not an object")
        allowed = {"code", "detail", "retryable", "recovery_required", "data"}
        if set(value) - allowed:
            raise MaterialExecutionSubmissionSchemaError("error metadata has unknown fields")
        try:
            return cls(
                code=value.get("code"),
                detail=value.get("detail"),
                retryable=value.get("retryable", False),
                recovery_required=value.get("recovery_required", False),
                data=value.get("data", {}),
            )
        except (TypeError, ValueError) as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "error metadata is invalid"
            ) from exc


@dataclass(frozen=True, slots=True)
class MaterialExecutionSubmissionRecord:
    schema_version: int
    submission_id: str
    context: MaterialExecutionSubmissionContext
    state: SubmissionState
    revision: int
    created_at: str
    updated_at: str
    owner_instance_id: str | None = None
    owner_epoch: int | None = None
    attempt_id: str | None = None
    response_json: Mapping[str, Any] | None = None
    error: MaterialExecutionSubmissionErrorMetadata | None = None
    recovery_ticket: MaterialRecoveryTicket | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise MaterialExecutionSubmissionSchemaError(
                f"unsupported submission schema version: {self.schema_version}"
            )
        _validate_uuid(self.submission_id, "submission_id")
        if type(self.context) is not MaterialExecutionSubmissionContext:
            raise TypeError("context must be MaterialExecutionSubmissionContext")
        if type(self.state) is not str or self.state not in _STATES:
            raise MaterialExecutionSubmissionSchemaError("submission state is invalid")
        if type(self.revision) is not int or self.revision < 0:
            raise MaterialExecutionSubmissionSchemaError("submission revision is invalid")
        if self.owner_instance_id is not None:
            _validate_uuid(self.owner_instance_id, "owner_instance_id")
        if self.owner_epoch is not None and self.owner_instance_id is None:
            raise MaterialExecutionSubmissionSchemaError(
                "owner instance and owner epoch must be present together"
            )
        if self.owner_epoch is not None and (
            type(self.owner_epoch) is not int or self.owner_epoch <= 0
        ):
            raise MaterialExecutionSubmissionSchemaError("submission owner epoch is invalid")
        if self.attempt_id is not None:
            _validate_uuid(self.attempt_id, "attempt_id")
        if self.response_json is not None:
            cloned = _clone_json(dict(self.response_json), label="response_json")
            if not isinstance(cloned, dict):
                raise MaterialExecutionSubmissionSchemaError("response_json is not an object")
            object.__setattr__(self, "response_json", cloned)
        if self.error is not None and type(self.error) is not MaterialExecutionSubmissionErrorMetadata:
            raise TypeError("error must be MaterialExecutionSubmissionErrorMetadata")
        if self.recovery_ticket is not None:
            _assert_ticket_identity(
                self.context,
                self.recovery_ticket,
                expected_attempt_id=self.attempt_id,
            )
            if self.attempt_id is None:
                raise MaterialExecutionSubmissionSchemaError(
                    "recovery ticket row has no matching attempt identity"
                )
        if self.state in {"blocked", "reconciling"} and self.recovery_ticket is None:
            raise MaterialExecutionSubmissionSchemaError(
                "blocked/reconciling submission must retain a recovery ticket"
            )


def _assert_ticket_identity(
    context: MaterialExecutionSubmissionContext,
    ticket: MaterialRecoveryTicket,
    *,
    expected_attempt_id: str | None = None,
) -> None:
    if type(ticket) is not MaterialRecoveryTicket:
        raise TypeError("recovery_ticket must be MaterialRecoveryTicket")
    command = context.command
    if (
        ticket.lane_id != context.lane_key.lane_id
        or ticket.pipeline_id != command.pipeline_id
        or ticket.material_step_id != command.material_step_id
        or ticket.material_task_id != command.material_task_id
        or ticket.execution_id != command.execution_id
        or ticket.operation != command.operation
        or (
            expected_attempt_id is not None
            and ticket.attempt_id != expected_attempt_id
        )
    ):
        raise MaterialExecutionSubmissionIdentityConflict(
            "recovery ticket does not match submission identity"
        )


def _context_json(context: MaterialExecutionSubmissionContext) -> dict[str, Any]:
    key = context.lane_key
    command = context.command
    return {
        "lane_id": key.lane_id,
        "database_path": str(key.database_path),
        "pipeline_id": key.pipeline_id,
        "material_step_id": key.material_step_id,
        "command": {
            "pipeline_id": command.pipeline_id,
            "material_step_id": command.material_step_id,
            "material_task_id": command.material_task_id,
            "execution_id": command.execution_id,
            "operation": command.operation,
            "startup_timeout_seconds": command.startup_timeout_seconds,
            "operation_timeout_seconds": command.operation_timeout_seconds,
        },
        "source_chain": list(context.source_chain),
        "snapshot_sha256": context.snapshot_sha256,
    }


def _context_from_row(row: sqlite3.Row) -> MaterialExecutionSubmissionContext:
    try:
        command_payload = _decode_json(row["command_json"], label="command_json")
        chain_payload = _decode_json(row["source_chain_json"], label="source_chain_json")
        if not isinstance(command_payload, dict) or not isinstance(chain_payload, list):
            raise MaterialExecutionSubmissionSchemaError("stored command metadata is invalid")
        command = MaterialExecutionCommand(
            pipeline_id=command_payload["pipeline_id"],
            material_step_id=command_payload["material_step_id"],
            material_task_id=command_payload["material_task_id"],
            execution_id=command_payload["execution_id"],
            operation=command_payload["operation"],
            startup_timeout_seconds=command_payload["startup_timeout_seconds"],
            operation_timeout_seconds=command_payload["operation_timeout_seconds"],
        )
        key = MaterialExecutionLaneKey(
            database_path=Path(row["database_path"]),
            pipeline_id=row["pipeline_id"],
            material_step_id=row["material_step_id"],
        )
        if row["lane_id"] != key.lane_id:
            raise MaterialExecutionSubmissionSchemaError("stored lane identity changed")
        if (
            command.pipeline_id != key.pipeline_id
            or command.material_step_id != key.material_step_id
            or command_payload.get("pipeline_id") != row["pipeline_id"]
            or command_payload.get("material_step_id") != row["material_step_id"]
            or command_payload.get("material_task_id") != row["material_task_id"]
            or command_payload.get("execution_id") != row["execution_id"]
            or command_payload.get("operation") != row["operation"]
            or command_payload.get("startup_timeout_seconds")
            != row["startup_timeout_seconds"]
            or command_payload.get("operation_timeout_seconds")
            != row["operation_timeout_seconds"]
        ):
            raise MaterialExecutionSubmissionSchemaError("stored command identity changed")
        try:
            _validate_sha256(row["snapshot_sha256"], "snapshot_sha256")
        except ValueError as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "stored snapshot hash changed"
            ) from exc
        return MaterialExecutionSubmissionContext(
            lane_key=key,
            command=command,
            source_chain=tuple(chain_payload),
            snapshot_sha256=row["snapshot_sha256"],
        )
    except MaterialExecutionSubmissionSchemaError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise MaterialExecutionSubmissionSchemaError(
            "stored submission context is invalid"
        ) from exc


def _ticket_json(ticket: MaterialRecoveryTicket | None) -> str | None:
    if ticket is None:
        return None
    return _encode_json(asdict(ticket), label="recovery_ticket")


def _ticket_from_json(value: Any) -> MaterialRecoveryTicket | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise MaterialExecutionSubmissionSchemaError("recovery ticket is not an object")
    try:
        return MaterialRecoveryTicket(**dict(value))
    except (TypeError, ValueError) as exc:
        raise MaterialExecutionSubmissionSchemaError("recovery ticket is invalid") from exc


def _response_from_json(value: Any) -> MaterialExecutionResponse:
    """Re-validate every field, including nested completion evidence."""

    if not isinstance(value, Mapping):
        raise MaterialExecutionSubmissionSchemaError("response_json is not an object")
    required = {
        "pipeline_id",
        "material_step_id",
        "material_task_id",
        "execution_id",
        "attempt_id",
        "operation",
        "status",
        "message",
        "executed_task",
        "tasks",
        "completion",
        "recovery_ticket",
    }
    if set(value) != required:
        raise MaterialExecutionSubmissionSchemaError(
            "response_json fields do not match the typed response DTO"
        )
    tasks = value["tasks"]
    if not isinstance(tasks, list):
        raise MaterialExecutionSubmissionSchemaError("response_json tasks is not a list")
    completion_payload = value["completion"]
    if completion_payload is None:
        completion = None
    elif isinstance(completion_payload, Mapping):
        try:
            completion = MaterialCompletionReceipt(**dict(completion_payload))
        except (TypeError, ValueError) as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "response_json completion evidence is invalid"
            ) from exc
    else:
        raise MaterialExecutionSubmissionSchemaError(
            "response_json completion is not an object"
        )
    ticket = _ticket_from_json(value["recovery_ticket"])
    try:
        response = MaterialExecutionResponse(
            pipeline_id=value["pipeline_id"],
            material_step_id=value["material_step_id"],
            material_task_id=value["material_task_id"],
            execution_id=value["execution_id"],
            attempt_id=value["attempt_id"],
            operation=value["operation"],
            status=value["status"],
            message=value["message"],
            executed_task=value["executed_task"],
            tasks=tuple(tasks),
            completion=completion,
            recovery_ticket=ticket,
        )
        if serialize_material_execution_response(response) != dict(value):
            raise MaterialExecutionSubmissionSchemaError(
                "response_json does not round-trip through the typed serializer"
            )
        return response
    except MaterialExecutionSubmissionError:
        raise
    except (TypeError, ValueError) as exc:
        raise MaterialExecutionSubmissionSchemaError(
            "response_json typed fields are invalid"
        ) from exc


class MaterialExecutionSubmissionStore:
    """Thread-safe SQLite sidecar for immutable submission identity and state."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = _normalise_sidecar_path(database_path)
        self._lock = threading.RLock()
        self._runtime_path_identity: tuple[
            tuple[int, int, int, int], tuple[int, int, int, int]
        ] | None = None
        self._active_owner_capability: MaterialExecutionOwnerLeaseCapability | None = None
        self._prepare_path()
        # Pin the file identity before SQLite opens it.  A fresh sidecar is
        # created with O_EXCL so a concurrent process cannot win the
        # create/pin gap and substitute a different file before schema
        # initialisation.  Existing or raced files are treated as
        # pre-existing and fail closed unless their v2 schema is recognised.
        self._preexisting_file = self.database_path.exists()
        if self._preexisting_file:
            self._runtime_path_identity = _runtime_identity(self.database_path)
        else:
            try:
                descriptor = os.open(
                    str(self.database_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                self._preexisting_file = True
                self._runtime_path_identity = _runtime_identity(self.database_path)
            except OSError as exc:
                raise MaterialExecutionSubmissionPathError(
                    "cannot atomically create submission sidecar"
                ) from exc
            else:
                try:
                    os.close(descriptor)
                except OSError as exc:
                    raise MaterialExecutionSubmissionPathError(
                        "cannot close newly created submission sidecar"
                    ) from exc
                self._runtime_path_identity = _runtime_identity(self.database_path)
        self._initialise_schema()
        if self._runtime_path_identity is None:
            self._runtime_path_identity = _runtime_identity(self.database_path)

    @property
    def path(self) -> Path:
        """Compatibility/readability alias for the sidecar path."""

        return self.database_path

    def _prepare_path(self) -> None:
        parent = self.database_path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MaterialExecutionSubmissionPathError(
                f"cannot create D: submission sidecar parent: {parent}"
            ) from exc
        if _is_reparse_point(parent):
            raise MaterialExecutionSubmissionPathError(
                "submission sidecar parent is a reparse point"
            )
        if self.database_path.exists():
            if _is_reparse_point(self.database_path):
                raise MaterialExecutionSubmissionPathError(
                    "submission sidecar is a reparse point"
                )
            if not self.database_path.is_file():
                raise MaterialExecutionSubmissionPathError(
                    "submission sidecar path is not a file"
                )
            try:
                if self.database_path.stat(follow_symlinks=False).st_nlink != 1:
                    raise MaterialExecutionSubmissionPathError(
                        "submission sidecar must have one hard link"
                    )
            except OSError as exc:
                raise MaterialExecutionSubmissionPathError(
                    "cannot inspect submission sidecar"
                ) from exc

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                self._assert_runtime_path()
                connection = sqlite3.connect(
                    str(self.database_path),
                    timeout=30.0,
                    isolation_level=None,
                    check_same_thread=False,
                )
                # Re-check after opening: a path replacement between the
                # lexical preflight and sqlite's open must fail closed.
                self._assert_runtime_path()
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA busy_timeout=30000")
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection
                # Fence the path immediately before commit.  A replaced file
                # or parent must never receive a transaction intended for the
                # original sidecar.
                self._assert_runtime_path()
                connection.execute("COMMIT")
            except BaseException:
                if connection is not None:
                    try:
                        connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
            finally:
                if connection is not None:
                    connection.close()

    def _assert_runtime_path(self) -> None:
        current = _normalise_sidecar_path(self.database_path)
        if current != self.database_path:
            raise MaterialExecutionSubmissionPathError(
                "submission sidecar path changed during an operation"
            )
        self._prepare_path()
        if self._runtime_path_identity is not None:
            current_identity = _runtime_identity(self.database_path)
            if current_identity != self._runtime_path_identity:
                raise MaterialExecutionSubmissionPathError(
                    "submission sidecar identity changed during an operation"
                )

    def _initialise_schema(self) -> None:
        try:
            with self._transaction(write=True) as connection:
                existing_tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                table_name = "material_execution_submissions"
                lease_table_name = "material_execution_submission_owner_leases"
                if self._preexisting_file and (
                    self.database_path.stat(follow_symlinks=False).st_size == 0
                    or table_name not in existing_tables
                    or lease_table_name not in existing_tables
                ):
                    raise MaterialExecutionSubmissionSchemaError(
                        "existing submission sidecar has no recognized schema"
                    )
                if table_name not in existing_tables:
                    connection.execute(
                        f"""
                        CREATE TABLE {lease_table_name} (
                        schema_version INTEGER NOT NULL,
                        owner_epoch INTEGER PRIMARY KEY,
                        owner_instance_id TEXT NOT NULL UNIQUE,
                        mutex_name TEXT NOT NULL,
                        acquired_at TEXT NOT NULL,
                        ended_at TEXT,
                        end_reason TEXT,
                        acquisition_kind TEXT NOT NULL,
                        process_id INTEGER NOT NULL,
                        holder_thread_id INTEGER NOT NULL,
                        active INTEGER NOT NULL CHECK (active IN (0, 1)),
                        UNIQUE (owner_epoch, owner_instance_id)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE material_execution_submissions (
                        schema_version INTEGER NOT NULL,
                        submission_id TEXT PRIMARY KEY,
                        lane_id TEXT NOT NULL,
                        database_path TEXT NOT NULL,
                        pipeline_id INTEGER NOT NULL,
                        material_step_id INTEGER NOT NULL,
                        material_task_id TEXT NOT NULL,
                        execution_id TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        startup_timeout_seconds REAL NOT NULL,
                        operation_timeout_seconds REAL NOT NULL,
                        command_json TEXT NOT NULL,
                        source_chain_json TEXT NOT NULL,
                        snapshot_sha256 TEXT NOT NULL,
                        state TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        owner_instance_id TEXT,
                        owner_epoch INTEGER,
                        attempt_id TEXT,
                        response_json TEXT,
                        error_json TEXT,
                        recovery_ticket_json TEXT,
                        FOREIGN KEY (owner_epoch, owner_instance_id)
                            REFERENCES material_execution_submission_owner_leases(
                                owner_epoch, owner_instance_id
                            )
                        )
                        """
                    )
                if not self._preexisting_file:
                    # ``user_version`` is a database-level marker, so an empty
                    # but structurally squatted lease table cannot masquerade as
                    # a freshly-created sidecar.  Existing files must already
                    # carry the current marker; there is no implicit migration.
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                try:
                    database_version = int(
                        connection.execute("PRAGMA user_version").fetchone()[0]
                    )
                except (TypeError, ValueError) as exc:
                    raise MaterialExecutionSubmissionSchemaError(
                        "submission sidecar has an invalid database schema version"
                    ) from exc
                if database_version != SCHEMA_VERSION:
                    raise MaterialExecutionSubmissionSchemaError(
                        "submission sidecar has an unsupported database schema version"
                    )
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "idx_material_execution_submission_owner_active "
                    "ON material_execution_submission_owner_leases(active) WHERE active = 1"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_material_execution_submissions_state "
                    "ON material_execution_submissions(state, owner_instance_id, owner_epoch)"
                )
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(material_execution_submissions)"
                    ).fetchall()
                }
                required = {
                    "schema_version",
                    "submission_id",
                    "lane_id",
                    "database_path",
                    "pipeline_id",
                    "material_step_id",
                    "material_task_id",
                    "execution_id",
                    "operation",
                    "startup_timeout_seconds",
                    "operation_timeout_seconds",
                    "command_json",
                    "source_chain_json",
                    "snapshot_sha256",
                    "state",
                    "revision",
                    "created_at",
                    "updated_at",
                    "owner_instance_id",
                    "owner_epoch",
                    "attempt_id",
                    "response_json",
                    "error_json",
                    "recovery_ticket_json",
                }
                if columns != required:
                    raise MaterialExecutionSubmissionSchemaError(
                        "submission sidecar schema is incomplete or unknown"
                    )
                lease_columns = {
                    row[1]
                    for row in connection.execute(
                        f"PRAGMA table_info({lease_table_name})"
                    ).fetchall()
                }
                required_lease_columns = {
                    "schema_version",
                    "owner_epoch",
                    "owner_instance_id",
                    "mutex_name",
                    "acquired_at",
                    "ended_at",
                    "end_reason",
                    "acquisition_kind",
                    "process_id",
                    "holder_thread_id",
                    "active",
                }
                if lease_columns != required_lease_columns:
                    raise MaterialExecutionSubmissionSchemaError(
                        "owner lease schema is incomplete or unknown"
                    )
                for current_table in (table_name, lease_table_name):
                    try:
                        versions = {
                            int(row[0])
                            for row in connection.execute(
                                f"SELECT DISTINCT schema_version FROM {current_table}"
                            ).fetchall()
                        }
                    except (TypeError, ValueError) as exc:
                        raise MaterialExecutionSubmissionSchemaError(
                            f"{current_table} contains an invalid schema version"
                        ) from exc
                    if any(version != SCHEMA_VERSION for version in versions):
                        raise MaterialExecutionSubmissionSchemaError(
                            f"{current_table} contains an unsupported schema version"
                        )
                self._validate_schema_constraints(connection)
        except MaterialExecutionSubmissionSchemaError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "cannot initialise submission sidecar schema"
            ) from exc

    @staticmethod
    def _validate_schema_constraints(connection: sqlite3.Connection) -> None:
        """Fail closed when a same-shaped sidecar has weaker DDL semantics.

        Column names alone are not enough: a sidecar can be created with the
        right names while omitting the composite foreign key or the partial
        unique active-owner index.  SQLite's PRAGMA introspection gives us a
        stable, low-level contract without relying on vendor-specific catalog
        tables.
        """

        lease_table = "material_execution_submission_owner_leases"
        submission_table = "material_execution_submissions"

        def table_info(table_name: str) -> dict[str, tuple[Any, ...]]:
            rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            return {str(row[1]): tuple(row) for row in rows}

        lease_info = table_info(lease_table)
        submission_info = table_info(submission_table)
        if lease_info.get("owner_epoch", (None,) * 6)[5] != 1:
            raise MaterialExecutionSubmissionSchemaError(
                "owner lease epoch must be the primary key"
            )
        for name in (
            "schema_version",
            "owner_instance_id",
            "mutex_name",
            "acquired_at",
            "acquisition_kind",
            "process_id",
            "holder_thread_id",
            "active",
        ):
            if lease_info.get(name, (None, None, None, 0))[3] != 1:
                raise MaterialExecutionSubmissionSchemaError(
                    f"owner lease column {name} must be NOT NULL"
                )
        if submission_info.get("submission_id", (None,) * 6)[5] != 1:
            raise MaterialExecutionSubmissionSchemaError(
                "submission ID must be the submission primary key"
            )
        for name in (
            "schema_version",
            "lane_id",
            "database_path",
            "pipeline_id",
            "material_step_id",
            "material_task_id",
            "execution_id",
            "operation",
            "startup_timeout_seconds",
            "operation_timeout_seconds",
            "command_json",
            "source_chain_json",
            "snapshot_sha256",
            "state",
            "revision",
            "created_at",
            "updated_at",
        ):
            if submission_info.get(name, (None, None, None, 0))[3] != 1:
                raise MaterialExecutionSubmissionSchemaError(
                    f"submission column {name} must be NOT NULL"
                )

        index_rows = {
            str(row[1]): tuple(row)
            for row in connection.execute(f"PRAGMA index_list({lease_table})").fetchall()
        }
        active_index = index_rows.get("idx_material_execution_submission_owner_active")
        if active_index is None or active_index[2] != 1 or active_index[4] != 1:
            raise MaterialExecutionSubmissionSchemaError(
                "owner lease active index is not a partial unique index"
            )
        active_columns = [
            str(row[2])
            for row in connection.execute(
                "PRAGMA index_info(idx_material_execution_submission_owner_active)"
            ).fetchall()
        ]
        if active_columns != ["active"]:
            raise MaterialExecutionSubmissionSchemaError(
                "owner lease active index covers an unexpected column"
            )
        index_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("idx_material_execution_submission_owner_active",),
        ).fetchone()
        index_sql = re.sub(r"\s+", " ", str(index_sql_row[0]).upper()) if index_sql_row else ""
        index_sql = re.sub(r"[\[\]`\"]", "", index_sql).strip()
        expected_index_sql = (
            "CREATE UNIQUE INDEX IDX_MATERIAL_EXECUTION_SUBMISSION_OWNER_ACTIVE "
            "ON MATERIAL_EXECUTION_SUBMISSION_OWNER_LEASES(ACTIVE) WHERE ACTIVE = 1"
        )
        if index_sql != expected_index_sql:
            raise MaterialExecutionSubmissionSchemaError(
                "owner lease active index has an unexpected predicate"
            )
        unique_lease_column_sets: set[tuple[str, ...]] = set()
        for index_name, index_row in index_rows.items():
            if index_name == "idx_material_execution_submission_owner_active" or index_row[2] != 1:
                continue
            unique_lease_column_sets.add(
                tuple(
                    str(row[2])
                    for row in connection.execute(
                        f"PRAGMA index_info({index_name})"
                    ).fetchall()
                )
            )
        if {
            ("owner_instance_id",),
            ("owner_epoch", "owner_instance_id"),
        } - unique_lease_column_sets:
            raise MaterialExecutionSubmissionSchemaError(
                "owner lease uniqueness constraints are incomplete"
            )
        state_index_rows = {
            str(row[1]): tuple(row)
            for row in connection.execute(
                f"PRAGMA index_list({submission_table})"
            ).fetchall()
        }
        state_index = state_index_rows.get("idx_material_execution_submissions_state")
        if state_index is None or state_index[2] != 0:
            raise MaterialExecutionSubmissionSchemaError(
                "submission state index is missing or unexpectedly unique"
            )
        state_columns = [
            str(row[2])
            for row in connection.execute(
                "PRAGMA index_info(idx_material_execution_submissions_state)"
            ).fetchall()
        ]
        if state_columns != ["state", "owner_instance_id", "owner_epoch"]:
            raise MaterialExecutionSubmissionSchemaError(
                "submission state index covers unexpected columns"
            )

        foreign_key_rows = connection.execute(
            f"PRAGMA foreign_key_list({submission_table})"
        ).fetchall()
        foreign_key_pairs = {
            (int(row[0]), str(row[2]), str(row[3]), str(row[4]))
            for row in foreign_key_rows
        }
        grouped: dict[tuple[int, str], set[tuple[str, str]]] = {}
        for constraint_id, target, source, destination in foreign_key_pairs:
            grouped.setdefault((constraint_id, target), set()).add(
                (source, destination)
            )
        if {
            ("owner_epoch", "owner_epoch"),
            ("owner_instance_id", "owner_instance_id"),
        } not in [
            pairs for (constraint_id, target), pairs in grouped.items() if target == lease_table
        ]:
            raise MaterialExecutionSubmissionSchemaError(
                "submission owner identity foreign key is missing or incomplete"
            )

        table_sql_rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name IN (?, ?)",
            (lease_table, submission_table),
        ).fetchall()
        table_sql = {str(row[0]): re.sub(r"\s+", " ", str(row[1]).upper()) for row in table_sql_rows}
        lease_sql = table_sql.get(lease_table, "")
        if "CHECK (ACTIVE IN (0, 1))" not in lease_sql:
            raise MaterialExecutionSubmissionSchemaError(
                "owner lease active check constraint is missing"
            )

    def _activate_owner_lease(
        self,
        *,
        owner_instance_id: str,
        mutex_name: str,
        was_abandoned: bool,
        process_id: int,
        holder_thread_id: int,
    ) -> MaterialExecutionOwnerLeaseActivation:
        """Allocate a fencing epoch and fence prior in-flight work atomically.

        This is an internal seam.  The owner-lease holder thread calls it only
        after asserting ownership of the stable, path-scoped Windows mutex.  It
        records proof metadata but cannot manufacture liveness evidence from a
        UUID, PID or timestamp on its own.
        """

        token = MaterialExecutionOwnerToken(
            owner_instance_id=owner_instance_id,
            owner_epoch=1,
        )
        if type(mutex_name) is not str or not mutex_name or len(mutex_name) > 260:
            raise ValueError("mutex_name must be a bounded non-empty string")
        expected_mutex_name = _expected_owner_mutex_name(self.database_path)
        if expected_mutex_name is not None and mutex_name != expected_mutex_name:
            raise MaterialExecutionSubmissionIdentityConflict(
                "owner lease mutex is not bound to this submission sidecar"
            )
        if type(was_abandoned) is not bool:
            raise TypeError("was_abandoned must be a boolean")
        if type(process_id) is not int or process_id <= 0:
            raise ValueError("process_id must be a positive integer")
        if type(holder_thread_id) is not int or holder_thread_id <= 0:
            raise ValueError("holder_thread_id must be a positive integer")

        fenced: list[MaterialExecutionSubmissionRecord] = []
        now = _timestamp()
        try:
            with self._transaction(write=True) as connection:
                previous = connection.execute(
                    """
                    SELECT * FROM material_execution_submission_owner_leases
                    ORDER BY owner_epoch DESC LIMIT 1
                    """
                ).fetchone()
                previous_epoch = 0 if previous is None else int(previous["owner_epoch"])
                if previous is not None and previous["owner_instance_id"] == owner_instance_id:
                    raise MaterialExecutionSubmissionIdentityConflict(
                        "owner instance ID was already used by a prior lease"
                    )
                next_epoch = previous_epoch + 1
                previous_was_active = previous is not None and int(previous["active"]) == 1
                if previous_was_active:
                    connection.execute(
                        """
                        UPDATE material_execution_submission_owner_leases
                        SET active = 0, ended_at = ?, end_reason = 'superseded'
                        WHERE owner_epoch = ? AND active = 1
                        """,
                        (now, previous_epoch),
                    )
                if was_abandoned:
                    acquisition_kind = "abandoned_mutex"
                elif previous is None:
                    acquisition_kind = "initial"
                elif previous_was_active:
                    acquisition_kind = "exclusive_reacquire"
                else:
                    acquisition_kind = "clean_handoff"
                connection.execute(
                    """
                    INSERT INTO material_execution_submission_owner_leases (
                        schema_version, owner_epoch, owner_instance_id, mutex_name,
                        acquired_at, ended_at, end_reason, acquisition_kind,
                        process_id, holder_thread_id, active
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, 1)
                    """,
                    (
                        SCHEMA_VERSION,
                        next_epoch,
                        owner_instance_id,
                        mutex_name,
                        now,
                        acquisition_kind,
                        process_id,
                        holder_thread_id,
                    ),
                )
                token = MaterialExecutionOwnerToken(
                    owner_instance_id=owner_instance_id,
                    owner_epoch=next_epoch,
                )
                capability = MaterialExecutionOwnerLeaseCapability(
                    owner_instance_id=owner_instance_id,
                    owner_epoch=next_epoch,
                    _nonce=object(),
                )
                rows = connection.execute(
                    """
                    SELECT * FROM material_execution_submissions
                    WHERE state IN ('dispatching', 'running', 'blocked', 'reconciling')
                    ORDER BY created_at, submission_id
                    """
                ).fetchall()
                error = MaterialExecutionSubmissionErrorMetadata(
                    code="owner_lease_superseded",
                    detail=(
                        "submission belonged to a previous exclusive owner epoch; "
                        "explicit reconciliation is required"
                    ),
                    retryable=False,
                    recovery_required=True,
                    data={
                        "successor_owner_epoch": next_epoch,
                        "acquisition_kind": acquisition_kind,
                    },
                )
                for row in rows:
                    revision = int(row["revision"]) + 1
                    result = connection.execute(
                        """
                        UPDATE material_execution_submissions
                        SET state = 'unknown', revision = ?, updated_at = ?,
                            response_json = NULL, error_json = ?
                        WHERE submission_id = ? AND revision = ?
                        """,
                        (
                            revision,
                            now,
                            _encode_json(error.to_json(), label="error_json"),
                            row["submission_id"],
                            row["revision"],
                        ),
                    )
                    if result.rowcount != 1:
                        raise MaterialExecutionSubmissionStateConflict(
                            "owner lease fence transition was lost"
                        )
                    changed = connection.execute(
                        "SELECT * FROM material_execution_submissions WHERE submission_id = ?",
                        (row["submission_id"],),
                    ).fetchone()
                    if changed is None:
                        raise MaterialExecutionSubmissionSchemaError(
                            "fenced submission disappeared before commit"
                        )
                    fenced.append(self._row_to_record(changed))
                lease_row = connection.execute(
                    """
                    SELECT * FROM material_execution_submission_owner_leases
                    WHERE owner_epoch = ?
                    """,
                    (next_epoch,),
                ).fetchone()
                if lease_row is None:
                    raise MaterialExecutionSubmissionSchemaError(
                        "owner lease disappeared before commit"
                    )
                lease = self._lease_row_to_record(lease_row)
                activation = MaterialExecutionOwnerLeaseActivation(
                    token=token,
                    record=lease,
                    acquisition_kind=acquisition_kind,
                    was_abandoned=was_abandoned,
                    fenced_submissions=tuple(fenced),
                    capability=capability,
                )
            # Update the in-memory capability only after the transaction
            # context has committed successfully.  A path/commit failure must
            # not strand the still-held previous lease.
            with self._lock:
                current = self._active_owner_capability
                if current is None or current.owner_epoch < capability.owner_epoch:
                    self._active_owner_capability = capability
            return activation
        except MaterialExecutionSubmissionError:
            raise
        except sqlite3.IntegrityError as exc:
            raise MaterialExecutionSubmissionIdentityConflict(
                "owner lease activation conflicted with another owner"
            ) from exc
        except sqlite3.Error as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "cannot activate owner lease"
            ) from exc

    def assert_owner_lease(
        self,
        token: MaterialExecutionOwnerToken,
        *,
        capability: MaterialExecutionOwnerLeaseCapability | None = None,
    ) -> MaterialExecutionOwnerLeaseRecord:
        if type(token) is not MaterialExecutionOwnerToken:
            raise TypeError("token must be MaterialExecutionOwnerToken")
        self._require_owner_capability(capability, token)
        try:
            with self._transaction(write=False) as connection:
                row = connection.execute(
                    """
                    SELECT * FROM material_execution_submission_owner_leases
                    WHERE owner_epoch = ? AND owner_instance_id = ? AND active = 1
                    """,
                    (token.owner_epoch, token.owner_instance_id),
                ).fetchone()
                if row is None:
                    raise MaterialExecutionSubmissionIdentityConflict(
                        "owner lease is not the active fencing epoch"
                    )
                return self._lease_row_to_record(row)
        except MaterialExecutionSubmissionError:
            raise
        except sqlite3.Error as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "cannot read active owner lease"
            ) from exc

    def end_owner_lease(
        self,
        token: MaterialExecutionOwnerToken,
        *,
        reason: str = "clean_shutdown",
        capability: MaterialExecutionOwnerLeaseCapability | None = None,
    ) -> MaterialExecutionOwnerLeaseRecord:
        if type(token) is not MaterialExecutionOwnerToken:
            raise TypeError("token must be MaterialExecutionOwnerToken")
        self._require_owner_capability(capability, token)
        if type(reason) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
            raise ValueError("owner lease end reason is invalid")
        now = _timestamp()
        try:
            with self._transaction(write=True) as connection:
                row = connection.execute(
                    """
                    SELECT * FROM material_execution_submission_owner_leases
                    WHERE owner_epoch = ? AND owner_instance_id = ?
                    """,
                    (token.owner_epoch, token.owner_instance_id),
                ).fetchone()
                if row is None:
                    raise MaterialExecutionSubmissionIdentityConflict(
                        "owner lease is not the active fencing epoch"
                    )
                if int(row["active"]) == 0:
                    record = self._lease_row_to_record(row)
                    if record.end_reason != reason:
                        raise MaterialExecutionSubmissionIdentityConflict(
                            "owner lease was already closed for another reason"
                        )
                    return record
                busy = connection.execute(
                    """
                    SELECT 1 FROM material_execution_submissions
                    WHERE owner_epoch = ?
                      AND state IN ('dispatching', 'running', 'blocked', 'reconciling')
                    LIMIT 1
                    """,
                    (token.owner_epoch,),
                ).fetchone()
                if busy is not None:
                    raise MaterialExecutionSubmissionStateConflict(
                        "cannot cleanly close an owner lease with in-flight submissions"
                    )
                result = connection.execute(
                    """
                    UPDATE material_execution_submission_owner_leases
                    SET active = 0, ended_at = ?, end_reason = ?
                    WHERE owner_epoch = ? AND owner_instance_id = ? AND active = 1
                    """,
                    (now, reason, token.owner_epoch, token.owner_instance_id),
                )
                if result.rowcount != 1:
                    raise MaterialExecutionSubmissionStateConflict(
                        "owner lease close was lost"
                    )
                changed = connection.execute(
                    """
                    SELECT * FROM material_execution_submission_owner_leases
                    WHERE owner_epoch = ?
                    """,
                    (token.owner_epoch,),
                ).fetchone()
                if changed is None:
                    raise MaterialExecutionSubmissionSchemaError(
                        "owner lease disappeared after close"
                    )
                record = self._lease_row_to_record(changed)
            return record
        except MaterialExecutionSubmissionError:
            raise
        except sqlite3.Error as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "cannot close owner lease"
            ) from exc

    def release_owner_capability(
        self,
        token: MaterialExecutionOwnerToken,
        *,
        capability: MaterialExecutionOwnerLeaseCapability,
    ) -> None:
        """Forget an inactive capability after its OS holder thread stopped."""

        self._require_owner_capability(capability, token)
        with self._lock:
            if self._active_owner_capability is capability:
                self._active_owner_capability = None

    def _require_owner_capability(
        self,
        capability: MaterialExecutionOwnerLeaseCapability | None,
        token: MaterialExecutionOwnerToken,
    ) -> None:
        with self._lock:
            if (
                type(capability) is not MaterialExecutionOwnerLeaseCapability
                or capability is not self._active_owner_capability
                or capability.owner_instance_id != token.owner_instance_id
                or capability.owner_epoch != token.owner_epoch
            ):
                raise MaterialExecutionSubmissionIdentityConflict(
                    "owner lease capability is not valid for this store and epoch"
                )

    def _lease_row_to_record(
        self, row: sqlite3.Row
    ) -> MaterialExecutionOwnerLeaseRecord:
        try:
            active_value = row["active"]
            if type(active_value) is not int or active_value not in {0, 1}:
                raise MaterialExecutionSubmissionSchemaError(
                    "owner lease active marker is invalid"
                )
            record = MaterialExecutionOwnerLeaseRecord(
                schema_version=int(row["schema_version"]),
                owner_epoch=int(row["owner_epoch"]),
                owner_instance_id=row["owner_instance_id"],
                mutex_name=row["mutex_name"],
                acquired_at=row["acquired_at"],
                ended_at=row["ended_at"],
                end_reason=row["end_reason"],
                acquisition_kind=row["acquisition_kind"],
                process_id=int(row["process_id"]),
                holder_thread_id=int(row["holder_thread_id"]),
            )
            if (record.ended_at is None) != (active_value == 1):
                raise MaterialExecutionSubmissionSchemaError(
                    "owner lease active marker does not match end timestamp"
                )
            expected_mutex_name = _expected_owner_mutex_name(self.database_path)
            if expected_mutex_name is not None and record.mutex_name != expected_mutex_name:
                raise MaterialExecutionSubmissionSchemaError(
                    "owner lease mutex is not bound to this submission sidecar"
                )
            return record
        except MaterialExecutionSubmissionError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "stored owner lease row is invalid"
            ) from exc

    def create(
        self,
        submission_id: str,
        context: MaterialExecutionSubmissionContext,
    ) -> MaterialExecutionSubmissionRecord:
        _validate_uuid(submission_id, "submission_id")
        if type(context) is not MaterialExecutionSubmissionContext:
            raise TypeError("context must be MaterialExecutionSubmissionContext")
        payload = _context_json(context)
        command = context.command
        now = _timestamp()
        values = (
            SCHEMA_VERSION,
            submission_id,
            context.lane_key.lane_id,
            str(context.lane_key.database_path),
            command.pipeline_id,
            command.material_step_id,
            command.material_task_id,
            command.execution_id,
            command.operation,
            command.startup_timeout_seconds,
            command.operation_timeout_seconds,
            _encode_json(payload["command"], label="command_json"),
            _encode_json(payload["source_chain"], label="source_chain_json"),
            context.snapshot_sha256,
            "accepted",
            0,
            now,
            now,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        try:
            with self._transaction(write=True) as connection:
                try:
                    connection.execute(
                        """
                        INSERT INTO material_execution_submissions (
                            schema_version, submission_id, lane_id, database_path,
                            pipeline_id, material_step_id, material_task_id,
                            execution_id, operation, startup_timeout_seconds,
                            operation_timeout_seconds, command_json, source_chain_json,
                            snapshot_sha256, state, revision, created_at, updated_at,
                            owner_instance_id, owner_epoch, attempt_id, response_json, error_json,
                            recovery_ticket_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                except sqlite3.IntegrityError:
                    existing = connection.execute(
                        "SELECT * FROM material_execution_submissions WHERE submission_id = ?",
                        (submission_id,),
                    ).fetchone()
                    if existing is None:
                        raise
                    existing_record = self._row_to_record(existing)
                    if existing_record.context != context:
                        raise MaterialExecutionSubmissionIdentityConflict(
                            "submission ID already belongs to another context"
                        )
                    return existing_record
                row = connection.execute(
                    "SELECT * FROM material_execution_submissions WHERE submission_id = ?",
                    (submission_id,),
                ).fetchone()
                if row is None:
                    raise MaterialExecutionSubmissionSchemaError(
                        "created submission disappeared before commit"
                    )
                return self._row_to_record(row)
        except MaterialExecutionSubmissionError:
            raise
        except sqlite3.Error as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "cannot create durable submission"
            ) from exc

    def get(self, submission_id: str) -> MaterialExecutionSubmissionRecord | None:
        _validate_uuid(submission_id, "submission_id")
        try:
            with self._transaction(write=False) as connection:
                row = connection.execute(
                    "SELECT * FROM material_execution_submissions WHERE submission_id = ?",
                    (submission_id,),
                ).fetchone()
                return None if row is None else self._row_to_record(row)
        except MaterialExecutionSubmissionError:
            raise
        except sqlite3.Error as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "cannot read durable submission"
            ) from exc

    def list_inflight(self) -> tuple[MaterialExecutionSubmissionRecord, ...]:
        try:
            with self._transaction(write=False) as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM material_execution_submissions
                    WHERE state IN ('accepted', 'dispatching', 'running', 'blocked', 'reconciling')
                    ORDER BY created_at, submission_id
                    """
                ).fetchall()
                return tuple(self._row_to_record(row) for row in rows)
        except MaterialExecutionSubmissionError:
            raise
        except sqlite3.Error as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "cannot list durable submissions"
            ) from exc

    def list_unresolved(self) -> tuple[MaterialExecutionSubmissionRecord, ...]:
        """Return records requiring an explicit reconcile/close action.

        ``unknown`` is deliberately absent from :meth:`list_inflight` so a
        restart cannot be mistaken for work that is safe to enqueue again.
        Callers that implement the future reconcile gate use this explicit
        view instead.
        """

        try:
            with self._transaction(write=False) as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM material_execution_submissions
                    WHERE state IN ('unknown', 'blocked', 'reconciling')
                    ORDER BY created_at, submission_id
                    """
                ).fetchall()
                return tuple(self._row_to_record(row) for row in rows)
        except MaterialExecutionSubmissionError:
            raise
        except sqlite3.Error as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "cannot list unresolved submissions"
            ) from exc

    def _mark_inflight_restart_unknown_legacy(
        self,
        owner_instance_id: str,
    ) -> tuple[MaterialExecutionSubmissionRecord, ...]:
        """Legacy diagnostic transition; never use as process-death proof.

        The production coordinator no longer exposes or calls this UUID-only
        sweep. It remains private for migration-era diagnostics and tests that
        exercise the pre-lease state transition. A UUID, PID or stale
        timestamp cannot establish that an owner process died.
        """
        _validate_uuid(owner_instance_id, "owner_instance_id")
        marked: list[MaterialExecutionSubmissionRecord] = []
        error = MaterialExecutionSubmissionErrorMetadata(
            code="restart_unknown",
            detail="submission was in flight when its owner process stopped; explicit reconciliation is required",
            retryable=False,
            recovery_required=True,
        )
        now = _timestamp()
        try:
            with self._transaction(write=True) as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM material_execution_submissions
                    WHERE owner_instance_id = ?
                      AND state IN ('dispatching', 'running', 'blocked', 'reconciling')
                    ORDER BY created_at, submission_id
                    """,
                    (owner_instance_id,),
                ).fetchall()
                for row in rows:
                    revision = int(row["revision"]) + 1
                    result = connection.execute(
                        """
                        UPDATE material_execution_submissions
                        SET state = 'unknown', revision = ?, updated_at = ?,
                            response_json = NULL, error_json = ?
                        WHERE submission_id = ? AND revision = ?
                        """,
                        (
                            revision,
                            now,
                            _encode_json(error.to_json(), label="error_json"),
                            row["submission_id"],
                            row["revision"],
                        ),
                    )
                    if result.rowcount != 1:
                        raise MaterialExecutionSubmissionStateConflict(
                            "restart state transition was lost"
                        )
                    changed = connection.execute(
                        "SELECT * FROM material_execution_submissions WHERE submission_id = ?",
                        (row["submission_id"],),
                    ).fetchone()
                    if changed is None:
                        raise MaterialExecutionSubmissionSchemaError(
                            "restart transition lost its submission row"
                        )
                    marked.append(self._row_to_record(changed))
                return tuple(marked)
        except MaterialExecutionSubmissionError:
            raise
        except sqlite3.Error as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "cannot mark restart-unknown submissions"
            ) from exc

    def compare_and_swap(
        self,
        submission_id: str,
        *,
        expected_state: SubmissionState,
        next_state: SubmissionState,
        owner_instance_id: str | None = None,
        owner_epoch: int | None = None,
        owner_capability: MaterialExecutionOwnerLeaseCapability | None = None,
        attempt_id: str | None = None,
        response: MaterialExecutionResponse | None = None,
        error: MaterialExecutionSubmissionErrorMetadata | None = None,
        recovery_ticket: MaterialRecoveryTicket | None = None,
    ) -> MaterialExecutionSubmissionRecord:
        _validate_uuid(submission_id, "submission_id")
        if expected_state not in _STATES or next_state not in _STATES:
            raise MaterialExecutionSubmissionTransitionError("submission state is invalid")
        if owner_instance_id is not None:
            _validate_uuid(owner_instance_id, "owner_instance_id")
        if owner_epoch is not None and (
            type(owner_epoch) is not int or owner_epoch <= 0
        ):
            raise MaterialExecutionSubmissionTransitionError(
                "owner_epoch must be a positive integer"
            )
        if owner_epoch is not None and owner_instance_id is None:
            raise MaterialExecutionSubmissionTransitionError(
                "owner_epoch requires owner_instance_id"
            )
        if next_state in {"dispatching", "reconciling"} and owner_instance_id is None:
            raise MaterialExecutionSubmissionTransitionError(
                f"{next_state} state requires an owner instance"
            )
        if attempt_id is not None:
            _validate_uuid(attempt_id, "attempt_id")
        if response is not None and type(response) is not MaterialExecutionResponse:
            # Keep the strict serializer's diagnostic visible to callers.
            serialize_material_execution_response(response)  # type: ignore[arg-type]
            raise TypeError("response must be MaterialExecutionResponse")
        if error is not None and type(error) is not MaterialExecutionSubmissionErrorMetadata:
            raise TypeError("error must be MaterialExecutionSubmissionErrorMetadata")
        if attempt_id is not None and response is None and recovery_ticket is None:
            raise MaterialExecutionSubmissionTransitionError(
                "attempt ID requires a typed response or recovery ticket"
            )
        if next_state not in _TRANSITIONS[expected_state]:
            raise MaterialExecutionSubmissionTransitionError(
                f"illegal submission transition: {expected_state} -> {next_state}"
            )

        try:
            with self._transaction(write=True) as connection:
                row = connection.execute(
                    "SELECT * FROM material_execution_submissions WHERE submission_id = ?",
                    (submission_id,),
                ).fetchone()
                if row is None:
                    raise MaterialExecutionSubmissionNotFound(submission_id)
                current = self._row_to_record(row)
                if current.state != expected_state:
                    raise MaterialExecutionSubmissionStateConflict(
                        f"expected {expected_state}, found {current.state}",
                        current_state=current.state,
                    )
                if current.owner_epoch is not None and (
                    owner_instance_id is None or owner_epoch is None
                ):
                    raise MaterialExecutionSubmissionIdentityConflict(
                        "lease-owned submission requires the owner epoch token"
                    )
                if owner_epoch is not None:
                    if type(owner_capability) is not MaterialExecutionOwnerLeaseCapability:
                        raise MaterialExecutionSubmissionIdentityConflict(
                            "owner epoch mutations require the live lease capability"
                        )
                    self._require_owner_capability(
                        owner_capability,
                        MaterialExecutionOwnerToken(
                            owner_instance_id=owner_instance_id,  # type: ignore[arg-type]
                            owner_epoch=owner_epoch,
                        ),
                    )
                    active_owner = connection.execute(
                        """
                        SELECT 1 FROM material_execution_submission_owner_leases
                        WHERE owner_epoch = ? AND owner_instance_id = ? AND active = 1
                        """,
                        (owner_epoch, owner_instance_id),
                    ).fetchone()
                    if active_owner is None:
                        raise MaterialExecutionSubmissionIdentityConflict(
                            "owner token is not the active fencing epoch"
                        )
                if owner_instance_id is not None and current.owner_instance_id not in {
                    None,
                    owner_instance_id,
                } and not (expected_state == "unknown" and next_state == "failed"):
                    raise MaterialExecutionSubmissionIdentityConflict(
                        "owner instance does not match submission"
                    )
                if (
                    owner_epoch is not None
                    and current.owner_epoch is not None
                    and current.owner_epoch != owner_epoch
                    and not (expected_state == "unknown" and next_state == "failed")
                ):
                    raise MaterialExecutionSubmissionIdentityConflict(
                        "owner epoch does not match submission"
                    )
                self._validate_transition_metadata(
                    current,
                    next_state=next_state,
                    response=response,
                    error=error,
                    recovery_ticket=recovery_ticket,
                    attempt_id=attempt_id,
                )
                response_json = (
                    serialize_material_execution_response(response)
                    if response is not None
                    else current.response_json
                )
                error_value = error if error is not None else current.error
                ticket_value = (
                    recovery_ticket
                    if recovery_ticket is not None
                    else current.recovery_ticket
                )
                evidence_attempt_id = (
                    response.attempt_id
                    if response is not None
                    else recovery_ticket.attempt_id
                    if recovery_ticket is not None
                    else None
                )
                next_attempt_id = evidence_attempt_id or current.attempt_id
                if next_state in {"succeeded", "failed"}:
                    ticket_value = None
                    if response is not None:
                        error_value = None
                    else:
                        response_json = None
                if next_state == "blocked":
                    if response is not None:
                        error_value = None
                    elif error is not None:
                        response_json = None
                if next_state == "unknown" and error_value is None:
                    raise MaterialExecutionSubmissionTransitionError(
                        "unknown submission state requires typed error metadata"
                    )
                if next_state in {"accepted", "dispatching", "running", "reconciling"}:
                    # These states carry no newly completed response/error;
                    # preserve an existing recovery ticket only while entering
                    # reconciling, otherwise reject accidental metadata.
                    if next_state != "reconciling":
                        response_json = None if response is None else response_json
                        error_value = None if error is None else error_value
                revision = current.revision + 1
                updated = _timestamp(current.updated_at)
                result = connection.execute(
                    """
                    UPDATE material_execution_submissions
                    SET state = ?, revision = ?, updated_at = ?, owner_instance_id = ?,
                        owner_epoch = ?, attempt_id = ?, response_json = ?, error_json = ?,
                        recovery_ticket_json = ?
                    WHERE submission_id = ? AND revision = ? AND state = ?
                    """,
                    (
                        next_state,
                        revision,
                        updated,
                        owner_instance_id or current.owner_instance_id,
                        owner_epoch if owner_epoch is not None else current.owner_epoch,
                        next_attempt_id,
                        None if response_json is None else _encode_json(response_json, label="response_json"),
                        None if error_value is None else _encode_json(error_value.to_json(), label="error_json"),
                        _ticket_json(ticket_value),
                        submission_id,
                        current.revision,
                        expected_state,
                    ),
                )
                if result.rowcount != 1:
                    raise MaterialExecutionSubmissionStateConflict(
                        "submission state transition was lost"
                    )
                changed = connection.execute(
                    "SELECT * FROM material_execution_submissions WHERE submission_id = ?",
                    (submission_id,),
                ).fetchone()
                if changed is None:
                    raise MaterialExecutionSubmissionSchemaError(
                        "submission disappeared after state transition"
                    )
                return self._row_to_record(changed)
        except MaterialExecutionSubmissionError:
            raise
        except sqlite3.Error as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "cannot persist submission transition"
            ) from exc

    def _validate_transition_metadata(
        self,
        current: MaterialExecutionSubmissionRecord,
        *,
        next_state: str,
        response: MaterialExecutionResponse | None,
        error: MaterialExecutionSubmissionErrorMetadata | None,
        recovery_ticket: MaterialRecoveryTicket | None,
        attempt_id: str | None,
    ) -> None:
        context = current.context
        effective_ticket = recovery_ticket or current.recovery_ticket
        evidence_attempts: list[str] = []
        if response is not None:
            evidence_attempts.append(response.attempt_id)
        if recovery_ticket is not None:
            evidence_attempts.append(recovery_ticket.attempt_id)
        if len(set(evidence_attempts)) > 1:
            raise MaterialExecutionSubmissionIdentityConflict(
                "typed evidence contains different attempt identities"
            )
        evidence_attempt_id = evidence_attempts[0] if evidence_attempts else None
        if attempt_id is not None and evidence_attempt_id != attempt_id:
            raise MaterialExecutionSubmissionIdentityConflict(
                "attempt ID does not match typed evidence"
            )
        if (
            current.attempt_id is not None
            and evidence_attempt_id is not None
            and current.attempt_id != evidence_attempt_id
        ):
            raise MaterialExecutionSubmissionIdentityConflict(
                "typed evidence does not match the persisted attempt"
            )
        if response is not None:
            if (
                response.pipeline_id != context.command.pipeline_id
                or response.material_step_id != context.command.material_step_id
                or response.material_task_id != context.command.material_task_id
                or response.execution_id != context.command.execution_id
                or response.operation != context.command.operation
            ):
                raise MaterialExecutionSubmissionIdentityConflict(
                    "response does not match submission identity"
                )
            if response.recovery_ticket is not None and response.recovery_ticket != effective_ticket:
                raise MaterialExecutionSubmissionIdentityConflict(
                    "response and recovery ticket do not match"
                )
            expected_status = {
                "succeeded": {"succeeded"},
                "failed": {"failed", "terminated"},
                "blocked": {"blocked"},
            }.get(next_state)
            if expected_status is not None and response.status not in expected_status:
                raise MaterialExecutionSubmissionTransitionError(
                    "response status does not match next submission state"
                )
            if next_state in {"succeeded", "failed"} and response.recovery_ticket is not None:
                raise MaterialExecutionSubmissionTransitionError(
                    "terminal response cannot retain a recovery ticket"
                )
        if recovery_ticket is not None:
            _assert_ticket_identity(
                context,
                recovery_ticket,
                expected_attempt_id=evidence_attempt_id,
            )
            if next_state not in {"blocked", "reconciling"}:
                raise MaterialExecutionSubmissionTransitionError(
                    "recovery ticket is allowed only for blocked/reconciling states"
                )
        if next_state == "blocked" and recovery_ticket is None and current.recovery_ticket is None:
            raise MaterialExecutionSubmissionTransitionError(
                "blocked state requires a recovery ticket"
            )
        if next_state in {"succeeded", "failed"} and recovery_ticket is not None:
            raise MaterialExecutionSubmissionTransitionError(
                "terminal state cannot retain a recovery ticket"
            )
        if next_state == "succeeded" and (response is None or error is not None):
            raise MaterialExecutionSubmissionTransitionError(
                "succeeded state requires exactly one typed response"
            )
        if next_state == "failed" and ((response is None) == (error is None)):
            raise MaterialExecutionSubmissionTransitionError(
                "failed state requires exactly one typed response or error"
            )
        if next_state == "unknown" and response is not None:
            raise MaterialExecutionSubmissionTransitionError(
                "unknown state cannot masquerade as a terminal response"
            )
        if next_state == "unknown" and error is None:
            raise MaterialExecutionSubmissionTransitionError(
                "unknown state requires new typed error evidence"
            )
        if next_state == "blocked" and response is None and recovery_ticket is None:
            raise MaterialExecutionSubmissionTransitionError(
                "blocked state requires a typed response or recovery ticket"
            )
        if error is not None and next_state not in {"failed", "unknown", "blocked", "reconciling"}:
            raise MaterialExecutionSubmissionTransitionError(
                "error metadata is not allowed for this state"
            )
        if next_state in {"dispatching", "running", "reconciling"} and (
            response is not None or error is not None
        ):
            raise MaterialExecutionSubmissionTransitionError(
                f"{next_state} state cannot accept completion evidence"
            )

    def _row_to_record(self, row: sqlite3.Row) -> MaterialExecutionSubmissionRecord:
        try:
            if int(row["schema_version"]) != SCHEMA_VERSION:
                raise MaterialExecutionSubmissionSchemaError(
                    f"unsupported submission schema version: {row['schema_version']}"
                )
            context = _context_from_row(row)
            response_json = (
                None
                if row["response_json"] is None
                else _decode_json(row["response_json"], label="response_json")
            )
            if response_json is not None and not isinstance(response_json, dict):
                raise MaterialExecutionSubmissionSchemaError("response_json is not an object")
            response_obj = (
                None if response_json is None else _response_from_json(response_json)
            )
            error = (
                None
                if row["error_json"] is None
                else MaterialExecutionSubmissionErrorMetadata.from_json(
                    _decode_json(row["error_json"], label="error_json")
                )
            )
            ticket = _ticket_from_json(
                None
                if row["recovery_ticket_json"] is None
                else _decode_json(row["recovery_ticket_json"], label="recovery_ticket_json")
            )
            record = MaterialExecutionSubmissionRecord(
                schema_version=int(row["schema_version"]),
                submission_id=row["submission_id"],
                context=context,
                state=row["state"],
                revision=int(row["revision"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                owner_instance_id=row["owner_instance_id"],
                owner_epoch=(
                    None
                    if row["owner_epoch"] is None
                    else int(row["owner_epoch"])
                ),
                attempt_id=row["attempt_id"],
                response_json=response_json,
                error=error,
                recovery_ticket=ticket,
            )
            try:
                created = datetime.fromisoformat(record.created_at)
                updated = datetime.fromisoformat(record.updated_at)
                if (
                    created.tzinfo is None
                    or created.utcoffset() is None
                    or updated.tzinfo is None
                    or updated.utcoffset() is None
                ):
                    raise ValueError("submission timestamps must include a timezone")
                if updated < created:
                    raise ValueError("updated_at precedes created_at")
            except (TypeError, ValueError) as exc:
                raise MaterialExecutionSubmissionSchemaError(
                    "stored submission timestamps are invalid"
                ) from exc
            if response_json is not None:
                identity = {
                    "pipeline_id": context.command.pipeline_id,
                    "material_step_id": context.command.material_step_id,
                    "material_task_id": context.command.material_task_id,
                    "execution_id": context.command.execution_id,
                    "operation": context.command.operation,
                }
                if response_obj is None or any(
                    getattr(response_obj, key) != value
                    for key, value in identity.items()
                ):
                    raise MaterialExecutionSubmissionSchemaError(
                        "stored response identity changed"
                    )
                if record.attempt_id != response_obj.attempt_id:
                    raise MaterialExecutionSubmissionSchemaError(
                        "stored response attempt identity changed"
                    )
                if (
                    response_obj.recovery_ticket is not None
                    and record.recovery_ticket != response_obj.recovery_ticket
                ):
                    raise MaterialExecutionSubmissionSchemaError(
                        "stored response recovery identity changed"
                    )
                expected_statuses = {
                    "succeeded": {"succeeded"},
                    "failed": {"failed", "terminated"},
                    "blocked": {"blocked"},
                    "reconciling": {"blocked"},
                }
                allowed_statuses = expected_statuses.get(record.state)
                if allowed_statuses is not None and response_obj.status not in allowed_statuses:
                    raise MaterialExecutionSubmissionSchemaError(
                        "stored response status does not match submission state"
                    )
            if record.state in {"accepted", "dispatching", "running"} and any(
                value is not None
                for value in (
                    record.attempt_id,
                    record.response_json,
                    record.error,
                    record.recovery_ticket,
                )
            ):
                raise MaterialExecutionSubmissionSchemaError(
                    "pre-completion row contains unproved execution evidence"
                )
            if record.state == "blocked" and record.recovery_ticket is None:
                raise MaterialExecutionSubmissionSchemaError(
                    "blocked row has no recovery ticket"
                )
            if record.state == "blocked" and record.response_json is not None and record.error is not None:
                raise MaterialExecutionSubmissionSchemaError(
                    "blocked row contains ambiguous response and error evidence"
                )
            if record.state == "reconciling" and record.recovery_ticket is None:
                raise MaterialExecutionSubmissionSchemaError(
                    "reconciling row has no recovery ticket"
                )
            if record.state == "succeeded":
                if record.response_json is None or record.error is not None or record.recovery_ticket is not None:
                    raise MaterialExecutionSubmissionSchemaError(
                        "succeeded row does not contain exactly terminal response evidence"
                    )
            if record.state == "failed":
                if (
                    (record.response_json is None) == (record.error is None)
                    or record.recovery_ticket is not None
                ):
                    raise MaterialExecutionSubmissionSchemaError(
                        "failed row does not contain exactly one terminal evidence"
                    )
            if record.state == "unknown" and record.error is None:
                raise MaterialExecutionSubmissionSchemaError(
                    "unknown row has no typed error evidence"
                )
            return record
        except MaterialExecutionSubmissionError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise MaterialExecutionSubmissionSchemaError(
                "stored submission row is invalid"
            ) from exc


__all__ = [
    "MaterialExecutionOwnerLeaseActivation",
    "MaterialExecutionOwnerLeaseCapability",
    "MaterialExecutionOwnerLeaseRecord",
    "MaterialExecutionOwnerToken",
    "MaterialExecutionSubmissionError",
    "MaterialExecutionSubmissionErrorMetadata",
    "MaterialExecutionSubmissionIdentityConflict",
    "MaterialExecutionSubmissionNotFound",
    "MaterialExecutionSubmissionPathError",
    "MaterialExecutionSubmissionRecord",
    "MaterialExecutionSubmissionSchemaError",
    "MaterialExecutionSubmissionStateConflict",
    "MaterialExecutionSubmissionStore",
    "MaterialExecutionSubmissionTransitionError",
    "SCHEMA_VERSION",
]
