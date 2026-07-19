"""Durable checkpoint between material completion and runner retirement.

The business-result CAS is not the final ownership boundary.  A completed
attempt remains non-claimable until the runner has written and read back its
exact retirement tombstone and a second whole-blob CAS records that proof.
This module contains only the small, JSON-safe contract shared by lifecycle,
runner, claim and restart-recovery code.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Literal, Mapping, get_args
from uuid import UUID

from app.pipeline.material_operation_protocol import MaterialOperationKind


RUNNER_RETIREMENT_FORMAT = "material-runner-retirement-v1"
RunnerRetirementStatus = Literal["succeeded", "failed", "terminated"]
RunnerRetirementReleaseBasis = Literal[
    "in_process_close",
    "dead_controller_after_tombstone",
]

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ACTIVE_OWNER_FIELDS = (
    "active_material_task_id",
    "active_execution_id",
    "active_attempt_id",
    "active_attempt_kind",
    "active_control_id",
    "active_job_instance_id",
)


class MaterialRunnerRetirementError(ValueError):
    """The durable runner-retirement contract is missing or contradictory."""


@dataclass(frozen=True, slots=True)
class MaterialRunnerRetirementCheckpoint:
    material_task_id: str
    execution_id: str
    attempt_id: str
    operation: MaterialOperationKind
    control_id: str
    job_instance_id: str
    completion_status: RunnerRetirementStatus
    completion_revision: int
    completion_observed_at: str
    reference_revision: int
    reference_payload_sha256: str
    attempt_history_sha256: str
    result_sha256: str | None
    publication_journal_sha256: str | None
    publication_intent_sha256: str | None
    publication_result_sha256: str | None
    publication_worker_result_sha256: str | None
    publication_reference_base_revision: int | None
    publication_reference_base_payload_sha256: str | None
    checkpoint_sha256: str

    def __post_init__(self) -> None:
        if type(self.material_task_id) is not str or not self.material_task_id.strip():
            raise MaterialRunnerRetirementError("material_task_id is invalid")
        for value, label in (
            (self.execution_id, "execution_id"),
            (self.attempt_id, "attempt_id"),
            (self.control_id, "control_id"),
            (self.job_instance_id, "job_instance_id"),
        ):
            _validate_uuid(value, label)
        if self.operation not in get_args(MaterialOperationKind):
            raise MaterialRunnerRetirementError("operation is invalid")
        if self.completion_status not in {"succeeded", "failed", "terminated"}:
            raise MaterialRunnerRetirementError("completion_status is invalid")
        if (
            type(self.reference_revision) is not int
            or isinstance(self.reference_revision, bool)
            or self.reference_revision <= 0
            or type(self.completion_revision) is not int
            or isinstance(self.completion_revision, bool)
            or self.completion_revision != self.reference_revision + 1
        ):
            raise MaterialRunnerRetirementError(
                "completion revision must advance the reference exactly once"
            )
        _validate_time(self.completion_observed_at, "completion_observed_at")
        for value, label in (
            (self.reference_payload_sha256, "reference_payload_sha256"),
            (self.attempt_history_sha256, "attempt_history_sha256"),
            (self.checkpoint_sha256, "checkpoint_sha256"),
        ):
            _validate_sha256(value, label)
        if self.result_sha256 is not None:
            _validate_sha256(self.result_sha256, "result_sha256")

        publication_values = (
            self.publication_journal_sha256,
            self.publication_intent_sha256,
            self.publication_result_sha256,
            self.publication_worker_result_sha256,
            self.publication_reference_base_payload_sha256,
        )
        if self.completion_status == "succeeded":
            if self.result_sha256 is None or any(value is None for value in publication_values):
                raise MaterialRunnerRetirementError(
                    "successful retirement checkpoint lacks publication receipts"
                )
            for index, value in enumerate(publication_values):
                _validate_sha256(value, f"publication receipt {index}")
            if (
                type(self.publication_reference_base_revision) is not int
                or isinstance(self.publication_reference_base_revision, bool)
                or self.publication_reference_base_revision <= 0
            ):
                raise MaterialRunnerRetirementError(
                    "successful checkpoint has no publication base revision"
                )
            if self.publication_worker_result_sha256 != self.result_sha256:
                raise MaterialRunnerRetirementError(
                    "publication worker receipt differs from completion result"
                )
        else:
            if self.completion_status == "failed" and self.result_sha256 is None:
                raise MaterialRunnerRetirementError(
                    "failed checkpoint requires a worker-result receipt"
                )
            if self.completion_status == "terminated" and self.result_sha256 is not None:
                raise MaterialRunnerRetirementError(
                    "terminated checkpoint cannot carry a worker-result receipt"
                )
            if any(value is not None for value in publication_values) or (
                self.publication_reference_base_revision is not None
            ):
                raise MaterialRunnerRetirementError(
                    "terminal checkpoint cannot claim publication receipts"
                )
        if _json_sha256(self._body()) != self.checkpoint_sha256:
            raise MaterialRunnerRetirementError("checkpoint_sha256 does not match its body")

    @classmethod
    def create(
        cls,
        *,
        material_task_id: str,
        execution_id: str,
        attempt_id: str,
        operation: MaterialOperationKind,
        control_id: str,
        job_instance_id: str,
        completion_status: RunnerRetirementStatus,
        completion_revision: int,
        completion_observed_at: str,
        reference_revision: int,
        reference_payload_sha256: str,
        attempt_history_sha256: str,
        result_sha256: str | None,
        publication_journal_sha256: str | None = None,
        publication_intent_sha256: str | None = None,
        publication_result_sha256: str | None = None,
        publication_worker_result_sha256: str | None = None,
        publication_reference_base_revision: int | None = None,
        publication_reference_base_payload_sha256: str | None = None,
    ) -> "MaterialRunnerRetirementCheckpoint":
        values: dict[str, Any] = {
            "material_task_id": material_task_id,
            "execution_id": execution_id,
            "attempt_id": attempt_id,
            "operation": operation,
            "control_id": control_id,
            "job_instance_id": job_instance_id,
            "completion_status": completion_status,
            "completion_revision": completion_revision,
            "completion_observed_at": completion_observed_at,
            "reference_revision": reference_revision,
            "reference_payload_sha256": reference_payload_sha256,
            "attempt_history_sha256": attempt_history_sha256,
            "result_sha256": result_sha256,
            "publication_journal_sha256": publication_journal_sha256,
            "publication_intent_sha256": publication_intent_sha256,
            "publication_result_sha256": publication_result_sha256,
            "publication_worker_result_sha256": publication_worker_result_sha256,
            "publication_reference_base_revision": publication_reference_base_revision,
            "publication_reference_base_payload_sha256": (
                publication_reference_base_payload_sha256
            ),
        }
        return cls(**values, checkpoint_sha256=_json_sha256(values))

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "MaterialRunnerRetirementCheckpoint":
        expected_keys = {
            "material_task_id",
            "execution_id",
            "attempt_id",
            "operation",
            "control_id",
            "job_instance_id",
            "completion_status",
            "completion_revision",
            "completion_observed_at",
            "reference_revision",
            "reference_payload_sha256",
            "attempt_history_sha256",
            "result_sha256",
            "publication_journal_sha256",
            "publication_intent_sha256",
            "publication_result_sha256",
            "publication_worker_result_sha256",
            "publication_reference_base_revision",
            "publication_reference_base_payload_sha256",
            "checkpoint_sha256",
        }
        if type(value) is not dict or set(value) != expected_keys:
            raise MaterialRunnerRetirementError("retirement checkpoint schema is invalid")
        return cls(**copy.deepcopy(dict(value)))

    def _body(self) -> dict[str, Any]:
        value = self.to_json()
        value.pop("checkpoint_sha256")
        return value

    def to_json(self) -> dict[str, Any]:
        return {
            "material_task_id": self.material_task_id,
            "execution_id": self.execution_id,
            "attempt_id": self.attempt_id,
            "operation": self.operation,
            "control_id": self.control_id,
            "job_instance_id": self.job_instance_id,
            "completion_status": self.completion_status,
            "completion_revision": self.completion_revision,
            "completion_observed_at": self.completion_observed_at,
            "reference_revision": self.reference_revision,
            "reference_payload_sha256": self.reference_payload_sha256,
            "attempt_history_sha256": self.attempt_history_sha256,
            "result_sha256": self.result_sha256,
            "publication_journal_sha256": self.publication_journal_sha256,
            "publication_intent_sha256": self.publication_intent_sha256,
            "publication_result_sha256": self.publication_result_sha256,
            "publication_worker_result_sha256": self.publication_worker_result_sha256,
            "publication_reference_base_revision": self.publication_reference_base_revision,
            "publication_reference_base_payload_sha256": (
                self.publication_reference_base_payload_sha256
            ),
            "checkpoint_sha256": self.checkpoint_sha256,
        }


@dataclass(frozen=True, slots=True)
class MaterialRunnerRetirementAttestation:
    status: RunnerRetirementStatus
    execution_id: str
    operation: MaterialOperationKind
    attempt_id: str
    control_id: str
    job_instance_id: str
    completion_revision: int
    completion_payload_sha256: str
    checkpoint_sha256: str
    attempt_history_sha256: str
    tombstone_sha256: str
    retired_at: str
    release_basis: RunnerRetirementReleaseBasis

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed", "terminated"}:
            raise MaterialRunnerRetirementError("attestation status is invalid")
        for value, label in (
            (self.execution_id, "execution_id"),
            (self.attempt_id, "attempt_id"),
            (self.control_id, "control_id"),
            (self.job_instance_id, "job_instance_id"),
        ):
            _validate_uuid(value, label)
        if self.operation not in get_args(MaterialOperationKind):
            raise MaterialRunnerRetirementError("attestation operation is invalid")
        if (
            type(self.completion_revision) is not int
            or isinstance(self.completion_revision, bool)
            or self.completion_revision <= 0
        ):
            raise MaterialRunnerRetirementError("completion_revision is invalid")
        for value, label in (
            (self.completion_payload_sha256, "completion_payload_sha256"),
            (self.checkpoint_sha256, "checkpoint_sha256"),
            (self.attempt_history_sha256, "attempt_history_sha256"),
            (self.tombstone_sha256, "tombstone_sha256"),
        ):
            _validate_sha256(value, label)
        _validate_time(self.retired_at, "retired_at")
        if self.release_basis not in {
            "in_process_close",
            "dead_controller_after_tombstone",
        }:
            raise MaterialRunnerRetirementError("release_basis is invalid")


@dataclass(frozen=True, slots=True)
class MaterialRunnerRetirementAcknowledgement:
    completion_revision: int
    ack_revision: int
    completion_payload_sha256: str
    checkpoint_sha256: str
    tombstone_sha256: str
    acknowledged_at: str

    def __post_init__(self) -> None:
        if self.ack_revision != self.completion_revision + 1:
            raise MaterialRunnerRetirementError("ack revision is not the next revision")
        for value, label in (
            (self.completion_payload_sha256, "completion_payload_sha256"),
            (self.checkpoint_sha256, "checkpoint_sha256"),
            (self.tombstone_sha256, "tombstone_sha256"),
        ):
            _validate_sha256(value, label)
        _validate_time(self.acknowledged_at, "acknowledged_at")


@dataclass(frozen=True, slots=True)
class AttestedMaterialRunnerRetirement:
    checkpoint: MaterialRunnerRetirementCheckpoint
    acknowledgement: MaterialRunnerRetirementAcknowledgement
    retired_at: str
    release_basis: RunnerRetirementReleaseBasis

    def __post_init__(self) -> None:
        if type(self.checkpoint) is not MaterialRunnerRetirementCheckpoint:
            raise MaterialRunnerRetirementError("attested checkpoint is invalid")
        if type(self.acknowledgement) is not MaterialRunnerRetirementAcknowledgement:
            raise MaterialRunnerRetirementError("retirement acknowledgement is invalid")
        if (
            self.acknowledgement.completion_revision
            != self.checkpoint.completion_revision
            or self.acknowledgement.checkpoint_sha256
            != self.checkpoint.checkpoint_sha256
        ):
            raise MaterialRunnerRetirementError(
                "retirement acknowledgement does not match its checkpoint"
            )
        _validate_time(self.retired_at, "retired_at")
        if self.release_basis not in get_args(RunnerRetirementReleaseBasis):
            raise MaterialRunnerRetirementError("release_basis is invalid")


def install_pending_runner_retirement(
    payload: dict[str, Any],
    checkpoint: MaterialRunnerRetirementCheckpoint,
) -> None:
    if type(checkpoint) is not MaterialRunnerRetirementCheckpoint:
        raise TypeError("checkpoint must be MaterialRunnerRetirementCheckpoint")
    payload["runner_retirement"] = {
        "format": RUNNER_RETIREMENT_FORMAT,
        "state": "pending",
        "checkpoint": checkpoint.to_json(),
        "attestation": None,
    }


def pending_runner_retirement_from_payload(
    payload: Mapping[str, Any],
) -> MaterialRunnerRetirementCheckpoint:
    current = _clone(payload)
    block = current.get("runner_retirement")
    if type(block) is not dict or set(block) != {
        "format",
        "state",
        "checkpoint",
        "attestation",
    }:
        raise MaterialRunnerRetirementError("runner_retirement schema is invalid")
    if (
        block.get("format") != RUNNER_RETIREMENT_FORMAT
        or block.get("state") != "pending"
        or block.get("attestation") is not None
    ):
        raise MaterialRunnerRetirementError("runner retirement is not pending")
    checkpoint = MaterialRunnerRetirementCheckpoint.from_json(block["checkpoint"])
    expected_phase = "committed" if checkpoint.completion_status == "succeeded" else "rolled_back"
    if (
        current.get("execution_id") != checkpoint.execution_id
        or current.get("execution_revision") != checkpoint.completion_revision
        or current.get("execution_state") != "idle"
        or current.get("publication_phase") != expected_phase
        or current.get("last_execution_status") != checkpoint.completion_status
        or current.get("execution_completed_at") != checkpoint.completion_observed_at
        or any(current.get(field) not in (None, "") for field in _ACTIVE_OWNER_FIELDS)
    ):
        raise MaterialRunnerRetirementError(
            "pending runner retirement does not match the completed payload"
        )
    history = current.get("material_attempt_history")
    if not isinstance(history, list):
        raise MaterialRunnerRetirementError("completed payload has no attempt history")
    matches = [
        item
        for item in history
        if isinstance(item, Mapping)
        and item.get("material_task_id") == checkpoint.material_task_id
        and item.get("execution_id") == checkpoint.execution_id
        and item.get("attempt_id") == checkpoint.attempt_id
        and item.get("operation") == checkpoint.operation
        and item.get("control_id") == checkpoint.control_id
        and item.get("job_instance_id") == checkpoint.job_instance_id
        and item.get("status") == checkpoint.completion_status
        and item.get("observed_at") == checkpoint.completion_observed_at
        and item.get("entry_sha256") == checkpoint.attempt_history_sha256
        and item.get("result_sha256") == checkpoint.result_sha256
        and item.get("publication_journal_sha256")
        == checkpoint.publication_journal_sha256
    ]
    if len(matches) != 1:
        raise MaterialRunnerRetirementError(
            "pending runner retirement does not match one history receipt"
        )
    if checkpoint.completion_status == "succeeded" and (
        current.get("publication_intent_sha256")
        != checkpoint.publication_intent_sha256
        or current.get("publication_result_sha256")
        != checkpoint.publication_result_sha256
        or current.get("publication_worker_result_sha256")
        != checkpoint.publication_worker_result_sha256
        or current.get("publication_reference_base_revision")
        != checkpoint.publication_reference_base_revision
        or current.get("publication_reference_base_payload_sha256")
        != checkpoint.publication_reference_base_payload_sha256
    ):
        raise MaterialRunnerRetirementError(
            "pending runner retirement publication receipts changed"
        )
    return checkpoint


def attested_runner_retirement_from_payload(
    payload: Mapping[str, Any],
) -> AttestedMaterialRunnerRetirement:
    """Reconstruct a fully bound acknowledgement from one exact final payload."""

    current = _clone(payload)
    block = current.get("runner_retirement")
    if type(block) is not dict or set(block) != {
        "format",
        "state",
        "checkpoint",
        "attestation",
    }:
        raise MaterialRunnerRetirementError("runner_retirement schema is invalid")
    if (
        block.get("format") != RUNNER_RETIREMENT_FORMAT
        or block.get("state") != "attested"
    ):
        raise MaterialRunnerRetirementError("runner retirement is not attested")

    checkpoint = MaterialRunnerRetirementCheckpoint.from_json(block["checkpoint"])
    attestation = block.get("attestation")
    if type(attestation) is not dict or set(attestation) != {
        "completion_payload_sha256",
        "tombstone_sha256",
        "retired_at",
        "release_basis",
        "ack_revision",
        "acknowledged_at",
    }:
        raise MaterialRunnerRetirementError(
            "runner retirement attestation schema is invalid"
        )

    acknowledgement = MaterialRunnerRetirementAcknowledgement(
        completion_revision=checkpoint.completion_revision,
        ack_revision=attestation["ack_revision"],
        completion_payload_sha256=attestation["completion_payload_sha256"],
        checkpoint_sha256=checkpoint.checkpoint_sha256,
        tombstone_sha256=attestation["tombstone_sha256"],
        acknowledged_at=attestation["acknowledged_at"],
    )
    if current.get("execution_revision") != acknowledgement.ack_revision:
        raise MaterialRunnerRetirementError(
            "attested runner retirement revision does not match the payload"
        )

    pending = copy.deepcopy(current)
    pending["execution_revision"] = checkpoint.completion_revision
    pending["runner_retirement"] = {
        "format": RUNNER_RETIREMENT_FORMAT,
        "state": "pending",
        "checkpoint": checkpoint.to_json(),
        "attestation": None,
    }
    if _json_sha256(pending) != acknowledgement.completion_payload_sha256:
        raise MaterialRunnerRetirementError(
            "runner retirement attestation does not bind the completed payload"
        )
    reconstructed = pending_runner_retirement_from_payload(pending)
    if reconstructed != checkpoint:
        raise MaterialRunnerRetirementError(
            "runner retirement checkpoint changed during reconstruction"
        )

    return AttestedMaterialRunnerRetirement(
        checkpoint=checkpoint,
        acknowledgement=acknowledgement,
        retired_at=attestation["retired_at"],
        release_basis=attestation["release_basis"],
    )


def json_payload_sha256(payload: Mapping[str, Any]) -> str:
    return _json_sha256(_clone(payload))


def _clone(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MaterialRunnerRetirementError("payload must be a JSON object")
    try:
        decoded = json.loads(
            json.dumps(
                dict(value),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MaterialRunnerRetirementError("payload is not strict JSON") from exc
    if type(decoded) is not dict:
        raise MaterialRunnerRetirementError("payload must be a JSON object")
    return copy.deepcopy(decoded)


def _json_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _validate_sha256(value: object, label: str) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise MaterialRunnerRetirementError(f"{label} must be a lowercase SHA-256")


def _validate_uuid(value: object, label: str) -> None:
    if type(value) is not str:
        raise MaterialRunnerRetirementError(f"{label} must be a canonical UUID")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise MaterialRunnerRetirementError(f"{label} must be a canonical UUID") from exc


def _validate_time(value: object, label: str) -> None:
    if type(value) is not str or not value:
        raise MaterialRunnerRetirementError(f"{label} must be ISO-8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MaterialRunnerRetirementError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MaterialRunnerRetirementError(f"{label} must include a timezone")


__all__ = [
    "AttestedMaterialRunnerRetirement",
    "MaterialRunnerRetirementAcknowledgement",
    "MaterialRunnerRetirementAttestation",
    "MaterialRunnerRetirementCheckpoint",
    "MaterialRunnerRetirementError",
    "RUNNER_RETIREMENT_FORMAT",
    "attested_runner_retirement_from_payload",
    "install_pending_runner_retirement",
    "json_payload_sha256",
    "pending_runner_retirement_from_payload",
]
