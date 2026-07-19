from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, or_
from sqlalchemy.orm import Session, joinedload

from app.hotspots.eligibility import is_today_recommendation_eligible
from app.models import AppConfig, HotItem, PipelineStep, ProductionPipeline, ScriptAsset, TopicIdea
from app.pipeline.list_service import derive_pipeline_items
from app.pipeline.models import PipelineListItem
from app.publishing.service import list_video_posts
from app.reviews.service import list_video_reviews


APP_TZ = timezone(timedelta(hours=8))
TODAY_HANDLED_KEY = "today_task_done"
TASK_REFRESH_HOUR = 9
TASK_REFRESH_MINUTE = 15


@dataclass(slots=True)
class TodayTaskItem:
    task_id: str
    title: str
    subtitle: str
    reason: str
    next_action: str
    target_type: str
    target_id: int | None
    priority: float
    meta: dict[str, Any] = field(default_factory=dict)
    is_done: bool = False


@dataclass(slots=True)
class TodayDashboard:
    date: str
    hotspots: list[TodayTaskItem]
    topic_ideas: list[TodayTaskItem]
    collectible_ideas: list[TodayTaskItem]
    projects: list[TodayTaskItem]
    confirmations: list[TodayTaskItem]
    publish_reminders: list[TodayTaskItem]
    suggestions: list[TodayTaskItem]
    decision_items: list[TodayTaskItem]
    running_items: list[TodayTaskItem]
    finished_items: list[TodayTaskItem]
    handled_task_ids: list[str]


class TodayTaskNotFoundError(ValueError):
    pass


class TodayTaskNotDismissibleError(ValueError):
    pass


def get_today_tasks(
    session: Session,
    *,
    now: datetime | None = None,
) -> TodayDashboard:
    current_time = _aware(now or datetime.now(timezone.utc))
    handled = _handled_task_ids(session, current_time)

    hotspots = _top_hotspots(session)
    topic_ideas = _top_topic_ideas(session, limit=5, collect_only=False, now=current_time)
    collectible_ideas = _top_topic_ideas(session, limit=3, collect_only=True, now=current_time)
    pipeline_items = derive_pipeline_items(session)
    projects = _project_tasks(pipeline_items)
    confirmations = _confirmation_tasks(session, pipeline_items)
    publish_reminders = _publish_reminders(session, current_time)
    finished_items = _finished_project_tasks(pipeline_items)
    finished_items.sort(key=lambda item: item.priority, reverse=True)
    script_confirmation_tasks = _script_confirmation_tasks(session, pipeline_items)
    topic_confirmation = _topic_confirmation_task(session, topic_ideas, current_time)
    final_review_decisions = [
        item for item in confirmations if item.target_type == "finished"
    ]
    decision_candidates = [
        *script_confirmation_tasks,
        *([topic_confirmation] if topic_confirmation is not None else []),
        *final_review_decisions[:1],
    ]
    decision_candidates.sort(key=lambda item: item.priority, reverse=True)
    decision_items = decision_candidates[:1]
    running_items = _running_project_tasks(projects)
    suggestions: list[TodayTaskItem] = []

    all_groups = [
        hotspots,
        topic_ideas,
        collectible_ideas,
        projects,
        confirmations,
        publish_reminders,
        suggestions,
        decision_items,
        running_items,
        finished_items,
    ]
    for group in all_groups:
        for item in group:
            item.is_done = item.task_id in handled

    return TodayDashboard(
        date=_task_day_key(current_time),
        hotspots=hotspots[:20],
        topic_ideas=topic_ideas[:5],
        collectible_ideas=collectible_ideas[:3],
        projects=projects[:5],
        # System blockers and live running work must never be silently dropped.
        # The task page can group them visually, but it needs the complete truth.
        confirmations=confirmations,
        publish_reminders=publish_reminders[:8],
        suggestions=suggestions[:6],
        decision_items=decision_items[:1],
        running_items=running_items,
        finished_items=finished_items[:8],
        handled_task_ids=sorted(handled),
    )


