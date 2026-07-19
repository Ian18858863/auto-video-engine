from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from typing import Any

from sqlalchemy.orm import Session

from app.hotspots.cleaning import public_url
from app.hotspots.eligibility import source_credibility
from app.hotspots.source_policy import is_internal_test_hotspot
from app.models import HotItem, ProductionPipeline

CANONICAL_CATEGORIES = ["社会", "财经", "科技", "消费", "AI", "国际", "生活", "娱乐"]


@dataclass(slots=True)
class HotspotListItem:
    id: int
    original_title: str
    original_url: str
    source_platform: str
    source_type: str
    title: str
    url: str
    published_at: datetime | None
    fetched_at: datetime
    summary: str
    metrics: dict[str, Any]
    tags: list[str]
    category: str
    data_origin: str
    origin_note: str
    is_filtered: bool
    filter_reason: str
    hot_score: float
    topic_score: float
    save_score: float
    risk_score: float
    freshness_score: float
    duplicate_reason: str
    source_credibility: str
    source_credibility_reason: str
    has_project: bool


@dataclass(slots=True)
class HotspotListResult:
    items: list[HotspotListItem]
    total: int
    page: int
    page_size: int
    total_pages: int
    categories: list[str]
    sources: list[str]
    category_counts: dict[str, int]
    source_counts: dict[str, int]


def list_hotspots(
    session: Session,
    *,
    category: str | None = None,
    source_platform: str | None = None,
    unfiltered_only: bool = False,
    sort_by: str = "topic_score",
    page: int = 1,
    page_size: int = 20,
) -> HotspotListResult:
    query = session.query(HotItem)
    if unfiltered_only:
        query = query.filter(HotItem.is_filtered.is_(False))
    base_rows = [item for item in query.all() if not is_internal_test_hotspot(item)]

    category_scope_rows = [
        item
        for item in base_rows
        if not source_platform or item.source_platform == source_platform
    ]
    source_scope_rows = [
        item
        for item in base_rows
        if not category or _canonical_category(item) == category
    ]
    filtered_rows = [
        item
        for item in base_rows
        if (not source_platform or item.source_platform == source_platform)
        and (not category or _canonical_category(item) == category)
    ]

    filtered_rows.sort(key=lambda item: _sort_key(item, sort_by), reverse=True)
    if not source_platform:
        filtered_rows = _source_balanced_order(filtered_rows, page_size=page_size)
    total = len(filtered_rows)
    start = (page - 1) * page_size
    items = filtered_rows[start : start + page_size]
    project_hotspot_ids = _project_hotspot_ids(
        session,
        [item.id for item in items],
    )
    return HotspotListResult(
        items=[
            _to_list_item(item, item.id in project_hotspot_ids) for item in items
        ],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=ceil(total / page_size) if total else 0,
        categories=CANONICAL_CATEGORIES,
        sources=sorted(_source_counts(source_scope_rows)),
        category_counts=_category_counts(category_scope_rows),
        source_counts=_source_counts(source_scope_rows),
    )


def get_hotspot(session: Session, hotspot_id: int) -> HotspotListItem | None:
    item = session.get(HotItem, hotspot_id)
    if item is None:
        return None
    return _to_list_item(
        item,
        bool(
            session.query(ProductionPipeline.id)
            .filter(ProductionPipeline.hotspot_id == hotspot_id)
            .first()
        ),
    )


def serialize_hotspot(item: HotspotListItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "original_title": item.original_title,
        "original_url": item.original_url,
        "source_platform": item.source_platform,
        "source_type": item.source_type,
        "title": item.title,
        "url": item.url,
        "published_at": _isoformat(item.published_at),
        "fetched_at": _isoformat(item.fetched_at),
        "summary": item.summary,
        "metrics": item.metrics,
        "tags": item.tags,
        "category": item.category,
        "data_origin": item.data_origin,
        "origin_note": item.origin_note,
        "is_filtered": item.is_filtered,
        "filter_reason": item.filter_reason,
        "hot_score": item.hot_score,
        "topic_score": item.topic_score,
        "save_score": item.save_score,
        "risk_score": item.risk_score,
        "freshness_score": item.freshness_score,
        "duplicate_reason": item.duplicate_reason,
        "source_credibility": item.source_credibility,
        "source_credibility_reason": item.source_credibility_reason,
        "has_project": item.has_project,
    }


def serialize_hotspot_list(result: HotspotListResult) -> dict[str, Any]:
    return {
        "items": [serialize_hotspot(item) for item in result.items],
        "pagination": {
            "page": result.page,
            "page_size": result.page_size,
            "total": result.total,
            "total_pages": result.total_pages,
        },
        "filters": {
            "categories": result.categories,
            "sources": result.sources,
            "category_counts": result.category_counts,
            "source_counts": result.source_counts,
        },
    }


