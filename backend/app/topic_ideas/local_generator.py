from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.hotspots.archive import archive_expired_hotspots
from app.hotspots.eligibility import is_today_recommendation_eligible
from app.hotspots.source_policy import (
    BROWSER_HOTSPOT_SOURCES,
    is_internal_test_hotspot,
    is_public_hotlist_source,
)
from app.models import HotItem, TopicIdea
from app.topic_ideas.event_scoring import (
    event_score,
    is_low_value_entertainment_item,
)
from app.topic_ideas.filter_settings import get_topic_filter_settings


TODAY_BUCKET = "today"
BACKUP_BUCKET = "backup"
LOCAL_GENERATION_SOURCE = "local_rule"
COLLECTION_EVENT_VIDEO_TYPE = "collection_event"
SINGLE_DEEP_DIVE_CANDIDATE_VIDEO_TYPE = "single_deep_dive_candidate"
MULTI_HOTSPOT_COLLECTION_VIDEO_TYPE = "multi_hotspot_collection"
DEFAULT_TODAY_SOURCE_LIMIT = 3
DEFAULT_STRICT_TODAY_SOURCE_LIMIT = 1
DEFAULT_STRICT_TODAY_CATEGORY_LIMIT = 2
DEFAULT_STRICT_CANDIDATE_CATEGORY_LIMIT = 3
MIN_GENERAL_RECOMMENDATION_SCORE = 40.0
MIN_TOPIC_GENERATION_SCORE = 40.0
DEFAULT_CATEGORY_QUOTAS = {
    "社会": 12,
    "财经": 10,
    "科技": 10,
    "消费": 10,
    "AI": 10,
    "国际": 8,
    "生活": 10,
}


@dataclass(slots=True)
class LocalTopicGenerationItem:
    hotspot_id: int
    title: str
    generated_count: int
    video_type: str
    recommendation_bucket: str
    error: str = ""


def generate_local_daily_topic_candidates(
    session: Session,
    *,
    max_hotspots: int = 12,
    today_limit: int = 5,
    now: datetime | None = None,
) -> list[LocalTopicGenerationItem]:
    archive_expired_hotspots(session, now=now)
    settings = get_topic_filter_settings(session)
    hotspot_groups = _candidate_hotspot_groups(session, limit=max_hotspots, now=now, settings=settings)
    generated: list[LocalTopicGenerationItem] = []

    for source_items in hotspot_groups:
        hot_item = source_items[0]
        idea, created = _upsert_single_topic(
            session,
            hot_item,
            source_items=source_items,
            settings=settings,
        )
        generated.append(
            LocalTopicGenerationItem(
                hotspot_id=hot_item.id,
                title=idea.title,
                generated_count=1 if created else 0,
                video_type=idea.video_type,
                recommendation_bucket=idea.recommendation_bucket,
            )
        )

    refresh_recommendation_buckets(session, today_limit=today_limit, now=now, settings=settings)
    return generated


def update_topic_recommendation_bucket(
    session: Session,
    topic_idea_id: int,
    bucket: str,
) -> TopicIdea | None:
    idea = session.get(TopicIdea, topic_idea_id)
    if idea is None:
        return None
    normalized_bucket = bucket if bucket in {TODAY_BUCKET, BACKUP_BUCKET} else BACKUP_BUCKET
    if normalized_bucket == TODAY_BUCKET and not _topic_is_today_eligible(session, idea):
        normalized_bucket = BACKUP_BUCKET
    idea.recommendation_bucket = normalized_bucket
    session.add(idea)
    session.commit()
    session.refresh(idea)
    return idea


def _candidate_hotspots(
    session: Session,
    *,
    limit: int,
    now: datetime | None = None,
) -> list[HotItem]:
    return [group[0] for group in _candidate_hotspot_groups(session, limit=limit, now=now)]