def mark_today_task_done(
    session: Session,
    task_id: str,
    *,
    now: datetime | None = None,
) -> list[str]:
    current_time = _aware(now or datetime.now(timezone.utc))
    dashboard = get_today_tasks(session, now=current_time)
    visible_groups = (
        dashboard.hotspots,
        dashboard.topic_ideas,
        dashboard.collectible_ideas,
        dashboard.projects,
        dashboard.confirmations,
        dashboard.publish_reminders,
        dashboard.suggestions,
        dashboard.decision_items,
        dashboard.running_items,
        dashboard.finished_items,
    )
    matching_items = [
        item
        for group in visible_groups
        for item in group
        if item.task_id == task_id
    ]
    if not matching_items:
        raise TodayTaskNotFoundError(f"今日任务不存在或已失效：{task_id}")

    non_dismissible_ids = {
        item.task_id
        for group in (
            dashboard.projects,
            dashboard.confirmations,
            dashboard.running_items,
            dashboard.finished_items,
        )
        for item in group
    }
    if task_id in non_dismissible_ids or any(
        item.target_type == "finished" for item in matching_items
    ):
        raise TodayTaskNotDismissibleError(
            "该状态来自真实生产流程，不能在任务页标记完成；"
            "请先解决实际阻断或完成对应流程。"
        )

    date_key = _task_day_key(current_time)
    config = (
        session.query(AppConfig)
        .filter(AppConfig.key == TODAY_HANDLED_KEY)
        .one_or_none()
    )
    value = dict(config.value) if config and isinstance(config.value, dict) else {}
    handled = set(value.get(date_key, []))
    handled.add(task_id)
    value = {date_key: sorted(handled)}
    if config is None:
        config = AppConfig(key=TODAY_HANDLED_KEY, value=value, updated_at=current_time)
        session.add(config)
    else:
        config.value = value
        config.updated_at = current_time
    session.commit()
    return sorted(handled)


def serialize_today_dashboard(result: TodayDashboard) -> dict[str, Any]:
    return {
        "date": result.date,
        "hotspots": [_serialize_item(item) for item in result.hotspots],
        "topic_ideas": [_serialize_item(item) for item in result.topic_ideas],
        "collectible_ideas": [
            _serialize_item(item) for item in result.collectible_ideas
        ],
        "projects": [_serialize_item(item) for item in result.projects],
        "confirmations": [_serialize_item(item) for item in result.confirmations],
        "publish_reminders": [
            _serialize_item(item) for item in result.publish_reminders
        ],
        "suggestions": [_serialize_item(item) for item in result.suggestions],
        "decision_items": [_serialize_item(item) for item in result.decision_items],
        "running_items": [_serialize_item(item) for item in result.running_items],
        "finished_items": [_serialize_item(item) for item in result.finished_items],
        "handled_task_ids": result.handled_task_ids,
    }


def _top_hotspots(session: Session) -> list[TodayTaskItem]:
    rows = (
        session.query(HotItem)
        .filter(HotItem.is_filtered.is_(False))
        .filter(HotItem.lifecycle_status != "archived")
        .filter(HotItem.data_origin == "real")
        .order_by(
            HotItem.topic_score.desc(),
            HotItem.save_score.desc(),
            HotItem.hot_score.desc(),
            HotItem.published_at.desc(),
        )
        .limit(20)
        .all()
    )
    return [
        TodayTaskItem(
            task_id=f"hotspot_{item.id}",
            title=item.title,
            subtitle=f"{item.source_platform} · {item.category}",
            reason=(
                f"选题价值 {round(item.topic_score)}，收藏价值 {round(item.save_score)}，"
                f"热度 {round(item.hot_score)}。"
            ),
            next_action="查看热点",
            target_type="hotspot",
            target_id=item.id,
            priority=item.topic_score * 0.45 + item.save_score * 0.35 + item.hot_score * 0.2,
            meta={
                "summary": item.summary,
                "hot_score": item.hot_score,
                "topic_score": item.topic_score,
                "save_score": item.save_score,
            },
        )
        for item in rows
    ]


