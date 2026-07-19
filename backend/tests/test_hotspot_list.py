from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.hotspots.schemas import HotspotCandidate
from app.hotspots.list_service import get_hotspot, list_hotspots
from app.main import app
from app.models import AiGeneration, HotItem, ProductionPipeline


_UNSET = object()


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def add_hotspot(
    session: Session,
    *,
    suffix: str,
    source: str,
    category: str,
    hot_score: float,
    topic_score: float,
    save_score: float,
    is_filtered: bool = False,
    url: str | None = None,
    published_at: datetime | None | object = _UNSET,
    summary: str | None = None,
) -> HotItem:
    item = HotItem(
        source_platform=source,
        source_type="news",
        title=f"热点 {suffix}",
        url=f"https://example.com/hotspot-{suffix}" if url is None else url,
        published_at=(
            datetime.now(timezone.utc) - timedelta(hours=1)
            if published_at is _UNSET
            else published_at
        ),
        summary=f"热点 {suffix} 的简短摘要。" if summary is None else summary,
        metrics={"rank": 1},
        tags=[category, "趋势"],
        category=category,
        is_filtered=is_filtered,
        filter_reason="敏感内容" if is_filtered else "",
        hot_score=hot_score,
        topic_score=topic_score,
        save_score=save_score,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def test_list_hotspots_supports_filters_sorting_and_project_status():
    with make_session() as session:
        ai_item = add_hotspot(
            session,
            suffix="AI",
            source="Hacker News",
            category="AI",
            hot_score=80,
            topic_score=95,
            save_score=88,
        )
        add_hotspot(
            session,
            suffix="消费",
            source="Google News",
            category="消费",
            hot_score=99,
            topic_score=70,
            save_score=92,
        )
        add_hotspot(
            session,
            suffix="过滤",
            source="Reddit",
            category="其他",
            hot_score=60,
            topic_score=20,
            save_score=10,
            is_filtered=True,
        )
        session.add(
            ProductionPipeline(
                hotspot_id=ai_item.id,
                project_name="AI 项目",
                status="已完成",
                provider="fake",
                model="fake-low-cost",
            )
        )
        session.commit()
        generation_count = session.query(AiGeneration).count()

        result = list_hotspots(
            session,
            category="AI",
            source_platform="Hacker News",
            unfiltered_only=True,
            sort_by="topic_score",
        )

        assert result.total == 1
        assert result.items[0].id == ai_item.id
        assert result.items[0].has_project is True
        assert result.categories == ["社会", "财经", "科技", "消费", "AI", "国际", "生活", "娱乐"]
        assert result.category_counts["AI"] == 1
        assert result.category_counts["消费"] == 0
        assert result.sources == ["Hacker News"]
        assert result.source_counts == {"Hacker News": 1}
        assert session.query(AiGeneration).count() == generation_count


def test_list_hotspots_uses_canonical_categories_and_counts_for_today_events():
    with make_session() as session:
        add_hotspot(
            session,
            suffix="社会",
            source="微博热搜网页版",
            category="其他",
            hot_score=78,
            topic_score=86,
            save_score=72,
            summary="就业和公共服务规则出现变化，影响普通人生活。",
        )
        add_hotspot(
            session,
            suffix="商业",
            source="36氪",
            category="商业",
            hot_score=75,
            topic_score=88,
            save_score=74,
        )
        add_hotspot(
            session,
            suffix="游戏",
            source="B站热门网页版",
            category="游戏",
            hot_score=70,
            topic_score=62,
            save_score=58,
        )

        all_result = list_hotspots(session, page_size=10)
        social_result = list_hotspots(session, category="社会", page_size=10)
        finance_result = list_hotspots(session, category="财经", page_size=10)
        entertainment_result = list_hotspots(session, category="娱乐", page_size=10)

        assert all_result.categories == ["社会", "财经", "科技", "消费", "AI", "国际", "生活", "娱乐"]
        assert all_result.category_counts["社会"] == 1
        assert all_result.category_counts["财经"] == 1
        assert all_result.category_counts["娱乐"] == 1
        assert social_result.total == 1
        assert social_result.items[0].source_platform == "微博热搜网页版"
        assert finance_result.total == 1
        assert finance_result.items[0].source_platform == "36氪"
        assert entertainment_result.total == 1
        assert entertainment_result.items[0].source_platform == "B站热门网页版"
        assert social_result.source_counts == {"微博热搜网页版": 1}


def test_list_hotspots_balances_sources_so_browser_hotspots_are_visible():
    with make_session() as session:
        for index in range(20):
            add_hotspot(
                session,
                suffix=f"IT{index}",
                source="IT之家",
                category="科技",
                hot_score=96 - index * 0.1,
                topic_score=96 - index * 0.1,
                save_score=90,
            )
        browser_item = add_hotspot(
            session,
            suffix="微博浏览器",
            source="微博热搜网页版",
            category="社会",
            hot_score=45,
            topic_score=35,
            save_score=30,
        )

        result = list_hotspots(session, sort_by="topic_score", page_size=10)
        sources = [item.source_platform for item in result.items]

        assert browser_item.id in {item.id for item in result.items}
        assert sources.count("IT之家") < 10


def test_list_hotspots_sorts_by_each_score():
    with make_session() as session:
        add_hotspot(
            session,
            suffix="A",
            source="RSS",
            category="科技",
            hot_score=90,
            topic_score=60,
            save_score=70,
        )
        add_hotspot(
            session,
            suffix="B",
            source="RSS",
            category="科技",
            hot_score=70,
            topic_score=95,
            save_score=80,
        )
        add_hotspot(
            session,
            suffix="C",
            source="RSS",
            category="科技",
            hot_score=60,
            topic_score=80,
            save_score=99,
        )

        assert list_hotspots(session, sort_by="hot_score").items[0].title == "热点 A"
        assert list_hotspots(session, sort_by="topic_score").items[0].title == "热点 B"
        assert list_hotspots(session, sort_by="save_score").items[0].title == "热点 C"


def test_get_hotspot_returns_detail_and_missing_is_none():
    with make_session() as session:
        item = add_hotspot(
            session,
            suffix="详情",
            source="RSS",
            category="商业",
            hot_score=88,
            topic_score=91,
            save_score=86,
        )

        detail = get_hotspot(session, item.id)

        assert detail is not None
        assert detail.title == "热点 详情"
        assert detail.original_title == "热点 详情"
        assert detail.original_url == "https://example.com/hotspot-详情"
        assert detail.source_credibility == "高"
        assert detail.metrics == {"rank": 1}
        assert get_hotspot(session, 999) is None


def test_hotspot_source_credibility_marks_low_when_missing_original_fields():
    with make_session() as session:
        item = add_hotspot(
            session,
            suffix="低可信",
            source="RSS",
            category="科技",
            hot_score=70,
            topic_score=70,
            save_score=70,
            url="",
            published_at=None,
            summary="",
        )

        detail = get_hotspot(session, item.id)

        assert detail is not None
        assert detail.source_credibility == "低"
        assert "缺少可验证" in detail.source_credibility_reason


def test_hotspot_list_and_detail_api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        item = add_hotspot(
            setup_session,
            suffix="接口",
            source="Google News",
            category="财经",
            hot_score=82,
            topic_score=89,
            save_score=85,
        )
        item_id = item.id

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
            "/api/hotspots",
            params={
                "category": "财经",
                "source_platform": "Google News",
                "unfiltered_only": True,
                "sort_by": "save_score",
            },
        )
        detail_response = client.get(f"/api/hotspots/{item_id}")
        missing_response = client.get("/api/hotspots/999")
    finally:
        app.dependency_overrides.clear()

    assert list_response.status_code == 200
    assert list_response.json()["items"][0]["title"] == "热点 接口"
    assert list_response.json()["items"][0]["original_url"]
    assert list_response.json()["items"][0]["source_credibility"] == "高"
    assert detail_response.status_code == 200
    assert detail_response.json()["id"] == item_id
    assert detail_response.json()["original_title"] == "热点 接口"
    assert missing_response.status_code == 404


