from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.pipeline.material_artifact_publisher import PublicationRejected

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
    StagedArtifact,
    TerminationEvidence,
)
from app.pipeline.material_operation_runner import (
    MaterialRunnerClaimBinding,
    PreparedMaterialAttempt,
)
from app.pipeline.material_runner_retirement import (
    MaterialRunnerRetirementAcknowledgement,
    MaterialRunnerRetirementAttestation,
)


EXECUTION_ID = "123e4567-e89b-12d3-a456-426614174000"
ATTEMPT_ID = "123e4567-e89b-12d3-a456-426614174001"
CONTROL_ID = "123e4567-e89b-12d3-a456-426614174002"
JOB_ID = "123e4567-e89b-12d3-a456-426614174003"
OBSERVED_AT = "2026-07-16T08:00:00.000000Z"


@dataclass
class _Lease:
    events: list[str]
    execution_id: str = EXECUTION_ID
    active_attempt_id: str = ATTEMPT_ID
    active_attempt_kind: str = "info_motion"
    active_control_id: str = CONTROL_ID
    active_job_instance_id: str = JOB_ID
    expected_revision: int = 1

    def assert_owned(self, session: object | None = None) -> None:
        del session

    def release(self) -> None:
        self.events.append("lease.release")


class _Publication:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def commit(self, *, retain_ownership: bool = False) -> None:
        assert retain_ownership is True
        self.events.append("publication.commit-retained")

    def release_after_retirement(self) -> None:
        self.events.append("publication.release")


class _Runner:
    def __init__(self, events: list[str], outcome: MaterialOperationOutcome) -> None:
        self.events = events
        self.outcome = outcome

    def run_claimed(self, *_args: object, **_kwargs: object) -> MaterialOperationOutcome:
        self.events.append("runner.run-claimed")
        return self.outcome

    def attest_completed_published_attempt(
        self,
        _prepared: PreparedMaterialAttempt,
        *,
        proof,
    ) -> MaterialRunnerRetirementAttestation:
        assert proof.publication_journal_sha256 == "1" * 64
        assert proof.checkpoint_sha256 == "3" * 64
        self.events.append("runner.retire-published")
        return MaterialRunnerRetirementAttestation(
            status="succeeded",
            execution_id=EXECUTION_ID,
            operation="info_motion",
            attempt_id=ATTEMPT_ID,
            control_id=CONTROL_ID,
            job_instance_id=JOB_ID,
            completion_revision=3,
            completion_payload_sha256="f" * 64,
            checkpoint_sha256="3" * 64,
            attempt_history_sha256="2" * 64,
            tombstone_sha256="7" * 64,
            retired_at=OBSERVED_AT,
            release_basis="in_process_close",
        )

    def attest_completed_terminal_attempt(
        self,
        _prepared: PreparedMaterialAttempt,
        *,
        proof,
    ) -> MaterialRunnerRetirementAttestation:
        assert proof.attempt_history_sha256 == "2" * 64
        assert proof.checkpoint_sha256 == "6" * 64
        self.events.append("runner.retire-terminal")
        return MaterialRunnerRetirementAttestation(
            status=proof.status,
            execution_id=EXECUTION_ID,
            operation="info_motion",
            attempt_id=ATTEMPT_ID,
            control_id=CONTROL_ID,
            job_instance_id=JOB_ID,
            completion_revision=3,
            completion_payload_sha256="e" * 64,
            checkpoint_sha256="6" * 64,
            attempt_history_sha256="2" * 64,
            tombstone_sha256="8" * 64,
            retired_at=OBSERVED_AT,
            release_basis="in_process_close",
        )

    # Constructor compatibility surface; normal completion must use the two
    # proof-returning methods above.
    def retire_published_attempt(self, *_args, **_kwargs):
        raise AssertionError("unproofed published retirement is forbidden")

    def retire_terminal_attempt(self, *_args, **_kwargs):
        raise AssertionError("unproofed terminal retirement is forbidden")