def _top_topic_ideas(
    session: Session,
    *,
    limit: int,
    collect_only: bool,
    now: datetime | None = None,
) -> list[TodayTaskItem]:
    rows = (
        session.query(TopicIdea)
        .join(HotItem, TopicIdea.hot_item_id == HotItem.id)
        .options(joinedload(TopicIdea.hot_item))
        .filter(HotItem.is_filtered.is_(False))
        .filter(HotItem.lifecycle_status != "archived")
        .filter(HotItem.data_origin == "real")
        .filter(TopicIdea.is_example.is_(False))
        .filter(TopicIdea.generation_source == "local_rule")
        .filter(TopicIdea.recommendation_bucket == "today")
        .order_by(
            case((TopicIdea.recommendation_bucket == "today", 0), else_=1),
            TopicIdea.recommendation_score.desc(),
            HotItem.save_score.desc() if collect_only else HotItem.topic_score.desc(),
            HotItem.topic_score.desc(),
            TopicIdea.id.desc(),
        )
        .limit(max(limit * 4, limit))
        .all()
    )
    tasks: list[TodayTaskItem] = []
    for idea in rows:
        if not _topic_recommendation_is_verifiable(session, idea, now=now):
            continue
        pipeline_id = _topic_pipeline_id(session, idea.id)
        hot_item = idea.hot_item
        tasks.append(
            TodayTaskItem(
                task_id=(
                    f"collect_topic_{idea.id}" if collect_only else f"topic_{idea.id}"
                ),
                title=_topic_display_title(idea),
                subtitle=f"来源：{hot_item.source_platform}",
                reason=idea.why_save if collect_only else idea.core_view,
                next_action="查看事件",
                target_type="topic",
                target_id=idea.id,
                priority=(
                    hot_item.save_score * 0.7 + hot_item.topic_score * 0.3
                    if collect_only
                    else hot_item.topic_score * 0.6 + hot_item.save_score * 0.4
                ),
                meta={
                    "recommended_format": idea.recommended_format,
                    "hook": idea.hook,
                    "hotspot_id": idea.hot_item_id,
                    "pipeline_id": pipeline_id,
                    "video_type": idea.video_type or "collection_event",
                    "video_type_label": _video_type_label(idea.video_type),
                    "recommendation_score": idea.recommendation_score,
                    "recommendation_bucket": idea.recommendation_bucket,
                    "source_hotspot_ids": idea.source_hotspot_ids or [idea.hot_item_id],
                    "source_hotspot_titles": idea.source_hotspot_titles or [hot_item.title],
                },
            )
        )
        if len(tasks) >= limit:
            break
    return tasks


def _topic_confirmation_task(
    session: Session,
    topic_ideas: list[TodayTaskItem],
    now: datetime,
) -> TodayTaskItem | None:
    if _has_script_started_today(session, now):
        return None
    selected_count = (
        session.query(TopicIdea)
        .filter(
            or_(
                TopicIdea.is_selected.is_(True),
                TopicIdea.recommendation_bucket == "today",
            )
        )
        .filter(TopicIdea.generation_source == "local_rule")
        .filter(TopicIdea.video_type.in_(["collection_event", "single_deep_dive_candidate"]))
        .count()
    )
    has_candidates = len(topic_ideas) > 0
    return TodayTaskItem(
        task_id="confirm_daily_events",
        title="待确认选题",
        subtitle=f"本期已选 {selected_count} 件事件" if has_candidates else "还没有候选事件",
        reason=(
            "选 3-6 件，组合成今天一期高密度视频。"
            if has_candidates
            else "先进入选题页更新今日事件，再选入本期。"
        ),
        next_action="去选题",
        target_type="topic_pool",
        target_id=None,
        priority=96,
        meta={
            "decision_type": "topic_confirmation",
            "selected_event_count": selected_count,
            "has_candidates": has_candidates,
        },
    )


