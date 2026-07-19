from __future__ import annotations

import asyncio
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, build_engine
from app.models.entities import HotItem, PipelineStep, ProductionPipeline
from app.pipeline.material_execution_application import (
    MaterialExecutionOwnerLane,
    MaterialExecutionResponse,
    MaterialRestartRecoveryCommand,
    MaterialRunnerRetirementRecoveryCommand,
)
from app.pipeline.material_execution_runtime import (
    MaterialExecutionRuntimeError,
    build_material_execution_runtime,
    material_execution_runtime_lifespan,
)
from app.pipeline.material_operation_factories import ServerMaterialOperationPlanFactory
from app.pipeline.material_runner_retirement import (
    MaterialRunnerRetirementCheckpoint,
    install_pending_runner_retirement,
)
from app.pipeline.material_restart_recovery import (
    MaterialRestartRecoveryScanOverflow,
    SqlAlchemyMaterialRestartRecoveryScanner,
)
from app.pipeline.project_identity import add_pipeline_with_identity


def _publishing_payload(*, material_task_id: str = "task-001") -> dict[str, object]:
    execution_id = str(uuid4())
    attempt_id = str(uuid4())
    control_id = str(uuid4())
    job_instance_id = str(uuid4())
    return {
        "material_tasks": [
            {
                "material_task_id": material_task_id,
                "status": "发布收口中",
                "status_history": ["进行中", "发布收口中"],
            }
        ],
        "execution_id": execution_id,
        "execution_revision": 8,
        "execution_state": "publishing",
        "publication_phase": "db_committed",
        "publication_journal": "D:\\recovery\\publication-journal.json",
        "publication_intent_sha256": "1" * 64,
        "publication_result_sha256": "2" * 64,
        "publication_worker_result_sha256": "3" * 64,
        "publication_reference_base_revision": 7,
        "publication_reference_base_payload_sha256": "4" * 64,
        "active_material_task_id": material_task_id,
        "active_execution_id": execution_id,
        "active_attempt_id": attempt_id,
        "active_attempt_kind": "stock_candidates",
        "active_control_id": control_id,
        "active_job_instance_id": job_instance_id,
    }


def _pending_runner_retirement_payload(
    *,
    material_task_id: str = "task-001",
) -> dict[str, object]:
    execution_id = str(uuid4())
    attempt_id = str(uuid4())
    control_id = str(uuid4())
    job_instance_id = str(uuid4())
    completed_at = "2026-07-18T08:30:00+00:00"
    history_sha256 = "5" * 64
    result_sha256 = "6" * 64
    checkpoint = MaterialRunnerRetirementCheckpoint.create(
        material_task_id=material_task_id,
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="stock_candidates",
        control_id=control_id,
        job_instance_id=job_instance_id,
        completion_status="failed",
        completion_revision=8,
        completion_observed_at=completed_at,
        reference_revision=7,
        reference_payload_sha256="4" * 64,
        attempt_history_sha256=history_sha256,
        result_sha256=result_sha256,
    )
    payload: dict[str, object] = {
        "material_tasks": [
            {
                "material_task_id": material_task_id,
                "status": "failed",
            }
        ],
        "execution_id": execution_id,
        "execution_revision": 8,
        "execution_state": "idle",
        "publication_phase": "rolled_back",
        "last_execution_status": "failed",
        "execution_completed_at": completed_at,
        "active_material_task_id": None,
        "active_execution_id": None,
        "active_attempt_id": None,
        "active_attempt_kind": None,
        "active_control_id": None,
        "active_job_instance_id": None,
        "material_attempt_history": [
            {
                "material_task_id": material_task_id,
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "operation": "stock_candidates",
                "control_id": control_id,
                "job_instance_id": job_instance_id,
                "status": "failed",
                "observed_at": completed_at,
                "entry_sha256": history_sha256,
                "result_sha256": result_sha256,
                "publication_journal_sha256": None,
            }
        ],
    }
    install_pending_runner_retirement(payload, checkpoint)
    return payload