class _Lifecycle:
    def __init__(
        self,
        events: list[str],
        *,
        fail_success_completion: bool = False,
    ) -> None:
        self.events = events
        self.fail_success_completion = fail_success_completion

    def complete_success(self, claim, *, outcome, publication):
        del claim, outcome, publication
        self.events.append("db.complete-success")
        if self.fail_success_completion:
            raise RuntimeError("injected completion acknowledgement unknown")
        return MaterialCompletionReceipt(
            status="succeeded",
            execution_id=EXECUTION_ID,
            operation="info_motion",
            attempt_id=ATTEMPT_ID,
            execution_revision=3,
            final_payload_sha256="f" * 64,
            publication_journal_sha256="1" * 64,
            attempt_history_sha256="2" * 64,
            result_sha256="a" * 64,
            termination_outcome_sha256="b" * 64,
            publication_intent_sha256="4" * 64,
            publication_result_sha256="5" * 64,
            publication_worker_result_sha256="a" * 64,
            publication_reference_base_revision=1,
            publication_reference_base_payload_sha256="d" * 64,
            runner_retirement_checkpoint_sha256="3" * 64,
            observed_at=OBSERVED_AT,
        )

    def complete_terminal(self, claim, *, outcome):
        del claim
        self.events.append("db.complete-terminal")
        return MaterialCompletionReceipt(
            status=outcome.status,
            execution_id=EXECUTION_ID,
            operation="info_motion",
            attempt_id=ATTEMPT_ID,
            execution_revision=3,
            final_payload_sha256="e" * 64,
            attempt_history_sha256="2" * 64,
            result_sha256="a" * 64,
            termination_outcome_sha256="b" * 64,
            runner_retirement_checkpoint_sha256="6" * 64,
            observed_at=OBSERVED_AT,
        )

    def acknowledge_runner_retirement(self, claim, *, completion, attestation):
        del claim
        assert attestation.completion_revision == completion.execution_revision
        self.events.append("db.ack-runner-retirement")
        return MaterialRunnerRetirementAcknowledgement(
            completion_revision=completion.execution_revision,
            ack_revision=completion.execution_revision + 1,
            completion_payload_sha256=completion.final_payload_sha256,
            checkpoint_sha256=attestation.checkpoint_sha256,
            tombstone_sha256=attestation.tombstone_sha256,
            acknowledged_at=OBSERVED_AT,
        )

    def record_blocked(self, claim, *, reason: str) -> None:
        del claim, reason
        self.events.append("db.record-blocked")


def _claim(events: list[str]) -> PreparedMaterialClaim:
    prepared = PreparedMaterialAttempt(
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        job_instance_id=JOB_ID,
        control_id=CONTROL_ID,
        control_directory=None,  # type: ignore[arg-type]
        directory=None,  # type: ignore[arg-type]
        pin_scope=(),
        container_hash="c" * 64,
    )
    binding = MaterialRunnerClaimBinding(
        material_task_id="task-001",
        execution_id=EXECUTION_ID,
        operation="info_motion",
        attempt_id=ATTEMPT_ID,
        control_id=CONTROL_ID,
        job_instance_id=JOB_ID,
        execution_revision=1,
        payload_sha256="d" * 64,
    )
    return PreparedMaterialClaim(
        prepared=prepared,
        binding=binding,
        lease=_Lease(events),
    )


def _request_and_secrets():
    request = MaterialOperationRequest(
        protocol_version=1,
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        operation="info_motion",
        payload=InfoMotionPayload(project_name="project", title="title"),
    )
    secrets = MaterialOperationSecrets(
        version=1,
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        operation="info_motion",
        payload=InfoMotionSecrets(),
    )
    return request, secrets


def _success_outcome() -> MaterialOperationOutcome:
    artifact = StagedArtifact(
        relative_path="payload/clip.mp4",
        media_kind="video",
        source_type="info-motion",
        source_url="",
        evidence_kind="explanation",
        is_locally_generated=True,
        sha256="a" * 64,
        size_bytes=1,
    )
    result = MaterialWorkerResult(
        protocol_version=1,
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        operation="info_motion",
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
            root_pid=1234,
            exit_code=0,
            termination_method="natural",
            active_processes=0,
            observed_at=OBSERVED_AT,
        ),
    )


