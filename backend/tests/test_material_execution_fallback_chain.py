from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from app.pipeline.material_execution_coordinator import (
    MaterialCompletionReceipt,
    MaterialExecutionCoordinator,
    PreparedMaterialClaim,
)
from app.pipeline.material_operation_protocol import (
    InfoMotionPayload,
    InfoMotionSecrets,
    MaterialOperationOutcome,
    MaterialOperationRequest,
    MaterialOperationSecrets,
    MaterialWorkerResult,
    PexelsSecrets,
    StockSearchPayload,
    StagedArtifact,
    TerminationEvidence,
)
from app.pipeline.material_operation_runner import (
    MaterialAttemptRetirementReceipt,
    MaterialRestartRetirementProof,
    MaterialRunnerClaimBinding,
    PreparedMaterialAttempt,
)
from app.pipeline.material_runner_retirement import (
    MaterialRunnerRetirementAcknowledgement,
    MaterialRunnerRetirementAttestation,
)


EXECUTION_ID = "123e4567-e89b-12d3-a456-426614174000"
ATTEMPT_ONE = "123e4567-e89b-12d3-a456-426614174001"
ATTEMPT_TWO = "123e4567-e89b-12d3-a456-426614174002"
CONTROL_ONE = "123e4567-e89b-12d3-a456-426614174003"
CONTROL_TWO = "123e4567-e89b-12d3-a456-426614174004"
JOB_ONE = "123e4567-e89b-12d3-a456-426614174005"
JOB_TWO = "123e4567-e89b-12d3-a456-426614174006"
OBSERVED = "2026-07-17T00:00:00+00:00"


def _sha(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


@dataclass
class _Lease:
    events: list[str]
    active_attempt_id: str = ATTEMPT_ONE
    active_attempt_kind: str = "info_motion"
    active_control_id: str = CONTROL_ONE
    active_job_instance_id: str = JOB_ONE
    expected_revision: int = 1
    released: bool = False

    def __post_init__(self) -> None:
        self._payload = {
            "execution_id": EXECUTION_ID,
            "execution_state": "claimed",
            "publication_phase": "preparing",
            "attempt_transition": None,
            "execution_revision": 1,
            "active_attempt_id": self.active_attempt_id,
            "active_attempt_kind": self.active_attempt_kind,
            "active_control_id": self.active_control_id,
            "active_job_instance_id": self.active_job_instance_id,
            "active_material_task_id": "task-001",
            "material_tasks": [{"material_task_id": "task-001", "status": "进行中"}],
            "material_attempt_history": [],
        }

    @property
    def execution_id(self) -> str:
        return EXECUTION_ID

    @property
    def expected_payload(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._payload))

    def assert_owned(self, *_args: object) -> None:
        if self.released:
            raise RuntimeError("lease released")

    def release(self) -> None:
        self.events.append("lease.release")
        self.released = True


def _prepared(attempt_id: str, control_id: str, job_id: str) -> PreparedMaterialAttempt:
    return PreparedMaterialAttempt(
        execution_id=EXECUTION_ID,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_id,
        control_directory=None,  # type: ignore[arg-type]
        directory=None,  # type: ignore[arg-type]
        pin_scope=(),
        container_hash="a" * 64,
    )


