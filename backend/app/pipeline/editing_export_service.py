from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_configured_path
from app.models import AppConfig, HotItem, PipelineStep, ProductionPipeline, ScriptAsset, TopicIdea
from app.pipeline.material_candidate_gate import assert_pipeline_candidate_review_ready
from app.pipeline.material_candidate_contract import CandidateContractError, CandidateSet
from app.pipeline.timeline_spec import (
    TimelineSpecLedger,
    TimelineSpecSnapshot,
    build_timeline_spec_payload,
    timeline_spec_edit_graph_hash,
    timeline_spec_input_hash,
)
from app.pipeline.project_identity import (
    PreparedProjectChildDirectoryUse,
    begin_prepared_project_child_directory_use,
    begin_prepared_project_directory_use,
    write_prepared_project_files,
)
from app.pipeline.workspace_service import script_use_avatar


EDITING_EXPORT_CONFIG_PREFIX = "editing_export_package:"


class EditingExportNotFoundError(ValueError):
    pass


@dataclass(slots=True)
class EditingExportPackage:
    pipeline_id: int
    status: str
    project_name: str
    project_key: str
    project_dir: str
    legacy_project_dir: str | None
    timeline: list[dict[str, Any]] = field(default_factory=list)
    versions: list[dict[str, Any]] = field(default_factory=list)
    recommended_template: dict[str, Any] = field(default_factory=dict)
    export_task: dict[str, Any] = field(default_factory=dict)
    generated_files: dict[str, str] = field(default_factory=dict)
    timeline_spec: dict[str, Any] = field(default_factory=dict)
    timeline_sync: dict[str, Any] = field(default_factory=dict)
    subtitle_preview: str = ""
    note: str = ""