def _script_confirmation_tasks(
    session: Session,
    items: list[PipelineListItem],
) -> list[TodayTaskItem]:
    tasks: list[TodayTaskItem] = []
    for item in items:
        if not _is_waiting_for_script_confirmation_item(item):
            continue
        script = session.get(ScriptAsset, item.script_asset_id)
        topic = (
            session.get(TopicIdea, item.topic_idea_id)
            if item.topic_idea_id is not None
            else None
        )
        event_titles = topic.source_hotspot_titles if topic and topic.source_hotspot_titles else []
        tasks.append(
            TodayTaskItem(
                task_id=f"confirm_voiceover_{item.pipeline_id}_{item.script_asset_id}",
                title="脚本确认",
                subtitle=item.project_name,
                reason="检查脚本、事件顺序和每段表达。",
                next_action="确认脚本",
                target_type="workspace",
                target_id=item.pipeline_id,
                priority=94,
                meta={
                    "decision_type": "script_confirmation",
                    "script_asset_id": item.script_asset_id,
                    "voiceover_preview": (script.voiceover[:120] if script else ""),
                    "source_hotspot_titles": event_titles[:6],
                    "video_type": topic.video_type if topic else "",
                    "video_type_label": _video_type_label(topic.video_type if topic else ""),
                },
            )
        )
    tasks.sort(key=lambda item: item.priority, reverse=True)
    return tasks[:1]


def _today_collection_topic(session: Session) -> TodayTaskItem | None:
    rows = (
        session.query(HotItem)
        .filter(HotItem.is_filtered.is_(False))
        .order_by(
            HotItem.topic_score.desc(),
            HotItem.hot_score.desc(),
            HotItem.save_score.desc(),
            HotItem.published_at.desc(),
        )
        .limit(5)
        .all()
    )
    if len(rows) < 3:
        return None
    average_priority = sum(
        item.topic_score * 0.55 + item.hot_score * 0.3 + item.save_score * 0.15
        for item in rows
    ) / len(rows)
    top_title = rows[0].title
    title = f"今日多热点合集：{top_title} 等 {len(rows)} 件事"
    return TodayTaskItem(
        task_id="collection_today_top_hotspots",
        title=title,
        subtitle=f"多热点合集型 · {len(rows)} 个公开热点",
        reason=(
            "适合做成一期「今天最值得看的几件事」：重要的放前面，"
            "每件事讲清发生了什么、为什么重要、普通人怎么看。"
        ),
        next_action="查看候选",
        target_type="topic_pool",
        target_id=None,
        priority=average_priority + 6,
        meta={
            "video_type": "multi_hotspot_collection",
            "video_type_label": "多热点合集型",
            "hotspot_ids": [item.id for item in rows],
            "hotspot_titles": [item.title for item in rows],
            "structure": [
                "开头：今天最值得看的几件事，不讲废话，直接说重点。",
                "第一件事：最重要，讲得稍微多一点。",
                "第二件事：次重要，讲清楚核心变化。",
                "第三件事：补充趋势或普通人影响。",
                "结尾：总结今天最值得记住的判断。",
            ],
        },
    )


