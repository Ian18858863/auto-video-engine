from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import pytest

from app.core.windows_durable_file import write_durable_bytes, write_durable_json
from app.core.windows_storage_policy import (
    ApprovedPhysicalWriteRoot,
    StoragePolicyError,
    approve_physical_d_write_root,
)
from app.pipeline import material_operation_runner as runner_subject
from app.pipeline.material_operation_protocol import (
    MaterialOperationOutcome,
    MaterialWorkerResult,
    StagedArtifact,
    TerminationEvidence,
    encode_material_worker_result,
)
from app.pipeline.material_operation_runner import (
    MaterialAttemptRetirementReceipt,
    MaterialLaunchPhase,
    MaterialOperationRunnerError,
    MaterialRestartPublishedRetirementProof,
    MaterialRestartRetirementProof,
    MaterialRunnerClaimBinding,
    PreparedMaterialAttempt,
    ProcessMaterialOperationRunner,
)
from app.pipeline.material_runner_retirement import MaterialRunnerRetirementAttestation


EXECUTION_ID = "123e4567-e89b-12d3-a456-426614174000"
REQUEST_SHA256 = "b" * 64
ROOT_CREATION_TIME = "2026-07-15T12:34:56.123456Z"


def _committed_journal_payload(
    prepared: PreparedMaterialAttempt,
    *,
    attempt_id: str | None = None,
) -> dict[str, object]:
    payload_dir = prepared.directory.path / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)
    destination = payload_dir / "selected.mp4"
    destination.write_bytes(b"x")
    part_path = payload_dir / ".selected.mp4.part"
    part_path.write_bytes(b"x")
    part_stat = part_path.stat()
    part_path.unlink()
    destination_stat = destination.stat()
    registry_path = prepared.directory.path / "registry.json"
    registry_bytes = b'{"format":"registry-v1","entries":[]}\n'
    registry_path.write_bytes(registry_bytes)
    registry_stat = registry_path.stat()
    return {
        "format": "material-publication-v1",
        "execution_id": prepared.execution_id,
        "attempt_id": prepared.attempt_id if attempt_id is None else attempt_id,
        "operation": "pexels",
        "phase": "committed",
        "attestation": "committed",
        "moves": [
            {
                "destination": str(prepared.directory.path / "payload" / "selected.mp4"),
                "part_path": str(part_path),
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "size_bytes": destination.stat().st_size,
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
    }


@pytest.fixture()
def successful_attempt(tmp_path: Path) -> Iterator[
    tuple[ProcessMaterialOperationRunner, PreparedMaterialAttempt, str]
]:
    assert tmp_path.drive.casefold() == "d:"
    root: ApprovedPhysicalWriteRoot = approve_physical_d_write_root(tmp_path)
    runner = ProcessMaterialOperationRunner(root=root)
    prepared = runner.prepare_attempt(
        execution_id=EXECUTION_ID,
        operation="pexels",
    )
    phases = (
        (MaterialLaunchPhase.JOB_CONFIGURED, {"request_sha256": REQUEST_SHA256}),
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

    record = runner._records[id(prepared)]
    record.claim_acquired = True
    record.pipe_close_attempted = True
    record.pipe_closed = True
    record.completion_close_attempted = True
    record.completion_resources_closed = True
    termination = TerminationEvidence(
        platform="windows_job",
        root_pid=4242,
        exit_code=0,
        termination_method="natural",
        active_processes=0,
        observed_at="2026-07-15T12:35:00.000000Z",
    )
    evidence = runner._commit_queried_empty_reconciliation(prepared, termination)
    artifact = StagedArtifact(
        relative_path="payload/selected.mp4",
        media_kind="video",
        source_type="Pexels",
        source_url="https://example.invalid/video",
        evidence_kind="atmosphere",
        is_locally_generated=False,
        sha256=hashlib.sha256(b"x").hexdigest(),
        size_bytes=1,
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
    record.outcome = MaterialOperationOutcome(
        status="succeeded",
        result=result,
        termination=termination,
    )
    runner._commit_termination_outcome(
        prepared,
        record.outcome,
        allow_natural=True,
    )
    journal = write_durable_json(
        parent=prepared.directory,
        filename="publication-journal.json",
        payload=_committed_journal_payload(prepared),
        replace=False,
        max_bytes=runner_subject.MAX_PUBLICATION_JOURNAL_BYTES,
    )
    assert record.reconciliation == evidence
    try:
        yield runner, prepared, journal.sha256
    finally:
        for pin in reversed(prepared.pin_scope):
            pin.close()
        root.close()


def test_published_retirement_writes_tombstone_then_releases_pins(
    successful_attempt,
):
    runner, prepared, journal_sha256 = successful_attempt

    runner.retire_published_attempt(
        prepared,
        publication_journal_sha256=journal_sha256,
    )

    retired = json.loads(
        (prepared.control_directory.path / "retired.json").read_bytes()
    )
    assert retired["state"] == "retired"
    assert retired["reason_code"] == "publication_committed"
    assert retired["publication_journal_sha256"] == journal_sha256
    assert retired["execution_id"] == prepared.execution_id
    assert runner._records[id(prepared)].retired is True
    for pin in prepared.pin_scope:
        with pytest.raises(StoragePolicyError):
            pin.verify()


def test_proof_bound_published_retirement_returns_typed_attestation(
    successful_attempt,
):
    runner, prepared, journal_sha256 = successful_attempt
    record = runner._records[id(prepared)]
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
    record.claim_bound = True
    record.claim_binding = binding
    assert record.outcome is not None and record.outcome.result is not None
    result_sha256 = hashlib.sha256(
        encode_material_worker_result(record.outcome.result)
    ).hexdigest()
    proof = MaterialRestartPublishedRetirementProof(
        status="succeeded",
        execution_id=prepared.execution_id,
        operation="pexels",
        attempt_id=prepared.attempt_id,
        execution_revision=3,
        final_payload_sha256="b" * 64,
        attempt_history_sha256="c" * 64,
        publication_journal_sha256=journal_sha256,
        publication_intent_sha256="d" * 64,
        publication_result_sha256="e" * 64,
        publication_worker_result_sha256=result_sha256,
        publication_reference_base_revision=1,
        publication_reference_base_payload_sha256="a" * 64,
        result_sha256=result_sha256,
        observed_at=record.outcome.termination.observed_at,
        checkpoint_sha256="f" * 64,
    )

    attestation = runner.attest_completed_published_attempt(prepared, proof=proof)

    assert type(attestation) is MaterialRunnerRetirementAttestation
    assert attestation.checkpoint_sha256 == "f" * 64
    assert attestation.completion_payload_sha256 == "b" * 64
    retired = json.loads(
        (prepared.control_directory.path / runner_subject.RETIRED_FILENAME).read_bytes()
    )
    assert retired["checkpoint_sha256"] == "f" * 64
    assert retired["final_payload_sha256"] == "b" * 64


def test_published_retirement_rejects_wrong_journal_hash(successful_attempt):
    runner, prepared, _journal_sha256 = successful_attempt

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner.retire_published_attempt(
            prepared,
            publication_journal_sha256="c" * 64,
        )

    assert exc_info.value.code == "publication_journal_changed"
    assert runner._records[id(prepared)].retired is False
    assert not (prepared.control_directory.path / "retired.json").exists()


def test_published_retirement_rechecks_published_target_bytes(successful_attempt):
    runner, prepared, journal_sha256 = successful_attempt
    (prepared.directory.path / "payload" / "selected.mp4").write_bytes(b"tampered")

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner.retire_published_attempt(
            prepared,
            publication_journal_sha256=journal_sha256,
        )

    assert exc_info.value.code == "publication_target_changed"
    assert runner._records[id(prepared)].retired is False
    assert all(pin.verify() is None for pin in prepared.pin_scope)


def test_published_retirement_rejects_worker_result_from_another_attempt(
    successful_attempt,
):
    runner, prepared, journal_sha256 = successful_attempt
    record = runner._records[id(prepared)]
    foreign_result = replace(
        record.outcome.result,
        execution_id=str(uuid4()),
        attempt_id=str(uuid4()),
    )
    record.outcome = replace(record.outcome, result=foreign_result)

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner.retire_published_attempt(
            prepared,
            publication_journal_sha256=journal_sha256,
        )

    assert exc_info.value.code == "publication_outcome_invalid"
    assert runner._records[id(prepared)].retired is False
    assert all(pin.verify() is None for pin in prepared.pin_scope)


def test_published_retirement_requires_natural_success(successful_attempt):
    runner, prepared, journal_sha256 = successful_attempt
    record = runner._records[id(prepared)]
    record.outcome = None

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner.retire_published_attempt(
            prepared,
            publication_journal_sha256=journal_sha256,
        )

    assert exc_info.value.code == "publication_outcome_unavailable"
    assert all(pin.verify() is None for pin in prepared.pin_scope)


def test_published_retirement_rejects_changed_durable_reconciliation(
    successful_attempt,
):
    runner, prepared, journal_sha256 = successful_attempt
    write_durable_bytes(
        parent=prepared.control_directory,
        filename=runner_subject.TERMINATION_FILENAME,
        payload=b"{}",
        replace=True,
        max_bytes=runner_subject.MAX_TERMINATION_BYTES,
    )

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner.retire_published_attempt(
            prepared,
            publication_journal_sha256=journal_sha256,
        )

    assert exc_info.value.code == "publication_reconciliation_changed"
    assert runner._records[id(prepared)].retired is False
    assert all(pin.verify() is None for pin in prepared.pin_scope)


def test_published_retirement_rejects_journal_for_another_attempt(
    successful_attempt,
):
    runner, prepared, _journal_sha256 = successful_attempt
    receipt = write_durable_json(
        parent=prepared.directory,
        filename=runner_subject.PUBLICATION_JOURNAL_FILENAME,
        payload=_committed_journal_payload(prepared, attempt_id=str(uuid4())),
        replace=True,
        max_bytes=runner_subject.MAX_PUBLICATION_JOURNAL_BYTES,
    )

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner.retire_published_attempt(
            prepared,
            publication_journal_sha256=receipt.sha256,
        )

    assert exc_info.value.code == "publication_journal_invalid"
    assert runner._records[id(prepared)].retired is False
    assert all(pin.verify() is None for pin in prepared.pin_scope)


def test_published_retirement_rejects_unattested_empty_publication_receipt(
    successful_attempt,
):
    runner, prepared, _journal_sha256 = successful_attempt
    payload = _committed_journal_payload(prepared)
    payload["moves"] = []
    payload["registry"] = {}
    receipt = write_durable_json(
        parent=prepared.directory,
        filename=runner_subject.PUBLICATION_JOURNAL_FILENAME,
        payload=payload,
        replace=True,
        max_bytes=runner_subject.MAX_PUBLICATION_JOURNAL_BYTES,
    )

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner.retire_published_attempt(
            prepared,
            publication_journal_sha256=receipt.sha256,
        )

    assert exc_info.value.code == "publication_journal_invalid"
    assert runner._records[id(prepared)].retired is False
    assert all(pin.verify() is None for pin in prepared.pin_scope)


def test_published_retirement_rejects_ambiguous_journal_json(successful_attempt):
    runner, prepared, _journal_sha256 = successful_attempt
    raw = (
        b'{"format":"material-publication-v1",'
        b'"format":"material-publication-v1",'
        b'"execution_id":"123e4567-e89b-12d3-a456-426614174000",'
        + b'"attempt_id":"'
        + prepared.attempt_id.encode("ascii")
        + b'","operation":"pexels","phase":"committed",'
        b'"attestation":"committed","moves":[],"registry":{}}'
    )
    receipt = write_durable_bytes(
        parent=prepared.directory,
        filename=runner_subject.PUBLICATION_JOURNAL_FILENAME,
        payload=raw,
        replace=True,
        max_bytes=runner_subject.MAX_PUBLICATION_JOURNAL_BYTES,
    )

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner.retire_published_attempt(
            prepared,
            publication_journal_sha256=receipt.sha256,
        )

    assert exc_info.value.code == "publication_journal_invalid"
    assert runner._records[id(prepared)].retired is False
    assert all(pin.verify() is None for pin in prepared.pin_scope)


def test_published_retirement_does_not_release_when_tombstone_write_is_unknown(
    successful_attempt,
    monkeypatch: pytest.MonkeyPatch,
):
    runner, prepared, journal_sha256 = successful_attempt
    real_write = runner_subject.write_durable_json

    def fail_retirement(**kwargs: object):
        if kwargs.get("filename") == runner_subject.RETIRED_FILENAME:
            raise OSError("injected retired tombstone failure")
        return real_write(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner_subject, "write_durable_json", fail_retirement)

    with pytest.raises(OSError):
        runner.retire_published_attempt(
            prepared,
            publication_journal_sha256=journal_sha256,
        )

    assert runner._records[id(prepared)].retired is False
    assert all(pin.verify() is None for pin in prepared.pin_scope)


def test_published_retirement_adopts_tombstone_when_write_ack_is_lost(
    successful_attempt,
    monkeypatch: pytest.MonkeyPatch,
):
    runner, prepared, journal_sha256 = successful_attempt
    real_write = runner_subject.write_durable_json

    def write_then_lose_ack(**kwargs: object):
        receipt = real_write(**kwargs)  # type: ignore[arg-type]
        if kwargs.get("filename") == runner_subject.RETIRED_FILENAME:
            raise OSError("injected acknowledgement loss after durable write")
        return receipt

    monkeypatch.setattr(runner_subject, "write_durable_json", write_then_lose_ack)

    runner.retire_published_attempt(
        prepared,
        publication_journal_sha256=journal_sha256,
    )

    assert runner._records[id(prepared)].retired is True
    for pin in prepared.pin_scope:
        with pytest.raises(StoragePolicyError):
            pin.verify()


def test_published_retirement_marks_release_audit_when_pin_close_is_unknown(
    successful_attempt,
    monkeypatch: pytest.MonkeyPatch,
):
    runner, prepared, journal_sha256 = successful_attempt

    def fail_close(_pins):
        raise OSError("injected pin close failure")

    monkeypatch.setattr(runner_subject, "_close_pins", fail_close)

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner.retire_published_attempt(
            prepared,
            publication_journal_sha256=journal_sha256,
        )

    assert exc_info.value.code == "retirement_release_unknown"
    assert runner._records[id(prepared)].retired is True
    audit = json.loads(
        (prepared.control_directory.path / runner_subject.RETIREMENT_RELEASE_AUDIT_FILENAME).read_bytes()
    )
    assert audit["reason_code"] == "pin_release_unknown"


def test_pin_close_failure_preserves_audit_pin_and_attempts_other_pins():
    events: list[str] = []

    class FakePin:
        def __init__(self, name: str, *, fail: bool = False):
            self.name = name
            self.fail = fail

        def close(self):
            events.append(self.name)
            if self.fail:
                raise OSError(f"{self.name} close failed")

    audit_pin = FakePin("audit")
    failing = FakePin("failing", fail=True)
    other = FakePin("other")

    with pytest.raises(BaseExceptionGroup):
        runner_subject._close_pins(
            (audit_pin, failing, other),
            audit_pin=audit_pin,
        )

    assert events == ["other", "failing"]


def test_terminal_failed_retirement_writes_history_bound_tombstone(
    successful_attempt,
):
    runner, prepared, _journal_sha256 = successful_attempt
    record = runner._records[id(prepared)]
    termination = record.outcome.termination
    failed_result = MaterialWorkerResult(
        protocol_version=1,
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        operation="pexels",
        status="failed",
        selected_artifact="",
        artifacts=(),
        reason_code="source_failed",
        reason="source returned no usable artifact",
    )
    record.outcome = MaterialOperationOutcome(
        status="failed",
        result=failed_result,
        termination=termination,
    )
    runner._commit_termination_outcome(
        prepared,
        record.outcome,
        allow_natural=True,
    )
    rolled_back = _committed_journal_payload(prepared)
    rolled_back["phase"] = "rolled_back"
    rolled_back["attestation"] = "rolled_back"
    write_durable_json(
        parent=prepared.directory,
        filename=runner_subject.PUBLICATION_JOURNAL_FILENAME,
        payload=rolled_back,
        replace=True,
        max_bytes=runner_subject.MAX_PUBLICATION_JOURNAL_BYTES,
    )
    (prepared.directory.path / "payload" / "selected.mp4").unlink()
    (prepared.directory.path / "registry.json").unlink()

    runner.retire_terminal_attempt(
        prepared,
        attempt_history_sha256="d" * 64,
    )

    retired = json.loads(
        (prepared.control_directory.path / runner_subject.RETIRED_FILENAME).read_bytes()
    )
    assert retired["reason_code"] == "attempt_failed"
    assert retired["attempt_history_sha256"] == "d" * 64
    assert runner._records[id(prepared)].retired is True
    for pin in prepared.pin_scope:
        with pytest.raises(StoragePolicyError):
            pin.verify()


def test_proof_bound_terminal_retirement_returns_typed_attestation(
    successful_attempt,
):
    runner, prepared, _journal_sha256 = successful_attempt
    record = runner._records[id(prepared)]
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
    record.claim_bound = True
    record.claim_binding = binding
    assert record.outcome is not None
    termination = record.outcome.termination
    failed_result = MaterialWorkerResult(
        protocol_version=1,
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        operation="pexels",
        status="failed",
        selected_artifact="",
        artifacts=(),
        reason_code="source_failed",
        reason="source returned no usable artifact",
    )
    record.outcome = MaterialOperationOutcome(
        status="failed",
        result=failed_result,
        termination=termination,
    )
    runner._commit_termination_outcome(prepared, record.outcome, allow_natural=True)
    rolled_back = _committed_journal_payload(prepared)
    rolled_back["phase"] = "rolled_back"
    rolled_back["attestation"] = "rolled_back"
    write_durable_json(
        parent=prepared.directory,
        filename=runner_subject.PUBLICATION_JOURNAL_FILENAME,
        payload=rolled_back,
        replace=True,
        max_bytes=runner_subject.MAX_PUBLICATION_JOURNAL_BYTES,
    )
    (prepared.directory.path / "payload" / "selected.mp4").unlink()
    (prepared.directory.path / "registry.json").unlink()
    result_sha256 = hashlib.sha256(
        encode_material_worker_result(failed_result)
    ).hexdigest()
    proof = MaterialRestartRetirementProof(
        status="failed",
        execution_id=prepared.execution_id,
        operation="pexels",
        attempt_id=prepared.attempt_id,
        execution_revision=2,
        final_payload_sha256="b" * 64,
        attempt_history_sha256="d" * 64,
        result_sha256=result_sha256,
        observed_at=termination.observed_at,
        checkpoint_sha256="f" * 64,
    )

    attestation = runner.attest_completed_terminal_attempt(prepared, proof=proof)

    assert type(attestation) is MaterialRunnerRetirementAttestation
    assert attestation.status == "failed"
    assert attestation.checkpoint_sha256 == "f" * 64
    retired = json.loads(
        (prepared.control_directory.path / runner_subject.RETIRED_FILENAME).read_bytes()
    )
    assert retired["checkpoint_sha256"] == "f" * 64


def test_terminal_retirement_receipt_uses_adopted_tombstone_timestamp(
    successful_attempt,
    monkeypatch: pytest.MonkeyPatch,
):
    runner, prepared, _journal_sha256 = successful_attempt
    record = runner._records[id(prepared)]
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
    record.claim_bound = True
    record.claim_binding = binding
    termination = record.outcome.termination
    failed_result = MaterialWorkerResult(
        protocol_version=1,
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        operation="pexels",
        status="failed",
        selected_artifact="",
        artifacts=(),
        reason_code="source_failed",
        reason="source returned no usable artifact",
    )
    record.outcome = MaterialOperationOutcome(
        status="failed",
        result=failed_result,
        termination=termination,
    )
    runner._commit_termination_outcome(
        prepared,
        record.outcome,
        allow_natural=True,
    )
    rolled_back = _committed_journal_payload(prepared)
    rolled_back["phase"] = "rolled_back"
    rolled_back["attestation"] = "rolled_back"
    write_durable_json(
        parent=prepared.directory,
        filename=runner_subject.PUBLICATION_JOURNAL_FILENAME,
        payload=rolled_back,
        replace=True,
        max_bytes=runner_subject.MAX_PUBLICATION_JOURNAL_BYTES,
    )
    (prepared.directory.path / "payload" / "selected.mp4").unlink()
    (prepared.directory.path / "registry.json").unlink()

    real_write = runner_subject.write_durable_json
    adopted_retired_at = "2026-07-15T12:36:00.000000Z"

    def write_with_old_timestamp(**kwargs: object):
        if kwargs.get("filename") == runner_subject.RETIRED_FILENAME:
            payload = dict(kwargs["payload"])  # type: ignore[arg-type]
            payload["retired_at"] = adopted_retired_at
            real_write(
                parent=kwargs["parent"],
                filename=kwargs["filename"],
                payload=payload,
                replace=kwargs["replace"],
                max_bytes=kwargs["max_bytes"],
            )
            raise OSError("injected retirement acknowledgement loss")
        return real_write(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner_subject, "write_durable_json", write_with_old_timestamp)
    proof = MaterialRestartRetirementProof(
        status="failed",
        execution_id=prepared.execution_id,
        operation="pexels",
        attempt_id=prepared.attempt_id,
        execution_revision=2,
        final_payload_sha256="f" * 64,
        attempt_history_sha256="d" * 64,
        result_sha256=hashlib.sha256(
            runner_subject.encode_material_worker_result(failed_result)
        ).hexdigest(),
        observed_at=termination.observed_at,
    )

    receipt = runner.retire_terminal_attempt_with_proof(prepared, proof=proof)

    assert type(receipt) is MaterialAttemptRetirementReceipt
    tombstone_bytes = (
        prepared.control_directory.path / runner_subject.RETIRED_FILENAME
    ).read_bytes()
    tombstone = json.loads(tombstone_bytes)
    assert tombstone["retired_at"] == adopted_retired_at
    assert receipt.retired_at == adopted_retired_at
    assert receipt.tombstone_sha256 == hashlib.sha256(tombstone_bytes).hexdigest()


def test_terminal_retirement_rejects_committed_publication_journal(successful_attempt):
    runner, prepared, _journal_sha256 = successful_attempt
    record = runner._records[id(prepared)]
    termination = record.outcome.termination
    failed_result = MaterialWorkerResult(
        protocol_version=1,
        execution_id=prepared.execution_id,
        attempt_id=prepared.attempt_id,
        operation="pexels",
        status="failed",
        selected_artifact="",
        artifacts=(),
        reason_code="source_failed",
        reason="source returned no usable artifact",
    )
    record.outcome = MaterialOperationOutcome(
        status="failed",
        result=failed_result,
        termination=termination,
    )
    runner._commit_termination_outcome(
        prepared,
        record.outcome,
        allow_natural=True,
    )

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner.retire_terminal_attempt(prepared)

    assert exc_info.value.code == "terminal_publication_state_present"
    assert runner._records[id(prepared)].retired is False
    assert all(pin.verify() is None for pin in prepared.pin_scope)


def test_terminal_retirement_rejects_minimal_rolled_back_journal(successful_attempt):
    runner, prepared, _journal_sha256 = successful_attempt
    record = runner._records[id(prepared)]
    failed_result = replace(
        record.outcome.result,
        status="failed",
        selected_artifact="",
        artifacts=(),
        reason_code="source_failed",
        reason="source returned no usable artifact",
    )
    record.outcome = replace(record.outcome, status="failed", result=failed_result)
    runner._commit_termination_outcome(
        prepared,
        record.outcome,
        allow_natural=True,
    )
    minimal = {
        "format": "material-publication-v1",
        "execution_id": prepared.execution_id,
        "attempt_id": prepared.attempt_id,
        "operation": "pexels",
        "phase": "rolled_back",
        "moves": [],
        "registry": {},
    }
    write_durable_json(
        parent=prepared.directory,
        filename=runner_subject.PUBLICATION_JOURNAL_FILENAME,
        payload=minimal,
        replace=True,
        max_bytes=runner_subject.MAX_PUBLICATION_JOURNAL_BYTES,
    )
    (prepared.directory.path / "payload" / "selected.mp4").unlink()
    (prepared.directory.path / "registry.json").unlink()

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner.retire_terminal_attempt(
            prepared,
            attempt_history_sha256="d" * 64,
        )

    assert exc_info.value.code == "terminal_publication_state_invalid"
    assert runner._records[id(prepared)].retired is False
    assert all(pin.verify() is None for pin in prepared.pin_scope)


def test_terminal_terminated_retirement_requires_durable_outcome_journal(
    successful_attempt,
):
    runner, prepared, _journal_sha256 = successful_attempt
    record = runner._records[id(prepared)]
    terminated = replace(
        record.outcome,
        status="terminated",
        result=None,
        termination=replace(
            record.outcome.termination,
            termination_method="job_terminate",
        ),
    )
    record.outcome = terminated
    record.termination_outcome_hash = None
    (prepared.control_directory.path / runner_subject.TERMINATION_OUTCOME_FILENAME).unlink()
    rolled_back = _committed_journal_payload(prepared)
    rolled_back["phase"] = "rolled_back"
    rolled_back["attestation"] = "rolled_back"
    write_durable_json(
        parent=prepared.directory,
        filename=runner_subject.PUBLICATION_JOURNAL_FILENAME,
        payload=rolled_back,
        replace=True,
        max_bytes=runner_subject.MAX_PUBLICATION_JOURNAL_BYTES,
    )

    with pytest.raises(MaterialOperationRunnerError) as exc_info:
        runner.retire_terminal_attempt(
            prepared,
            attempt_history_sha256="d" * 64,
        )

    assert exc_info.value.code == "terminal_outcome_unavailable"
    assert runner._records[id(prepared)].retired is False
    assert all(pin.verify() is None for pin in prepared.pin_scope)