def _candidate_hotspot_groups(
    session: Session,
    *,
    limit: int,
    now: datetime | None = None,
    settings: dict[str, Any] | None = None,
) -> list[list[HotItem]]:
    settings = settings or get_topic_filter_settings(session)
    minimum_score = float(settings.get("minimum_score", MIN_TOPIC_GENERATION_SCORE))
    rows = (
        session.query(HotItem)
        .filter(HotItem.is_filtered.is_(False))
        .filter(HotItem.data_origin == "real")
        .filter(HotItem.lifecycle_status != "archived")
        .order_by(
            HotItem.topic_score.desc(),
            HotItem.hot_score.desc(),
            HotItem.save_score.desc(),
            HotItem.freshness_score.desc(),
            HotItem.id.desc(),
        )
        .all()
    )
    eligible_rows = [
        row
        for row in rows
        if not is_low_value_entertainment_item(row)
        and not is_internal_test_hotspot(row)
        and is_today_recommendation_eligible(row, now=now)
        and _recommendation_score(row, settings=settings) >= minimum_score
    ]
    grouped: dict[str, list[HotItem]] = {}
    for row in eligible_rows:
        grouped.setdefault(_event_group_key(row), []).append(row)
    groups = []
    for group in grouped.values():
        group.sort(
            key=lambda item: (
                _recommendation_score(
                    item,
                    source_count=_source_count_for_items(group),
                    settings=settings,
                ),
                item.topic_score,
                item.hot_score,
                item.id,
            ),
            reverse=True,
        )
        groups.append(group)
    groups.sort(
        key=lambda group: (
            _recommendation_score(
                group[0],
                source_count=_source_count_for_items(group),
                settings=settings,
            ),
            group[0].topic_score,
            group[0].hot_score,
            group[0].id,
        ),
        reverse=True,
    )
    return _balanced_hotspot_group_selection(groups, limit=limit)


def _upsert_single_topic(
    session: Session,
    hot_item: HotItem,
    *,
    source_items: list[HotItem] | None = None,
    settings: dict[str, Any] | None = None,
) -> tuple[TopicIdea, bool]:
    existing = (
        session.query(TopicIdea)
        .filter(TopicIdea.hot_item_id == hot_item.id)
        .filter(
            TopicIdea.video_type.in_(
                [
                    "single_deep_dive",
                    COLLECTION_EVENT_VIDEO_TYPE,
                    SINGLE_DEEP_DIVE_CANDIDATE_VIDEO_TYPE,
                ]
            )
        )
        .filter(TopicIdea.generation_source == LOCAL_GENERATION_SOURCE)
        .one_or_none()
    )
    settings = settings or get_topic_filter_settings(session)
    payload = _single_topic_payload(hot_item, source_items=source_items, settings=settings)
    if existing is not None:
        _apply_topic_payload(existing, payload)
        session.add(existing)
        for item in source_items or [hot_item]:
            item.has_topic_ideas = True
            session.add(item)
        session.commit()
        session.refresh(existing)
        return existing, False

    idea = TopicIdea(hot_item_id=hot_item.id, **payload)
    session.add(idea)
    for item in source_items or [hot_item]:
        item.has_topic_ideas = True
        session.add(item)
    session.commit()
    session.refresh(idea)
    return idea, True


def _upsert_collection_topic(
    session: Session,
    hotspots: list[HotItem],
    *,
    now: datetime,
) -> tuple[TopicIdea, bool]:
    existing = (
        session.query(TopicIdea)
        .filter(TopicIdea.video_type == "multi_hotspot_collection")
        .filter(TopicIdea.generation_source == LOCAL_GENERATION_SOURCE)
        .filter(TopicIdea.hot_item_id == hotspots[0].id)
        .one_or_none()
    )
    payload = _collection_topic_payload(hotspots, now=now)
    if existing is not None:
        _apply_topic_payload(existing, payload)
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing, False

    idea = TopicIdea(hot_item_id=hotspots[0].id, **payload)
    session.add(idea)
    for item in hotspots:
        item.has_topic_ideas = True
        session.add(item)
    session.commit()
    session.refresh(idea)
    return idea, True


