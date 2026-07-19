from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.account_style.service import get_account_style_context
from app.ai.providers.base import AIProvider
from app.ai.schemas import AIRequest, GenerationType
from app.ai.service import generate_with_cache
from app.hotspots.cleaning import public_url
from app.models import HotItem, TopicIdea
from app.script_engine.service import get_script_engine_context


class HotspotNotFoundError(ValueError):
    pass


class FilteredHotspotError(ValueError):
    pass


@dataclass(slots=True)
class TopicIdeaGenerationResult:
    hotspot_id: int
    ideas: list[TopicIdea]
    from_cache: bool
    input_hash: str


def generate_topic_ideas_for_hotspot(
    session: Session,
    hotspot_id: int,
    provider: AIProvider | None = None,
) -> TopicIdeaGenerationResult:
    hot_item = session.get(HotItem, hotspot_id)
    if hot_item is None:
        raise HotspotNotFoundError(f"热点不存在：{hotspot_id}")
    if hot_item.is_filtered:
        reason = hot_item.filter_reason or "该热点已被规则过滤"
        raise FilteredHotspotError(f"该热点已过滤，不建议生成选题：{reason}")

    request = AIRequest(
        generation_type=GenerationType.TOPIC_IDEAS,
        input_content=_build_topic_idea_input(session, hot_item),
        system_prompt=_topic_idea_system_prompt(),
        user_prompt=_topic_idea_user_prompt(hot_item),
    )
    ai_result = generate_with_cache(session, request, provider=provider)
    idea_payloads = _extract_topic_ideas(ai_result.output_content)
    ideas = _save_topic_ideas(session, hot_item, idea_payloads)

    return TopicIdeaGenerationResult(
        hotspot_id=hot_item.id,
        ideas=ideas,
        from_cache=ai_result.from_cache,
        input_hash=ai_result.input_hash,
    )


def serialize_topic_idea(idea: TopicIdea) -> dict[str, Any]:
    return {
        "id": idea.id,
        "hot_item_id": idea.hot_item_id,
        "title": idea.title,
        "core_view": idea.core_view,
        "recommended_format": idea.recommended_format,
        "why_watch": idea.why_watch,
        "why_save": idea.why_save,
        "comment_trigger": idea.comment_trigger,
        "risk_note": idea.risk_note,
        "recommended_duration": idea.recommended_duration,
        "hook": idea.hook,
        "video_type": idea.video_type,
        "generation_source": idea.generation_source,
        "recommendation_score": idea.recommendation_score,
        "recommendation_bucket": idea.recommendation_bucket,
        "source_hotspot_ids": idea.source_hotspot_ids or [],
        "source_hotspot_titles": idea.source_hotspot_titles or [],
        "is_example": idea.is_example,
        "is_selected": idea.is_selected,
    }


def _build_topic_idea_input(session: Session, hot_item: HotItem) -> dict[str, Any]:
    account_style = get_account_style_context(session)
    script_engine = get_script_engine_context(session)
    return {
        "hotspot_id": hot_item.id,
        "title": hot_item.title,
        "summary": hot_item.summary,
        "source_platform": hot_item.source_platform,
        "source_type": hot_item.source_type,
        "url": public_url(hot_item.url),
        "category": hot_item.category,
        "tags": hot_item.tags,
        "scores": {
            "hot_score": hot_item.hot_score,
            "topic_score": hot_item.topic_score,
            "save_score": hot_item.save_score,
        },
        "account_style": account_style,
        "copywriting_samples": script_engine,
        "script_engine": script_engine,
        "account_direction": [account_style["account_positioning"]],
        "topic_directions": account_style["content_directions"],
    }


def _topic_idea_system_prompt() -> str:
    return (
        "你是短视频选题策划助手。只输出可保存为候选选题的数据。"
        "风格要清楚、克制、有判断、有收藏价值，不做投资建议。"
    )


def _topic_idea_user_prompt(hot_item: HotItem) -> str:
    return (
        "基于这条热点生成 3-5 个短视频选题角度。"
        f"热点标题：{hot_item.title}。"
        f"摘要：{hot_item.summary}。"
        "每个选题必须包含标题、核心观点、推荐视频形式、观看理由、收藏理由、评论触发点、风险提示、推荐时长和开头钩子。"
    )


def _extract_topic_ideas(output_content: dict[str, Any]) -> list[dict[str, Any]]:
    raw_ideas = output_content.get("topic_ideas")
    if not isinstance(raw_ideas, list):
        raise ValueError("AI 输出缺少 topic_ideas 列表。")
    cleaned = [idea for idea in raw_ideas if isinstance(idea, dict)]
    if not 3 <= len(cleaned) <= 5:
        raise ValueError("AI 选题数量必须是 3-5 个。")
    return cleaned


def _save_topic_ideas(
    session: Session,
    hot_item: HotItem,
    idea_payloads: list[dict[str, Any]],
) -> list[TopicIdea]:
    existing_by_title = {
        idea.title: idea
        for idea in session.query(TopicIdea).filter(TopicIdea.hot_item_id == hot_item.id).all()
    }
    ideas: list[TopicIdea] = []

    for payload in idea_payloads:
        title = _string(payload, "title", max_length=300)
        if not title:
            continue
        existing = existing_by_title.get(title)
        if existing is not None:
            ideas.append(existing)
            continue

        idea = TopicIdea(
            hot_item_id=hot_item.id,
            title=title,
            core_view=_string(payload, "core_view", "point", max_length=500),
            recommended_format=_string(payload, "recommended_format", max_length=80),
            why_watch=_string(payload, "why_watch", max_length=500),
            why_save=_string(payload, "why_save", max_length=500),
            comment_trigger=_string(payload, "comment_trigger", max_length=500),
            risk_note=_string(payload, "risk_note", max_length=500),
            recommended_duration=_string(payload, "recommended_duration", max_length=80),
            hook=_string(payload, "hook", max_length=500),
            video_type="single_deep_dive",
            generation_source="fake_provider",
            recommendation_score=_recommendation_score(hot_item),
            recommendation_bucket="backup",
            source_hotspot_ids=[hot_item.id],
            source_hotspot_titles=[hot_item.title],
            is_example=hot_item.data_origin == "sample",
        )
        session.add(idea)
        ideas.append(idea)

    hot_item.has_topic_ideas = bool(ideas)
    session.add(hot_item)
    session.commit()

    for idea in ideas:
        session.refresh(idea)
    session.refresh(hot_item)
    return ideas


def _string(payload: dict[str, Any], *keys: str, max_length: int) -> str:
    for key in keys:
        value = str(payload.get(key, "") or "").strip()
        if value:
            return value[:max_length]
    return ""


def _recommendation_score(hot_item: HotItem) -> float:
    return round(
        max(
            0.0,
            min(
                100.0,
                hot_item.topic_score * 0.45
                + hot_item.save_score * 0.3
                + hot_item.hot_score * 0.2
                + hot_item.freshness_score * 0.05
                - hot_item.risk_score * 0.25,
            ),
        ),
        2,
    )