def _project_tasks(items: list[PipelineListItem]) -> list[TodayTaskItem]:
    tasks: list[TodayTaskItem] = []
    for item in items:
        if item.completion_reason_code == "approved_artifact_verified":
            continue
        if _is_waiting_for_script_confirmation_item(item):
            continue
        has_active_running_step = item.completion_reason_code == "step_running"
        priority = 80 if item.needs_confirmation else 55
        if item.has_failed:
            priority += 15
        if item.status != "已完成":
            priority += 10
        if item.next_surface == "finished":
            priority += 10
        stage_title = _running_stage_label(
            item.current_step,
            has_failed=item.has_failed,
        )
        tasks.append(
            TodayTaskItem(
                task_id=f"project_{item.pipeline_id}",
                title=stage_title,
                subtitle=f"{item.completed_steps}/{item.total_steps} · {item.project_name}",
                reason=item.next_suggestion,
                next_action="查看进度",
                target_type=item.next_surface,
                target_id=item.pipeline_id,
                priority=priority,
                meta=_pipeline_task_meta(
                    item,
                    project_name=item.project_name,
                    completed_steps=item.completed_steps,
                    total_steps=item.total_steps,
                    has_failed=item.has_failed,
                    has_active_running_step=has_active_running_step,
                ),
            )
        )
    tasks.sort(key=lambda item: item.priority, reverse=True)
    return tasks


def _running_project_tasks(projects: list[TodayTaskItem]) -> list[TodayTaskItem]:
    return [
        item
        for item in projects
        if item.meta.get("status") == "进行中"
        and item.meta.get("has_active_running_step")
        and not item.meta.get("has_failed")
        and not item.meta.get("needs_confirmation")
    ]


def _is_waiting_for_script_confirmation_item(item: PipelineListItem) -> bool:
    return (
        item.script_asset_id is not None
        and item.completion_reason_code == "preparation_incomplete"
        and item.current_step == "生成素材任务"
        and item.completed_steps == 3
    )


def _has_script_started_today(session: Session, now: datetime) -> bool:
    local_day = now.astimezone(APP_TZ).date()
    start = datetime.combine(local_day, datetime.min.time(), tzinfo=APP_TZ).astimezone(
        timezone.utc
    )
    end = start + timedelta(days=1)
    return (
        session.query(ProductionPipeline.id)
        .filter(ProductionPipeline.script_asset_id.is_not(None))
        .filter(ProductionPipeline.created_at >= start)
        .filter(ProductionPipeline.created_at < end)
        .first()
        is not None
    )


def _running_stage_label(current_step: str, *, has_failed: bool) -> str:
    if has_failed:
        return "流程需要检查"
    labels = {
        "生成候选选题": "正在生成候选事件",
        "选择候选选题": "正在确定本期事件",
        "生成脚本素材": "正在生成脚本",
        "生成素材任务": "正在处理素材",
        "生成 Codex 图文任务": "正在补充图文",
        "生成数字人字段": "正在准备配音字段",
        "创建中文项目目录": "正在整理项目文件",
        "生成剪辑清单": "正在生成剪辑方案",
        "导出 script.md": "正在写出口播文件",
        "导出 shotlist.csv": "正在写出分镜表",
        "导出 edit_manifest.json": "正在写出剪辑清单",
    }
    return labels.get(current_step, f"正在{current_step or '推进流程'}")


def _finished_project_tasks(items: list[PipelineListItem]) -> list[TodayTaskItem]:
    tasks: list[TodayTaskItem] = []
    for item in items:
        if item.completion_reason_code != "approved_artifact_verified":
            continue
        tasks.append(
            TodayTaskItem(
                task_id=f"finished_project_{item.pipeline_id}",
                title=item.project_name,
                subtitle="成片导出任务已就绪",
                reason="可以检查时间轴、字幕、剪辑说明和最终确认状态。",
                next_action="预览",
                target_type="finished",
                target_id=item.pipeline_id,
                priority=76,
                meta=_pipeline_task_meta(
                    item,
                    completed_steps=item.completed_steps,
                    total_steps=item.total_steps,
                ),
            )
        )
    tasks.sort(key=lambda item: item.priority, reverse=True)
    return tasks


