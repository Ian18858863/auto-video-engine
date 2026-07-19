from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any

from sqlalchemy import case, or_
from sqlalchemy.orm import Session, joinedload

from app.hotspots.archive import archive_expired_hotspots
from app.hotspots.cleaning import public_url
from app.hotspots.eligibility import is_today_recommendation_eligible, source_credibility
from app.hotspots.source_policy import is_internal_test_hotspot
from app.models import HotItem, ProductionPipeline, ScriptAsset, TopicIdea
from app.topic_ideas.event_scoring import event_score, score_breakdown_dict
from app.topic_ideas.filter_settings import get_topic_filter_settings
from app.topic_ideas.local_generator import refresh_recommendation_buckets

GENERAL_POOL_VIDEO_TYPES = {
    "collection_event",
    "single_deep_dive",
    "single_deep_dive_candidate",
}
GENERAL_POOL_GENERATION_SOURCE = "local_rule"
MIN_GENERAL_RECOMMENDATION_SCORE = 40.0


@dataclass(slots=True)
class HotspotOption:
    id: int
    title: str


@dataclass(slots=True)
class TopicIdeaPoolItem:
    id: int
    hot_item_id: int
    hotspot_title: str
    title: str
    core_view: str
    recommended_format: str
    why_watch: str
    why_save: str
    comment_trigger: str
    risk_note: str
    recommended_duration: str
    hook: str
    video_type: str
    generation_source: str
    recommendation_score: float
    recommendation_bucket: str
    source_hotspot_ids: list[int]
    source_hotspot_titles: list[str]
    source_hotspot_urls: list[str]
    source_published_at: list[str]
    source_fetched_at: list[str]
    source_summaries: list[str]
    source_platforms: list[str]
    source_credibility: str
    source_credibility_reason: str
    score_breakdown: dict[str, float]
    score_bonus_reasons: list[str]
    fits_collection: bool
    can_single_deep_dive: bool
    is_example: bool
    is_selected: bool
    has_script: bool
    script_asset_id: int | None
    pipeline_id: int | None


@dataclass(slots=True)
class TopicIdeaListResult:
    items: list[TopicIdeaPoolItem]
    total: int
    page: int
    page_size: int
    total_pages: int
    hotspots: list[HotspotOption]


