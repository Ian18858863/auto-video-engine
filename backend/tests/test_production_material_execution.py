from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import threading
from uuid import uuid4

import pytest

from app.materials.asset_paths import AutoMaterialPaths
from app.materials.candidate_media_validation import validate_candidate_media
from app.pipeline.material_execution_claims import MaterialExecutionClaimConflict
from app.pipeline.material_execution_lifecycle import ClaimLeasePublicationDatabase
from app.pipeline.material_execution_application import (
    MaterialExecutionCommand,
    MaterialExecutionLaneBusy,
    MaterialExecutionLaneError,
    MaterialExecutionLaneUnknown,
    MaterialExecutionOwnerLane,
    MaterialExecutionResponse,
    MaterialRestartRecoveryCommand,
    MaterialRunnerRetirementRecoveryCommand,
)
from app.pipeline.material_execution_coordinator import MaterialCompletionReceipt
from app.pipeline.material_operation_protocol import (
    MaterialOperationBlocked,
    MaterialOperationOutcome,
    MaterialOperationRequest,
    MaterialOperationSecrets,
    MaterialWorkerResult,
    PexelsSecrets,
    StagedArtifact,
    StockSearchPayload,
    TerminationEvidence,
)
from app.pipeline.material_operation_runner import (
    MaterialRunnerClaimBinding,
    PreparedMaterialAttempt,
)
from app.pipeline.material_runner_retirement import (
    MaterialRunnerRetirementAcknowledgement,
    MaterialRunnerRetirementAttestation,
    MaterialRunnerRetirementCheckpoint,
    install_pending_runner_retirement,
)
from app.pipeline.production_material_execution import (
    ProductionMaterialExecutionHandler,
    ProductionMaterialExecutionHandlerFactory,
    ProductionMaterialOperationPlan,
    ProductionMaterialPublicationRecoveryContext,
    _assert_task_snapshot,
    _default_publisher_factory,
    _protocol_version_for_operation,
)


def _ids() -> tuple[str, str, str, str]:
    return tuple(str(uuid4()) for _ in range(4))  # type: ignore[return-value]


def test_default_parent_publisher_enables_real_media_validation() -> None:
    publisher = _default_publisher_factory(
        canonical_paths=AutoMaterialPaths(root=Path("D:/canonical")),
        database=object(),  # type: ignore[arg-type]
        expected_payload={"state": "running"},
    )

    assert publisher.media_validator is validate_candidate_media


def test_stock_candidate_operation_uses_protocol_v2_only() -> None:
    assert _protocol_version_for_operation("stock_candidates") == 2
    assert _protocol_version_for_operation("pexels") == 1


def test_owner_lane_rejects_shot_alias_without_exact_material_task_id() -> None:
    with pytest.raises(ValueError, match="requested task"):
        _assert_task_snapshot(
            {
                "material_tasks": [
                    {
                        "material_task_id": "other-task",
                        "shot_id": "task-001",
                    }
                ]
            },
            "task-001",
        )


def test_owner_lane_response_selects_exact_task_when_shot_alias_collides() -> None:
    snapshot = {
        "material_tasks": [
            {
                "material_task_id": "other-task",
                "shot_id": "task-001",
                "status": "wrong",
            },
            {
                "material_task_id": "task-001",
                "shot_id": "shot-exact",
                "status": "completed",
            },
        ]
    }

    response = ProductionMaterialExecutionHandler._response_from_snapshot(
        object.__new__(ProductionMaterialExecutionHandler),
        _command(),
        str(uuid4()),
        snapshot,
        status="succeeded",
        message="done",
    )

    assert response.executed_task is not None
    assert response.executed_task["material_task_id"] == "task-001"
    assert response.executed_task["status"] == "completed"


def _command(*, execution_id: str | None = None) -> MaterialExecutionCommand:
    return MaterialExecutionCommand(
        pipeline_id=18,
        material_step_id=31,
        material_task_id="task-001",
        execution_id=execution_id or str(uuid4()),
        operation="pexels",
        startup_timeout_seconds=2.0,
        operation_timeout_seconds=5.0,
    )


