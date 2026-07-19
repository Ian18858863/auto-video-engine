from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models import AppConfig, HotItem
from app.topic_ideas.filter_settings import (
    DEFAULT_TOPIC_FILTER_SETTINGS,
    TOPIC_FILTER_SETTINGS_KEY,
    get_topic_filter_settings,
    save_topic_filter_settings,
)
from app.topic_ideas.event_scoring import event_score, score_breakdown_dict


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def add_hotspot(session: Session, *, category: str = "AI", title: str = "AI 搜索改变普通人获取信息方式") -> HotItem:
    item = HotItem(
        source_platform="微博热搜网页版",
        source_type="news",
        title=title,
        url=f"https://example.com/topic-filter-{category}-{title}",
        summary="这件事影响平台流量、广告模式、工具使用和普通人学习工作。",
        tags=["AI", "工具"],
        category=category,
        hot_score=80,
        topic_score=85,
        save_score=78,
        risk_score=0,
        freshness_score=90,
        data_origin="real",
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def test_topic_filter_settings_default_and_save_with_bonus_rules():
    with make_session() as session:
        settings = get_topic_filter_settings(session)

        assert settings["weights"] == DEFAULT_TOPIC_FILTER_SETTINGS["weights"]
        assert settings["current_period_limit"] == 5
        assert settings["backup_pool_limit"] == 10
        assert settings["source_limit"] == 3
        assert settings["bonus_rules"]["categories"]

        updated = get_topic_filter_settings(session)
        updated["weights"] = {
            "impact": 30,
            "account_fit": 20,
            "freshness_heat": 15,
            "cross_source_resonance": 10,
            "ordinary_relevance": 25,
        }
        updated["bonus_rules"]["categories"][0]["score"] = 9
        saved = save_topic_filter_settings(session, updated)

        assert saved["weights"]["impact"] == 30
        assert saved["bonus_rules"]["categories"][0]["score"] == 9
        assert get_topic_filter_settings(session)["weights"]["ordinary_relevance"] == 25


def test_concurrent_default_reads_create_one_settings_row(tmp_path):
    database_path = tmp_path / "topic-filter-settings-race.db"
    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    insert_barrier = Barrier(2)

    class RacingSession(Session):
        pass

    @event.listens_for(RacingSession, "before_flush")
    def synchronize_default_inserts(session, _flush_context, _instances):
        if any(
            isinstance(row, AppConfig) and row.key == TOPIC_FILTER_SETTINGS_KEY
            for row in session.new
        ):
            insert_barrier.wait(timeout=5)

    testing_session_local = sessionmaker(bind=engine, class_=RacingSession)

    def read_settings():
        with testing_session_local() as session:
            return get_topic_filter_settings(session)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(read_settings) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]

        with testing_session_local() as session:
            row_count = (
                session.query(AppConfig)
                .filter(AppConfig.key == TOPIC_FILTER_SETTINGS_KEY)
                .count()
            )
    finally:
        engine.dispose()

    assert [result["weights"] for result in results] == [
        DEFAULT_TOPIC_FILTER_SETTINGS["weights"],
        DEFAULT_TOPIC_FILTER_SETTINGS["weights"],
    ]
    assert row_count == 1


def test_topic_filter_settings_rejects_weight_sum_not_100():
    with make_session() as session:
        payload = get_topic_filter_settings(session)
        payload["weights"]["impact"] = 99

        try:
            save_topic_filter_settings(session, payload)
        except ValueError as exc:
            assert "五个评分权重总和必须等于 100" in str(exc)
        else:
            raise AssertionError("权重总和不等于 100 时应该拒绝保存")


def test_event_score_uses_saved_weights_and_adjustable_bonus_rules():
    with make_session() as session:
        payload = get_topic_filter_settings(session)
        payload["weights"] = {
            "impact": 10,
            "account_fit": 10,
            "freshness_heat": 10,
            "cross_source_resonance": 10,
            "ordinary_relevance": 60,
        }
        payload["bonus_rules"]["categories"] = [
            {"id": "ai_bonus", "label": "AI 重点", "category": "AI", "score": 7, "enabled": True}
        ]
        payload["bonus_rules"]["keywords"] = [
            {"id": "tool_bonus", "label": "工具关键词", "keyword": "工具", "score": 6, "enabled": True}
        ]
        settings = save_topic_filter_settings(session, payload)
        hotspot = add_hotspot(session)

        score = event_score(hotspot, source_count=2, settings=settings)
        breakdown = score_breakdown_dict(score)

        assert breakdown["base_total"] < breakdown["total"]
        assert breakdown["bonus_total"] == 13
        assert score.bonus_reasons == ["AI 重点", "工具关键词"]
        assert score.total <= 100


def test_topic_filter_settings_api_round_trip():
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
        get_response = client.get("/api/topic-filter-settings")
        payload = get_response.json()
        payload["minimum_score"] = 42
        payload["bonus_rules"]["keywords"].append(
            {
                "id": "ordinary_people",
                "label": "普通人关键词",
                "keyword": "普通人",
                "score": 5,
                "enabled": True,
            }
        )
        save_response = client.put("/api/topic-filter-settings", json=payload)
    finally:
        app.dependency_overrides.clear()

    assert get_response.status_code == 200
    assert save_response.status_code == 200
    assert save_response.json()["minimum_score"] == 42
    assert any(
        item["label"] == "普通人关键词"
        for item in save_response.json()["bonus_rules"]["keywords"]
    )
