from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.factory import build_provider
from app.ai.providers.base import AIProvider
from app.ai.schemas import AIProviderConfig, AIRequest, GenerationType
from app.ai.service import generate_with_cache
from app.core.database import Base
from app.models import AiGeneration


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def test_fake_provider_returns_test_content():
    provider = build_provider(AIProviderConfig(provider="fake", model="fake-low-cost"))
    request = AIRequest(
        generation_type=GenerationType.TOPIC_IDEAS,
        input_content={"title": "AI 工具进入企业采购"},
    )

    response = provider.generate(request)

    assert response.provider == "fake"
    assert response.model == "fake-low-cost"
    assert "本地测试内容" in response.output_content["text"]


def test_same_input_uses_cache_on_second_call():
    with make_session() as session:
        provider = build_provider(AIProviderConfig(provider="fake", model="fake-low-cost"))
        request = AIRequest(
            generation_type=GenerationType.SCRIPT,
            input_content={"topic": "AI 工具为什么进入企业采购"},
        )

        first = generate_with_cache(session, request, provider)
        second = generate_with_cache(session, request, provider)

        assert first.from_cache is False
        assert second.from_cache is True
        assert first.output_content == second.output_content
        assert provider.call_count == 1
        assert session.query(AiGeneration).count() == 1


def test_different_generation_type_does_not_reuse_cache():
    with make_session() as session:
        provider = build_provider(AIProviderConfig(provider="fake", model="fake-low-cost"))
        shared_input = {"topic": "AI 工具为什么进入企业采购"}

        topic_result = generate_with_cache(
            session,
            AIRequest(generation_type=GenerationType.TOPIC_IDEAS, input_content=shared_input),
            provider,
        )
        script_result = generate_with_cache(
            session,
            AIRequest(generation_type=GenerationType.SCRIPT, input_content=shared_input),
            provider,
        )

        assert topic_result.from_cache is False
        assert script_result.from_cache is False
        assert topic_result.input_hash != script_result.input_hash
        assert provider.call_count == 2
        assert session.query(AiGeneration).count() == 2


def test_missing_provider_config_falls_back_to_fake_without_crashing():
    with make_session() as session:
        provider = build_provider(AIProviderConfig(provider="", base_url="", api_key="", model=""))
        request = AIRequest(
            generation_type=GenerationType.COPY_STYLE_TEST,
            input_content={"title": "消费趋势新变化"},
        )

        result = generate_with_cache(session, request, provider)

        assert result.provider == "fake"
        assert result.model == "fake-low-cost"
        assert result.from_cache is False
        assert session.query(AiGeneration).count() == 1


class BrokenRealProvider(AIProvider):
    name = "doubao"
    model = "doubao-test"

    def generate(self, request: AIRequest):
        raise RuntimeError("quota exceeded")


def test_real_provider_failure_falls_back_to_fake_and_caches_result():
    with make_session() as session:
        request = AIRequest(
            generation_type=GenerationType.SCRIPT,
            input_content={"topic": "AI 搜索正在改变普通人获取信息的方式"},
        )

        first = generate_with_cache(session, request, BrokenRealProvider())
        second = generate_with_cache(session, request, BrokenRealProvider())

        assert first.provider == "fake"
        assert first.model == "fake-low-cost"
        assert first.from_cache is False
        assert first.output_content["provider_notice"] == "真实 AI 不可用，已回退 fake provider。"
        assert second.from_cache is True
        assert session.query(AiGeneration).count() == 1
