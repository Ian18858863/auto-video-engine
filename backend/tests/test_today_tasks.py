from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
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
from app.pipeline.models import PIPELINE_STEP_NAMES
from app.publishing.service import create_video_post
from app.today import service as today_service
from app.today.service import get_today_tasks, mark_today_task_done


NOW = datetime(2026, 6, 16, 9, 30, tzinfo=timezone.utc)


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def add_hotspot(
    session: Session,
    suffix: str,
    *,
    hot_score: float,
    topic_score: float,
    save_score: float,
) -> HotItem:
    item = HotItem(
        source_platform="Hacker News",
        source_type="community",
        title=f"热点 {suffix}",
        url=f"https://example.com/today-{suffix}",
        published_at=NOW - timedelta(hours=1),
        summary=f"热点 {suffix} 摘要。",
        tags=["AI", "商业"],
        category="AI",
        hot_score=hot_score,
        topic_score=topic_score,
        save_score=save_score,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def add_topic(session: Session, hot_item: HotItem, suffix: str) -> TopicIdea:
    idea = TopicIdea(
        hot_item_id=hot_item.id,
        title=f"选题 {suffix}",
        core_view="用大白话解释这个趋势背后的商业逻辑。",
        recommended_format="图文/PPT 动画 + 右下角数字人辅助",
        why_watch="能快速看懂变化。",
        why_save="可以复用判断框架。",
        comment_trigger="你会怎么判断这个趋势？",
        risk_note="不做投资建议。",
        recommended_duration="45 秒",
        hook="为什么这件事不只是一个热点？",
        video_type="single_deep_dive",
        generation_source="local_rule",
        recommendation_score=92,
        recommendation_bucket="today",
        source_hotspot_ids=[hot_item.id],
        source_hotspot_titles=[hot_item.title],
    )
    session.add(idea)
    session.commit()
    session.refresh(idea)
    return idea


def add_pipeline(
    session: Session,
    hot_item: HotItem,
    *,
    topic_idea_id: int | None = None,
    script_asset_id: int | None = None,
    status: str = "需要人工确认",
    failed: bool = False,
    confirmation_required: bool = False,
    completed_through: int = 1,
) -> ProductionPipeline:
    pipeline = ProductionPipeline(
        hotspot_id=hot_item.id,
        topic_idea_id=topic_idea_id,
        script_asset_id=script_asset_id,
        project_name="AI 项目",
        project_dir="D:/短视频工厂/项目/AI 项目",
        status=status,
        provider="fake",
        model="fake-low-cost",
        created_at=NOW - timedelta(hours=3),
        updated_at=NOW - timedelta(hours=2),
    )
    pipeline.steps = [
        PipelineStep(
            name=name,
            step_order=index,
            status=(
                "失败"
                if failed and index == 4
                else "需要人工确认"
                if confirmation_required and index == 4
                else "已完成"
                if index <= completed_through
                else "未开始"
            ),
            error="素材任务需要人工确认" if failed and index == 4 else "",
            output_summary=(
                {"topic_idea_id": topic_idea_id}
                if index == 2 and index <= completed_through
                else {"script_asset_id": script_asset_id}
                if index == 3 and index <= completed_through
                else {}
            ),
        )
        for index, name in enumerate(PIPELINE_STEP_NAMES, start=1)
    ]
    session.add(pipeline)
    session.commit()
    session.refresh(pipeline)
    return pipeline


def seed_dashboard(session: Session) -> tuple[HotItem, TopicIdea, ProductionPipeline]:
    hot = add_hotspot(
        session,
        "AI工具",
        hot_score=90,
        topic_score=96,
        save_score=92,
    )
    add_hotspot(
        session,
        "普通消费",
        hot_score=80,
        topic_score=76,
        save_score=65,
    )
    topic = add_topic(session, hot, "企业采购 AI")
    pipeline = add_pipeline(session, hot, topic_idea_id=topic.id, failed=True)
    post = create_video_post(
        session,
        {
            "pipeline_id": pipeline.id,
            "platform": "抖音",
            "video_title": "AI 工具采购复盘",
            "published_at": NOW - timedelta(hours=26),
            "video_format": "组合内容项目",
            "views": 1200,
            "likes": 90,
            "comments": 18,
            "favorites": 60,
            "shares": 28,
            "followers_gained": 12,
            "completion_rate": 48,
            "average_watch_seconds": 32,
        },
        captured_at=NOW - timedelta(hours=25),
    )
    session.flush()
    assert post.id
    return hot, topic, pipeline


def test_today_tasks_aggregates_hotspots_topics_projects_and_decision_points():
    with make_session() as session:
        hot, topic, pipeline = seed_dashboard(session)
        generation_count = session.query(AiGeneration).count()

        result = get_today_tasks(session, now=NOW)

        assert result.hotspots[0].target_id == hot.id
        assert result.topic_ideas[0].target_id == topic.id
        assert result.collectible_ideas[0].target_type == "topic"
        assert result.projects[0].target_id == pipeline.id
        assert result.confirmations[0].target_type == "project"
        assert [item.target_id for item in result.confirmations].count(pipeline.id) == 1
        assert any(item.target_type == "tracking" for item in result.publish_reminders)
        assert any(item.target_type == "review" for item in result.publish_reminders)
        assert result.suggestions == []
        assert [item.title for item in result.decision_items] == ["待确认选题"]
        assert result.decision_items[0].target_type == "topic_pool"
        assert session.query(AiGeneration).count() == generation_count


def test_today_tasks_reminds_to_select_topics_even_without_candidates():
    with make_session() as session:
        add_hotspot(
            session,
            "待选题",
            hot_score=82,
            topic_score=88,
            save_score=80,
        )

        result = get_today_tasks(session, now=NOW)

        assert len(result.decision_items) == 1
        reminder = result.decision_items[0]
        assert reminder.task_id == "confirm_daily_events"
        assert reminder.title == "待确认选题"
        assert reminder.subtitle == "还没有候选事件"
        assert reminder.next_action == "去选题"
        assert reminder.target_type == "topic_pool"


def test_mark_today_task_done_marks_matching_items_only_for_today():
    with make_session() as session:
        hot, _, _ = seed_dashboard(session)
        task_id = f"hotspot_{hot.id}"

        mark_today_task_done(session, task_id, now=NOW)
        result = get_today_tasks(session, now=NOW)

        assert task_id in result.handled_task_ids
        handled = next(item for item in result.hotspots if item.task_id == task_id)
        assert handled.is_done is True


def test_mark_today_task_done_refreshes_after_daily_0915_boundary():
    with make_session() as session:
        task_id = "confirm_daily_events"
        before_refresh = datetime(2026, 6, 16, 1, 14, tzinfo=timezone.utc)
        after_refresh = datetime(2026, 6, 16, 1, 15, tzinfo=timezone.utc)

        mark_today_task_done(session, task_id, now=before_refresh)

        assert task_id in get_today_tasks(session, now=before_refresh).handled_task_ids
        assert task_id not in get_today_tasks(session, now=after_refresh).handled_task_ids


def test_script_confirmation_disappears_after_downstream_starts():
    with make_session() as session:
        hot = add_hotspot(
            session,
            "AI脚本",
            hot_score=90,
            topic_score=96,
            save_score=92,
        )
        topic = add_topic(session, hot, "脚本确认")
        script = ScriptAsset(topic_idea_id=topic.id, voiceover="本期口播稿")
        session.add(script)
        session.commit()
        session.refresh(script)
        pipeline = add_pipeline(
            session,
            hot,
            topic_idea_id=topic.id,
            script_asset_id=script.id,
            status="需要人工确认",
            completed_through=3,
        )

        waiting = get_today_tasks(session, now=NOW)

        assert [item.title for item in waiting.decision_items] == ["脚本确认"]
        assert waiting.running_items == []

        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        material_step.status = "进行中"
        pipeline.status = "进行中"
        session.add_all([pipeline, material_step])
        session.commit()

        running = get_today_tasks(session, now=NOW)

        assert running.decision_items == []
        assert running.running_items[0].title == "正在处理素材"
        assert "AI 项目" in running.running_items[0].subtitle


def test_running_items_only_show_actively_running_projects():
    with make_session() as session:
        hot = add_hotspot(
            session,
            "AI流程",
            hot_score=90,
            topic_score=96,
            save_score=92,
        )
        topic = add_topic(session, hot, "流程状态")
        failed_pipeline = add_pipeline(
            session,
            hot,
            topic_idea_id=topic.id,
            status="需要人工确认",
            failed=True,
        )
        waiting_pipeline = add_pipeline(
            session,
            hot,
            topic_idea_id=topic.id,
            status="需要人工确认",
            failed=False,
            confirmation_required=True,
        )
        running_pipeline = add_pipeline(
            session,
            hot,
            topic_idea_id=topic.id,
            status="进行中",
            failed=False,
        )
        running_step = next(
            step for step in running_pipeline.steps if step.name == "生成素材任务"
        )
        running_step.status = "进行中"
        session.add_all([running_pipeline, running_step])
        session.commit()

        result = get_today_tasks(session, now=NOW)

        confirmation_ids = {item.target_id for item in result.confirmations}
        running_ids = [item.target_id for item in result.running_items]
        assert {failed_pipeline.id, waiting_pipeline.id}.issubset(confirmation_ids)
        assert running_ids == [running_pipeline.id]


def _derived_item(
    pipeline_id: int,
    *,
    topic_idea_id: int | None = None,
    script_asset_id: int | None = None,
    status: str = "进行中",
    reason_code: str = "preparation_incomplete",
    next_surface: str = "project",
    current_step: str = "生成素材任务",
    needs_confirmation: bool = False,
    has_failed: bool = False,
    failure_reason: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        pipeline_id=pipeline_id,
        topic_idea_id=topic_idea_id,
        script_asset_id=script_asset_id,
        project_name=f"项目 {pipeline_id}",
        status=status,
        completion_reason_code=reason_code,
        next_surface=next_surface,
        current_step=current_step,
        completed_steps=3,
        total_steps=len(PIPELINE_STEP_NAMES),
        has_failed=has_failed,
        failure_reason=failure_reason,
        needs_confirmation=needs_confirmation,
        next_suggestion=f"处理项目 {pipeline_id}",
    )


def _database_row_snapshot(session: Session, model, row_id: int) -> tuple:
    columns = tuple(model.__table__.columns)
    return tuple(
        session.execute(
            select(*columns).where(model.__table__.c.id == row_id)
        ).one()
    )


def test_today_get_does_not_archive_expired_active_hotspot_or_rebucket_topic():
    with make_session() as session:
        hot = add_hotspot(
            session,
            "已到期但仍活跃",
            hot_score=90,
            topic_score=96,
            save_score=92,
        )
        hot.published_at = NOW - timedelta(hours=72)
        topic = add_topic(session, hot, "不得由 GET 归档")
        session.add(hot)
        session.commit()
        before_hotspot = _database_row_snapshot(session, HotItem, hot.id)
        before_topic = _database_row_snapshot(session, TopicIdea, topic.id)

        get_today_tasks(session, now=NOW)

        session.expire_all()
        assert _database_row_snapshot(session, HotItem, hot.id) == before_hotspot
        assert _database_row_snapshot(session, TopicIdea, topic.id) == before_topic


def test_today_script_confirmation_uses_all_derived_items_without_first_100_limit(
    monkeypatch,
):
    with make_session() as session:
        hot = add_hotspot(
            session,
            "第101个脚本",
            hot_score=90,
            topic_score=96,
            save_score=92,
        )
        topic = add_topic(session, hot, "旧脚本确认")
        script = ScriptAsset(topic_idea_id=topic.id, voiceover="第101个项目的口播稿")
        session.add(script)
        session.commit()
        session.refresh(script)
        items = [_derived_item(index) for index in range(1, 101)]
        items.append(
            _derived_item(
                101,
                topic_idea_id=topic.id,
                script_asset_id=script.id,
            )
        )
        calls = 0

        def derive_once(_session):
            nonlocal calls
            calls += 1
            return items

        monkeypatch.setattr(today_service, "derive_pipeline_items", derive_once)
        monkeypatch.setattr(
            today_service,
            "_topic_confirmation_task",
            lambda *_args, **_kwargs: None,
        )

        result = get_today_tasks(session, now=NOW)

        assert calls == 1
        assert [item.task_id for item in result.decision_items] == [
            f"confirm_voiceover_101_{script.id}"
        ]
        assert result.decision_items[0].meta["script_asset_id"] == script.id


def test_today_malformed_derived_pipeline_cannot_bypass_unified_script_decision(
    monkeypatch,
):
    with make_session() as session:
        hot = add_hotspot(
            session,
            "异常脚本",
            hot_score=90,
            topic_score=96,
            save_score=92,
        )
        topic = add_topic(session, hot, "异常步骤不得确认脚本")
        script = ScriptAsset(topic_idea_id=topic.id, voiceover="不应展示的口播稿")
        session.add(script)
        session.commit()
        session.refresh(script)
        pipeline = add_pipeline(
            session,
            hot,
            topic_idea_id=topic.id,
            script_asset_id=script.id,
            completed_through=3,
        )
        session.add(
            PipelineStep(
                pipeline_id=pipeline.id,
                name="生成素材任务",
                step_order=len(PIPELINE_STEP_NAMES) + 1,
                status="未开始",
                output_summary={},
            )
        )
        session.commit()
        malformed_item = _derived_item(
            pipeline.id,
            topic_idea_id=topic.id,
            script_asset_id=script.id,
            status="需要人工确认",
            reason_code="preparation_invalid",
            current_step="项目检查",
            needs_confirmation=True,
            failure_reason="生产步骤数据不完整或不一致。",
        )
        monkeypatch.setattr(
            today_service,
            "derive_pipeline_items",
            lambda _session: [malformed_item],
        )
        monkeypatch.setattr(
            today_service,
            "_topic_confirmation_task",
            lambda *_args, **_kwargs: None,
        )

        result = get_today_tasks(session, now=NOW)

        assert result.decision_items == []


def test_today_derives_all_projects_once_then_ranks_old_final_review_before_limit(
    monkeypatch,
):
    with make_session() as session:
        items = [_derived_item(index) for index in range(1, 107)]
        items.append(
            _derived_item(
                107,
                status="需要人工确认",
                reason_code="step_confirmation_required",
                needs_confirmation=True,
            )
        )
        items.append(
            _derived_item(
                108,
                status="需要人工确认",
                reason_code="artifact_hash_mismatch",
                next_surface="finished",
                current_step="成片验收",
                needs_confirmation=True,
                failure_reason="当前成片文件已发生变化。",
            )
        )
        calls = 0

        def derive_once(_session):
            nonlocal calls
            calls += 1
            return items

        monkeypatch.setattr(today_service, "derive_pipeline_items", derive_once)

        result = get_today_tasks(session, now=NOW)

        assert calls == 1
        assert result.confirmations[0].target_id == 108
        assert result.confirmations[0].target_type == "finished"
        assert result.decision_items[0].target_id == 108
        assert result.decision_items[0].target_type == "finished"
        assert result.confirmations[0].meta == {
            "status": "需要人工确认",
            "completion_reason_code": "artifact_hash_mismatch",
            "next_surface": "finished",
            "current_step": "成片验收",
            "needs_confirmation": True,
            "next_suggestion": "处理项目 108",
            "has_failed": False,
        }


def test_today_does_not_silently_drop_ninth_system_blocker(monkeypatch):
    with make_session() as session:
        blockers = [
            _derived_item(
                pipeline_id,
                status="需要人工确认",
                reason_code="artifact_hash_mismatch",
                next_surface="finished",
                current_step="成片验收",
                needs_confirmation=True,
                failure_reason=f"项目 {pipeline_id} 的成片已变化。",
            )
            for pipeline_id in range(1, 10)
        ]
        running = [
            _derived_item(
                pipeline_id,
                status="进行中",
                reason_code="step_running",
            )
            for pipeline_id in range(99, 108)
        ]
        monkeypatch.setattr(
            today_service,
            "derive_pipeline_items",
            lambda _session: [*blockers, *running],
        )
        monkeypatch.setattr(
            today_service,
            "_topic_confirmation_task",
            lambda *_args, **_kwargs: None,
        )

        result = get_today_tasks(session, now=NOW)

        assert [item.target_id for item in result.confirmations] == list(range(1, 10))
        assert [item.target_id for item in result.running_items] == list(range(99, 108))


def test_today_finished_contains_only_verified_artifact_and_routes_evidence_failure(
    monkeypatch,
):
    with make_session() as session:
        approved = _derived_item(
            1,
            status="已完成",
            reason_code="approved_artifact_verified",
            next_surface="none",
            current_step="已完成",
        )
        evidence_failure = _derived_item(
            2,
            status="需要人工确认",
            reason_code="quality_gate_failed",
            next_surface="finished",
            current_step="成片验收",
            needs_confirmation=True,
            failure_reason="成片仍有未通过的质量门。",
        )
        monkeypatch.setattr(
            today_service,
            "derive_pipeline_items",
            lambda _session: [approved, evidence_failure],
        )

        result = get_today_tasks(session, now=NOW)

        assert [item.target_id for item in result.finished_items] == [1]
        assert result.finished_items[0].target_type == "finished"
        assert result.finished_items[0].meta["completion_reason_code"] == (
            "approved_artifact_verified"
        )
        attention = next(item for item in result.confirmations if item.target_id == 2)
        assert attention.target_type == "finished"
        assert attention.meta["completion_reason_code"] == "quality_gate_failed"


def test_today_tasks_api_is_read_only_and_supports_mark_done():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        hot, _, _ = seed_dashboard(setup_session)
        task_id = f"hotspot_{hot.id}"
        generation_count = setup_session.query(AiGeneration).count()

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.get("/api/today/tasks")
        done_response = client.post(f"/api/today/tasks/{task_id}/complete")
        refreshed_response = client.get("/api/today/tasks")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert done_response.status_code == 200
    assert refreshed_response.status_code == 200
    assert task_id in refreshed_response.json()["handled_task_ids"]
    with testing_session_local() as verify_session:
        assert verify_session.query(AiGeneration).count() == generation_count


def test_today_complete_api_rejects_current_system_blocker(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    failed = _derived_item(
        7,
        status="失败",
        reason_code="step_failed",
        current_step="生成素材任务",
        needs_confirmation=True,
        has_failed=True,
        failure_reason="素材下载失败。",
    )
    monkeypatch.setattr(
        today_service,
        "derive_pipeline_items",
        lambda _session: [failed],
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
        response = client.post("/api/today/tasks/confirm_project_7/complete")
        refreshed = client.get("/api/today/tasks")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert "解决实际阻断" in response.json()["detail"]
    assert "confirm_project_7" not in refreshed.json()["handled_task_ids"]