def _failed_outcome() -> MaterialOperationOutcome:
    result = MaterialWorkerResult(
        protocol_version=1,
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        operation="info_motion",
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
            root_pid=1234,
            exit_code=1,
            termination_method="natural",
            active_processes=0,
            observed_at=OBSERVED_AT,
        ),
    )


def test_success_order_retains_publication_until_runner_retirement() -> None:
    events: list[str] = []
    outcome = _success_outcome()
    runner = _Runner(events, outcome)
    lifecycle = _Lifecycle(events)

    def publication_factory(_claim, _request, _outcome):
        events.append("publication.prepare")
        return _Publication(events)

    coordinator = MaterialExecutionCoordinator(
        runner=runner,  # type: ignore[arg-type]
        lifecycle=lifecycle,
        publication_factory=publication_factory,
    )
    request, secrets = _request_and_secrets()
    result = coordinator.run_once(
        _claim(events),
        request,
        secrets,
        startup_timeout_seconds=1.0,
        operation_timeout_seconds=2.0,
    )

    assert result.status == "succeeded"
    assert events == [
        "runner.run-claimed",
        "publication.prepare",
        "publication.commit-retained",
        "db.complete-success",
        "runner.retire-published",
        "db.ack-runner-retirement",
        "publication.release",
        "lease.release",
    ]


def test_terminal_order_commits_history_before_runner_retirement() -> None:
    events: list[str] = []
    outcome = _failed_outcome()
    coordinator = MaterialExecutionCoordinator(
        runner=_Runner(events, outcome),  # type: ignore[arg-type]
        lifecycle=_Lifecycle(events),
        publication_factory=lambda *_args: pytest.fail("failure must not publish"),
    )
    request, secrets = _request_and_secrets()
    result = coordinator.run_once(
        _claim(events),
        request,
        secrets,
        startup_timeout_seconds=1.0,
        operation_timeout_seconds=2.0,
    )

    assert result.status == "failed"
    assert events == [
        "runner.run-claimed",
        "db.complete-terminal",
        "runner.retire-terminal",
        "db.ack-runner-retirement",
        "lease.release",
    ]


def test_prepublication_media_rejection_becomes_terminal_failure_and_releases() -> None:
    events: list[str] = []
    outcome = _success_outcome()

    def publication_factory(_claim, _request, _outcome):
        events.append("publication.prepare")
        raise PublicationRejected(
            "staged artifact media validation failed [decode_failed]"
        )

    coordinator = MaterialExecutionCoordinator(
        runner=_Runner(events, outcome),  # type: ignore[arg-type]
        lifecycle=_Lifecycle(events),
        publication_factory=publication_factory,
    )
    request, secrets = _request_and_secrets()

    result = coordinator.run_once(
        _claim(events),
        request,
        secrets,
        startup_timeout_seconds=1.0,
        operation_timeout_seconds=2.0,
    )

    assert result.status == "failed"
    assert result.outcome is not None
    assert result.outcome.result is not None
    assert result.outcome.result.reason_code == "publication_rejected"
    assert result.outcome.result.artifacts == ()
    assert events == [
        "runner.run-claimed",
        "publication.prepare",
        "db.complete-terminal",
        "runner.retire-terminal",
        "db.ack-runner-retirement",
        "lease.release",
    ]


def test_unknown_success_completion_keeps_publication_and_lease_owned() -> None:
    events: list[str] = []
    outcome = _success_outcome()
    coordinator = MaterialExecutionCoordinator(
        runner=_Runner(events, outcome),  # type: ignore[arg-type]
        lifecycle=_Lifecycle(events, fail_success_completion=True),
        publication_factory=lambda *_args: (
            events.append("publication.prepare") or _Publication(events)
        ),
    )
    request, secrets = _request_and_secrets()

    with pytest.raises(RuntimeError, match="acknowledgement unknown"):
        coordinator.run_once(
            _claim(events),
            request,
            secrets,
            startup_timeout_seconds=1.0,
            operation_timeout_seconds=2.0,
        )

    assert events == [
        "runner.run-claimed",
        "publication.prepare",
        "publication.commit-retained",
        "db.complete-success",
    ]
