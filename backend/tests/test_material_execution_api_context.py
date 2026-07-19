from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, build_engine
from app.models.entities import HotItem, PipelineStep, ProductionPipeline
from app.pipeline.material_execution_api_context import (
    FreshMaterialExecutionSnapshot,
    MaterialExecutionCommandFactory,
    MaterialExecutionIntent,
    MaterialExecutionSubmissionContext,
    MaterialExecutionSnapshotIdentityError,
    SqlAlchemyFreshMaterialExecutionSnapshotLoader,
)
from app.pipeline.material_execution_application import (
    MaterialExecutionCommand,
    MaterialExecutionLaneKey,
)


class _CountingLoader:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0

    def load(self, **kwargs):
        self.calls += 1
        return self.inner.load(**kwargs)


@pytest.fixture()
def context_runtime(tmp_path: Path):
    database_path = tmp_path / "api-context.db"
    assert database_path.drive.upper() == "D:"
    engine = build_engine(f"sqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )
    with session_factory() as session:
        hot_item = HotItem(
            source_platform="test",
            source_type="test",
            title="api context",
            url=f"https://example.invalid/{uuid4().hex}",
        )
        session.add(hot_item)
        session.flush()
        pipeline = ProductionPipeline(
            hotspot_id=hot_item.id,
            project_name="api-context",
            project_dir=str(tmp_path / "project"),
            project_key=f"pending-{uuid4().hex}-api-context",
        )
        step = PipelineStep(
            name="material",
            step_order=3,
            output_summary={
                "material_tasks": [
                    {
                        "shot_id": "shot-1",
                        "material_task_id": "material-shot-1",
                        "status": "pending",
                    }
                ],
                "execution_revision": 0,
            },
        )
        pipeline.steps.append(step)
        session.add(pipeline)
        session.commit()
        pipeline_id = pipeline.id
        step_id = step.id

    loader = _CountingLoader(
        SqlAlchemyFreshMaterialExecutionSnapshotLoader(
            session_factory=session_factory,
        )
    )
    factory = MaterialExecutionCommandFactory(
        engine=engine,
        snapshot_loader=loader,
    )
    try:
        yield {
            "database_path": database_path,
            "engine": engine,
            "factory": factory,
            "loader": loader,
            "pipeline_id": pipeline_id,
            "session_factory": session_factory,
            "step_id": step_id,
            "tmp_path": tmp_path,
        }
    finally:
        engine.dispose()


def _intent(runtime, **overrides) -> dict:
    value = {
        "pipeline_id": runtime["pipeline_id"],
        "material_step_id": runtime["step_id"],
        "material_task_id": "material-shot-1",
        "execution_id": str(uuid4()),
        "operation": "pexels",
        "source_chain": ["pexels", "pixabay"],
        "startup_timeout_seconds": 1.0,
        "operation_timeout_seconds": 10.0,
    }
    value.update(overrides)
    return value


def test_factory_uses_engine_path_and_one_fresh_database_snapshot(context_runtime):
    runtime = context_runtime
    prepared = runtime["factory"].build(_intent(runtime))

    assert runtime["loader"].calls == 1
    assert prepared.lane_key.database_path == runtime["database_path"].resolve()
    assert prepared.command.pipeline_id == runtime["pipeline_id"]
    assert prepared.command.material_step_id == runtime["step_id"]
    assert prepared.command.material_task_id == "material-shot-1"
    assert prepared.source_chain == ("pexels", "pixabay")
    assert len(prepared.snapshot_sha256) == 64


def test_context_accepts_atomic_stock_candidates_and_rejects_mixed_chain(
    context_runtime,
):
    runtime = context_runtime
    prepared = runtime["factory"].build(
        _intent(
            runtime,
            operation="stock_candidates",
            source_chain=["stock_candidates"],
        )
    )
    assert prepared.command.operation == "stock_candidates"
    assert prepared.source_chain == ("stock_candidates",)
    calls_after_success = runtime["loader"].calls

    with pytest.raises(ValidationError, match="must run alone"):
        runtime["factory"].build(
            _intent(
                runtime,
                operation="stock_candidates",
                source_chain=["stock_candidates", "pexels"],
            )
        )
    assert runtime["loader"].calls == calls_after_success


def test_intent_rejects_client_path_or_snapshot_before_fresh_read(context_runtime):
    runtime = context_runtime
    for forbidden in (
        {"database_path": "C:/attacker.db"},
        {"output_summary": {"material_tasks": [{"material_task_id": "fake"}]}},
        {"pipeline_snapshot": {"id": runtime["pipeline_id"]}},
    ):
        body = _intent(runtime)
        body.update(forbidden)
        with pytest.raises(ValidationError):
            runtime["factory"].build(body)

    assert runtime["loader"].calls == 0


def test_second_build_reads_database_again_and_rejects_stale_task(context_runtime):
    runtime = context_runtime
    first = runtime["factory"].build(_intent(runtime))

    with runtime["session_factory"]() as session:
        step = session.get(PipelineStep, runtime["step_id"])
        assert step is not None
        step.output_summary = {
            "material_tasks": [
                {
                    "material_task_id": "material-shot-2",
                    "shot_id": "shot-2",
                    "status": "pending",
                }
            ],
            "execution_revision": 1,
        }
        session.commit()

    with pytest.raises(ValueError, match="does not contain"):
        runtime["factory"].build(_intent(runtime))

    second = runtime["factory"].build(
        _intent(runtime, material_task_id="material-shot-2")
    )
    assert runtime["loader"].calls == 3
    assert second.command.material_task_id == "material-shot-2"
    assert second.snapshot_sha256 != first.snapshot_sha256


def test_factory_rejects_snapshot_identity_or_database_mismatch(context_runtime):
    runtime = context_runtime

    class _WrongIdentityLoader:
        def load(self, **kwargs):
            return FreshMaterialExecutionSnapshot(
                database_path=runtime["database_path"],
                pipeline_id=kwargs["pipeline_id"] + 1,
                material_step_id=kwargs["material_step_id"],
                output_summary={"material_tasks": []},
            )

    factory = MaterialExecutionCommandFactory(
        engine=runtime["engine"],
        snapshot_loader=_WrongIdentityLoader(),
    )
    with pytest.raises(MaterialExecutionSnapshotIdentityError):
        factory.build(_intent(runtime))

    other_path = runtime["tmp_path"] / "other.db"
    other_engine = build_engine(f"sqlite:///{other_path.as_posix()}")
    Base.metadata.create_all(other_engine)
    other_sessions = sessionmaker(bind=other_engine, autoflush=False, autocommit=False)
    try:
        wrong_database_loader = SqlAlchemyFreshMaterialExecutionSnapshotLoader(
            session_factory=other_sessions,
        )
        factory = MaterialExecutionCommandFactory(
            engine=runtime["engine"],
            snapshot_loader=wrong_database_loader,
        )
        with pytest.raises(MaterialExecutionSnapshotIdentityError):
            factory.build(_intent(runtime))
    finally:
        other_engine.dispose()


def test_factory_rejects_non_d_or_memory_engine_before_loading():
    memory_engine = build_engine("sqlite:///:memory:")
    try:
        with pytest.raises(ValueError, match="disk|磁盘|D:"):
            MaterialExecutionCommandFactory(
                engine=memory_engine,
                snapshot_loader=object(),
            )
    finally:
        memory_engine.dispose()


def test_intent_defaults_and_reparse_close_pydantic_copy_bypass(context_runtime):
    runtime = context_runtime
    body = _intent(runtime)
    body.pop("startup_timeout_seconds")
    body.pop("operation_timeout_seconds")
    parsed = MaterialExecutionIntent.model_validate(body)
    assert parsed.startup_timeout_seconds == 2.0
    assert parsed.operation_timeout_seconds == 180.0

    forged = parsed.model_copy(update={"source_chain": ("pexels", "pexels")})
    with pytest.raises(ValidationError):
        runtime["factory"].build(forged)
    assert runtime["loader"].calls == 0


def test_submission_context_rejects_unsupported_chain_and_bad_digest():
    key = MaterialExecutionLaneKey(
        database_path=Path("D:/context-test.db"),
        pipeline_id=1,
        material_step_id=2,
    )
    command = MaterialExecutionCommand(
        pipeline_id=1,
        material_step_id=2,
        material_task_id="material-shot-1",
        execution_id=str(uuid4()),
        operation="pexels",
        startup_timeout_seconds=1.0,
        operation_timeout_seconds=2.0,
    )
    with pytest.raises(ValueError, match="unsupported"):
        MaterialExecutionSubmissionContext(
            lane_key=key,
            command=command,
            source_chain=("pexels", "not-an-operation"),
            snapshot_sha256="0" * 64,
        )
    with pytest.raises(ValueError, match="SHA-256"):
        MaterialExecutionSubmissionContext(
            lane_key=key,
            command=command,
            source_chain=("pexels",),
            snapshot_sha256=None,  # type: ignore[arg-type]
        )
