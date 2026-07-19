from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.hotspots.archive import archive_expired_hotspots
from app.hotspots.config import load_source_config
from app.hotspots.pipeline import HotspotPipeline
from app.hotspots.schemas import PipelineRunResult
from app.hotspots.sources.base import HotspotSource
from app.hotspots.sources.factory import build_hotspot_sources
from app.models import AppConfig, HotItem, SourceRun
from app.models.entities import utc_now
from app.topic_ideas.local_generator import generate_local_daily_topic_candidates


APP_TZ = ZoneInfo("Asia/Shanghai")
AUTO_HOTSPOT_CONFIG_KEY = "auto_hotspot_intake_state"
DAILY_REFRESH_HOUR = 9
DAILY_REFRESH_MINUTE = 15


@dataclass(slots=True)
class SourceRunStatus:
    source_name: str
    status: str
    last_run_at: datetime | None = None
    items_fetched: int = 0
    success_count: int = 0
    failure_reason: str = ""
    is_rate_limited: bool = False


@dataclass(slots=True)
class TopicGenerationStatus:
    hotspot_id: int
    title: str
    generated_count: int
    video_type: str = ""
    recommendation_bucket: str = ""
    error: str = ""


@dataclass(slots=True)
class AutoHotspotIntakeResult:
    source_count: int
    skipped_count: int
    fetched_count: int
    saved_count: int
    duplicate_count: int
    filtered_count: int
    failed_sources: list[str] = field(default_factory=list)
    rate_limited_sources: list[str] = field(default_factory=list)
    source_statuses: list[SourceRunStatus] = field(default_factory=list)
    generated_hotspot_count: int = 0
    generated_topic_idea_count: int = 0
    archived_expired_count: int = 0
    preserved_expired_count: int = 0
    topic_generation: list[TopicGenerationStatus] = field(default_factory=list)
    provider: str = "local_rule"
    real_ai_called: bool = False
    manual_import_is_fallback: bool = True
    message: str = ""
    ran_at: datetime | None = None
    next_due_at: datetime | None = None


def run_auto_hotspot_intake(
    session: Session,
    config_path: str | Path,
    *,
    source_names: list[str] | None = None,
    force: bool = False,
    generate_topics: bool = True,
    max_hotspots_for_topics: int = 70,
    interval_hours: int = 24,
    sources: list[HotspotSource] | None = None,
    now: datetime | None = None,
) -> AutoHotspotIntakeResult:
    current_time = _aware(now or utc_now())
    configured_sources = (
        sources
        if sources is not None
        else build_hotspot_sources(config_path, source_names=source_names)
    )
    runnable_sources, skipped = _split_sources_by_due(
        session,
        configured_sources,
        force=force,
        now=current_time,
    )
    pipeline_result = (
        HotspotPipeline(session).run_sources(runnable_sources)
        if runnable_sources
        else PipelineRunResult()
    )
    archive_result = archive_expired_hotspots(session, now=current_time)
    topic_statuses: list[TopicGenerationStatus] = []
    generated_topic_count = 0
    if generate_topics:
        for item in generate_local_daily_topic_candidates(
            session,
            max_hotspots=max_hotspots_for_topics,
            today_limit=5,
            now=current_time,
        ):
            generated_topic_count += item.generated_count
            topic_statuses.append(
                TopicGenerationStatus(
                    hotspot_id=item.hotspot_id,
                    title=item.title,
                    generated_count=item.generated_count,
                    video_type=item.video_type,
                    recommendation_bucket=item.recommendation_bucket,
                    error=item.error,
                )
            )
    next_due_at = current_time + timedelta(hours=max(interval_hours, 1))
    _save_auto_state(
        session,
        {
            "last_run_at": current_time.isoformat(),
            "next_due_at": next_due_at.isoformat(),
            "source_count": len(configured_sources),
            "skipped_count": len(skipped),
            "saved_count": pipeline_result.saved_count,
            "failed_sources": pipeline_result.failed_sources,
            "rate_limited_sources": pipeline_result.rate_limited_sources,
            "generated_topic_idea_count": generated_topic_count,
            "archived_expired_count": archive_result.archived_count,
            "preserved_expired_count": archive_result.preserved_count,
            "real_ai_called": False,
        },
        now=current_time,
    )
    statuses = list_source_run_statuses(
        session,
        config_path,
        source_names=source_names,
        sources=configured_sources,
    )
    skipped_names = {source.name for source in skipped}
    statuses = [
        SourceRunStatus(
            source_name=status.source_name,
            status="跳过" if status.source_name in skipped_names else status.status,
            last_run_at=status.last_run_at,
            items_fetched=status.items_fetched,
            success_count=status.success_count,
            failure_reason=(
                "今日已抓取，等待下次自动检查"
                if status.source_name in skipped_names
                else status.failure_reason
            ),
            is_rate_limited=status.is_rate_limited,
        )
        for status in statuses
    ]
    message = _run_message(
        pipeline_result,
        len(skipped),
        generated_topic_count,
        archive_result.archived_count,
    )
    return AutoHotspotIntakeResult(
        source_count=len(configured_sources),
        skipped_count=len(skipped),
        fetched_count=pipeline_result.fetched_count,
        saved_count=pipeline_result.saved_count,
        duplicate_count=pipeline_result.duplicate_count,
        filtered_count=pipeline_result.filtered_count,
        failed_sources=pipeline_result.failed_sources,
        rate_limited_sources=pipeline_result.rate_limited_sources,
        source_statuses=statuses,
        generated_hotspot_count=len(topic_statuses),
        generated_topic_idea_count=generated_topic_count,
        archived_expired_count=archive_result.archived_count,
        preserved_expired_count=archive_result.preserved_count,
        topic_generation=topic_statuses,
        message=message,
        ran_at=current_time,
        next_due_at=next_due_at,
    )


