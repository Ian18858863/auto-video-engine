from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any

from sqlalchemy.orm import Session

from app.copywriting_samples.schemas import CopywritingSamplePayload
from app.models import CopywritingSample


REFERENCE_RULE = "只学习表达方式和结构，不直接复制原文；如与账号风格库冲突，以账号风格库为准。"

SAMPLE_TYPES = [
    "开头钩子",
    "标题",
    "封面文案",
    "口播片段",
    "结尾引导",
    "收藏清单结构",
    "小白转译表达",
    "商业逻辑拆解表达",
    "风口避坑表达",
    "趋势解释表达",
    "发布文案",
]

SAMPLE_SCENES = [
    "热点解释",
    "清单型内容",
    "小白转译",
    "风口避坑",
    "商业逻辑拆解",
    "AI 工具介绍",
    "财经新闻转译",
    "趋势判断",
    "数据复盘",
    "评论区回应",
]


class CopywritingSampleNotFoundError(ValueError):
    pass


@dataclass(slots=True)
class CopywritingSampleListResult:
    items: list[CopywritingSample]
    page: int
    page_size: int
    total: int
    sample_types: list[str]
    scenes: list[str]
    content_directions: list[str]
    tags: list[str]

    @property
    def total_pages(self) -> int:
        return ceil(self.total / self.page_size) if self.total else 0


def create_copywriting_sample(
    session: Session,
    payload: CopywritingSamplePayload,
) -> CopywritingSample:
    item = CopywritingSample(**_payload_values(payload))
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def update_copywriting_sample(
    session: Session,
    sample_id: int,
    payload: CopywritingSamplePayload,
) -> CopywritingSample:
    item = get_copywriting_sample(session, sample_id)
    values = _payload_values(payload)
    for key, value in values.items():
        setattr(item, key, value)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def delete_copywriting_sample(session: Session, sample_id: int) -> None:
    item = get_copywriting_sample(session, sample_id)
    session.delete(item)
    session.commit()


def get_copywriting_sample(session: Session, sample_id: int) -> CopywritingSample:
    item = session.get(CopywritingSample, sample_id)
    if item is None:
        raise CopywritingSampleNotFoundError(f"文案样本不存在：{sample_id}")
    return item


def list_copywriting_samples(
    session: Session,
    *,
    sample_type: str | None = None,
    scene: str | None = None,
    content_direction: str | None = None,
    tag: str | None = None,
    keyword: str | None = None,
    enabled_only: bool = False,
    key_reference_only: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> CopywritingSampleListResult:
    query = session.query(CopywritingSample)
    if sample_type:
        query = query.filter(CopywritingSample.sample_type == sample_type)
    if scene:
        query = query.filter(CopywritingSample.scene == scene)
    if content_direction:
        query = query.filter(CopywritingSample.content_direction == content_direction)
    if enabled_only:
        query = query.filter(CopywritingSample.enabled.is_(True))
    if key_reference_only:
        query = query.filter(CopywritingSample.is_key_reference.is_(True))

    all_items = (
        query.order_by(
            CopywritingSample.is_key_reference.desc(),
            CopywritingSample.updated_at.desc(),
            CopywritingSample.id.desc(),
        )
        .all()
    )
    filtered_items = [
        item
        for item in all_items
        if _matches_tag(item, tag) and _matches_keyword(item, keyword)
    ]
    total = len(filtered_items)
    start = (page - 1) * page_size
    items = filtered_items[start : start + page_size]

    return CopywritingSampleListResult(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
        sample_types=_distinct_values(session, CopywritingSample.sample_type, SAMPLE_TYPES),
        scenes=_distinct_values(session, CopywritingSample.scene, SAMPLE_SCENES),
        content_directions=_distinct_values(session, CopywritingSample.content_direction, []),
        tags=_distinct_tags(session),
    )


def get_copywriting_sample_context(
    session: Session,
    *,
    limit: int = 6,
) -> dict[str, Any]:
    items = (
        session.query(CopywritingSample)
        .filter(CopywritingSample.enabled.is_(True))
        .order_by(
            CopywritingSample.is_key_reference.desc(),
            CopywritingSample.updated_at.desc(),
            CopywritingSample.id.desc(),
        )
        .limit(limit)
        .all()
    )
    return {
        "source": "copywriting_sample_library",
        "reference_rule": REFERENCE_RULE,
        "account_style_priority": "账号风格库是最高优先级，样本只作为表达方式参考。",
        "samples": [
            {
                "sample_type": item.sample_type,
                "scene": item.scene,
                "content_direction": item.content_direction,
                "structure_type": item.structure_type,
                "why_reference": item.why_reference,
                "reusable_points": item.reusable_points,
                "avoid_points": item.avoid_points,
                "tags": item.tags or [],
                "is_key_reference": item.is_key_reference,
                "text_excerpt": _short_text(item.sample_text, 120),
            }
            for item in items
        ],
    }


def serialize_copywriting_sample(item: CopywritingSample) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "sample_type": item.sample_type,
        "sample_text": item.sample_text,
        "scene": item.scene,
        "content_direction": item.content_direction,
        "structure_type": item.structure_type,
        "why_reference": item.why_reference,
        "reusable_points": item.reusable_points,
        "avoid_points": item.avoid_points,
        "tags": item.tags or [],
        "enabled": item.enabled,
        "is_key_reference": item.is_key_reference,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "notes": item.notes,
    }