def _confirmation_tasks(
    session: Session,
    items: list[PipelineListItem],
) -> list[TodayTaskItem]:
    tasks: list[TodayTaskItem] = []
    for item in items:
        if not item.needs_confirmation:
            continue
        reason = item.failure_reason or item.next_suggestion
        tasks.append(
            TodayTaskItem(
                task_id=f"confirm_project_{item.pipeline_id}",
                title=item.project_name,
                subtitle=f"状态：{item.status}",
                reason=reason,
                next_action="去确认",
                target_type=item.next_surface,
                target_id=item.pipeline_id,
                priority=(
                    100
                    if item.has_failed
                    else 97
                    if item.next_surface == "finished"
                    else 85
                ),
                meta=_pipeline_task_meta(
                    item,
                    has_failed=item.has_failed,
                ),
            )
        )
    tasks.sort(key=lambda item: item.priority, reverse=True)
    return tasks


def _pipeline_task_meta(
    item: PipelineListItem,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "status": item.status,
        "completion_reason_code": item.completion_reason_code,
        "next_surface": item.next_surface,
        "current_step": item.current_step,
        "needs_confirmation": item.needs_confirmation,
        "next_suggestion": item.next_suggestion,
        **extra,
    }


def _publish_reminders(
    session: Session,
    now: datetime,
) -> list[TodayTaskItem]:
    posts = list_video_posts(session, page=1, page_size=100, now=now).items
    reviews = {
        item.video_post_id: item
        for item in list_video_reviews(session, page=1, page_size=100, now=now).items
    }
    reminders: list[TodayTaskItem] = []
    for post in posts:
        due = _due_snapshot_label(post, now)
        if due:
            reminders.append(
                TodayTaskItem(
                    task_id=f"post_update_{post.id}_{due}",
                    title=post.video_title,
                    subtitle=f"{post.platform} · {due}",
                    reason=f"这条视频到了「{due}」数据更新时间点。",
                    next_action="更新数据",
                    target_type="tracking",
                    target_id=post.id,
                    priority=86,
                    meta={"stage": due, "project_name": post.project_name},
                )
            )
        if post.current_status in {"掉速", "需要复盘"}:
            reminders.append(
                TodayTaskItem(
                    task_id=f"post_status_{post.id}_{post.current_status}",
                    title=post.video_title,
                    subtitle=f"当前状态：{post.current_status}",
                    reason="数据表现异常，建议检查开头、节奏或选题匹配。",
                    next_action="查看发布数据",
                    target_type="tracking",
                    target_id=post.id,
                    priority=82,
                    meta={"status": post.current_status},
                )
            )
        review = reviews.get(post.id)
        if review and review.judgement in {"有潜力", "值得继续做"}:
            reminders.append(
                TodayTaskItem(
                    task_id=f"review_{post.id}_{review.judgement}",
                    title=post.video_title,
                    subtitle=f"复盘判断：{review.judgement}",
                    reason=review.next_suggestion,
                    next_action="查看复盘",
                    target_type="review",
                    target_id=post.id,
                    priority=90 if review.judgement == "值得继续做" else 78,
                    meta={
                        "favorite_rate": review.favorite_rate,
                        "share_rate": review.share_rate,
                    },
                )
            )
    reminders.sort(key=lambda item: item.priority, reverse=True)
    return reminders


def _suggestions(
    *,
    hotspots: list[TodayTaskItem],
    topic_ideas: list[TodayTaskItem],
    projects: list[TodayTaskItem],
    confirmations: list[TodayTaskItem],
    publish_reminders: list[TodayTaskItem],
) -> list[TodayTaskItem]:
    suggestions: list[TodayTaskItem] = []
    if hotspots:
        suggestions.append(
            _suggestion_from(
                "suggest_hotspot",
                hotspots[0],
                f"先看热点「{hotspots[0].title}」。",
            )
        )
    if topic_ideas:
        suggestions.append(
            _suggestion_from(
                "suggest_topic",
                topic_ideas[0],
                f"今天最值得做的选题：{topic_ideas[0].title}。",
            )
        )
    if confirmations:
        suggestions.append(
            _suggestion_from(
                "suggest_confirm",
                confirmations[0],
                f"先处理需要确认的项目：{confirmations[0].title}。",
            )
        )
    elif projects:
        suggestions.append(
            _suggestion_from(
                "suggest_project",
                projects[0],
                f"继续推进内容项目：{projects[0].title}。",
            )
        )
    if publish_reminders:
        suggestions.append(
            _suggestion_from(
                "suggest_publish",
                publish_reminders[0],
                publish_reminders[0].reason,
            )
        )
    return suggestions


