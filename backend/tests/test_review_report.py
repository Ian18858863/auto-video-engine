from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models import AiGeneration, HotItem, ProductionPipeline
from app.publishing.service import add_video_snapshot, create_video_post
from app.reviews.service import get_video_review, list_video_reviews
from app.reviews.weekly_service import build_weekly_review_report


NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def add_pipeline(session: Session, name: str) -> ProductionPipeline:
    hotspot = HotItem(
        source_platform="手动导入",
        source_type="manual",
        title=f"热点 {name}",
        url=f"https://example.com/{name}",
        published_at=NOW,
        summary="复盘测试。",
        category="AI",
    )
    session.add(hotspot)
    session.flush()
    pipeline = ProductionPipeline(
        hotspot_id=hotspot.id,
        project_name=name,
        status="已完成",
        provider="fake",
        model="fake-low-cost",
    )
    session.add(pipeline)
    session.commit()
    session.refresh(pipeline)
    return pipeline


def create_strong_video(session: Session, pipeline_id: int):
    created = create_video_post(
        session,
        {
            "pipeline_id": pipeline_id,
            "platform": "抖音",
            "video_title": "高收藏工具清单",
            "published_at": NOW - timedelta(hours=26),
            "video_format": "图文混剪",
            "duration_seconds": 50,
            "views": 1000,
            "likes": 90,
            "comments": 20,
            "favorites": 45,
            "shares": 20,
            "followers_gained": 8,
            "completion_rate": 48,
            "average_watch_seconds": 32,
            "notes": "首日表现",
        },
        captured_at=NOW - timedelta(hours=2),
    )
    return add_video_snapshot(
        session,
        created.id,
        {
            "views": 1800,
            "likes": 150,
            "comments": 34,
            "favorites": 90,
            "shares": 45,
            "followers_gained": 20,
            "completion_rate": 52,
            "average_watch_seconds": 35,
            "notes": "24 小时后仍增长",
        },
        captured_at=NOW,
    )


def test_review_calculates_rates_trend_judgement_and_suggestion():
    with make_session() as session:
        pipeline = add_pipeline(session, "AI工具项目")
        post = create_strong_video(session, pipeline.id)

        review = get_video_review(session, post.id, now=NOW)

        assert review is not None
        assert review.favorite_rate == 5.0
        assert review.share_rate == 2.5
        assert review.follower_rate == 1.11
        assert review.growth_trend in {"加速", "长尾增长", "稳定增长"}
        assert review.judgement == "值得继续做"
        assert "继续" in review.next_suggestion
        assert len(review.snapshots) == 2


def test_low_completion_and_low_value_video_needs_adjustment():
    with make_session() as session:
        pipeline = add_pipeline(session, "低完播项目")
        post = create_video_post(
            session,
            {
                "pipeline_id": pipeline.id,
                "platform": "B站",
                "video_title": "需要调整的视频",
                "published_at": NOW - timedelta(hours=30),
                "views": 800,
                "likes": 20,
                "comments": 2,
                "favorites": 4,
                "shares": 1,
                "followers_gained": 0,
                "completion_rate": 18,
                "average_watch_seconds": 8,
            },
            captured_at=NOW,
        )

        review = get_video_review(session, post.id, now=NOW)

        assert review is not None
        assert review.judgement == "需要调整"
        assert any("完播率" in reason for reason in review.reasons)


def test_review_list_filters_by_platform_project_date_and_sorts_rates():
    with make_session() as session:
        first_pipeline = add_pipeline(session, "项目A")
        second_pipeline = add_pipeline(session, "项目B")
        first = create_strong_video(session, first_pipeline.id)
        create_video_post(
            session,
            {
                "pipeline_id": second_pipeline.id,
                "platform": "B站",
                "video_title": "普通视频",
                "published_at": NOW - timedelta(days=5),
                "views": 3000,
                "favorites": 15,
                "shares": 6,
                "followers_gained": 3,
                "completion_rate": 35,
            },
            captured_at=NOW,
        )

        result = list_video_reviews(
            session,
            platform="抖音",
            pipeline_id=first_pipeline.id,
            published_from=NOW - timedelta(days=2),
            sort_by="favorite_rate",
            now=NOW,
        )

        assert [item.video_post_id for item in result.items] == [first.id]
        assert result.items[0].favorite_rate == 5.0
        assert result.platforms == ["B站", "抖音"]
        assert {project.title for project in result.projects} == {"项目A", "项目B"}


