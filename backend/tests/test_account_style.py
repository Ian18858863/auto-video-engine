from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.account_style.service import (
    get_account_style_context,
    get_account_style_library,
    reset_account_style_library,
    save_account_style_library,
)
from app.ai.factory import build_provider
from app.ai.schemas import AIProviderConfig
from app.core.database import Base, get_db
from app.main import app
from app.models import AiGeneration, AppConfig, HotItem, TopicIdea
from app.pipeline.script_service import generate_script_for_topic
from app.topic_ideas.service import generate_topic_ideas_for_hotspot


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def add_hot_item(session: Session) -> HotItem:
    item = HotItem(
        source_platform="手动导入",
        source_type="manual",
        title="企业开始采购 AI 工具",
        url="https://example.com/account-style-hotspot",
        published_at=datetime.now(timezone.utc),
        summary="AI 工具从试用进入预算。",
        tags=["AI", "商业"],
        category="AI",
        hot_score=80,
        topic_score=90,
        save_score=85,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def test_account_style_returns_default_library():
    with make_session() as session:
        style = get_account_style_library(session)

        assert style["account_positioning"].startswith("用大白话")
        assert "未解释的专业财经术语" in [
            item["label"] for item in style["banned_expressions"]
        ]
        assert "大家都知道" in style["validation_rules"]["default_common_knowledge_phrases"]
        assert style["is_active"] is True
        assert style["using_default"] is True
        assert (
            session.query(AppConfig)
            .filter(AppConfig.key == "account_style_library")
            .count()
            == 1
        )
        assert get_account_style_library(session)["using_default"] is True


def test_account_style_can_save_items_and_reset_defaults():
    with make_session() as session:
        style = get_account_style_library(session)
        style["account_positioning"] = "测试账号定位：只讲普通人能复用的商业判断。"
        style["content_directions"].append(
            {"id": "new_direction", "label": "新职业变化", "enabled": False}
        )
        style["structure_preferences"][0]["framework"] = "表面变化 → 人话解释 → 判断标准"
        style["opening_templates"][0]["template"] = "这件事先别急着下结论。"

        saved = save_account_style_library(session, style)

        assert saved["using_default"] is False
        assert saved["last_saved_at"]
        assert saved["account_positioning"] == "测试账号定位：只讲普通人能复用的商业判断。"
        assert saved["content_directions"][-1]["enabled"] is False
        assert saved["structure_preferences"][0]["framework"] == "表面变化 → 人话解释 → 判断标准"
        assert get_account_style_context(session)["account_positioning"] == saved[
            "account_positioning"
        ]
        assert "新职业变化" not in get_account_style_context(session)["content_directions"]

        reset = reset_account_style_library(session)

        assert reset["account_positioning"].startswith("用大白话")
        assert reset["using_default"] is False


def test_account_style_api_reads_saves_and_resets():
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
        response = client.get("/api/account-style")
        assert response.status_code == 200
        payload = response.json()
        payload["account_positioning"] = "接口保存后的账号定位"

        save_response = client.put("/api/account-style", json=payload)
        assert save_response.status_code == 200
        assert save_response.json()["account_positioning"] == "接口保存后的账号定位"

        reset_response = client.post("/api/account-style/reset")
        assert reset_response.status_code == 200
        assert reset_response.json()["account_positioning"].startswith("用大白话")
    finally:
        app.dependency_overrides.clear()


def test_topic_and_script_generation_read_global_account_style():
    with make_session() as session:
        style = get_account_style_library(session)
        style["account_positioning"] = "测试风格：讲清楚趋势，不用营销号表达。"
        save_account_style_library(session, style)
        hot_item = add_hot_item(session)
        provider = build_provider(AIProviderConfig(provider="fake", model="fake-low-cost"))

        topic_result = generate_topic_ideas_for_hotspot(session, hot_item.id, provider=provider)
        topic_generation = (
            session.query(AiGeneration)
            .filter(AiGeneration.generation_type == "topic_ideas")
            .one()
        )
        assert topic_generation.input_content["input_content"]["account_style"][
            "account_positioning"
        ] == (
            "测试风格：讲清楚趋势，不用营销号表达。"
        )

        topic = session.get(TopicIdea, topic_result.ideas[0].id)
        assert topic is not None
        generate_script_for_topic(session, topic, provider=provider)
        script_generation = (
            session.query(AiGeneration)
            .filter(AiGeneration.generation_type == "script")
            .one()
        )
        assert script_generation.input_content["input_content"]["account_style"][
            "account_positioning"
        ] == (
            "测试风格：讲清楚趋势，不用营销号表达。"
        )
        assert (
            "大家都知道"
            in script_generation.input_content["input_content"]["style_validation"][
                "default_common_knowledge_phrases"
            ]
        )
