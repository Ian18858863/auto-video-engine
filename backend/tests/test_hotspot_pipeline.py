from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.hotspots.config import load_source_config
from app.hotspots.pipeline import HotspotPipeline
from app.hotspots.schemas import HotspotCandidate
from app.hotspots.scoring import score_hotspot
from app.hotspots.sources.base import HotspotSource
from app.hotspots.sources.errors import RateLimitedSourceError
from app.hotspots.sources.http_client import public_get
from app.hotspots.sources.reddit import RedditSource
from app.models import HotItem, SourceRun


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def test_manual_hotspot_import_saves_hot_item():
    with make_session() as session:
        pipeline = HotspotPipeline(session)
        item, created = pipeline.import_manual(
            HotspotCandidate(
                source_platform="手动导入",
                source_type="manual",
                title="一个 AI 工具开始进入企业采购流程",
                url="https://example.com/manual-ai-tool",
                summary="这条适合拆企业为什么愿意为 AI 工具付费。",
                published_at=datetime.now(timezone.utc),
                tags=["AI", "工具", "商业"],
                category="AI",
            )
        )

        assert created is True
        assert item.id is not None
        assert session.query(HotItem).count() == 1
        assert item.hot_score > 0
        assert item.topic_score > 0
        assert item.save_score > 0


def test_duplicate_url_is_not_saved_twice():
    with make_session() as session:
        pipeline = HotspotPipeline(session)
        candidate = HotspotCandidate(
            source_platform="手动导入",
            source_type="manual",
            title="同一个新工具热点",
            url="https://example.com/same-tool",
        )

        first_item, first_created = pipeline.import_manual(candidate)
        second_item, second_created = pipeline.import_manual(candidate)

        assert first_created is True
        assert second_created is False
        assert second_item.id == first_item.id
        assert session.query(HotItem).count() == 1
        assert session.get(HotItem, first_item.id).duplicate_reason


def test_manual_hotspots_without_url_are_not_deduplicated_by_blank_url():
    with make_session() as session:
        pipeline = HotspotPipeline(session)

        first_item, first_created = pipeline.import_manual(
            HotspotCandidate(
                source_platform="手动导入",
                source_type="manual",
                title="AI 搜索改变普通人获取信息",
                url="",
                category="AI",
            )
        )
        second_item, second_created = pipeline.import_manual(
            HotspotCandidate(
                source_platform="手动导入",
                source_type="manual",
                title="本地生活工具开始重做获客方式",
                url="",
                category="商业",
            )
        )

        assert first_created is True
        assert second_created is True
        assert second_item.id != first_item.id
        assert session.query(HotItem).count() == 2


def test_similar_title_is_merged_and_reason_is_saved():
    with make_session() as session:
        pipeline = HotspotPipeline(session)
        first_item, first_created = pipeline.import_manual(
            HotspotCandidate(
                source_platform="Google News",
                source_type="news",
                title="New AI agent changes how small teams work",
                url="https://example.com/ai-agent-a",
                category="AI",
            )
        )
        second_item, second_created = pipeline.import_manual(
            HotspotCandidate(
                source_platform="TechCrunch",
                source_type="news",
                title="New AI agent changes how small teams work!",
                url="https://example.com/ai-agent-b",
                category="AI",
            )
        )

        assert first_created is True
        assert second_created is False
        assert second_item.id == first_item.id
        assert "相似标题合并" in session.get(HotItem, first_item.id).duplicate_reason


def test_sensitive_keyword_is_filtered_and_reason_is_saved():
    with make_session() as session:
        pipeline = HotspotPipeline(session)
        item, _ = pipeline.import_manual(
            HotspotCandidate(
                source_platform="手动导入",
                source_type="manual",
                title="赌博平台引流教程",
                url="https://example.com/bad-topic",
                summary="教人怎么规避平台审核。",
            )
        )

        assert item.is_filtered is True
        assert "赌博" in item.filter_reason


def test_scoring_returns_reasonable_scores_for_valuable_hotspot():
    candidate = HotspotCandidate(
        source_platform="Hacker News",
        source_type="community",
        title="New AI coding agent changes how small teams ship products",
        url="https://example.com/hn-ai-agent",
        summary="A new AI coding workflow is getting strong developer discussion.",
        published_at=datetime.now(timezone.utc) - timedelta(hours=2),
        metrics={"rank": 1, "upvotes": 520, "comments": 140},
        tags=["AI", "developer tools", "productivity"],
        category="AI",
    )

    scores = score_hotspot(candidate)

    assert 0 <= scores.hot_score <= 100
    assert 0 <= scores.topic_score <= 100
    assert 0 <= scores.save_score <= 100
    assert scores.hot_score >= 70
    assert scores.topic_score >= 70
    assert scores.save_score >= 50


