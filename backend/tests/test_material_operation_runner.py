from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Iterator
from uuid import UUID, uuid4

import pytest

from app.pipeline import material_operation_runner as runner_subject
from app.core.windows_durable_file import write_durable_bytes, write_durable_json
from app.core.windows_storage_policy import (
    ApprovedPhysicalDirectory,
    ApprovedPhysicalWriteRoot,
    StoragePolicyError,
    approve_physical_d_write_root,
)
from app.pipeline.material_operation_runner import (
    AdoptedMaterialAttempt,
    MATERIAL_LAUNCH_PHASES,
    MaterialLaunchPhase,
    MaterialOperationRunnerError,
    MaterialRestartOwnershipGuard,
    MaterialRestartPublishedRetirementProof,
    MaterialRestartRetirementProof,
    MaterialRunnerClaimBinding,
    PreparedMaterialAttempt,
    ProcessMaterialOperationRunner,
    ReconciliationEvidence,
    decode_material_container_descriptor,
    encode_material_container_descriptor,
)
from app.pipeline.material_runner_retirement import MaterialRunnerRetirementAttestation
from app.pipeline.windows_atomic_process import WindowsAtomicProcessResourceError
from app.pipeline.windows_overlapped_pipe_io import (
    CancelOutcome,
    Completion,
    FailureReason,
    LifecycleStage,
    NativeOutcome,
    OperationKind,
    OverlappedPipeError,
)
from app.pipeline.material_operation_control_protocol import (
    MaterialWorkerReady,
    encode_material_worker_ready,
)
from app.pipeline.material_operation_protocol import (
    MAX_REQUEST_BYTES,
    MaterialOperationBlocked,
    MaterialOperationRequest,
    MaterialOperationSecrets,
    MaterialWorkerResult,
    PexelsSecrets,
    StagedArtifact,
    StockSearchPayload,
    TerminationEvidence,
    decode_material_worker_result,
    encode_material_operation_request,
    encode_material_operation_secrets,
    encode_material_worker_result,
)


EXECUTION_ID = "123e4567-e89b-12d3-a456-426614174000"
REQUEST_SHA256 = "b" * 64
ROOT_CREATION_TIME = "2026-07-15T12:34:56.123456Z"
OTHER_ATTEMPT_ID = "123e4567-e89b-12d3-a456-426614174099"
CONTROL_ID = "123e4567-e89b-12d3-a456-426614174098"
JOB_INSTANCE_ID = "123e4567-e89b-12d3-a456-426614174097"
SECRET_CANARY = "stage-3b-secret-canary-never-write"


@dataclass(slots=True)
class _RunnerHarness:
    root: ApprovedPhysicalWriteRoot
    runner: ProcessMaterialOperationRunner
    prepared: list[PreparedMaterialAttempt] = field(default_factory=list)

    def track(self, value: PreparedMaterialAttempt) -> PreparedMaterialAttempt:
        self.prepared.append(value)
        return value


@pytest.fixture
def runner_harness(tmp_path: Path) -> Iterator[_RunnerHarness]:
    assert tmp_path.drive.casefold() == "d:"
    root = approve_physical_d_write_root(tmp_path)
    runner = ProcessMaterialOperationRunner(root=root)
    harness = _RunnerHarness(root=root, runner=runner)
    try:
        yield harness
    finally:
        for prepared in reversed(harness.prepared):
            for pin in reversed(prepared.pin_scope):
                pin.close()
        root.close()


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    assert type(value) is dict
    return value


def _assert_canonical_uuid(value: str) -> None:
    assert str(UUID(value)) == value


def _valid_run_inputs(
    prepared: PreparedMaterialAttempt,
    *,
    attempt_id: str | None = None,
) -> tuple[MaterialOperationRequest, MaterialOperationSecrets]:
    selected_attempt_id = attempt_id or prepared.attempt_id
    request = MaterialOperationRequest(
        protocol_version=1,
        execution_id=prepared.execution_id,
        attempt_id=selected_attempt_id,
        operation="pexels",
        payload=StockSearchPayload(query="short video workflow", per_page=10),
    )
    secrets = MaterialOperationSecrets(
        version=1,
        execution_id=prepared.execution_id,
        attempt_id=selected_attempt_id,
        operation="pexels",
        payload=PexelsSecrets(api_key=SECRET_CANARY),
    )
    return request, secrets


class _OwnedRestartMutex:
    def __init__(self) -> None:
        self.assertions = 0
        self.releases = 0

    def assert_owned(self) -> None:
        self.assertions += 1

    def release(self) -> None:
        self.releases += 1


def _restart_guard(prepared: PreparedMaterialAttempt) -> MaterialRestartOwnershipGuard:
    return MaterialRestartOwnershipGuard(
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        control_id=prepared.control_id,
        job_instance_id=prepared.job_instance_id,
        mutex=_OwnedRestartMutex(),
    )


def _checkpointed_terminated_proof(
    prepared: PreparedMaterialAttempt,
    termination: TerminationEvidence,
) -> MaterialRestartRetirementProof:
    return MaterialRestartRetirementProof(
        status="terminated",
        execution_id=prepared.execution_id,
        operation="pexels",
        attempt_id=prepared.attempt_id,
        execution_revision=3,
        final_payload_sha256="d" * 64,
        attempt_history_sha256="e" * 64,
        result_sha256=None,
        observed_at=termination.observed_at,
        checkpoint_sha256="f" * 64,
    )


def _close_recovered_runner(
    runner: ProcessMaterialOperationRunner,
    adopted: AdoptedMaterialAttempt | None,
) -> None:
    if adopted is not None:
        record = runner._records.get(id(adopted.prepared))
        if record is not None and not record.retired:
            for pin in reversed(record.pin_scope):
                pin.close()
            record.retired = True
    runner.close()


def _seal_terminated_attempt_for_restart(
    runner: ProcessMaterialOperationRunner,
    prepared: PreparedMaterialAttempt,
) -> tuple[TerminationEvidence, ReconciliationEvidence]:
    request, _secrets = _valid_run_inputs(prepared)
    request_bytes = encode_material_operation_request(request)
    request_receipt = write_durable_bytes(
        parent=prepared.directory,
        filename=runner_subject.REQUEST_FILENAME,
        payload=request_bytes,
        replace=False,
        max_bytes=MAX_REQUEST_BYTES,
    )
    runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=request_receipt.sha256,
    )
    runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
        root_pid=4242,
        root_creation_time=ROOT_CREATION_TIME,
    )
    termination = TerminationEvidence(
        platform="windows_job",
        root_pid=4242,
        exit_code=23,
        termination_method="job_terminate",
        active_processes=0,
        observed_at=ROOT_CREATION_TIME,
    )
    reconciliation = runner._commit_queried_empty_reconciliation(
        prepared,
        termination,
    )
    outcome = runner_subject.MaterialOperationOutcome(
        status="terminated",
        result=None,
        termination=termination,
    )
    runner._commit_termination_outcome(prepared, outcome)
    runner._records[id(prepared)].outcome = outcome
    return termination, reconciliation


def _canonical_sha256(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _seal_succeeded_publication_for_restart(
    runner: ProcessMaterialOperationRunner,
    prepared: PreparedMaterialAttempt,
    *,
    binding: MaterialRunnerClaimBinding,
) -> tuple[MaterialWorkerResult, TerminationEvidence, str, Path]:
    request, _secrets = _valid_run_inputs(prepared)
    request_receipt = write_durable_bytes(
        parent=prepared.directory,
        filename=runner_subject.REQUEST_FILENAME,
        payload=encode_material_operation_request(request),
        replace=False,
        max_bytes=MAX_REQUEST_BYTES,
    )
    phases = (
        (MaterialLaunchPhase.JOB_CONFIGURED, {"request_sha256": request_receipt.sha256}),
        (MaterialLaunchPhase.CREATE_INTENT, {}),
        (
            MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
            {"root_pid": 4242, "root_creation_time": ROOT_CREATION_TIME},
        ),
        (MaterialLaunchPhase.RESUME_INTENT, {}),
        (MaterialLaunchPhase.RESUMED_NO_SECRET, {}),
        (MaterialLaunchPhase.SECRET_SEND_INTENT, {}),
        (MaterialLaunchPhase.SECRET_SENT, {}),
        (MaterialLaunchPhase.READY, {}),
    )
    for phase, kwargs in phases:
        runner._advance_launch_descriptor(prepared, phase=phase, **kwargs)

    destination = prepared.directory.path / "payload" / "clip.mp4"
    destination.write_bytes(b"published-once")
    destination_stat = destination.stat()
    part_path = prepared.directory.path / "payload" / ".clip.mp4.part"
    part_path.write_bytes(b"published-once")
    part_stat = part_path.stat()
    part_path.unlink()
    registry_path = prepared.directory.path / "registry.json"
    registry_bytes = b'{"format":"registry-v1","entries":[]}\n'
    registry_path.write_bytes(registry_bytes)
    registry_stat = registry_path.stat()

    artifact = StagedArtifact(
        relative_path="payload/clip.mp4",
        media_kind="video",
        source_type="Pexels",
        source_url="https://example.com/clip.mp4",
        evidence_kind="direct_evidence",
        is_locally_generated=False,
        sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
        size_bytes=destination.stat().st_size,
    )
    result = MaterialWorkerResult(
        protocol_version=1,
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        operation="pexels",
        status="succeeded",
        selected_artifact=artifact.relative_path,
        artifacts=(artifact,),
        reason_code="",
        reason="",
    )
    termination = TerminationEvidence(
        platform="windows_job",
        root_pid=4242,
        exit_code=0,
        termination_method="natural",
        active_processes=0,
        observed_at="2026-07-15T12:35:00.000000Z",
    )
    runner._commit_queried_empty_reconciliation(prepared, termination)
    outcome = runner_subject.MaterialOperationOutcome(
        status="succeeded",
        result=result,
        termination=termination,
    )
    record = runner._records[id(prepared)]
    record.outcome = outcome
    write_durable_bytes(
        parent=prepared.directory,
        filename="result.json",
        payload=encode_material_worker_result(result),
        replace=False,
        max_bytes=runner_subject.MAX_RESULT_BYTES,
    )
    runner._commit_termination_outcome(prepared, outcome, allow_natural=True)

    published_result: dict[str, object] = {
        "kind": "material_execution_result",
        "success": True,
        "status": "已完成",
        "material_path": str(destination),
        "material_type": "video",
        "source_type": "Pexels",
        "reason": "",
        "evidence_kind": "direct_evidence",
        "source_url": "https://example.com/clip.mp4",
        "is_locally_generated": False,
        "rights_evidence": None,
    }
    completion_intent: dict[str, object] = {
        "format": "material-publication-completion-intent-v1",
        "claim_receipt": {
            "material_task_id": binding.material_task_id,
            "execution_id": prepared.execution_id,
            "attempt_id": prepared.attempt_id,
            "operation": "pexels",
            "control_id": prepared.control_id,
            "job_instance_id": prepared.job_instance_id,
            "reference_base_revision": binding.execution_revision - 1,
            "reference_base_payload_sha256": "a" * 64,
        },
        "worker_result_sha256": hashlib.sha256(
            encode_material_worker_result(result)
        ).hexdigest(),
        "published_result": published_result,
        "published_result_sha256": _canonical_sha256(published_result),
    }
    completion_intent["intent_sha256"] = _canonical_sha256(completion_intent)
    journal_payload = {
        "format": "material-publication-v1",
        "execution_id": prepared.execution_id,
        "attempt_id": prepared.attempt_id,
        "operation": "pexels",
        "phase": "committed",
        "attestation": "committed",
        "moves": [
            {
                "destination": str(destination),
                "part_path": str(part_path),
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "target_kind": "project",
                "ordinal": 0,
                "target_label": "all_materials",
                "owned": True,
                "part_identity": {
                    "device": int(part_stat.st_dev),
                    "inode": int(part_stat.st_ino),
                },
                "destination_identity": {
                    "device": int(destination_stat.st_dev),
                    "inode": int(destination_stat.st_ino),
                },
                "state": "move_applied",
            }
        ],
        "registry": {
            "path": str(registry_path),
            "old_sha256": None,
            "old_bytes_b64": None,
            "old_identity": None,
            "new_sha256": hashlib.sha256(registry_bytes).hexdigest(),
            "new_identity": {
                "device": int(registry_stat.st_dev),
                "inode": int(registry_stat.st_ino),
            },
            "state": "registry_replace_applied",
        },
        "completion_intent": completion_intent,
    }
    journal = write_durable_json(
        parent=prepared.directory,
        filename=runner_subject.PUBLICATION_JOURNAL_FILENAME,
        payload=journal_payload,
        replace=False,
        max_bytes=runner_subject.MAX_PUBLICATION_JOURNAL_BYTES,
    )
    return result, termination, journal.sha256, destination


def test_prepare_creates_separate_worker_and_controller_trees(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="official_capture",
        )
    )

    jobs = runner_harness.root.path / ".jobs"
    assert prepared.directory.path == jobs / prepared.attempt_id
    assert prepared.control_directory.path == (
        runner_harness.root.path / ".jobs-control" / prepared.control_id
    )
    assert prepared.directory.path not in prepared.control_directory.path.parents
    assert prepared.control_directory.path not in prepared.directory.path.parents
    assert {child.name for child in prepared.directory.path.iterdir()} == {
        "payload",
        "tmp",
        "profile",
    }
    assert {child.name for child in prepared.control_directory.path.iterdir()} == {
        "container.json"
    }

    for value in (
        prepared.attempt_id,
        prepared.control_id,
        prepared.job_instance_id,
    ):
        _assert_canonical_uuid(value)
    assert len(
        {
            EXECUTION_ID,
            prepared.attempt_id,
            prepared.control_id,
            prepared.job_instance_id,
        }
    ) == 4

    descriptor_bytes = (prepared.control_directory.path / "container.json").read_bytes()
    descriptor = _read_json(prepared.control_directory.path / "container.json")
    assert descriptor["protocol_version"] == 1
    assert descriptor["phase"] == "planned"
    assert descriptor["execution_id"] == EXECUTION_ID
    assert descriptor["attempt_id"] == prepared.attempt_id
    assert descriptor["control_id"] == prepared.control_id
    assert descriptor["job_instance_id"] == prepared.job_instance_id
    assert descriptor["operation"] == "official_capture"
    assert type(descriptor["controller_pid"]) is int
    assert descriptor["controller_pid"] > 0
    assert type(descriptor["controller_creation_time"]) is str
    assert descriptor["previous_phase"] is None
    assert descriptor["previous_descriptor_sha256"] is None
    assert descriptor["operation_may_have_started"] is False
    assert not {
        "request_sha256",
        "request_receipt",
        "root_pid",
        "root_creation_time",
        "root_identity",
    } & set(descriptor)
    assert prepared.container_hash == hashlib.sha256(descriptor_bytes).hexdigest()

    for pin in prepared.pin_scope:
        pin.verify()