@pytest.fixture()
def restart_database(tmp_path: Path):
    database_path = tmp_path / "restart-scan.db"
    engine = build_engine(f"sqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def add_step(
        payload: dict[str, object],
        *,
        name: str = "生成素材任务",
    ) -> tuple[int, int]:
        with session_factory() as session:
            hot_item = HotItem(
                source_platform="test",
                source_type="test",
                title="restart recovery",
                url=f"https://example.invalid/{uuid4().hex}",
            )
            session.add(hot_item)
            session.flush()
            pipeline = ProductionPipeline(
                hotspot_id=hot_item.id,
                project_name=f"restart-{uuid4().hex[:8]}",
                status="进行中",
            )
            add_pipeline_with_identity(session, pipeline)
            step = PipelineStep(
                name=name,
                step_order=3,
                output_summary=payload,
            )
            pipeline.steps.append(step)
            session.commit()
            return pipeline.id, step.id

    try:
        yield database_path, engine, session_factory, add_step
    finally:
        engine.dispose()


def test_startup_scanner_returns_only_exact_receipt_bound_publishing_owner(
    restart_database,
):
    database_path, _engine, session_factory, add_step = restart_database
    payload = _publishing_payload()
    pipeline_id, step_id = add_step(payload)
    scanner = SqlAlchemyMaterialRestartRecoveryScanner(
        session_factory=session_factory,
        database_path=database_path,
    )

    report = scanner.scan()

    assert report.audit_required == ()
    assert len(report.candidates) == 1
    candidate = report.candidates[0]
    assert type(candidate.command) is MaterialRestartRecoveryCommand
    assert candidate.lane_key.pipeline_id == pipeline_id
    assert candidate.lane_key.material_step_id == step_id
    assert candidate.command.material_task_id == "task-001"
    assert candidate.command.execution_id == payload["active_execution_id"]
    assert candidate.command.operation == "stock_candidates"
    assert candidate.command.expected_revision == 8


def test_startup_scanner_classifies_exact_idle_pending_runner_retirement(
    restart_database,
):
    database_path, _engine, session_factory, add_step = restart_database
    payload = _pending_runner_retirement_payload()
    pipeline_id, step_id = add_step(payload)
    scanner = SqlAlchemyMaterialRestartRecoveryScanner(
        session_factory=session_factory,
        database_path=database_path,
    )

    report = scanner.scan()

    assert report.audit_required == ()
    assert len(report.candidates) == 1
    candidate = report.candidates[0]
    command = candidate.command
    assert type(command) is MaterialRunnerRetirementRecoveryCommand
    checkpoint = payload["runner_retirement"]["checkpoint"]
    assert command.pipeline_id == pipeline_id
    assert command.material_step_id == step_id
    assert (
        command.material_task_id,
        command.execution_id,
        command.attempt_id,
        command.operation,
        command.control_id,
        command.job_instance_id,
    ) == (
        checkpoint["material_task_id"],
        checkpoint["execution_id"],
        checkpoint["attempt_id"],
        checkpoint["operation"],
        checkpoint["control_id"],
        checkpoint["job_instance_id"],
    )
    assert command.completion_status == checkpoint["completion_status"]
    assert command.completion_revision == checkpoint["completion_revision"]
    assert command.checkpoint_sha256 == checkpoint["checkpoint_sha256"]


def test_startup_scanner_ignores_idle_attested_runner_retirement(restart_database):
    database_path, _engine, session_factory, add_step = restart_database
    payload = _pending_runner_retirement_payload()
    payload["execution_revision"] += 1
    payload["runner_retirement"]["state"] = "attested"
    payload["runner_retirement"]["attestation"] = {
        "completion_payload_sha256": "7" * 64,
        "tombstone_sha256": "8" * 64,
        "retired_at": "2026-07-18T08:31:00+00:00",
        "release_basis": "dead_controller_after_tombstone",
        "ack_revision": payload["execution_revision"],
        "acknowledged_at": "2026-07-18T08:31:01+00:00",
    }
    add_step(payload)
    scanner = SqlAlchemyMaterialRestartRecoveryScanner(
        session_factory=session_factory,
        database_path=database_path,
    )

    report = scanner.scan()

    assert report.candidates == ()
    assert report.audit_required == ()


