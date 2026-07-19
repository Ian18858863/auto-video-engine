from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import ceil
from typing import Any

from sqlalchemy.orm import Session, selectinload

from app.models import ProductionPipeline, VideoPost, VideoPostSnapshot


TRACKING_STATUSES = ["观察中", "正常", "加速", "掉速", "有潜力", "需要复盘"]
METRIC_FIELDS = (
    "views",
    "likes",
    "comments",
    "favorites",
    "shares",
    "followers_gained",
    "completion_rate",
    "average_watch_seconds",
)
POST_FIELDS = (
    "pipeline_id",
    "platform",
    "video_title",
    "published_at",
    "video_url",
    "topic_type",
    "video_format",
    "hook_type",
    "duration_seconds",
    "notes",
)


class VideoPostNotFoundError(ValueError):
    pass


class PipelineNotFoundError(ValueError):
    pass


@dataclass
class ProjectOption:
    id: int
    title: str


@dataclass
class SnapshotItem:
    id: int
    captured_at: datetime
    elapsed_minutes: float
    views: int
    likes: int
    comments: int
    favorites: int
    shares: int
    followers_gained: int
    completion_rate: float
    average_watch_seconds: float
    notes: str


@dataclass
class VideoPostItem:
    id: int
    pipeline_id: int | None
    project_name: str
    platform: str
    video_title: str
    published_at: datetime | None
    video_url: str
    topic_type: str
    video_format: str
    hook_type: str
    duration_seconds: int
    views: int
    likes: int
    comments: int
    favorites: int
    shares: int
    followers_gained: int
    completion_rate: float
    average_watch_seconds: float
    latest_updated_at: datetime
    current_stage: str
    current_status: str
    notes: str
    snapshot_count: int
    favorite_rate: float
    share_rate: float
    view_velocity: float
    snapshots: list[SnapshotItem] = field(default_factory=list)


@dataclass
class VideoPostListResult:
    items: list[VideoPostItem]
    total: int
    page: int
    page_size: int
    total_pages: int
    platforms: list[str]
    projects: list[ProjectOption]


def create_video_post(
    session: Session,
    payload: dict[str, Any],
    *,
    captured_at: datetime | None = None,
) -> VideoPostItem:
    _validate_pipeline(session, payload.get("pipeline_id"))
    captured = _aware(captured_at or datetime.now(timezone.utc))
    post = VideoPost(
        **{field: payload.get(field) for field in POST_FIELDS if field in payload},
        **{field: payload.get(field, 0) for field in METRIC_FIELDS},
        updated_at=captured,
    )
    session.add(post)
    session.flush()
    _add_snapshot_model(session, post, payload, captured)
    session.commit()
    return _require_post(session, post.id, now=captured)


def update_video_post(
    session: Session,
    post_id: int,
    payload: dict[str, Any],
) -> VideoPostItem:
    post = session.get(VideoPost, post_id)
    if post is None:
        raise VideoPostNotFoundError(f"发布记录不存在：{post_id}")
    if "pipeline_id" in payload:
        _validate_pipeline(session, payload.get("pipeline_id"))
    for field in POST_FIELDS:
        if field in payload:
            setattr(post, field, payload[field])
    post.updated_at = datetime.now(timezone.utc)
    session.commit()
    return _require_post(session, post_id)


def add_video_snapshot(
    session: Session,
    post_id: int,
    payload: dict[str, Any],
    *,
    captured_at: datetime | None = None,
) -> VideoPostItem:
    post = session.get(VideoPost, post_id)
    if post is None:
        raise VideoPostNotFoundError(f"发布记录不存在：{post_id}")
    captured = _aware(captured_at or datetime.now(timezone.utc))
    previous_views = post.views
    previous_time = _latest_snapshot_time(post)
    _add_snapshot_model(session, post, payload, captured)
    for field in METRIC_FIELDS:
        setattr(post, field, payload.get(field, getattr(post, field)))
    hours = max((captured - previous_time).total_seconds() / 3600, 0)
    post.current_view_velocity = (
        max(post.views - previous_views, 0) / hours if hours > 0 else 0.0
    )
    post.updated_at = captured
    session.commit()
    return _require_post(session, post_id, now=captured)


def get_video_post(
    session: Session,
    post_id: int,
    *,
    now: datetime | None = None,
) -> VideoPostItem | None:
    post = (
        session.query(VideoPost)
        .options(selectinload(VideoPost.snapshots))
        .filter(VideoPost.id == post_id)
        .one_or_none()
    )
    if post is None:
        return None
    project_names = _project_names(session, [post.pipeline_id])
    return _build_item(post, project_names, now=_aware(now or datetime.now(timezone.utc)))