class _Mutex:
    def __init__(self, events: list[tuple[str, int]]) -> None:
        self.events = events
        self.released = False

    def assert_owned(self) -> None:
        self.events.append(("mutex.assert", threading.get_ident()))
        if self.released:
            raise RuntimeError("released")

    def release(self) -> None:
        self.events.append(("mutex.release", threading.get_ident()))
        self.released = True


class _Lease:
    def __init__(
        self,
        *,
        execution_id: str,
        attempt_id: str,
        control_id: str,
        job_instance_id: str,
        mutex: _Mutex,
        payload: dict,
        events: list[tuple[str, int]],
    ) -> None:
        self.execution_id = execution_id
        self._attempt_id = attempt_id
        self._control_id = control_id
        self._job_instance_id = job_instance_id
        self._payload = dict(payload)
        self._revision = 1
        self.mutex = mutex
        self.events = events
        self.released = False

    @property
    def active_attempt_id(self) -> str:
        return self._attempt_id

    @property
    def active_attempt_kind(self) -> str:
        return "pexels"

    @property
    def active_control_id(self) -> str:
        return self._control_id

    @property
    def active_job_instance_id(self) -> str:
        return self._job_instance_id

    @property
    def expected_revision(self) -> int:
        return self._revision

    @property
    def expected_payload(self) -> dict:
        return dict(self._payload)

    def assert_owned(self) -> None:
        self.events.append(("lease.assert", threading.get_ident()))
        self.mutex.assert_owned()

    def compare_and_swap(self, replacement: dict) -> None:
        self.events.append(("lease.cas", threading.get_ident()))
        self._payload = dict(replacement)
        self._revision += 1

    def release(self) -> None:
        self.events.append(("lease.release", threading.get_ident()))
        self.released = True
        self.mutex.release()


@dataclass
class _RuntimeFixture:
    events: list[tuple[str, int]]
    payload: dict
    blocked: bool = False
    claim_error: bool = False

    def __post_init__(self) -> None:
        self.mutex: _Mutex | None = None
        self.lease: _Lease | None = None
        self.prepared: PreparedMaterialAttempt | None = None
        self.binding: MaterialRunnerClaimBinding | None = None
        self.plan_cleaned = False

    def mark(self, name: str) -> None:
        self.events.append((name, threading.get_ident()))


class _Store:
    def __init__(self, fixture: _RuntimeFixture) -> None:
        self.fixture = fixture

    def acquire_claim_mutex(self, *, timeout_ms: int = 0):
        assert timeout_ms == 0
        self.fixture.mark("store.acquire")
        self.fixture.mutex = _Mutex(self.fixture.events)
        return self.fixture.mutex

    def acquire_recovery_mutex(self, *, timeout_ms: int = 0):
        self.fixture.mark("store.acquire_recovery")
        assert timeout_ms == 2000
        self.fixture.mutex = _Mutex(self.fixture.events)
        return self.fixture.mutex

    def fresh_read(self):
        self.fixture.mark("store.fresh")
        return dict(self.fixture.payload)

    def claim_lease_with_owned_mutex(self, *, mutex, **values):
        self.fixture.mark("store.claim")
        assert mutex is self.fixture.mutex
        if self.fixture.claim_error:
            raise MaterialExecutionClaimConflict("claim conflict")
        self.fixture.lease = _Lease(
            execution_id=values["execution_id"],
            attempt_id=values["attempt_id"],
            control_id=values["control_id"],
            job_instance_id=values["job_instance_id"],
            mutex=mutex,
            payload=self.fixture.payload,
            events=self.fixture.events,
        )
        return self.fixture.lease