def test_startup_scanner_quarantines_malformed_pending_runner_retirement(
    restart_database,
):
    database_path, _engine, session_factory, add_step = restart_database
    payload = _pending_runner_retirement_payload()
    payload["runner_retirement"]["checkpoint"]["checkpoint_sha256"] = "not-a-checkpoint"
    pipeline_id, step_id = add_step(payload)
    scanner = SqlAlchemyMaterialRestartRecoveryScanner(
        session_factory=session_factory,
        database_path=database_path,
    )

    report = scanner.scan()

    assert report.candidates == ()
    assert len(report.audit_required) == 1
    issue = report.audit_required[0]
    assert issue.pipeline_id == pipeline_id
    assert issue.material_step_id == step_id
    assert issue.code == "invalid_pending_runner_retirement_evidence"
    assert "not-a-checkpoint" not in issue.detail


def test_startup_scanner_quarantines_malformed_publishing_owner_without_guessing(
    restart_database,
):
    database_path, _engine, session_factory, add_step = restart_database
    payload = _publishing_payload()
    payload["publication_intent_sha256"] = "not-a-receipt"
    pipeline_id, step_id = add_step(payload)
    scanner = SqlAlchemyMaterialRestartRecoveryScanner(
        session_factory=session_factory,
        database_path=database_path,
    )

    report = scanner.scan()

    assert report.candidates == ()
    assert len(report.audit_required) == 1
    issue = report.audit_required[0]
    assert issue.pipeline_id == pipeline_id
    assert issue.material_step_id == step_id
    assert issue.code == "invalid_publishing_recovery_evidence"
    assert "not-a-receipt" not in issue.detail


def test_startup_scanner_fails_closed_when_candidate_bound_is_exceeded(
    restart_database,
):
    database_path, _engine, session_factory, add_step = restart_database
    add_step(_publishing_payload(material_task_id="task-001"))
    add_step(_publishing_payload(material_task_id="task-002"))
    scanner = SqlAlchemyMaterialRestartRecoveryScanner(
        session_factory=session_factory,
        database_path=database_path,
        max_candidates=1,
    )

    with pytest.raises(MaterialRestartRecoveryScanOverflow):
        scanner.scan()


def test_startup_scanner_candidate_bound_covers_publishing_and_pending_retirement(
    restart_database,
):
    database_path, _engine, session_factory, add_step = restart_database
    add_step(_publishing_payload(material_task_id="task-publishing"))
    add_step(_pending_runner_retirement_payload(material_task_id="task-retirement"))
    scanner = SqlAlchemyMaterialRestartRecoveryScanner(
        session_factory=session_factory,
        database_path=database_path,
        max_candidates=1,
    )

    with pytest.raises(MaterialRestartRecoveryScanOverflow):
        scanner.scan()


def test_startup_scanner_surfaces_unsupported_active_state_for_audit(
    restart_database,
):
    database_path, _engine, session_factory, add_step = restart_database
    payload = _publishing_payload()
    payload["execution_state"] = "running"
    pipeline_id, step_id = add_step(payload)
    scanner = SqlAlchemyMaterialRestartRecoveryScanner(
        session_factory=session_factory,
        database_path=database_path,
    )

    report = scanner.scan()

    assert report.candidates == ()
    assert len(report.audit_required) == 1
    assert report.audit_required[0].pipeline_id == pipeline_id
    assert report.audit_required[0].material_step_id == step_id
    assert report.audit_required[0].code == "unsupported_active_recovery_state"


def test_startup_scanner_ignores_non_material_steps(restart_database):
    database_path, _engine, session_factory, add_step = restart_database
    add_step(_publishing_payload(), name="生成脚本")
    scanner = SqlAlchemyMaterialRestartRecoveryScanner(
        session_factory=session_factory,
        database_path=database_path,
    )

    report = scanner.scan()

    assert report.candidates == ()
    assert report.audit_required == ()