def build_editing_export_package(
    session: Session,
    pipeline_id: int,
    *,
    write_files: bool = True,
    project_root: Path | None = None,
) -> EditingExportPackage:
    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None:
        raise EditingExportNotFoundError(f"流程不存在：{pipeline_id}")
    topic = session.get(TopicIdea, pipeline.topic_idea_id) if pipeline.topic_idea_id else None
    hotspot = session.get(HotItem, pipeline.hotspot_id)
    script = session.get(ScriptAsset, pipeline.script_asset_id) if pipeline.script_asset_id else None
    steps = {step.name: step for step in pipeline.steps}
    material_tasks = _step_items(steps, "生成素材任务", "material_tasks")
    candidate_states: dict[str, CandidateSet] = {}
    if write_files:
        candidate_states = assert_pipeline_candidate_review_ready(
            pipeline,
            [str(item.get("shot_id") or "") for item in material_tasks],
        )
    else:
        candidate_states = _read_locked_candidate_states(pipeline)
    graphic_tasks = _step_items(steps, "生成 Codex 图文任务", "graphic_tasks")
    if not graphic_tasks:
        graphic_tasks = _step_items(steps, "生成素材任务", "graphic_task_candidates")
    use_avatar = script_use_avatar(script)
    avatar_tasks = _step_items(steps, "生成数字人字段", "avatar_tasks") if use_avatar else []
    timeline = _build_timeline(
        script,
        material_tasks,
        graphic_tasks,
        avatar_tasks,
        candidate_states=candidate_states,
    )
    recommended_template = _build_recommended_template(timeline)
    versions = [recommended_template]
    subtitle_text = _build_srt(script, timeline)
    resolved_project_dir: Path | None = None
    directory_use = None
    approved_project_root = resolve_configured_path(
        project_root or get_settings().project_root_dir
    )
    if pipeline.project_dir:
        directory_use = begin_prepared_project_directory_use(
            pipeline,
            project_root=approved_project_root,
        )
        resolved_project_dir = directory_use.path
    timeline_spec_payload = (
        build_timeline_spec_payload(
            timeline,
            selected_candidate_ids=_locked_candidate_ids(pipeline),
        )
        if timeline
        else None
    )
    timeline_spec: dict[str, Any] = {}
    timeline_sync: dict[str, Any] = {
        "status": "unsaved",
        "current_version_id": "",
        "previous_version_id": "",
        "desired_input_hash": "",
        "affected_segment_ids": [],
        "created_new_version": False,
    }
    if resolved_project_dir is not None and timeline_spec_payload is not None:
        timeline_ledger = TimelineSpecLedger(
            pipeline,
            project_root=approved_project_root,
        )
        timeline_versions = timeline_ledger.list()
        previous = timeline_versions[-1] if timeline_versions else None
        edit_graph_base = _editing_graph_base(timeline_versions, previous)
        desired_input_hash = timeline_spec_input_hash(timeline_spec_payload)
        desired_graph_hash = timeline_spec_edit_graph_hash(timeline_spec_payload)
        previous_graph_hash = (
            timeline_spec_edit_graph_hash(_snapshot_payload(edit_graph_base))
            if edit_graph_base is not None
            else ""
        )
        affected_segment_ids = _affected_segment_ids(edit_graph_base, timeline_spec_payload)
        if write_files:
            active = (
                previous
                if previous is not None
                and edit_graph_base is not None
                and previous_graph_hash == desired_graph_hash
                else timeline_ledger.create(
                    timeline_spec_payload,
                    source="editing_export",
                    change_base_version_id=(
                        edit_graph_base.version_id if edit_graph_base is not None else None
                    ),
                )
            )
            timeline_spec = active.public_dict()
            timeline_sync = {
                "status": "current",
                "current_version_id": active.version_id,
                "previous_version_id": previous.version_id if previous is not None else "",
                "desired_input_hash": desired_input_hash,
                "affected_segment_ids": affected_segment_ids,
                "created_new_version": (
                    previous is None or active.version_id != previous.version_id
                ),
            }
        else:
            timeline_spec = (
                previous.public_dict()
                if previous is not None
                else {
                    "status": "未保存",
                    "segments": timeline_spec_payload["segments"],
                    "tracks": timeline_spec_payload["tracks"],
                    "settings": timeline_spec_payload["settings"],
                }
            )
            graph_is_current = (
                previous is not None
                and edit_graph_base is not None
                and previous_graph_hash == desired_graph_hash
            )
            timeline_sync = {
                "status": (
                    "current"
                    if graph_is_current
                    else "stale" if previous is not None else "unsaved"
                ),
                "current_version_id": previous.version_id if previous is not None else "",
                "previous_version_id": previous.version_id if previous is not None else "",
                "desired_input_hash": desired_input_hash,
                "affected_segment_ids": [] if graph_is_current else affected_segment_ids,
                "created_new_version": False,
            }
    status = _export_status(resolved_project_dir is not None, timeline)
    export_task = {
        "pipeline_id": pipeline.id,
        "status": status,
        "need_final_confirmation": True,
        "version_count": len(versions),
        "timeline_version_id": timeline_spec.get("version_id", ""),
        "timeline_version_number": timeline_spec.get("version_number", 0),
        "material_ready_count": sum(1 for item in timeline if item.get("material_path")),
        "timeline_segment_count": len(timeline),
        "real_jianying_automation": False,
        "official_capture_executed": False,
        "pexels_download_executed": False,
        "info_motion_render_executed": False,
        "real_avatar_generation": False,
        "real_publish_executed": False,
    }
    package = EditingExportPackage(
        pipeline_id=pipeline.id,
        status=status,
        project_name=pipeline.project_name,
        project_key=pipeline.project_key,
        project_dir=str(resolved_project_dir or ""),
        legacy_project_dir=pipeline.legacy_project_dir,
        timeline=timeline,
        versions=versions,
        recommended_template=recommended_template,
        export_task=export_task,
        timeline_spec=timeline_spec,
        timeline_sync=timeline_sync,
        subtitle_preview=subtitle_text[:800],
        note=(
            "V1 只生成自动剪辑数据结构、字幕和一个智能推荐成片方案；"
            "素材流改为官方页面录屏、Pexels 和信息动效；不操作剪映桌面版、不生成真实数字人、不自动发布。"
        ),
    )
    if write_files and resolved_project_dir is not None:
        info_use = begin_prepared_project_child_directory_use(
            pipeline,
            project_root=approved_project_root,
            relative_path="素材信息",
            create=False,
        )
        package.generated_files = _write_editing_files(
            info_use,
            package,
            topic=topic,
            hotspot=hotspot,
            subtitle_text=subtitle_text,
        )
        info_use.verify()
        directory_use.verify()
        _save_package_state(session, package)
    elif directory_use is not None:
        directory_use.verify()
    return package


