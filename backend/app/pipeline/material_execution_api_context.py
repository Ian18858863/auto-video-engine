"""Server-authoritative context for material execution API adapters.

This module is intentionally one boundary *before* an owner-lane submission.
It accepts only scalar execution intent, derives the database path from the
configured SQLAlchemy engine, and performs one fresh detached read from that
database for every command build.  A request body can therefore not select a
different database or smuggle an old ``output_summary`` into the command.

The factory returns a handle-free submission context.  It does not submit a
``Future``, create a registry, register a FastAPI route, or touch the legacy
``run-next`` endpoint.  Those are separate lifecycle decisions which require a
durable submission ticket and an explicit owner-lane status API.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from app.core.database import get_sqlite_database_path
from app.models.entities import PipelineStep, ProductionPipeline
from app.pipeline.material_execution_api import (
    MaterialExecutionRequest,
    build_trusted_material_execution_context,
)
from app.pipeline.material_execution_application import (
    MaterialExecutionCommand,
    MaterialExecutionLaneKey,
)
from app.pipeline.material_operation_protocol import MaterialOperationKind


_TASK_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$"
_TASK_ID_RE = re.compile(_TASK_ID_PATTERN)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SNAPSHOT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
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


def _normalise_database_path(value: str | Path, *, label: str) -> Path:
    """Resolve an absolute D: path and reject every other volume."""

    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value.strip():
        path = Path(value).expanduser()
    else:
        raise TypeError(f"{label} must be a non-empty path")
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    resolved = path.resolve(strict=False)
    if resolved.drive.upper() != "D:":
        raise ValueError(f"{label} must remain on D:")
    return resolved


def _same_path(left: Path, right: Path) -> bool:
    return str(left).casefold() == str(right).casefold()


def _positive_int(value: Any, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _clone_json_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a JSON object")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TypeError(f"{label} must contain only JSON values") from exc
    if not isinstance(decoded, dict):
        raise TypeError(f"{label} must be a JSON object")
    return decoded


def _snapshot_digest(
    database_path: Path,
    pipeline_id: int,
    material_step_id: int,
    output_summary: Mapping[str, Any],
) -> str:
    payload = {
        "database_path": str(database_path).casefold(),
        "pipeline_id": pipeline_id,
        "material_step_id": material_step_id,
        "output_summary": output_summary,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MaterialExecutionSnapshotError(RuntimeError):
    """Base class for fail-closed fresh-snapshot failures."""


class MaterialExecutionSnapshotNotFound(MaterialExecutionSnapshotError):
    """The requested pipeline or material step no longer exists."""


class MaterialExecutionSnapshotIdentityError(MaterialExecutionSnapshotError):
    """A loader, database, or snapshot returned a different identity."""


class MaterialExecutionSnapshotUnknown(MaterialExecutionSnapshotError):
    """The database read failed without a trustworthy interpretation."""


class MaterialExecutionIntent(BaseModel):
    """Only client-authorized scalar intent accepted by the context factory.

    Paths, ORM snapshots, output payloads, and owner-lane handles are
    deliberately not fields on this model.  ``extra='forbid'`` is part of the
    safety contract: adding one of those fields is a validation failure before
    the database loader is called.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pipeline_id: int = Field(gt=0)
    material_step_id: int = Field(gt=0)
    material_task_id: str = Field(pattern=_TASK_ID_PATTERN)
    execution_id: str = Field(pattern=_UUID_RE.pattern, json_schema_extra={"format": "uuid"})
    operation: MaterialOperationKind
    source_chain: tuple[MaterialOperationKind, ...] = Field(
        min_length=1,
        max_length=5,
    )
    startup_timeout_seconds: float = Field(default=2.0, gt=0.0, le=3600.0)
    operation_timeout_seconds: float = Field(default=180.0, gt=0.0, le=86400.0)

    @field_validator("source_chain", mode="before")
    @classmethod
    def _coerce_source_chain(cls, value: Any) -> Any:
        # JSON request bodies naturally carry arrays.  Keep strict scalar
        # validation while accepting only the two sequence shapes we intend
        # to support (a string must never become a tuple of characters).
        if isinstance(value, list):
            return tuple(value)
        if isinstance(value, tuple):
            return value
        raise TypeError("source_chain must be an array")

    @field_validator("pipeline_id", "material_step_id")
    @classmethod
    def _strict_positive_ids(cls, value: int) -> int:
        _positive_int(value, "pipeline/material step id")
        return value

    @field_validator("execution_id")
    @classmethod
    def _canonical_execution_id(cls, value: str) -> str:
        if _UUID_RE.fullmatch(value) is None:
            raise ValueError("execution_id must be a canonical lowercase UUID")
        try:
            if str(UUID(value)) != value:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("execution_id must be a canonical UUID") from exc
        return value

    @model_validator(mode="after")
    def _validate_chain(self) -> "MaterialExecutionIntent":
        if self.source_chain[0] != self.operation:
            raise ValueError("source_chain must begin with operation")
        if len(set(self.source_chain)) != len(self.source_chain):
            raise ValueError("source_chain must not repeat an operation")
        if "stock_candidates" in self.source_chain and self.source_chain != (
            "stock_candidates",
        ):
            raise ValueError(
                "stock_candidates must run alone and cannot be a fallback source"
            )
        return self