def test_bind_claim_requires_exact_prepared_identity_and_blocks_unstarted_retirement(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    binding = MaterialRunnerClaimBinding(
        material_task_id="task-001",
        execution_id=prepared.execution_id,
        operation="pexels",
        attempt_id=prepared.attempt_id,
        control_id=prepared.control_id,
        job_instance_id=prepared.job_instance_id,
        execution_revision=1,
        payload_sha256="a" * 64,
    )

    runner_harness.runner.bind_claim(prepared, binding)
    assert runner_harness.runner.claim_binding(prepared) == binding
    with pytest.raises(MaterialOperationRunnerError, match="claim"):
        runner_harness.runner.retire_unstarted_attempt(prepared)
    with pytest.raises(MaterialOperationRunnerError, match="claim boundary"):
        runner_harness.runner.bind_claim(prepared, binding)


def test_bind_claim_rejects_foreign_attempt_identity(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    binding = MaterialRunnerClaimBinding(
        material_task_id="task-001",
        execution_id=prepared.execution_id,
        operation="pexels",
        attempt_id=OTHER_ATTEMPT_ID,
        control_id=prepared.control_id,
        job_instance_id=prepared.job_instance_id,
        execution_revision=1,
        payload_sha256="b" * 64,
    )
    with pytest.raises(MaterialOperationRunnerError, match="identities"):
        runner_harness.runner.bind_claim(prepared, binding)
    assert runner_harness.runner.claim_binding(prepared) is None


def test_run_claimed_binds_before_delegating_to_runner(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    binding = MaterialRunnerClaimBinding(
        material_task_id="task-001",
        execution_id=prepared.execution_id,
        operation="pexels",
        attempt_id=prepared.attempt_id,
        control_id=prepared.control_id,
        job_instance_id=prepared.job_instance_id,
        execution_revision=1,
        payload_sha256="c" * 64,
    )
    request, secrets = _valid_run_inputs(prepared)
    delegated: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> str:
        delegated["args"] = args
        delegated["kwargs"] = kwargs
        return "delegated"  # type: ignore[return-value]

    monkeypatch.setattr(runner_harness.runner, "run", fake_run)
    result = runner_harness.runner.run_claimed(
        prepared,
        request,
        binding=binding,
        secrets=secrets,
        startup_timeout_seconds=1.0,
        operation_timeout_seconds=2.0,
    )

    assert result == "delegated"
    assert delegated["args"][:2] == (prepared, request)
    assert runner_harness.runner.claim_binding(prepared) == binding

    # prepare_and_claim binds immediately after the DB CAS; run_claimed must
    # accept that same immutable receipt without attempting a second bind.
    assert (
        runner_harness.runner.run_claimed(
            prepared,
            request,
            binding=binding,
            secrets=secrets,
            startup_timeout_seconds=1.0,
            operation_timeout_seconds=2.0,
        )
        == "delegated"
    )


def test_restart_adoption_uses_completed_db_proof_and_retires_without_native_rehydrate(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    termination, reconciliation = _seal_terminated_attempt_for_restart(
        runner_harness.runner,
        prepared,
    )
    original_controller = decode_material_container_descriptor(
        (prepared.control_directory.path / runner_subject.CONTAINER_FILENAME).read_bytes()
    )
    for pin in reversed(prepared.pin_scope):
        pin.close()

    class _ExitedOps:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return True

        def create_job(self) -> object:
            raise AssertionError("restart adoption must not recreate a Job")

        def create_secret_pipe(self, **_kwargs: object) -> object:
            raise AssertionError("restart adoption must not recreate a pipe")

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitedOps())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    adopted: AdoptedMaterialAttempt | None = None
    try:
        adopted = recovered.adopt_durable_attempt(
            ownership_guard=_restart_guard(prepared),
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
            job_instance_id=prepared.job_instance_id,
            expected_operation="pexels",
        )
        assert adopted.binding is None
        assert adopted.reconciliation == reconciliation
        assert adopted.outcome is not None
        assert adopted.outcome.status == "terminated"
        assert adopted.native_release_basis == "queried_empty_dead_controller"
        assert adopted.descriptor == original_controller
        assert len(adopted.prepared.pin_scope) == 7
        for pin in adopted.prepared.pin_scope:
            pin.verify()

        record = recovered._records[id(adopted.prepared)]
        assert record.adopted_after_restart is True
        assert record.controller.pid == original_controller.controller_pid
        assert record.controller.creation_time == original_controller.controller_creation_time
        assert record.job_owner is None
        assert record.pipe_owner is None
        assert record.process_owner is None
        assert record.process_quarantine_owner is None
        assert record.completion_close_attempted is False
        assert record.completion_resources_closed is False
        assert record.pipe_close_attempted is False
        assert record.pipe_closed is False

        proof = MaterialRestartRetirementProof(
            status="terminated",
            execution_id=prepared.execution_id,
            operation="pexels",
            attempt_id=prepared.attempt_id,
            execution_revision=3,
            final_payload_sha256="d" * 64,
            attempt_history_sha256="e" * 64,
            result_sha256=None,
            observed_at=termination.observed_at,
        )
        recovered.retire_adopted_terminal_attempt(adopted, proof=proof)
        assert record.retired is True
        tombstone = _read_json(
            prepared.control_directory.path / runner_subject.RETIRED_FILENAME
        )
        assert tombstone["reason_code"] == "attempt_terminated_after_restart"
        assert tombstone["final_payload_sha256"] == proof.final_payload_sha256
        assert tombstone["attempt_history_sha256"] == proof.attempt_history_sha256
        assert tombstone["native_release_basis"] == adopted.native_release_basis
        recovered.close()
    finally:
        if not recovered._closed:
            if adopted is not None:
                record = recovered._records.get(id(adopted.prepared))
                if record is not None and not record.retired:
                    for pin in reversed(record.pin_scope):
                        pin.close()
                    record.retired = True
            recovered.close()


def test_completed_terminal_retirement_retry_adopts_exact_existing_tombstone(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    termination, _reconciliation = _seal_terminated_attempt_for_restart(
        runner_harness.runner,
        prepared,
    )
    proof = _checkpointed_terminated_proof(prepared, termination)
    for pin in reversed(prepared.pin_scope):
        pin.close()

    class _ExitedOps:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitedOps())
    first = ProcessMaterialOperationRunner(root=runner_harness.root)
    second: ProcessMaterialOperationRunner | None = None
    first_adopted: AdoptedMaterialAttempt | None = None
    second_adopted: AdoptedMaterialAttempt | None = None
    try:
        first_adopted = first.adopt_durable_attempt(
            ownership_guard=_restart_guard(prepared),
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
            job_instance_id=prepared.job_instance_id,
            expected_operation="pexels",
        )
        first_attestation = first.attest_completed_adopted_terminal_attempt(
            first_adopted,
            proof=proof,
        )
        assert type(first_attestation) is MaterialRunnerRetirementAttestation
        tombstone_path = (
            prepared.control_directory.path / runner_subject.RETIRED_FILENAME
        )
        original_bytes = tombstone_path.read_bytes()
        original = json.loads(original_bytes)
        first.close()

        second = ProcessMaterialOperationRunner(root=runner_harness.root)
        second_adopted = second.adopt_durable_attempt(
            ownership_guard=_restart_guard(prepared),
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
            job_instance_id=prepared.job_instance_id,
            expected_operation="pexels",
            retirement_proof=proof,
        )
        retried_attestation = second.attest_completed_adopted_terminal_attempt(
            second_adopted,
            proof=proof,
        )

        assert type(retried_attestation) is MaterialRunnerRetirementAttestation
        assert retried_attestation.release_basis == "dead_controller_after_tombstone"
        assert retried_attestation.tombstone_sha256 == hashlib.sha256(
            original_bytes
        ).hexdigest()
        assert retried_attestation.retired_at == original["retired_at"]
        assert tombstone_path.read_bytes() == original_bytes
        for pin in second_adopted.prepared.pin_scope:
            with pytest.raises(StoragePolicyError):
                pin.verify()
    finally:
        if second is not None and not second._closed:
            _close_recovered_runner(second, second_adopted)
        if not first._closed:
            _close_recovered_runner(first, first_adopted)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("checkpoint_sha256", "0" * 64),
        ("job_instance_id", JOB_INSTANCE_ID),
    ),
)
def test_completed_terminal_retirement_retry_rejects_tampered_tombstone(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: str,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    termination, _reconciliation = _seal_terminated_attempt_for_restart(
        runner_harness.runner,
        prepared,
    )
    proof = _checkpointed_terminated_proof(prepared, termination)
    for pin in reversed(prepared.pin_scope):
        pin.close()

    class _ExitedOps:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitedOps())
    first = ProcessMaterialOperationRunner(root=runner_harness.root)
    first_adopted: AdoptedMaterialAttempt | None = None
    second: ProcessMaterialOperationRunner | None = None
    try:
        first_adopted = first.adopt_durable_attempt(
            ownership_guard=_restart_guard(prepared),
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
            job_instance_id=prepared.job_instance_id,
            expected_operation="pexels",
        )
        first.attest_completed_adopted_terminal_attempt(first_adopted, proof=proof)
        first.close()

        tombstone_path = (
            prepared.control_directory.path / runner_subject.RETIRED_FILENAME
        )
        tampered = json.loads(tombstone_path.read_bytes())
        tampered[field] = replacement
        tampered_bytes = json.dumps(
            tampered,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        tombstone_path.write_bytes(tampered_bytes)

        second = ProcessMaterialOperationRunner(root=runner_harness.root)
        with pytest.raises(MaterialOperationRunnerError) as exc_info:
            second.adopt_durable_attempt(
                ownership_guard=_restart_guard(prepared),
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
                job_instance_id=prepared.job_instance_id,
                expected_operation="pexels",
                retirement_proof=proof,
            )

        assert exc_info.value.code == "restart_retirement_tombstone_mismatch"
        assert tombstone_path.read_bytes() == tampered_bytes
        assert second._records == {}
    finally:
        if second is not None and not second._closed:
            second.close()
        if not first._closed:
            _close_recovered_runner(first, first_adopted)


def test_restart_retirement_rejects_request_replaced_after_adoption(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    termination, _reconciliation = _seal_terminated_attempt_for_restart(
        runner_harness.runner,
        prepared,
    )
    for pin in reversed(prepared.pin_scope):
        pin.close()

    class _ExitedOps:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitedOps())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    adopted: AdoptedMaterialAttempt | None = None
    try:
        adopted = recovered.adopt_durable_attempt(
            ownership_guard=_restart_guard(prepared),
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
            job_instance_id=prepared.job_instance_id,
            expected_operation="pexels",
        )
        changed_request = MaterialOperationRequest(
            protocol_version=1,
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            operation="pexels",
            payload=StockSearchPayload(query="replaced after adoption", per_page=10),
        )
        write_durable_bytes(
            parent=adopted.prepared.directory,
            filename=runner_subject.REQUEST_FILENAME,
            payload=encode_material_operation_request(changed_request),
            replace=True,
            max_bytes=MAX_REQUEST_BYTES,
        )
        proof = MaterialRestartRetirementProof(
            status="terminated",
            execution_id=prepared.execution_id,
            operation="pexels",
            attempt_id=prepared.attempt_id,
            execution_revision=3,
            final_payload_sha256="d" * 64,
            attempt_history_sha256="e" * 64,
            result_sha256=None,
            observed_at=termination.observed_at,
        )

        with pytest.raises(MaterialOperationRunnerError, match="request bytes"):
            recovered.retire_adopted_terminal_attempt(adopted, proof=proof)
        assert not (
            prepared.control_directory.path / runner_subject.RETIRED_FILENAME
        ).exists()
    finally:
        if adopted is not None:
            record = recovered._records.get(id(adopted.prepared))
            if record is not None and not record.retired:
                for pin in reversed(record.pin_scope):
                    pin.close()
                record.retired = True
        recovered.close()


def test_restart_retirement_requires_active_claim_completion_to_advance(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    termination, _reconciliation = _seal_terminated_attempt_for_restart(
        runner_harness.runner,
        prepared,
    )
    binding = MaterialRunnerClaimBinding(
        material_task_id="task-001",
        execution_id=prepared.execution_id,
        operation="pexels",
        attempt_id=prepared.attempt_id,
        control_id=prepared.control_id,
        job_instance_id=prepared.job_instance_id,
        execution_revision=7,
        payload_sha256="c" * 64,
    )
    for pin in reversed(prepared.pin_scope):
        pin.close()

    class _ExitedOps:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitedOps())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    adopted: AdoptedMaterialAttempt | None = None
    try:
        adopted = recovered.adopt_durable_attempt(
            ownership_guard=_restart_guard(prepared),
            binding=binding,
            expected_operation="pexels",
        )
        stale_proof = MaterialRestartRetirementProof(
            status="terminated",
            execution_id=prepared.execution_id,
            operation="pexels",
            attempt_id=prepared.attempt_id,
            execution_revision=binding.execution_revision,
            final_payload_sha256="d" * 64,
            attempt_history_sha256="e" * 64,
            result_sha256=None,
            observed_at=termination.observed_at,
        )
        with pytest.raises(MaterialOperationRunnerError, match="did not advance"):
            recovered.retire_adopted_terminal_attempt(adopted, proof=stale_proof)

        unchanged_payload_proof = MaterialRestartRetirementProof(
            status="terminated",
            execution_id=prepared.execution_id,
            operation="pexels",
            attempt_id=prepared.attempt_id,
            execution_revision=binding.execution_revision + 1,
            final_payload_sha256=binding.payload_sha256,
            attempt_history_sha256="e" * 64,
            result_sha256=None,
            observed_at=termination.observed_at,
        )
        with pytest.raises(MaterialOperationRunnerError, match="payload did not change"):
            recovered.retire_adopted_terminal_attempt(
                adopted,
                proof=unchanged_payload_proof,
            )
        assert not (
            prepared.control_directory.path / runner_subject.RETIRED_FILENAME
        ).exists()
    finally:
        if adopted is not None:
            record = recovered._records.get(id(adopted.prepared))
            if record is not None and not record.retired:
                for pin in reversed(record.pin_scope):
                    pin.close()
                record.retired = True
        recovered.close()


def test_restart_adoption_does_not_return_unretirable_no_outcome_attempt(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    for pin in reversed(prepared.pin_scope):
        pin.close()

    class _ExitedOps:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitedOps())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(MaterialOperationRunnerError, match="failed or terminated"):
            recovered.adopt_durable_attempt(
                ownership_guard=_restart_guard(prepared),
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
                job_instance_id=prepared.job_instance_id,
                expected_operation="pexels",
            )
        assert recovered._records == {}
        assert not (
            prepared.control_directory.path / runner_subject.RETIRED_FILENAME
        ).exists()
    finally:
        recovered.close()


def test_restart_adoption_rejects_succeeded_outcome_without_bound_publishing_owner(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    _seal_terminated_attempt_for_restart(runner_harness.runner, prepared)
    for pin in reversed(prepared.pin_scope):
        pin.close()

    artifact = StagedArtifact(
        relative_path="payload/clip.mp4",
        media_kind="video",
        source_type="test",
        source_url="https://example.com/clip.mp4",
        evidence_kind="direct_evidence",
        is_locally_generated=False,
        sha256="a" * 64,
        size_bytes=1,
    )
    succeeded = runner_subject.MaterialOperationOutcome(
        status="succeeded",
        result=MaterialWorkerResult(
            protocol_version=1,
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            operation="pexels",
            status="succeeded",
            selected_artifact="payload/clip.mp4",
            artifacts=(artifact,),
            reason_code="",
            reason="",
        ),
        termination=TerminationEvidence(
            platform="windows_job",
            root_pid=4242,
            exit_code=0,
            termination_method="natural",
            active_processes=0,
            observed_at=ROOT_CREATION_TIME,
        ),
    )
    monkeypatch.setattr(
        runner_subject,
        "_read_adopted_outcome",
        lambda **_kwargs: (succeeded, "f" * 64),
    )

    class _ExitedOps:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitedOps())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(MaterialOperationRunnerError, match="bound publishing owner"):
            recovered.adopt_durable_attempt(
                ownership_guard=_restart_guard(prepared),
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
                job_instance_id=prepared.job_instance_id,
                expected_operation="pexels",
            )
        assert recovered._records == {}
    finally:
        recovered.close()


def test_restart_adopts_published_success_and_retires_without_copying_again(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    publishing_binding = MaterialRunnerClaimBinding(
        material_task_id="task-001",
        execution_id=prepared.execution_id,
        operation="pexels",
        attempt_id=prepared.attempt_id,
        control_id=prepared.control_id,
        job_instance_id=prepared.job_instance_id,
        execution_revision=8,
        payload_sha256="b" * 64,
    )
    result, termination, journal_sha256, destination = (
        _seal_succeeded_publication_for_restart(
            runner_harness.runner,
            prepared,
            binding=publishing_binding,
        )
    )
    before_identity = destination.stat().st_ino
    before_bytes = destination.read_bytes()
    before_files = sorted(
        str(path.relative_to(prepared.directory.path))
        for path in prepared.directory.path.rglob("*")
        if path.is_file()
    )
    for pin in reversed(prepared.pin_scope):
        pin.close()

    class _ExitedOps:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitedOps())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    adopted: AdoptedMaterialAttempt | None = None
    try:
        guard = _restart_guard(prepared)
        adopted = recovered.adopt_durable_attempt(
            ownership_guard=guard,
            binding=publishing_binding,
            expected_operation="pexels",
        )
        assert adopted.outcome is not None
        assert adopted.outcome.status == "succeeded"
        assert adopted.request.payload.query == "short video workflow"
        durable_journal = _read_json(
            prepared.directory.path / runner_subject.PUBLICATION_JOURNAL_FILENAME
        )
        intent = durable_journal["completion_intent"]
        assert isinstance(intent, dict)
        claim_receipt = intent["claim_receipt"]
        assert isinstance(claim_receipt, dict)

        proof = MaterialRestartPublishedRetirementProof(
            status="succeeded",
            execution_id=prepared.execution_id,
            operation="pexels",
            attempt_id=prepared.attempt_id,
            execution_revision=publishing_binding.execution_revision + 1,
            final_payload_sha256="c" * 64,
            attempt_history_sha256="d" * 64,
            publication_journal_sha256=journal_sha256,
            publication_intent_sha256=str(intent["intent_sha256"]),
            publication_result_sha256=str(intent["published_result_sha256"]),
            publication_worker_result_sha256=str(intent["worker_result_sha256"]),
            publication_reference_base_revision=int(
                claim_receipt["reference_base_revision"]
            ),
            publication_reference_base_payload_sha256=str(
                claim_receipt["reference_base_payload_sha256"]
            ),
            result_sha256=hashlib.sha256(
                encode_material_worker_result(result)
            ).hexdigest(),
            observed_at=termination.observed_at,
        )

        class _PublicationOwnership:
            request = adopted.request
            committed_journal_sha256 = journal_sha256

            def __init__(self):
                self.assertions = 0

            def assert_retained_ownership(self):
                self.assertions += 1

        publication_ownership = _PublicationOwnership()
        recovered.retire_adopted_published_attempt(
            adopted,
            proof=proof,
            publication_ownership=publication_ownership,
        )

        tombstone = _read_json(
            prepared.control_directory.path / runner_subject.RETIRED_FILENAME
        )
        assert tombstone["reason_code"] == "publication_committed_after_restart"
        assert tombstone["publication_journal_sha256"] == journal_sha256
        assert tombstone["completion_revision"] == proof.execution_revision
        assert destination.read_bytes() == before_bytes
        assert destination.stat().st_ino == before_identity
        assert sorted(
            str(path.relative_to(prepared.directory.path))
            for path in prepared.directory.path.rglob("*")
            if path.is_file()
        ) == before_files
        assert recovered._records[id(adopted.prepared)].retired is True
        assert publication_ownership.assertions >= 2
        for pin in adopted.prepared.pin_scope:
            with pytest.raises(StoragePolicyError):
                pin.verify()
    finally:
        if adopted is not None:
            record = recovered._records.get(id(adopted.prepared))
            if record is not None and not record.retired:
                for pin in reversed(record.pin_scope):
                    pin.close()
                record.retired = True
        recovered.close()


def test_completed_published_retirement_retry_adopts_exact_existing_tombstone(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    publishing_binding = MaterialRunnerClaimBinding(
        material_task_id="task-001",
        execution_id=prepared.execution_id,
        operation="pexels",
        attempt_id=prepared.attempt_id,
        control_id=prepared.control_id,
        job_instance_id=prepared.job_instance_id,
        execution_revision=8,
        payload_sha256="b" * 64,
    )
    result, termination, journal_sha256, _destination = (
        _seal_succeeded_publication_for_restart(
            runner_harness.runner,
            prepared,
            binding=publishing_binding,
        )
    )
    journal = _read_json(
        prepared.directory.path / runner_subject.PUBLICATION_JOURNAL_FILENAME
    )
    intent = journal["completion_intent"]
    assert isinstance(intent, dict)
    claim_receipt = intent["claim_receipt"]
    assert isinstance(claim_receipt, dict)
    proof = MaterialRestartPublishedRetirementProof(
        status="succeeded",
        execution_id=prepared.execution_id,
        operation="pexels",
        attempt_id=prepared.attempt_id,
        execution_revision=publishing_binding.execution_revision + 1,
        final_payload_sha256="c" * 64,
        attempt_history_sha256="d" * 64,
        publication_journal_sha256=journal_sha256,
        publication_intent_sha256=str(intent["intent_sha256"]),
        publication_result_sha256=str(intent["published_result_sha256"]),
        publication_worker_result_sha256=str(intent["worker_result_sha256"]),
        publication_reference_base_revision=int(
            claim_receipt["reference_base_revision"]
        ),
        publication_reference_base_payload_sha256=str(
            claim_receipt["reference_base_payload_sha256"]
        ),
        result_sha256=hashlib.sha256(
            encode_material_worker_result(result)
        ).hexdigest(),
        observed_at=termination.observed_at,
        checkpoint_sha256="f" * 64,
    )
    for pin in reversed(prepared.pin_scope):
        pin.close()

    class _ExitedOps:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return True

    class _PublicationOwnership:
        def __init__(self, adopted: AdoptedMaterialAttempt) -> None:
            self.request = adopted.request
            self.committed_journal_sha256 = journal_sha256
            self.assertions = 0

        def assert_retained_ownership(self) -> None:
            self.assertions += 1

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitedOps())
    first = ProcessMaterialOperationRunner(root=runner_harness.root)
    second: ProcessMaterialOperationRunner | None = None
    first_adopted: AdoptedMaterialAttempt | None = None
    second_adopted: AdoptedMaterialAttempt | None = None
    try:
        first_adopted = first.adopt_durable_attempt(
            ownership_guard=_restart_guard(prepared),
            binding=publishing_binding,
            expected_operation="pexels",
        )
        first_attestation = first.retire_adopted_published_attempt(
            first_adopted,
            proof=proof,
            publication_ownership=_PublicationOwnership(first_adopted),
        )
        assert type(first_attestation) is MaterialRunnerRetirementAttestation
        tombstone_path = (
            prepared.control_directory.path / runner_subject.RETIRED_FILENAME
        )
        original_bytes = tombstone_path.read_bytes()
        original = json.loads(original_bytes)
        first.close()

        second = ProcessMaterialOperationRunner(root=runner_harness.root)
        second_adopted = second.adopt_durable_attempt(
            ownership_guard=_restart_guard(prepared),
            binding=publishing_binding,
            expected_operation="pexels",
            retirement_proof=proof,
        )
        retried_attestation = second.retire_adopted_published_attempt(
            second_adopted,
            proof=proof,
            publication_ownership=_PublicationOwnership(second_adopted),
        )

        assert type(retried_attestation) is MaterialRunnerRetirementAttestation
        assert retried_attestation.release_basis == "dead_controller_after_tombstone"
        assert retried_attestation.tombstone_sha256 == hashlib.sha256(
            original_bytes
        ).hexdigest()
        assert retried_attestation.retired_at == original["retired_at"]
        assert tombstone_path.read_bytes() == original_bytes
        for pin in second_adopted.prepared.pin_scope:
            with pytest.raises(StoragePolicyError):
                pin.verify()
    finally:
        if second is not None and not second._closed:
            _close_recovered_runner(second, second_adopted)
        if not first._closed:
            _close_recovered_runner(first, first_adopted)


def test_restart_adoption_rejects_full_identity_mismatch_before_reconciliation_write(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    assert prepared.job_instance_id != JOB_INSTANCE_ID
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(
            MaterialOperationRunnerError,
            match="does not match the durable attempt",
        ):
            recovered.adopt_durable_attempt(
                ownership_guard=_restart_guard(prepared),
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
                job_instance_id=JOB_INSTANCE_ID,
                expected_operation="pexels",
            )
        assert recovered._records == {}
        assert not (
            prepared.control_directory.path / runner_subject.TERMINATION_FILENAME
        ).exists()
    finally:
        recovered.close()


def test_restart_adoption_rejects_live_controller_and_releases_all_new_pins(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    _seal_terminated_attempt_for_restart(runner_harness.runner, prepared)
    for pin in reversed(prepared.pin_scope):
        pin.close()

    class _LiveControllerOps:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return False

    monkeypatch.setattr(
        runner_subject,
        "_WINDOWS_RUNNER_OPS",
        _LiveControllerOps(),
    )
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(MaterialOperationRunnerError, match="still alive"):
            recovered.adopt_durable_attempt(
                ownership_guard=_restart_guard(prepared),
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
                job_instance_id=prepared.job_instance_id,
                expected_operation="pexels",
            )
        assert recovered._records == {}
        assert not (
            prepared.control_directory.path / runner_subject.RETIRED_FILENAME
        ).exists()
        recovered.close()
    finally:
        if not recovered._closed:
            recovered.close()


def test_restart_adoption_proves_dead_controller_before_io_drain_resolution_write(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    _termination, reconciliation = _seal_terminated_attempt_for_restart(
        runner_harness.runner,
        prepared,
    )
    runner_harness.runner._commit_quarantine(
        prepared,
        reason_code="io_drain_blocked",
        evidence=reconciliation,
    )
    for pin in reversed(prepared.pin_scope):
        pin.close()

    class _LiveControllerOps:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return False

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _LiveControllerOps())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(MaterialOperationRunnerError, match="still alive"):
            recovered.adopt_durable_attempt(
                ownership_guard=_restart_guard(prepared),
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
                job_instance_id=prepared.job_instance_id,
                expected_operation="pexels",
            )
        assert not (
            prepared.control_directory.path
            / runner_subject.QUARANTINE_RESOLUTION_FILENAME
        ).exists()
        assert recovered._records == {}
    finally:
        recovered.close()


def test_reconcile_planned_attempt_persists_honest_never_created_evidence(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )

    evidence = runner_harness.runner.reconcile(
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        control_id=prepared.control_id,
    )

    assert evidence.method == "never_created"
    assert evidence.observation_basis == "pre_create_journal"
    assert evidence.active_processes is None
    assert evidence.root_pid is None
    assert evidence.operation_may_have_started is False
    assert evidence.container_sha256 == prepared.container_hash
    persisted = _read_json(
        prepared.control_directory.path / "termination.json"
    )
    assert persisted == {
        "active_processes": None,
        "attempt_id": prepared.attempt_id,
        "container_sha256": prepared.container_hash,
        "execution_id": prepared.execution_id,
        "job_instance_id": prepared.job_instance_id,
        "method": "never_created",
        "observation_basis": "pre_create_journal",
        "observed_at": evidence.observed_at,
        "operation_may_have_started": False,
        "protocol_version": 1,
        "root_pid": None,
    }
    assert all(pin.verify() is None for pin in prepared.pin_scope)
    assert (
        runner_harness.runner.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        )
        == evidence
    )
    runner_harness.runner.retire_unstarted_attempt(prepared)


def test_reconcile_recovers_durable_evidence_without_in_memory_record(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    expected = runner_harness.runner.reconcile(
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        control_id=prepared.control_id,
    )

    class _ExitProbe:
        def wait_exact_process_exit(
            self,
            *,
            pid: int,
            creation_time: str,
            timeout_seconds: float,
        ) -> bool:
            assert timeout_seconds == 0.0
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitProbe())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        assert recovered.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        ) == expected
    finally:
        recovered.close()

    runner_harness.runner.retire_unstarted_attempt(prepared)


def test_reconcile_restart_commits_never_created_from_planned_tip(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    assert not (prepared.control_directory.path / "termination.json").exists()

    class _ExitProbe:
        def wait_exact_process_exit(
            self,
            *,
            pid: int,
            creation_time: str,
            timeout_seconds: float,
        ) -> bool:
            assert timeout_seconds == 0.0
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitProbe())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        evidence = recovered.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        )
    finally:
        recovered.close()

    assert evidence.method == "never_created"
    assert evidence.observation_basis == "pre_create_journal"
    assert evidence.container_sha256 == prepared.container_hash
    assert (prepared.control_directory.path / "termination.json").is_file()
    runner_harness.runner.retire_unstarted_attempt(prepared)


def test_reconcile_restart_blocks_live_planned_controller(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )

    class _ExitProbe:
        def wait_exact_process_exit(
            self,
            *,
            pid: int,
            creation_time: str,
            timeout_seconds: float,
        ) -> bool:
            assert timeout_seconds == 0.0
            return False

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitProbe())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(MaterialOperationRunnerError) as exc_info:
            recovered.reconcile(
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
            )
        assert exc_info.value.code == "reconciliation_blocked"
        assert not (
            prepared.control_directory.path / runner_subject.TERMINATION_FILENAME
        ).exists()
    finally:
        recovered.close()
    runner_harness.runner.retire_unstarted_attempt(prepared)