def serialize_editing_export_package(package: EditingExportPackage) -> dict[str, Any]:
    return {
        "pipeline_id": package.pipeline_id,
        "status": package.status,
        "project_name": package.project_name,
        "project_key": package.project_key,
        "project_dir": package.project_dir,
        "legacy_project_dir": package.legacy_project_dir,
        "timeline": package.timeline,
        "versions": package.versions,
        "recommended_template": package.recommended_template,
        "export_task": package.export_task,
        "generated_files": package.generated_files,
        "timeline_spec": package.timeline_spec,
        "timeline_sync": package.timeline_sync,
        "subtitle_preview": package.subtitle_preview,
        "note": package.note,
    }


def _snapshot_payload(snapshot: Any) -> dict[str, Any]:
    return {
        "tracks": [dict(item) for item in getattr(snapshot, "tracks", ())],
        "segments": [dict(item) for item in getattr(snapshot, "segments", ())],
        "settings": dict(getattr(snapshot, "settings", {}) or {}),
        "artifacts": dict(getattr(snapshot, "artifacts", {}) or {}),
    }


def _editing_graph_base(
    versions: list[TimelineSpecSnapshot],
    latest: TimelineSpecSnapshot | None,
) -> TimelineSpecSnapshot | None:
    """Skip render-only timing bindings when comparing user editing intent.

    ``audio_first_timing`` snapshots are valid render inputs, but their scaled
    timestamps and bound artifacts are outputs of rendering preparation.  They
    are not material selections or editor changes.  Walking their explicit
    parent chain keeps the append-only lineage while comparing the next edit to
    the last logical editing graph.
    """

    if latest is None:
        return None
    by_id = {item.version_id: item for item in versions}
    current = latest
    visited: set[str] = set()
    while current.source == "audio_first_timing" and current.parent_version_id:
        if current.version_id in visited:
            break
        visited.add(current.version_id)
        parent = by_id.get(current.parent_version_id)
        if parent is None:
            break
        current = parent
    return current


def _affected_segment_ids(
    previous: Any,
    desired_payload: dict[str, Any],
) -> list[str]:
    desired_segments = [
        dict(item)
        for item in desired_payload.get("segments", [])
        if isinstance(item, dict)
    ]
    desired_ids = [str(item.get("segment_id") or "") for item in desired_segments]
    if previous is None:
        return [item for item in desired_ids if item]
    previous_payload = _snapshot_payload(previous)
    previous_segments = previous_payload["segments"]
    previous_ids = [str(item.get("segment_id") or "") for item in previous_segments]
    global_changed = (
        previous_payload["tracks"] != desired_payload.get("tracks")
        or previous_payload["settings"] != desired_payload.get("settings")
    )
    ordered_ids = list(dict.fromkeys([*desired_ids, *previous_ids]))
    if global_changed:
        return [item for item in ordered_ids if item]
    previous_by_id = {
        str(item.get("segment_id") or ""): item for item in previous_segments
    }
    desired_by_id = {
        str(item.get("segment_id") or ""): item for item in desired_segments
    }
    return [
        segment_id
        for segment_id in ordered_ids
        if segment_id
        and previous_by_id.get(segment_id) != desired_by_id.get(segment_id)
    ]


def _step_items(
    steps: dict[str, PipelineStep],
    step_name: str,
    key: str,
) -> list[dict[str, Any]]:
    step = steps.get(step_name)
    payload = step.output_summary if step is not None and step.output_summary else {}
    items = payload.get(key, []) if isinstance(payload, dict) else []
    return [dict(item) for item in items if isinstance(item, dict)]