def _to_list_item(item: HotItem, has_project: bool) -> HotspotListItem:
    visible_url = public_url(item.url)
    credibility, credibility_reason = source_credibility(
        url=visible_url,
        published_at=item.published_at,
        summary=item.summary,
    )
    return HotspotListItem(
        id=item.id,
        original_title=item.title,
        original_url=visible_url,
        source_platform=item.source_platform,
        source_type=item.source_type,
        title=item.title,
        url=visible_url,
        published_at=item.published_at,
        fetched_at=item.fetched_at,
        summary=item.summary,
        metrics=item.metrics or {},
        tags=item.tags or [],
        category=item.category,
        data_origin=item.data_origin or "real",
        origin_note=item.origin_note or "",
        is_filtered=item.is_filtered,
        filter_reason=item.filter_reason,
        hot_score=item.hot_score,
        topic_score=item.topic_score,
        save_score=item.save_score,
        risk_score=item.risk_score,
        freshness_score=item.freshness_score,
        duplicate_reason=item.duplicate_reason or "",
        source_credibility=credibility,
        source_credibility_reason=credibility_reason,
        has_project=has_project,
    )


def _project_hotspot_ids(session: Session, hotspot_ids: list[int]) -> set[int]:
    if not hotspot_ids:
        return set()
    rows = (
        session.query(ProductionPipeline.hotspot_id)
        .filter(ProductionPipeline.hotspot_id.in_(hotspot_ids))
        .distinct()
        .all()
    )
    return {row[0] for row in rows}


def _category_counts(items: list[HotItem]) -> dict[str, int]:
    counts = {category: 0 for category in CANONICAL_CATEGORIES}
    for item in items:
        counts[_canonical_category(item)] += 1
    return counts


def _source_counts(items: list[HotItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        if item.source_platform:
            counts[item.source_platform] = counts.get(item.source_platform, 0) + 1
    return dict(sorted(counts.items()))


def _canonical_category(item: HotItem) -> str:
    category = (item.category or "").strip()
    if category in CANONICAL_CATEGORIES:
        return category
    if category in {"商业", "金融"}:
        return "财经"
    if category in {"互联网趋势", "工具", "产品"}:
        return "科技"
    if category in {"游戏", "电影", "体育", "八卦"}:
        return "娱乐"
    text = f"{category} {item.title} {item.summary} {' '.join(item.tags or [])}"
    if _has_any(text, ["社会", "公共服务", "就业", "工作", "民生", "规则调整", "监管", "安全"]):
        return "社会"
    if _has_any(text, ["AI", "人工智能", "大模型", "智能体", "Agent", "OpenAI", "DeepSeek", "豆包"]):
        return "AI"
    if _has_any(text, ["财经", "金融", "银行", "央行", "股市", "基金", "房贷", "楼市", "利率", "价格", "涨价", "降价", "通胀"]):
        return "财经"
    if _has_any(text, ["科技", "芯片", "机器人", "自动驾驶", "智驾", "手机", "新能源", "硬件", "系统"]):
        return "科技"
    if _has_any(text, ["消费", "电商", "外卖", "零售", "补贴", "用户", "品牌", "门店", "商家"]):
        return "消费"
    if _has_any(text, ["国际", "全球", "美国", "欧洲", "日本", "韩国", "海外", "关税", "出口"]):
        return "国际"
    if _has_any(text, ["生活", "教育", "医疗", "出行", "住房", "通勤", "旅游", "养老", "医保"]):
        return "生活"
    if _has_any(text, ["电影", "上映", "明星", "综艺", "游戏", "电竞", "体育", "比赛", "八卦"]):
        return "娱乐"
    return "社会"


def _has_any(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _sort_key(item: HotItem, sort_by: str) -> tuple[Any, datetime, int]:
    sort_values = {
        "hot_score": item.hot_score,
        "topic_score": item.topic_score,
        "save_score": item.save_score,
        "risk_score": item.risk_score,
        "freshness_score": item.freshness_score,
        "published_at": item.published_at,
    }
    primary = sort_values.get(sort_by, item.topic_score)
    if isinstance(primary, datetime):
        primary_value: Any = _sort_datetime(primary)
    else:
        primary_value = float(primary or 0)
    return (primary_value, _sort_datetime(item.published_at), item.id)


def _sort_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _source_balanced_order(items: list[HotItem], *, page_size: int) -> list[HotItem]:
    if not items:
        return []
    first_page_source_limit = max(2, min(5, page_size // 4 or 2))
    remaining = list(items)
    ordered: list[HotItem] = []
    selected_ids: set[int] = set()
    source_counts: dict[str, int] = {}

    def add_pass(*, limit: int | None) -> None:
        for item in remaining:
            if item.id in selected_ids:
                continue
            if limit is not None and source_counts.get(item.source_platform, 0) >= limit:
                continue
            ordered.append(item)
            selected_ids.add(item.id)
            source_counts[item.source_platform] = source_counts.get(item.source_platform, 0) + 1

    add_pass(limit=1)
    add_pass(limit=first_page_source_limit)
    add_pass(limit=None)
    return ordered


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
