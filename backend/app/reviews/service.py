from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import ceil
from typing import Any

from sqlalchemy.orm import Session

from app.publishing.service import (
    ProjectOption,
    SnapshotItem,
    VideoPostItem,
    get_video_post,
    list_video_posts,
)


JUDGEMENTS = ["表现一般", "有潜力", "值得继续做", "需要调整", "不建议继续"]


@dataclass
class VideoReviewItem:
    video_post_id: int
    pipeline_id: int | None
    project_name: str
    video_title: str
    platform: str
    published_at: datetime | None
    views: int
    likes: int
    comments: int
    favorites: int
    shares: int
    followers_gained: int
    completion_rate: float
    average_watch_seconds: float
    favorite_rate: float
    share_rate: float
    follower_rate: float
    view_velocity: float
    growth_trend: str
    judgement: str
    reasons: list[str]
    next_suggestion: str
    snapshot_count: int
    snapshots: list[SnapshotItem] = field(default_factory=list)


@dataclass
class VideoReviewListResult:
    items: list[VideoReviewItem]
    total: int
    page: int
    page_size: int
    total_pages: int
    platforms: list[str]
    projects: list[ProjectOption]


def get_video_review(
    session: Session,
    video_post_id: int,
    *,
    now: datetime | None = None,
) -> VideoReviewItem | None:
    current_time = _aware(now or datetime.now(timezone.utc))
    post = get_video_post(session, video_post_id, now=current_time)
    if post is None:
        return None
    return _build_review(post, now=current_time)


def list_video_reviews(
    session: Session,
    *,
    platform: str | None = None,
    pipeline_id: int | None = None,
    published_from: datetime | None = None,
    published_to: datetime | None = None,
    judgement: str | None = None,
    sort_by: str = "published_at",
    page: int = 1,
    page_size: int = 20,
    now: datetime | None = None,
) -> VideoReviewListResult:
    current_time = _aware(now or datetime.now(timezone.utc))
    source = list_video_posts(
        session,
        platform=platform,
        pipeline_id=pipeline_id,
        page=1,
        page_size=100_000,
        now=current_time,
    )
    reviews = [_build_review(item, now=current_time) for item in source.items]
    if published_from:
        start = _aware(published_from)
        reviews = [
            item
            for item in reviews
            if item.published_at is not None and item.published_at >= start
        ]
    if published_to:
        end = _aware(published_to)
        reviews = [
            item
            for item in reviews
            if item.published_at is not None and item.published_at <= end
        ]
    if judgement:
        reviews = [item for item in reviews if item.judgement == judgement]

    sort_key = {
        "favorite_rate": lambda item: item.favorite_rate,
        "share_rate": lambda item: item.share_rate,
        "follower_rate": lambda item: item.follower_rate,
        "view_velocity": lambda item: item.view_velocity,
        "published_at": lambda item: _aware(item.published_at or datetime.min),
    }.get(sort_by, lambda item: _aware(item.published_at or datetime.min))
    reviews.sort(key=sort_key, reverse=True)
    total = len(reviews)
    start_index = (page - 1) * page_size
    return VideoReviewListResult(
        items=reviews[start_index : start_index + page_size],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=ceil(total / page_size) if total else 0,
        platforms=source.platforms,
        projects=source.projects,
    )