def test_reconcile_restart_rejects_descriptor_rewrite_before_new_journal(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )

    class _ExitProbe:
        def wait_exact_process_exit(
            self,
            *,
            pid: int,
            creation_time: str,
            timeout_seconds: float,
        ) -> bool:
            runner_harness.runner._advance_launch_descriptor(
                prepared,
                phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
                root_pid=4242,
                root_creation_time=ROOT_CREATION_TIME,
            )
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitProbe())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(MaterialOperationRunnerError) as exc_info:
            recovered.reconcile(
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
            )
        assert exc_info.value.code == "reconciliation_changed"
    finally:
        recovered.close()
    assert not (
        prepared.control_directory.path / runner_subject.TERMINATION_FILENAME
    ).exists()


def test_reconcile_restart_rejects_forged_root_and_phase_evidence(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    write_durable_json(
        parent=prepared.control_directory,
        filename="termination.json",
        payload={
            "protocol_version": 1,
            "execution_id": prepared.execution_id,
            "attempt_id": prepared.attempt_id,
            "job_instance_id": prepared.job_instance_id,
            "method": "queried_empty",
            "observation_basis": "queried_job",
            "active_processes": 0,
            "root_pid": 999,
            "operation_may_have_started": True,
            "observed_at": ROOT_CREATION_TIME,
            "container_sha256": prepared.container_hash,
        },
        replace=False,
        max_bytes=runner_subject.MAX_TERMINATION_BYTES,
    )

    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(MaterialOperationRunnerError) as exc_info:
            recovered.reconcile(
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
            )
        assert exc_info.value.code == "reconciliation_changed"
    finally:
        recovered.close()

    runner_harness.runner.retire_unstarted_attempt(prepared)


def test_reconcile_restart_blocks_unresolved_close_quarantine(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    evidence = runner_harness.runner.reconcile(
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        control_id=prepared.control_id,
    )
    write_durable_json(
        parent=prepared.control_directory,
        filename="quarantine.json",
        payload={
            "protocol_version": 1,
            "state": "blocked",
            "reason_code": "close_unknown",
            "execution_id": prepared.execution_id,
            "attempt_id": prepared.attempt_id,
            "control_id": prepared.control_id,
            "job_instance_id": prepared.job_instance_id,
            "phase": "planned",
            "operation_may_have_started": False,
            "container_sha256": prepared.container_hash,
            "termination": runner_subject._reconciliation_to_mapping(evidence),
            "quarantined_at": ROOT_CREATION_TIME,
        },
        replace=False,
        max_bytes=runner_subject.MAX_QUARANTINE_BYTES,
    )

    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(MaterialOperationRunnerError) as exc_info:
            recovered.reconcile(
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
            )
        assert exc_info.value.code == "reconciliation_blocked"
    finally:
        recovered.close()

    runner_harness.runner.retire_unstarted_attempt(prepared)


def test_reconcile_restart_rejects_missing_or_mismatched_io_drain_resolution(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    evidence = runner_harness.runner.reconcile(
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        control_id=prepared.control_id,
    )
    quarantine_receipt = write_durable_json(
        parent=prepared.control_directory,
        filename=runner_subject.QUARANTINE_FILENAME,
        payload={
            "protocol_version": 1,
            "state": "blocked",
            "reason_code": "io_drain_blocked",
            "execution_id": prepared.execution_id,
            "attempt_id": prepared.attempt_id,
            "control_id": prepared.control_id,
            "job_instance_id": prepared.job_instance_id,
            "phase": "planned",
            "operation_may_have_started": False,
            "container_sha256": prepared.container_hash,
            "termination": runner_subject._reconciliation_to_mapping(evidence),
            "quarantined_at": ROOT_CREATION_TIME,
        },
        replace=False,
        max_bytes=runner_subject.MAX_QUARANTINE_BYTES,
    )

    missing = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(MaterialOperationRunnerError) as exc_info:
            missing.reconcile(
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
            )
        assert exc_info.value.code == "reconciliation_blocked"
    finally:
        missing.close()

    termination_hash = hashlib.sha256(
        (prepared.control_directory.path / "termination.json").read_bytes()
    ).hexdigest()
    write_durable_json(
        parent=prepared.control_directory,
        filename=runner_subject.QUARANTINE_RESOLUTION_FILENAME,
        payload={
            "protocol_version": 1,
            "state": "resolved",
            "execution_id": prepared.execution_id,
            "attempt_id": prepared.attempt_id,
            "control_id": prepared.control_id,
            "job_instance_id": prepared.job_instance_id,
            "quarantine_sha256": "f" * 64,
            "container_sha256": prepared.container_hash,
            "termination_sha256": termination_hash,
            "resolved_at": ROOT_CREATION_TIME,
        },
        replace=False,
        max_bytes=runner_subject.MAX_QUARANTINE_RESOLUTION_BYTES,
    )
    assert quarantine_receipt.sha256 != "f" * 64

    mismatched = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(MaterialOperationRunnerError) as exc_info:
            mismatched.reconcile(
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
            )
        assert exc_info.value.code == "reconciliation_blocked"
    finally:
        mismatched.close()

    runner_harness.runner.retire_unstarted_attempt(prepared)


def test_reconcile_restart_requires_dead_controller_for_job_configured(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )

    class _ExitProbe:
        exited = False

        def wait_exact_process_exit(
            self,
            *,
            pid: int,
            creation_time: str,
            timeout_seconds: float,
        ) -> bool:
            return self.exited

    probe = _ExitProbe()
    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", probe)
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(MaterialOperationRunnerError) as exc_info:
            recovered.reconcile(
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
            )
        assert exc_info.value.code == "reconciliation_blocked"
        probe.exited = True
        evidence = recovered.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        )
        assert evidence.method == "never_created"
        assert evidence.active_processes is None
        probe.exited = False
        with pytest.raises(MaterialOperationRunnerError) as exc_info:
            recovered.reconcile(
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
            )
        assert exc_info.value.code == "reconciliation_blocked"
    finally:
        recovered.close()


def test_reconcile_restart_records_owner_exit_inference_after_create_intent(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )

    class _ExitProbe:
        def wait_exact_process_exit(
            self,
            *,
            pid: int,
            creation_time: str,
            timeout_seconds: float,
        ) -> bool:
            assert timeout_seconds == 0.0
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitProbe())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        evidence = recovered.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        )
        assert evidence.method == "owner_exit_kill_on_close"
        assert evidence.observation_basis == "controller_exit_last_handle_inference"
        assert evidence.active_processes is None
        assert evidence.root_pid is None
    finally:
        recovered.close()


def test_reconcile_restart_owner_exit_resolves_prior_io_drain_quarantine(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._commit_quarantine(
        prepared,
        reason_code="io_drain_blocked",
        evidence=None,
    )

    class _ExitProbe:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitProbe())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        evidence = recovered.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        )
        assert evidence.method == "owner_exit_kill_on_close"
        assert (
            prepared.control_directory.path
            / runner_subject.QUARANTINE_RESOLUTION_FILENAME
        ).is_file()
        assert recovered.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        ) == evidence
    finally:
        recovered.close()


def test_reconcile_restart_queried_empty_resolves_missing_io_drain_resolution(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
        root_pid=4242,
        root_creation_time=ROOT_CREATION_TIME,
    )
    termination = TerminationEvidence(
        platform="windows_job",
        root_pid=4242,
        exit_code=23,
        termination_method="job_terminate",
        active_processes=0,
        observed_at=ROOT_CREATION_TIME,
    )
    evidence = runner_harness.runner._commit_queried_empty_reconciliation(
        prepared,
        termination,
    )
    runner_harness.runner._commit_quarantine(
        prepared,
        reason_code="io_drain_blocked",
        evidence=evidence,
    )

    class _ExitProbe:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitProbe())

    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        assert recovered.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        ) == evidence
        assert (
            prepared.control_directory.path
            / runner_subject.QUARANTINE_RESOLUTION_FILENAME
        ).is_file()
        assert recovered.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        ) == evidence
    finally:
        recovered.close()


def test_reconcile_restart_queried_empty_does_not_resolve_while_controller_lives(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
        root_pid=4242,
        root_creation_time=ROOT_CREATION_TIME,
    )
    termination = TerminationEvidence(
        platform="windows_job",
        root_pid=4242,
        exit_code=23,
        termination_method="job_terminate",
        active_processes=0,
        observed_at=ROOT_CREATION_TIME,
    )
    evidence = runner_harness.runner._commit_queried_empty_reconciliation(
        prepared,
        termination,
    )
    runner_harness.runner._commit_quarantine(
        prepared,
        reason_code="io_drain_blocked",
        evidence=evidence,
    )

    class _LiveProbe:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return False

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _LiveProbe())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(MaterialOperationRunnerError) as exc_info:
            recovered.reconcile(
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
            )
        assert exc_info.value.code == "reconciliation_blocked"
        assert not (
            prepared.control_directory.path
            / runner_subject.QUARANTINE_RESOLUTION_FILENAME
        ).exists()
    finally:
        recovered.close()


def test_cached_queried_empty_reconcile_retries_missing_io_drain_resolution(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
        root_pid=4242,
        root_creation_time=ROOT_CREATION_TIME,
    )
    termination = TerminationEvidence(
        platform="windows_job",
        root_pid=4242,
        exit_code=23,
        termination_method="job_terminate",
        active_processes=0,
        observed_at=ROOT_CREATION_TIME,
    )
    evidence = runner_harness.runner._commit_queried_empty_reconciliation(
        prepared,
        termination,
    )
    runner_harness.runner._commit_quarantine(
        prepared,
        reason_code="io_drain_blocked",
        evidence=evidence,
    )
    record = runner_harness.runner._records[id(prepared)]

    class _ExitProbe:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return True

    fail_resolution = True
    real_write_json = runner_subject.write_durable_json

    def fail_first_resolution(**kwargs: object) -> object:
        nonlocal fail_resolution
        if (
            fail_resolution
            and kwargs["filename"] == runner_subject.QUARANTINE_RESOLUTION_FILENAME
        ):
            fail_resolution = False
            raise OSError("injected cached resolution write failure")
        return real_write_json(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitProbe())
    monkeypatch.setattr(runner_subject, "write_durable_json", fail_first_resolution)
    with pytest.raises(OSError):
        runner_harness.runner._commit_quarantine_resolution(record)
    assert not (
        prepared.control_directory.path
        / runner_subject.QUARANTINE_RESOLUTION_FILENAME
    ).exists()

    recovered = runner_harness.runner.reconcile(
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        control_id=prepared.control_id,
    )
    assert recovered == evidence
    assert (
        prepared.control_directory.path
        / runner_subject.QUARANTINE_RESOLUTION_FILENAME
    ).is_file()
    (prepared.control_directory.path / runner_subject.QUARANTINE_FILENAME).unlink()
    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        )
    assert exc_info.value.code == "reconciliation_changed"


def test_reconcile_restart_owner_exit_waits_for_exact_root_identity(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
        root_pid=4242,
        root_creation_time=ROOT_CREATION_TIME,
    )
    class _ExitProbe:
        calls: list[tuple[int, float]] = []

        def wait_exact_process_exit(
            self,
            *,
            pid: int,
            creation_time: str,
            timeout_seconds: float,
        ) -> bool:
            self.calls.append((pid, timeout_seconds))
            return True

    probe = _ExitProbe()
    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", probe)
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        evidence = recovered.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        )
        assert evidence.method == "owner_exit_kill_on_close"
        assert evidence.root_pid == 4242
        descriptor = decode_material_container_descriptor(
            (prepared.control_directory.path / "container.json").read_bytes()
        )
        assert probe.calls[0] == (descriptor.controller_pid, 0.0)
        assert probe.calls[1] == (4242, 10.0)
    finally:
        recovered.close()


def test_reconcile_restart_rejects_descriptor_rewrite_during_exit_wait(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
        root_pid=4242,
        root_creation_time=ROOT_CREATION_TIME,
    )
    evidence = ReconciliationEvidence(
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        job_instance_id=prepared.job_instance_id,
        method="owner_exit_kill_on_close",
        observation_basis="controller_exit_last_handle_inference",
        active_processes=None,
        root_pid=4242,
        operation_may_have_started=False,
        observed_at=ROOT_CREATION_TIME,
        container_sha256=prepared.container_hash,
    )
    write_durable_json(
        parent=prepared.control_directory,
        filename=runner_subject.TERMINATION_FILENAME,
        payload=runner_subject._reconciliation_to_mapping(evidence),
        replace=False,
        max_bytes=runner_subject.MAX_TERMINATION_BYTES,
    )

    class _ExitProbe:
        calls = 0

        def wait_exact_process_exit(
            self,
            *,
            pid: int,
            creation_time: str,
            timeout_seconds: float,
        ) -> bool:
            self.calls += 1
            if self.calls == 2:
                # Simulate a controller-side descriptor advancement while the
                # restart reconciler is waiting for the exact root identity.
                runner_harness.runner._advance_launch_descriptor(
                    prepared,
                    phase=MaterialLaunchPhase.RESUME_INTENT,
                )
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitProbe())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(MaterialOperationRunnerError) as exc_info:
            recovered.reconcile(
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
            )
        assert exc_info.value.code == "reconciliation_changed"
    finally:
        recovered.close()

    assert _read_json(
        prepared.control_directory.path / runner_subject.TERMINATION_FILENAME
    ) == runner_subject._reconciliation_to_mapping(evidence)


def test_exact_process_exit_compares_creation_time_as_an_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Handle:
        closed = False

        def Close(self) -> None:
            self.closed = True

    handle = _Handle()

    class _Api:
        @staticmethod
        def OpenProcess(access: int, inherit: bool, pid: int) -> object:
            return handle

    class _Process:
        @staticmethod
        def GetProcessId(value: object) -> int:
            return 4242

        @staticmethod
        def GetProcessTimes(value: object) -> dict[str, object]:
            return {
                "CreationTime": datetime(
                    2026,
                    7,
                    15,
                    12,
                    34,
                    56,
                    123456,
                    tzinfo=timezone.utc,
                )
            }

    class _Event:
        WAIT_OBJECT_0 = 0
        WAIT_TIMEOUT = 258

        @staticmethod
        def WaitForSingleObject(value: object, timeout: int) -> int:
            return _Event.WAIT_TIMEOUT

    monkeypatch.setattr(runner_subject, "win32api", _Api())
    monkeypatch.setattr(runner_subject, "win32con", object())
    monkeypatch.setattr(runner_subject, "win32process", _Process())
    monkeypatch.setattr(runner_subject, "win32event", _Event())
    assert (
        runner_subject._wait_exact_process_exit(
            pid=4242,
            creation_time="2026-07-15T20:34:56.123456+08:00",
            timeout_seconds=0.0,
        )
        is False
    )
    assert handle.closed is True