@dataclass(frozen=True, slots=True)
class FreshMaterialExecutionSnapshot:
    """Detached, JSON-safe snapshot read from the authoritative database."""

    database_path: Path
    pipeline_id: int
    material_step_id: int
    output_summary: Mapping[str, Any]
    snapshot_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        path = _normalise_database_path(self.database_path, label="snapshot database_path")
        _positive_int(self.pipeline_id, "pipeline_id")
        _positive_int(self.material_step_id, "material_step_id")
        payload = _clone_json_mapping(self.output_summary, label="output_summary")
        digest = _snapshot_digest(
            path,
            self.pipeline_id,
            self.material_step_id,
            payload,
        )
        object.__setattr__(self, "database_path", path)
        object.__setattr__(self, "output_summary", payload)
        object.__setattr__(self, "snapshot_sha256", digest)


class FreshMaterialExecutionSnapshotLoader(Protocol):
    """Read one fresh detached snapshot from a server-owned database."""

    def load(
        self,
        *,
        database_path: Path,
        pipeline_id: int,
        material_step_id: int,
    ) -> FreshMaterialExecutionSnapshot:
        ...


def _engine_from_bind(bind: Any) -> Engine:
    if isinstance(bind, Engine):
        return bind
    if isinstance(bind, Connection):
        return bind.engine
    candidate = getattr(bind, "engine", None)
    if isinstance(candidate, Engine):
        return candidate
    raise TypeError("session bind must be a SQLAlchemy SQLite engine or connection")


class SqlAlchemyFreshMaterialExecutionSnapshotLoader:
    """Fresh-read loader that verifies the session is bound to the given DB."""

    def __init__(self, *, session_factory: Callable[[], Any]) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable, not a Session instance")
        self._session_factory = session_factory

    def load(
        self,
        *,
        database_path: Path,
        pipeline_id: int,
        material_step_id: int,
    ) -> FreshMaterialExecutionSnapshot:
        requested_path = _normalise_database_path(database_path, label="database_path")
        _positive_int(pipeline_id, "pipeline_id")
        _positive_int(material_step_id, "material_step_id")
        session: Any | None = None
        try:
            session = self._session_factory()
            if session is None:
                raise TypeError("session_factory returned None")
            bind = session.get_bind()
            try:
                actual_path = _normalise_database_path(
                    get_sqlite_database_path(_engine_from_bind(bind)),
                    label="bound database_path",
                )
            except ValueError as exc:
                raise MaterialExecutionSnapshotIdentityError(
                    "bound session database is not an absolute D: SQLite file"
                ) from exc
            if not _same_path(actual_path, requested_path):
                raise MaterialExecutionSnapshotIdentityError(
                    "fresh snapshot session is bound to another database"
                )

            # A factory is expected to return a new session.  Expiring here is
            # cheap and also prevents a custom session factory from serving an
            # already-cached ORM value.
            rollback = getattr(session, "rollback", None)
            if callable(rollback):
                # Drop any accidental transaction/pending state from a custom
                # session factory before the read.  This keeps this adapter
                # strictly read-only and starts the fresh snapshot boundary.
                rollback()
            expire_all = getattr(session, "expire_all", None)
            if callable(expire_all):
                expire_all()
            no_autoflush = getattr(session, "no_autoflush", None)
            context = no_autoflush if no_autoflush is not None else nullcontext()
            with context:
                pipeline_id_value = session.execute(
                    select(ProductionPipeline.id).where(
                        ProductionPipeline.id == pipeline_id
                    )
                ).scalar_one_or_none()
                if pipeline_id_value is None:
                    raise MaterialExecutionSnapshotNotFound(
                        f"pipeline {pipeline_id} does not exist"
                    )
                row = session.execute(
                    select(
                        PipelineStep.id,
                        PipelineStep.pipeline_id,
                        PipelineStep.output_summary,
                    ).where(
                        PipelineStep.id == material_step_id,
                        PipelineStep.pipeline_id == pipeline_id,
                    )
                ).one_or_none()
            if row is None:
                raise MaterialExecutionSnapshotNotFound(
                    f"material step {material_step_id} does not belong to pipeline {pipeline_id}"
                )
            step_id_value, parent_id_value, output_summary = row
            if step_id_value != material_step_id or parent_id_value != pipeline_id:
                raise MaterialExecutionSnapshotIdentityError(
                    "fresh snapshot returned a mismatched pipeline/step identity"
                )
            return FreshMaterialExecutionSnapshot(
                database_path=requested_path,
                pipeline_id=pipeline_id,
                material_step_id=material_step_id,
                output_summary=output_summary,
            )
        except (
            MaterialExecutionSnapshotNotFound,
            MaterialExecutionSnapshotIdentityError,
            MaterialExecutionSnapshotUnknown,
        ):
            raise
        except Exception as exc:
            raise MaterialExecutionSnapshotUnknown(
                "fresh material snapshot read failed"
            ) from exc
        finally:
            if session is not None:
                close = getattr(session, "close", None)
                if callable(close):
                    close()


