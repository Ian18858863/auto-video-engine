from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
from uuid import uuid4

from app.materials.asset_paths import AutoMaterialPaths
from app.pipeline.material_execution_application import (
    MaterialExecutionCommand,
    MaterialExecutionLaneKey,
    MaterialExecutionOwnerLane,
    MaterialExecutionResponse,
)
from app.pipeline.material_execution_coordinator import MaterialCompletionReceipt
from app.pipeline.material_operation_protocol import (
    InfoMotionPayload,
    InfoMotionSecrets,
    MaterialOperationOutcome,
    MaterialWorkerResult,
    PexelsSecrets,
    StockSearchPayload,
    StagedArtifact,
    TerminationEvidence,
)
from app.pipeline.material_operation_runner import (
    MaterialAttemptRetirementReceipt,
    MaterialRestartRetirementProof,
    PreparedMaterialAttempt,
)
from app.pipeline.material_runner_retirement import (
    MaterialRunnerRetirementAcknowledgement,
    MaterialRunnerRetirementAttestation,
)
from app.pipeline.production_material_execution import (
    ProductionMaterialExecutionHandler,
    ProductionMaterialOperationPlan,
)


def _ids() -> tuple[str, str, str, str, str, str, str]:
    return tuple(str(uuid4()) for _ in range(7))  # type: ignore[return-value]


class _Mutex:
    def __init__(self, events: list[tuple[str, int]]) -> None:
        self.events = events
        self.released = False

    def assert_owned(self) -> None:
        if self.released:
            raise RuntimeError("mutex released")

    def release(self) -> None:
        self.events.append(("mutex.release", threading.get_ident()))
        self.released = True


class _Lease:
    def __init__(
        self,
        execution_id: str,
        attempt: PreparedMaterialAttempt,
        mutex: _Mutex,
        events,
    ):
        self.execution_id = execution_id
        self.mutex = mutex
        self.events = events
        self.released = False
        self.active_attempt_id = attempt.attempt_id
        self.active_attempt_kind = "info_motion"
        self.active_control_id = attempt.control_id
        self.active_job_instance_id = attempt.job_instance_id
        self.expected_revision = 1
        self._payload = {
            "material_tasks": [
                {
                    "material_task_id": "task-001",
                    "status": "进行中",
                    "status_history": ["等待", "进行中"],
                }
            ],
            "execution_id": execution_id,
            "execution_started_at": "2026-07-17T00:00:00Z",
            "claim_key": {
                "pipeline_id": 1,
                "material_step_id": 2,
                "material_task_id": "task-001",
            },
            "execution_state": "claimed",
            "publication_phase": "preparing",
            "execution_revision": 1,
            "active_material_task_id": "task-001",
            "active_execution_id": execution_id,
            "active_attempt_id": attempt.attempt_id,
            "active_attempt_kind": "info_motion",
            "active_control_id": attempt.control_id,
            "active_job_instance_id": attempt.job_instance_id,
        }

    @property
    def expected_payload(self):
        return dict(self._payload)

    def assert_owned(self, *_args):
        self.mutex.assert_owned()

    def compare_and_swap(self, replacement):
        self.assert_owned()
        self._payload = dict(replacement)
        self.expected_revision = int(replacement["execution_revision"])

    def release(self):
        self.events.append(("lease.release", threading.get_ident()))
        self.released = True
        self.mutex.release()


class _Store:
    def __init__(self, lease: _Lease, events):
        self.lease = lease
        self.events = events
        self.mutex = lease.mutex

    def acquire_claim_mutex(self, *, timeout_ms=0):
        assert timeout_ms == 0
        self.events.append(("store.acquire", threading.get_ident()))
        self.mutex.assert_owned()
        return self.mutex

    def fresh_read(self):
        self.events.append(("store.fresh", threading.get_ident()))
        return dict(self.lease._payload)

    def claim_lease_with_owned_mutex(self, *, mutex, **_values):
        self.events.append(("store.claim", threading.get_ident()))
        assert mutex is self.mutex
        return self.lease