@pytest.mark.parametrize("winerror", [87, 1168])
def test_exact_process_exit_accepts_only_missing_pid_open_errors(
    monkeypatch: pytest.MonkeyPatch,
    winerror: int,
) -> None:
    class _MissingProcessError(Exception):
        def __init__(self) -> None:
            self.winerror = winerror

    class _Api:
        @staticmethod
        def OpenProcess(access: int, inherit: bool, pid: int) -> object:
            raise _MissingProcessError()

    monkeypatch.setattr(runner_subject, "win32api", _Api())
    monkeypatch.setattr(runner_subject, "win32con", object())
    monkeypatch.setattr(runner_subject, "win32process", object())
    monkeypatch.setattr(runner_subject, "win32event", object())
    assert runner_subject._wait_exact_process_exit(
        pid=4242,
        creation_time=ROOT_CREATION_TIME,
        timeout_seconds=0.0,
    )


def test_exact_process_exit_blocks_access_denied_and_handle_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WinError(Exception):
        def __init__(self, winerror: int) -> None:
            self.winerror = winerror

    class _DeniedApi:
        @staticmethod
        def OpenProcess(access: int, inherit: bool, pid: int) -> object:
            raise _WinError(5)

    monkeypatch.setattr(runner_subject, "win32api", _DeniedApi())
    monkeypatch.setattr(runner_subject, "win32con", object())
    monkeypatch.setattr(runner_subject, "win32process", object())
    monkeypatch.setattr(runner_subject, "win32event", object())
    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_subject._wait_exact_process_exit(
            pid=4242,
            creation_time=ROOT_CREATION_TIME,
            timeout_seconds=0.0,
        )
    assert exc_info.value.code == "process_exit_unavailable"

    class _CloseFailHandle:
        def Close(self) -> None:
            raise OSError("injected handle close failure")

    class _Api:
        @staticmethod
        def OpenProcess(access: int, inherit: bool, pid: int) -> object:
            return _CloseFailHandle()

    class _Process:
        @staticmethod
        def GetProcessId(value: object) -> int:
            return 4242

        @staticmethod
        def GetProcessTimes(value: object) -> dict[str, object]:
            return {
                "CreationTime": datetime.fromisoformat(
                    ROOT_CREATION_TIME.replace("Z", "+00:00")
                )
            }

    class _Event:
        WAIT_OBJECT_0 = 0
        WAIT_TIMEOUT = 258

        @staticmethod
        def WaitForSingleObject(value: object, timeout: int) -> int:
            return _Event.WAIT_TIMEOUT

    monkeypatch.setattr(runner_subject, "win32api", _Api())
    monkeypatch.setattr(runner_subject, "win32process", _Process())
    monkeypatch.setattr(runner_subject, "win32event", _Event())
    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_subject._wait_exact_process_exit(
            pid=4242,
            creation_time=ROOT_CREATION_TIME,
            timeout_seconds=0.0,
        )
    assert exc_info.value.code == "process_exit_close_unknown"


def test_prepare_reuses_only_approved_existing_jobs_layout(tmp_path: Path) -> None:
    jobs = tmp_path / ".jobs"
    jobs.mkdir()
    controls = tmp_path / ".jobs-control"
    controls.mkdir()
    root = approve_physical_d_write_root(tmp_path)
    runner = ProcessMaterialOperationRunner(root=root)
    prepared: PreparedMaterialAttempt | None = None
    try:
        prepared = runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
        assert prepared.directory.path.parent == jobs
        assert prepared.control_directory.path.parent == controls
        runner.retire_unstarted_attempt(prepared)
    finally:
        if prepared is not None:
            for pin in reversed(prepared.pin_scope):
                pin.close()
        root.close()


def test_two_prepared_attempts_never_reuse_ids_or_directories(
    runner_harness: _RunnerHarness,
) -> None:
    first = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    second = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pixabay",
        )
    )

    assert first.attempt_id != second.attempt_id
    assert first.control_id != second.control_id
    assert first.job_instance_id != second.job_instance_id
    assert first.directory.identity != second.directory.identity
    assert first.control_directory.identity != second.control_directory.identity
    runner_harness.runner.retire_unstarted_attempt(first)
    runner_harness.runner.retire_unstarted_attempt(second)


def test_retire_unstarted_writes_only_controller_tombstone_then_releases_pins(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="info_motion",
        )
    )

    runner_harness.runner.retire_unstarted_attempt(prepared)

    retired = _read_json(prepared.control_directory.path / "retired.json")
    assert retired["state"] == "retired"
    assert retired["reason_code"] == "claim_not_acquired"
    assert retired["execution_id"] == EXECUTION_ID
    assert retired["attempt_id"] == prepared.attempt_id
    assert retired["control_id"] == prepared.control_id
    assert retired["job_instance_id"] == prepared.job_instance_id
    assert retired["container_sha256"] == prepared.container_hash
    assert prepared.directory.path.is_dir()
    assert prepared.control_directory.path.is_dir()
    assert not (prepared.directory.path / "retired.json").exists()
    for pin in prepared.pin_scope:
        with pytest.raises(StoragePolicyError) as exc_info:
            pin.verify()
        assert exc_info.value.code == "storage_pin_closed"
    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner.retire_unstarted_attempt(prepared)
    assert exc_info.value.code == "attempt_already_retired"


@pytest.mark.parametrize(
    ("execution_id", "operation"),
    [
        ("not-a-uuid", "pexels"),
        (EXECUTION_ID.upper(), "pexels"),
        (EXECUTION_ID, "unknown"),
        (EXECUTION_ID, "pexels "),
    ],
)
def test_prepare_rejects_invalid_identity_before_creating_jobs(
    runner_harness: _RunnerHarness,
    execution_id: str,
    operation: str,
) -> None:
    with pytest.raises(MaterialOperationRunnerError):
        runner_harness.runner.prepare_attempt(
            execution_id=execution_id,
            operation=operation,  # type: ignore[arg-type]
        )

    assert not (runner_harness.root.path / ".jobs").exists()


@pytest.mark.parametrize(
    "raw_root",
    [r"C:\never-touch", r"\\server\share\never-touch", r"relative\never-touch"],
)
def test_runner_rejects_non_physical_d_root_before_any_mutation(raw_root: str) -> None:
    with pytest.raises(StoragePolicyError):
        ProcessMaterialOperationRunner(root=Path(raw_root))


def test_jobs_path_collision_with_file_fails_closed(
    runner_harness: _RunnerHarness,
) -> None:
    (runner_harness.root.path / ".jobs").write_text("collision", encoding="utf-8")

    with pytest.raises(StoragePolicyError):
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="coverr",
        )

    assert (runner_harness.root.path / ".jobs").read_text(encoding="utf-8") == "collision"


@pytest.mark.parametrize("collision_kind", ["attempt", "control"])
def test_uuid_directory_collision_fails_closed_without_retry(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
    collision_kind: str,
) -> None:
    attempt_id = "123e4567-e89b-12d3-a456-426614174010"
    control_id = "123e4567-e89b-12d3-a456-426614174011"
    job_instance_id = "123e4567-e89b-12d3-a456-426614174012"
    jobs = runner_harness.root.path / ".jobs"
    controls = runner_harness.root.path / ".jobs-control"
    jobs.mkdir()
    controls.mkdir()
    if collision_kind == "attempt":
        (jobs / attempt_id).mkdir()
    else:
        (controls / control_id).mkdir()

    generated = iter(
        [UUID(attempt_id), UUID(control_id), UUID(job_instance_id)]
    )
    calls: list[str] = []

    def next_uuid() -> UUID:
        value = next(generated)
        calls.append(str(value))
        return value

    monkeypatch.setattr(runner_subject, "uuid4", next_uuid)

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )

    assert exc_info.value.code == "exclusive_directory_collision"
    assert calls == [attempt_id, control_id, job_instance_id]
    assert not any(path.name == "container.json" for path in controls.rglob("*"))
    if collision_kind == "control":
        # The fresh attempt is retained as evidence; production code never
        # performs string-path deletion after releasing its pin.
        assert (jobs / attempt_id).is_dir()
    assert {path.name for path in jobs.iterdir()} <= {attempt_id}
    assert {path.name for path in controls.iterdir()} <= {control_id}


def test_retire_rejects_changed_planned_descriptor_and_keeps_pins(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="official_capture",
        )
    )
    write_durable_json(
        parent=prepared.control_directory,
        filename="container.json",
        payload={"phase": "tampered"},
        replace=True,
        max_bytes=64 * 1024,
    )

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner.retire_unstarted_attempt(prepared)

    assert exc_info.value.code == "descriptor_changed"
    assert not (prepared.control_directory.path / "retired.json").exists()
    for pin in prepared.pin_scope:
        pin.verify()


def test_prepared_attempt_uses_only_approved_pinned_directory_objects(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pixabay",
        )
    )

    assert isinstance(prepared.directory, ApprovedPhysicalDirectory)
    assert isinstance(prepared.control_directory, ApprovedPhysicalDirectory)
    assert prepared.directory in prepared.pin_scope
    assert prepared.control_directory in prepared.pin_scope
    assert len({id(pin) for pin in prepared.pin_scope}) == len(prepared.pin_scope)
    assert all(pin.path.drive.casefold() == "d:" for pin in prepared.pin_scope)
    runner_harness.runner.retire_unstarted_attempt(prepared)


def test_planned_descriptor_decodes_to_typed_first_phase(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="official_capture",
        )
    )

    descriptor = decode_material_container_descriptor(
        (prepared.control_directory.path / "container.json").read_bytes()
    )

    assert descriptor.phase is MaterialLaunchPhase.PLANNED
    assert descriptor.previous_phase is None
    assert descriptor.previous_descriptor_sha256 is None
    assert descriptor.request_sha256 is None
    assert descriptor.root_pid is None
    assert descriptor.root_creation_time is None
    assert descriptor.operation_may_have_started is False


def test_all_nine_launch_phases_form_an_exact_durable_hash_chain(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    assert MATERIAL_LAUNCH_PHASES == (
        MaterialLaunchPhase.PLANNED,
        MaterialLaunchPhase.JOB_CONFIGURED,
        MaterialLaunchPhase.CREATE_INTENT,
        MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
        MaterialLaunchPhase.RESUME_INTENT,
        MaterialLaunchPhase.RESUMED_NO_SECRET,
        MaterialLaunchPhase.SECRET_SEND_INTENT,
        MaterialLaunchPhase.SECRET_SENT,
        MaterialLaunchPhase.READY,
    )

    previous_hash = prepared.container_hash
    previous_phase = MaterialLaunchPhase.PLANNED
    for phase in MATERIAL_LAUNCH_PHASES[1:]:
        kwargs: dict[str, object] = {"request_sha256": REQUEST_SHA256}
        if MATERIAL_LAUNCH_PHASES.index(phase) >= MATERIAL_LAUNCH_PHASES.index(
            MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED
        ):
            # The method accepts the exact root identity on and after creation;
            # later transitions must not change it.
            kwargs.update(root_pid=4242, root_creation_time=ROOT_CREATION_TIME)
        current_hash = runner_harness.runner._advance_launch_descriptor(
            prepared,
            phase=phase,
            **kwargs,  # type: ignore[arg-type]
        )
        raw = (prepared.control_directory.path / "container.json").read_bytes()
        descriptor = decode_material_container_descriptor(raw)
        assert encode_material_container_descriptor(descriptor) == raw
        assert descriptor.phase is phase
        assert descriptor.previous_phase is previous_phase
        assert descriptor.previous_descriptor_sha256 == previous_hash
        assert hashlib.sha256(raw).hexdigest() == current_hash
        assert prepared.container_hash == current_hash
        assert descriptor.request_sha256 == REQUEST_SHA256
        assert descriptor.job_handle_inheritable is False
        if MATERIAL_LAUNCH_PHASES.index(phase) < MATERIAL_LAUNCH_PHASES.index(
            MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED
        ):
            assert descriptor.root_pid is None
            assert descriptor.root_creation_time is None
            assert descriptor.job_active_processes_after_create is None
        else:
            assert descriptor.root_pid == 4242
            assert descriptor.root_creation_time == ROOT_CREATION_TIME
            assert descriptor.job_active_processes_after_create == 1
        assert descriptor.operation_may_have_started is (
            MATERIAL_LAUNCH_PHASES.index(phase)
            >= MATERIAL_LAUNCH_PHASES.index(MaterialLaunchPhase.SECRET_SEND_INTENT)
        )
        previous_hash = current_hash
        previous_phase = phase

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner.retire_unstarted_attempt(prepared)
    assert exc_info.value.code == "claim_already_acquired"


@pytest.mark.parametrize(
    "phase",
    [
        MaterialLaunchPhase.PLANNED,
        MaterialLaunchPhase.CREATE_INTENT,
        MaterialLaunchPhase.READY,
        "unknown",
    ],
)
def test_descriptor_rejects_repeat_skip_and_unknown_transitions_without_write(
    runner_harness: _RunnerHarness,
    phase: object,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pixabay",
        )
    )
    before = (prepared.control_directory.path / "container.json").read_bytes()

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner._advance_launch_descriptor(
            prepared,
            phase=phase,  # type: ignore[arg-type]
            request_sha256=REQUEST_SHA256,
        )

    assert exc_info.value.code == "descriptor_transition_invalid"
    assert (prepared.control_directory.path / "container.json").read_bytes() == before
    assert prepared.container_hash == hashlib.sha256(before).hexdigest()
    runner_harness.runner.retire_unstarted_attempt(prepared)


def test_descriptor_replace_failure_keeps_old_tip_but_conservative_claim_and_retries(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    before = (prepared.control_directory.path / "container.json").read_bytes()
    before_hash = prepared.container_hash
    real_write = runner_subject.write_durable_json
    calls = 0

    def fail_first_replace(**kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected descriptor replacement failure")
        return real_write(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner_subject, "write_durable_json", fail_first_replace)

    with pytest.raises(OSError, match="injected descriptor replacement failure"):
        runner_harness.runner._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.JOB_CONFIGURED,
            request_sha256=REQUEST_SHA256,
        )

    assert (prepared.control_directory.path / "container.json").read_bytes() == before
    assert prepared.container_hash == before_hash
    for pin in prepared.pin_scope:
        pin.verify()
    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner.retire_unstarted_attempt(prepared)
    assert exc_info.value.code == "claim_already_acquired"

    current_hash = runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    raw = (prepared.control_directory.path / "container.json").read_bytes()
    descriptor = decode_material_container_descriptor(raw)
    assert calls == 2
    assert descriptor.phase is MaterialLaunchPhase.JOB_CONFIGURED
    assert descriptor.previous_phase is MaterialLaunchPhase.PLANNED
    assert descriptor.previous_descriptor_sha256 == before_hash
    assert descriptor.request_sha256 == REQUEST_SHA256
    assert current_hash == prepared.container_hash == hashlib.sha256(raw).hexdigest()


def test_descriptor_replace_uncertain_after_move_never_false_acknowledges_tip(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="coverr",
        )
    )
    before_hash = prepared.container_hash
    real_write = runner_subject.write_durable_json

    def move_then_lose_receipt(**kwargs: object) -> object:
        real_write(**kwargs)  # type: ignore[arg-type]
        raise OSError("injected acknowledgement loss after durable move")

    monkeypatch.setattr(
        runner_subject,
        "write_durable_json",
        move_then_lose_receipt,
    )

    with pytest.raises(OSError, match="acknowledgement loss"):
        runner_harness.runner._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.JOB_CONFIGURED,
            request_sha256=REQUEST_SHA256,
        )

    raw = (prepared.control_directory.path / "container.json").read_bytes()
    durable = decode_material_container_descriptor(raw)
    assert durable.phase is MaterialLaunchPhase.JOB_CONFIGURED
    assert durable.previous_descriptor_sha256 == before_hash
    assert prepared.container_hash == before_hash
    assert hashlib.sha256(raw).hexdigest() != prepared.container_hash
    for pin in prepared.pin_scope:
        pin.verify()

    monkeypatch.setattr(runner_subject, "write_durable_json", real_write)
    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.JOB_CONFIGURED,
            request_sha256=REQUEST_SHA256,
        )
    assert exc_info.value.code == "descriptor_changed"
    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner.retire_unstarted_attempt(prepared)
    assert exc_info.value.code == "claim_already_acquired"


def test_quarantine_adopts_exact_descriptor_successor_after_lost_receipt(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="coverr",
        )
    )
    real_write = runner_subject.write_durable_json

    def move_then_lose_receipt(**kwargs: object) -> object:
        real_write(**kwargs)  # type: ignore[arg-type]
        raise OSError("injected acknowledgement loss after durable move")

    monkeypatch.setattr(runner_subject, "write_durable_json", move_then_lose_receipt)
    with pytest.raises(OSError, match="acknowledgement loss"):
        runner_harness.runner._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.JOB_CONFIGURED,
            request_sha256=REQUEST_SHA256,
        )
    monkeypatch.setattr(runner_subject, "write_durable_json", real_write)

    quarantine_dir = runner_harness.runner._commit_quarantine(
        prepared,
        reason_code="run_blocked",
        evidence=None,
    )

    durable_bytes = (prepared.control_directory.path / "container.json").read_bytes()
    durable_hash = hashlib.sha256(durable_bytes).hexdigest()
    record = runner_harness.runner._records[id(prepared)]
    quarantine = _read_json(quarantine_dir / "quarantine.json")
    assert record.phase is MaterialLaunchPhase.JOB_CONFIGURED
    assert record.container_hash == prepared.container_hash == durable_hash
    assert quarantine["phase"] == "job_configured"
    assert quarantine["container_sha256"] == durable_hash


def test_reconciliation_adopts_lost_descriptor_receipt_before_restart(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
        root_pid=4242,
        root_creation_time=ROOT_CREATION_TIME,
    )
    real_write = runner_subject.write_durable_json

    def move_then_lose_receipt(**kwargs: object) -> object:
        real_write(**kwargs)  # type: ignore[arg-type]
        raise OSError("injected acknowledgement loss after durable move")

    monkeypatch.setattr(runner_subject, "write_durable_json", move_then_lose_receipt)
    with pytest.raises(OSError, match="acknowledgement loss"):
        runner_harness.runner._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.RESUME_INTENT,
        )
    monkeypatch.setattr(runner_subject, "write_durable_json", real_write)

    termination = TerminationEvidence(
        platform="windows_job",
        root_pid=4242,
        exit_code=23,
        termination_method="job_terminate",
        active_processes=0,
        observed_at=ROOT_CREATION_TIME,
    )
    evidence = runner_harness.runner._commit_queried_empty_reconciliation(
        prepared,
        termination,
    )

    record = runner_harness.runner._records[id(prepared)]
    assert record.phase is MaterialLaunchPhase.RESUME_INTENT
    assert evidence.container_sha256 == prepared.container_hash
    assert (prepared.control_directory.path / "termination.json").is_file()
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        assert recovered.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        ) == evidence
    finally:
        recovered.close()


def test_later_descriptor_transitions_reuse_root_identity_when_omitted(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="info_motion",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
        root_pid=4242,
        root_creation_time=ROOT_CREATION_TIME,
    )

    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.RESUME_INTENT,
    )

    descriptor = decode_material_container_descriptor(
        (prepared.control_directory.path / "container.json").read_bytes()
    )
    assert descriptor.request_sha256 == REQUEST_SHA256
    assert descriptor.root_pid == 4242
    assert descriptor.root_creation_time == ROOT_CREATION_TIME


@pytest.mark.parametrize("request_sha256", [None, "B" * 64, "b" * 63, "b" * 65])
def test_job_configured_requires_exact_lowercase_request_receipt_hash(
    runner_harness: _RunnerHarness,
    request_sha256: str | None,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="coverr",
        )
    )

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.JOB_CONFIGURED,
            request_sha256=request_sha256,
        )

    assert exc_info.value.code == "request_hash_invalid"


def test_root_identity_is_required_at_creation_and_fixed_afterward(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="info_motion",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
        request_sha256=REQUEST_SHA256,
    )

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
            request_sha256=REQUEST_SHA256,
        )
    assert exc_info.value.code == "root_identity_required"

    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
        request_sha256=REQUEST_SHA256,
        root_pid=4242,
        root_creation_time=ROOT_CREATION_TIME,
    )
    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.RESUME_INTENT,
            request_sha256=REQUEST_SHA256,
            root_pid=4243,
            root_creation_time=ROOT_CREATION_TIME,
        )
    assert exc_info.value.code == "root_identity_mismatch"