class BrokenSource(HotspotSource):
    name = "broken-source"

    def fetch(self) -> list[HotspotCandidate]:
        raise RuntimeError("source is down")


def test_source_failure_is_logged_without_crashing_pipeline():
    with make_session() as session:
        pipeline = HotspotPipeline(session)

        result = pipeline.run_sources([BrokenSource()])

        source_run = session.query(SourceRun).one()
        assert result.saved_count == 0
        assert result.failed_sources == ["broken-source"]
        assert source_run.source_name == "broken-source"
        assert source_run.success is False
        assert "source is down" in source_run.error_message


class RateLimitedSource(HotspotSource):
    name = "reddit-limited"

    def fetch(self) -> list[HotspotCandidate]:
        raise RateLimitedSourceError("HTTP 429")


def test_source_rate_limit_is_logged_as_limited_not_system_failure():
    with make_session() as session:
        pipeline = HotspotPipeline(session)

        result = pipeline.run_sources([RateLimitedSource()])

        source_run = session.query(SourceRun).one()
        assert result.failed_sources == ["reddit-limited"]
        assert result.rate_limited_sources == ["reddit-limited"]
        assert source_run.status == "限流"
        assert source_run.is_rate_limited is True
        assert "HTTP 429" in source_run.error_message


def test_source_config_loads_rss_sources_from_file(tmp_path):
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(
        """
rss_sources:
  - name: TechCrunch
    url: https://techcrunch.com/feed/
    enabled: true
    limit: 12
    timeout: 4
    retries: 2
google_news_keywords:
  - AI
reddit_subreddits:
  - technology
""",
        encoding="utf-8",
    )

    config = load_source_config(config_path)

    assert config.rss_sources[0].name == "TechCrunch"
    assert config.rss_sources[0].url == "https://techcrunch.com/feed/"
    assert config.rss_sources[0].limit == 12
    assert config.rss_sources[0].timeout == 4
    assert config.rss_sources[0].retries == 2
    assert config.google_news_keywords == ["AI"]
    assert config.reddit_subreddits == ["technology"]


def test_public_get_retries_transient_server_errors(monkeypatch):
    calls: list[str] = []
    responses = [
        httpx.Response(503, request=httpx.Request("GET", "https://example.com/rss")),
        httpx.Response(200, text="ok", request=httpx.Request("GET", "https://example.com/rss")),
    ]

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, params=None):
            calls.append(url)
            return responses.pop(0)

    monkeypatch.setattr("app.hotspots.sources.http_client.httpx.Client", FakeClient)

    response = public_get("https://example.com/rss", label="测试 RSS", retries=1)

    assert response.status_code == 200
    assert calls == ["https://example.com/rss", "https://example.com/rss"]


def test_public_get_reports_429_as_rate_limited(monkeypatch):
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, params=None):
            return httpx.Response(
                429,
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr("app.hotspots.sources.http_client.httpx.Client", FakeClient)

    with pytest.raises(RateLimitedSourceError, match="HTTP 429"):
        public_get("https://example.com/rss", label="测试 RSS", retries=1)


def test_reddit_source_falls_back_to_rss_when_json_is_blocked(monkeypatch):
    source = RedditSource("technology", limit=1)
    fallback_candidate = HotspotCandidate(
        source_platform="Reddit",
        source_type="community",
        title="AI infrastructure discussion",
        url="https://www.reddit.com/r/technology/comments/demo",
        tags=["technology"],
        category="科技",
    )

    def fail_json():
        raise RuntimeError("json blocked")

    monkeypatch.setattr(source, "_fetch_json", fail_json)
    monkeypatch.setattr(source, "_fetch_rss", lambda: [fallback_candidate])

    candidates = source.fetch()

    assert candidates == [fallback_candidate]


def test_reddit_source_reports_both_json_and_rss_failures(monkeypatch):
    source = RedditSource("technology", limit=1)
    monkeypatch.setattr(source, "_fetch_json", lambda: (_ for _ in ()).throw(RuntimeError("json blocked")))
    monkeypatch.setattr(source, "_fetch_rss", lambda: (_ for _ in ()).throw(RuntimeError("rss blocked")))

    with pytest.raises(RuntimeError, match="JSON 失败.*RSS 失败"):
        source.fetch()


def test_reddit_source_reports_429_as_rate_limited(monkeypatch):
    source = RedditSource("technology", limit=1)
    monkeypatch.setattr(
        source,
        "_fetch_json",
        lambda: (_ for _ in ()).throw(RateLimitedSourceError("json 429")),
    )
    monkeypatch.setattr(
        source,
        "_fetch_rss",
        lambda: (_ for _ in ()).throw(RuntimeError("rss blocked")),
    )

    with pytest.raises(RateLimitedSourceError, match="被限流"):
        source.fetch()
