from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import HotItem, ProductionPipeline, ScriptAsset, TopicIdea
from app.models.entities import utc_now


ACTIVE_STATUS = "active"
ARCHIVED_STATUS = "archived"
DEFAULT_ACTIVE_WINDOW_HOURS = 36


@dataclass(slots=True)
class HotspotArchiveResult:
    archived_count: int
    preserved_count: int
    cutoff_at: datetime


def archive_expired_hotspots(
    session: Session,
    *,
    now: datetime | None = None,
    active_window_hours: int = DEFAULT_ACTIVE_WINDOW_HOURS,
) -> HotspotArchiveResult:
    current_time = _aware(now or utc_now())
    cutoff_at = current_time - timedelta(hours=max(active_window_hours, 1))
    protected_ids = _protected_hotspot_ids(session)
    rows = (
        session.query(HotItem)
        .filter(HotItem.lifecycle_status != ARCHIVED_STATUS)
        .order_by(HotItem.fetched_at.asc(), HotItem.id.asc())
        .all()
    )
    archived_count = 0
    preserved_count = 0
    for item in rows:
        item_time = _aware(item.published_at or item.fetched_at)
        if item_time >= cutoff_at:
            continue
        if item.id in protected_ids:
            preserved_count += 1
            continue
        item.lifecycle_status = ARCHIVED_STATUS
        item.archived_at = current_time
        item.archive_reason = f"超过 {active_window_hours} 小时未选入本期，自动归档。"
        _move_unprotected_topic_ideas_to_backup(session, item)
        session.add(item)
        archived_count += 1

    if archived_count:
        session.commit()
    return HotspotArchiveResult(
        archived_count=archived_count,
        preserved_count=preserved_count,
        cutoff_at=cutoff_at,
    )


def _protected_hotspot_ids(session: Session) -> set[int]:
    protected: set[int] = set()
    selected_ideas = (
        session.query(TopicIdea)
        .filter(TopicIdea.is_selected.is_(True))
        .all()
    )
    scripted_ideas = (
        session.query(TopicIdea)
        .join(ScriptAsset, ScriptAsset.topic_idea_id == TopicIdea.id)
        .all()
    )
    pipeline_hotspot_ids = [
        item_id
        for (item_id,) in session.query(ProductionPipeline.hotspot_id).all()
        if item_id is not None
    ]
    pipeline_ideas = (
        session.query(TopicIdea)
        .join(ProductionPipeline, ProductionPipeline.topic_idea_id == TopicIdea.id)
        .all()
    )
    for idea in [*selected_ideas, *scripted_ideas, *pipeline_ideas]:
        protected.update(_idea_hotspot_ids(idea))
    protected.update(pipeline_hotspot_ids)
    return protected


def _move_unprotected_topic_ideas_to_backup(session: Session, item: HotItem) -> None:
    ideas = (
        session.query(TopicIdea)
        .filter(TopicIdea.hot_item_id == item.id)
        .filter(TopicIdea.is_selected.is_(False))
        .all()
    )
    for idea in ideas:
        idea.recommendation_bucket = "backup"
        session.add(idea)


def _idea_hotspot_ids(idea: TopicIdea) -> set[int]:
    ids = set(idea.source_hotspot_ids or [])
    ids.add(idea.hot_item_id)
    return {item_id for item_id in ids if item_id is not None}


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