def serialize_video_review(item: VideoReviewItem) -> dict[str, Any]:
    return {
        "video_post_id": item.video_post_id,
        "pipeline_id": item.pipeline_id,
        "project_name": item.project_name,
        "video_title": item.video_title,
        "platform": item.platform,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "views": item.views,
        "likes": item.likes,
        "comments": item.comments,
        "favorites": item.favorites,
        "shares": item.shares,
        "followers_gained": item.followers_gained,
        "completion_rate": item.completion_rate,
        "average_watch_seconds": item.average_watch_seconds,
        "favorite_rate": item.favorite_rate,
        "share_rate": item.share_rate,
        "follower_rate": item.follower_rate,
        "view_velocity": item.view_velocity,
        "growth_trend": item.growth_trend,
        "judgement": item.judgement,
        "reasons": item.reasons,
        "next_suggestion": item.next_suggestion,
        "snapshot_count": item.snapshot_count,
        "snapshots": [
            {
                "id": snapshot.id,
                "captured_at": snapshot.captured_at.isoformat(),
                "elapsed_minutes": snapshot.elapsed_minutes,
                "views": snapshot.views,
                "likes": snapshot.likes,
                "comments": snapshot.comments,
                "favorites": snapshot.favorites,
                "shares": snapshot.shares,
                "followers_gained": snapshot.followers_gained,
                "completion_rate": snapshot.completion_rate,
                "average_watch_seconds": snapshot.average_watch_seconds,
                "notes": snapshot.notes,
            }
            for snapshot in item.snapshots
        ],
    }


def serialize_video_review_list(result: VideoReviewListResult) -> dict[str, Any]:
    return {
        "items": [serialize_video_review(item) for item in result.items],
        "pagination": {
            "page": result.page,
            "page_size": result.page_size,
            "total": result.total,
            "total_pages": result.total_pages,
        },
        "filters": {
            "platforms": result.platforms,
            "projects": [
                {"id": project.id, "title": project.title}
                for project in result.projects
            ],
            "judgements": JUDGEMENTS,
        },
    }


def _build_review(post: VideoPostItem, *, now: datetime) -> VideoReviewItem:
    follower_rate = _rate(post.followers_gained, post.views)
    trend = _growth_trend(post.snapshots, post.view_velocity)
    elapsed_minutes = (
        max((now - post.published_at).total_seconds() / 60, 0)
        if post.published_at
        else 0
    )
    judgement = _judgement(
        elapsed_minutes=elapsed_minutes,
        views=post.views,
        favorite_rate=post.favorite_rate,
        share_rate=post.share_rate,
        follower_rate=follower_rate,
        completion_rate=post.completion_rate,
        growth_trend=trend,
    )
    reasons = _reasons(
        favorite_rate=post.favorite_rate,
        share_rate=post.share_rate,
        follower_rate=follower_rate,
        completion_rate=post.completion_rate,
        growth_trend=trend,
    )
    return VideoReviewItem(
        video_post_id=post.id,
        pipeline_id=post.pipeline_id,
        project_name=post.project_name,
        video_title=post.video_title,
        platform=post.platform,
        published_at=post.published_at,
        views=post.views,
        likes=post.likes,
        comments=post.comments,
        favorites=post.favorites,
        shares=post.shares,
        followers_gained=post.followers_gained,
        completion_rate=post.completion_rate,
        average_watch_seconds=post.average_watch_seconds,
        favorite_rate=post.favorite_rate,
        share_rate=post.share_rate,
        follower_rate=follower_rate,
        view_velocity=post.view_velocity,
        growth_trend=trend,
        judgement=judgement,
        reasons=reasons,
        next_suggestion=_next_suggestion(judgement, trend, post.completion_rate),
        snapshot_count=post.snapshot_count,
        snapshots=post.snapshots,
    )


def _growth_trend(snapshots: list[SnapshotItem], fallback_velocity: float) -> str:
    if len(snapshots) < 2:
        return "数据不足" if fallback_velocity <= 0 else "稳定增长"

    previous, latest = snapshots[-2:]
    view_growth = latest.views - previous.views
    hours = max((latest.captured_at - previous.captured_at).total_seconds() / 3600, 0)
    growth_rate = view_growth / max(previous.views, 1)
    if latest.elapsed_minutes >= 1440 and view_growth > max(previous.views * 0.05, 50):
        return "长尾增长"
    if hours > 0 and hours <= 2 and view_growth >= 300 and growth_rate >= 0.3:
        return "加速"
    if hours >= 1 and view_growth <= max(previous.views * 0.02, 10):
        return "停滞"
    if len(snapshots) >= 3:
        earlier = snapshots[-3]
        earlier_hours = max(
            (previous.captured_at - earlier.captured_at).total_seconds() / 3600,
            0,
        )
        previous_velocity = (
            (previous.views - earlier.views) / earlier_hours if earlier_hours > 0 else 0
        )
        current_velocity = view_growth / hours if hours > 0 else 0
        if current_velocity > max(previous_velocity * 1.3, 100):
            return "加速"
        if current_velocity < previous_velocity * 0.5:
            return "放缓"
    return "稳定增长" if view_growth > 0 else "停滞"