def test_request_receipt_hash_is_fixed_after_job_configuration(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    before = (prepared.control_directory.path / "container.json").read_bytes()

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.CREATE_INTENT,
            request_sha256="c" * 64,
        )

    assert exc_info.value.code == "request_hash_mismatch"
    assert (prepared.control_directory.path / "container.json").read_bytes() == before


def test_descriptor_decoder_requires_noninherit_and_query_one_evidence(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pixabay",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    job_configured = _read_json(
        prepared.control_directory.path / "container.json"
    )
    for mutation in (
        {key: value for key, value in job_configured.items() if key != "job_handle_inheritable"},
        {**job_configured, "job_handle_inheritable": True},
    ):
        with pytest.raises(MaterialOperationRunnerError):
            decode_material_container_descriptor(
                json.dumps(mutation, separators=(",", ":")).encode()
            )

    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
        request_sha256=REQUEST_SHA256,
        root_pid=4242,
        root_creation_time=ROOT_CREATION_TIME,
    )
    created = _read_json(prepared.control_directory.path / "container.json")
    for mutation in (
        {
            key: value
            for key, value in created.items()
            if key != "job_active_processes_after_create"
        },
        {**created, "job_active_processes_after_create": 0},
        {**created, "job_active_processes_after_create": True},
    ):
        with pytest.raises(MaterialOperationRunnerError):
            decode_material_container_descriptor(
                json.dumps(mutation, separators=(",", ":")).encode()
            )


def test_advance_rejects_disk_tamper_without_replacing_descriptor(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="coverr",
        )
    )
    write_durable_json(
        parent=prepared.control_directory,
        filename="container.json",
        payload={"phase": "tampered", "controller_pid": 1},
        replace=True,
        max_bytes=64 * 1024,
    )
    tampered = (prepared.control_directory.path / "container.json").read_bytes()

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.JOB_CONFIGURED,
            request_sha256=REQUEST_SHA256,
        )

    assert exc_info.value.code == "descriptor_changed"
    assert (prepared.control_directory.path / "container.json").read_bytes() == tampered
    for pin in prepared.pin_scope:
        pin.verify()


def test_decoder_rejects_wrong_predecessor_hash_and_immutable_identity(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="info_motion",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    value = _read_json(prepared.control_directory.path / "container.json")
    mutations = (
        {**value, "previous_phase": "create_intent"},
        {**value, "previous_descriptor_sha256": "A" * 64},
        {**value, "controller_pid": 0},
        {**value, "attempt_id": EXECUTION_ID},
    )

    for mutation in mutations:
        with pytest.raises(MaterialOperationRunnerError):
            decode_material_container_descriptor(
                json.dumps(mutation, separators=(",", ":")).encode()
            )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "unknown": True},
        lambda value: {key: item for key, item in value.items() if key != "operation"},
        lambda value: {**value, "phase": "unknown"},
        lambda value: {**value, "root_pid": 7, "root_creation_time": ROOT_CREATION_TIME},
        lambda value: {**value, "operation_may_have_started": True},
    ],
)
def test_planned_descriptor_decoder_rejects_unknown_missing_and_phase_illegal_fields(
    runner_harness: _RunnerHarness,
    mutation: object,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="official_capture",
        )
    )
    value = _read_json(prepared.control_directory.path / "container.json")
    changed = mutation(value)  # type: ignore[operator]

    with pytest.raises(MaterialOperationRunnerError):
        decode_material_container_descriptor(
            json.dumps(changed, separators=(",", ":")).encode()
        )


def test_descriptor_decoder_rejects_duplicate_keys_and_floats() -> None:
    for raw in (
        b'{"phase":"planned","phase":"planned"}',
        b'{"protocol_version":1.0}',
        b'{} ',
        b"\xef\xbb\xbf{}",
    ):
        with pytest.raises(MaterialOperationRunnerError):
            decode_material_container_descriptor(raw)