class _Runner:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.prepared = {
            ATTEMPT_ONE: _prepared(ATTEMPT_ONE, CONTROL_ONE, JOB_ONE),
            ATTEMPT_TWO: _prepared(ATTEMPT_TWO, CONTROL_TWO, JOB_TWO),
        }
        self.calls = 0

    def run_claimed(self, prepared, request, **_kwargs):
        self.calls += 1
        self.events.append(f"run:{request.operation}")
        if self.calls == 1:
            result = MaterialWorkerResult(
                protocol_version=1,
                execution_id=EXECUTION_ID,
                attempt_id=prepared.attempt_id,
                operation=request.operation,
                status="failed",
                selected_artifact="",
                artifacts=(),
                reason_code="source_unavailable",
                reason="source unavailable",
            )
            return MaterialOperationOutcome(
                status="failed",
                result=result,
                termination=TerminationEvidence(
                    platform="windows_job",
                    root_pid=1,
                    exit_code=1,
                    termination_method="natural",
                    active_processes=0,
                    observed_at=OBSERVED,
                ),
            )
        artifact = StagedArtifact(
            relative_path="payload/clip.mp4",
            media_kind="video",
            source_type="Pexels",
            source_url="https://example.test/clip",
            evidence_kind="atmosphere",
            is_locally_generated=False,
            sha256="b" * 64,
            size_bytes=1,
        )
        result = MaterialWorkerResult(
            protocol_version=1,
            execution_id=EXECUTION_ID,
            attempt_id=prepared.attempt_id,
            operation=request.operation,
            status="succeeded",
            selected_artifact=artifact.relative_path,
            artifacts=(artifact,),
            reason_code="",
            reason="ok",
        )
        return MaterialOperationOutcome(
            status="succeeded",
            result=result,
            termination=TerminationEvidence(
                platform="windows_job",
                root_pid=2,
                exit_code=0,
                termination_method="natural",
                active_processes=0,
                observed_at=OBSERVED,
            ),
        )

    def bind_claim(self, prepared, binding) -> None:
        self.events.append("bind-next")
        assert prepared.attempt_id == binding.attempt_id

    def retire_terminal_attempt_with_proof(
        self,
        prepared,
        *,
        proof: MaterialRestartRetirementProof,
    ):
        self.events.append("retire-terminal")
        return MaterialAttemptRetirementReceipt(
            status=proof.status,
            execution_id=proof.execution_id,
            operation=proof.operation,
            attempt_id=proof.attempt_id,
            control_id=CONTROL_ONE,
            job_instance_id=JOB_ONE,
            execution_revision=proof.execution_revision,
            final_payload_sha256=proof.final_payload_sha256,
            attempt_history_sha256=proof.attempt_history_sha256,
            result_sha256=proof.result_sha256,
            observed_at=proof.observed_at,
            retired_at=OBSERVED,
            tombstone_sha256="c" * 64,
        )

    def retire_published_attempt(self, *_args: object, **_kwargs: object) -> None:
        self.events.append("retire-published")

    def attest_completed_published_attempt(self, prepared, *, proof):
        self.events.append("retire-published")
        return MaterialRunnerRetirementAttestation(
            status="succeeded",
            execution_id=proof.execution_id,
            operation=proof.operation,
            attempt_id=proof.attempt_id,
            control_id=prepared.control_id,
            job_instance_id=prepared.job_instance_id,
            completion_revision=proof.execution_revision,
            completion_payload_sha256=proof.final_payload_sha256,
            checkpoint_sha256=proof.checkpoint_sha256,
            attempt_history_sha256=proof.attempt_history_sha256,
            tombstone_sha256="7" * 64,
            retired_at=OBSERVED,
            release_basis="in_process_close",
        )

    def retire_terminal_attempt(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("chain must use post-runner retirement proof")


class _Publication:
    phase = "committed"
    ownership_retained = True

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def commit(self, *, retain_ownership: bool) -> None:
        assert retain_ownership is True
        self.events.append("publication.commit")

    def release_after_retirement(self) -> None:
        self.events.append("publication.release")


class _Lifecycle:
    def __init__(self, lease: _Lease, events: list[str]) -> None:
        self.lease = lease
        self.events = events

    def record_terminal_attempt_for_fallback(self, claim, *, outcome):
        self.events.append("record-terminal")
        self.lease._payload.update(
            {
                "execution_state": "running",
                "publication_phase": "rolled_back",
                "attempt_transition": "retiring",
                "execution_revision": 2,
                "active_attempt_id": None,
                "active_attempt_kind": None,
                "active_control_id": None,
                "active_job_instance_id": None,
                "active_material_task_id": None,
            }
        )
        self.lease.expected_revision = 2
        self.lease.active_attempt_id = ""
        self.lease.active_attempt_kind = ""
        self.lease.active_control_id = ""
        self.lease.active_job_instance_id = ""
        entry = {
            "execution_id": EXECUTION_ID,
            "attempt_id": ATTEMPT_ONE,
            "operation": "info_motion",
            "status": outcome.status,
        }
        entry_hash = "d" * 64
        self.lease._payload["material_attempt_history"] = [
            {**entry, "entry_sha256": entry_hash}
        ]
        return MaterialCompletionReceipt(
            status=outcome.status,
            execution_id=EXECUTION_ID,
            operation="info_motion",
            attempt_id=ATTEMPT_ONE,
            execution_revision=2,
            final_payload_sha256=_sha(self.lease._payload),
            attempt_history_sha256=entry_hash,
            result_sha256="e" * 64,
            observed_at=OBSERVED,
        )

    def rotate_terminal_attempt(
        self,
        claim,
        *,
        attempt_id,
        attempt_kind,
        control_id,
        job_instance_id,
        **_kwargs,
    ):
        self.events.append("rotate")
        self.lease._payload.update(
            {
                "execution_state": "claimed",
                "publication_phase": "preparing",
                "attempt_transition": None,
                "execution_revision": 3,
                "active_attempt_id": attempt_id,
                "active_attempt_kind": attempt_kind,
                "active_control_id": control_id,
                "active_job_instance_id": job_instance_id,
                "active_material_task_id": "task-001",
            }
        )
        self.lease.expected_revision = 3
        self.lease.active_attempt_id = attempt_id
        self.lease.active_attempt_kind = attempt_kind
        self.lease.active_control_id = control_id
        self.lease.active_job_instance_id = job_instance_id

    def complete_success(self, claim, *, outcome, publication):
        self.events.append("complete-success")
        return MaterialCompletionReceipt(
            status="succeeded",
            execution_id=EXECUTION_ID,
            operation=claim.binding.operation,
            attempt_id=claim.binding.attempt_id,
            execution_revision=4,
            final_payload_sha256="f" * 64,
            publication_journal_sha256="1" * 64,
            attempt_history_sha256="3" * 64,
            result_sha256="2" * 64,
            publication_intent_sha256="4" * 64,
            publication_result_sha256="5" * 64,
            publication_worker_result_sha256="2" * 64,
            publication_reference_base_revision=3,
            publication_reference_base_payload_sha256="b" * 64,
            runner_retirement_checkpoint_sha256="6" * 64,
            observed_at=OBSERVED,
        )

    def acknowledge_runner_retirement(self, claim, *, completion, attestation):
        del claim
        self.events.append("ack-retirement")
        return MaterialRunnerRetirementAcknowledgement(
            completion_revision=completion.execution_revision,
            ack_revision=completion.execution_revision + 1,
            completion_payload_sha256=completion.final_payload_sha256,
            checkpoint_sha256=attestation.checkpoint_sha256,
            tombstone_sha256=attestation.tombstone_sha256,
            acknowledged_at=OBSERVED,
        )

    def record_blocked(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("chain fixture did not block")


def _request(operation: str, attempt_id: str):
    if operation == "info_motion":
        payload = InfoMotionPayload(project_name="project", title="title")
        secret = InfoMotionSecrets()
    else:
        payload = StockSearchPayload(query="office", per_page=1)
        secret = PexelsSecrets(api_key="secret")
    return (
        MaterialOperationRequest(
            protocol_version=1,
            execution_id=EXECUTION_ID,
            attempt_id=attempt_id,
            operation=operation,
            payload=payload,
        ),
        MaterialOperationSecrets(
            version=1,
            execution_id=EXECUTION_ID,
            attempt_id=attempt_id,
            operation=operation,
            payload=secret,
        ),
    )


def test_same_execution_fallback_waits_for_post_runner_retirement_before_rotation():
    events: list[str] = []
    lease = _Lease(events)
    runner = _Runner(events)
    lifecycle = _Lifecycle(lease, events)
    first_prepared = runner.prepared[ATTEMPT_ONE]
    first_binding = MaterialRunnerClaimBinding(
        material_task_id="task-001",
        execution_id=EXECUTION_ID,
        operation="info_motion",
        attempt_id=ATTEMPT_ONE,
        control_id=CONTROL_ONE,
        job_instance_id=JOB_ONE,
        execution_revision=1,
        payload_sha256="a" * 64,
    )
    first_claim = PreparedMaterialClaim(
        prepared=first_prepared,
        binding=first_binding,
        lease=lease,
    )
    first_request, first_secrets = _request("info_motion", ATTEMPT_ONE)

    def prepare_next(operation, execution_id):
        events.append("prepare-next")
        assert operation == "pexels"
        assert execution_id == EXECUTION_ID
        return runner.prepared[ATTEMPT_TWO]

    def bind_next(previous, prepared, operation):
        binding = MaterialRunnerClaimBinding(
            material_task_id="task-001",
            execution_id=EXECUTION_ID,
            operation=operation,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
            job_instance_id=prepared.job_instance_id,
            execution_revision=lease.expected_revision,
            payload_sha256="b" * 64,
        )
        runner.bind_claim(prepared, binding)
        return PreparedMaterialClaim(prepared=prepared, binding=binding, lease=lease)

    coordinator = MaterialExecutionCoordinator(
        runner=runner,  # type: ignore[arg-type]
        lifecycle=lifecycle,
        publication_factory=lambda *_args: _Publication(events),
    )
    second_request, second_secrets = _request("pexels", ATTEMPT_TWO)
    result = coordinator.run_with_fallback(
        first_claim,
        first_request,
        first_secrets,
        fallback_operations=("pexels",),
        prepare_next_attempt=prepare_next,
        bind_rotated_attempt=bind_next,
        request_factory=lambda *_args: (second_request, second_secrets),
        startup_timeout_seconds=1.0,
        operation_timeout_seconds=2.0,
    )

    assert result.status == "succeeded"
    assert lease.released is True
    assert events == [
        "run:info_motion",
        "record-terminal",
        "retire-terminal",
        "prepare-next",
        "rotate",
        "bind-next",
        "run:pexels",
        "publication.commit",
        "complete-success",
        "retire-published",
        "ack-retirement",
        "publication.release",
        "lease.release",
    ]