class _Runner:
    def __init__(self, fixture: _RuntimeFixture) -> None:
        self.fixture = fixture
        self.closed = False

    def prepare_attempt(self, *, execution_id: str, operation: str):
        self.fixture.mark("runner.prepare")
        _execution, attempt_id, control_id, job_instance_id = _ids()
        self.fixture.prepared = PreparedMaterialAttempt(
            execution_id=execution_id,
            attempt_id=attempt_id,
            control_id=control_id,
            job_instance_id=job_instance_id,
            control_directory=object(),
            directory=object(),
            pin_scope=(),
            container_hash="a" * 64,
        )
        return self.fixture.prepared

    def bind_claim(self, prepared, binding):
        self.fixture.mark("runner.bind")
        self.fixture.binding = binding

    def run_claimed(self, prepared, request, *, binding, secrets, **timeouts):
        self.fixture.mark("runner.run")
        assert isinstance(request, MaterialOperationRequest)
        assert isinstance(secrets, MaterialOperationSecrets)
        assert binding is self.fixture.binding
        if self.fixture.blocked:
            raise MaterialOperationBlocked(
                execution_id=request.execution_id,
                attempt_id=request.attempt_id,
                reason_code="tree_not_empty",
                job_instance_id=binding.job_instance_id,
                quarantine_dir=Path("D:/quarantine"),
            )
        result = MaterialWorkerResult(
            protocol_version=1,
            execution_id=request.execution_id,
            attempt_id=request.attempt_id,
            operation=request.operation,
            status="succeeded",
            selected_artifact="payload/clip.mp4",
            artifacts=(
                StagedArtifact(
                    relative_path="payload/clip.mp4",
                    media_kind="video",
                    source_type="Pexels",
                    source_url="https://example.test/clip",
                    evidence_kind="atmosphere",
                    is_locally_generated=False,
                    sha256="b" * 64,
                    size_bytes=1,
                ),
            ),
            reason_code="",
            reason="",
        )
        return MaterialOperationOutcome(
            status="succeeded",
            result=result,
            termination=TerminationEvidence(
                platform="windows_job",
                root_pid=1,
                exit_code=0,
                termination_method="natural",
                active_processes=0,
                observed_at="2026-01-01T00:00:00+00:00",
            ),
        )

    def retire_published_attempt(self, prepared, *, publication_journal_sha256):
        self.fixture.mark("runner.retire")

    def attest_completed_published_attempt(self, prepared, *, proof):
        self.fixture.mark("runner.retire")
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
            retired_at="2026-01-01T00:00:01+00:00",
            release_basis="in_process_close",
        )

    def retire_terminal_attempt(self, prepared, *, attempt_history_sha256):
        self.fixture.mark("runner.retire")

    def retire_unstarted_attempt(self, prepared):
        self.fixture.mark("runner.retire_unstarted")

    def close(self):
        self.closed = True


class _Publication:
    phase = "committed"
    ownership_retained = True
    committed_journal_sha256 = "c" * 64
    result = type(
        "PublishedResult",
        (),
        {
            "success": True,
            "material_path": "D:/canonical/clip.mp4",
            "material_type": "Pexels视频",
            "source_type": "Pexels",
            "evidence_kind": "atmosphere",
            "source_url": "https://example.test/clip",
            "is_locally_generated": False,
            "reason": "ok",
        },
    )()

    def commit(self, *, retain_ownership: bool):
        assert retain_ownership is True

    def release_after_retirement(self):
        pass


class _Publisher:
    def __init__(self, database, expected_payload) -> None:
        self.database = database
        self.expected_payload = expected_payload

    def prepare(self, request, attempt_dir, worker_result, *, project_directory_uses):
        return _Publication()