def list_video_posts(
    session: Session,
    *,
    platform: str | None = None,
    pipeline_id: int | None = None,
    status: str | None = None,
    sort_by: str = "published_at",
    page: int = 1,
    page_size: int = 20,
    now: datetime | None = None,
) -> VideoPostListResult:
    query = session.query(VideoPost).options(selectinload(VideoPost.snapshots))
    if platform:
        query = query.filter(VideoPost.platform == platform)
    if pipeline_id is not None:
        query = query.filter(VideoPost.pipeline_id == pipeline_id)
    posts = query.all()
    project_names = _project_names(session, [post.pipeline_id for post in posts])
    current_time = _aware(now or datetime.now(timezone.utc))
    items = [_build_item(post, project_names, now=current_time) for post in posts]
    if status:
        items = [item for item in items if item.current_status == status]
    sort_key = {
        "views": lambda item: item.views,
        "favorites": lambda item: item.favorites,
        "shares": lambda item: item.shares,
        "published_at": lambda item: _aware(item.published_at or datetime.min),
    }.get(sort_by, lambda item: _aware(item.published_at or datetime.min))
    items.sort(key=sort_key, reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    platforms = [
        value[0]
        for value in session.query(VideoPost.platform)
        .distinct()
        .order_by(VideoPost.platform)
        .all()
    ]
    projects = [
        ProjectOption(id=pipeline.id, title=pipeline.project_name)
        for pipeline in session.query(ProductionPipeline)
        .order_by(ProductionPipeline.project_name)
        .all()
    ]
    return VideoPostListResult(
        items=items[start : start + page_size],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=ceil(total / page_size) if total else 0,
        platforms=platforms,
        projects=projects,
    )


def serialize_video_post(item: VideoPostItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "pipeline_id": item.pipeline_id,
        "project_name": item.project_name,
        "platform": item.platform,
        "video_title": item.video_title,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "video_url": item.video_url,
        "topic_type": item.topic_type,
        "video_format": item.video_format,
        "hook_type": item.hook_type,
        "duration_seconds": item.duration_seconds,
        "views": item.views,
        "likes": item.likes,
        "comments": item.comments,
        "favorites": item.favorites,
        "shares": item.shares,
        "followers_gained": item.followers_gained,
        "completion_rate": item.completion_rate,
        "average_watch_seconds": item.average_watch_seconds,
        "latest_updated_at": item.latest_updated_at.isoformat(),
        "current_stage": item.current_stage,
        "current_status": item.current_status,
        "notes": item.notes,
        "snapshot_count": item.snapshot_count,
        "favorite_rate": item.favorite_rate,
        "share_rate": item.share_rate,
        "view_velocity": item.view_velocity,
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


def serialize_video_post_list(result: VideoPostListResult) -> dict[str, Any]:
    return {
        "items": [serialize_video_post(item) for item in result.items],
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
            "statuses": TRACKING_STATUSES,
        },
    }


def _require_post(
    session: Session,
    post_id: int,
    *,
    now: datetime | None = None,
) -> VideoPostItem:
    item = get_video_post(session, post_id, now=now)
    if item is None:
        raise VideoPostNotFoundError(f"发布记录不存在：{post_id}")
    return item


def _validate_pipeline(session: Session, pipeline_id: int | None) -> None:
    if pipeline_id is not None and session.get(ProductionPipeline, pipeline_id) is None:
        raise PipelineNotFoundError(f"内容项目不存在：{pipeline_id}")


def _add_snapshot_model(
    session: Session,
    post: VideoPost,
    payload: dict[str, Any],
    captured_at: datetime,
) -> None:
    published_at = _aware(post.published_at or captured_at)
    elapsed = max((captured_at - published_at).total_seconds() / 60, 0)
    session.add(
        VideoPostSnapshot(
            video_post_id=post.id,
            captured_at=captured_at,
            elapsed_minutes=round(elapsed, 2),
            **{field: payload.get(field, getattr(post, field)) for field in METRIC_FIELDS},
            notes=payload.get("notes", ""),
        )
    )


def _build_item(
    post: VideoPost,
    project_names: dict[int, str],
    *,
    now: datetime,
) -> VideoPostItem:
    snapshots = [
        SnapshotItem(
            id=snapshot.id,
            captured_at=_aware(snapshot.captured_at),
            elapsed_minutes=snapshot.elapsed_minutes,
            views=snapshot.views,
            likes=snapshot.likes,
            comments=snapshot.comments,
            favorites=snapshot.favorites,
            shares=snapshot.shares,
            followers_gained=snapshot.followers_gained,
            completion_rate=snapshot.completion_rate,
            average_watch_seconds=snapshot.average_watch_seconds,
            notes=snapshot.notes,
        )
        for snapshot in sorted(post.snapshots, key=lambda item: item.captured_at)
    ]
    latest_updated = (
        snapshots[-1].captured_at
        if snapshots
        else _aware(post.updated_at or post.published_at or now)
    )
    published_at = _aware(post.published_at) if post.published_at else None
    elapsed_minutes = (
        max((now - published_at).total_seconds() / 60, 0) if published_at else 0
    )
    favorite_rate = _rate(post.favorites, post.views)
    share_rate = _rate(post.shares, post.views)
    velocity = _view_velocity(snapshots, post.current_view_velocity)
    return VideoPostItem(
        id=post.id,
        pipeline_id=post.pipeline_id,
        project_name=project_names.get(post.pipeline_id or -1, "未关联项目"),
        platform=post.platform,
        video_title=post.video_title,
        published_at=published_at,
        video_url=post.video_url,
        topic_type=post.topic_type,
        video_format=post.video_format,
        hook_type=post.hook_type,
        duration_seconds=post.duration_seconds,
        views=post.views,
        likes=post.likes,
        comments=post.comments,
        favorites=post.favorites,
        shares=post.shares,
        followers_gained=post.followers_gained,
        completion_rate=post.completion_rate,
        average_watch_seconds=post.average_watch_seconds,
        latest_updated_at=latest_updated,
        current_stage=_stage(elapsed_minutes),
        current_status=_status(
            elapsed_minutes=elapsed_minutes,
            views=post.views,
            favorite_rate=favorite_rate,
            share_rate=share_rate,
            snapshots=snapshots,
        ),
        notes=post.notes,
        snapshot_count=len(snapshots),
        favorite_rate=favorite_rate,
        share_rate=share_rate,
        view_velocity=velocity,
        snapshots=snapshots,
    )


def _status(
    *,
    elapsed_minutes: float,
    views: int,
    favorite_rate: float,
    share_rate: float,
    snapshots: list[SnapshotItem],
) -> str:
    if views >= 100 and (favorite_rate >= 3 or share_rate >= 1.5):
        return "有潜力"
    if len(snapshots) >= 2:
        previous, latest = snapshots[-2:]
        minutes = max(
            (latest.captured_at - previous.captured_at).total_seconds() / 60,
            0,
        )
        growth = latest.views - previous.views
        growth_rate = growth / max(previous.views, 1)
        if minutes <= 120 and growth >= 300 and growth_rate >= 0.3:
            return "加速"
        if minutes >= 60 and growth <= max(previous.views * 0.02, 10):
            return "掉速"
    if elapsed_minutes >= 240 and views < 100:
        return "需要复盘"
    if elapsed_minutes <= 60:
        return "观察中"
    return "正常"


def _stage(elapsed_minutes: float) -> str:
    if elapsed_minutes <= 30:
        return "30分钟"
    if elapsed_minutes <= 60:
        return "1小时"
    if elapsed_minutes <= 120:
        return "2小时"
    if elapsed_minutes <= 240:
        return "4小时"
    if elapsed_minutes <= 1440:
        return "24小时"
    if elapsed_minutes <= 4320:
        return "3天"
    return "7天"


def _rate(value: int, views: int) -> float:
    return round(value / views * 100, 2) if views else 0.0


def _view_velocity(
    snapshots: list[SnapshotItem],
    fallback: float,
) -> float:
    if len(snapshots) < 2:
        return round(fallback, 2)
    previous, latest = snapshots[-2:]
    hours = (latest.captured_at - previous.captured_at).total_seconds() / 3600
    return round(max(latest.views - previous.views, 0) / hours, 2) if hours > 0 else 0.0


def _latest_snapshot_time(post: VideoPost) -> datetime:
    if post.snapshots:
        return _aware(max(snapshot.captured_at for snapshot in post.snapshots))
    return _aware(post.updated_at or post.published_at or datetime.now(timezone.utc))


def _project_names(
    session: Session,
    pipeline_ids: list[int | None],
) -> dict[int, str]:
    ids = {pipeline_id for pipeline_id in pipeline_ids if pipeline_id is not None}
    if not ids:
        return {}
    return {
        pipeline.id: pipeline.project_name
        for pipeline in session.query(ProductionPipeline)
        .filter(ProductionPipeline.id.in_(ids))
        .all()
    }


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