def test_owner_lane_runs_restart_recovery_on_the_permanent_owner_thread(
    tmp_path: Path,
):
    command = MaterialRestartRecoveryCommand(
        pipeline_id=1,
        material_step_id=2,
        material_task_id="task-001",
        execution_id=str(uuid4()),
        attempt_id=str(uuid4()),
        operation="stock_candidates",
        control_id=str(uuid4()),
        job_instance_id=str(uuid4()),
        expected_revision=8,
        expected_payload_sha256="a" * 64,
        startup_timeout_seconds=1.0,
    )
    observed_threads: list[int] = []

    class _Handler:
        def execute(self, _command):
            raise AssertionError("restart recovery must not execute a new worker")

        def reconcile(self, _state, _ticket):
            raise AssertionError("restart recovery must not use an in-process ticket")

        def recover_restart_published(self, received):
            import threading

            observed_threads.append(threading.get_ident())
            assert received == command
            return MaterialExecutionResponse(
                pipeline_id=received.pipeline_id,
                material_step_id=received.material_step_id,
                material_task_id=received.material_task_id,
                execution_id=received.execution_id,
                attempt_id=received.attempt_id,
                operation=received.operation,
                status="succeeded",
                message="recovered",
                executed_task=None,
                tasks=(),
            )

    from app.pipeline.material_execution_application import MaterialExecutionLaneKey

    lane = MaterialExecutionOwnerLane(
        key=MaterialExecutionLaneKey(
            database_path=tmp_path / "lane.db",
            pipeline_id=1,
            material_step_id=2,
        ),
        handler=_Handler(),
    )
    try:
        result = lane.recover_restart_published(command).result(timeout=5)
        assert result.status == "succeeded"
        assert observed_threads == [lane.owner_thread_ident]
    finally:
        lane.close()


def test_publication_recovery_context_does_not_load_provider_secrets(tmp_path: Path):
    payload = _publishing_payload()
    command = MaterialRestartRecoveryCommand(
        pipeline_id=1,
        material_step_id=2,
        material_task_id="task-001",
        execution_id=payload["active_execution_id"],
        attempt_id=payload["active_attempt_id"],
        operation="stock_candidates",
        control_id=payload["active_control_id"],
        job_instance_id=payload["active_job_instance_id"],
        expected_revision=8,
        expected_payload_sha256="a" * 64,
    )
    secret_calls: list[str] = []

    def forbidden_secret_loader(operation, _paths):
        secret_calls.append(operation)
        raise AssertionError("restart recovery must not load provider secrets")

    factory = ServerMaterialOperationPlanFactory(
        material_root=tmp_path / "materials",
        secret_loader=forbidden_secret_loader,
    )

    context = factory.build_publication_recovery_context(command, payload)

    assert context.canonical_paths.root == (tmp_path / "materials").resolve()
    assert context.project_directory_uses == {}
    assert secret_calls == []


def test_route_free_lifespan_performs_read_only_startup_discovery(
    restart_database,
    tmp_path: Path,
):
    _database_path, engine, session_factory, add_step = restart_database
    add_step(_publishing_payload())
    runner_calls: list[str] = []

    def runner_factory(_key):
        runner_calls.append("runner")
        raise AssertionError("startup discovery must not create or rerun a worker")

    runtime = build_material_execution_runtime(
        engine=engine,
        session_factory=session_factory,
        material_root=tmp_path / "materials",
        runner_factory=runner_factory,
    )

    async def exercise() -> None:
        async with material_execution_runtime_lifespan(lambda: runtime) as active:
            assert active.startup_recovery_report is not None
            assert len(active.startup_recovery_report.candidates) == 1
            assert runner_calls == []

    asyncio.run(exercise())
    assert runtime.closed is True