def _suggestion_from(task_id: str, source: TodayTaskItem, reason: str) -> TodayTaskItem:
    return TodayTaskItem(
        task_id=task_id,
        title=reason,
        subtitle=source.subtitle,
        reason=source.reason,
        next_action=source.next_action,
        target_type=source.target_type,
        target_id=source.target_id,
        priority=source.priority,
        meta=source.meta,
    )


def _video_type_label(video_type: str) -> str:
    if video_type == "multi_hotspot_collection":
        return "多热点合集型"
    if video_type == "single_deep_dive_candidate":
        return "可单独深挖"
    return "合集事件"


def _topic_recommendation_is_verifiable(
    session: Session,
    idea: TopicIdea,
    *,
    now: datetime | None = None,
) -> bool:
    source_ids = idea.source_hotspot_ids or [idea.hot_item_id]
    hotspots = session.query(HotItem).filter(HotItem.id.in_(source_ids)).all()
    if len(hotspots) != len(set(source_ids)):
        return False
    return all(is_today_recommendation_eligible(item, now=now) for item in hotspots)


def _handled_task_ids(session: Session, now: datetime) -> set[str]:
    date_key = _task_day_key(now)
    config = (
        session.query(AppConfig)
        .filter(AppConfig.key == TODAY_HANDLED_KEY)
        .one_or_none()
    )
    if config is None or not isinstance(config.value, dict):
        return set()
    return set(config.value.get(date_key, []))


def _task_day_key(now: datetime) -> str:
    local_now = now.astimezone(APP_TZ)
    refresh_at = local_now.replace(
        hour=TASK_REFRESH_HOUR,
        minute=TASK_REFRESH_MINUTE,
        second=0,
        microsecond=0,
    )
    if local_now < refresh_at:
        local_now = local_now - timedelta(days=1)
    return local_now.date().isoformat()


def _topic_display_title(idea: TopicIdea) -> str:
    source_titles = [
        str(title).strip()
        for title in (idea.source_hotspot_titles or [])
        if str(title).strip()
    ]
    if source_titles:
        return " / ".join(source_titles[:5])
    if idea.hot_item and idea.hot_item.title:
        return idea.hot_item.title
    return idea.title


def _topic_pipeline_id(session: Session, topic_idea_id: int) -> int | None:
    row = (
        session.query(ProductionPipeline.id)
        .filter(ProductionPipeline.topic_idea_id == topic_idea_id)
        .order_by(ProductionPipeline.id.desc())
        .first()
    )
    return row[0] if row else None


def _due_snapshot_label(post, now: datetime) -> str:
    if post.published_at is None:
        return ""
    elapsed = max((now - post.published_at).total_seconds() / 60, 0)
    stages = [
        (30, "30分钟"),
        (60, "1小时"),
        (120, "2小时"),
        (240, "4小时"),
        (1440, "24小时"),
        (4320, "3天"),
        (10080, "7天"),
    ]
    due_label = ""
    for minutes, label in stages:
        if elapsed >= minutes and not any(
            snapshot.elapsed_minutes >= minutes for snapshot in post.snapshots
        ):
            due_label = label
            break
    return due_label


def _serialize_item(item: TodayTaskItem) -> dict[str, Any]:
    return {
        "task_id": item.task_id,
        "title": item.title,
        "subtitle": item.subtitle,
        "reason": item.reason,
        "next_action": item.next_action,
        "target_type": item.target_type,
        "target_id": item.target_id,
        "priority": round(item.priority, 2),
        "meta": item.meta,
        "is_done": item.is_done,
    }


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
