"""Bounded, read-only discovery of restart-recoverable material ownership.

This module does not acquire a material-step mutex, launch a worker, copy a
file, or register a route.  It only projects exact receipt-bound ``publishing``
rows and exact post-completion runner-retirement checkpoints into handle-free
commands that may later be submitted to the permanent owner lane.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlalchemy import and_, or_, select
from sqlalchemy.engine import Connection, Engine

from app.core.database import get_sqlite_database_path
from app.models.entities import PipelineStep
from app.pipeline.material_execution_application import (
    MaterialExecutionLaneKey,
    MaterialRestartRecoveryCommand,
    MaterialRunnerRetirementRecoveryCommand,
)
from app.pipeline.material_execution_claims import (
    MaterialExecutionClaimError,
    publishing_recovery_identity_from_payload,
)
from app.pipeline.material_runner_retirement import (
    MaterialRunnerRetirementError,
    pending_runner_retirement_from_payload,
)


DEFAULT_MAX_RESTART_RECOVERY_CANDIDATES = 128
_ACTIVE_RECOVERY_STATES = ("blocked", "claimed", "publishing", "running")


class MaterialRestartRecoveryScanError(RuntimeError):
    """Startup discovery could not produce trustworthy bounded evidence."""


class MaterialRestartRecoveryScanOverflow(MaterialRestartRecoveryScanError):
    """More recoverable rows exist than this startup is allowed to inspect."""


@dataclass(frozen=True, slots=True)
class MaterialRestartRecoveryCandidate:
    lane_key: MaterialExecutionLaneKey
    command: MaterialRestartRecoveryCommand | MaterialRunnerRetirementRecoveryCommand
    candidate_id: str

    def __post_init__(self) -> None:
        if type(self.lane_key) is not MaterialExecutionLaneKey:
            raise TypeError("lane_key must be MaterialExecutionLaneKey")
        if type(self.command) not in {
            MaterialRestartRecoveryCommand,
            MaterialRunnerRetirementRecoveryCommand,
        }:
            raise TypeError("command must be a supported restart recovery command")
        if (
            self.lane_key.pipeline_id != self.command.pipeline_id
            or self.lane_key.material_step_id != self.command.material_step_id
        ):
            raise ValueError("restart candidate lane and command disagree")
        _validate_sha256(self.candidate_id, "candidate_id")


@dataclass(frozen=True, slots=True)
class MaterialRestartRecoveryAuditItem:
    pipeline_id: int
    material_step_id: int
    code: str
    detail: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.pipeline_id, "pipeline_id"),
            (self.material_step_id, "material_step_id"),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        if self.code not in {
            "invalid_publishing_recovery_evidence",
            "invalid_pending_runner_retirement_evidence",
            "unsupported_active_recovery_state",
        }:
            raise ValueError("restart audit code is not allowed")
        if type(self.detail) is not str or not self.detail or len(self.detail) > 240:
            raise ValueError("restart audit detail is invalid")


@dataclass(frozen=True, slots=True)
class MaterialRestartRecoveryScanReport:
    candidates: tuple[MaterialRestartRecoveryCandidate, ...]
    audit_required: tuple[MaterialRestartRecoveryAuditItem, ...]

    def __post_init__(self) -> None:
        if type(self.candidates) is not tuple or any(
            type(item) is not MaterialRestartRecoveryCandidate
            for item in self.candidates
        ):
            raise TypeError("candidates must contain restart recovery candidates")
        if type(self.audit_required) is not tuple or any(
            type(item) is not MaterialRestartRecoveryAuditItem
            for item in self.audit_required
        ):
            raise TypeError("audit_required must contain restart audit items")
        candidate_ids = tuple(item.candidate_id for item in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("restart recovery candidate IDs must be unique")


class SqlAlchemyMaterialRestartRecoveryScanner:
    """Fresh-read bounded scanner for the authoritative SQLite database."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        database_path: Path | str,
        max_candidates: int = DEFAULT_MAX_RESTART_RECOVERY_CANDIDATES,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        if (
            type(max_candidates) is not int
            or isinstance(max_candidates, bool)
            or max_candidates <= 0
            or max_candidates > 10_000
        ):
            raise ValueError("max_candidates must be between 1 and 10000")
        self._session_factory = session_factory
        self.database_path = _normalise_d_path(database_path)
        self.max_candidates = max_candidates

    def scan(self) -> MaterialRestartRecoveryScanReport:
        session: Any | None = None
        try:
            session = self._session_factory()
            if session is None:
                raise TypeError("session_factory returned None")
            actual_path = _database_path_from_bind(session.get_bind())
            if str(actual_path).casefold() != str(self.database_path).casefold():
                raise MaterialRestartRecoveryScanError(
                    "restart scanner session is bound to another database"
                )
            rollback = getattr(session, "rollback", None)
            if callable(rollback):
                rollback()
            expire_all = getattr(session, "expire_all", None)
            if callable(expire_all):
                expire_all()
            rows = tuple(
                session.execute(
                    select(
                        PipelineStep.id,
                        PipelineStep.pipeline_id,
                        PipelineStep.output_summary,
                    )
                    .where(
                        PipelineStep.name == "生成素材任务",
                        or_(
                            PipelineStep.output_summary["execution_state"]
                            .as_string()
                            .in_(_ACTIVE_RECOVERY_STATES),
                            and_(
                                PipelineStep.output_summary["execution_state"]
                                .as_string()
                                == "idle",
                                PipelineStep.output_summary["runner_retirement"]
                                ["state"]
                                .as_string()
                                == "pending",
                            ),
                        ),
                    )
                    .order_by(PipelineStep.id.asc())
                    .limit(self.max_candidates + 1)
                ).all()
            )
        except (MaterialRestartRecoveryScanError, MaterialRestartRecoveryScanOverflow):
            raise
        except Exception as exc:
            raise MaterialRestartRecoveryScanError(
                "restart recovery startup scan could not be completed"
            ) from exc
        finally:
            if session is not None:
                close = getattr(session, "close", None)
                if callable(close):
                    close()

        if len(rows) > self.max_candidates:
            raise MaterialRestartRecoveryScanOverflow(
                "restart recovery candidate bound was exceeded"
            )

        candidates: list[MaterialRestartRecoveryCandidate] = []
        audit_required: list[MaterialRestartRecoveryAuditItem] = []
        for material_step_id, pipeline_id, raw_payload in rows:
            if (
                type(material_step_id) is not int
                or material_step_id <= 0
                or type(pipeline_id) is not int
                or pipeline_id <= 0
            ):
                raise MaterialRestartRecoveryScanError(
                    "restart scan returned an invalid pipeline identity"
                )
            try:
                payload = _clone_json_mapping(raw_payload)
                if payload.get("execution_state") == "idle":
                    try:
                        checkpoint = pending_runner_retirement_from_payload(payload)
                        payload_sha256 = _json_sha256(payload)
                        lane_key = MaterialExecutionLaneKey(
                            database_path=self.database_path,
                            pipeline_id=pipeline_id,
                            material_step_id=material_step_id,
                        )
                        command = MaterialRunnerRetirementRecoveryCommand(
                            pipeline_id=pipeline_id,
                            material_step_id=material_step_id,
                            material_task_id=checkpoint.material_task_id,
                            execution_id=checkpoint.execution_id,
                            attempt_id=checkpoint.attempt_id,
                            operation=checkpoint.operation,
                            control_id=checkpoint.control_id,
                            job_instance_id=checkpoint.job_instance_id,
                            completion_status=checkpoint.completion_status,
                            completion_revision=checkpoint.completion_revision,
                            checkpoint_sha256=checkpoint.checkpoint_sha256,
                        )
                        command_sha256 = _json_sha256(
                            {
                                "pipeline_id": command.pipeline_id,
                                "material_step_id": command.material_step_id,
                                "material_task_id": command.material_task_id,
                                "execution_id": command.execution_id,
                                "attempt_id": command.attempt_id,
                                "operation": command.operation,
                                "control_id": command.control_id,
                                "job_instance_id": command.job_instance_id,
                                "completion_status": command.completion_status,
                                "completion_revision": command.completion_revision,
                                "checkpoint_sha256": command.checkpoint_sha256,
                            }
                        )
                        candidate_id = hashlib.sha256(
                            (
                                f"{lane_key.lane_id}|runner_retirement|"
                                f"{payload_sha256}|{command_sha256}"
                            ).encode("utf-8")
                        ).hexdigest()
                        candidates.append(
                            MaterialRestartRecoveryCandidate(
                                lane_key=lane_key,
                                command=command,
                                candidate_id=candidate_id,
                            )
                        )
                    except (MaterialRunnerRetirementError, TypeError, ValueError):
                        audit_required.append(
                            MaterialRestartRecoveryAuditItem(
                                pipeline_id=pipeline_id,
                                material_step_id=material_step_id,
                                code="invalid_pending_runner_retirement_evidence",
                                detail=(
                                    "pending runner retirement evidence is incomplete or "
                                    "inconsistent; no restart command was created"
                                ),
                            )
                        )
                    continue
                if payload.get("execution_state") != "publishing":
                    audit_required.append(
                        MaterialRestartRecoveryAuditItem(
                            pipeline_id=pipeline_id,
                            material_step_id=material_step_id,
                            code="unsupported_active_recovery_state",
                            detail=(
                                "an abandoned active material state requires a separate "
                                "proof path; no restart command was created"
                            ),
                        )
                    )
                    continue
                identity = publishing_recovery_identity_from_payload(payload)
                payload_sha256 = _json_sha256(payload)
                lane_key = MaterialExecutionLaneKey(
                    database_path=self.database_path,
                    pipeline_id=pipeline_id,
                    material_step_id=material_step_id,
                )
                command = MaterialRestartRecoveryCommand(
                    pipeline_id=pipeline_id,
                    material_step_id=material_step_id,
                    material_task_id=identity.material_task_id,
                    execution_id=identity.execution_id,
                    attempt_id=identity.attempt_id,
                    operation=identity.attempt_kind,
                    control_id=identity.control_id,
                    job_instance_id=identity.job_instance_id,
                    expected_revision=identity.execution_revision,
                    expected_payload_sha256=payload_sha256,
                )
                candidate_id = hashlib.sha256(
                    (
                        f"{lane_key.lane_id}|{payload_sha256}|"
                        f"{identity.attempt_id}"
                    ).encode("utf-8")
                ).hexdigest()
                candidates.append(
                    MaterialRestartRecoveryCandidate(
                        lane_key=lane_key,
                        command=command,
                        candidate_id=candidate_id,
                    )
                )
            except (MaterialExecutionClaimError, TypeError, ValueError):
                audit_required.append(
                    MaterialRestartRecoveryAuditItem(
                        pipeline_id=pipeline_id,
                        material_step_id=material_step_id,
                        code="invalid_publishing_recovery_evidence",
                        detail=(
                            "publishing owner evidence is incomplete or inconsistent; "
                            "no restart command was created"
                        ),
                    )
                )
        return MaterialRestartRecoveryScanReport(
            candidates=tuple(candidates),
            audit_required=tuple(audit_required),
        )


