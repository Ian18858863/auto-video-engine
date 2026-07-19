from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from app.materials.planner import GraphicTask, MaterialTask
from app.models import ScriptAsset, TopicIdea


def export_project_files(
    project_dir: Path,
    *,
    pipeline_id: int,
    provider: str,
    model: str,
    topic_idea: TopicIdea | None,
    script_asset: ScriptAsset | None,
    material_tasks: list[MaterialTask],
    graphic_tasks: list[GraphicTask],
) -> dict[str, str]:
    info_dir = project_dir / "素材信息"
    info_dir.mkdir(parents=True, exist_ok=True)
    script_path = write_script_md(project_dir, topic_idea, script_asset, material_tasks)
    shotlist_path = write_shotlist_csv(project_dir, material_tasks)
    manifest_path = write_edit_manifest_json(
        project_dir,
        pipeline_id=pipeline_id,
        provider=provider,
        model=model,
        topic_idea=topic_idea,
        script_asset=script_asset,
        material_tasks=material_tasks,
        graphic_tasks=graphic_tasks,
    )
    return {
        "script_md": str(script_path),
        "shotlist_csv": str(shotlist_path),
        "edit_manifest_json": str(manifest_path),
    }


def material_task_to_dict(task: MaterialTask) -> dict[str, Any]:
    return {
        "镜头编号": task.shot_id,
        "时间段": f"{task.start}-{task.end}",
        "口播内容": task.voiceover,
        "推荐画面": task.recommended_visual,
        "素材类型": task.material_type,
        "素材关键词": task.asset_keywords or task.jianying_keywords,
        "是否需要来源页面录屏": task.needs_official_capture,
        "是否需要 Pexels": task.needs_pexels,
        "是否需要信息动效": task.needs_info_motion,
        "是否需要备用素材": task.needs_backup_asset,
        "是否需要 Codex 图文": task.needs_codex_graphic,
        "是否需要 Codex 图表": task.needs_codex_chart,
        "是否需要 Codex 信息卡片": task.needs_codex_info_card,
        "是否需要数字人": task.avatar_required,
        "数字人位置": task.avatar_position,
        "数字人状态": task.avatar_state,
        "数字人素材已生成": bool(task.avatar_material_path),
        "素材状态": task.status,
        "素材路径": task.material_path,
        "evidence_kind": task.evidence_kind,
        "source_url": task.source_url,
        "is_locally_generated": task.is_locally_generated,
        "beat_id": task.beat_id,
        "beat_type": task.beat_type,
        "viewer_takeaway": task.viewer_takeaway,
        "visual_intent": task.visual_intent,
        "source_refs": task.source_refs,
        "acceptance_conditions": task.acceptance_conditions,
        "forbidden_items": task.forbidden_items,
        "director_version": task.director_version,
    }


def graphic_task_to_dict(task: GraphicTask) -> dict[str, Any]:
    return {
        "镜头编号": task.shot_id,
        "时间段": task.time_range,
        "图文形式": task.visual_form,
        "核心信息": task.core_message,
        "关键词": task.keywords,
        "是否需要图表": task.needs_chart,
        "是否需要清单卡片": task.needs_checklist_card,
        "是否需要流程图": task.needs_flowchart,
        "是否需要对比": task.needs_compare,
        "保存目录": task.save_path,
        "是否已调用 Codex skill": task.generated_by_codex_skill,
        "状态": "未开始",
    }