def test_run_rejects_prepared_request_secret_identity_mismatch_before_writes(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    request, secrets = _valid_run_inputs(
        prepared,
        attempt_id=OTHER_ATTEMPT_ID,
    )

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner.run(
            prepared,
            request,
            secrets=secrets,
            startup_timeout_seconds=5.0,
            operation_timeout_seconds=30.0,
        )

    assert exc_info.value.code == "run_identity_mismatch"
    assert {child.name for child in prepared.directory.path.iterdir()} == {
        "payload",
        "tmp",
        "profile",
    }
    runner_harness.runner.retire_unstarted_attempt(prepared)


@pytest.mark.parametrize(
    ("startup_timeout", "operation_timeout"),
    [
        (True, 30.0),
        (0.0, 30.0),
        (-1.0, 30.0),
        (float("nan"), 30.0),
        (2**4096, 30.0),
        (5.0, False),
        (5.0, 0.0),
        (5.0, float("inf")),
    ],
)
def test_run_rejects_invalid_deadlines_before_claim_or_write(
    runner_harness: _RunnerHarness,
    startup_timeout: object,
    operation_timeout: object,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    request, secrets = _valid_run_inputs(prepared)

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner.run(
            prepared,
            request,
            secrets=secrets,
            startup_timeout_seconds=startup_timeout,  # type: ignore[arg-type]
            operation_timeout_seconds=operation_timeout,  # type: ignore[arg-type]
        )

    assert exc_info.value.code == "run_timeout_invalid"
    assert not (prepared.directory.path / "request.json").exists()
    runner_harness.runner.retire_unstarted_attempt(prepared)


def test_run_preflights_worker_runtime_before_claim_write_or_native_resources(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    request, secrets = _valid_run_inputs(prepared)
    events: list[str] = []

    class _FakeOps:
        def preflight_worker_runtime(
            self,
            *,
            prepared: PreparedMaterialAttempt,
        ) -> object:
            assert prepared is runner_harness.prepared[0]
            events.append("runtime_preflight")
            raise MaterialOperationRunnerError(
                "worker_runtime_unavailable",
                "injected reversible runtime preflight failure",
            )

        def create_job(self) -> object:
            raise AssertionError("native Job must not exist before runtime preflight")

    def forbidden_request_write(**_kwargs: object) -> object:
        raise AssertionError("request must not be written before runtime preflight")

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _FakeOps())
    monkeypatch.setattr(
        runner_subject,
        "write_durable_bytes",
        forbidden_request_write,
    )

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner.run(
            prepared,
            request,
            secrets=secrets,
            startup_timeout_seconds=5.0,
            operation_timeout_seconds=30.0,
        )

    assert exc_info.value.code == "worker_runtime_unavailable"
    assert events == ["runtime_preflight"]
    assert not (prepared.directory.path / "request.json").exists()
    record = runner_harness.runner._records[id(prepared)]
    assert record.claim_acquired is False
    assert record.job_owner is None
    assert record.pipe_owner is None
    assert record.process_owner is None
    runner_harness.runner.retire_unstarted_attempt(prepared)


def test_run_freezes_inputs_once_and_publishes_request_before_native_resources(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    request, secrets = _valid_run_inputs(prepared)
    expected_request = encode_material_operation_request(request)
    expected_secret = encode_material_operation_secrets(
        secrets,
        request=request,
    )
    events: list[str] = []
    real_request_encoder = encode_material_operation_request
    real_secret_encoder = encode_material_operation_secrets

    def encode_request_once(value: MaterialOperationRequest) -> bytes:
        events.append("encode_request")
        return real_request_encoder(value)

    def encode_secret_once(
        value: MaterialOperationSecrets,
        *,
        request: MaterialOperationRequest,
    ) -> bytes:
        events.append("encode_secret")
        return real_secret_encoder(value, request=request)

    def fail_after_request_publish(**kwargs: object) -> object:
        events.append("write_request")
        assert kwargs == {
            "parent": prepared.directory,
            "filename": "request.json",
            "payload": expected_request,
            "replace": False,
            "max_bytes": MAX_REQUEST_BYTES,
        }
        assert SECRET_CANARY.encode() not in expected_request
        raise OSError("injected stop after request publication boundary")

    runtime_preflight = object()

    class _FakeOps:
        def preflight_worker_runtime(
            self,
            *,
            prepared: PreparedMaterialAttempt,
        ) -> object:
            assert prepared is runner_harness.prepared[0]
            events.append("runtime_preflight")
            return runtime_preflight

        def create_job(self) -> object:
            events.append("native_job")
            raise AssertionError("Job must not exist before request receipt")

    monkeypatch.setattr(
        runner_subject,
        "encode_material_operation_request",
        encode_request_once,
        raising=False,
    )
    monkeypatch.setattr(
        runner_subject,
        "encode_material_operation_secrets",
        encode_secret_once,
        raising=False,
    )
    monkeypatch.setattr(
        runner_subject,
        "write_durable_bytes",
        fail_after_request_publish,
        raising=False,
    )
    monkeypatch.setattr(
        runner_subject,
        "_WINDOWS_RUNNER_OPS",
        _FakeOps(),
    )

    with pytest.raises(OSError, match="request publication boundary"):
        runner_harness.runner.run(
            prepared,
            request,
            secrets=secrets,
            startup_timeout_seconds=5.0,
            operation_timeout_seconds=30.0,
        )

    assert events == [
        "encode_request",
        "encode_secret",
        "runtime_preflight",
        "write_request",
    ]
    assert expected_secret == encode_material_operation_secrets(
        secrets,
        request=request,
    )
    assert SECRET_CANARY.encode() in expected_secret
    assert not (prepared.directory.path / "request.json").exists()
    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner.retire_unstarted_attempt(prepared)
    assert exc_info.value.code == "claim_already_acquired"


def test_worker_result_protocol_supports_parent_operation_identity_gate() -> None:
    raw = json.dumps(
        {
            "protocol_version": 1,
            "execution_id": EXECUTION_ID,
            "attempt_id": OTHER_ATTEMPT_ID,
            "operation": "pexels",
            "status": "failed",
            "selected_artifact": "",
            "artifacts": [],
            "reason_code": "source_unavailable",
            "reason": "No suitable material was found.",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    result = decode_material_worker_result(
        raw,
        expected_execution_id=EXECUTION_ID,
        expected_attempt_id=OTHER_ATTEMPT_ID,
        expected_operation="pexels",
    )

    assert result.operation == "pexels"


@pytest.mark.parametrize(
    ("execution_id", "attempt_id", "operation"),
    [
        (CONTROL_ID, None, "pexels"),
        (None, OTHER_ATTEMPT_ID, "pexels"),
        (None, None, "pixabay"),
    ],
)
def test_runner_result_read_blocks_every_parent_identity_mismatch(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
    execution_id: str | None,
    attempt_id: str | None,
    operation: str,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    raw = encode_material_worker_result(
        MaterialWorkerResult(
            protocol_version=1,
            execution_id=execution_id or prepared.execution_id,
            attempt_id=attempt_id or prepared.attempt_id,
            operation=operation,  # type: ignore[arg-type]
            status="failed",
            selected_artifact="",
            artifacts=(),
            reason_code="source_unavailable",
            reason="No suitable material was found.",
        )
    )
    monkeypatch.setattr(
        runner_subject,
        "_read_verified_bytes",
        lambda **_kwargs: raw,
    )

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_subject._read_worker_result(
            prepared=prepared,
            expected_operation="pexels",
        )

    assert exc_info.value.code == "worker_result_invalid"
    monkeypatch.undo()
    runner_harness.runner.retire_unstarted_attempt(prepared)


def test_run_durably_configures_job_before_creating_secret_pipe(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    request, secrets = _valid_run_inputs(prepared)
    request_bytes = encode_material_operation_request(request)
    request_sha256 = hashlib.sha256(request_bytes).hexdigest()
    events: list[str] = []
    real_write_bytes = runner_subject.write_durable_bytes
    real_write_json = runner_subject.write_durable_json

    class _StopAtSecretPipe(RuntimeError):
        pass

    class _FakeJob:
        handle = 777
        closed = False

        def close(self) -> None:
            self.closed = True

    job = _FakeJob()

    class _FakeOps:
        def preflight_worker_runtime(
            self,
            *,
            prepared: PreparedMaterialAttempt,
        ) -> object:
            assert prepared is runner_harness.prepared[0]
            events.append("runtime_preflight")
            return object()

        def create_job(self) -> _FakeJob:
            events.append("job_create")
            return job

        def create_secret_pipe(self, *, forbidden_ids: tuple[str, ...]) -> object:
            events.append("pipe_create")
            assert forbidden_ids == (
                prepared.execution_id,
                prepared.attempt_id,
                prepared.control_id,
                prepared.job_instance_id,
            )
            descriptor = decode_material_container_descriptor(
                (prepared.control_directory.path / "container.json").read_bytes()
            )
            assert descriptor.phase is MaterialLaunchPhase.JOB_CONFIGURED
            assert descriptor.request_sha256 == request_sha256
            raise _StopAtSecretPipe("stop after job_configured")

    def write_request(**kwargs: object) -> object:
        events.append("request_durable")
        return real_write_bytes(**kwargs)  # type: ignore[arg-type]

    def write_descriptor(**kwargs: object) -> object:
        payload = kwargs.get("payload")
        if kwargs.get("filename") == "termination.json":
            events.append("termination_durable")
        elif isinstance(payload, dict):
            events.append(f"descriptor:{payload.get('phase')}")
        return real_write_json(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner_subject, "write_durable_bytes", write_request)
    monkeypatch.setattr(runner_subject, "write_durable_json", write_descriptor)
    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _FakeOps(), raising=False)

    class _ContainmentOps(_FakeOps):
        def terminate_job_for_quarantine(self, *, job_owner: object) -> bool:
            assert job_owner is job
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ContainmentOps(), raising=False)

    with pytest.raises(MaterialOperationBlocked) as exc_info:
        runner_harness.runner.run(
            prepared,
            request,
            secrets=secrets,
            startup_timeout_seconds=5.0,
            operation_timeout_seconds=30.0,
        )
    assert exc_info.value.reason_code == "run_blocked"

    assert events == [
        "runtime_preflight",
        "request_durable",
        "job_create",
        "descriptor:job_configured",
        "pipe_create",
        "descriptor:job_configured",
    ]
    assert (prepared.directory.path / "request.json").read_bytes() == request_bytes
    record = runner_harness.runner._records[id(prepared)]
    assert record.job_owner is job
    assert record.pipe_owner is None
    assert job.closed is False
    all_file_bytes = b"".join(
        path.read_bytes()
        for root in (prepared.directory.path, prepared.control_directory.path)
        for path in root.rglob("*")
        if path.is_file()
    )
    assert SECRET_CANARY.encode() not in all_file_bytes


def test_atomic_process_resource_error_is_transferred_to_runner_quarantine(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    request, secrets = _valid_run_inputs(prepared)

    class _FakeJob:
        handle = 777

    class _FakePipe:
        name = r"\\.\pipe\AutoVideoEngine.Material.atomic-owner-test"
        active_operation = None
        closed = False

        def close(self) -> None:
            self.closed = True

    class _AtomicOwner:
        transferred = False
        cleaned = False

        @property
        def suspended_process(self) -> object:
            return object()

        def transfer_to_quarantine(self) -> object:
            self.transferred = True
            return self

        def cleanup_all_stop_on_failure(self) -> list[BaseException]:
            self.cleaned = True
            return []

    job = _FakeJob()
    pipe = _FakePipe()
    atomic_owner = _AtomicOwner()
    launch_spec = object()
    runtime_preflight = object()

    class _FakeOps:
        def preflight_worker_runtime(
            self,
            *,
            prepared: PreparedMaterialAttempt,
        ) -> object:
            assert prepared is runner_harness.prepared[0]
            return runtime_preflight

        def create_job(self) -> _FakeJob:
            return job

        def create_secret_pipe(self, *, forbidden_ids: tuple[str, ...]) -> _FakePipe:
            del forbidden_ids
            return pipe

        def build_worker_launch_spec(
            self,
            *,
            prepared: PreparedMaterialAttempt,
            pipe_name: str,
            runtime_preflight: object,
        ) -> object:
            assert prepared is runner_harness.prepared[0]
            assert pipe_name == pipe.name
            assert runtime_preflight is globals_runtime_preflight
            return launch_spec

        def create_suspended_process(
            self,
            *,
            launch_spec: object,
            job_owner: object,
        ) -> object:
            assert launch_spec is globals_launch_spec
            assert job_owner is job
            raise WindowsAtomicProcessResourceError(
                "injected atomic resource handoff",
                resource_owner=atomic_owner,  # type: ignore[arg-type]
            )

        def terminate_job_for_quarantine(self, *, job_owner: object) -> bool:
            assert job_owner is job
            return True

    globals_runtime_preflight = runtime_preflight
    globals_launch_spec = launch_spec
    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _FakeOps())

    with pytest.raises(MaterialOperationBlocked) as exc_info:
        runner_harness.runner.run(
            prepared,
            request,
            secrets=secrets,
            startup_timeout_seconds=5.0,
            operation_timeout_seconds=30.0,
        )
    assert exc_info.value.reason_code == "run_blocked"

    record = runner_harness.runner._records[id(prepared)]
    assert record.job_owner is job
    assert record.pipe_owner is pipe
    assert record.process_owner is None
    assert record.process_quarantine_owner is atomic_owner
    assert atomic_owner.transferred is True
    assert atomic_owner.cleaned is True
    assert pipe.closed is True
    descriptor = decode_material_container_descriptor(
        (prepared.control_directory.path / "container.json").read_bytes()
    )
    assert descriptor.phase is MaterialLaunchPhase.CREATE_INTENT
    assert (prepared.directory.path / "request.json").is_file()


def test_secure_secret_pipe_uses_first_instance_remote_reject_and_exact_dacl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    real_create = runner_subject.win32pipe.CreateNamedPipe

    def record_create(*args: object) -> object:
        calls.append(args)
        return real_create(*args)

    monkeypatch.setattr(runner_subject.win32pipe, "CreateNamedPipe", record_create)
    owner = runner_subject._WindowsRunnerOps().create_secret_pipe(
        forbidden_ids=(
            EXECUTION_ID,
            OTHER_ATTEMPT_ID,
            CONTROL_ID,
            JOB_INSTANCE_ID,
        )
    )
    try:
        assert len(calls) == 1
        (
            name,
            open_mode,
            pipe_mode,
            max_instances,
            output_buffer_size,
            input_buffer_size,
            default_timeout,
            security_attributes,
        ) = calls[0]
        assert name == owner.name
        assert open_mode == (
            runner_subject.win32pipe.PIPE_ACCESS_OUTBOUND
            | runner_subject._FILE_FLAG_FIRST_PIPE_INSTANCE
            | runner_subject.win32file.FILE_FLAG_OVERLAPPED
        )
        assert pipe_mode == (
            runner_subject.win32pipe.PIPE_TYPE_MESSAGE
            | runner_subject.win32pipe.PIPE_READMODE_MESSAGE
            | runner_subject.win32pipe.PIPE_WAIT
            | runner_subject.win32pipe.PIPE_REJECT_REMOTE_CLIENTS
        )
        assert max_instances == 1
        assert output_buffer_size == 4 + runner_subject.MAX_SECRET_BYTES
        assert input_buffer_size == 4 + runner_subject.MAX_SECRET_BYTES
        assert default_timeout == 0
        assert security_attributes is owner.security_attributes
        assert all(
            value not in owner.name and UUID(value).hex not in owner.name
            for value in (
                EXECUTION_ID,
                OTHER_ATTEMPT_ID,
                CONTROL_ID,
                JOB_INSTANCE_ID,
            )
        )
        assert (
            runner_subject.win32api.GetHandleInformation(owner.pipe_handle)
            & runner_subject.win32con.HANDLE_FLAG_INHERIT
        ) == 0

        descriptor = runner_subject.win32security.GetKernelObjectSecurity(
            owner.pipe_handle,
            runner_subject.win32security.OWNER_SECURITY_INFORMATION
            | runner_subject.win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        assert dacl is not None
        actual: dict[str, int] = {}
        for index in range(dacl.GetAceCount()):
            (ace_type, _flags), access_mask, sid = dacl.GetAce(index)
            assert ace_type == runner_subject.win32security.ACCESS_ALLOWED_ACE_TYPE
            actual[str(runner_subject.win32security.ConvertSidToStringSid(sid))] = (
                access_mask & 0xFFFFFFFF
            )
        token = runner_subject.win32security.OpenProcessToken(
            runner_subject.win32api.GetCurrentProcess(),
            runner_subject.win32con.TOKEN_QUERY,
        )
        try:
            user_sid, _attributes = runner_subject.win32security.GetTokenInformation(
                token,
                runner_subject.win32security.TokenUser,
            )
        finally:
            token.Close()
        user_sid_text = str(
            runner_subject.win32security.ConvertSidToStringSid(user_sid)
        )
        assert actual == {
            user_sid_text: runner_subject._SECRET_PIPE_USER_ACCESS_EFFECTIVE,
            "S-1-5-18": runner_subject._SECRET_PIPE_SYSTEM_ACCESS_EFFECTIVE,
        }

        with pytest.raises(runner_subject.pywintypes.error):
            real_create(*calls[0])
    finally:
        owner.close()


def test_run_orders_create_resume_secret_ready_result_and_natural_completion(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    request, secrets = _valid_run_inputs(prepared)
    events: list[str] = []
    real_write_json = runner_subject.write_durable_json

    class _FakeJob:
        handle = 777

        def close(self) -> None:
            events.append("job_close")

    class _FakePipe:
        name = r"\\.\pipe\AutoVideoEngine.Material.fake-independent-nonce"

        def close(self) -> None:
            events.append("pipe_close")

    class _FakeProcess:
        pid = 4242

        def resume(self) -> int:
            events.append("resume")
            return 1

        def close_thread_handle(self) -> None:
            events.append("close_thread")

        def close_process_handle(self) -> None:
            events.append("process_close")

    job = _FakeJob()
    pipe = _FakePipe()
    process = _FakeProcess()
    launch_spec = object()
    runtime_preflight = object()

    class _FakeOps:
        def preflight_worker_runtime(
            self,
            *,
            prepared: PreparedMaterialAttempt,
        ) -> object:
            assert prepared is runner_harness.prepared[0]
            events.append("runtime_preflight")
            return runtime_preflight

        def create_job(self) -> _FakeJob:
            events.append("job_create")
            return job

        def create_secret_pipe(self, *, forbidden_ids: tuple[str, ...]) -> _FakePipe:
            del forbidden_ids
            events.append("pipe_create")
            return pipe

        def build_worker_launch_spec(
            self,
            *,
            prepared: PreparedMaterialAttempt,
            pipe_name: str,
            runtime_preflight: object,
        ) -> object:
            assert prepared is runner_harness.prepared[0]
            assert pipe_name == pipe.name
            assert runtime_preflight is globals_runtime_preflight
            events.append("build_launch_spec")
            return launch_spec

        def create_suspended_process(
            self,
            *,
            launch_spec: object,
            job_owner: object,
        ) -> _FakeProcess:
            assert launch_spec is globals_launch_spec
            assert job_owner is job
            events.append("atomic_create")
            return process

        def query_process_identity(self, *, pid: int) -> tuple[int, str]:
            assert pid == process.pid
            events.append("root_identity")
            return process.pid, ROOT_CREATION_TIME

        def verify_single_root_job(self, *, job_owner: object, root_pid: int) -> None:
            assert job_owner is job
            assert root_pid == process.pid
            events.append("membership_query_one")

        def connect_secret_pipe(
            self,
            *,
            pipe_owner: object,
            absolute_deadline: float,
        ) -> None:
            assert pipe_owner is pipe
            assert absolute_deadline > 0
            events.append("pipe_connect")
            descriptor = decode_material_container_descriptor(
                (prepared.control_directory.path / "container.json").read_bytes()
            )
            assert descriptor.phase is MaterialLaunchPhase.RESUMED_NO_SECRET
            assert absolute_deadline == 105.0

        def verify_secret_pipe_client(
            self,
            *,
            pipe_owner: object,
            root_pid: int,
            root_creation_time: str,
            job_owner: object,
        ) -> None:
            assert pipe_owner is pipe
            assert root_pid == process.pid
            assert root_creation_time == ROOT_CREATION_TIME
            assert job_owner is job
            events.append("client_pid_and_job_verified")

        def write_secret_frame(
            self,
            *,
            pipe_owner: object,
            immutable_frame: bytes,
            absolute_deadline: float,
        ) -> None:
            assert pipe_owner is pipe
            payload_size = int.from_bytes(immutable_frame[:4], "big")
            assert payload_size == len(immutable_frame) - 4
            assert immutable_frame[4:] == encode_material_operation_secrets(
                secrets,
                request=request,
            )
            assert absolute_deadline == 105.0
            events.append("secret_exact_write")

        def wait_worker_ready(
            self,
            *,
            prepared: PreparedMaterialAttempt,
            expected_operation: str,
            expected_root_pid: int,
            expected_request_sha256: str,
            startup_deadline: float,
            operation_deadline: float,
        ) -> MaterialWorkerReady:
            assert prepared is runner_harness.prepared[0]
            assert expected_operation == "pexels"
            assert expected_root_pid == process.pid
            assert expected_request_sha256 == hashlib.sha256(
                encode_material_operation_request(request)
            ).hexdigest()
            assert startup_deadline == 105.0
            assert operation_deadline == 230.0
            events.append("ready_wait")
            descriptor = decode_material_container_descriptor(
                (prepared.control_directory.path / "container.json").read_bytes()
            )
            assert descriptor.phase is MaterialLaunchPhase.SECRET_SENT
            return MaterialWorkerReady(
                protocol_version=1,
                state="ready",
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                operation="pexels",
                root_pid=process.pid,
                request_sha256=expected_request_sha256,
            )

        def wait_job_empty(
            self,
            *,
            job_owner: object,
            operation_deadline: float,
        ) -> None:
            assert job_owner is job
            assert operation_deadline == 230.0
            events.append("job_empty_wait")
            descriptor = decode_material_container_descriptor(
                (prepared.control_directory.path / "container.json").read_bytes()
            )
            assert descriptor.phase is MaterialLaunchPhase.READY

        def read_worker_result(
            self,
            *,
            prepared: PreparedMaterialAttempt,
            expected_operation: str,
        ) -> MaterialWorkerResult:
            assert prepared is runner_harness.prepared[0]
            assert expected_operation == "pexels"
            events.append("result_read")
            return MaterialWorkerResult(
                protocol_version=1,
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                operation="pexels",
                status="failed",
                selected_artifact="",
                artifacts=(),
                reason_code="source_unavailable",
                reason="No suitable material was found.",
            )

        def collect_natural_termination(
            self,
            *,
            process_owner: object,
            root_pid: int,
        ) -> TerminationEvidence:
            assert process_owner is process
            assert root_pid == process.pid
            events.append("natural_termination")
            return TerminationEvidence(
                platform="windows_job",
                root_pid=root_pid,
                exit_code=23,
                termination_method="natural",
                active_processes=0,
                observed_at="2026-07-15T12:35:00.000000Z",
            )

        def close_natural_completion_resources(
            self,
            *,
            process_owner: object,
            job_owner: object,
        ) -> None:
            assert process_owner is process
            assert job_owner is job
            process.close_process_handle()
            job.close()

    globals_launch_spec = launch_spec
    globals_runtime_preflight = runtime_preflight

    def write_descriptor(**kwargs: object) -> object:
        payload = kwargs.get("payload")
        if kwargs.get("filename") == "termination.json":
            events.append("termination_durable")
        elif isinstance(payload, dict):
            events.append(f"descriptor:{payload.get('phase')}")
        return real_write_json(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner_subject, "write_durable_json", write_descriptor)
    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _FakeOps())
    clock_values = iter((100.0, 200.0))

    def controlled_clock() -> float:
        value = next(clock_values)
        events.append("clock:startup" if value == 100.0 else "clock:operation")
        return value

    monkeypatch.setattr(runner_subject, "_monotonic", controlled_clock)

    outcome = runner_harness.runner.run(
        prepared,
        request,
        secrets=secrets,
        startup_timeout_seconds=5.0,
        operation_timeout_seconds=30.0,
    )

    assert events == [
        "clock:startup",
        "runtime_preflight",
        "job_create",
        "descriptor:job_configured",
        "pipe_create",
        "build_launch_spec",
        "descriptor:create_intent",
        "atomic_create",
        "root_identity",
        "membership_query_one",
        "descriptor:created_in_job_suspended",
        "descriptor:resume_intent",
        "resume",
        "descriptor:resumed_no_secret",
        "close_thread",
        "pipe_connect",
        "client_pid_and_job_verified",
        "descriptor:secret_send_intent",
        "clock:operation",
        "secret_exact_write",
        "descriptor:secret_sent",
        "pipe_close",
        "ready_wait",
        "descriptor:ready",
        "job_empty_wait",
        "result_read",
        "natural_termination",
        "termination_durable",
        "process_close",
        "job_close",
    ]
    record = runner_harness.runner._records[id(prepared)]
    assert record.job_owner is job
    assert record.pipe_owner is pipe
    assert record.process_owner is process
    assert record.root_pid == process.pid
    assert record.root_creation_time == ROOT_CREATION_TIME
    assert record.operation_deadline == 230.0
    assert record.phase is MaterialLaunchPhase.READY
    assert record.outcome is outcome
    assert record.completion_resources_closed is True
    assert outcome.status == "failed"
    assert outcome.result is not None
    assert outcome.result.operation == "pexels"
    assert outcome.termination.exit_code == 23
    assert record.reconciliation is not None
    assert record.reconciliation.method == "queried_empty"
    assert record.reconciliation.observation_basis == "queried_job"
    assert record.reconciliation.active_processes == 0
    assert record.reconciliation.root_pid == process.pid
    assert _read_json(
        prepared.control_directory.path / "termination.json"
    ) == {
        "active_processes": 0,
        "attempt_id": prepared.attempt_id,
        "container_sha256": record.container_hash,
        "execution_id": prepared.execution_id,
        "job_instance_id": prepared.job_instance_id,
        "method": "queried_empty",
        "observation_basis": "queried_job",
        "observed_at": record.reconciliation.observed_at,
        "operation_may_have_started": True,
        "protocol_version": 1,
        "root_pid": process.pid,
    }


@pytest.mark.skipif(os.name != "nt", reason="real containment gate is Windows-only")
def test_real_windows_worker_completes_inside_job_without_persisting_secret(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    request, secrets = _valid_run_inputs(prepared)
    worker = Path(__file__).parent / "fixtures" / "material_operation_test_worker.py"
    assert worker.resolve(strict=True).drive.casefold() == "d:"
    monkeypatch.setattr(runner_subject, "_WORKER_BOOTSTRAP_PATH", worker)

    outcome = runner_harness.runner.run(
        prepared,
        request,
        secrets=secrets,
        startup_timeout_seconds=10.0,
        operation_timeout_seconds=10.0,
    )

    assert outcome.status == "failed"
    assert outcome.result is not None
    assert outcome.result.execution_id == prepared.execution_id
    assert outcome.result.attempt_id == prepared.attempt_id
    assert outcome.result.operation == "pexels"
    assert outcome.termination.termination_method == "natural"
    assert outcome.termination.active_processes == 0
    assert outcome.termination.exit_code == 23
    record = runner_harness.runner._records[id(prepared)]
    assert record.outcome is outcome
    assert record.completion_resources_closed is True
    assert record.phase is MaterialLaunchPhase.READY
    assert record.root_pid == outcome.termination.root_pid
    assert (
        runner_harness.runner.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        )
        == record.reconciliation
    )
    assert (prepared.directory.path / "request.json").is_file()
    assert (prepared.directory.path / "ready.json").is_file()
    assert (prepared.directory.path / "result.json").is_file()
    persisted = b"".join(
        path.read_bytes()
        for root in (prepared.directory.path, prepared.control_directory.path)
        for path in root.rglob("*")
        if path.is_file()
    )
    assert SECRET_CANARY.encode() not in persisted


@pytest.mark.skipif(os.name != "nt", reason="real containment gate is Windows-only")
def test_real_windows_invalid_result_is_blocked_after_durable_termination_evidence(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    request, secrets = _valid_run_inputs(prepared)
    worker = Path(__file__).parent / "fixtures" / "material_operation_test_worker.py"
    monkeypatch.setattr(runner_subject, "_WORKER_BOOTSTRAP_PATH", worker)

    class _InvalidResultOps(runner_subject._WindowsRunnerOps):
        def read_worker_result(
            self,
            *,
            prepared: PreparedMaterialAttempt,
            expected_operation: str,
        ) -> object:
            assert prepared is runner_harness.prepared[0]
            assert expected_operation == "pexels"
            raise MaterialOperationRunnerError(
                "worker_result_invalid",
                "injected malformed result after Job empty",
            )

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _InvalidResultOps())

    with pytest.raises(MaterialOperationBlocked) as exc_info:
        runner_harness.runner.run(
            prepared,
            request,
            secrets=secrets,
            startup_timeout_seconds=10.0,
            operation_timeout_seconds=10.0,
        )

    assert exc_info.value.reason_code == "worker_result_invalid"
    assert exc_info.value.quarantine_dir == prepared.control_directory.path
    record = runner_harness.runner._records[id(prepared)]
    assert record.blocked is True
    assert record.completion_resources_closed is True
    assert record.reconciliation is not None
    assert record.reconciliation.method == "queried_empty"
    quarantine = _read_json(prepared.control_directory.path / "quarantine.json")
    assert quarantine["state"] == "blocked"
    assert quarantine["reason_code"] == "worker_result_invalid"
    assert quarantine["termination"]["method"] == "queried_empty"
    persisted = b"".join(
        path.read_bytes()
        for root in (prepared.directory.path, prepared.control_directory.path)
        for path in root.rglob("*")
        if path.is_file()
    )
    assert SECRET_CANARY.encode() not in persisted


@pytest.mark.skipif(os.name != "nt", reason="real containment gate is Windows-only")
def test_real_windows_ready_timeout_commits_terminated_outcome_before_close(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    request, secrets = _valid_run_inputs(prepared)
    worker = Path(__file__).parent / "fixtures" / "material_operation_hanging_worker.py"
    monkeypatch.setattr(runner_subject, "_WORKER_BOOTSTRAP_PATH", worker)

    outcome = runner_harness.runner.run(
        prepared,
        request,
        secrets=secrets,
        startup_timeout_seconds=2.0,
        operation_timeout_seconds=10.0,
    )

    assert outcome.status == "terminated"
    assert outcome.result is None
    assert outcome.termination.termination_method == "job_terminate"
    assert outcome.termination.active_processes == 0
    record = runner_harness.runner._records[id(prepared)]
    assert record.outcome is outcome
    assert record.completion_resources_closed is True
    assert record.termination_outcome_hash is not None
    persisted = _read_json(
        prepared.control_directory.path / "termination-outcome.json"
    )
    assert persisted == {
        "attempt_id": prepared.attempt_id,
        "execution_id": prepared.execution_id,
        "job_instance_id": prepared.job_instance_id,
        "observed_at": outcome.termination.observed_at,
        "protocol_version": 1,
        "state": "terminated",
        "termination": {
            "active_processes": 0,
            "exit_code": outcome.termination.exit_code,
            "observed_at": outcome.termination.observed_at,
            "platform": "windows_job",
            "root_pid": outcome.termination.root_pid,
            "termination_method": "job_terminate",
        },
    }
    assert not (prepared.directory.path / "ready.json").exists()
    assert not (prepared.directory.path / "result.json").exists()
    persisted_bytes = b"".join(
        path.read_bytes()
        for root in (prepared.directory.path, prepared.control_directory.path)
        for path in root.rglob("*")
        if path.is_file()
    )
    assert SECRET_CANARY.encode() not in persisted_bytes


@pytest.mark.skipif(os.name != "nt", reason="real containment gate is Windows-only")
def test_real_windows_invalid_ready_is_quarantined_after_job_termination(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    request, secrets = _valid_run_inputs(prepared)
    worker = Path(__file__).parent / "fixtures" / "material_operation_hanging_worker.py"
    monkeypatch.setattr(runner_subject, "_WORKER_BOOTSTRAP_PATH", worker)

    class _InvalidReadyOps(runner_subject._WindowsRunnerOps):
        def wait_worker_ready(self, **_kwargs: object) -> object:
            raise MaterialOperationRunnerError(
                "worker_ready_invalid",
                "injected invalid ready state after worker launch",
            )

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _InvalidReadyOps())

    with pytest.raises(MaterialOperationBlocked) as exc_info:
        runner_harness.runner.run(
            prepared,
            request,
            secrets=secrets,
            startup_timeout_seconds=10.0,
            operation_timeout_seconds=10.0,
        )

    assert exc_info.value.reason_code == "worker_ready_invalid"
    assert exc_info.value.quarantine_dir == prepared.control_directory.path
    record = runner_harness.runner._records[id(prepared)]
    assert record.blocked is True
    assert record.outcome is None
    assert record.completion_resources_closed is True
    quarantine = _read_json(prepared.control_directory.path / "quarantine.json")
    assert quarantine["state"] == "blocked"
    assert quarantine["reason_code"] == "worker_ready_invalid"
    assert quarantine["termination"]["platform"] == "windows_job"
    assert quarantine["termination"]["active_processes"] == 0
    assert quarantine["termination"]["termination_method"] in {
        "job_terminate",
        "never_resumed",
    }
    assert not (prepared.control_directory.path / "termination-outcome.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="real containment gate is Windows-only")
def test_real_windows_operation_timeout_commits_terminated_outcome_and_reconcile(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    request, secrets = _valid_run_inputs(prepared)
    worker = Path(__file__).parent / "fixtures" / "material_operation_hanging_worker.py"
    monkeypatch.setattr(runner_subject, "_WORKER_BOOTSTRAP_PATH", worker)

    class _ReadyThenHangOps(runner_subject._WindowsRunnerOps):
        def wait_worker_ready(
            self,
            *,
            prepared: PreparedMaterialAttempt,
            expected_operation: str,
            expected_root_pid: int,
            expected_request_sha256: str,
            startup_deadline: float,
            operation_deadline: float,
        ) -> MaterialWorkerReady:
            del startup_deadline, operation_deadline
            return MaterialWorkerReady(
                protocol_version=1,
                state="ready",
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                operation=expected_operation,  # type: ignore[arg-type]
                root_pid=expected_root_pid,
                request_sha256=expected_request_sha256,
            )

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ReadyThenHangOps())

    outcome = runner_harness.runner.run(
        prepared,
        request,
        secrets=secrets,
        startup_timeout_seconds=10.0,
        operation_timeout_seconds=1.0,
    )

    assert outcome.status == "terminated"
    assert outcome.result is None
    assert outcome.termination.termination_method == "job_terminate"
    record = runner_harness.runner._records[id(prepared)]
    assert record.outcome is outcome
    assert record.completion_resources_closed is True
    assert record.reconciliation is not None
    assert record.reconciliation.method == "queried_empty"
    assert record.reconciliation.active_processes == 0
    assert (
        runner_harness.runner.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        )
        == record.reconciliation
    )
    assert (prepared.control_directory.path / "termination.json").is_file()
    assert (prepared.control_directory.path / "termination-outcome.json").is_file()


@pytest.mark.parametrize("winerror", [32, 33])
def test_ready_wait_retries_transient_windows_publish_sharing_violation(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
    winerror: int,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    ready = MaterialWorkerReady(
        protocol_version=1,
        state="ready",
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        operation="pexels",
        root_pid=4242,
        request_sha256=REQUEST_SHA256,
    )
    reads = 0

    def read_after_publish(**_kwargs: object) -> bytes:
        nonlocal reads
        reads += 1
        if reads == 1:
            error = OSError("durable writer still owns ready.json")
            error.winerror = winerror  # type: ignore[attr-defined]
            raise error
        return encode_material_worker_ready(ready)

    monkeypatch.setattr(runner_subject, "_read_verified_bytes", read_after_publish)
    monkeypatch.setattr(runner_subject, "_monotonic", lambda: 1.0)
    monkeypatch.setattr(runner_subject.time, "sleep", lambda _seconds: None)

    observed = runner_subject._wait_worker_ready(
        prepared=prepared,
        expected_operation="pexels",
        expected_root_pid=4242,
        expected_request_sha256=REQUEST_SHA256,
        startup_deadline=10.0,
        operation_deadline=20.0,
    )

    assert observed == ready
    assert reads == 2
    monkeypatch.undo()
    runner_harness.runner.retire_unstarted_attempt(prepared)


def test_ready_wait_rejects_valid_message_observed_after_deadline(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    ready = MaterialWorkerReady(
        protocol_version=1,
        state="ready",
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        operation="pexels",
        root_pid=4242,
        request_sha256=REQUEST_SHA256,
    )
    clock = iter((1.0, 10.0))
    monkeypatch.setattr(runner_subject, "_monotonic", lambda: next(clock))
    monkeypatch.setattr(
        runner_subject,
        "_read_verified_bytes",
        lambda **_kwargs: encode_material_worker_ready(ready),
    )

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_subject._wait_worker_ready(
            prepared=prepared,
            expected_operation="pexels",
            expected_root_pid=4242,
            expected_request_sha256=REQUEST_SHA256,
            startup_deadline=10.0,
            operation_deadline=20.0,
        )

    assert exc_info.value.code == "worker_ready_timeout"
    monkeypatch.undo()
    runner_harness.runner.retire_unstarted_attempt(prepared)


def test_job_empty_query_completed_at_deadline_is_operation_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeJob:
        def wait_until_empty(self, timeout_seconds: float) -> bool:
            assert timeout_seconds == 9.0
            return True

        def active_process_count(self) -> int:
            return 0

    job = _FakeJob()
    clock = iter((1.0, 2.0, 10.0))
    monkeypatch.setattr(runner_subject, "WindowsJobObject", _FakeJob)
    monkeypatch.setattr(runner_subject, "_monotonic", lambda: next(clock))

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_subject._wait_job_empty(
            job_owner=job,
            operation_deadline=10.0,
        )

    assert exc_info.value.code == "operation_timeout"


def test_quarantine_drain_closes_terminal_error_with_completion(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    completion = Completion(
        operation=OperationKind.CONNECT,
        native_outcome=NativeOutcome.CANCELLED,
        bytes_transferred=0,
        winerror=995,
        deadline_exceeded=True,
        cancel_outcome=CancelOutcome.ACCEPTED,
        cancel_winerror=None,
    )

    class _TerminalOperation:
        closed = False

        def cancel_and_drain(self) -> object:
            raise OverlappedPipeError(
                reason=FailureReason.DEADLINE_EXCEEDED,
                stage=LifecycleStage.DRAIN,
                winerror=995,
                completion=completion,
                resource_owner=self,
            )

        def close(self) -> None:
            self.closed = True

    class _Pipe:
        closed = False

        def __init__(self, operation: object) -> None:
            self.active_operation = operation

        def close(self) -> None:
            self.closed = True

    operation = _TerminalOperation()
    pipe = _Pipe(operation)
    record = runner_harness.runner._records[id(prepared)]
    record.pipe_owner = pipe
    errors: list[BaseException] = []

    assert runner_harness.runner._drain_pipe_for_quarantine(record, errors)
    assert errors == []
    assert operation.closed is True
    assert pipe.closed is True
    assert pipe.active_operation is None
    assert record.io_drain_pending is False
    assert record.pipe_closed is True


def test_pending_pipe_drain_is_retained_and_resolved_by_explicit_poll(
    runner_harness: _RunnerHarness,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )

    class _PendingOperation:
        calls = 0
        closed = False

        def cancel_and_drain(self) -> object:
            self.calls += 1
            if self.calls == 1:
                raise OverlappedPipeError(
                    reason=FailureReason.DRAIN_PENDING,
                    stage=LifecycleStage.DRAIN,
                    winerror=None,
                    completion=None,
                    resource_owner=self,
                )
            return None

        def close(self) -> None:
            self.closed = True

    class _Pipe:
        closed = False

        def __init__(self, operation: object) -> None:
            self.active_operation = operation

        def close(self) -> None:
            self.closed = True

    operation = _PendingOperation()
    pipe = _Pipe(operation)
    record = runner_harness.runner._records[id(prepared)]
    record.pipe_owner = pipe
    errors: list[BaseException] = []
    assert not runner_harness.runner._drain_pipe_for_quarantine(record, errors)
    assert record.io_drain_pending is True
    assert len(runner_harness.runner._pending_pipe_drains) == 1
    assert runner_harness.runner.poll_io_drain(prepared) == "resolved"
    assert operation.calls == 2
    assert operation.closed is True
    assert pipe.closed is True
    assert record.io_drain_pending is False
    assert runner_harness.runner._pending_pipe_drains == {}


def test_pending_pipe_resolution_is_durable_and_allows_restart_reconcile(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
        root_pid=4242,
        root_creation_time=ROOT_CREATION_TIME,
    )
    termination = TerminationEvidence(
        platform="windows_job",
        root_pid=4242,
        exit_code=23,
        termination_method="job_terminate",
        active_processes=0,
        observed_at=ROOT_CREATION_TIME,
    )
    evidence = runner_harness.runner._commit_queried_empty_reconciliation(
        prepared,
        termination,
    )
    runner_harness.runner._commit_quarantine(
        prepared,
        reason_code="io_drain_blocked",
        evidence=evidence,
    )

    class _CompletionOps:
        def close_natural_completion_resources(
            self,
            *,
            process_owner: object,
            job_owner: object,
        ) -> None:
            assert process_owner is process
            assert job_owner is job

    monkeypatch.setattr(
        runner_subject,
        "_WINDOWS_RUNNER_OPS",
        _CompletionOps(),
    )

    class _PendingOperation:
        calls = 0
        closed = False

        def cancel_and_drain(self) -> object:
            self.calls += 1
            if self.calls == 1:
                raise OverlappedPipeError(
                    reason=FailureReason.DRAIN_PENDING,
                    stage=LifecycleStage.DRAIN,
                    winerror=None,
                    completion=None,
                    resource_owner=self,
                )
            return None

        def close(self) -> None:
            self.closed = True

    class _Pipe:
        closed = False

        def __init__(self, operation: object) -> None:
            self.active_operation = operation

        def close(self) -> None:
            self.closed = True

    process = object()
    job = object()
    operation = _PendingOperation()
    pipe = _Pipe(operation)
    record = runner_harness.runner._records[id(prepared)]
    record.process_owner = process
    record.job_owner = job
    record.pipe_owner = pipe
    errors: list[BaseException] = []
    assert not runner_harness.runner._drain_pipe_for_quarantine(record, errors)
    assert runner_harness.runner.poll_io_drain(prepared) == "resolved"
    resolution_path = (
        prepared.control_directory.path
        / runner_subject.QUARANTINE_RESOLUTION_FILENAME
    )
    assert resolution_path.is_file()
    resolution = _read_json(resolution_path)
    assert resolution["state"] == "resolved"
    assert resolution["quarantine_sha256"] == record.quarantine_hash
    assert resolution["termination_sha256"] == record.reconciliation_hash
    assert record.completion_resources_closed is True

    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        assert recovered.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        ) == evidence
    finally:
        recovered.close()


def test_pending_pipe_poll_reaches_durable_last_handle_resolution(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )

    class _PendingOperation:
        calls = 0
        closed = False

        def cancel_and_drain(self) -> object:
            self.calls += 1
            if self.calls == 1:
                raise OverlappedPipeError(
                    reason=FailureReason.DRAIN_PENDING,
                    stage=LifecycleStage.DRAIN,
                    winerror=None,
                    completion=None,
                    resource_owner=self,
                )
            return None

        def close(self) -> None:
            self.closed = True

    class _Pipe:
        closed = False

        def __init__(self, operation: object) -> None:
            self.active_operation = operation

        def close(self) -> None:
            self.closed = True

    class _LastHandleOps:
        job_closes = 0

        def close_last_handle_job(self, *, job_owner: object) -> None:
            self.job_closes += 1

    operation = _PendingOperation()
    pipe = _Pipe(operation)
    ops = _LastHandleOps()
    record = runner_harness.runner._records[id(prepared)]
    record.pipe_owner = pipe
    record.job_owner = object()
    errors: list[BaseException] = []
    assert not runner_harness.runner._drain_pipe_for_quarantine(record, errors)
    runner_harness.runner._commit_quarantine(
        prepared,
        reason_code="io_drain_blocked",
        evidence=None,
    )
    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", ops)

    assert runner_harness.runner.poll_io_drain(prepared) == "resolved"
    assert ops.job_closes == 1
    assert operation.closed is True
    assert pipe.closed is True
    assert record.reconciliation is not None
    assert record.reconciliation.method == "last_handle_close_kill_on_close"
    for filename in (
        runner_subject.LAST_HANDLE_CLOSE_INTENT_FILENAME,
        runner_subject.LAST_HANDLE_CLOSED_FILENAME,
        runner_subject.TERMINATION_FILENAME,
        runner_subject.QUARANTINE_RESOLUTION_FILENAME,
    ):
        assert (prepared.control_directory.path / filename).is_file()


def test_pending_pipe_poll_after_monitor_deadline_still_accepts_late_terminal(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )

    class _LateTerminalOperation:
        calls = 0
        closed = False

        def cancel_and_drain(self) -> object:
            self.calls += 1
            if self.calls == 1:
                raise OverlappedPipeError(
                    reason=FailureReason.DRAIN_PENDING,
                    stage=LifecycleStage.DRAIN,
                    winerror=None,
                    completion=None,
                    resource_owner=self,
                )
            raise OverlappedPipeError(
                reason=FailureReason.DEADLINE_EXCEEDED,
                stage=LifecycleStage.DRAIN,
                winerror=995,
                completion=Completion(
                    operation=OperationKind.CONNECT,
                    native_outcome=NativeOutcome.CANCELLED,
                    bytes_transferred=0,
                    winerror=995,
                    deadline_exceeded=True,
                    cancel_outcome=CancelOutcome.ACCEPTED,
                    cancel_winerror=None,
                ),
                resource_owner=self,
            )

        def close(self) -> None:
            self.closed = True

    class _Pipe:
        closed = False

        def __init__(self, operation: object) -> None:
            self.active_operation = operation

        def close(self) -> None:
            self.closed = True

    operation = _LateTerminalOperation()
    pipe = _Pipe(operation)
    record = runner_harness.runner._records[id(prepared)]
    record.pipe_owner = pipe
    errors: list[BaseException] = []
    monkeypatch.setattr(runner_subject, "_monotonic", lambda: 1.0)
    assert not runner_harness.runner._drain_pipe_for_quarantine(record, errors)
    key = (record.execution_id, record.attempt_id, record.control_id)
    runner_harness.runner._pending_pipe_drains[key].monitor_deadline = 0.5
    assert runner_harness.runner.poll_io_drain(prepared) == "resolved"
    assert operation.closed is True
    assert pipe.closed is True


def test_pending_pipe_poll_does_not_resolve_after_completion_close_unknown(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )

    class _CompletionOps:
        def close_natural_completion_resources(
            self,
            *,
            process_owner: object,
            job_owner: object,
        ) -> None:
            raise OSError("completion close unknown")

    monkeypatch.setattr(
        runner_subject,
        "_WINDOWS_RUNNER_OPS",
        _CompletionOps(),
    )

    class _Operation:
        calls = 0
        closed = False

        def cancel_and_drain(self) -> object:
            self.calls += 1
            if self.calls == 1:
                raise OverlappedPipeError(
                    reason=FailureReason.DRAIN_PENDING,
                    stage=LifecycleStage.DRAIN,
                    winerror=None,
                    completion=None,
                    resource_owner=self,
                )
            return None

        def close(self) -> None:
            self.closed = True

    class _Pipe:
        closed = False

        def __init__(self, operation: object) -> None:
            self.active_operation = operation

        def close(self) -> None:
            self.closed = True

    process = object()
    job = object()
    operation = _Operation()
    pipe = _Pipe(operation)
    record = runner_harness.runner._records[id(prepared)]
    record.process_owner = process
    record.job_owner = job
    record.reconciliation = ReconciliationEvidence(
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        job_instance_id=prepared.job_instance_id,
        method="never_created",
        observation_basis="pre_create_journal",
        active_processes=None,
        root_pid=None,
        operation_may_have_started=False,
        observed_at=ROOT_CREATION_TIME,
        container_sha256=prepared.container_hash,
    )
    record.pipe_owner = pipe
    errors: list[BaseException] = []
    assert not runner_harness.runner._drain_pipe_for_quarantine(record, errors)
    key = (record.execution_id, record.attempt_id, record.control_id)
    assert runner_harness.runner.poll_io_drain(prepared) == "pending"
    # A second poll cannot turn a one-shot close failure into a false
    # resolution, even though the overlapped pipe itself is already closed.
    assert runner_harness.runner.poll_io_drain(prepared) == "pending"
    assert key in runner_harness.runner._pending_pipe_drains
    assert not (
        prepared.control_directory.path
        / runner_subject.QUARANTINE_RESOLUTION_FILENAME
    ).exists()


def test_intentional_last_handle_close_persists_intent_before_native_close(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
        root_pid=4242,
        root_creation_time=ROOT_CREATION_TIME,
    )
    runner_harness.runner._commit_quarantine(
        prepared,
        reason_code="io_drain_blocked",
        evidence=None,
    )
    record = runner_harness.runner._records[id(prepared)]
    job = object()
    process = object()
    record.job_owner = job
    record.process_owner = process
    events: list[str] = []

    class _LastHandleOps:
        def close_last_handle_job(self, *, job_owner: object) -> None:
            assert job_owner is job
            events.append("job_close")
            assert (
                prepared.control_directory.path
                / runner_subject.LAST_HANDLE_CLOSE_INTENT_FILENAME
            ).is_file()

        def wait_exact_process_exit(
            self,
            *,
            pid: int,
            creation_time: str,
            timeout_seconds: float,
        ) -> bool:
            assert (pid, creation_time, timeout_seconds) == (
                4242,
                ROOT_CREATION_TIME,
                10.0,
            )
            events.append("root_exit")
            return True

        def close_last_handle_process(self, *, process_owner: object) -> None:
            assert process_owner is process
            events.append("process_close")

    real_write_json = runner_subject.write_durable_json

    def write_json(**kwargs: object) -> object:
        filename = kwargs["filename"]
        if filename in {
            runner_subject.LAST_HANDLE_CLOSE_INTENT_FILENAME,
            runner_subject.LAST_HANDLE_CLOSED_FILENAME,
        }:
            events.append(f"durable:{filename}")
        return real_write_json(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _LastHandleOps())
    monkeypatch.setattr(runner_subject, "write_durable_json", write_json)

    evidence = runner_harness.runner._commit_intentional_last_handle_close(prepared)
    assert evidence.method == "last_handle_close_kill_on_close"
    assert evidence.active_processes is None
    assert evidence.root_pid == 4242
    assert events == [
        f"durable:{runner_subject.LAST_HANDLE_CLOSE_INTENT_FILENAME}",
        "job_close",
        "root_exit",
        "process_close",
        f"durable:{runner_subject.LAST_HANDLE_CLOSED_FILENAME}",
        "root_exit",
    ]
    record = runner_harness.runner._records[id(prepared)]
    assert record.last_handle_close_attempted is True
    assert record.last_handle_job_closed is True
    assert record.last_handle_closed is True
    assert record.completion_resources_closed is True
    intent = _read_json(
        prepared.control_directory.path
        / runner_subject.LAST_HANDLE_CLOSE_INTENT_FILENAME
    )
    closed = _read_json(
        prepared.control_directory.path / runner_subject.LAST_HANDLE_CLOSED_FILENAME
    )
    assert intent["state"] == "intent"
    assert closed["state"] == "closed"
    assert closed["intent_sha256"] == hashlib.sha256(
        (
            prepared.control_directory.path
            / runner_subject.LAST_HANDLE_CLOSE_INTENT_FILENAME
        ).read_bytes()
    ).hexdigest()
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        assert recovered.reconcile(
            execution_id=prepared.execution_id,
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
        ) == evidence
    finally:
        recovered.close()


def test_intentional_last_handle_close_failure_is_one_shot_and_never_writes_closed(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._commit_quarantine(
        prepared,
        reason_code="io_drain_blocked",
        evidence=None,
    )
    record = runner_harness.runner._records[id(prepared)]
    record.job_owner = object()
    calls = 0

    class _FailingOps:
        def close_last_handle_job(self, *, job_owner: object) -> None:
            nonlocal calls
            calls += 1
            raise OSError("injected CloseHandle failure")

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _FailingOps())
    for _ in range(2):
        with pytest.raises(MaterialOperationRunnerError) as exc_info:
            runner_harness.runner._commit_intentional_last_handle_close(prepared)
        assert exc_info.value.code == "last_handle_close_unknown"
    assert calls == 1
    assert (
        prepared.control_directory.path
        / runner_subject.LAST_HANDLE_CLOSE_INTENT_FILENAME
    ).is_file()
    assert not (
        prepared.control_directory.path / runner_subject.LAST_HANDLE_CLOSED_FILENAME
    ).exists()
    assert not (
        prepared.control_directory.path / runner_subject.TERMINATION_FILENAME
    ).exists()


def test_intentional_last_handle_close_rechecks_native_state_after_durable_intent(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._commit_quarantine(
        prepared,
        reason_code="io_drain_blocked",
        evidence=None,
    )
    record = runner_harness.runner._records[id(prepared)]
    record.job_owner = object()
    native_closes = 0

    class _Ops:
        def close_last_handle_job(self, *, job_owner: object) -> None:
            nonlocal native_closes
            native_closes += 1

    real_write_json = runner_subject.write_durable_json

    def race_after_intent(**kwargs: object) -> object:
        receipt = real_write_json(**kwargs)  # type: ignore[arg-type]
        if kwargs["filename"] == runner_subject.LAST_HANDLE_CLOSE_INTENT_FILENAME:
            # Model a paired-close contender crossing its one-shot boundary
            # immediately after intent durability but before native Job close.
            with runner_harness.runner._lock:
                record.completion_close_attempted = True
        return receipt

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _Ops())
    monkeypatch.setattr(runner_subject, "write_durable_json", race_after_intent)
    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner._commit_intentional_last_handle_close(prepared)
    assert exc_info.value.code == "last_handle_close_state_changed"
    assert native_closes == 0
    assert not (
        prepared.control_directory.path / runner_subject.LAST_HANDLE_CLOSED_FILENAME
    ).exists()


def test_intentional_last_handle_close_retries_resolution_without_reclosing_job(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._commit_quarantine(
        prepared,
        reason_code="io_drain_blocked",
        evidence=None,
    )
    record = runner_harness.runner._records[id(prepared)]
    record.job_owner = object()

    class _Ops:
        job_closes = 0

        def close_last_handle_job(self, *, job_owner: object) -> None:
            self.job_closes += 1

    ops = _Ops()
    real_write_json = runner_subject.write_durable_json
    fail_resolution = True

    def fail_first_resolution(**kwargs: object) -> object:
        if (
            fail_resolution
            and kwargs["filename"] == runner_subject.QUARANTINE_RESOLUTION_FILENAME
        ):
            raise OSError("injected resolution write failure")
        return real_write_json(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", ops)
    monkeypatch.setattr(runner_subject, "write_durable_json", fail_first_resolution)
    with pytest.raises(OSError):
        runner_harness.runner._commit_intentional_last_handle_close(prepared)
    assert ops.job_closes == 1
    assert record.reconciliation is not None
    assert not (
        prepared.control_directory.path
        / runner_subject.QUARANTINE_RESOLUTION_FILENAME
    ).exists()

    fail_resolution = False
    evidence = runner_harness.runner._commit_intentional_last_handle_close(prepared)
    assert evidence.method == "last_handle_close_kill_on_close"
    assert ops.job_closes == 1
    assert (
        prepared.control_directory.path
        / runner_subject.QUARANTINE_RESOLUTION_FILENAME
    ).is_file()


def test_intentional_last_handle_close_retries_closed_journal_without_reclosing_job(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._commit_quarantine(
        prepared,
        reason_code="io_drain_blocked",
        evidence=None,
    )
    record = runner_harness.runner._records[id(prepared)]
    record.job_owner = object()
    job_closes = 0
    fail_closed_write = True

    class _Ops:
        def close_last_handle_job(self, *, job_owner: object) -> None:
            nonlocal job_closes
            job_closes += 1

    real_write_json = runner_subject.write_durable_json

    def fail_first_closed_write(**kwargs: object) -> object:
        nonlocal fail_closed_write
        if (
            fail_closed_write
            and kwargs["filename"] == runner_subject.LAST_HANDLE_CLOSED_FILENAME
        ):
            fail_closed_write = False
            raise OSError("injected closed-journal write failure")
        return real_write_json(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _Ops())
    monkeypatch.setattr(runner_subject, "write_durable_json", fail_first_closed_write)
    with pytest.raises(OSError):
        runner_harness.runner._commit_intentional_last_handle_close(prepared)
    assert job_closes == 1
    assert record.last_handle_closed is True
    assert not (
        prepared.control_directory.path / runner_subject.LAST_HANDLE_CLOSED_FILENAME
    ).exists()

    evidence = runner_harness.runner._commit_intentional_last_handle_close(prepared)
    assert evidence.method == "last_handle_close_kill_on_close"
    assert job_closes == 1
    assert (
        prepared.control_directory.path / runner_subject.LAST_HANDLE_CLOSED_FILENAME
    ).is_file()


def test_intentional_last_handle_close_retries_only_root_wait_after_job_closed(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
        root_pid=4242,
        root_creation_time=ROOT_CREATION_TIME,
    )
    runner_harness.runner._commit_quarantine(
        prepared,
        reason_code="io_drain_blocked",
        evidence=None,
    )
    record = runner_harness.runner._records[id(prepared)]
    record.job_owner = object()
    record.process_owner = object()

    class _LastHandleOps:
        job_closes = 0
        root_exited = False
        process_closes = 0

        def close_last_handle_job(self, *, job_owner: object) -> None:
            self.job_closes += 1

        def wait_exact_process_exit(
            self,
            *,
            pid: int,
            creation_time: str,
            timeout_seconds: float,
        ) -> bool:
            return self.root_exited

        def close_last_handle_process(self, *, process_owner: object) -> None:
            self.process_closes += 1

    ops = _LastHandleOps()
    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", ops)
    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner_harness.runner._commit_intentional_last_handle_close(prepared)
    assert exc_info.value.code == "last_handle_close_root_unproven"
    assert ops.job_closes == 1
    assert ops.process_closes == 0
    assert not (
        prepared.control_directory.path / runner_subject.LAST_HANDLE_CLOSED_FILENAME
    ).exists()

    ops.root_exited = True
    evidence = runner_harness.runner._commit_intentional_last_handle_close(prepared)
    assert evidence.method == "last_handle_close_kill_on_close"
    assert ops.job_closes == 1
    assert ops.process_closes == 1


def test_reconcile_rejects_forged_last_handle_closed_pair(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    descriptor = decode_material_container_descriptor(
        (prepared.control_directory.path / runner_subject.CONTAINER_FILENAME).read_bytes()
    )
    common = {
        "protocol_version": 1,
        "execution_id": prepared.execution_id,
        "attempt_id": prepared.attempt_id,
        "control_id": prepared.control_id,
        "job_instance_id": prepared.job_instance_id,
        "phase": descriptor.phase.value,
        "operation_may_have_started": False,
        "container_sha256": prepared.container_hash,
        "controller_pid": descriptor.controller_pid,
        "controller_creation_time": descriptor.controller_creation_time,
        "root_pid": None,
        "root_creation_time": None,
    }
    write_durable_json(
        parent=prepared.control_directory,
        filename=runner_subject.LAST_HANDLE_CLOSE_INTENT_FILENAME,
        payload={**common, "state": "intent", "intent_at": ROOT_CREATION_TIME},
        replace=False,
        max_bytes=runner_subject.MAX_LAST_HANDLE_CLOSE_INTENT_BYTES,
    )
    write_durable_json(
        parent=prepared.control_directory,
        filename=runner_subject.LAST_HANDLE_CLOSED_FILENAME,
        payload={
            **common,
            "state": "closed",
            "intent_sha256": "f" * 64,
            "closed_at": ROOT_CREATION_TIME,
        },
        replace=False,
        max_bytes=runner_subject.MAX_LAST_HANDLE_CLOSED_BYTES,
    )
    evidence = ReconciliationEvidence(
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        job_instance_id=prepared.job_instance_id,
        method="last_handle_close_kill_on_close",
        observation_basis="controller_closed_last_handle_inference",
        active_processes=None,
        root_pid=None,
        operation_may_have_started=False,
        observed_at=ROOT_CREATION_TIME,
        container_sha256=prepared.container_hash,
    )
    write_durable_json(
        parent=prepared.control_directory,
        filename=runner_subject.TERMINATION_FILENAME,
        payload=runner_subject._reconciliation_to_mapping(evidence),
        replace=False,
        max_bytes=runner_subject.MAX_TERMINATION_BYTES,
    )

    class _ExitProbe:
        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _ExitProbe())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(MaterialOperationRunnerError) as exc_info:
            recovered.reconcile(
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
            )
        assert exc_info.value.code == "reconciliation_blocked"
    finally:
        recovered.close()


def test_reconcile_rejects_last_handle_pair_removed_during_root_wait(
    runner_harness: _RunnerHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = runner_harness.track(
        runner_harness.runner.prepare_attempt(
            execution_id=EXECUTION_ID,
            operation="pexels",
        )
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.JOB_CONFIGURED,
        request_sha256=REQUEST_SHA256,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATE_INTENT,
    )
    runner_harness.runner._advance_launch_descriptor(
        prepared,
        phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
        root_pid=4242,
        root_creation_time=ROOT_CREATION_TIME,
    )
    runner_harness.runner._commit_quarantine(
        prepared,
        reason_code="io_drain_blocked",
        evidence=None,
    )
    record = runner_harness.runner._records[id(prepared)]
    record.job_owner = object()
    record.process_owner = object()

    class _LiveOps:
        def close_last_handle_job(self, *, job_owner: object) -> None:
            pass

        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            return True

        def close_last_handle_process(self, *, process_owner: object) -> None:
            pass

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _LiveOps())
    runner_harness.runner._commit_intentional_last_handle_close(prepared)
    closed_path = (
        prepared.control_directory.path / runner_subject.LAST_HANDLE_CLOSED_FILENAME
    )
    assert closed_path.is_file()

    class _RacingExitProbe:
        removed = False

        def wait_exact_process_exit(self, **_kwargs: object) -> bool:
            if not self.removed:
                closed_path.unlink()
                self.removed = True
            return True

    monkeypatch.setattr(runner_subject, "_WINDOWS_RUNNER_OPS", _RacingExitProbe())
    recovered = ProcessMaterialOperationRunner(root=runner_harness.root)
    try:
        with pytest.raises(MaterialOperationRunnerError) as exc_info:
            recovered.reconcile(
                execution_id=prepared.execution_id,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
            )
        assert exc_info.value.code == "reconciliation_blocked"
    finally:
        recovered.close()


def test_real_secret_pipe_connect_reaches_terminal_and_releases_operation() -> None:
    owner = runner_subject._WindowsRunnerOps().create_secret_pipe(
        forbidden_ids=(
            EXECUTION_ID,
            OTHER_ATTEMPT_ID,
            CONTROL_ID,
            JOB_INSTANCE_ID,
        )
    )
    client_handle: list[object] = []
    client_errors: list[BaseException] = []
    connected = threading.Event()

    def connect_client() -> None:
        try:
            handle = runner_subject.win32file.CreateFile(
                owner.name,
                runner_subject.win32con.GENERIC_READ,
                0,
                None,
                runner_subject.win32con.OPEN_EXISTING,
                0,
                None,
            )
            client_handle.append(handle)
            connected.set()
        except BaseException as error:
            client_errors.append(error)
            connected.set()

    thread = threading.Thread(target=connect_client, daemon=False)
    thread.start()
    try:
        runner_subject._WindowsRunnerOps().connect_secret_pipe(
            pipe_owner=owner,
            absolute_deadline=time.monotonic() + 5.0,
        )
        assert connected.wait(5.0)
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert client_errors == []
        assert len(client_handle) == 1
        assert owner.active_operation is None
        frame = (5).to_bytes(4, "big") + b"hello"
        runner_subject._WindowsRunnerOps().write_secret_frame(
            pipe_owner=owner,
            immutable_frame=frame,
            absolute_deadline=time.monotonic() + 5.0,
        )
        status, payload = runner_subject.win32file.ReadFile(
            client_handle[0],
            len(frame),
        )
        assert status in {0, None}
        assert bytes(payload) == frame
        assert owner.active_operation is None
    finally:
        for handle in client_handle:
            handle.Close()  # type: ignore[union-attr]
        owner.close()


_TASK3B7_SCENARIOS: tuple[tuple[str | None, str | None], ...] = (
    ("planned", None),
    ("job_configured", None),
    ("create_intent", None),
    ("created_in_job_suspended", None),
    ("resume_intent", None),
    ("resumed_no_secret", None),
    ("secret_send_intent", None),
    ("secret_sent", None),
    ("ready", None),
    (None, "create"),
    (None, "query"),
    (None, "resume"),
    (None, "write"),
    (None, "ready"),
    (None, "raw_create"),
    (None, "raw_query"),
    (None, "raw_resume"),
    (None, "raw_write"),
)


def _task3b7_snapshot(directory: Path) -> tuple[tuple[str, str], ...]:
    if not directory.exists():
        return ()
    result: list[tuple[str, str]] = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            result.append(
                (
                    str(path.relative_to(directory)),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
    return tuple(result)


def _read_task3b7_checkpoint(path: Path) -> dict[str, object]:
    deadline = time.monotonic() + 5.0
    while True:
        try:
            return _read_json(path)
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def _open_task3b7_exact_process(
    *,
    pid: int,
    creation_time: str,
    allow_terminate: bool,
) -> object:
    assert runner_subject.win32api is not None
    assert runner_subject.win32process is not None
    desired_access = 0x00100000 | 0x00001000
    if allow_terminate:
        desired_access |= 0x00000001
    handle = runner_subject.win32api.OpenProcess(
        desired_access,
        False,
        pid,
    )
    try:
        assert int(runner_subject.win32process.GetProcessId(handle)) == pid
        process_times = runner_subject.win32process.GetProcessTimes(handle)
        observed_creation = process_times["CreationTime"]
        assert isinstance(observed_creation, datetime)
        assert observed_creation.tzinfo is not None
        observed_creation_time = (
            observed_creation.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        assert observed_creation_time == creation_time
        return handle
    except BaseException:
        handle.Close()
        raise


@pytest.mark.skipif(os.name != "nt", reason="Task 3B-7 crash matrix is Windows-only")
@pytest.mark.parametrize(
    ("target_phase", "boundary"),
    _TASK3B7_SCENARIOS,
    ids=[
        "phase-" + (phase or "none")
        + ("-boundary-" + boundary if boundary else "")
        for phase, boundary in _TASK3B7_SCENARIOS
    ],
)
def test_real_windows_task3b7_crash_matrix(
    tmp_path: Path,
    target_phase: str | None,
    boundary: str | None,
) -> None:
    assert tmp_path.drive.casefold() == "d:"
    backend_root = Path(__file__).resolve().parents[1]
    controller_fixture = (
        Path(__file__).parent / "fixtures" / "material_operation_crash_controller.py"
    )
    checkpoint_name = f"crash-{uuid4().hex}.json"
    # ``Scripts/python.exe`` installs the venv prefix needed by the runner's
    # worker preflight.  On this Windows runtime it is a launcher, so the
    # descriptor controller PID is the process killed below, not Popen.pid.
    controller_executable = Path(sys.executable).resolve()
    assert controller_executable.drive.casefold() == "d:"
    command = [
        str(controller_executable),
        str(controller_fixture),
        "--root",
        str(tmp_path),
        "--checkpoint",
        checkpoint_name,
    ]
    if target_phase is not None:
        command.extend(("--target-phase", target_phase))
    if boundary is not None:
        command.extend(("--boundary", boundary))
    environment = os.environ.copy()
    for key in ("TEMP", "TMP", "TMPDIR"):
        environment[key] = str(tmp_path)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    site_packages = backend_root / ".venv" / "Lib" / "site-packages"
    environment["PYTHONPATH"] = os.pathsep.join(
        str(path)
        for path in (
            backend_root,
            site_packages,
            site_packages / "win32",
            site_packages / "win32" / "lib",
            site_packages / "pythonwin",
        )
    )
    controller = subprocess.Popen(
        command,
        cwd=backend_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    checkpoint_path = tmp_path / checkpoint_name
    controller_handle: object | None = None
    worker_handle: object | None = None
    checkpoint: dict[str, object] | None = None
    try:
        deadline = time.monotonic() + 30.0
        while not checkpoint_path.exists():
            if controller.poll() is not None:
                stdout, stderr = controller.communicate(timeout=5.0)
                raise AssertionError(
                    f"crash controller exited before checkpoint: {stdout}\n{stderr}"
                )
            if time.monotonic() >= deadline:
                raise AssertionError("crash controller did not publish checkpoint")
            time.sleep(0.02)
        checkpoint = _read_task3b7_checkpoint(checkpoint_path)
        assert checkpoint["state"] == "checkpoint"
        controller_pid = checkpoint["controller_pid"]
        controller_creation_time = checkpoint["controller_creation_time"]
        assert isinstance(controller_pid, int)
        assert controller_pid > 0
        assert isinstance(controller_creation_time, str)
        controller_handle = _open_task3b7_exact_process(
            pid=controller_pid,
            creation_time=controller_creation_time,
            allow_terminate=True,
        )
        expected_label = (
            f"phase:{target_phase}"
            if target_phase is not None
            else (
                "boundary:ready_parent_flush"
                if boundary == "ready"
                else f"boundary:{boundary}_return"
            )
        )
        assert checkpoint["label"] == expected_label
        control_id = checkpoint["control_id"]
        attempt_id = checkpoint["attempt_id"]
        assert isinstance(control_id, str)
        assert isinstance(attempt_id, str)
        attempt_directory = tmp_path / ".jobs" / attempt_id
        control_directory = tmp_path / ".jobs-control" / control_id
        assert attempt_directory.is_dir()
        assert control_directory.is_dir()
        attempt_before = _task3b7_snapshot(attempt_directory)
        descriptor_before = (control_directory / "container.json").read_bytes()
        assert checkpoint["execution_id"] == EXECUTION_ID
        assert checkpoint["container_sha256"] == hashlib.sha256(
            descriptor_before
        ).hexdigest()
        root_pid = checkpoint["root_pid"]
        root_creation_time = checkpoint["root_creation_time"]
        if boundary is not None:
            # Every controller/native boundary is after a real child has been
            # created; accepting a missing PID would turn a crash test into a
            # descriptor-only test and would not prove Job containment.
            assert isinstance(root_pid, int)
            assert root_pid > 0
            assert isinstance(root_creation_time, str)
        if root_pid is not None:
            assert isinstance(root_pid, int)
            assert isinstance(root_creation_time, str)
            worker_handle = _open_task3b7_exact_process(
                pid=root_pid,
                creation_time=root_creation_time,
                allow_terminate=False,
            )
        assert runner_subject.win32api is not None
        assert runner_subject.win32event is not None
        runner_subject.win32api.TerminateProcess(controller_handle, 137)
        assert (
            runner_subject.win32event.WaitForSingleObject(
                controller_handle,
                10_000,
            )
            == runner_subject.win32event.WAIT_OBJECT_0
        )
        controller.wait(timeout=10.0)
        assert controller.returncode != 0
        if worker_handle is not None:
            assert (
                runner_subject.win32event.WaitForSingleObject(
                    worker_handle,
                    10_000,
                )
                == runner_subject.win32event.WAIT_OBJECT_0
            )
        time.sleep(2.5)
        assert _task3b7_snapshot(attempt_directory) == attempt_before
        assert not (attempt_directory / "late-write.txt").exists()

        approved_root = approve_physical_d_write_root(tmp_path)
        recovered = ProcessMaterialOperationRunner(root=approved_root)
        try:
            evidence = recovered.reconcile(
                execution_id=checkpoint["execution_id"],  # type: ignore[arg-type]
                attempt_id=attempt_id,
                control_id=control_id,
            )
        finally:
            recovered.close()
            approved_root.close()
        assert (control_directory / "container.json").read_bytes() == descriptor_before
        descriptor = decode_material_container_descriptor(descriptor_before)
        assert descriptor.phase.value == checkpoint["phase"]
        if descriptor.phase in {
            MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
            MaterialLaunchPhase.RESUME_INTENT,
            MaterialLaunchPhase.RESUMED_NO_SECRET,
            MaterialLaunchPhase.SECRET_SEND_INTENT,
            MaterialLaunchPhase.SECRET_SENT,
            MaterialLaunchPhase.READY,
        }:
            assert isinstance(root_pid, int)
            assert root_pid > 0
            assert isinstance(root_creation_time, str)
        if descriptor.phase in {
            MaterialLaunchPhase.SECRET_SEND_INTENT,
            MaterialLaunchPhase.SECRET_SENT,
            MaterialLaunchPhase.READY,
        }:
            assert descriptor.operation_may_have_started is True
        if target_phase == "ready" or boundary == "ready":
            assert (attempt_directory / "ready.json").is_file()
        assert evidence.active_processes is None
        if descriptor.phase in {
            MaterialLaunchPhase.PLANNED,
            MaterialLaunchPhase.JOB_CONFIGURED,
        }:
            assert evidence.method == "never_created"
            assert evidence.root_pid is None
            assert evidence.operation_may_have_started is False
        elif descriptor.phase is MaterialLaunchPhase.CREATE_INTENT:
            # Create/Query can return with a live root before the durable
            # created-in-job-suspended tip exists; recovery must not invent
            # that root identity from a volatile checkpoint.
            assert evidence.method == "owner_exit_kill_on_close"
            assert evidence.root_pid is None
            assert evidence.operation_may_have_started is False
        else:
            assert evidence.method == "owner_exit_kill_on_close"
            assert evidence.root_pid == root_pid
            assert evidence.operation_may_have_started is (
                descriptor.operation_may_have_started
            )
        assert (control_directory / "termination.json").is_file()
    finally:
        if worker_handle is not None:
            worker_handle.Close()  # type: ignore[union-attr]
        if controller_handle is not None:
            try:
                assert runner_subject.win32api is not None
                assert runner_subject.win32event is not None
                if (
                    runner_subject.win32event.WaitForSingleObject(
                        controller_handle,
                        0,
                    )
                    == runner_subject.win32event.WAIT_TIMEOUT
                ):
                    runner_subject.win32api.TerminateProcess(
                        controller_handle,
                        137,
                    )
                    runner_subject.win32event.WaitForSingleObject(
                        controller_handle,
                        10_000,
                    )
            finally:
                controller_handle.Close()  # type: ignore[union-attr]
        if controller.poll() is None:
            controller.kill()
            controller.wait(timeout=10.0)
        if controller.stdout is not None:
            controller.stdout.close()
        if controller.stderr is not None:
            controller.stderr.close()