def list_topic_ideas(
    session: Session,
    *,
    hotspot_id: int | None = None,
    recommendation_bucket: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> TopicIdeaListResult:
    archive_expired_hotspots(session)
    settings = get_topic_filter_settings(session)
    refresh_recommendation_buckets(session, today_limit=int(settings.get("current_period_limit", 5)), settings=settings)
    query = session.query(TopicIdea).join(HotItem, TopicIdea.hot_item_id == HotItem.id)
    query = query.filter(
        or_(
            HotItem.lifecycle_status != "archived",
            TopicIdea.is_selected.is_(True),
            TopicIdea.script_assets.any(),
        )
    )
    if hotspot_id is None:
        query = _exclude_non_public_hotspots(query)
        query = _only_general_topic_pool_items(query, settings=settings)
    if hotspot_id is not None:
        query = query.filter(TopicIdea.hot_item_id == hotspot_id)
    if recommendation_bucket:
        query = query.filter(TopicIdea.recommendation_bucket == recommendation_bucket)

    ordered_ideas = (
        query.options(joinedload(TopicIdea.hot_item))
        .order_by(
            case((TopicIdea.recommendation_bucket == "today", 0), else_=1),
            TopicIdea.recommendation_score.desc(),
            TopicIdea.id.desc(),
        )
        .all()
    )
    if hotspot_id is None:
        ordered_ideas = [
            idea
            for idea in ordered_ideas
            if not _idea_references_non_public_hotspot(session, idea)
        ]
    total = len(ordered_ideas)
    ideas = ordered_ideas[(page - 1) * page_size : page * page_size]
    script_ids, pipeline_ids = _latest_outputs(
        session,
        [idea.id for idea in ideas],
    )
    hotspot_rows_query = (
        session.query(HotItem.id, HotItem.title)
        .join(TopicIdea, TopicIdea.hot_item_id == HotItem.id)
        .filter(HotItem.lifecycle_status != "archived")
    )
    if hotspot_id is None:
        hotspot_rows_query = _exclude_non_public_hotspots(hotspot_rows_query)
        hotspot_rows_query = _only_general_topic_pool_items(hotspot_rows_query, settings=settings)
    hotspot_rows = hotspot_rows_query.distinct().order_by(HotItem.id.desc()).all()
    return TopicIdeaListResult(
        items=[
            _to_pool_item(
                session,
                idea,
                script_asset_id=script_ids.get(idea.id),
                pipeline_id=pipeline_ids.get(idea.id),
                settings=settings,
            )
            for idea in ideas
        ],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=ceil(total / page_size) if total else 0,
        hotspots=[
            HotspotOption(id=hotspot_row.id, title=hotspot_row.title)
            for hotspot_row in hotspot_rows
        ],
    )


def get_topic_idea(
    session: Session,
    topic_idea_id: int,
) -> TopicIdeaPoolItem | None:
    idea = (
        session.query(TopicIdea)
        .options(joinedload(TopicIdea.hot_item))
        .filter(TopicIdea.id == topic_idea_id)
        .one_or_none()
    )
    if idea is None:
        return None
    script_ids, pipeline_ids = _latest_outputs(session, [idea.id])
    return _to_pool_item(
        session,
        idea,
        script_asset_id=script_ids.get(idea.id),
        pipeline_id=pipeline_ids.get(idea.id),
        settings=get_topic_filter_settings(session),
    )


def select_topic_idea(
    session: Session,
    topic_idea_id: int,
) -> TopicIdeaPoolItem | None:
    selected = session.get(TopicIdea, topic_idea_id)
    if selected is None:
        return None
    ideas = (
        session.query(TopicIdea)
        .filter(TopicIdea.hot_item_id == selected.hot_item_id)
        .all()
    )
    for idea in ideas:
        idea.is_selected = idea.id == selected.id
        session.add(idea)
    session.commit()
    return get_topic_idea(session, selected.id)


def set_topic_event_selection(
    session: Session,
    topic_idea_id: int,
    *,
    selected: bool,
) -> TopicIdeaPoolItem | None:
    idea = session.get(TopicIdea, topic_idea_id)
    if idea is None:
        return None
    idea.is_selected = selected
    session.add(idea)
    session.commit()
    return get_topic_idea(session, topic_idea_id)


def serialize_topic_idea_pool_item(item: TopicIdeaPoolItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "hot_item_id": item.hot_item_id,
        "hotspot_title": item.hotspot_title,
        "title": item.title,
        "core_view": item.core_view,
        "recommended_format": item.recommended_format,
        "why_watch": item.why_watch,
        "why_save": item.why_save,
        "comment_trigger": item.comment_trigger,
        "risk_note": item.risk_note,
        "recommended_duration": item.recommended_duration,
        "hook": item.hook,
        "video_type": item.video_type,
        "generation_source": item.generation_source,
        "recommendation_score": item.recommendation_score,
        "recommendation_bucket": item.recommendation_bucket,
        "source_hotspot_ids": item.source_hotspot_ids,
        "source_hotspot_titles": item.source_hotspot_titles,
        "source_hotspot_urls": item.source_hotspot_urls,
        "source_published_at": item.source_published_at,
        "source_fetched_at": item.source_fetched_at,
        "source_summaries": item.source_summaries,
        "source_platforms": item.source_platforms,
        "source_credibility": item.source_credibility,
        "source_credibility_reason": item.source_credibility_reason,
        "score_breakdown": item.score_breakdown,
        "score_bonus_reasons": item.score_bonus_reasons,
        "fits_collection": item.fits_collection,
        "can_single_deep_dive": item.can_single_deep_dive,
        "is_example": item.is_example,
        "is_selected": item.is_selected,
        "has_script": item.has_script,
        "script_asset_id": item.script_asset_id,
        "pipeline_id": item.pipeline_id,
    }


def serialize_topic_idea_list(result: TopicIdeaListResult) -> dict[str, Any]:
    return {
        "items": [serialize_topic_idea_pool_item(item) for item in result.items],
        "pagination": {
            "page": result.page,
            "page_size": result.page_size,
            "total": result.total,
            "total_pages": result.total_pages,
        },
        "filters": {
            "hotspots": [
                {"id": hotspot.id, "title": hotspot.title}
                for hotspot in result.hotspots
            ],
            "recommendation_buckets": ["today", "backup"],
        },
    }


def _exclude_non_public_hotspots(query: Any) -> Any:
    return query.filter(
        HotItem.data_origin.notin_(["sample", "test", "manual_v1_test"]),
        HotItem.source_platform != "手动跑通测试",
        HotItem.source_platform.notlike("%?%"),
        HotItem.title.notlike("%????%"),
    )


def _only_general_topic_pool_items(query: Any, *, settings: dict[str, Any] | None = None) -> Any:
    minimum_score = float((settings or {}).get("minimum_score", MIN_GENERAL_RECOMMENDATION_SCORE))
    return query.filter(
        TopicIdea.generation_source == GENERAL_POOL_GENERATION_SOURCE,
        or_(TopicIdea.video_type.in_(GENERAL_POOL_VIDEO_TYPES), TopicIdea.video_type.is_(None)),
        TopicIdea.recommendation_score >= minimum_score,
    )


def _idea_references_non_public_hotspot(session: Session, idea: TopicIdea) -> bool:
    return any(_is_non_public_hotspot(item) for item in _source_hotspots(session, idea))


def _is_non_public_hotspot(item: HotItem) -> bool:
    return (
        item.data_origin in {"sample", "test", "manual_v1_test"}
        or item.source_platform == "手动跑通测试"
        or _has_mojibake(item.source_platform)
        or _has_mojibake(item.title)
        or not is_today_recommendation_eligible(item)
    )


def _has_mojibake(value: str | None) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    return "????" in text or set(text) <= {"?"}


def _latest_outputs(
    session: Session,
    topic_idea_ids: list[int],
) -> tuple[dict[int, int], dict[int, int]]:
    if not topic_idea_ids:
        return {}, {}

    script_ids: dict[int, int] = {}
    scripts = (
        session.query(ScriptAsset)
        .filter(ScriptAsset.topic_idea_id.in_(topic_idea_ids))
        .order_by(ScriptAsset.id.desc())
        .all()
    )
    for script in scripts:
        script_ids.setdefault(script.topic_idea_id, script.id)

    pipeline_ids: dict[int, int] = {}
    pipelines = (
        session.query(ProductionPipeline)
        .filter(ProductionPipeline.topic_idea_id.in_(topic_idea_ids))
        .order_by(ProductionPipeline.id.desc())
        .all()
    )
    for pipeline in pipelines:
        pipeline_ids.setdefault(pipeline.topic_idea_id, pipeline.id)
    return script_ids, pipeline_ids


def _to_pool_item(
    session: Session,
    idea: TopicIdea,
    *,
    script_asset_id: int | None,
    pipeline_id: int | None,
    settings: dict[str, Any] | None = None,
) -> TopicIdeaPoolItem:
    settings = settings or get_topic_filter_settings(session)
    source_hotspots = _source_hotspots(session, idea)
    recommendation_bucket = idea.recommendation_bucket or "backup"
    if recommendation_bucket == "today" and not _today_bucket_is_valid(source_hotspots, idea):
        recommendation_bucket = "backup"
    credibility, credibility_reason = _aggregate_source_credibility(source_hotspots)
    breakdown, bonus_reasons, fits_collection, can_single_deep_dive = _event_fit_info(
        source_hotspots,
        idea,
        settings=settings,
    )
    return TopicIdeaPoolItem(
        id=idea.id,
        hot_item_id=idea.hot_item_id,
        hotspot_title=idea.hot_item.title,
        title=idea.title,
        core_view=idea.core_view,
        recommended_format=idea.recommended_format,
        why_watch=idea.why_watch,
        why_save=idea.why_save,
        comment_trigger=idea.comment_trigger,
        risk_note=idea.risk_note,
        recommended_duration=idea.recommended_duration,
        hook=idea.hook,
        video_type=idea.video_type or "single_deep_dive",
        generation_source=idea.generation_source or "",
        recommendation_score=idea.recommendation_score,
        recommendation_bucket=recommendation_bucket,
        source_hotspot_ids=idea.source_hotspot_ids or [idea.hot_item_id],
        source_hotspot_titles=idea.source_hotspot_titles or [idea.hot_item.title],
        source_hotspot_urls=[
            visible_url
            for item in source_hotspots
            if (visible_url := public_url(item.url))
        ],
        source_published_at=[_isoformat(item.published_at) for item in source_hotspots if item.published_at],
        source_fetched_at=[_isoformat(item.fetched_at) for item in source_hotspots if item.fetched_at],
        source_summaries=[item.summary for item in source_hotspots if item.summary],
        source_platforms=_unique([item.source_platform for item in source_hotspots]),
        source_credibility=credibility,
        source_credibility_reason=credibility_reason,
        score_breakdown=breakdown,
        score_bonus_reasons=bonus_reasons,
        fits_collection=fits_collection,
        can_single_deep_dive=can_single_deep_dive,
        is_example=idea.is_example,
        is_selected=idea.is_selected,
        has_script=script_asset_id is not None,
        script_asset_id=script_asset_id,
        pipeline_id=pipeline_id,
    )


def _source_hotspots(session: Session, idea: TopicIdea) -> list[HotItem]:
    source_ids = idea.source_hotspot_ids or [idea.hot_item_id]
    if not source_ids:
        return []
    rows = session.query(HotItem).filter(HotItem.id.in_(source_ids)).all()
    by_id = {item.id: item for item in rows}
    return [by_id[item_id] for item_id in source_ids if item_id in by_id]


def _isoformat(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _today_bucket_is_valid(source_hotspots: list[HotItem], idea: TopicIdea) -> bool:
    if idea.is_example or idea.generation_source != "local_rule":
        return False
    if any(is_internal_test_hotspot(item) or _is_non_public_hotspot(item) for item in source_hotspots):
        return False
    source_ids = idea.source_hotspot_ids or [idea.hot_item_id]
    if len(source_hotspots) != len(set(source_ids)):
        return False
    return all(is_today_recommendation_eligible(item) for item in source_hotspots)


def _aggregate_source_credibility(source_hotspots: list[HotItem]) -> tuple[str, str]:
    if not source_hotspots:
        return "低", "没有可验证来源热点。"
    results = [
        source_credibility(
            url=public_url(item.url),
            published_at=item.published_at,
            summary=item.summary,
        )
        for item in source_hotspots
    ]
    levels = [level for level, _ in results]
    if all(level == "高" for level in levels):
        return "高", "所有来源都有原文 URL、发布时间和正文摘要。"
    if any(level == "低" for level in levels):
        return "低", "至少一个来源缺少可验证的原文信息。"
    return "中", "部分来源信息完整，部分来源缺少发布时间或正文摘要。"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _event_fit_info(
    source_hotspots: list[HotItem],
    idea: TopicIdea,
    *,
    settings: dict[str, Any],
) -> tuple[dict[str, float], list[str], bool, bool]:
    if not source_hotspots:
        return {}, [], False, False
    source_count = len({item.source_platform for item in source_hotspots if item.source_platform}) or 1
    scores = [event_score(item, source_count=source_count, settings=settings) for item in source_hotspots]
    if len(scores) == 1:
        breakdown = score_breakdown_dict(scores[0])
    else:
        keys = score_breakdown_dict(scores[0]).keys()
        breakdown = {
            key: round(sum(score_breakdown_dict(score)[key] for score in scores) / len(scores), 2)
            for key in keys
        }
    bonus_reasons = _unique([reason for score in scores for reason in score.bonus_reasons])
    fits_collection = idea.video_type == "multi_hotspot_collection" or all(
        score.fits_collection for score in scores
    )
    can_single_deep_dive = len(scores) == 1 and scores[0].can_single_deep_dive
    return breakdown, bonus_reasons, fits_collection, can_single_deep_dive
