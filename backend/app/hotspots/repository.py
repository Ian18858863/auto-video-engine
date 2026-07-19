from __future__ import annotations

from difflib import SequenceMatcher

from sqlalchemy.orm import Session

from app.hotspots.cleaning import clean_candidate, make_internal_manual_url, title_fingerprint
from app.hotspots.filters import evaluate_filter
from app.hotspots.schemas import HotspotCandidate
from app.hotspots.scoring import score_hotspot
from app.models import HotItem
from app.models.entities import utc_now


def save_hotspot(session: Session, candidate: HotspotCandidate) -> tuple[HotItem, bool]:
    cleaned = clean_candidate(candidate)
    if cleaned.url:
        existing = session.query(HotItem).filter(HotItem.url == cleaned.url).one_or_none()
        if existing is not None:
            existing.duplicate_reason = f"重复链接：{cleaned.source_platform}"
            _reactivate_duplicate(existing, cleaned)
            session.add(existing)
            session.commit()
            return existing, False
    duplicate_key = title_fingerprint(cleaned.title)
    similar = _find_similar_title(session, cleaned.title, duplicate_key)
    if similar is not None:
        similar.duplicate_reason = f"相似标题合并：{cleaned.source_platform}《{cleaned.title}》"
        _reactivate_duplicate(similar, cleaned)
        session.add(similar)
        session.commit()
        return similar, False

    filter_result = evaluate_filter(cleaned)
    scores = score_hotspot(cleaned)
    stored_url = cleaned.url or make_internal_manual_url()
    if filter_result.is_filtered:
        scores.hot_score = min(scores.hot_score, 20)
        scores.topic_score = 0
        scores.save_score = 0

    hot_item = HotItem(
        source_platform=cleaned.source_platform,
        source_type=cleaned.source_type,
        title=cleaned.title,
        url=stored_url,
        published_at=cleaned.published_at,
        summary=cleaned.summary,
        metrics=cleaned.metrics,
        tags=cleaned.tags,
        category=cleaned.category,
        is_filtered=filter_result.is_filtered,
        filter_reason=filter_result.reason,
        hot_score=scores.hot_score,
        topic_score=scores.topic_score,
        save_score=scores.save_score,
        risk_score=scores.risk_score,
        freshness_score=scores.freshness_score,
        data_origin=_data_origin(cleaned),
        origin_note=cleaned.origin_note,
        duplicate_group_key=duplicate_key,
        lifecycle_status="active",
    )
    session.add(hot_item)
    session.commit()
    session.refresh(hot_item)
    return hot_item, True


def _find_similar_title(
    session: Session,
    title: str,
    duplicate_key: str,
) -> HotItem | None:
    if not duplicate_key:
        return None
    direct = (
        session.query(HotItem)
        .filter(HotItem.duplicate_group_key == duplicate_key)
        .order_by(HotItem.id.desc())
        .first()
    )
    if direct is not None:
        return direct

    rows = (
        session.query(HotItem)
        .order_by(HotItem.fetched_at.desc(), HotItem.id.desc())
        .limit(300)
        .all()
    )
    incoming = duplicate_key
    for item in rows:
        existing = item.duplicate_group_key or title_fingerprint(item.title)
        if not existing:
            continue
        if SequenceMatcher(None, incoming, existing).ratio() >= 0.9:
            return item
    return None


def _data_origin(candidate: HotspotCandidate) -> str:
    if candidate.source_platform == "示例数据" or candidate.data_origin == "sample":
        return "sample"
    if candidate.source_type == "manual" or candidate.data_origin == "manual":
        return "manual"
    return "real"


def _reactivate_duplicate(item: HotItem, candidate: HotspotCandidate) -> None:
    item.fetched_at = utc_now()
    if candidate.published_at and not item.published_at:
        item.published_at = candidate.published_at
    if candidate.summary and not item.summary:
        item.summary = candidate.summary
    item.lifecycle_status = "active"
    item.archived_at = None
    item.archive_reason = ""