def should_run_daily_hotspot_refresh(
    session: Session,
    *,
    now: datetime | None = None,
) -> bool:
    current_time = _aware(now or utc_now())
    local_time = current_time.astimezone(APP_TZ)
    scheduled_at = datetime.combine(
        local_time.date(),
        time(hour=DAILY_REFRESH_HOUR, minute=DAILY_REFRESH_MINUTE),
        tzinfo=APP_TZ,
    )
    if local_time < scheduled_at:
        return False
    state = _load_auto_state(session)
    return state.get("daily_refresh_date") != local_time.date().isoformat()


def run_daily_hotspot_refresh(
    session: Session,
    config_path: str | Path,
    *,
    source_names: list[str] | None = None,
    generate_topics: bool = True,
    max_hotspots_for_topics: int = 70,
    interval_hours: int = 24,
    sources: list[HotspotSource] | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> AutoHotspotIntakeResult:
    current_time = _aware(now or utc_now())
    configured_sources = (
        sources
        if sources is not None
        else build_hotspot_sources(config_path, source_names=source_names)
    )
    if not force and not should_run_daily_hotspot_refresh(session, now=current_time):
        statuses = list_source_run_statuses(
            session,
            config_path,
            source_names=source_names,
            sources=configured_sources,
        )
        return AutoHotspotIntakeResult(
            source_count=len(configured_sources),
            skipped_count=len(configured_sources),
            fetched_count=0,
            saved_count=0,
            duplicate_count=0,
            filtered_count=0,
            source_statuses=statuses,
            message="今日 9:15 刷新已完成，或尚未到自动刷新时间。",
            ran_at=current_time,
            next_due_at=_next_daily_refresh_at(current_time),
        )

    result = run_auto_hotspot_intake(
        session,
        config_path,
        source_names=source_names,
        force=True,
        generate_topics=generate_topics,
        max_hotspots_for_topics=max_hotspots_for_topics,
        interval_hours=interval_hours,
        sources=configured_sources,
        now=current_time,
    )
    _mark_daily_refresh_complete(session, current_time)
    result.next_due_at = _next_daily_refresh_at(current_time)
    return result


def list_source_run_statuses(
    session: Session,
    config_path: str | Path,
    *,
    source_names: list[str] | None = None,
    sources: list[HotspotSource] | None = None,
) -> list[SourceRunStatus]:
    configured_sources = (
        sources
        if sources is not None
        else build_hotspot_sources(config_path, source_names=source_names)
    )
    statuses: list[SourceRunStatus] = []
    for source in configured_sources:
        last_run = _last_source_run(session, source.name)
        if last_run is None:
            statuses.append(
                SourceRunStatus(
                    source_name=source.name,
                    status="跳过",
                    failure_reason="尚未抓取",
                )
            )
            continue
        statuses.append(
            SourceRunStatus(
                source_name=source.name,
                status=(
                    "成功"
                    if last_run.success
                    else last_run.status
                    or ("限流" if last_run.is_rate_limited else "失败")
                ),
                last_run_at=last_run.ended_at or last_run.started_at,
                items_fetched=last_run.items_fetched,
                success_count=last_run.items_fetched if last_run.success else 0,
                failure_reason="" if last_run.success else last_run.error_message,
                is_rate_limited=last_run.is_rate_limited,
            )
        )
    return statuses


def list_all_source_run_statuses(
    session: Session,
    config_path: str | Path,
    *,
    source_names: list[str] | None = None,
) -> list[SourceRunStatus]:
    statuses = list_source_run_statuses(
        session,
        config_path,
        source_names=source_names,
    )
    config = load_source_config(config_path)
    names = {name.strip().lower() for name in (source_names or []) if name.strip()}
    for source in config.browser_hotspot_sources:
        if not source.enabled:
            continue
        if names and source.name.lower() not in names and not any(
            token in source.name.lower() for token in names
        ):
            continue
        statuses.append(_source_run_status_from_last_run(session, source.name))
    return statuses


def get_auto_hotspot_state(session: Session) -> dict[str, Any]:
    return _load_auto_state(session)


def _source_run_status_from_last_run(
    session: Session,
    source_name: str,
) -> SourceRunStatus:
    last_run = _last_source_run(session, source_name)
    if last_run is None:
        return SourceRunStatus(
            source_name=source_name,
            status="跳过",
            failure_reason="尚未采集",
        )
    return SourceRunStatus(
        source_name=source_name,
        status=(
            "成功"
            if last_run.success
            else last_run.status
            or ("限流" if last_run.is_rate_limited else "失败")
        ),
        last_run_at=last_run.ended_at or last_run.started_at,
        items_fetched=last_run.items_fetched,
        success_count=last_run.items_fetched if last_run.success else 0,
        failure_reason="" if last_run.success else last_run.error_message,
        is_rate_limited=last_run.is_rate_limited,
    )


def serialize_auto_hotspot_result(result: AutoHotspotIntakeResult) -> dict[str, Any]:
    return {
        "source_count": result.source_count,
        "skipped_count": result.skipped_count,
        "fetched_count": result.fetched_count,
        "saved_count": result.saved_count,
        "duplicate_count": result.duplicate_count,
        "filtered_count": result.filtered_count,
        "failed_sources": result.failed_sources,
        "rate_limited_sources": result.rate_limited_sources,
        "source_statuses": [
            serialize_source_status(status) for status in result.source_statuses
        ],
        "generated_hotspot_count": result.generated_hotspot_count,
        "generated_topic_idea_count": result.generated_topic_idea_count,
        "archived_expired_count": result.archived_expired_count,
        "preserved_expired_count": result.preserved_expired_count,
        "topic_generation": [
            {
                "hotspot_id": item.hotspot_id,
                "title": item.title,
                "generated_count": item.generated_count,
                "video_type": item.video_type,
                "recommendation_bucket": item.recommendation_bucket,
                "error": item.error,
            }
            for item in result.topic_generation
        ],
        "provider": result.provider,
        "real_ai_called": result.real_ai_called,
        "manual_import_is_fallback": result.manual_import_is_fallback,
        "message": result.message,
        "ran_at": _isoformat(result.ran_at),
        "next_due_at": _isoformat(result.next_due_at),
    }


def serialize_source_status(status: SourceRunStatus) -> dict[str, Any]:
    return {
        "source_name": status.source_name,
        "status": status.status,
        "last_run_at": _isoformat(status.last_run_at),
        "items_fetched": status.items_fetched,
        "success_count": status.success_count,
        "failure_reason": status.failure_reason,
        "is_rate_limited": status.is_rate_limited,
    }


def _split_sources_by_due(
    session: Session,
    sources: list[HotspotSource],
    *,
    force: bool,
    now: datetime,
) -> tuple[list[HotspotSource], list[HotspotSource]]:
    if force:
        return sources, []
    start_of_day = datetime.combine(
        now.astimezone(APP_TZ).date(),
        time.min,
        tzinfo=APP_TZ,
    ).astimezone(timezone.utc)
    runnable: list[HotspotSource] = []
    skipped: list[HotspotSource] = []
    for source in sources:
        successful_today = (
            session.query(SourceRun.id)
            .filter(SourceRun.source_name == source.name)
            .filter(SourceRun.success.is_(True))
            .filter(SourceRun.ended_at >= start_of_day)
            .first()
        )
        if successful_today is None:
            runnable.append(source)
        else:
            skipped.append(source)
    return runnable, skipped


def _topic_candidate_hotspots(session: Session, limit: int) -> list[HotItem]:
    if limit <= 0:
        return []
    return (
        session.query(HotItem)
        .filter(HotItem.is_filtered.is_(False))
        .filter(HotItem.lifecycle_status != "archived")
        .filter(HotItem.has_topic_ideas.is_(False))
        .order_by(
            HotItem.topic_score.desc(),
            HotItem.save_score.desc(),
            HotItem.hot_score.desc(),
            HotItem.fetched_at.desc(),
            HotItem.id.desc(),
        )
        .limit(limit)
        .all()
    )


def _last_source_run(session: Session, source_name: str) -> SourceRun | None:
    return (
        session.query(SourceRun)
        .filter(SourceRun.source_name == source_name)
        .order_by(SourceRun.started_at.desc(), SourceRun.id.desc())
        .first()
    )


def _save_auto_state(session: Session, value: dict[str, Any], *, now: datetime) -> None:
    item = (
        session.query(AppConfig)
        .filter(AppConfig.key == AUTO_HOTSPOT_CONFIG_KEY)
        .one_or_none()
    )
    if item is None:
        item = AppConfig(key=AUTO_HOTSPOT_CONFIG_KEY, value=value, updated_at=now)
        session.add(item)
    else:
        existing = dict(item.value) if isinstance(item.value, dict) else {}
        item.value = {**existing, **value}
        item.updated_at = now
        session.add(item)
    session.commit()


def _load_auto_state(session: Session) -> dict[str, Any]:
    item = (
        session.query(AppConfig)
        .filter(AppConfig.key == AUTO_HOTSPOT_CONFIG_KEY)
        .one_or_none()
    )
    if item is None or not isinstance(item.value, dict):
        return {}
    return dict(item.value)


def _mark_daily_refresh_complete(session: Session, now: datetime) -> None:
    local_date = now.astimezone(APP_TZ).date().isoformat()
    _save_auto_state(
        session,
        {
            "daily_refresh_date": local_date,
            "daily_refresh_completed_at": now.isoformat(),
            "daily_refresh_time": f"{DAILY_REFRESH_HOUR:02d}:{DAILY_REFRESH_MINUTE:02d}",
        },
        now=now,
    )


def _next_daily_refresh_at(now: datetime) -> datetime:
    local_time = now.astimezone(APP_TZ)
    candidate = datetime.combine(
        local_time.date(),
        time(hour=DAILY_REFRESH_HOUR, minute=DAILY_REFRESH_MINUTE),
        tzinfo=APP_TZ,
    )
    if local_time >= candidate:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _run_message(
    result: PipelineRunResult,
    skipped_count: int,
    generated_topic_count: int,
    archived_expired_count: int = 0,
) -> str:
    parts = [
        f"新增热点 {result.saved_count} 条",
        f"重复 {result.duplicate_count} 条",
        f"过滤 {result.filtered_count} 条",
        f"跳过来源 {skipped_count} 个",
        f"归档过期热点 {archived_expired_count} 条",
        f"生成候选选题 {generated_topic_count} 条",
    ]
    if result.failed_sources:
        parts.append(f"失败来源 {len(result.failed_sources)} 个，已隔离记录")
    if result.rate_limited_sources:
        parts.append(f"限流来源 {len(result.rate_limited_sources)} 个")
    return "；".join(parts) + "。"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