@dataclass(frozen=True, slots=True)
class MaterialExecutionSubmissionContext:
    """Validated, handle-free inputs ready for a future owner-lane submit."""

    lane_key: MaterialExecutionLaneKey
    command: MaterialExecutionCommand
    source_chain: tuple[MaterialOperationKind, ...]
    snapshot_sha256: str

    def __post_init__(self) -> None:
        if type(self.lane_key) is not MaterialExecutionLaneKey:
            raise TypeError("lane_key must be MaterialExecutionLaneKey")
        if type(self.command) is not MaterialExecutionCommand:
            raise TypeError("command must be MaterialExecutionCommand")
        if not isinstance(self.source_chain, tuple) or not self.source_chain:
            raise TypeError("source_chain must be a non-empty tuple")
        if len(self.source_chain) > 5:
            raise ValueError("source_chain must contain at most five operations")
        if any(item not in _OPERATIONS for item in self.source_chain):
            raise ValueError("source_chain contains an unsupported operation")
        if self.source_chain[0] != self.command.operation:
            raise ValueError("source_chain must begin with command operation")
        if len(set(self.source_chain)) != len(self.source_chain):
            raise ValueError("source_chain must not repeat an operation")
        if "stock_candidates" in self.source_chain and self.source_chain != (
            "stock_candidates",
        ):
            raise ValueError(
                "stock_candidates must run alone and cannot be a fallback source"
            )
        if type(self.snapshot_sha256) is not str or _SNAPSHOT_HASH_RE.fullmatch(
            self.snapshot_sha256
        ) is None:
            raise ValueError("snapshot_sha256 must be a lowercase SHA-256 digest")
        if (
            self.lane_key.pipeline_id != self.command.pipeline_id
            or self.lane_key.material_step_id != self.command.material_step_id
        ):
            raise ValueError("lane key and command identities disagree")

    @property
    def database_path(self) -> Path:
        return self.lane_key.database_path


