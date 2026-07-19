from __future__ import annotations

from sqlalchemy.orm import Session

from app.ai.cache import build_input_hash, build_input_snapshot
from app.ai.factory import build_provider
from app.ai.providers.base import AIProvider
from app.ai.providers.fake import FakeProvider
from app.ai.schemas import AIProviderConfig, AIRequest, CachedAIResponse
from app.models import AiGeneration


def generate_with_cache(
    session: Session,
    request: AIRequest,
    provider: AIProvider | None = None,
    config: AIProviderConfig | None = None,
) -> CachedAIResponse:
    provider = provider or build_provider(config)
    cached = _get_cached(session, request, provider)
    if cached is not None:
        return cached

    try:
        response = provider.generate(request)
    except Exception:
        if provider.name == "fake":
            raise
        fallback_provider = FakeProvider(model="fake-low-cost")
        fallback_cached = _get_cached(session, request, fallback_provider)
        if fallback_cached is not None:
            return fallback_cached
        response = fallback_provider.generate(request)
        response.output_content["provider_notice"] = "真实 AI 不可用，已回退 fake provider。"
        provider = fallback_provider

    input_hash = build_input_hash(request, provider.name, provider.model)
    generation = AiGeneration(
        generation_type=request.generation_type_value,
        input_hash=input_hash,
        input_content=build_input_snapshot(request),
        output_content=response.output_content,
        provider=response.provider,
        model=response.model,
    )
    session.add(generation)
    session.commit()
    session.refresh(generation)
    return CachedAIResponse(
        output_content=generation.output_content,
        provider=generation.provider,
        model=generation.model,
        input_hash=generation.input_hash,
        from_cache=False,
    )


def _get_cached(
    session: Session,
    request: AIRequest,
    provider: AIProvider,
) -> CachedAIResponse | None:
    input_hash = build_input_hash(request, provider.name, provider.model)
    cached = session.query(AiGeneration).filter(AiGeneration.input_hash == input_hash).one_or_none()
    if cached is not None:
        return CachedAIResponse(
            output_content=cached.output_content,
            provider=cached.provider,
            model=cached.model,
            input_hash=cached.input_hash,
            from_cache=True,
        )
    return None
