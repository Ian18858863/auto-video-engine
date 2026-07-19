from __future__ import annotations

import asyncio
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, build_engine
from app.materials.asset_paths import AutoMaterialPaths
from app.materials.paths import PROJECT_DIRS
from app.models.entities import HotItem, PipelineStep, ProductionPipeline
from app.pipeline.material_execution_application import (
    MaterialExecutionCommand,
    MaterialExecutionLaneKey,
    MaterialExecutionResponse,
    MaterialRunnerRetirementRecoveryCommand,
)
from app.pipeline.material_execution_runtime import (
    MaterialExecutionRuntime,
    MaterialExecutionRuntimeError,
    build_material_execution_runtime,
    derive_submission_sidecar_path,
    material_execution_runtime_lifespan,
)
from app.pipeline.material_restart_recovery import (
    MaterialRestartRecoveryCandidate,
    MaterialRestartRecoveryScanReport,
)
from app.pipeline.material_operation_protocol import PexelsSecrets
from app.pipeline.project_identity import (
    PreparedProjectChildDirectoryUse,
    add_pipeline_with_identity,
    begin_prepared_project_child_directory_use,
    prepare_pipeline_project_directory,
)
from app.pipeline.material_execution_submission_owner_lease import (
    MaterialExecutionSubmissionOwnerLease,
)
from app.pipeline.material_execution_submission_store import (
    MaterialExecutionSubmissionStore,
)