def serialize_copywriting_sample_list(result: CopywritingSampleListResult) -> dict[str, Any]:
    return {
        "items": [serialize_copywriting_sample(item) for item in result.items],
        "pagination": {
            "page": result.page,
            "page_size": result.page_size,
            "total": result.total,
            "total_pages": result.total_pages,
        },
        "filters": {
            "sample_types": result.sample_types,
            "scenes": result.scenes,
            "content_directions": result.content_directions,
            "tags": result.tags,
        },
        "reference_rule": REFERENCE_RULE,
        "style_priority": "账号风格库优先；样本库只做参考。",
        "real_ai_called": False,
    }


def _payload_values(payload: CopywritingSamplePayload) -> dict[str, Any]:
    title = _clean_text(payload.title) or "未命名文案样本"
    return {
        "title": title[:300],
        "sample_type": _clean_text(payload.sample_type)[:80],
        "sample_text": _clean_text(payload.sample_text),
        "scene": _clean_text(payload.scene)[:100],
        "content_direction": _clean_text(payload.content_direction)[:120],
        "structure_type": _clean_text(payload.structure_type)[:120],
        "why_reference": _clean_text(payload.why_reference),
        "reusable_points": _clean_text(payload.reusable_points),
        "avoid_points": _clean_text(payload.avoid_points),
        "tags": _clean_tags(payload.tags),
        "enabled": bool(payload.enabled),
        "is_key_reference": bool(payload.is_key_reference),
        "notes": _clean_text(payload.notes),
    }


def _matches_tag(item: CopywritingSample, tag: str | None) -> bool:
    if not tag:
        return True
    normalized = tag.strip().casefold()
    return any(str(item_tag).strip().casefold() == normalized for item_tag in item.tags or [])


def _matches_keyword(item: CopywritingSample, keyword: str | None) -> bool:
    if not keyword:
        return True
    target = keyword.strip().casefold()
    haystack = "\n".join(
        [
            item.title,
            item.sample_text,
            item.why_reference,
            item.reusable_points,
            item.avoid_points,
            item.notes,
        ]
    ).casefold()
    return target in haystack


def _distinct_values(session: Session, column: Any, defaults: list[str]) -> list[str]:
    values = [value for (value,) in session.query(column).distinct().all() if value]
    combined = [*defaults, *values]
    return sorted(dict.fromkeys(combined))


def _distinct_tags(session: Session) -> list[str]:
    tags: list[str] = []
    for (value,) in session.query(CopywritingSample.tags).all():
        if isinstance(value, list):
            tags.extend(str(item).strip() for item in value if str(item).strip())
    return sorted(dict.fromkeys(tags))


def _clean_tags(tags: list[str]) -> list[str]:
    cleaned = [str(tag).strip() for tag in tags if str(tag).strip()]
    return list(dict.fromkeys(cleaned))[:12]


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _short_text(value: str, max_length: int) -> str:
    text = _clean_text(value)
    return text if len(text) <= max_length else f"{text[:max_length]}..."


def _iso(value: Any) -> str:
    return value.isoformat() if value else ""
