from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.factory import build_provider
from app.ai.schemas import AIProviderConfig
from app.core.database import Base, get_db
from app.main import app
from app.models import AiGeneration, HotItem, TopicIdea
from app.topic_ideas.service import (
    FilteredHotspotError,
    HotspotNotFoundError,
    generate_topic_ideas_for_hotspot,
)


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def add_hot_item(session: Session, *, is_filtered: bool = False) -> HotItem:
    item = HotItem(
        source_platform="手动导入",
        source_type="manual",
        title="AI 工具开始进入企业采购流程",
        url="https://example.com/ai-buying",
        published_at=datetime.now(timezone.utc),
        summary="越来越多企业不再只是试用 AI 工具，而是开始把它放进真实预算。",
        metrics={"rank": 1},
        tags=["AI", "工具", "商业"],
        category="AI",
        is_filtered=is_filtered,
        filter_reason="赌博:赌博" if is_filtered else "",
        hot_score=86,
        topic_score=91,
        save_score=78,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def test_generate_topic_ideas_for_hotspot_saves_candidates():
    with make_session() as session:
        hot_item = add_hot_item(session)
        provider = build_provider(AIProviderConfig(provider="fake", model="fake-low-cost"))

        result = generate_topic_ideas_for_hotspot(session, hot_item.id, provider=provider)

        assert result.from_cache is False
        assert len(result.ideas) == 4
        assert session.query(TopicIdea).count() == 4
        assert session.get(HotItem, hot_item.id).has_topic_ideas is True
        first = result.ideas[0]
        assert first.hot_item_id == hot_item.id
        assert first.title
        assert first.core_view
        assert first.recommended_format in {"数字人口播", "动画讲解", "图文混剪", "录屏演示"}
        assert first.why_watch
        assert first.why_save
        assert first.comment_trigger
        assert first.risk_note
        assert first.recommended_duration
        assert first.hook.startswith("为什么")


def test_second_generation_uses_ai_cache_and_does_not_duplicate_rows():
    with make_session() as session:
        hot_item = add_hot_item(session)
        provider = build_provider(AIProviderConfig(provider="fake", model="fake-low-cost"))

        first = generate_topic_ideas_for_hotspot(session, hot_item.id, provider=provider)
        second = generate_topic_ideas_for_hotspot(session, hot_item.id, provider=provider)

        assert first.from_cache is False
        assert second.from_cache is True
        assert provider.call_count == 1
        assert session.query(AiGeneration).count() == 1
        assert session.query(TopicIdea).count() == 4


def test_filtered_hotspot_cannot_generate_topic_ideas():
    with make_session() as session:
        hot_item = add_hot_item(session, is_filtered=True)

        with pytest.raises(FilteredHotspotError, match="不建议生成选题"):
            generate_topic_ideas_for_hotspot(session, hot_item.id)


def test_missing_hotspot_returns_clear_service_error():
    with make_session() as session:
        with pytest.raises(HotspotNotFoundError, match="热点不存在"):
            generate_topic_ideas_for_hotspot(session, 999)


def test_generate_topic_ideas_api_endpoint_returns_candidates():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine)

    with TestingSessionLocal() as setup_session:
        hot_item = add_hot_item(setup_session)
        hot_item_id = hot_item.id

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(f"/api/hotspots/{hot_item_id}/topic-ideas/generate")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["hotspot_id"] == hot_item_id
    assert data["from_cache"] is False
    assert len(data["ideas"]) == 4
    assert data["ideas"][0]["title"]


def test_generate_topic_ideas_api_returns_404_for_missing_hotspot():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post("/api/hotspots/999/topic-ideas/generate")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert "热点不存在" in response.json()["detail"]