@pytest.fixture()
def runtime_inputs(tmp_path: Path):
    database_path = tmp_path / "runtime.db"
    engine = build_engine(f"sqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    material_root = tmp_path / "materials"
    try:
        yield {
            "database_path": database_path,
            "engine": engine,
            "session_factory": session_factory,
            "material_root": material_root,
        }
    finally:
        engine.dispose()


def test_sidecar_is_a_d_drive_sibling_and_not_the_primary_database(runtime_inputs):
    database_path = runtime_inputs["database_path"]
    sidecar = derive_submission_sidecar_path(database_path)

    assert sidecar.drive.upper() == "D:"
    assert sidecar.parent == database_path.parent.resolve()
    assert sidecar != database_path.resolve()
    assert sidecar.name == "runtime.material-submissions.sqlite3"


def test_runtime_dispatches_pending_retirement_to_the_retirement_lane_surface(
    tmp_path: Path,
) -> None:
    execution_id, attempt_id, control_id, job_instance_id = (
        str(uuid4()) for _ in range(4)
    )
    lane_key = MaterialExecutionLaneKey(
        database_path=tmp_path / "retirement-runtime.db",
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
        checkpoint_sha256="a" * 64,
    )
    candidate = MaterialRestartRecoveryCandidate(
        lane_key=lane_key,
        command=command,
        candidate_id="b" * 64,
    )
    report = MaterialRestartRecoveryScanReport(
        candidates=(candidate,),
        audit_required=(),
    )
    empty_report = MaterialRestartRecoveryScanReport(
        candidates=(),
        audit_required=(),
    )
    response = MaterialExecutionResponse(
        pipeline_id=18,
        material_step_id=31,
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="pexels",
        status="failed",
        message="runner retirement recovered",
        executed_task=None,
        tasks=(),
    )
    calls: list[tuple[str, object]] = []

    class _Lane:
        def recover_restart_published(self, _command):
            raise AssertionError("pending retirement must not use publication recovery")

        def recover_runner_retirement(self, value):
            calls.append(("retirement", value))
            future: Future[MaterialExecutionResponse] = Future()
            future.set_result(response)
            return future

    class _Scanner:
        def __init__(self):
            self.reports = [report, empty_report]

        def scan(self):
            return self.reports.pop(0)

    runtime = MaterialExecutionRuntime(
        database_path=lane_key.database_path,
        submission_store=SimpleNamespace(),
        owner_lease=SimpleNamespace(assert_active=lambda: None),
        registry=SimpleNamespace(get_or_create=lambda value: _Lane()),
        coordinator=SimpleNamespace(close=lambda **_kwargs: None),
        command_factory=SimpleNamespace(),
        plan_factory=SimpleNamespace(),
        restart_scanner=_Scanner(),
        _startup_recovery_report=report,
    )

    result = runtime.reconcile_restart_recovery(candidate, timeout=1)

    assert result is response
    assert calls == [("retirement", command)]
    assert runtime.startup_recovery_report == empty_report


def test_explicit_sidecar_path_is_used_as_given(runtime_inputs):
    explicit = runtime_inputs["database_path"].with_name("explicit.sqlite3")
    runtime = build_material_execution_runtime(
        engine=runtime_inputs["engine"],
        session_factory=runtime_inputs["session_factory"],
        material_root=runtime_inputs["material_root"],
        sidecar_path=explicit,
    )
    try:
        assert runtime.submission_store.database_path == explicit.resolve()
    finally:
        runtime.close()


def test_runtime_wires_store_lease_registry_and_fresh_command_factory(runtime_inputs):
    runtime = build_material_execution_runtime(
        engine=runtime_inputs["engine"],
        session_factory=runtime_inputs["session_factory"],
        material_root=runtime_inputs["material_root"],
    )
    try:
        assert isinstance(runtime.submission_store, MaterialExecutionSubmissionStore)
        assert runtime.submission_store.database_path.drive.upper() == "D:"
        assert runtime.owner_lease.store is runtime.submission_store
        assert runtime.coordinator.owner_lease is runtime.owner_lease
        assert runtime.command_factory.database_path == runtime_inputs["database_path"].resolve()
        assert runtime.plan_factory.material_root == runtime_inputs["material_root"].resolve()
        assert not runtime_inputs["material_root"].exists()
    finally:
        runtime.close()


def test_runtime_plan_factory_builds_real_project_guards_from_authoritative_database(
    runtime_inputs,
    tmp_path: Path,
):
    project_root = tmp_path / "projects"
    project_root.mkdir()
    with runtime_inputs["session_factory"]() as session:
        hot_item = HotItem(
            source_platform="test",
            source_type="test",
            title="project guard runtime",
            url=f"https://example.invalid/{uuid4().hex}",
        )
        session.add(hot_item)
        session.flush()
        pipeline = ProductionPipeline(
            hotspot_id=hot_item.id,
            project_name="runtime-project-guard",
            status="进行中",
        )
        add_pipeline_with_identity(session, pipeline)
        step = PipelineStep(
            name="生成素材任务",
            step_order=3,
            output_summary={
                "material_tasks": [
                    {
                        "material_task_id": "material-shot-1",
                        "shot_id": "shot-1",
                        "asset_keywords": ["AI", "办公"],
                    }
                ]
            },
        )
        pipeline.steps.append(step)
        session.commit()
        prepare_pipeline_project_directory(pipeline, project_root=project_root)
        for dirname in PROJECT_DIRS:
            begin_prepared_project_child_directory_use(
                pipeline,
                project_root=project_root,
                relative_path=dirname,
                create=True,
            )
        session.commit()
        pipeline_id = pipeline.id
        project_key = pipeline.project_key
        step_id = step.id
        snapshot = dict(step.output_summary or {})

    def secret_loader(operation: str, _paths: AutoMaterialPaths):
        assert operation == "pexels"
        return PexelsSecrets("server-test-key")

    runtime = build_material_execution_runtime(
        engine=runtime_inputs["engine"],
        session_factory=runtime_inputs["session_factory"],
        material_root=runtime_inputs["material_root"],
        project_root=project_root,
        secret_loader=secret_loader,
    )
    try:
        plan = runtime.plan_factory(
            MaterialExecutionCommand(
                pipeline_id=pipeline_id,
                material_step_id=step_id,
                material_task_id="material-shot-1",
                execution_id=str(uuid4()),
                operation="pexels",
                startup_timeout_seconds=1.0,
                operation_timeout_seconds=20.0,
            ),
            snapshot,
        )

        assert set(plan.project_directory_uses) == set(PROJECT_DIRS)
        assert all(
            isinstance(item, PreparedProjectChildDirectoryUse)
            for item in plan.project_directory_uses.values()
        )
        assert plan.project_directory_uses["Pexels素材"].path == (
            project_root / project_key / "Pexels素材"
        )
        assert plan.project_directory_uses["全部素材"].path == (
            project_root / project_key / "全部素材"
        )
        for item in plan.project_directory_uses.values():
            item.verify()
    finally:
        runtime.close()


def test_runtime_close_is_idempotent_and_keeps_runner_lazy(runtime_inputs):
    calls: list[str] = []

    def runner_factory(_key):
        calls.append("runner")
        raise AssertionError("runner must be created only after a lane executes")

    runtime = build_material_execution_runtime(
        engine=runtime_inputs["engine"],
        session_factory=runtime_inputs["session_factory"],
        material_root=runtime_inputs["material_root"],
        runner_factory=runner_factory,
    )
    runtime.close()
    runtime.close()
    assert calls == []
    assert runtime.owner_lease.closed is True


def test_runtime_unwinds_owner_lease_when_registry_setup_fails(runtime_inputs):
    def broken_registry(**_kwargs):
        raise RuntimeError("registry setup failed")

    with pytest.raises(RuntimeError, match="registry setup failed"):
        build_material_execution_runtime(
            engine=runtime_inputs["engine"],
            session_factory=runtime_inputs["session_factory"],
            material_root=runtime_inputs["material_root"],
            registry_factory=broken_registry,
        )

    # The failed build must not leave the process-wide mutex held.
    store = MaterialExecutionSubmissionStore(
        derive_submission_sidecar_path(runtime_inputs["database_path"])
    )
    lease = MaterialExecutionSubmissionOwnerLease.acquire(store)
    lease.close()


@pytest.mark.parametrize("bad_path", [Path("C:/primary.db"), Path("relative.db")])
def test_sidecar_derivation_rejects_non_d_paths(bad_path: Path):
    with pytest.raises((MaterialExecutionRuntimeError, ValueError)):
        derive_submission_sidecar_path(bad_path)


def test_runtime_rejects_non_callable_session_factory(runtime_inputs):
    with pytest.raises(TypeError, match="session_factory"):
        build_material_execution_runtime(
            engine=runtime_inputs["engine"],
            session_factory=object(),
            material_root=runtime_inputs["material_root"],
        )


def test_route_free_runtime_lifespan_closes_after_yield(runtime_inputs):
    runtime = build_material_execution_runtime(
        engine=runtime_inputs["engine"],
        session_factory=runtime_inputs["session_factory"],
        material_root=runtime_inputs["material_root"],
    )

    async def exercise() -> None:
        async with material_execution_runtime_lifespan(lambda: runtime) as active:
            assert active is runtime
            assert active.closed is False

    asyncio.run(exercise())
    assert runtime.closed is True
    assert runtime.owner_lease.closed is True


def test_route_free_runtime_lifespan_propagates_startup_failure():
    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="startup failed"):
            async with material_execution_runtime_lifespan(
                lambda: (_ for _ in ()).throw(RuntimeError("startup failed"))
            ):
                raise AssertionError("lifespan must not yield after startup failure")

    asyncio.run(exercise())


def test_route_free_runtime_lifespan_keeps_lease_when_shutdown_is_unknown(
    runtime_inputs,
):
    runtime = build_material_execution_runtime(
        engine=runtime_inputs["engine"],
        session_factory=runtime_inputs["session_factory"],
        material_root=runtime_inputs["material_root"],
    )
    original_close = runtime.coordinator.close

    def fail_close(*, timeout: float = 5.0) -> None:
        raise RuntimeError("shutdown acknowledgement is unknown")

    runtime.coordinator.close = fail_close  # type: ignore[method-assign]
    try:
        async def exercise() -> None:
            with pytest.raises(MaterialExecutionRuntimeError, match="could not close"):
                async with material_execution_runtime_lifespan(lambda: runtime):
                    pass

        asyncio.run(exercise())
        assert runtime.closed is False
        assert runtime.owner_lease.closed is False
    finally:
        runtime.coordinator.close = original_close  # type: ignore[method-assign]
        runtime.close()