class MaterialExecutionCommandFactory:
    """Build a command only after a server-authoritative fresh DB read."""

    def __init__(
        self,
        *,
        engine: Engine,
        snapshot_loader: FreshMaterialExecutionSnapshotLoader | Any,
    ) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("engine must be a SQLAlchemy Engine")
        # Derive and validate this before looking at the loader.  In-memory or
        # C: engines must fail even if the caller passed an unusable loader.
        try:
            database_path = _normalise_database_path(
                get_sqlite_database_path(engine),
                label="engine database_path",
            )
        except ValueError:
            raise
        load = getattr(snapshot_loader, "load", None)
        if not callable(load):
            raise TypeError("snapshot_loader must expose a callable load method")
        self._engine = engine
        self._database_path = database_path
        self._load = load

    @property
    def database_path(self) -> Path:
        return self._database_path

    def build(
        self,
        intent: MaterialExecutionIntent | Mapping[str, Any],
    ) -> MaterialExecutionSubmissionContext:
        parsed = self._coerce_intent(intent)
        try:
            snapshot = self._load(
                database_path=self._database_path,
                pipeline_id=parsed.pipeline_id,
                material_step_id=parsed.material_step_id,
            )
        except (
            MaterialExecutionSnapshotNotFound,
            MaterialExecutionSnapshotIdentityError,
            MaterialExecutionSnapshotUnknown,
        ):
            raise
        except Exception as exc:
            raise MaterialExecutionSnapshotUnknown(
                "fresh material snapshot loader failed"
            ) from exc
        if type(snapshot) is not FreshMaterialExecutionSnapshot:
            raise MaterialExecutionSnapshotIdentityError(
                "snapshot loader returned an unsupported snapshot type"
            )
        if not _same_path(snapshot.database_path, self._database_path):
            raise MaterialExecutionSnapshotIdentityError(
                "snapshot belongs to another database"
            )
        if (
            snapshot.pipeline_id != parsed.pipeline_id
            or snapshot.material_step_id != parsed.material_step_id
        ):
            raise MaterialExecutionSnapshotIdentityError(
                "snapshot identity does not match execution intent"
            )
        expected_digest = _snapshot_digest(
            snapshot.database_path,
            snapshot.pipeline_id,
            snapshot.material_step_id,
            snapshot.output_summary,
        )
        if snapshot.snapshot_sha256 != expected_digest:
            raise MaterialExecutionSnapshotIdentityError(
                "snapshot digest does not match its detached payload"
            )

        # The only snapshot values crossing into the old DTO are values read
        # above from the authoritative database.  The request body has no way
        # to provide either path or output_summary.
        request = MaterialExecutionRequest.model_validate(
            {
                "pipeline_snapshot": {
                    "id": snapshot.pipeline_id,
                    "database_path": str(self._database_path),
                },
                "step_snapshot": {
                    "id": snapshot.material_step_id,
                    "pipeline_id": snapshot.pipeline_id,
                    "output_summary": snapshot.output_summary,
                    "database_path": str(self._database_path),
                },
                "material_task_id": parsed.material_task_id,
                "execution_id": parsed.execution_id,
                "operation": parsed.operation,
                "source_chain": parsed.source_chain,
                "startup_timeout_seconds": parsed.startup_timeout_seconds,
                "operation_timeout_seconds": parsed.operation_timeout_seconds,
                "database_path": str(self._database_path),
            }
        )
        lane_key, command = build_trusted_material_execution_context(
            request,
            database_path=self._database_path,
        )
        return MaterialExecutionSubmissionContext(
            lane_key=lane_key,
            command=command,
            source_chain=tuple(parsed.source_chain),
            snapshot_sha256=snapshot.snapshot_sha256,
        )

    @staticmethod
    def _coerce_intent(
        intent: MaterialExecutionIntent | Mapping[str, Any],
    ) -> MaterialExecutionIntent:
        if isinstance(intent, MaterialExecutionIntent):
            # Reparse an existing model: ``model_copy(update=...)`` can bypass
            # nested/model validators in Pydantic and must not bypass this
            # boundary either.
            return MaterialExecutionIntent.model_validate(intent.model_dump())
        return MaterialExecutionIntent.model_validate(intent)


# Concise aliases for route adapters and tests.
FreshMaterialSnapshot = FreshMaterialExecutionSnapshot
MaterialExecutionContext = MaterialExecutionSubmissionContext
build_material_execution_context_from_db = MaterialExecutionCommandFactory


__all__ = [
    "FreshMaterialExecutionSnapshot",
    "FreshMaterialSnapshot",
    "FreshMaterialExecutionSnapshotLoader",
    "MaterialExecutionCommandFactory",
    "MaterialExecutionContext",
    "MaterialExecutionIntent",
    "MaterialExecutionSnapshotError",
    "MaterialExecutionSnapshotIdentityError",
    "MaterialExecutionSnapshotNotFound",
    "MaterialExecutionSnapshotUnknown",
    "MaterialExecutionSubmissionContext",
    "SqlAlchemyFreshMaterialExecutionSnapshotLoader",
    "build_material_execution_context_from_db",
]
