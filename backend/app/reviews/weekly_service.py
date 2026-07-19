from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.reviews.service import list_video_reviews, serialize_video_review


def build_weekly_review_report(
    session: Session,
    *,
    now: datetime | None = None,
    pipeline_id: int | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    start = current - timedelta(days=7)
    result = list_video_reviews(
        session,
        pipeline_id=pipeline_id,
        published_from=start,
        published_to=current,
        page=1,
        page_size=1000,
        now=current,
    )
    items = result.items
    high_favorites = sorted(items, key=lambda item: item.favorite_rate, reverse=True)[:5]
    high_shares = sorted(items, key=lambda item: item.share_rate, reverse=True)[:5]
    long_tail = [
        item
        for item in items
        if item.growth_trend in {"长尾增长", "加速"} or item.judgement in {"有潜力", "值得继续做"}
    ][:5]
    stalled = [
        item
        for item in items
        if item.growth_trend in {"停滞", "放缓"} or item.judgement in {"需要调整", "不建议继续"}
    ][:5]
    continue_topics = [
        item for item in items if item.judgement in {"值得继续做", "有潜力"}
    ][:5]
    pause_topics = [
        item for item in items if item.judgement in {"需要调整", "不建议继续"}
    ][:5]
    has_published_data = bool(items)
    return {
        "period": {
            "start": start.isoformat(),
            "end": current.isoformat(),
        },
        "published_count": len(items),
        "data_note": (
            "最近 7 天没有发布数据，无法判断趋势、因果或下周方向。"
            if not has_published_data
            else (
                "当前发布数据不足，周报仅供参考。"
                if len(items) < 3
                else "基于最近 7 天手动录入数据生成。"
            )
        ),
        "published_videos": [serialize_video_review(item) for item in items],
        "high_favorite_videos": [serialize_video_review(item) for item in high_favorites],
        "high_share_videos": [serialize_video_review(item) for item in high_shares],
        "long_tail_videos": [serialize_video_review(item) for item in long_tail],
        "stalled_videos": [serialize_video_review(item) for item in stalled],
        "continue_topics": [serialize_video_review(item) for item in continue_topics],
        "pause_topics": [serialize_video_review(item) for item in pause_topics],
        "next_week_do": (
            _next_week_do(continue_topics, high_favorites, long_tail)
            if has_published_data
            else []
        ),
        "next_week_avoid": (
            _next_week_avoid(pause_topics, stalled) if has_published_data else []
        ),
        "local_rules_only": True,
        "real_ai_called": False,
    }


def _next_week_do(continue_topics: list, high_favorites: list, long_tail: list) -> list[str]:
    suggestions: list[str] = []
    if continue_topics:
        suggestions.append("优先延展“有潜力 / 值得继续做”的同类选题。")
    if high_favorites:
        suggestions.append("保留收藏率高的视频结构，多做判断框架和清单型内容。")
    if long_tail:
        suggestions.append("关注 24 小时后仍增长的题材，适合做系列或补充篇。")
    return suggestions or ["先补充发布数据快照，再判断下周方向。"]


def _next_week_avoid(pause_topics: list, stalled: list) -> list[str]:
    suggestions: list[str] = []
    if pause_topics:
        suggestions.append("暂缓继续做“需要调整 / 不建议继续”的同类表达。")
    if stalled:
        suggestions.append("掉速视频先复查开头和信息密度，不要直接复制同一结构。")
    return suggestions or ["暂时没有明显需要避开的方向。"]