def _normalise_d_path(value: Path | str) -> Path:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value.strip():
        path = Path(value)
    else:
        raise TypeError("database_path must be a path")
    if not path.is_absolute():
        raise ValueError("database_path must be absolute")
    resolved = path.resolve(strict=False)
    if resolved.drive.casefold() != "d:" or str(resolved).startswith("\\\\"):
        raise ValueError("database_path must remain on D:")
    return resolved


def _database_path_from_bind(bind: Any) -> Path:
    if isinstance(bind, Engine):
        engine = bind
    elif isinstance(bind, Connection):
        engine = bind.engine
    else:
        engine = getattr(bind, "engine", None)
        if not isinstance(engine, Engine):
            raise TypeError("restart scanner has an unsupported database bind")
    return _normalise_d_path(get_sqlite_database_path(engine))


def _clone_json_mapping(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("publishing output must be a JSON object")
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
        raise TypeError("publishing output must contain only JSON values") from exc
    if not isinstance(decoded, dict):
        raise TypeError("publishing output must be a JSON object")
    return decoded


def _json_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_sha256(value: object, label: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


__all__ = [
    "MaterialRestartRecoveryAuditItem",
    "MaterialRestartRecoveryCandidate",
    "MaterialRestartRecoveryScanError",
    "MaterialRestartRecoveryScanOverflow",
    "MaterialRestartRecoveryScanReport",
    "SqlAlchemyMaterialRestartRecoveryScanner",
]