def _locked_candidate_ids(pipeline: ProductionPipeline) -> dict[str, str]:
    """Read selected IDs without making the timeline service own candidate state."""

    step = next(
        (item for item in pipeline.steps if item.name == "生成素材任务"),
        None,
    )
    payload = step.output_summary if step else {}
    raw = payload.get("candidate_pool") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    if "shot_id" in raw and "candidates" in raw:
        raw = {str(raw.get("shot_id") or ""): raw}
    result: dict[str, str] = {}
    for shot_id, pool in raw.items():
        if not isinstance(pool, dict):
            continue
        locked_id = str(pool.get("locked_candidate_id") or "").strip()
        if not locked_id:
            for candidate in pool.get("candidates", []):
                if isinstance(candidate, dict) and candidate.get("status") == "locked":
                    locked_id = str(candidate.get("candidate_id") or "").strip()
                    break
        if locked_id:
            result[str(shot_id)] = locked_id
    return result


def _read_locked_candidate_states(
    pipeline: ProductionPipeline,
) -> dict[str, CandidateSet]:
    """Read valid locked rows for read-only package previews.

    The write path uses the stricter gate above. Read-only callers should not
    be blocked by an incomplete pool, but a valid lock must still be reflected
    in the timeline so the UI and eventual export do not show two material
    truths.
    """

    step = next(
        (item for item in pipeline.steps if item.name == "生成素材任务"),
        None,
    )
    payload = step.output_summary if step else {}
    raw = payload.get("candidate_pool") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    if "shot_id" in raw and "candidates" in raw:
        raw = {str(raw.get("shot_id") or ""): raw}
    result: dict[str, CandidateSet] = {}
    for shot_id, value in raw.items():
        if not isinstance(shot_id, str) or not isinstance(value, dict):
            continue
        try:
            state = CandidateSet.from_dict(value)
        except CandidateContractError:
            continue
        if state.shot_id == shot_id and state.locked_candidate is not None:
            result[shot_id] = state
    return result


def _build_timeline(
    script: ScriptAsset | None,
    material_tasks: list[dict[str, Any]],
    graphic_tasks: list[dict[str, Any]],
    avatar_tasks: list[dict[str, Any]],
    *,
    candidate_states: dict[str, CandidateSet] | None = None,
) -> list[dict[str, Any]]:
    candidate_states = candidate_states or {}
    graphic_by_shot = {item.get("shot_id"): item for item in graphic_tasks}
    avatar_by_shot = {
        item.get("shot_id"): item
        for item in avatar_tasks
        if item.get("required", False)
    }
    timeline: list[dict[str, Any]] = []
    if material_tasks:
        for index, task in enumerate(material_tasks, start=1):
            shot_id = str(task.get("shot_id") or f"shot-{index:02d}")
            avatar_task = avatar_by_shot.get(shot_id, {})
            locked = candidate_states.get(shot_id)
            locked_candidate = locked.locked_candidate if locked is not None else None
            timeline.append(
                {
                    "segment_id": shot_id,
                    "start": str(task.get("start", "")),
                    "end": str(task.get("end", "")),
                    "voiceover": str(task.get("voiceover", "")),
                    "visual_type": str(task.get("material_type", "")),
                    "visual_direction": str(task.get("recommended_visual", "")),
                    "material_keywords": _as_list(
                        task.get("asset_keywords") or task.get("jianying_keywords")
                    ),
                    "material_sources": _as_list(task.get("source_priority")),
                    "material_status": (
                        "已锁定"
                        if locked_candidate is not None
                        else str(task.get("status", "未开始"))
                    ),
                    "material_path": str(
                        locked_candidate.preview_path
                        if locked_candidate is not None
                        else task.get("material_path", "")
                    ),
                    "selected_candidate_id": (
                        locked_candidate.candidate_id if locked_candidate is not None else ""
                    ),
                    "evidence_kind": str(
                        locked_candidate.evidence_kind
                        if locked_candidate is not None
                        else task.get("evidence_kind", "")
                    ),
                    "source_url": str(
                        locked_candidate.url
                        if locked_candidate is not None
                        else task.get("source_url", "")
                    ),
                    "is_locally_generated": task.get("is_locally_generated") is True,
                    "needs_graphic": bool(task.get("needs_codex_graphic", False)),
                    "graphic_task": graphic_by_shot.get(shot_id, {}),
                    "avatar_task": avatar_task,
                    "subtitle_strategy": (
                        "跟随口播，避开右下角数字人区域"
                        if avatar_task
                        else "跟随配音和字幕，不预留数字人区域"
                    ),
                    "edit_instruction": _edit_instruction(task),
                }
            )
        return timeline

    storyboard = script.storyboard if script is not None and script.storyboard else []
    for index, shot in enumerate(storyboard, start=1):
        if not isinstance(shot, dict):
            continue
        timeline.append(
            {
                "segment_id": f"shot-{index:02d}",
                "start": str(shot.get("start", "")),
                "end": str(shot.get("end", "")),
                "voiceover": str(shot.get("voiceover", "")),
                "visual_type": "待生成素材任务",
                "visual_direction": str(shot.get("recommended_visual", "")),
                "material_keywords": [],
                "material_sources": ["素材任务尚未生成"],
                "material_status": "等待素材任务",
                "material_path": "",
                "evidence_kind": "",
                "source_url": "",
                "is_locally_generated": False,
                "needs_graphic": False,
                "graphic_task": {},
                "avatar_task": {},
                "subtitle_strategy": "跟随口播",
                "edit_instruction": "先保留口播段落，等待素材任务自动补齐。",
            }
        )
    return timeline