class _Runner:
    def __init__(self, execution_id: str, events):
        self.execution_id = execution_id
        self.events = events
        self.attempts = [
            _prepared(execution_id),
            _prepared(execution_id),
        ]
        self.index = 0
        self.bound = {}

    def prepare_attempt(self, *, execution_id: str, operation: str):
        self.events.append((f"runner.prepare:{operation}", threading.get_ident()))
        prepared = self.attempts[self.index]
        self.index += 1
        assert prepared.execution_id == execution_id
        return prepared

    def bind_claim(self, prepared, binding):
        self.events.append(("runner.bind", threading.get_ident()))
        self.bound[prepared.attempt_id] = binding

    def retire_unstarted_attempt(self, prepared):
        self.events.append(("runner.retire-unstarted", threading.get_ident()))

    def run_claimed(self, prepared, request, *, binding, secrets, **_timeouts):
        self.events.append((f"runner.run:{request.operation}", threading.get_ident()))
        assert self.bound[prepared.attempt_id] == binding
        assert secrets.attempt_id == prepared.attempt_id
        if request.operation == "info_motion":
            result = MaterialWorkerResult(
                protocol_version=1,
                execution_id=self.execution_id,
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
                termination=_termination("natural"),
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
            execution_id=self.execution_id,
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
            termination=_termination("natural", exit_code=0),
        )

    def retire_terminal_attempt_with_proof(
        self,
        prepared,
        *,
        proof: MaterialRestartRetirementProof,
    ):
        self.events.append(("runner.retire-terminal", threading.get_ident()))
        return MaterialAttemptRetirementReceipt(
            status=proof.status,
            execution_id=proof.execution_id,
            operation=proof.operation,
            attempt_id=proof.attempt_id,
            control_id=prepared.control_id,
            job_instance_id=prepared.job_instance_id,
            execution_revision=proof.execution_revision,
            final_payload_sha256=proof.final_payload_sha256,
            attempt_history_sha256=proof.attempt_history_sha256,
            result_sha256=proof.result_sha256,
            observed_at=proof.observed_at,
            retired_at=proof.observed_at,
            tombstone_sha256="c" * 64,
        )

    def retire_published_attempt(self, prepared, *, publication_journal_sha256):
        self.events.append(("runner.retire-published", threading.get_ident()))

    def attest_completed_published_attempt(self, prepared, *, proof):
        self.events.append(("runner.retire-published", threading.get_ident()))
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
            tombstone_sha256="9" * 64,
            retired_at="2026-07-17T00:00:01+00:00",
            release_basis="in_process_close",
        )

    def retire_terminal_attempt(self, *args, **kwargs):
        raise AssertionError("chain must use typed retirement proof")

    def close(self):
        pass