def _single_topic_payload(
    hot_item: HotItem,
    *,
    source_items: list[HotItem] | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_items = source_items or [hot_item]
    source_count = _source_count_for_items(source_items)
    score = _recommendation_score(hot_item, source_count=source_count, settings=settings)
    scoring = event_score(hot_item, source_count=source_count, settings=settings)
    summary = _plain_summary(hot_item)
    can_deep_dive = scoring.can_single_deep_dive
    return {
        "title": hot_item.title,
        "core_view": summary,
        "recommended_format": "合集事件" if not can_deep_dive else "合集事件 / 可单独深挖",
        "why_watch": _why_watch(hot_item),
        "why_save": _why_save(hot_item),
        "comment_trigger": "这件事对你更像短期热闹，还是长期变化？",
        "risk_note": _risk_note(hot_item),
        "recommended_duration": "合集内 12-25 秒" if not can_deep_dive else "合集内 20-35 秒，可单独扩展",
        "hook": f"这条热点不用看太复杂，关键是看懂：{summary}",
        "video_type": (
            SINGLE_DEEP_DIVE_CANDIDATE_VIDEO_TYPE
            if can_deep_dive
            else COLLECTION_EVENT_VIDEO_TYPE
        ),
        "generation_source": LOCAL_GENERATION_SOURCE,
        "recommendation_score": score,
        "recommendation_bucket": BACKUP_BUCKET,
        "source_hotspot_ids": [item.id for item in source_items],
        "source_hotspot_titles": [item.title for item in source_items],
        "is_example": hot_item.data_origin == "sample",
    }


def compose_collection_from_events(
    session: Session,
    topic_idea_ids: list[int],
    *,
    now: datetime | None = None,
) -> TopicIdea:
    unique_ids = list(dict.fromkeys(topic_idea_ids))
    if len(unique_ids) < 1:
        raise ValueError("请至少选择 1 件事件再生成本期字幕/口播稿。")
    ideas = (
        session.query(TopicIdea)
        .filter(TopicIdea.id.in_(unique_ids))
        .all()
    )
    by_id = {idea.id: idea for idea in ideas}
    ordered_ideas = [by_id[item_id] for item_id in unique_ids if item_id in by_id]
    if len(ordered_ideas) != len(unique_ids):
        raise ValueError("有事件不存在，无法生成本期合集。")
    if any(idea.is_example or idea.generation_source != LOCAL_GENERATION_SOURCE for idea in ordered_ideas):
        raise ValueError("本期合集只能由真实公开数据生成的事件组成。")
    hotspots = _ordered_source_hotspots(session, ordered_ideas)
    if len(hotspots) < len(unique_ids):
        raise ValueError("有事件缺少可验证来源，无法生成本期合集。")
    current_time = now or datetime.now(timezone.utc)
    if not all(is_today_recommendation_eligible(item, now=current_time) for item in hotspots):
        raise ValueError("本期合集只能使用带原文链接的真实公开热点。")

    existing = _existing_collection_for_hotspots(session, hotspots)
    payload = _collection_topic_payload(
        hotspots,
        now=current_time,
        settings=get_topic_filter_settings(session),
    )
    if existing is not None:
        _apply_topic_payload(existing, payload)
        collection = existing
    else:
        collection = TopicIdea(hot_item_id=hotspots[0].id, **payload)
    collection.is_selected = True
    session.add(collection)
    for idea in ordered_ideas:
        idea.is_selected = True
        session.add(idea)
    session.commit()
    session.refresh(collection)
    return collection


def _collection_topic_payload(
    hotspots: list[HotItem],
    *,
    now: datetime,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    count = len(hotspots)
    average_score = sum(_recommendation_score(item, settings=settings) for item in hotspots) / count
    titles = [item.title for item in hotspots]
    ordered_points = "；".join(
        f"{index}. {item.title}" for index, item in enumerate(hotspots, start=1)
    )
    format_label = _collection_format_label(count)
    return {
        "title": f"今日{count}件事",
        "core_view": _collection_core_view(count),
        "recommended_format": format_label,
        "why_watch": _collection_why_watch(count),
        "why_save": "这类合集适合沉淀每日判断框架，也方便后续复盘哪些趋势连续出现。",
        "risk_note": _collection_risk_note(hotspots),
        "recommended_duration": _collection_duration(count),
        "hook": _collection_hook(count),
        "video_type": MULTI_HOTSPOT_COLLECTION_VIDEO_TYPE,
        "generation_source": LOCAL_GENERATION_SOURCE,
        "recommendation_score": min(100.0, round(average_score + 6, 2)),
        "recommendation_bucket": BACKUP_BUCKET,
        "source_hotspot_ids": [item.id for item in hotspots],
        "source_hotspot_titles": titles,
        "is_example": all(item.data_origin == "sample" for item in hotspots),
        "comment_trigger": f"今天这几件事里，你最关注哪一个？包含：{ordered_points}",
    }


def _collection_format_label(count: int) -> str:
    if count == 1:
        return "单条深挖型"
    if count == 2:
        return "双事件组合型"
    return "多热点合集型"


def _collection_core_view(count: int) -> str:
    if count == 1:
        return "围绕今天这一件最值得讲的公开热点，用大白话讲清发生了什么、为什么重要、普通人怎么看。"
    if count == 2:
        return "把今天两件重要公开热点放在一起讲清楚，重点看它们分别代表什么变化。"
    return (
        "把今天最重要的公开热点按重要性排序，用大白话讲清发生了什么、"
        "为什么重要、普通人怎么看。"
    )


def _collection_why_watch(count: int) -> str:
    if count == 1:
        return "用户可以快速看懂今天最值得单独展开的一件事。"
    return "用户不用逐条刷新闻，可以快速知道今天最值得看的几件事。"


def _collection_duration(count: int) -> str:
    if count == 1:
        return "45-75 秒"
    if count == 2:
        return "60-90 秒"
    if count > 6:
        return "90-150 秒，建议精简"
    return "60-90 秒"


def _collection_hook(count: int) -> str:
    if count == 1:
        return "今天重点看这一件事，别被标题带跑，关键是它背后的变化。"
    return "今天最值得看的几件事，不讲废话，直接说重点。"


def _ordered_source_hotspots(session: Session, ideas: list[TopicIdea]) -> list[HotItem]:
    source_ids: list[int] = []
    for idea in ideas:
        ids = idea.source_hotspot_ids or [idea.hot_item_id]
        if ids:
            source_ids.append(ids[0])
    rows = session.query(HotItem).filter(HotItem.id.in_(source_ids)).all()
    by_id = {item.id: item for item in rows}
    return [by_id[item_id] for item_id in source_ids if item_id in by_id]


def _existing_collection_for_hotspots(
    session: Session,
    hotspots: list[HotItem],
) -> TopicIdea | None:
    source_ids = [item.id for item in hotspots]
    candidates = (
        session.query(TopicIdea)
        .filter(TopicIdea.video_type == MULTI_HOTSPOT_COLLECTION_VIDEO_TYPE)
        .filter(TopicIdea.generation_source == LOCAL_GENERATION_SOURCE)
        .filter(TopicIdea.hot_item_id == hotspots[0].id)
        .all()
    )
    return next(
        (idea for idea in candidates if (idea.source_hotspot_ids or []) == source_ids),
        None,
    )


def _apply_topic_payload(idea: TopicIdea, payload: dict[str, Any]) -> None:
    for key, value in payload.items():
        setattr(idea, key, value)


def refresh_recommendation_buckets(
    session: Session,
    *,
    today_limit: int = 5,
    now: datetime | None = None,
    settings: dict[str, Any] | None = None,
) -> None:
    settings = settings or get_topic_filter_settings(session)
    today_limit = int(settings.get("current_period_limit", today_limit) or today_limit)
    window_limit = today_limit + int(settings.get("backup_pool_limit", 10) or 10)
    minimum_score = float(settings.get("minimum_score", MIN_GENERAL_RECOMMENDATION_SCORE))
    ideas = (
        session.query(TopicIdea)
        .join(HotItem, TopicIdea.hot_item_id == HotItem.id)
        .filter(HotItem.is_filtered.is_(False))
        .filter(HotItem.lifecycle_status != "archived")
        .order_by(
            TopicIdea.recommendation_score.desc(),
            HotItem.topic_score.desc(),
            HotItem.hot_score.desc(),
            TopicIdea.id.desc(),
        )
        .all()
    )
    eligible_topic_rows: list[tuple[TopicIdea, HotItem]] = []
    for idea in ideas:
        if not _is_general_event_candidate(idea):
            continue
        source_item = _primary_hotspot_for_topic(session, idea)
        if source_item is None:
            continue
        source_count = _topic_source_count(session, idea)
        idea.recommendation_score = _recommendation_score(
            source_item,
            source_count=source_count,
            settings=settings,
        )
        session.add(idea)
        if idea.recommendation_score < minimum_score:
            continue
        if source_item is not None and _topic_is_today_eligible(session, idea, now=now):
            eligible_topic_rows.append((idea, source_item))
    eligible_topic_rows.sort(
        key=lambda row: (row[0].recommendation_score, row[1].topic_score, row[1].hot_score, row[0].id),
        reverse=True,
    )
    top_window = _balanced_topic_selection(
        eligible_topic_rows,
        limit=max(today_limit, window_limit),
        source_limit=int(settings.get("source_limit", DEFAULT_TODAY_SOURCE_LIMIT) or DEFAULT_TODAY_SOURCE_LIMIT),
    )
    today_ids = {idea.id for idea in top_window[:today_limit]}
    for idea in ideas:
        if idea.id in today_ids:
            idea.recommendation_bucket = TODAY_BUCKET
        else:
            idea.recommendation_bucket = BACKUP_BUCKET
        if _is_auto_managed_event(idea):
            idea.is_selected = idea.id in today_ids
        session.add(idea)
    session.commit()


def _balanced_hotspot_group_selection(
    groups: list[list[HotItem]],
    *,
    limit: int,
) -> list[list[HotItem]]:
    selected: list[list[HotItem]] = []
    selected_ids: set[int] = set()
    category_counts: dict[str, int] = {}

    def add_group(group: list[HotItem]) -> None:
        item = group[0]
        selected.append(group)
        selected_ids.add(item.id)
        category_counts[item.category] = category_counts.get(item.category, 0) + 1

    def select_pass(
        *,
        category_limit: int | None,
        source_filter: set[str] | None = None,
        one_per_source: bool = False,
    ) -> None:
        seen_sources = {group[0].source_platform for group in selected}
        for group in groups:
            item = group[0]
            if item.id in selected_ids:
                continue
            if source_filter is not None and item.source_platform not in source_filter:
                continue
            if one_per_source and item.source_platform in seen_sources:
                continue
            if category_limit is not None and category_counts.get(item.category, 0) >= category_limit:
                continue
            if category_limit is None and not _within_category_quota(item, category_counts):
                continue
            add_group(group)
            seen_sources.add(item.source_platform)
            if len(selected) >= limit:
                return

    select_pass(
        category_limit=None,
        source_filter=BROWSER_HOTSPOT_SOURCES,
        one_per_source=True,
    )
    if len(selected) < limit:
        select_pass(
            category_limit=None,
            source_filter={source for source in {group[0].source_platform for group in groups} if is_public_hotlist_source(source)},
            one_per_source=True,
        )
    if len(selected) < limit:
        select_pass(category_limit=None)
    if len(selected) < limit:
        select_pass(category_limit=None, one_per_source=False)
    return selected


def _balanced_hotspot_selection(rows: list[HotItem], *, limit: int) -> list[HotItem]:
    return [group[0] for group in _balanced_hotspot_group_selection([[row] for row in rows], limit=limit)]


def _balanced_topic_selection(
    rows: list[tuple[TopicIdea, HotItem]],
    *,
    limit: int,
    source_limit: int = DEFAULT_TODAY_SOURCE_LIMIT,
) -> list[TopicIdea]:
    selected: list[TopicIdea] = []
    selected_ids: set[int] = set()
    source_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}

    def select_pass(*, source_limit: int, category_limit: int | None) -> None:
        for idea, hot_item in rows:
            if idea.id in selected_ids:
                continue
            if not _within_source_limit(hot_item, source_counts, limit=source_limit):
                continue
            if category_limit is not None and category_counts.get(hot_item.category, 0) >= category_limit:
                continue
            if category_limit is None and not _within_category_quota(hot_item, category_counts):
                continue
            selected.append(idea)
            selected_ids.add(idea.id)
            source_counts[hot_item.source_platform] = (
                source_counts.get(hot_item.source_platform, 0) + 1
            )
            category_counts[hot_item.category] = category_counts.get(hot_item.category, 0) + 1
            if len(selected) >= limit:
                return

    select_pass(source_limit=source_limit, category_limit=None)
    return selected


def _is_auto_managed_event(idea: TopicIdea) -> bool:
    return (
        idea.generation_source == LOCAL_GENERATION_SOURCE
        and idea.video_type
        in {
            "single_deep_dive",
            COLLECTION_EVENT_VIDEO_TYPE,
            SINGLE_DEEP_DIVE_CANDIDATE_VIDEO_TYPE,
        }
        and not idea.script_assets
    )


def _is_general_event_candidate(idea: TopicIdea) -> bool:
    return (
        idea.generation_source == LOCAL_GENERATION_SOURCE
        and idea.video_type
        in {
            "single_deep_dive",
            COLLECTION_EVENT_VIDEO_TYPE,
            SINGLE_DEEP_DIVE_CANDIDATE_VIDEO_TYPE,
        }
    )


def _topic_is_today_eligible(
    session: Session,
    idea: TopicIdea,
    *,
    now: datetime | None = None,
) -> bool:
    if idea.is_example or idea.generation_source != LOCAL_GENERATION_SOURCE:
        return False
    source_ids = idea.source_hotspot_ids or [idea.hot_item_id]
    if not source_ids:
        return False
    hotspots = session.query(HotItem).filter(HotItem.id.in_(source_ids)).all()
    if len(hotspots) != len(set(source_ids)):
        return False
    if any(is_internal_test_hotspot(item) for item in hotspots):
        return False
    return all(is_today_recommendation_eligible(item, now=now) for item in hotspots)


def _primary_hotspot_for_topic(session: Session, idea: TopicIdea) -> HotItem | None:
    source_ids = idea.source_hotspot_ids or [idea.hot_item_id]
    if not source_ids:
        return None
    return session.get(HotItem, source_ids[0])


def _within_source_limit(
    hot_item: HotItem,
    source_counts: dict[str, int],
    *,
    limit: int = DEFAULT_TODAY_SOURCE_LIMIT,
) -> bool:
    return source_counts.get(hot_item.source_platform, 0) < limit


def _within_category_quota(
    hot_item: HotItem,
    category_counts: dict[str, int],
    *,
    quotas: dict[str, int] = DEFAULT_CATEGORY_QUOTAS,
) -> bool:
    quota = quotas.get(hot_item.category)
    if quota is None:
        return True
    return category_counts.get(hot_item.category, 0) < quota


def _recommendation_score(
    hot_item: HotItem,
    *,
    source_count: int = 1,
    settings: dict[str, Any] | None = None,
) -> float:
    return event_score(hot_item, source_count=source_count, settings=settings).total


def _source_count_for_items(items: list[HotItem]) -> int:
    return len({item.source_platform for item in items if item.source_platform}) or 1


def _topic_source_count(session: Session, idea: TopicIdea) -> int:
    source_ids = idea.source_hotspot_ids or [idea.hot_item_id]
    if not source_ids:
        return 1
    rows = session.query(HotItem.source_platform).filter(HotItem.id.in_(source_ids)).all()
    return len({row[0] for row in rows if row[0]}) or 1


def _event_group_key(hot_item: HotItem) -> str:
    title = hot_item.original_title if hasattr(hot_item, "original_title") else ""
    raw = (title or hot_item.title or "").lower()
    cleaned = re.sub(r"https?://\S+", "", raw)
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", cleaned, flags=re.UNICODE)
    return cleaned or f"hotspot-{hot_item.id}"


def _plain_summary(hot_item: HotItem) -> str:
    summary = (hot_item.summary or "").strip()
    if summary:
        return summary[:180]
    return f"这件事来自 {hot_item.source_platform}，重点不在新闻细节，而在它可能代表的趋势变化。"


def _why_watch(hot_item: HotItem) -> str:
    return (
        f"它和{hot_item.category or '趋势'}变化有关，适合用短视频快速讲清发生了什么和为什么值得注意。"
    )


def _why_save(hot_item: HotItem) -> str:
    if hot_item.save_score >= 75:
        return "收藏价值较高，可以沉淀成判断同类趋势、工具或商业变化的参考框架。"
    return "可以作为观察后续变化的线索，不需要讲得过深，讲清核心影响即可。"


def _risk_note(hot_item: HotItem) -> str:
    if hot_item.risk_score >= 60:
        return "风险较高，只适合客观转述，不做立场判断和操作建议。"
    if hot_item.risk_score >= 30:
        return "注意避免绝对化判断，不延伸到敏感争议。"
    return "风险较低，仍需避免投资建议和未经证实的结论。"


def _collection_risk_note(hotspots: list[HotItem]) -> str:
    max_risk = max((item.risk_score for item in hotspots), default=0)
    if max_risk >= 60:
        return "合集里有高风险热点，建议弱化争议，只做事实转译。"
    if max_risk >= 30:
        return "合集里有中等风险热点，避免站队和过度预测。"
    return "整体风险较低，注意保持客观转译，不做投资或平台操作建议。"