def _build_recommended_template(timeline: list[dict[str, Any]]) -> dict[str, Any]:
    base_duration = _duration_seconds(timeline)
    needs_avatar = any(item.get("avatar_task") for item in timeline)
    return {
        "version_id": "recommended-main",
        "name": "智能推荐模板",
        "status": "待最终确认",
        "estimated_duration_seconds": base_duration,
        "strategy": (
            "按重要性和口播节奏生成一版主成片方案，前段信息密度更高，"
            "中段用图文/PPT/图表承接解释，字幕跟随口播。"
        ),
        "best_for": "V1 默认成片；确认后再决定是否发布或局部重做。",
        "template": "plain-explainer-with-visual-cards",
        "requires_avatar_reconfirmation": needs_avatar,
        "avatar_note": (
            "数字人已启用；真实生成会消耗额度，必须人工确认后再执行。"
            if needs_avatar
            else "本期未启用数字人，按配音、字幕和画面剪辑。"
        ),
    }


def _write_editing_files(
    info_use: PreparedProjectChildDirectoryUse,
    package: EditingExportPackage,
    *,
    topic: TopicIdea | None,
    hotspot: HotItem | None,
    subtitle_text: str,
) -> dict[str, str]:
    filenames = {
        "timeline_json": "timeline.json",
        "rough_cut_plan_json": "rough_cut_plan.json",
        "subtitles_srt": "subtitles.srt",
        "edit_notes_md": "剪辑说明.md",
    }
    contents = {
        "timeline.json": (
            json.dumps(
            {
                "pipeline_id": package.pipeline_id,
                "project_name": package.project_name,
                "timeline_spec": package.timeline_spec,
                "timeline": package.timeline,
            },
            ensure_ascii=False,
            indent=2,
            )
        ).encode("utf-8"),
        "rough_cut_plan.json": (
            json.dumps(
            {
                "pipeline_id": package.pipeline_id,
                "status": package.status,
                "export_task": package.export_task,
                "timeline_spec": package.timeline_spec,
                "timeline": package.timeline,
                "recommended_template": package.recommended_template,
                "versions": package.versions,
                "safety": {
                    "real_jianying_automation": False,
                    "official_capture_executed": False,
                    "pexels_download_executed": False,
                    "info_motion_render_executed": False,
                    "real_avatar_generation": False,
                    "real_publish_executed": False,
                },
            },
            ensure_ascii=False,
            indent=2,
            )
        ).encode("utf-8"),
        "subtitles.srt": subtitle_text.encode("utf-8"),
        "剪辑说明.md": _notes_md(
            package,
            topic=topic,
            hotspot=hotspot,
        ).encode("utf-8"),
    }
    written = write_prepared_project_files(info_use, contents)
    return {key: str(written[filename]) for key, filename in filenames.items()}


