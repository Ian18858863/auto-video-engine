"""Explicit, handle-free API seam for source-chain material execution.

This module intentionally stops at the application boundary.  It does not
register a FastAPI route and it does not alter the legacy ``run-next`` entry
point.  Request payloads contain detached pipeline/step snapshots; the
conversion helpers validate identity and construct the owner-lane command and
key without exposing a SQLAlchemy session or any native ownership handle.
Route adapters must use the ``build_trusted_*`` helpers with the
server-authoritative database path and must fresh-read the canonical DB before
trusting a detached snapshot as an executable task.
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import re
from typing import Any, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.pipeline.material_execution_application import (
    MaterialExecutionCommand,
    MaterialExecutionLaneBusy,
    MaterialExecutionLaneClosed,
    MaterialExecutionLaneError,
    MaterialExecutionLaneKey,
    MaterialExecutionLaneUnknown,
    MaterialExecutionResponse,
)
from app.pipeline.material_execution_claims import (
    MaterialExecutionClaimConflict,
    MaterialExecutionClaimError,
    MaterialExecutionClaimUnknown,
)
from app.materials.planner import material_task_id_for_shot
from app.pipeline.material_operation_protocol import MaterialOperationKind


_TASK_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$"
_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
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


class DetachedPipelineSnapshot(BaseModel):
    """The small immutable projection needed to identify a pipeline lane.

    Both ``id`` and ``pipeline_id`` are accepted because SQLAlchemy detached
    rows and serialized API snapshots use those names interchangeably.
    """

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        from_attributes=True,
        json_schema_extra={
            "anyOf": [
                {"required": ["id"]},
                {"required": ["pipeline_id"]},
            ]
        },
    )

    id: int | None = None
    pipeline_id: int | None = None
    database_path: str | None = None
    project_name: str = ""
    project_key: str = ""

    @model_validator(mode="after")
    def _validate_identity(self) -> "DetachedPipelineSnapshot":
        values = [value for value in (self.id, self.pipeline_id) if value is not None]
        if not values or any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("pipeline snapshot must contain a positive id")
        if len(set(values)) != 1:
            raise ValueError("pipeline snapshot id and pipeline_id disagree")
        return self

    @property
    def identity_id(self) -> int:
        if self.pipeline_id is not None:
            return self.pipeline_id
        assert self.id is not None
        return self.id


class DetachedPipelineStepSnapshot(BaseModel):
    """Detached material-step projection used for task and parent checks."""

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        from_attributes=True,
        json_schema_extra={
            "anyOf": [
                {"required": ["id"]},
                {"required": ["step_id"]},
                {"required": ["material_step_id"]},
            ]
        },
    )

    id: int | None = None
    step_id: int | None = None
    material_step_id: int | None = None
    pipeline_id: int = Field(gt=0)
    output_summary: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_identity(self) -> "DetachedPipelineStepSnapshot":
        values = [
            value
            for value in (self.id, self.step_id, self.material_step_id)
            if value is not None
        ]
        if not values or any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("step snapshot must contain a positive id")
        if len(set(values)) != 1:
            raise ValueError("step snapshot id fields disagree")
        return self

    @property
    def identity_id(self) -> int:
        for value in (self.material_step_id, self.step_id, self.id):
            if value is not None:
                return value
        raise AssertionError("validated step has no id")


class MaterialExecutionRequest(BaseModel):
    """Opt-in source-chain request accepted by a future route adapter."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    pipeline: DetachedPipelineSnapshot = Field(
        validation_alias="pipeline_snapshot",
        serialization_alias="pipeline_snapshot",
    )
    step: DetachedPipelineStepSnapshot = Field(
        validation_alias="step_snapshot",
        serialization_alias="step_snapshot",
    )
    material_task_id: str = Field(
        pattern=_TASK_ID_PATTERN,
        description=(
            "Canonical material_task_id. A legacy shot token is accepted only "
            "when the detached snapshot resolves it to the persisted canonical ID."
        ),
    )
    execution_id: str = Field(
        pattern=_UUID_PATTERN,
        json_schema_extra={"format": "uuid"},
    )
    operation: MaterialOperationKind
    source_chain: tuple[MaterialOperationKind, ...] = Field(
        min_length=1,
        max_length=5,
        description="Distinct ordered source operations; the first equals operation.",
    )
    startup_timeout_seconds: float = Field(default=2.0, gt=0.0, le=3600.0)
    operation_timeout_seconds: float = Field(default=180.0, gt=0.0, le=86400.0)
    database_path: str | None = None

    @field_validator("execution_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        try:
            if str(UUID(value)) != value:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("execution_id must be a canonical UUID") from exc
        return value

    @model_validator(mode="after")
    def _validate_chain_and_snapshots(self) -> "MaterialExecutionRequest":
        if self.step.pipeline_id is not None and self.step.pipeline_id != self.pipeline.identity_id:
            raise ValueError("step snapshot belongs to another pipeline")
        if not self.source_chain:
            raise ValueError("source_chain must be a non-empty ordered list")
        if self.source_chain[0] != self.operation:
            raise ValueError("source_chain must begin with operation")
        if any(item not in _OPERATIONS for item in self.source_chain):
            raise ValueError("source_chain contains an unsupported operation")
        if len(set(self.source_chain)) != len(self.source_chain):
            raise ValueError("source_chain must not repeat an operation")
        if "stock_candidates" in self.source_chain and self.source_chain != (
            "stock_candidates",
        ):
            raise ValueError(
                "stock_candidates must run alone and cannot be a fallback source"
            )
        return self

    @property
    def pipeline_snapshot(self) -> DetachedPipelineSnapshot:
        return self.pipeline

    @property
    def step_snapshot(self) -> DetachedPipelineStepSnapshot:
        return self.step


def _coerce_request(
    request: MaterialExecutionRequest | Mapping[str, Any],
) -> MaterialExecutionRequest:
    if isinstance(request, MaterialExecutionRequest):
        # ``model_copy(update=...)`` can bypass nested validation.  Reparse
        # even an existing DTO before it reaches a command or lane key.
        return MaterialExecutionRequest.model_validate(
            request.model_dump(by_alias=True)
        )
    return MaterialExecutionRequest.model_validate(request)


def build_material_execution_lane_key(
    request: MaterialExecutionRequest | Mapping[str, Any],
    *,
    database_path: str | Path | None = None,
) -> MaterialExecutionLaneKey:
    """Build a D-drive owner-lane key from a trusted server DB path."""

    request = _coerce_request(request)
    explicit_path = _normalize_database_path(database_path, label="database_path")
    if explicit_path is None:
        raise ValueError("server-authoritative database_path is required")
    snapshot_paths = [
        value
        for value in (request.database_path, request.pipeline.database_path)
        if value is not None
    ]
    normalized_snapshot_paths = tuple(
        _normalize_database_path(value, label="snapshot database_path")
        for value in snapshot_paths
    )
    if len(set(normalized_snapshot_paths)) > 1:
        raise ValueError("snapshot database_path values disagree")
    snapshot_path = normalized_snapshot_paths[0] if normalized_snapshot_paths else None
    if (
        explicit_path is not None
        and snapshot_path is not None
        and explicit_path != snapshot_path
    ):
        raise ValueError("database_path arguments disagree")
    path = explicit_path
    return MaterialExecutionLaneKey(
        database_path=path,
        pipeline_id=request.pipeline.identity_id,
        material_step_id=request.step.identity_id,
    )


def build_material_execution_command(
    request: MaterialExecutionRequest | Mapping[str, Any],
) -> MaterialExecutionCommand:
    """Convert a validated detached request into a handle-free command."""

    request = _coerce_request(request)
    canonical_task_id = _canonical_material_task_id(
        request.step.output_summary,
        request.material_task_id,
    )
    return MaterialExecutionCommand(
        pipeline_id=request.pipeline.identity_id,
        material_step_id=request.step.identity_id,
        material_task_id=canonical_task_id,
        execution_id=request.execution_id,
        operation=request.operation,
        startup_timeout_seconds=request.startup_timeout_seconds,
        operation_timeout_seconds=request.operation_timeout_seconds,
    )


def build_material_execution_context(
    request: MaterialExecutionRequest | Mapping[str, Any],
    *,
    database_path: str | Path | None = None,
) -> tuple[MaterialExecutionLaneKey, MaterialExecutionCommand]:
    """Build key and command together, preserving one validated request."""

    request = _coerce_request(request)
    return (
        build_material_execution_lane_key(request, database_path=database_path),
        build_material_execution_command(request),
    )


def build_trusted_material_execution_lane_key(
    request: MaterialExecutionRequest | Mapping[str, Any],
    *,
    database_path: str | Path,
) -> MaterialExecutionLaneKey:
    """Build a lane key using a server-authoritative database path.

    The required keyword prevents a future route from accidentally using a
    client-supplied snapshot path as the serialization authority.
    """

    if database_path is None:
        raise TypeError("server-authoritative database_path is required")
    return build_material_execution_lane_key(request, database_path=database_path)


def build_trusted_material_execution_context(
    request: MaterialExecutionRequest | Mapping[str, Any],
    *,
    database_path: str | Path,
) -> tuple[MaterialExecutionLaneKey, MaterialExecutionCommand]:
    """Build the future route context with an explicit trusted DB path."""

    if database_path is None:
        raise TypeError("server-authoritative database_path is required")
    return build_material_execution_context(request, database_path=database_path)


def serialize_material_execution_response(
    response: MaterialExecutionResponse,
) -> dict[str, Any]:
    """Return a strict JSON-safe copy with no owner-lane internals."""

    if type(response) is not MaterialExecutionResponse:
        raise TypeError("response must be MaterialExecutionResponse")
    value: Any = asdict(response)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, default=_json_default)
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TypeError("material execution response is not JSON-safe") from exc
    if not isinstance(decoded, dict):
        raise TypeError("material execution response must be a JSON object")
    return decoded