class _Lifecycle:
    def __init__(self, fixture: _RuntimeFixture) -> None:
        self.fixture = fixture

    def complete_success(self, claim, *, outcome, publication):
        self.fixture.mark("lifecycle.complete")
        return MaterialCompletionReceipt(
            status="succeeded",
            execution_id=claim.binding.execution_id,
            operation=claim.binding.operation,
            attempt_id=claim.binding.attempt_id,
            execution_revision=3,
            final_payload_sha256="d" * 64,
            publication_journal_sha256="c" * 64,
            attempt_history_sha256="e" * 64,
            result_sha256="f" * 64,
            publication_intent_sha256="1" * 64,
            publication_result_sha256="2" * 64,
            publication_worker_result_sha256="f" * 64,
            publication_reference_base_revision=1,
            publication_reference_base_payload_sha256="3" * 64,
            runner_retirement_checkpoint_sha256="4" * 64,
            observed_at="2026-01-01T00:00:00+00:00",
        )

    def complete_terminal(self, claim, *, outcome):
        raise AssertionError("success fixture should not complete terminal")

    def record_blocked(self, claim, *, reason):
        self.fixture.mark("lifecycle.blocked")

    def acknowledge_runner_retirement(self, claim, *, completion, attestation):
        del claim
        self.fixture.mark("lifecycle.ack-retirement")
        return MaterialRunnerRetirementAcknowledgement(
            completion_revision=completion.execution_revision,
            ack_revision=completion.execution_revision + 1,
            completion_payload_sha256=completion.final_payload_sha256,
            checkpoint_sha256=attestation.checkpoint_sha256,
            tombstone_sha256=attestation.tombstone_sha256,
            acknowledged_at="2026-01-01T00:00:02+00:00",
        )


def _factory(fixture: _RuntimeFixture, *, recovery_handler=None):
    def store_factory(_key):
        fixture.mark("store.factory")
        return _Store(fixture)

    def runner_factory(_key):
        fixture.mark("runner.factory")
        return _Runner(fixture)

    def plan_factory(command, fresh_payload):
        fixture.mark("plan.factory")
        assert fresh_payload == fixture.payload
        return ProductionMaterialOperationPlan(
            request_payload=StockSearchPayload(query="office", per_page=1),
            secret_payload=PexelsSecrets(api_key="secret-value"),
            canonical_paths=AutoMaterialPaths(root=Path("D:/canonical")),
            project_directory_uses={},
            cleanup=lambda: setattr(fixture, "plan_cleaned", True),
        )

    def publication_factory(**kwargs):
        fixture.mark("publisher.factory")
        assert isinstance(kwargs["database"], ClaimLeasePublicationDatabase)
        assert kwargs["expected_payload"] == fixture.payload
        return _Publisher(kwargs["database"], kwargs["expected_payload"])

    def lifecycle_factory(_key, _store):
        fixture.mark("lifecycle.factory")
        return _Lifecycle(fixture)

    factory = ProductionMaterialExecutionHandlerFactory(
        claim_store_factory=store_factory,
        runner_factory=runner_factory,
        operation_plan_factory=plan_factory,
        lifecycle_factory=lifecycle_factory,
        publisher_factory=publication_factory,
        recovery_handler=recovery_handler,
    )
    return factory


def test_production_factory_defers_all_runtime_construction_and_preserves_order(
    tmp_path: Path,
):
    fixture = _RuntimeFixture(
        events=[],
        payload={
            "material_tasks": [{"material_task_id": "task-001", "status": "pending"}]
        },
    )
    factory = _factory(fixture)
    key_path = tmp_path / "lane.db"
    key = __import__(
        "app.pipeline.material_execution_application",
        fromlist=["MaterialExecutionLaneKey"],
    ).MaterialExecutionLaneKey(
        database_path=key_path,
        pipeline_id=18,
        material_step_id=31,
    )

    handler = factory(key)
    assert fixture.events == []
    lane = MaterialExecutionOwnerLane(key=key, handler=handler)
    try:
        response = lane.submit(_command()).result(timeout=5)
        assert isinstance(response, MaterialExecutionResponse)
        assert response.status == "succeeded"
        assert "secret-value" not in repr(response)
        names = [name for name, _thread_id in fixture.events]
        assert names.index("store.acquire") < names.index("store.fresh")
        assert names.index("store.fresh") < names.index("plan.factory")
        assert names.index("plan.factory") < names.index("runner.prepare")
        assert names.index("runner.prepare") < names.index("store.claim")
        assert names.index("store.claim") < names.index("runner.bind")
        assert names.index("runner.bind") < names.index("runner.run")
        assert names.index("runner.run") < names.index("publisher.factory")
        assert names.index("publisher.factory") < names.index("lifecycle.complete")
        assert names.index("lifecycle.complete") < names.index("runner.retire")
        owner_ids = {thread_id for _name, thread_id in fixture.events}
        assert owner_ids == {lane.owner_thread_ident}
        assert fixture.plan_cleaned is True
        assert fixture.lease is not None and fixture.lease.released is True
    finally:
        lane.close(timeout=5)


