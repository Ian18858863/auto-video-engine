from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models import AiGeneration, HotItem, ProductionPipeline, VideoPostSnapshot
from app.publishing.service import (
    add_video_snapshot,
    create_video_post,
    get_video_post,
    list_video_posts,
    update_video_post,
)


NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def add_pipeline(session: Session, suffix: str = "AI项目") -> ProductionPipeline:
    hotspot = HotItem(
        source_platform="手动导入",
        source_type="manual",
        title=f"热点 {suffix}",
        url=f"https://example.com/post-{suffix}",
        published_at=NOW,
        summary="发布数据测试。",
        category="AI",
    )
    session.add(hotspot)
    session.flush()
    pipeline = ProductionPipeline(
        hotspot_id=hotspot.id,
        project_name=suffix,
        status="已完成",
        provider="fake",
        model="fake-low-cost",
    )
    session.add(pipeline)
    session.commit()
    session.refresh(pipeline)
    return pipeline


def post_payload(pipeline_id: int) -> dict:
    return {
        "pipeline_id": pipeline_id,
        "platform": "抖音",
        "video_title": "企业为什么开始采购 AI 工具",
        "published_at": NOW - timedelta(hours=2),
        "video_url": "https://example.com/video",
        "video_format": "数字人口播",
        "duration_seconds": 48,
        "notes": "首发测试",
        "views": 1000,
        "likes": 80,
        "comments": 12,
        "favorites": 40,
        "shares": 18,
        "followers_gained": 9,
        "completion_rate": 42.5,
        "average_watch_seconds": 28.0,
    }


def test_create_post_saves_initial_snapshot_and_latest_data():
    with make_session() as session:
        pipeline = add_pipeline(session)

        result = create_video_post(
            session,
            post_payload(pipeline.id),
            captured_at=NOW,
        )

        assert result.project_name == "AI项目"
        assert result.views == 1000
        assert result.current_stage == "2小时"
        assert result.current_status == "有潜力"
        assert result.snapshot_count == 1
        assert session.query(VideoPostSnapshot).count() == 1


def test_edit_post_and_multiple_snapshots_preserve_history():
    with make_session() as session:
        pipeline = add_pipeline(session)
        created = create_video_post(
            session,
            post_payload(pipeline.id),
            captured_at=NOW - timedelta(hours=1),
        )

        updated = update_video_post(
            session,
            created.id,
            {"video_title": "更新后标题", "notes": "标题已调整"},
        )
        latest = add_video_snapshot(
            session,
            created.id,
            {
                "views": 1800,
                "likes": 130,
                "comments": 22,
                "favorites": 70,
                "shares": 35,
                "followers_gained": 18,
                "completion_rate": 46.0,
                "average_watch_seconds": 30.0,
                "notes": "2 小时更新",
            },
            captured_at=NOW,
        )
        detail = get_video_post(session, created.id, now=NOW)

        assert updated.video_title == "更新后标题"
        assert latest.views == 1800
        assert detail is not None
        assert detail.snapshot_count == 2
        assert [snapshot.views for snapshot in detail.snapshots] == [1000, 1800]
        assert detail.view_velocity > 0


def test_list_supports_platform_project_status_and_metric_sorting():
    with make_session() as session:
        first_pipeline = add_pipeline(session, "项目A")
        second_pipeline = add_pipeline(session, "项目B")
        first = create_video_post(
            session,
            post_payload(first_pipeline.id),
            captured_at=NOW,
        )
        second_payload = {
            **post_payload(second_pipeline.id),
            "platform": "B站",
            "video_title": "第二条视频",
            "video_url": "https://example.com/video-2",
            "views": 3000,
            "favorites": 20,
            "shares": 8,
        }
        second = create_video_post(session, second_payload, captured_at=NOW)

        platform_result = list_video_posts(
            session,
            platform="B站",
            sort_by="views",
            now=NOW,
        )
        project_result = list_video_posts(
            session,
            pipeline_id=first_pipeline.id,
            now=NOW,
        )
        status_result = list_video_posts(
            session,
            status="有潜力",
            sort_by="favorites",
            now=NOW,
        )

        assert [item.id for item in platform_result.items] == [second.id]
        assert [item.id for item in project_result.items] == [first.id]
        assert status_result.items[0].favorites >= status_result.items[-1].favorites
        assert "抖音" in platform_result.platforms
        assert {project.title for project in platform_result.projects} == {
            "项目A",
            "项目B",
        }


def test_video_tracking_api_is_manual_and_does_not_call_ai():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        pipeline_id = add_pipeline(setup_session).id
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
        create_response = client.post(
            "/api/video-posts",
            json={
                **post_payload(pipeline_id),
                "published_at": (NOW - timedelta(hours=2)).isoformat(),
            },
        )
        post_id = create_response.json()["id"]
        edit_response = client.put(
            f"/api/video-posts/{post_id}",
            json={"video_title": "接口更新标题"},
        )
        snapshot_response = client.post(
            f"/api/video-posts/{post_id}/snapshots",
            json={
                "views": 2200,
                "likes": 160,
                "comments": 30,
                "favorites": 88,
                "shares": 45,
                "followers_gained": 24,
                "completion_rate": 48,
                "average_watch_seconds": 31,
                "notes": "手动更新",
            },
        )
        list_response = client.get(
            "/api/video-posts",
            params={"platform": "抖音", "sort_by": "views"},
        )
        detail_response = client.get(f"/api/video-posts/{post_id}")
    finally:
        app.dependency_overrides.clear()

    assert create_response.status_code == 200
    assert edit_response.status_code == 200
    assert snapshot_response.status_code == 200
    assert list_response.status_code == 200
    assert detail_response.status_code == 200
    assert detail_response.json()["snapshot_count"] == 2
    assert detail_response.json()["snapshots"][-1]["views"] == 2200
    with testing_session_local() as verify_session:
        assert verify_session.query(AiGeneration).count() == generation_count