def test_runtime_explicit_reconcile_revalidates_then_dispatches_exact_candidate(
    restart_database,
    tmp_path: Path,
):
    _database_path, engine, session_factory, add_step = restart_database
    pipeline_id, step_id = add_step(_publishing_payload())
    observed: list[MaterialRestartRecoveryCommand] = []

    class _Lane:
        def recover_restart_published(self, command):
            observed.append(command)
            with session_factory() as session:
                step = session.get(PipelineStep, step_id)
                assert step is not None
                step.output_summary = {
                    "material_tasks": [
                        {
                            "material_task_id": command.material_task_id,
                            "status": "已完成",
                        }
                    ],
                    "execution_id": command.execution_id,
                    "execution_revision": command.expected_revision + 1,
                    "execution_state": "idle",
                    "publication_phase": "committed",
                }
                session.commit()
            future: Future[MaterialExecutionResponse] = Future()
            future.set_result(
                MaterialExecutionResponse(
                    pipeline_id=command.pipeline_id,
                    material_step_id=command.material_step_id,
                    material_task_id=command.material_task_id,
                    execution_id=command.execution_id,
                    attempt_id=command.attempt_id,
                    operation=command.operation,
                    status="succeeded",
                    message="recovered",
                    executed_task=None,
                    tasks=(),
                )
            )
            return future

    class _Registry:
        def __init__(self):
            self.lane = _Lane()

        def get_or_create(self, key):
            assert key.pipeline_id == pipeline_id
            assert key.material_step_id == step_id
            return self.lane

        def close_all(self, *, timeout: float = 5.0):
            assert timeout > 0

    registry = _Registry()
    runtime = build_material_execution_runtime(
        engine=engine,
        session_factory=session_factory,
        material_root=tmp_path / "materials",
        registry_factory=lambda **_kwargs: registry,
    )

    async def exercise() -> None:
        async with material_execution_runtime_lifespan(lambda: runtime) as active:
            report = active.startup_recovery_report
            assert report is not None and len(report.candidates) == 1
            response = active.reconcile_restart_recovery(report.candidates[0])
            assert response.status == "succeeded"
            assert active.startup_recovery_report is not None
            assert active.startup_recovery_report.candidates == ()

    asyncio.run(exercise())
    assert len(observed) == 1


def test_runtime_rejects_candidate_changed_after_startup_without_dispatching(
    restart_database,
    tmp_path: Path,
):
    _database_path, engine, session_factory, add_step = restart_database
    _pipeline_id, step_id = add_step(_publishing_payload())
    observed: list[MaterialRestartRecoveryCommand] = []

    class _Lane:
        def recover_restart_published(self, command):
            observed.append(command)
            raise AssertionError("changed startup evidence must not reach the owner lane")

    class _Registry:
        def get_or_create(self, _key):
            return _Lane()

        def close_all(self, *, timeout: float = 5.0):
            assert timeout > 0

    runtime = build_material_execution_runtime(
        engine=engine,
        session_factory=session_factory,
        material_root=tmp_path / "materials",
        registry_factory=lambda **_kwargs: _Registry(),
    )

    async def exercise() -> None:
        async with material_execution_runtime_lifespan(lambda: runtime) as active:
            report = active.startup_recovery_report
            assert report is not None and len(report.candidates) == 1
            candidate = report.candidates[0]
            with session_factory() as session:
                step = session.get(PipelineStep, step_id)
                assert step is not None
                changed = dict(step.output_summary)
                changed["publication_result_sha256"] = "9" * 64
                step.output_summary = changed
                session.commit()

            with pytest.raises(
                MaterialExecutionRuntimeError,
                match="changed after discovery",
            ):
                active.reconcile_restart_recovery(candidate)

    asyncio.run(exercise())
    assert observed == []


def test_main_lifespan_installs_internal_startup_report_without_adding_a_route(
    restart_database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _database_path, engine, session_factory, add_step = restart_database
    add_step(_publishing_payload())
    runtime = build_material_execution_runtime(
        engine=engine,
        session_factory=session_factory,
        material_root=tmp_path / "materials",
    )

    import app.main as main_module

    monkeypatch.setattr(main_module, "engine", engine)
    monkeypatch.setattr(
        main_module,
        "audit_database_identity",
        lambda _path: SimpleNamespace(blockers=(), warnings=()),
    )
    monkeypatch.setattr(main_module, "ensure_schema_ready", lambda _path: None)
    monkeypatch.setattr(
        main_module,
        "_run_startup_hotspot_check_if_enabled",
        lambda: None,
    )
    monkeypatch.setattr(
        main_module,
        "_build_internal_material_execution_runtime",
        lambda: runtime,
    )
    routes_before = tuple(route.path for route in main_module.app.routes)

    async def exercise() -> None:
        async with main_module.lifespan(main_module.app):
            assert main_module.app.state.material_execution_runtime is runtime
            report = main_module.app.state.material_restart_recovery_report
            assert len(report.candidates) == 1
            assert tuple(route.path for route in main_module.app.routes) == routes_before

    asyncio.run(exercise())
    assert runtime.closed is True
    assert not hasattr(main_module.app.state, "material_execution_runtime")
