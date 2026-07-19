from __future__ import annotations

import csv
import json
from pathlib import Path

from app.materials.planner import MaterialTask


def write_edit_manifest(project_dir: Path, tasks: list[MaterialTask]) -> Path:
    path = project_dir / "素材信息" / "edit_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"shots": [_task_to_manifest(task) for task in tasks]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_shotlist_csv(project_dir: Path, tasks: list[MaterialTask]) -> Path:
    path = project_dir / "素材信息" / "shotlist.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "时间段",
        "口播内容",
        "推荐画面",
        "素材来源类型",
        "素材路径",
        "素材状态",
        "素材关键词",
        "是否来自本地素材库",
        "是否来自来源页面录屏",
        "是否来自 Pexels",
        "是否来自信息动效",
        "是否来自 Codex 生成图文",
        "是否需要数字人",
        "数字人位置",
        "是否需要替换",
        "拒绝原因",
        "备注",
        "evidence_kind",
        "source_url",
        "is_locally_generated",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for task in tasks:
            writer.writerow(_task_to_csv(task))
    return path


def write_script_md(project_dir: Path, tasks: list[MaterialTask]) -> Path:
    path = project_dir / "素材信息" / "script.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 脚本与素材清单", ""]
    for task in tasks:
        lines.extend(
            [
                f"## {task.start}-{task.end}",
                f"- 口播内容：{task.voiceover}",
                f"- 推荐画面：{task.recommended_visual}",
                f"- 素材状态：{task.status}",
                f"- 素材路径：{task.material_path}",
                f"- 证据类型：{task.evidence_kind or '无素材'}",
                f"- 来源链接：{task.source_url}",
                f"- 是否本地生成：{'是' if task.is_locally_generated else '否'}",
                f"- 是否需要数字人：{'是' if task.avatar_required else '否'}",
                f"- 数字人位置：{task.avatar_position}",
                f"- 数字人状态：{task.avatar_state}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _task_to_manifest(task: MaterialTask) -> dict:
    return {
        "shot_id": task.shot_id,
        "start": task.start,
        "end": task.end,
        "voiceover": task.voiceover,
        "recommended_visual": task.recommended_visual,
        "material_source_type": task.source_type or task.material_type,
        "material_path": task.material_path,
        "material_status": task.status,
        "asset_keywords": task.asset_keywords or task.jianying_keywords,
        "source_priority": task.source_priority,
        "needs_official_capture": task.needs_official_capture,
        "needs_pexels": task.needs_pexels,
        "needs_info_motion": task.needs_info_motion,
        "needs_backup_asset": task.needs_backup_asset,
        "from_local_library": task.source_type == "本地素材库",
        "from_source_page_capture": task.evidence_kind == "direct_evidence",
        "from_pexels": task.source_type in {"Pexels视频", "Pexels照片"},
        "from_info_motion": task.source_type == "信息动效",
        "from_codex_graphic": task.needs_codex_graphic,
        "avatar_required": task.avatar_required,
        "avatar_position": task.avatar_position,
        "avatar_state": task.avatar_state,
        "avatar_avoid_subtitle": task.avatar_avoid_subtitle,
        "avatar_avoid_chart": task.avatar_avoid_chart,
        "avatar_lip_sync": task.avatar_lip_sync,
        "avatar_material_path": task.avatar_material_path,
        "needs_replacement": task.status in {"需要人工确认", "下载失败", "筛选不通过"},
        "reject_reason": task.reject_reason,
        "notes": task.notes,
        "evidence_kind": task.evidence_kind,
        "source_url": task.source_url,
        "is_locally_generated": task.is_locally_generated,
    }


def _task_to_csv(task: MaterialTask) -> dict:
    return {
        "时间段": f"{task.start}-{task.end}",
        "口播内容": task.voiceover,
        "推荐画面": task.recommended_visual,
        "素材来源类型": task.source_type or task.material_type,
        "素材路径": task.material_path,
        "素材状态": task.status,
        "素材关键词": "、".join(task.asset_keywords or task.jianying_keywords),
        "是否来自本地素材库": "是" if task.source_type == "本地素材库" else "否",
        "是否来自来源页面录屏": "是" if task.evidence_kind == "direct_evidence" else "否",
        "是否来自 Pexels": "是" if task.source_type in {"Pexels视频", "Pexels照片"} else "否",
        "是否来自信息动效": "是" if task.source_type == "信息动效" else "否",
        "是否来自 Codex 生成图文": "是" if task.needs_codex_graphic else "否",
        "是否需要数字人": "是" if task.avatar_required else "否",
        "数字人位置": task.avatar_position,
        "是否需要替换": "是" if task.status in {"需要人工确认", "下载失败", "筛选不通过"} else "否",
        "拒绝原因": task.reject_reason,
        "备注": task.notes,
        "evidence_kind": task.evidence_kind,
        "source_url": task.source_url,
        "is_locally_generated": str(task.is_locally_generated).lower(),
    }