def _json_default(value: Any) -> Any:
    raise TypeError(f"unsupported response value: {type(value).__name__}")


class MaterialExecutionApiError(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    detail: str
    retryable: bool = False
    recovery_required: bool = False


def map_material_execution_error(exc: BaseException) -> tuple[int, dict[str, Any]]:
    """Map registry failures to a stable, JSON-safe HTTP-shaped response."""

    if isinstance(exc, MaterialExecutionLaneBusy):
        error = MaterialExecutionApiError(
            code="material_lane_busy",
            detail=str(exc),
            recovery_required=True,
        )
        return 409, error.model_dump(mode="json")
    if isinstance(exc, MaterialExecutionLaneClosed):
        error = MaterialExecutionApiError(code="material_lane_closed", detail=str(exc))
        return 409, error.model_dump(mode="json")
    if isinstance(exc, MaterialExecutionLaneUnknown):
        error = MaterialExecutionApiError(
            code="material_lane_unknown",
            detail=str(exc),
            recovery_required=True,
        )
        return 503, error.model_dump(mode="json")
    if isinstance(exc, MaterialExecutionClaimConflict):
        error = MaterialExecutionApiError(
            code="material_claim_conflict",
            detail=str(exc),
        )
        return 409, error.model_dump(mode="json")
    if isinstance(exc, MaterialExecutionClaimUnknown):
        error = MaterialExecutionApiError(
            code="material_claim_unknown",
            detail=str(exc),
            recovery_required=True,
        )
        return 503, error.model_dump(mode="json")
    if isinstance(exc, MaterialExecutionClaimError):
        error = MaterialExecutionApiError(
            code="material_claim_error",
            detail=str(exc),
            recovery_required=True,
        )
        return 503, error.model_dump(mode="json")
    if isinstance(exc, TimeoutError):
        error = MaterialExecutionApiError(
            code="material_execution_timeout",
            detail=str(exc),
            recovery_required=True,
        )
        return 504, error.model_dump(mode="json")
    if isinstance(exc, (ValueError, TypeError)):
        error = MaterialExecutionApiError(
            code="invalid_material_execution_request", detail=str(exc)
        )
        return 422, error.model_dump(mode="json")
    if isinstance(exc, MaterialExecutionLaneError):
        error = MaterialExecutionApiError(
            code="material_lane_error",
            detail=str(exc),
            recovery_required=True,
        )
        return 503, error.model_dump(mode="json")
    error = MaterialExecutionApiError(
        code="material_execution_error", detail="material execution failed"
    )
    return 500, error.model_dump(mode="json")


# Short aliases for route adapters and tests.
registry_error_response = map_material_execution_error
response_to_json = serialize_material_execution_response
# Descriptive aliases keep the adapter convenient for a route module without
# forcing it to know the internal owner-lane terminology.  The lane alias is
# deliberately the trusted variant: a public route must provide its DB path
# from application configuration, never from the request body.
MaterialExecutionRequestDTO = MaterialExecutionRequest
build_command_from_snapshots = build_material_execution_command
build_lane_key_from_snapshots = build_trusted_material_execution_lane_key


def _validate_task_snapshot(payload: Mapping[str, Any], task_id: str) -> None:
    _canonical_material_task_id(payload, task_id)


def _canonical_material_task_id(payload: Mapping[str, Any], task_id: str) -> str:
    """Resolve a request/legacy shot token to the persisted claim ID.

    The claim store matches only canonical ``material_task_id`` values.  A
    shot-only snapshot is accepted as an input compatibility form, but the
    command never carries the shot alias onward.
    """

    if not isinstance(payload, Mapping) or "material_tasks" not in payload:
        raise ValueError("step snapshot material_tasks are required")
    tasks = payload.get("material_tasks")
    if not isinstance(tasks, (list, tuple)):
        raise ValueError("step snapshot material_tasks must be a list")
    exact_matches = [
        item
        for item in tasks
        if isinstance(item, Mapping) and item.get("material_task_id") == task_id
    ]
    if len(exact_matches) == 1:
        if re.fullmatch(_TASK_ID_PATTERN, task_id) is None:
            raise ValueError("persisted material_task_id is invalid")
        return task_id
    if len(exact_matches) > 1:
        raise ValueError("step snapshot contains duplicate material_task_id")
    shot_matches = [
        item
        for item in tasks
        if isinstance(item, Mapping) and item.get("shot_id") == task_id
    ]
    if len(shot_matches) != 1:
        raise ValueError("step snapshot does not contain requested material task")
    persisted_id = shot_matches[0].get("material_task_id")
    if isinstance(persisted_id, str) and persisted_id:
        if re.fullmatch(_TASK_ID_PATTERN, persisted_id) is None:
            raise ValueError("persisted material_task_id is invalid")
        return persisted_id
    try:
        return material_task_id_for_shot(task_id)
    except ValueError as exc:
        raise ValueError("requested shot has no canonical material_task_id") from exc


def _normalize_database_path(value: str | Path | None, *, label: str) -> Path | None:
    if value is None:
        return None
    if not str(value).strip():
        raise ValueError(f"{label} is required when provided")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    resolved = path.resolve(strict=False)
    if resolved.drive.upper() != "D:":
        raise ValueError(f"{label} must remain on D:")
    return resolved


__all__ = [
    "DetachedPipelineSnapshot",
    "DetachedPipelineStepSnapshot",
    "MaterialExecutionApiError",
    "MaterialExecutionRequest",
    "MaterialExecutionRequestDTO",
    "build_command_from_snapshots",
    "build_material_execution_command",
    "build_material_execution_context",
    "build_material_execution_lane_key",
    "build_trusted_material_execution_context",
    "build_trusted_material_execution_lane_key",
    "build_lane_key_from_snapshots",
    "map_material_execution_error",
    "registry_error_response",
    "response_to_json",
    "serialize_material_execution_response",
]