def test_review_api_is_read_only_and_does_not_call_ai():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        pipeline = add_pipeline(setup_session, "接口项目")
        post_id = create_strong_video(setup_session, pipeline.id).id
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
        list_response = client.get(
            "/api/reviews",
            params={"platform": "抖音", "sort_by": "share_rate"},
        )
        detail_response = client.get(f"/api/reviews/{post_id}")
    finally:
        app.dependency_overrides.clear()

    assert list_response.status_code == 200
    assert list_response.json()["items"][0]["judgement"] == "值得继续做"
    assert detail_response.status_code == 200
    assert len(detail_response.json()["snapshots"]) == 2
    with testing_session_local() as verify_session:
        assert verify_session.query(AiGeneration).count() == generation_count


def test_weekly_review_report_uses_local_rules_only():
    with make_session() as session:
        pipeline = add_pipeline(session, "周报项目")
        create_strong_video(session, pipeline.id)
        generation_count = session.query(AiGeneration).count()

        report = build_weekly_review_report(session, now=NOW)

        assert report["published_count"] == 1
        assert report["local_rules_only"] is True
        assert report["real_ai_called"] is False
        assert report["data_note"] == "当前发布数据不足，周报仅供参考。"
        assert report["high_favorite_videos"][0]["video_title"] == "高收藏工具清单"
        assert report["next_week_do"]
        assert session.query(AiGeneration).count() == generation_count


def test_empty_weekly_review_report_refuses_to_invent_direction():
    with make_session() as session:
        generation_count = session.query(AiGeneration).count()

        report = build_weekly_review_report(session, now=NOW)

        assert report["published_count"] == 0
        assert report["data_note"] == "最近 7 天没有发布数据，无法判断趋势、因果或下周方向。"
        assert report["published_videos"] == []
        assert report["high_favorite_videos"] == []
        assert report["high_share_videos"] == []
        assert report["long_tail_videos"] == []
        assert report["stalled_videos"] == []
        assert report["continue_topics"] == []
        assert report["pause_topics"] == []
        assert report["next_week_do"] == []
        assert report["next_week_avoid"] == []
        assert report["local_rules_only"] is True
        assert report["real_ai_called"] is False
        assert session.query(AiGeneration).count() == generation_count


def test_weekly_review_report_keeps_selected_project_scope_empty():
    with make_session() as session:
        empty_pipeline = add_pipeline(session, "没有发布数据的项目")
        populated_pipeline = add_pipeline(session, "已有发布数据的项目")
        create_strong_video(session, populated_pipeline.id)

        empty_report = build_weekly_review_report(
            session,
            now=NOW,
            pipeline_id=empty_pipeline.id,
        )
        populated_report = build_weekly_review_report(
            session,
            now=NOW,
            pipeline_id=populated_pipeline.id,
        )

        assert empty_report["published_count"] == 0
        assert empty_report["published_videos"] == []
        assert empty_report["next_week_do"] == []
        assert empty_report["next_week_avoid"] == []
        assert populated_report["published_count"] == 1
        assert {
            item["pipeline_id"] for item in populated_report["published_videos"]
        } == {populated_pipeline.id}


def test_weekly_review_api_honors_pipeline_scope():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        empty_pipeline = add_pipeline(setup_session, "接口空项目")
        populated_pipeline = add_pipeline(setup_session, "接口有数据项目")
        api_now = datetime.now(timezone.utc)
        create_video_post(
            setup_session,
            {
                "pipeline_id": populated_pipeline.id,
                "platform": "抖音",
                "video_title": "接口范围验证视频",
                "published_at": api_now - timedelta(hours=2),
                "views": 500,
                "favorites": 20,
                "shares": 8,
                "completion_rate": 45,
            },
            captured_at=api_now,
        )
        empty_pipeline_id = empty_pipeline.id
        populated_pipeline_id = populated_pipeline.id
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
        empty_response = client.get(
            "/api/reviews/weekly/report",
            params={"pipeline_id": empty_pipeline_id},
        )
        populated_response = client.get(
            "/api/reviews/weekly/report",
            params={"pipeline_id": populated_pipeline_id},
        )
    finally:
        app.dependency_overrides.clear()

    assert empty_response.status_code == 200
    assert empty_response.json()["published_count"] == 0
    assert empty_response.json()["next_week_do"] == []
    assert populated_response.status_code == 200
    assert populated_response.json()["published_count"] == 1
    assert {
        item["pipeline_id"] for item in populated_response.json()["published_videos"]
    } == {populated_pipeline_id}
    with testing_session_local() as verify_session:
        assert verify_session.query(AiGeneration).count() == generation_count