def _judgement(
    *,
    elapsed_minutes: float,
    views: int,
    favorite_rate: float,
    share_rate: float,
    follower_rate: float,
    completion_rate: float,
    growth_trend: str,
) -> str:
    if (
        favorite_rate >= 3
        and share_rate >= 1.5
        and (completion_rate >= 40 or follower_rate >= 0.5)
    ):
        return "值得继续做"
    if (
        elapsed_minutes >= 4320
        and views < 300
        and favorite_rate < 1
        and share_rate < 0.5
        and follower_rate < 0.2
        and completion_rate < 25
    ):
        return "不建议继续"
    if completion_rate < 25 or (
        elapsed_minutes >= 1440 and favorite_rate < 1 and share_rate < 0.5
    ):
        return "需要调整"
    if (
        favorite_rate >= 3
        or share_rate >= 1.5
        or follower_rate >= 0.5
        or growth_trend in {"加速", "长尾增长"}
    ):
        return "有潜力"
    return "表现一般"


def _reasons(
    *,
    favorite_rate: float,
    share_rate: float,
    follower_rate: float,
    completion_rate: float,
    growth_trend: str,
) -> list[str]:
    reasons: list[str] = []
    if favorite_rate >= 3:
        reasons.append("收藏率高，内容有保存价值。")
    elif favorite_rate < 1:
        reasons.append("收藏率偏低，实用价值或信息密度需要加强。")
    if share_rate >= 1.5:
        reasons.append("转发率高，内容有传播价值。")
    elif share_rate < 0.5:
        reasons.append("转发率偏低，传播理由不够明显。")
    if completion_rate >= 40:
        reasons.append("完播率较好，当前结构和节奏基本有效。")
    elif completion_rate < 25:
        reasons.append("完播率偏低，开头或中段节奏需要调整。")
    if follower_rate >= 0.5:
        reasons.append("涨粉率较好，选题与账号定位比较匹配。")
    elif follower_rate < 0.2:
        reasons.append("涨粉率偏低，选题与账号定位的连接不够强。")
    if growth_trend == "长尾增长":
        reasons.append("24 小时后仍在增长，有长尾潜力。")
    elif growth_trend == "加速":
        reasons.append("近期播放增长正在加速。")
    elif growth_trend in {"停滞", "放缓"}:
        reasons.append("播放增长已经放缓，需要观察是否继续投入。")
    return reasons[:5] or ["当前数据没有明显强弱信号，建议继续补充快照。"]


def _next_suggestion(
    judgement: str,
    growth_trend: str,
    completion_rate: float,
) -> str:
    if judgement == "值得继续做":
        return "继续做同类选题，并保留当前有效的结构和表达方式。"
    if judgement == "有潜力":
        return "继续观察后续快照，可围绕同一主题再做一条不同角度。"
    if judgement == "需要调整":
        if completion_rate < 25:
            return "先调整开头和节奏，再测试同类选题，不要直接放弃方向。"
        return "提高实用信息和收藏理由，再测试一次同类内容。"
    if judgement == "不建议继续":
        return "暂时停止重复这个方向，把精力转向收藏率和转发率更高的题材。"
    if growth_trend == "数据不足":
        return "继续补充 24 小时和 3 天数据，再决定是否系列化。"
    return "保持观察，优先比较下一条视频的收藏率、转发率和完播率。"


def _rate(value: int, views: int) -> float:
    return round(value / views * 100, 2) if views else 0.0


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
