from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models import (
    AiGeneration,
    HotItem,
    PipelineStep,
    ProductionPipeline,
    ScriptAsset,
    TopicIdea,
)
from app.pipeline.list_service import derive_pipeline_items, list_pipelines
from app.pipeline.models import PIPELINE_STEP_NAMES


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def add_hot_item(session: Session, suffix: str) -> HotItem:
    item = HotItem(
        source_platform="手动导入",
        source_type="manual",
        title=f"测试热点 {suffix}",
        url=f"https://example.com/project-list-{suffix}",
        published_at=datetime.now(timezone.utc),
        summary="用于测试项目列表查询。",
        category="AI",
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def add_pipeline(
    session: Session,
    *,
    suffix: str,
    status: str,
    step_statuses: list[str],
    error: str = "",
    created_minutes_ago: int = 0,
) -> ProductionPipeline:
    hot_item = add_hot_item(session, suffix)
    topic = TopicIdea(hot_item_id=hot_item.id, title=f"选题 {suffix}")
    session.add(topic)
    session.flush()
    script = ScriptAsset(topic_idea_id=topic.id, voiceover=f"脚本 {suffix}")
    session.add(script)
    session.flush()
    created_at = datetime.now(timezone.utc) - timedelta(minutes=created_minutes_ago)
    pipeline = ProductionPipeline(
        hotspot_id=hot_item.id,
        topic_idea_id=topic.id,
        script_asset_id=script.id,
        project_name=f"项目 {suffix}",
        project_dir=f"D:/短视频工厂/项目/项目 {suffix}",
        status=status,
        provider="fake",
        model="fake-low-cost",
        created_at=created_at,
        updated_at=created_at,
    )
    pipeline.steps = []
    for index, name in enumerate(PIPELINE_STEP_NAMES, start=1):
        step_status = step_statuses[index - 1]
        output_summary = {}
        if name == "选择候选选题" and step_status == "已完成":
            output_summary = {"topic_idea_id": topic.id}
        elif name == "生成脚本素材" and step_status == "已完成":
            output_summary = {"script_asset_id": script.id}
        pipeline.steps.append(
            PipelineStep(
                name=name,
                step_order=index,
                status=step_status,
                error=error if step_status == "失败" else "",
                output_summary=output_summary,
            )
        )
    session.add(pipeline)
    session.commit()
    session.refresh(pipeline)
    return pipeline


def test_list_pipelines_returns_multiple_projects_and_progress():
    with make_session() as session:
        add_pipeline(
            session,
            suffix="完成",
            status="已完成",
            step_statuses=["已完成"] * len(PIPELINE_STEP_NAMES),
            created_minutes_ago=5,
        )
        paused = add_pipeline(
            session,
            suffix="暂停",
            status="需要人工确认",
            step_statuses=["已完成"] * 3
            + ["未开始"] * (len(PIPELINE_STEP_NAMES) - 3),
        )

        result = list_pipelines(session)

        assert result.total == 2
        assert len(result.items) == 2
        assert result.items[0].pipeline_id == paused.id
        assert result.items[0].current_step == "生成素材任务"
        assert result.items[0].completed_steps == 3
        assert result.items[0].total_steps == len(PIPELINE_STEP_NAMES)
        assert result.items[0].needs_confirmation is False
        assert result.items[0].completion_reason_code == "preparation_incomplete"
        assert result.items[0].next_surface == "project"
        assert result.items[0].next_suggestion == "打开项目页继续生产"


def test_list_pipelines_supports_status_failure_and_confirmation_filters():
    with make_session() as session:
        completed = add_pipeline(
            session,
            suffix="完成",
            status="已完成",
            step_statuses=["已完成"] * len(PIPELINE_STEP_NAMES),
        )
        failed = add_pipeline(
            session,
            suffix="失败",
            status="需要人工确认",
            step_statuses=["已完成", "失败"]
            + ["未开始"] * (len(PIPELINE_STEP_NAMES) - 2),
            error="候选选择失败",
        )
        confirmation = add_pipeline(
            session,
            suffix="确认",
            status="需要人工确认",
            step_statuses=["已完成", "需要人工确认"]
            + ["未开始"] * (len(PIPELINE_STEP_NAMES) - 2),
        )

        status_result = list_pipelines(session, status="需要人工确认")
        failed_result = list_pipelines(session, failed_only=True)
        confirmation_result = list_pipelines(
            session,
            needs_confirmation_only=True,
        )

        assert {item.completion_reason_code for item in status_result.items} == {
            "approval_missing",
            "step_confirmation_required",
        }
        assert [item.pipeline_id for item in failed_result.items] == [failed.id]
        assert failed_result.items[0].has_failed is True
        assert failed_result.items[0].failure_reason == "候选选择失败"
        assert {item.pipeline_id for item in confirmation_result.items} == {
            completed.id,
            failed.id,
            confirmation.id,
        }


def test_list_pipelines_supports_limit_and_pagination_without_ai_calls():
    with make_session() as session:
        for index in range(5):
            add_pipeline(
                session,
                suffix=str(index),
                status="已完成",
                step_statuses=["已完成"] * len(PIPELINE_STEP_NAMES),
                created_minutes_ago=index,
            )
        generation_count = session.query(AiGeneration).count()

        result = list_pipelines(session, page=2, page_size=2)

        assert result.total == 5
        assert result.page == 2
        assert result.page_size == 2
        assert result.total_pages == 3
        assert len(result.items) == 2
        assert session.query(AiGeneration).count() == generation_count


def test_pipeline_list_api_is_read_only_and_supports_filters():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        add_pipeline(
            setup_session,
            suffix="完成",
            status="已完成",
            step_statuses=["已完成"] * len(PIPELINE_STEP_NAMES),
        )
        failed = add_pipeline(
            setup_session,
            suffix="失败",
            status="需要人工确认",
            step_statuses=["失败"] + ["未开始"] * (len(PIPELINE_STEP_NAMES) - 1),
            error="测试失败",
        )

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.get(
            "/api/pipeline",
            params={"failed_only": True, "page": 1, "page_size": 1},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["pagination"] == {
        "page": 1,
        "page_size": 1,
        "total": 1,
        "total_pages": 1,
    }
    assert data["items"][0]["pipeline_id"] == failed.id
    assert data["items"][0]["failure_reason"] == "测试失败"
    assert data["items"][0]["status"] == "失败"
    assert data["items"][0]["completion_reason_code"] == "step_failed"
    assert data["items"][0]["next_surface"] == "project"


def test_completion_status_api_exposes_derived_decision_without_mutating_pipeline():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        pipeline = add_pipeline(
            setup_session,
            suffix="completion-status",
            status="已完成",
            step_statuses=["已完成"] * len(PIPELINE_STEP_NAMES),
        )
        stored_status = pipeline.status

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.get(f"/api/pipeline/{pipeline.id}/completion-status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "pipeline_id": pipeline.id,
        "status": "需要人工确认",
        "is_complete": False,
        "completion_reason_code": "approval_missing",
        "message": "生产准备已完成，但尚未验收成片。",
        "next_action": "打开成片页检查当前文件和验收状态",
        "next_surface": "finished",
        "current_step": "成片验收",
        "needs_confirmation": True,
        "evidence_present": False,
        "quality_gate_state": "not_evaluated",
    }

    with testing_session_local() as check_session:
        assert check_session.get(ProductionPipeline, pipeline.id).status == stored_status


def test_completion_status_api_reports_submitted_but_invalid_evidence_as_blocked():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        pipeline = add_pipeline(
            setup_session,
            suffix="completion-evidence-invalid",
            status="已完成",
            step_statuses=["已完成"] * len(PIPELINE_STEP_NAMES),
        )
        pipeline.completion_evidence = {"evidence_version": 1}
        setup_session.commit()
        pipeline_id = pipeline.id

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = TestClient(app).get(
            f"/api/pipeline/{pipeline_id}/completion-status"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["completion_reason_code"] == "evidence_invalid"
    assert payload["evidence_present"] is True
    assert payload["quality_gate_state"] == "blocked"


def test_completion_status_api_returns_verified_project_relative_evidence_after_restart(
    monkeypatch,
):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        pipeline = add_pipeline(
            setup_session,
            suffix="completion-evidence-restart",
            status="已完成",
            step_statuses=["已完成"] * len(PIPELINE_STEP_NAMES),
        )
        pipeline.completion_evidence = {
            "artifact_relative_path": "成片/versions/v1/rendered.mp4",
            "quality_report_relative_path": "成片/quality-reports/qr-v1.json",
        }
        setup_session.commit()
        pipeline_id = pipeline.id

    class _VerifiedDecision:
        is_complete = True
        status = "已完成"
        reason_code = "approved_artifact_verified"
        message = "当前成片与五项质量门均已通过验收。"
        next_action = ""
        next_surface = "none"
        current_step = "成片验收"
        needs_confirmation = False

    monkeypatch.setattr(
        "app.pipeline.service.evaluate_pipeline_completion",
        lambda *_args, **_kwargs: _VerifiedDecision(),
    )

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = TestClient(app).get(
            f"/api/pipeline/{pipeline_id}/completion-status"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_complete"] is True
    assert payload["artifact_relative_path"] == "成片/versions/v1/rendered.mp4"
    assert (
        payload["quality_report_relative_path"]
        == "成片/quality-reports/qr-v1.json"
    )


def test_completion_status_api_returns_not_found_for_unknown_pipeline():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = TestClient(app).get("/api/pipeline/999999/completion-status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_list_filters_derived_status_before_pagination_and_ignores_stored_status():
    with make_session() as session:
        failed = add_pipeline(
            session,
            suffix="stored-complete-failed",
            status="已完成",
            step_statuses=["失败"] + ["未开始"] * (len(PIPELINE_STEP_NAMES) - 1),
            error="真实失败",
            created_minutes_ago=2,
        )
        add_pipeline(
            session,
            suffix="stored-failed-running",
            status="失败",
            step_statuses=["已完成"] + ["未开始"] * (len(PIPELINE_STEP_NAMES) - 1),
            created_minutes_ago=1,
        )
        add_pipeline(
            session,
            suffix="approval-missing",
            status="进行中",
            step_statuses=["已完成"] * len(PIPELINE_STEP_NAMES),
        )

        result = list_pipelines(session, status="失败", page=1, page_size=1)

        assert result.total == 1
        assert result.total_pages == 1
        assert [item.pipeline_id for item in result.items] == [failed.id]
        assert result.items[0].status == "失败"


def test_derive_pipeline_items_marks_malformed_steps_without_repairing_database():
    with make_session() as session:
        pipeline = add_pipeline(
            session,
            suffix="malformed",
            status="已完成",
            step_statuses=["已完成"] * len(PIPELINE_STEP_NAMES),
        )
        session.delete(pipeline.steps[-1])
        session.commit()
        before = session.query(PipelineStep).filter_by(pipeline_id=pipeline.id).count()

        item = next(
            item for item in derive_pipeline_items(session) if item.pipeline_id == pipeline.id
        )

        assert item.status == "需要人工确认"
        assert item.completion_reason_code == "preparation_invalid"
        assert item.current_step == "项目检查"
        assert item.next_surface == "project"
        assert session.query(PipelineStep).filter_by(pipeline_id=pipeline.id).count() == before