def write_script_md(
    project_dir: Path,
    topic_idea: TopicIdea | None,
    script_asset: ScriptAsset | None,
    tasks: list[MaterialTask],
) -> Path:
    info_dir = project_dir / "素材信息"
    info_dir.mkdir(parents=True, exist_ok=True)
    path = info_dir / "script.md"
    title = topic_idea.title if topic_idea else "待人工确认的短视频项目"
    lines = [f"# {title}", "", "## 口播稿", ""]
    lines.append(script_asset.voiceover if script_asset else "脚本尚未生成。")
    lines.extend(["", "## 分镜", ""])
    if script_asset and script_asset.storyboard:
        for shot in script_asset.storyboard:
            lines.append(
                f"- {shot.get('start', '')}-{shot.get('end', '')}："
                f"{shot.get('voiceover', '')}｜画面：{shot.get('recommended_visual', '')}"
            )
    else:
        lines.append("- 分镜尚未生成。")
    lines.extend(["", "## 字幕", ""])
    if script_asset and script_asset.subtitles:
        for subtitle in script_asset.subtitles:
            lines.append(
                f"- {subtitle.get('start', '')}-{subtitle.get('end', '')}："
                f"{subtitle.get('text', '')}"
            )
    else:
        lines.append("- 字幕尚未生成。")
    lines.extend(["", "## 素材说明", ""])
    if tasks:
        for task in tasks:
            lines.append(
                f"- {task.start}-{task.end}：{task.material_type}；"
                f"素材关键词：{'、'.join(task.asset_keywords or task.jianying_keywords)}；"
                f"优先来源：{'、'.join(task.source_priority)}；状态：{task.status}"
            )
    else:
        lines.append("- 素材任务尚未生成。")
    lines.extend(
        [
            "",
            "## 数字人说明",
            "",
            "- 数字人仅作为右下角辅助讲解层，不遮挡字幕、图表和关键词。",
            "- 本轮只生成出镜标记，不生成真实数字人素材或口型同步。",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_shotlist_csv(project_dir: Path, tasks: list[MaterialTask]) -> Path:
    info_dir = project_dir / "素材信息"
    info_dir.mkdir(parents=True, exist_ok=True)
    path = info_dir / "shotlist.csv"
    fieldnames = [
        "时间段",
        "口播内容",
        "推荐画面",
        "素材类型",
        "素材关键词",
        "是否需要来源页面录屏",
        "是否需要 Pexels",
        "是否需要信息动效",
        "是否需要 Codex 图文",
        "是否需要数字人",
        "素材状态",
        "evidence_kind",
        "source_url",
        "is_locally_generated",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for task in tasks:
            writer.writerow(
                {
                    "时间段": f"{task.start}-{task.end}",
                    "口播内容": task.voiceover,
                    "推荐画面": task.recommended_visual,
                    "素材类型": task.material_type,
                    "素材关键词": "、".join(task.asset_keywords or task.jianying_keywords),
                    "是否需要来源页面录屏": "是" if task.needs_official_capture else "否",
                    "是否需要 Pexels": "是" if task.needs_pexels else "否",
                    "是否需要信息动效": "是" if task.needs_info_motion else "否",
                    "是否需要 Codex 图文": "是" if task.needs_codex_graphic else "否",
                    "是否需要数字人": "是" if task.avatar_required else "否",
                    "素材状态": task.status,
                    "evidence_kind": task.evidence_kind,
                    "source_url": task.source_url,
                    "is_locally_generated": str(task.is_locally_generated).lower(),
                }
            )
    return path


def write_edit_manifest_json(
    project_dir: Path,
    *,
    pipeline_id: int,
    provider: str,
    model: str,
    topic_idea: TopicIdea | None,
    script_asset: ScriptAsset | None,
    material_tasks: list[MaterialTask],
    graphic_tasks: list[GraphicTask],
) -> Path:
    info_dir = project_dir / "素材信息"
    info_dir.mkdir(parents=True, exist_ok=True)
    path = info_dir / "edit_manifest.json"
    payload = {
        "pipeline": {
            "id": pipeline_id,
            "provider": provider,
            "model": model,
            "real_ai_called": False,
            "jianying_download_executed": False,
            "auto_material_flow": "official_page_pexels_info_motion",
            "official_capture_executed": False,
            "pexels_download_executed": False,
            "info_motion_render_executed": False,
            "codex_skill_executed": False,
            "avatar_generation_executed": False,
        },
        "topic": {
            "id": topic_idea.id if topic_idea else None,
            "title": topic_idea.title if topic_idea else "",
            "core_view": topic_idea.core_view if topic_idea else "",
        },
        "script": {
            "id": script_asset.id if script_asset else None,
            "voiceover": script_asset.voiceover if script_asset else "",
            "storyboard": script_asset.storyboard if script_asset else [],
            "subtitles": script_asset.subtitles if script_asset else [],
            "animation_prompts": script_asset.animation_prompts if script_asset else [],
            "title_options": script_asset.title_options if script_asset else [],
            "cover_copy": script_asset.cover_copy if script_asset else [],
            "publish_copy": script_asset.publish_copy if script_asset else {},
        },
        "shots": [material_task_to_dict(task) for task in material_tasks],
        "graphic_tasks": [graphic_task_to_dict(task) for task in graphic_tasks],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