def test_manual_hotspot_import_api_saves_and_deduplicates():
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
        client = TestClient(app)
        payload = {
            "title": "AI 搜索正在改变普通人获取信息的方式",
            "source_platform": "手动导入",
            "url": "https://example.com/manual-hotspot",
            "summary": "用于测试手动热点进入热点池。",
            "category": "AI",
            "tags": ["AI", "搜索"],
            "notes": "示例备注",
        }
        first = client.post("/api/hotspots/import", json=payload)
        second = client.post("/api/hotspots/import", json=payload)
        list_response = client.get("/api/hotspots", params={"source_platform": "手动导入"})
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert first.json()["created"] is True
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert list_response.json()["items"][0]["title"] == payload["title"]


def test_manual_hotspot_import_api_accepts_missing_url():
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
        client = TestClient(app)
        response = client.post(
            "/api/hotspots/import",
            json={
                "title": "AI 搜索正在改变普通人获取信息的方式",
                "source_platform": "手动导入",
                "summary": "链接暂时没有，也应该能先保存。",
                "category": "AI",
                "tags": ["AI", "搜索"],
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["created"] is True
    assert response.json()["item"]["title"] == "AI 搜索正在改变普通人获取信息的方式"
    assert response.json()["item"]["url"] == ""


class WorkingSource:
    name = "测试 RSS"

    def fetch(self):
        return [
            HotspotCandidate(
                source_platform="测试 RSS",
                source_type="news",
                title="AI 工具进入企业采购",
                url="https://example.com/source-ok",
                summary="公开来源测试数据。",
                tags=["AI"],
                category="AI",
            )
        ]


class FailingSource:
    name = "失败来源"

    def fetch(self):
        raise RuntimeError("source down")


def test_fetch_hotspot_sources_api_records_failures(monkeypatch):
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

    def fake_build_sources(*_args, **_kwargs):
        return [WorkingSource(), FailingSource()]

    monkeypatch.setattr("app.main.build_hotspot_sources", fake_build_sources)
    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post("/api/hotspots/fetch", json={})
    finally:
        app.dependency_overrides.clear()

    data = response.json()
    assert response.status_code == 200
    assert data["source_count"] == 2
    assert data["saved_count"] == 1
    assert data["failed_sources"] == ["失败来源"]