class _Lifecycle:
    def __init__(self, lease: _Lease, events):
        self.lease = lease
        self.events = events

    def record_terminal_attempt_for_fallback(self, claim, *, outcome):
        self.events.append(("lifecycle.record-terminal", threading.get_ident()))
        self.lease._payload.update(
            {
                "execution_state": "running",
                "publication_phase": "rolled_back",
                "attempt_transition": "retiring",
                "execution_revision": 2,
                "active_material_task_id": None,
                "active_execution_id": None,
                "active_attempt_id": None,
                "active_attempt_kind": None,
                "active_control_id": None,
                "active_job_instance_id": None,
                "material_attempt_history": [
                    {
                        "material_task_id": "task-001",
                        "execution_id": claim.binding.execution_id,
                        "operation": claim.binding.operation,
                        "attempt_id": claim.binding.attempt_id,
                        "control_id": claim.binding.control_id,
                        "job_instance_id": claim.binding.job_instance_id,
                        "status": outcome.status,
                        "observed_at": outcome.termination.observed_at,
                        "result_sha256": "d" * 64,
                        "publication_journal_sha256": None,
                        "reason_code": "source_unavailable",
                        "reason": "source unavailable",
                        "entry_sha256": "e" * 64,
                    }
                ],
            }
        )
        self.lease.expected_revision = 2
        self.lease.active_attempt_id = ""
        self.lease.active_attempt_kind = ""
        self.lease.active_control_id = ""
        self.lease.active_job_instance_id = ""
        return MaterialCompletionReceipt(
            status=outcome.status,
            execution_id=claim.binding.execution_id,
            operation=claim.binding.operation,
            attempt_id=claim.binding.attempt_id,
            execution_revision=2,
            final_payload_sha256=_sha(self.lease._payload),
            attempt_history_sha256="e" * 64,
            result_sha256="d" * 64,
            observed_at=outcome.termination.observed_at,
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
        self.events.append(("lifecycle.rotate", threading.get_ident()))
        self.lease._payload.update(
            {
                "execution_state": "claimed",
                "publication_phase": "preparing",
                "attempt_transition": None,
                "execution_revision": 3,
                "active_material_task_id": "task-001",
                "active_execution_id": claim.binding.execution_id,
                "active_attempt_id": attempt_id,
                "active_attempt_kind": attempt_kind,
                "active_control_id": control_id,
                "active_job_instance_id": job_instance_id,
            }
        )
        self.lease.expected_revision = 3
        self.lease.active_attempt_id = attempt_id
        self.lease.active_attempt_kind = attempt_kind
        self.lease.active_control_id = control_id
        self.lease.active_job_instance_id = job_instance_id

    def complete_success(self, claim, *, outcome, publication):
        self.events.append(("lifecycle.complete-success", threading.get_ident()))
        return MaterialCompletionReceipt(
            status="succeeded",
            execution_id=claim.binding.execution_id,
            operation=claim.binding.operation,
            attempt_id=claim.binding.attempt_id,
            execution_revision=4,
            final_payload_sha256="f" * 64,
            publication_journal_sha256="a" * 64,
            attempt_history_sha256="e" * 64,
            result_sha256="b" * 64,
            publication_intent_sha256="1" * 64,
            publication_result_sha256="2" * 64,
            publication_worker_result_sha256="b" * 64,
            publication_reference_base_revision=3,
            publication_reference_base_payload_sha256="3" * 64,
            runner_retirement_checkpoint_sha256="4" * 64,
            observed_at=outcome.termination.observed_at,
        )

    def acknowledge_runner_retirement(
        self, claim, *, completion, attestation
    ):
        self.events.append(("lifecycle.ack-retirement", threading.get_ident()))
        return MaterialRunnerRetirementAcknowledgement(
            completion_revision=completion.execution_revision,
            ack_revision=completion.execution_revision + 1,
            completion_payload_sha256=completion.final_payload_sha256,
            checkpoint_sha256=attestation.checkpoint_sha256,
            tombstone_sha256=attestation.tombstone_sha256,
            acknowledged_at="2026-07-17T00:00:02+00:00",
        )

    def record_blocked(self, *args, **kwargs):
        raise AssertionError("chain fixture did not block")


class _Publication:
    phase = "committed"
    ownership_retained = True

    def commit(self, *, retain_ownership):
        assert retain_ownership is True

    def release_after_retirement(self):
        pass


class _Publisher:
    def __init__(self, database, expected_payload):
        self.database = database
        self.expected_payload = expected_payload

    def prepare(self, request, attempt_dir, worker_result, *, project_directory_uses):
        return _Publication()


def _prepared(execution_id: str) -> PreparedMaterialAttempt:
    attempt, control, job = _ids()[0:3]
    return PreparedMaterialAttempt(
        execution_id=execution_id,
        attempt_id=attempt,
        control_id=control,
        job_instance_id=job,
        control_directory=None,  # type: ignore[arg-type]
        directory=None,  # type: ignore[arg-type]
        pin_scope=(),
        container_hash="a" * 64,
    )


def _termination(method: str, *, exit_code: int | None = 1):
    return TerminationEvidence(
        platform="windows_job",
        root_pid=1,
        exit_code=exit_code,
        termination_method=method,
        active_processes=0,
        observed_at="2026-07-17T00:00:00+00:00",
    )


def _sha(payload):
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def test_production_source_chain_keeps_execution_id_and_owner_thread(tmp_path: Path):
    execution_id = str(uuid4())
    events: list[tuple[str, int]] = []
    first = _prepared(execution_id)
    mutex = _Mutex(events)
    lease = _Lease(execution_id, first, mutex, events)
    store = _Store(lease, events)
    runner = _Runner(execution_id, events)
    # Use deterministic attempts from the lease's first owner.
    runner.attempts[0] = first
    lifecycle = _Lifecycle(lease, events)
    cleanup: list[str] = []

    def plan_factory(command, _snapshot):
        if command.operation == "info_motion":
            request_payload = InfoMotionPayload(project_name="project", title="title")
            secret_payload = InfoMotionSecrets()
        else:
            request_payload = StockSearchPayload(query="office", per_page=1)
            secret_payload = PexelsSecrets(api_key="not-returned")
        return ProductionMaterialOperationPlan(
            request_payload=request_payload,
            secret_payload=secret_payload,
            canonical_paths=AutoMaterialPaths(root=Path("D:/canonical")),
            cleanup=lambda: cleanup.append(command.operation),
        )

    def publisher_factory(*, canonical_paths, database, expected_payload):
        return _Publisher(database, expected_payload)

    key = MaterialExecutionLaneKey(
        database_path=tmp_path / "chain.db",
        pipeline_id=1,
        material_step_id=2,
    )
    handler = ProductionMaterialExecutionHandler(
        key=key,
        claim_store_factory=lambda _key: store,
        runner_factory=lambda _key: runner,
        operation_plan_factory=plan_factory,
        lifecycle_factory=lambda _key, _store: lifecycle,
        publisher_factory=publisher_factory,
        recovery_handler=None,
    )
    command = MaterialExecutionCommand(
        pipeline_id=1,
        material_step_id=2,
        material_task_id="task-001",
        execution_id=execution_id,
        operation="info_motion",
        startup_timeout_seconds=1.0,
        operation_timeout_seconds=2.0,
    )

    lane = MaterialExecutionOwnerLane(key=key, handler=handler)
    try:
        response = lane.submit_source_chain(
            command,
            source_chain=("info_motion", "pexels"),
        ).result(timeout=5)
    finally:
        lane.close(timeout=5)

    assert isinstance(response, MaterialExecutionResponse)
    assert response.status == "succeeded"
    assert response.operation == "pexels"
    assert response.execution_id == execution_id
    assert lease.released is True
    assert cleanup == ["info_motion", "pexels"]
    owner_ids = {thread_id for _name, thread_id in events}
    assert owner_ids == {lane.owner_thread_ident}
    names = [name for name, _thread_id in events]
    assert names.index("runner.retire-terminal") < names.index("runner.prepare:pexels")
    bind_indices = [index for index, name in enumerate(names) if name == "runner.bind"]
    assert names.index("lifecycle.rotate") < bind_indices[-1]
    assert bind_indices[-1] < names.index("runner.run:pexels")
