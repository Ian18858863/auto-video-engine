from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.factory import build_provider
from app.ai.schemas import AIProviderConfig
from app.copywriting_samples.schemas import CopywritingSamplePayload
from app.copywriting_samples.service import (
    REFERENCE_RULE,
    create_copywriting_sample,
    delete_copywriting_sample,
    get_copywriting_sample_context,
    list_copywriting_samples,
    serialize_copywriting_sample,
    update_copywriting_sample,
)
from app.core.database import Base, get_db
from app.main import app
from app.models import AiGeneration, CopywritingSample, HotItem, TopicIdea
from app.pipeline.script_service import generate_script_for_topic
from app.topic_ideas.service import generate_topic_ideas_for_hotspot


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def sample_payload(**overrides) -> CopywritingSamplePayload:
    data = {
        "title": "别只看热度的开头",
        "sample_type": "开头钩子",
        "sample_text": "这件事表面上是一个热点，但背后真正值得看的，是平台为什么开始抢这个入口。",
        "scene": "热点解释",
        "content_direction": "商业热点",
        "structure_type": "热点背后逻辑型",
        "why_reference": "开头先压住情绪，再把注意力拉到商业逻辑。",
        "reusable_points": "表面事件 → 背后逻辑 → 入口变化",
        "avoid_points": "不要照抄原句，不要夸张成全网疯传。",
        "tags": ["开头", "商业逻辑"],
        "enabled": True,
        "is_key_reference": True,
        "notes": "适合热点解释。",
    }
    data.update(overrides)
    return CopywritingSamplePayload(**data)


def add_hot_item(session: Session) -> HotItem:
    item = HotItem(
        source_platform="手动导入",
        source_type="manual",
        title="AI 工具进入企业采购预算",
        url="https://example.com/copywriting-sample-context",
        published_at=datetime.now(timezone.utc),
        summary="企业开始真实采购 AI 工具，需要看背后的商业逻辑。",
        tags=["AI", "商业"],
        category="AI",
        hot_score=88,
        topic_score=92,
        save_score=86,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def test_copywriting_sample_crud_and_detail_serialization():
    with make_session() as session:
        created = create_copywriting_sample(session, sample_payload())

        assert created.id is not None
        assert created.is_key_reference is True
        assert created.tags == ["开头", "商业逻辑"]

        updated = update_copywriting_sample(
            session,
            created.id,
            sample_payload(
                title="更新后的收藏清单样本",
                sample_type="收藏清单结构",
                enabled=False,
                is_key_reference=False,
                tags=["清单"],
            ),
        )

        assert updated.title == "更新后的收藏清单样本"
        assert updated.sample_type == "收藏清单结构"
        assert updated.enabled is False
        assert updated.is_key_reference is False
        assert serialize_copywriting_sample(updated)["tags"] == ["清单"]

        delete_copywriting_sample(session, created.id)

        assert session.query(CopywritingSample).count() == 0


def test_copywriting_samples_can_filter_search_and_build_context():
    with make_session() as session:
        create_copywriting_sample(session, sample_payload())
        create_copywriting_sample(
            session,
            sample_payload(
                title="停用标题样本",
                sample_type="标题",
                sample_text="看懂 AI 工具采购，先看这 3 点",
                scene="清单型内容",
                content_direction="AI 工具",
                tags=["标题", "AI"],
                enabled=False,
                is_key_reference=False,
            ),
        )

        by_type = list_copywriting_samples(session, sample_type="开头钩子")
        by_scene = list_copywriting_samples(session, scene="清单型内容")
        by_tag = list_copywriting_samples(session, tag="商业逻辑")
        by_keyword = list_copywriting_samples(session, keyword="平台为什么")
        enabled_only = list_copywriting_samples(session, enabled_only=True)

        assert by_type.total == 1
        assert by_scene.total == 1
        assert by_tag.total == 1
        assert by_keyword.total == 1
        assert enabled_only.total == 1
        assert "开头钩子" in by_type.sample_types
        assert "热点解释" in by_type.scenes
        assert "商业逻辑" in by_type.tags

        context = get_copywriting_sample_context(session)

        assert context["reference_rule"] == REFERENCE_RULE
        assert context["samples"][0]["sample_type"] == "开头钩子"
        assert context["samples"][0]["is_key_reference"] is True
        assert len(context["samples"]) == 1


def test_copywriting_sample_api_crud_filters_and_does_not_call_ai():
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

    payload = sample_payload().model_dump()
    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        create_response = client.post("/api/copywriting-samples", json=payload)
        sample_id = create_response.json()["id"]

        list_response = client.get(
            "/api/copywriting-samples?sample_type=开头钩子&tag=商业逻辑&keyword=平台为什么"
        )
        detail_response = client.get(f"/api/copywriting-samples/{sample_id}")

        payload["enabled"] = False
        payload["is_key_reference"] = False
        update_response = client.put(f"/api/copywriting-samples/{sample_id}", json=payload)
        delete_response = client.delete(f"/api/copywriting-samples/{sample_id}")
    finally:
        app.dependency_overrides.clear()

    assert create_response.status_code == 200
    assert list_response.status_code == 200
    assert list_response.json()["items"][0]["title"] == "别只看热度的开头"
    assert list_response.json()["real_ai_called"] is False
    assert "不直接复制原文" in list_response.json()["reference_rule"]
    assert detail_response.status_code == 200
    assert update_response.json()["enabled"] is False
    assert delete_response.json() == {"deleted": True, "id": sample_id}
    with testing_session_local() as verify_session:
        assert verify_session.query(AiGeneration).count() == 0
        assert verify_session.query(CopywritingSample).count() == 0


def test_topic_and_script_generation_read_copywriting_sample_library():
    with make_session() as session:
        create_copywriting_sample(session, sample_payload())
        create_copywriting_sample(
            session,
            sample_payload(
                title="停用样本不应进入生成上下文",
                sample_text="这是一条停用样本，不能进入脚本生成参考。",
                content_direction="停用方向",
                tags=["停用"],
                enabled=False,
                is_key_reference=True,
            ),
        )
        hot_item = add_hot_item(session)
        provider = build_provider(AIProviderConfig(provider="fake", model="fake-low-cost"))

        topic_result = generate_topic_ideas_for_hotspot(session, hot_item.id, provider=provider)
        topic_generation = (
            session.query(AiGeneration)
            .filter(AiGeneration.generation_type == "topic_ideas")
            .one()
        )

        topic_input = topic_generation.input_content["input_content"]
        assert topic_input["copywriting_samples"]["source"] == "script_engine"
        assert topic_input["copywriting_samples"]["samples"][0]["sample_type"] == "开头钩子"
        assert "不直接复制原文" in topic_input["copywriting_samples"]["reference_rule"]
        assert topic_input["script_engine"]["source"] == "script_engine"

        topic = session.get(TopicIdea, topic_result.ideas[0].id)
        assert topic is not None
        generate_script_for_topic(session, topic, provider=provider)
        script_generation = (
            session.query(AiGeneration)
            .filter(AiGeneration.generation_type == "script")
            .one()
        )
        script_input = script_generation.input_content["input_content"]
        script_samples = script_input["copywriting_samples"]["samples"]
        assert script_samples[0]["content_direction"] == "商业热点"
        assert all(sample["content_direction"] != "停用方向" for sample in script_samples)
        assert all("停用样本" not in sample["text_excerpt"] for sample in script_samples)
        assert script_input["copywriting_samples"]["account_style_priority"].startswith("账号风格库")
        assert script_input["script_engine"]["source"] == "script_engine"
