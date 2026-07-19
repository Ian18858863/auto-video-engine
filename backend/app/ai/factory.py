from __future__ import annotations

from app.ai.providers.base import AIProvider
from app.ai.providers.fake import FakeProvider
from app.ai.providers.openai_compatible import OpenAICompatibleProvider
from app.ai.schemas import AIProviderConfig
from app.core.config import Settings, get_settings


OPENAI_COMPATIBLE_PROVIDERS = {
    "openai-compatible",
    "openai",
    "deepseek",
    "kimi",
    "tongyi",
    "qwen",
    "doubao",
}


def provider_config_from_settings(settings: Settings | None = None) -> AIProviderConfig:
    settings = settings or get_settings()
    return AIProviderConfig(
        provider=settings.ai_provider,
        base_url=settings.ai_base_url,
        api_key=settings.ai_api_key,
        model=settings.ai_model,
        timeout=settings.ai_timeout,
        max_retries=settings.ai_max_retries,
    )


def build_provider(config: AIProviderConfig | None = None) -> AIProvider:
    config = config or provider_config_from_settings()
    provider_name = (config.provider or "fake").strip().lower()

    if provider_name == "fake":
        return FakeProvider(model=config.model or "fake-low-cost")

    if provider_name in OPENAI_COMPATIBLE_PROVIDERS:
        if not config.base_url or not config.api_key or not config.model:
            return FakeProvider()
        return OpenAICompatibleProvider(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            provider_name=provider_name,
            timeout=config.timeout,
            max_retries=config.max_retries,
        )

    return FakeProvider()