def test_restart_publication_recovery_uses_owner_lane_without_preparing_or_running_worker(
    tmp_path: Path,
):
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    payload = {
        "material_tasks": [
            {
                "material_task_id": "task-001",
                "status": "发布收口中",
                "status_history": ["进行中", "发布收口中"],
            }
        ],
        "execution_id": execution_id,
        "execution_revision": 8,
        "execution_state": "publishing",
        "publication_phase": "db_committed",
        "active_material_task_id": "task-001",
        "active_execution_id": execution_id,
        "active_attempt_id": attempt_id,
        "active_attempt_kind": "pexels",
        "active_control_id": control_id,
        "active_job_instance_id": job_instance_id,
    }
    fixture = _RuntimeFixture(events=[], payload=payload)

    def store_factory(_key):
        fixture.mark("store.factory")
        return _Store(fixture)

    def runner_factory(_key):
        fixture.mark("runner.factory")
        return _Runner(fixture)

    def plan_factory(_command, _fresh_payload):
        raise AssertionError("restart recovery must not build a normal operation plan")

    def recovery_context_factory(command, fresh_payload):
        fixture.mark("recovery_context.factory")
        assert fresh_payload == payload
        assert command.attempt_id == attempt_id
        return ProductionMaterialPublicationRecoveryContext(
            canonical_paths=AutoMaterialPaths(root=Path("D:/canonical")),
            project_directory_uses={},
        )

    def publisher_factory(**kwargs):
        return _Publisher(kwargs["database"], kwargs["expected_payload"])

    def lifecycle_factory(_key, _store):
        fixture.mark("lifecycle.factory")
        return _Lifecycle(fixture)

    class _RestartCoordinator:
        def __init__(self, **kwargs):
            fixture.mark("restart.coordinator")
            assert kwargs["store"].fixture is fixture
            assert callable(kwargs["publication_recovery_factory"])

        def complete_published_attempt(self, **values):
            fixture.mark("restart.complete")
            assert values == {
                "mutex": fixture.mutex,
                "material_task_id": "task-001",
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "control_id": control_id,
                "job_instance_id": job_instance_id,
                "operation": "pexels",
            }
            final = {
                "material_tasks": [
                    {
                        "material_task_id": "task-001",
                        "status": "已完成",
                        "status_history": ["进行中", "发布收口中", "已完成"],
                    }
                ],
                "execution_id": execution_id,
                "execution_revision": 9,
                "execution_state": "idle",
                "publication_phase": "committed",
                "active_material_task_id": None,
                "active_execution_id": None,
                "active_attempt_id": None,
                "active_attempt_kind": None,
                "active_control_id": None,
                "active_job_instance_id": None,
            }
            fixture.payload = final
            final_sha256 = hashlib.sha256(
                json.dumps(
                    final,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            return MaterialCompletionReceipt(
                status="succeeded",
                execution_id=execution_id,
                operation="pexels",
                attempt_id=attempt_id,
                execution_revision=9,
                final_payload_sha256=final_sha256,
                publication_journal_sha256="5" * 64,
                attempt_history_sha256="6" * 64,
                result_sha256="7" * 64,
                observed_at="2026-07-18T00:00:00.000000Z",
            )

    factory = ProductionMaterialExecutionHandlerFactory(
        claim_store_factory=store_factory,
        runner_factory=runner_factory,
        operation_plan_factory=plan_factory,
        lifecycle_factory=lifecycle_factory,
        publisher_factory=publisher_factory,
        publication_recovery_context_factory=recovery_context_factory,
        restart_recovery_coordinator_factory=_RestartCoordinator,
    )
    key = __import__(
        "app.pipeline.material_execution_application",
        fromlist=["MaterialExecutionLaneKey"],
    ).MaterialExecutionLaneKey(
        database_path=tmp_path / "restart-lane.db",
        pipeline_id=18,
        material_step_id=31,
    )
    command = MaterialRestartRecoveryCommand(
        pipeline_id=18,
        material_step_id=31,
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
        expected_revision=8,
        expected_payload_sha256=hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        startup_timeout_seconds=2.0,
    )
    lane = MaterialExecutionOwnerLane(key=key, handler=factory(key))
    try:
        response = lane.recover_restart_published(command).result(timeout=5)
        assert response.status == "succeeded"
        names = [name for name, _thread_id in fixture.events]
        assert "runner.prepare" not in names
        assert "runner.run" not in names
        assert names.index("store.fresh") < names.index("store.acquire_recovery")
        assert names.index("store.acquire_recovery") < names.index("restart.complete")
    finally:
        lane.close()


def test_pending_runner_retirement_recovery_uses_owner_lane_without_worker_replay(
    tmp_path: Path,
) -> None:
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    observed_at = "2026-07-18T08:30:00+00:00"
    checkpoint = MaterialRunnerRetirementCheckpoint.create(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
        completion_status="failed",
        completion_revision=8,
        completion_observed_at=observed_at,
        reference_revision=7,
        reference_payload_sha256="4" * 64,
        attempt_history_sha256="5" * 64,
        result_sha256="6" * 64,
    )
    payload = {
        "material_tasks": [{"material_task_id": "task-001", "status": "failed"}],
        "execution_id": execution_id,
        "execution_revision": 8,
        "execution_state": "idle",
        "publication_phase": "rolled_back",
        "last_execution_status": "failed",
        "execution_completed_at": observed_at,
        "active_material_task_id": None,
        "active_execution_id": None,
        "active_attempt_id": None,
        "active_attempt_kind": None,
        "active_control_id": None,
        "active_job_instance_id": None,
        "material_attempt_history": [
            {
                "material_task_id": "task-001",
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "operation": "pexels",
                "control_id": control_id,
                "job_instance_id": job_instance_id,
                "status": "failed",
                "observed_at": observed_at,
                "entry_sha256": "5" * 64,
                "result_sha256": "6" * 64,
                "publication_journal_sha256": None,
            }
        ],
    }
    install_pending_runner_retirement(payload, checkpoint)
    pending_payload = json.loads(json.dumps(payload))
    fixture = _RuntimeFixture(events=[], payload=payload)

    def forbidden_plan_factory(_command, _fresh_payload):
        raise AssertionError("retirement recovery must not build a normal plan")

    class _RestartCoordinator:
        def __init__(self, **kwargs):
            fixture.mark("restart.coordinator")
            assert kwargs["store"].fixture is fixture

        def complete_pending_runner_retirement(self, **values):
            fixture.mark("restart.retirement")
            assert values == {
                "mutex": fixture.mutex,
                "material_task_id": "task-001",
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "control_id": control_id,
                "job_instance_id": job_instance_id,
                "operation": "pexels",
                "completion_status": "failed",
                "completion_revision": 8,
                "checkpoint_sha256": checkpoint.checkpoint_sha256,
            }
            fixture.payload["execution_revision"] = 9
            fixture.payload["runner_retirement"]["state"] = "attested"
            fixture.mutex.release()
            return MaterialCompletionReceipt(
                status="failed",
                execution_id=execution_id,
                operation="pexels",
                attempt_id=attempt_id,
                execution_revision=8,
                final_payload_sha256=hashlib.sha256(
                    json.dumps(
                        pending_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
                attempt_history_sha256="5" * 64,
                result_sha256="6" * 64,
                runner_retirement_checkpoint_sha256=checkpoint.checkpoint_sha256,
                observed_at=observed_at,
            )

    factory = ProductionMaterialExecutionHandlerFactory(
        claim_store_factory=lambda _key: _Store(fixture),
        runner_factory=lambda _key: _Runner(fixture),
        operation_plan_factory=forbidden_plan_factory,
        lifecycle_factory=lambda _key, _store: _Lifecycle(fixture),
        publisher_factory=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed retirement must not recover publication")
        ),
        restart_recovery_coordinator_factory=_RestartCoordinator,
    )
    key = __import__(
        "app.pipeline.material_execution_application",
        fromlist=["MaterialExecutionLaneKey"],
    ).MaterialExecutionLaneKey(
        database_path=tmp_path / "retirement-lane.db",
        pipeline_id=18,
        material_step_id=31,
    )
    command = MaterialRunnerRetirementRecoveryCommand(
        pipeline_id=18,
        material_step_id=31,
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
        completion_status="failed",
        completion_revision=8,
        checkpoint_sha256=checkpoint.checkpoint_sha256,
    )
    lane = MaterialExecutionOwnerLane(key=key, handler=factory(key))
    try:
        response = lane.recover_runner_retirement(command).result(timeout=5)
        assert response.status == "failed"
        names = [name for name, _thread_id in fixture.events]
        assert "runner.prepare" not in names
        assert "runner.run" not in names
        assert names.index("store.fresh") < names.index("store.acquire_recovery")
        assert names.index("store.acquire_recovery") < names.index("restart.retirement")
    finally:
        lane.close()


def test_unknown_restart_recovery_retains_mutex_and_blocks_lane_shutdown(
    tmp_path: Path,
):
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    payload = {
        "material_tasks": [
            {
                "material_task_id": "task-001",
                "status": "publishing",
            }
        ],
        "execution_id": execution_id,
        "execution_revision": 8,
        "execution_state": "publishing",
        "publication_phase": "db_committed",
        "active_material_task_id": "task-001",
        "active_execution_id": execution_id,
        "active_attempt_id": attempt_id,
        "active_attempt_kind": "pexels",
        "active_control_id": control_id,
        "active_job_instance_id": job_instance_id,
    }
    fixture = _RuntimeFixture(events=[], payload=payload)

    def forbidden_plan_factory(_command, _fresh_payload):
        raise AssertionError("restart recovery must not build a normal operation plan")

    def recovery_context_factory(_command, fresh_payload):
        assert fresh_payload == payload
        return ProductionMaterialPublicationRecoveryContext(
            canonical_paths=AutoMaterialPaths(root=Path("D:/canonical")),
            project_directory_uses={},
        )

    class _UnknownRestartCoordinator:
        def __init__(self, **_kwargs):
            pass

        def complete_published_attempt(self, **_values):
            raise RuntimeError("completion acknowledgement is ambiguous")

    factory = ProductionMaterialExecutionHandlerFactory(
        claim_store_factory=lambda _key: _Store(fixture),
        runner_factory=lambda _key: _Runner(fixture),
        operation_plan_factory=forbidden_plan_factory,
        lifecycle_factory=lambda _key, _store: _Lifecycle(fixture),
        publisher_factory=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unknown coordinator must not request a new publisher")
        ),
        publication_recovery_context_factory=recovery_context_factory,
        restart_recovery_coordinator_factory=_UnknownRestartCoordinator,
    )
    key = __import__(
        "app.pipeline.material_execution_application",
        fromlist=["MaterialExecutionLaneKey"],
    ).MaterialExecutionLaneKey(
        database_path=tmp_path / "restart-unknown.db",
        pipeline_id=18,
        material_step_id=31,
    )
    command = MaterialRestartRecoveryCommand(
        pipeline_id=18,
        material_step_id=31,
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
        expected_revision=8,
        expected_payload_sha256=hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        startup_timeout_seconds=2.0,
    )
    handler = factory(key)
    lane = MaterialExecutionOwnerLane(key=key, handler=handler)
    try:
        with pytest.raises(MaterialExecutionLaneUnknown):
            lane.recover_restart_published(command).result(timeout=5)
        assert fixture.mutex is not None and fixture.mutex.released is False
        assert handler._restart_retained is not None
        with pytest.raises(MaterialExecutionLaneUnknown):
            lane.close(timeout=1)
        assert fixture.mutex.released is False
        assert lane.owner_thread_ident is not None
    finally:
        handler._restart_retained = None
        if fixture.mutex is not None and not fixture.mutex.released:
            fixture.mutex.release()
        lane.close(timeout=5)


def test_blocked_attempt_returns_opaque_ticket_and_recovery_stays_on_owner_lane(
    tmp_path: Path,
):
    fixture = _RuntimeFixture(
        events=[],
        payload={
            "material_tasks": [{"material_task_id": "task-001", "status": "pending"}]
        },
        blocked=True,
    )
    recovery_threads: list[int] = []

    def recover(state, ticket):
        recovery_threads.append(threading.get_ident())
        assert state.claim is not None
        assert state.claim.binding.attempt_id == ticket.attempt_id
        state.claim.lease.release()
        state.plan.release()
        return state.handler._response_from_snapshot(
            _command(execution_id=ticket.execution_id),
            ticket.attempt_id,
            {"material_tasks": [{"material_task_id": "task-001", "status": "terminated"}]},
            status="terminated",
            message="reconciled",
        )

    factory = _factory(fixture, recovery_handler=recover)
    from app.pipeline.material_execution_application import MaterialExecutionLaneKey

    key = MaterialExecutionLaneKey(
        database_path=tmp_path / "lane.db",
        pipeline_id=18,
        material_step_id=31,
    )
    lane = MaterialExecutionOwnerLane(key=key, handler=factory(key))
    try:
        first = lane.submit(_command()).result(timeout=5)
        assert first.status == "blocked"
        assert first.recovery_ticket is not None
        assert fixture.lease is not None and fixture.lease.released is False
        mismatched = replace(first.recovery_ticket, operation="coverr")
        with pytest.raises(MaterialExecutionLaneError, match="recovery ticket"):
            lane.reconcile(mismatched).result(timeout=5)
        with pytest.raises(MaterialExecutionLaneBusy):
            lane.close(timeout=5)
        final = lane.reconcile(first.recovery_ticket).result(timeout=5)
        assert final.status == "terminated"
        assert final.recovery_ticket is None
        assert recovery_threads == [lane.owner_thread_ident]
        assert fixture.lease.released is True
        assert fixture.plan_cleaned is True
    finally:
        lane.close(timeout=5)


def test_known_claim_conflict_releases_mutex_and_plan_before_future_fails(
    tmp_path: Path,
):
    fixture = _RuntimeFixture(
        events=[],
        payload={
            "material_tasks": [{"material_task_id": "task-001", "status": "pending"}]
        },
        claim_error=True,
    )
    from app.pipeline.material_execution_application import MaterialExecutionLaneKey

    key = MaterialExecutionLaneKey(
        database_path=tmp_path / "lane.db",
        pipeline_id=18,
        material_step_id=31,
    )
    lane = MaterialExecutionOwnerLane(key=key, handler=_factory(fixture)(key))
    try:
        with pytest.raises(MaterialExecutionLaneUnknown):
            lane.submit(_command()).result(timeout=5)
        assert fixture.plan_cleaned is True
        assert fixture.mutex is not None and fixture.mutex.released is True
        names = [name for name, _thread_id in fixture.events]
        assert "runner.retire_unstarted" in names
        assert "runner.run" not in names
        assert "publisher.factory" not in names
    finally:
        lane.close(timeout=5)
