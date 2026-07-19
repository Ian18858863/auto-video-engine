from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.automation.hotspot_service import (
    list_source_run_statuses,
    run_auto_hotspot_intake,
    run_daily_hotspot_refresh,
    should_run_daily_hotspot_refresh,
)
from app.core.config import get_settings
from app.core.database import Base, get_db
from app.hotspots.schemas import HotspotCandidate
from app.hotspots.sources.base import HotspotSource
from app.main import app
from app.models import AiGeneration, AppConfig, HotItem, SourceRun, TopicIdea


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


class StaticSource(HotspotSource):
    name = "RSS Demo"

    def fetch(self) -> list[HotspotCandidate]:
        return [
            HotspotCandidate(
                source_platform="RSS Demo",
                source_type="rss",
                title="AI Agent workflow enters daily operations",
                url="https://example.com/rss-demo-agent",
                published_at=datetime.now(timezone.utc),
                summary="Teams are testing AI agents in recurring daily workflows.",
                tags=["AI", "agent"],
                category="AI",
            )
        ]


class BrokenSource(HotspotSource):
    name = "Reddit Demo"

    def fetch(self) -> list[HotspotCandidate]:
        raise RuntimeError("reddit temporarily unavailable")


def test_auto_hotspot_intake_isolates_source_failures_and_generates_topics(tmp_path):
    with make_session() as session:
        result = run_auto_hotspot_intake(
            session,
            tmp_path / "missing-source-config.yaml",
            force=True,
            sources=[StaticSource(), BrokenSource()],
        )

        assert result.source_count == 2
        assert result.saved_count == 1
        assert result.failed_sources == ["Reddit Demo"]
        assert result.generated_topic_idea_count == 1
        assert result.provider == "local_rule"
        assert result.real_ai_called is False
        assert session.query(HotItem).count() == 1
        assert session.query(HotItem).one().has_topic_ideas is True
        assert session.query(TopicIdea).count() == 1
        assert session.query(AiGeneration).count() == 0
        assert session.query(TopicIdea).one().generation_source == "local_rule"

        statuses = {item.source_name: item for item in result.source_statuses}
        assert statuses["RSS Demo"].status == "成功"
        assert statuses["RSS Demo"].items_fetched == 1
        assert statuses["RSS Demo"].success_count == 1
        assert statuses["Reddit Demo"].status == "失败"
        assert "reddit temporarily unavailable" in statuses["Reddit Demo"].failure_reason


def test_auto_hotspot_intake_skips_source_already_successful_today(tmp_path):
    with make_session() as session:
        session.add(
            SourceRun(
                source_name="RSS Demo",
                started_at=datetime.now(timezone.utc) - timedelta(minutes=20),
                ended_at=datetime.now(timezone.utc) - timedelta(minutes=19),
                success=True,
                items_fetched=3,
            )
        )
        session.commit()

        result = run_auto_hotspot_intake(
            session,
            tmp_path / "missing-source-config.yaml",
            sources=[StaticSource()],
        )

        assert result.skipped_count == 1
        assert result.fetched_count == 0
        assert result.source_statuses[0].status == "跳过"
        assert "今日已抓取" in result.source_statuses[0].failure_reason


def test_daily_hotspot_refresh_runs_after_915_and_skips_after_done(tmp_path):
    before_time = datetime(2026, 6, 19, 9, 10, tzinfo=timezone(timedelta(hours=8)))
    run_time = datetime(2026, 6, 19, 9, 16, tzinfo=timezone(timedelta(hours=8)))
    with make_session() as session:
        assert should_run_daily_hotspot_refresh(session, now=before_time) is False
        assert should_run_daily_hotspot_refresh(session, now=run_time) is True

        result = run_daily_hotspot_refresh(
            session,
            tmp_path / "missing-source-config.yaml",
            sources=[StaticSource()],
            now=run_time,
        )

        assert result.source_count == 1
        assert result.saved_count == 1
        assert result.generated_topic_idea_count == 1
        state = (
            session.query(AppConfig)
            .filter(AppConfig.key == "auto_hotspot_intake_state")
            .one()
            .value
        )
        assert state["daily_refresh_date"] == "2026-06-19"
        assert should_run_daily_hotspot_refresh(
            session,
            now=run_time + timedelta(hours=1),
        ) is False

        skipped = run_daily_hotspot_refresh(
            session,
            tmp_path / "missing-source-config.yaml",
            sources=[StaticSource()],
            now=run_time + timedelta(hours=1),
        )
        assert skipped.skipped_count == 1
        assert skipped.fetched_count == 0


def test_source_status_lists_configured_sources_without_network(tmp_path):
    with make_session() as session:
        statuses = list_source_run_statuses(
            session,
            tmp_path / "missing-source-config.yaml",
            sources=[StaticSource()],
        )

        assert statuses[0].source_name == "RSS Demo"
        assert statuses[0].status == "跳过"
        assert statuses[0].failure_reason == "尚未抓取"


def test_auto_hotspot_api_returns_source_statuses_without_real_ai(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    config_path = Path(tmp_path / "sources.yaml")
    config_path.write_text("reddit:\n  enabled: false\nhacker_news:\n  enabled: false\n", encoding="utf-8")

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setenv("SOURCE_CONFIG_PATH", str(config_path))
    get_settings.cache_clear()
    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post("/api/automation/hotspots/run", json={"force": True})
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert response.status_code == 200
    data = response.json()
    assert data["source_count"] == 0
    assert data["real_ai_called"] is False
    assert data["manual_import_is_fallback"] is True


def test_source_status_api_includes_browser_and_bilibili_sources(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    config_path = Path(tmp_path / "sources.yaml")
    config_path.write_text(
        """
rss_sources: []
china_public_sources:
  - provider: bilibili_ranking
    name: B站科技区排行
    enabled: true
    rid: 188
    limit: 5
browser_hotspot_sources:
  - provider: weibo_web
    name: 微博热搜网页版
    url: https://s.weibo.com/top/summary
    enabled: true
  - provider: douyin_web
    name: 抖音热点网页版
    url: https://www.douyin.com/hot
    enabled: true
  - provider: xiaohongshu_web
    name: 小红书公开发现页
    url: https://www.xiaohongshu.com/explore
    enabled: true
hacker_news:
  enabled: false
reddit:
  enabled: false
""".strip(),
        encoding="utf-8",
    )

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setenv("SOURCE_CONFIG_PATH", str(config_path))
    get_settings.cache_clear()
    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.get("/api/automation/sources/status")
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert response.status_code == 200
    names = {item["source_name"] for item in response.json()["items"]}
    assert "B站科技区排行" in names
    assert "微博热搜网页版" in names
    assert "抖音热点网页版" in names
    assert "小红书公开发现页" in names