def _notes_md(
    package: EditingExportPackage,
    *,
    topic: TopicIdea | None,
    hotspot: HotItem | None,
) -> str:
    title = topic.title if topic else package.project_name
    lines = [
        f"# {title}",
        "",
        "## 自动剪辑状态",
        "",
        f"- 项目状态：{package.status}",
        "- 当前只生成剪辑清单、时间轴、SRT 字幕和成片方案。",
        "- 素材流使用官方页面录屏、Pexels 和信息动效任务；不操作剪映桌面版，不生成真实数字人，不自动发布。",
        "",
        "## 来源热点",
        "",
        f"- {hotspot.title if hotspot else ''}",
        "",
        "## 时间轴",
        "",
    ]
    for segment in package.timeline:
        lines.append(
            f"- {segment.get('start', '')}-{segment.get('end', '')}："
            f"{segment.get('voiceover', '')}｜画面：{segment.get('visual_type', '')}"
        )
    lines.extend(["", "## 智能推荐模板", ""])
    template = package.recommended_template or (package.versions[0] if package.versions else {})
    if template:
        lines.append(
            f"- {template.get('name', '')}：{template.get('strategy', '')}"
            f"（状态：{template.get('status', '')}）"
        )
        lines.append(
            f"- 数字人说明：{template.get('avatar_note', '数字人重做必须人工确认。')}"
        )
    return "\n".join(lines)


def _build_srt(script: ScriptAsset | None, timeline: list[dict[str, Any]]) -> str:
    subtitles = script.subtitles if script is not None and script.subtitles else []
    rows: list[str] = []
    if subtitles:
        for index, item in enumerate(subtitles, start=1):
            if not isinstance(item, dict):
                continue
            rows.extend(
                [
                    str(index),
                    f"{_srt_time(str(item.get('start', '')))} --> {_srt_time(str(item.get('end', '')))}",
                    str(item.get("text", "")),
                    "",
                ]
            )
    else:
        for index, item in enumerate(timeline, start=1):
            rows.extend(
                [
                    str(index),
                    f"{_srt_time(str(item.get('start', '')))} --> {_srt_time(str(item.get('end', '')))}",
                    str(item.get("voiceover", "")),
                    "",
                ]
            )
    return "\n".join(rows).strip() + "\n" if rows else ""


def _srt_time(value: str) -> str:
    parts = [part for part in value.replace(".", ":").split(":") if part != ""]
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        numbers = [0, 0]
    if len(numbers) == 1:
        hours, minutes, seconds = 0, 0, numbers[0]
    elif len(numbers) == 2:
        hours, minutes, seconds = 0, numbers[0], numbers[1]
    else:
        hours, minutes, seconds = numbers[-3], numbers[-2], numbers[-1]
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},000"


def _export_status(
    has_valid_project_directory: bool,
    timeline: list[dict[str, Any]],
) -> str:
    if not has_valid_project_directory:
        return "等待项目目录"
    if not timeline:
        return "等待脚本或素材任务"
    if any(item.get("material_status") in {"失败", "需要人工确认"} for item in timeline):
        return "需要素材确认"
    return "已生成待确认"


def _edit_instruction(task: dict[str, Any]) -> str:
    visual = str(task.get("recommended_visual", "") or "按素材任务匹配画面")
    keywords = "、".join(_as_list(task.get("asset_keywords") or task.get("jianying_keywords")))
    keyword_text = f"；关键词：{keywords}" if keywords else ""
    return f"{visual}{keyword_text}；字幕跟随口播节奏。"


def _duration_seconds(timeline: list[dict[str, Any]]) -> int:
    total = 0
    for item in timeline:
        start = _seconds(str(item.get("start", "")))
        end = _seconds(str(item.get("end", "")))
        if end > start:
            total += end - start
    return max(total, len(timeline) * 8, 45)


def _seconds(value: str) -> int:
    parts = [part for part in value.split(":") if part != ""]
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return 0
    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return numbers[-3] * 3600 + numbers[-2] * 60 + numbers[-1]


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _save_package_state(session: Session, package: EditingExportPackage) -> None:
    key = f"{EDITING_EXPORT_CONFIG_PREFIX}{package.pipeline_id}"
    value = {
        "status": package.status,
        "project_dir": package.project_dir,
        "generated_files": package.generated_files,
        "version_count": len(package.versions),
        "recommended_template": package.recommended_template,
        "timeline_segment_count": len(package.timeline),
    }
    item = session.query(AppConfig).filter(AppConfig.key == key).one_or_none()
    if item is None:
        item = AppConfig(key=key, value=value)
        session.add(item)
    else:
        item.value = value
        session.add(item)
    session.commit()
