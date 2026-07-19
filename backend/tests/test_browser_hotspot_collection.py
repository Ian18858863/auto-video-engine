from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.hotspots.browser_collection import (
    BrowserHotspotLoginRequired,
    BrowserHotspotRecord,
    BrowserHotspotSourceConfig,
    run_browser_hotspot_collection,
)
from app.models import HotItem, SourceRun


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


class FakeBrowserCollector:
    def __init__(self, records_by_source: dict[str, list[BrowserHotspotRecord]]):
        self.records_by_source = records_by_source
        self.calls: list[str] = []

    def collect(self, source: BrowserHotspotSourceConfig) -> list[BrowserHotspotRecord]:
        self.calls.append(source.name)
        return self.records_by_source[source.name]


class LoginExpiredCollector:
    def collect(self, source: BrowserHotspotSourceConfig) -> list[BrowserHotspotRecord]:
        raise BrowserHotspotLoginRequired("登录态过期，请重新登录后再采集。")


def test_browser_hotspot_collection_exports_csv_dedupes_and_imports_hotspots(tmp_path: Path):
    csv_path = tmp_path / "hotspots.csv"
    source = BrowserHotspotSourceConfig(
        provider="weibo_web",
        name="微博热搜网页版",
        url="https://s.weibo.com/top/summary",
        category="社会",
        tags=["微博", "热搜"],
        limit=5,
    )
    collector = FakeBrowserCollector(
        {
            "微博热搜网页版": [
                BrowserHotspotRecord(
                    source_platform="微博热搜网页版",
                    title="AI 搜索正在改变普通人获取信息的方式",
                    link="https://weibo.com/hot/ai-search",
                    author="微博用户",
                    published_at="",
                    likes=1200,
                    comments=88,
                    collected_at=datetime(2026, 6, 21, 9, 15, tzinfo=timezone.utc),
                ),
                BrowserHotspotRecord(
                    source_platform="微博热搜网页版",
                    title="消费趋势出现新变化",
                    link="https://weibo.com/hot/consumer",
                    author="微博用户",
                    published_at="",
                    likes=300,
                    comments=20,
                    collected_at=datetime(2026, 6, 21, 9, 15, tzinfo=timezone.utc),
                ),
            ]
        }
    )

    with make_session() as session:
        first = run_browser_hotspot_collection(
            session,
            sources=[source],
            collector=collector,
            csv_path=csv_path,
            force=True,
        )
        second = run_browser_hotspot_collection(
            session,
            sources=[source],
            collector=collector,
            csv_path=csv_path,
            force=True,
        )

        rows = _read_csv_rows(csv_path)
        assert first.saved_count == 2
        assert second.saved_count == 0
        assert second.duplicate_count == 2
        assert session.query(HotItem).count() == 2
        assert len(rows) == 2
        assert rows[0]["标题"] == "AI 搜索正在改变普通人获取信息的方式"
        assert rows[0]["来源平台"] == "微博热搜网页版"


def test_browser_hotspot_collection_login_expired_records_source_run(tmp_path: Path):
    source = BrowserHotspotSourceConfig(
        provider="xiaohongshu_web",
        name="小红书热点网页版",
        url="https://www.xiaohongshu.com/explore",
        category="生活",
        tags=["小红书", "趋势"],
        limit=5,
    )

    with make_session() as session:
        result = run_browser_hotspot_collection(
            session,
            sources=[source],
            collector=LoginExpiredCollector(),
            csv_path=tmp_path / "hotspots.csv",
            force=True,
        )

        assert result.failed_sources == ["小红书热点网页版"]
        assert result.needs_login_sources == ["小红书热点网页版"]
        assert result.saved_count == 0
        source_run = session.query(SourceRun).one()
        assert source_run.status == "需要重新登录"
        assert "重新登录" in source_run.error_message


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))

