from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any


_SAFE_TASK_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,139}$")


@dataclass(slots=True)
class ShotSegment:
    start: str
    end: str
    voiceover: str
    recommended_visual: str = ""
    source_priority: list[str] = field(default_factory=list)
    asset_keywords: list[str] = field(default_factory=list)
    material_requirements: dict[str, Any] = field(default_factory=dict)
    backup_visual: str = ""
    failure_fallback: str = ""
    official_source_url: str = ""
    matching_reason: str = ""
    # Semantic Shot Director fields.  They are optional so legacy storyboard
    # callers keep the exact old contract while approved ScriptVersions can
    # carry the production intent into material planning.
    shot_id: str = ""
    beat_id: str = ""
    beat_type: str = ""
    viewer_takeaway: str = ""
    visual_intent: str = ""
    evidence_kind: str = ""
    source_refs: list[dict[str, str]] = field(default_factory=list)
    acceptance_conditions: list[str] = field(default_factory=list)
    forbidden_items: list[str] = field(default_factory=list)
    director_version: str = ""
    subject: str = ""
    action: str = ""
    location: str = ""
    time_context: str = ""
    composition: str = ""
    visual_type: str = ""
    sound_notes: str = ""


@dataclass(slots=True)
class MaterialTask:
    shot_id: str
    start: str
    end: str
    voiceover: str
    recommended_visual: str
    material_type: str
    source_priority: list[str]
    jianying_keywords: list[str]
    needs_local_library: bool
    needs_jianying: bool
    needs_codex_graphic: bool
    needs_codex_chart: bool
    needs_codex_info_card: bool
    can_use_ai_motion: bool
    status: str = "待搜索"
    material_path: str = ""
    source_type: str = ""
    match_score: float = 0.0
    reject_reason: str = ""
    notes: str = ""
    avatar_required: bool = False
    avatar_position: str = "右下角"
    avatar_state: str = "轻微动作"
    avatar_avoid_subtitle: bool = True
    avatar_avoid_chart: bool = True
    avatar_lip_sync: bool = False
    avatar_material_path: str = ""
    avatar_generated_by_codex_skill: bool = False
    status_history: list[str] = field(default_factory=list)
    asset_keywords: list[str] = field(default_factory=list)
    needs_official_capture: bool = False
    needs_pexels: bool = False
    needs_info_motion: bool = False
    needs_backup_asset: bool = False
    official_source_url: str = ""
    official_source_name: str = ""
    official_page_title: str = ""
    asset_mix_target: dict[str, int] = field(default_factory=dict)
    evidence_kind: str = ""
    source_url: str = ""
    is_locally_generated: bool = False
    material_task_id: str = ""
    # Read-only trace of the semantic beat that caused this task.  Keeping it
    # on the task makes the material page and worker payload explainable.
    beat_id: str = ""
    beat_type: str = ""
    viewer_takeaway: str = ""
    visual_intent: str = ""
    source_refs: list[dict[str, str]] = field(default_factory=list)
    acceptance_conditions: list[str] = field(default_factory=list)
    forbidden_items: list[str] = field(default_factory=list)
    director_version: str = ""
    subject: str = ""
    action: str = ""
    location: str = ""
    time_context: str = ""
    composition: str = ""
    visual_type: str = ""
    sound_notes: str = ""


@dataclass(slots=True)
class GraphicTask:
    shot_id: str
    time_range: str
    voiceover: str
    visual_form: str
    core_message: str
    keywords: list[str]
    needs_chart: bool
    needs_checklist_card: bool
    needs_flowchart: bool
    needs_compare: bool
    needs_title_page: bool
    needs_conclusion_page: bool
    style_suggestion: str
    motion_suggestion: str
    generated_by_codex_skill: bool
    save_path: str
    shotlist_position: str


@dataclass(slots=True)
class MaterialPlan:
    project_name: str
    material_tasks: list[MaterialTask] = field(default_factory=list)
    graphic_tasks: list[GraphicTask] = field(default_factory=list)


def build_material_plan(shots: list[ShotSegment], project_name: str) -> MaterialPlan:
    plan = MaterialPlan(project_name=project_name)
    used_shot_ids: set[str] = set()
    for index, shot in enumerate(shots, start=1):
        base_shot_id = shot.shot_id.strip() or f"shot-{index:02d}"
        shot_id = base_shot_id
        if shot_id in used_shot_ids:
            semantic_suffix = shot.beat_id.strip() or f"beat-{index:02d}"
            shot_id = f"{shot_id}-{semantic_suffix}"
            suffix_index = 2
            while shot_id in used_shot_ids:
                shot_id = f"{base_shot_id}-{semantic_suffix}-{suffix_index}"
                suffix_index += 1
        used_shot_ids.add(shot_id)
        recommended_visual = shot.recommended_visual or shot.visual_intent
        text = " ".join(
            value
            for value in (
                shot.voiceover,
                recommended_visual,
                shot.viewer_takeaway,
                shot.subject,
                shot.action,
                shot.location,
                shot.composition,
            )
            if value
        )
        needs_graphic = _needs_graphic(text)
        asset_keywords = shot.asset_keywords or _keywords(text)
        needs_official = _needs_official_capture(text)
        needs_pexels = _needs_pexels(text, needs_graphic)
        needs_info_motion = _needs_info_motion(text, needs_graphic)
        source_priority = shot.source_priority or [
            "备选素材",
            "官方录屏",
            "Pexels视频",
            "信息动效",
        ]
        task = MaterialTask(
            shot_id=shot_id,
            start=shot.start,
            end=shot.end,
            voiceover=shot.voiceover,
            recommended_visual=recommended_visual,
            material_type="待匹配",
            source_priority=source_priority,
            jianying_keywords=asset_keywords,
            needs_local_library=True,
            needs_jianying=False,
            needs_codex_graphic=needs_graphic,
            needs_codex_chart=_has_any(text, ["图表", "数据", "增长"]),
            needs_codex_info_card=_has_any(text, ["清单", "判断", "框架", "三点"]),
            can_use_ai_motion=needs_info_motion,
            avatar_required=True,
            avatar_state=_avatar_state(text),
            asset_keywords=asset_keywords,
            needs_official_capture=needs_official or bool(shot.official_source_url),
            needs_pexels=needs_pexels,
            needs_info_motion=needs_info_motion,
            needs_backup_asset=bool(shot.backup_visual or shot.failure_fallback),
            official_source_url=shot.official_source_url,
            notes=_join_notes(shot.matching_reason, shot.failure_fallback),
            asset_mix_target={
                "官方录屏": 30,
                "Pexels": 30,
                "信息动效": 40,
            },
            material_task_id=material_task_id_for_shot(shot_id),
            beat_id=shot.beat_id,
            beat_type=shot.beat_type,
            viewer_takeaway=shot.viewer_takeaway,
            visual_intent=shot.visual_intent,
            evidence_kind=shot.evidence_kind,
            source_refs=list(shot.source_refs),
            acceptance_conditions=list(shot.acceptance_conditions),
            forbidden_items=list(shot.forbidden_items),
            director_version=shot.director_version,
            subject=shot.subject,
            action=shot.action,
            location=shot.location,
            time_context=shot.time_context,
            composition=shot.composition,
            visual_type=shot.visual_type,
            sound_notes=shot.sound_notes,
        )
        plan.material_tasks.append(task)
        if needs_graphic:
            plan.graphic_tasks.append(_graphic_task(shot_id, shot, text))
    return plan


def material_task_id_for_shot(shot_id: str) -> str:
    """Return one deterministic, claim-safe ID scoped by pipeline/step."""

    if type(shot_id) is not str or not shot_id.strip():
        raise ValueError("shot_id is required to derive material_task_id")
    normalized = shot_id.strip()
    if _SAFE_TASK_TOKEN_RE.fullmatch(normalized) is None:
        normalized = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return validate_material_task_id(f"material-{normalized}")


def validate_material_task_id(value: str) -> str:
    if type(value) is not str or _SAFE_TASK_TOKEN_RE.fullmatch(value) is None:
        raise ValueError("material_task_id is not a safe immutable identifier")
    return value


def _needs_graphic(text: str) -> bool:
    return _has_any(text, ["逻辑", "框架", "清单", "判断", "流程", "图表", "关键词", "标题", "结论", "省钱", "省人"])


def _needs_official_capture(text: str) -> bool:
    return _has_any(text, ["新闻", "官方", "公告", "原文", "页面", "报告", "论文", "产品"])


def _needs_pexels(text: str, needs_graphic: bool) -> bool:
    return _has_any(text, ["背景", "办公", "城市", "会议", "消费", "财经", "科技", "商业"]) or not needs_graphic


def _needs_info_motion(text: str, needs_graphic: bool) -> bool:
    return needs_graphic or _has_any(text, ["数据流", "动效", "信息", "趋势", "逻辑", "判断"])


def _keywords(text: str) -> list[str]:
    keyword_pool = [
        "AI工具",
        "科技办公",
        "商业会议",
        "财经新闻",
        "城市办公",
        "数据分析",
        "消费趋势",
        "企业采购",
        "商业逻辑",
        "效率提升",
    ]
    matched = [keyword for keyword in keyword_pool if any(part in text for part in keyword.split())]
    if "AI" in text or "工具" in text:
        matched.extend(["AI工具", "科技办公", "企业采购"])
    if "财经" in text or "商业" in text:
        matched.extend(["财经新闻", "商业会议", "数据分析"])
    if "消费" in text:
        matched.extend(["消费趋势", "城市办公"])
    unique = []
    for keyword in matched or ["科技办公", "商业会议", "数据分析"]:
        if keyword not in unique:
            unique.append(keyword)
    return unique[:5]


def _graphic_task(shot_id: str, shot: ShotSegment, text: str) -> GraphicTask:
    return GraphicTask(
        shot_id=shot_id,
        time_range=f"{shot.start}-{shot.end}",
        voiceover=shot.voiceover,
        visual_form=_visual_form(text),
        core_message=shot.voiceover[:80],
        keywords=_keywords(text),
        needs_chart=_has_any(text, ["图表", "数据", "增长"]),
        needs_checklist_card=_has_any(text, ["清单", "三点"]),
        needs_flowchart=_has_any(text, ["流程", "逻辑"]),
        needs_compare=_has_any(text, ["对比", "谁受益", "谁被影响"]),
        needs_title_page=_has_any(text, ["为什么", "标题", "开头"]),
        needs_conclusion_page=_has_any(text, ["结论", "总结"]),
        style_suggestion="干净、冷静、财经科技感，高信息密度，不营销号",
        motion_suggestion="轻微入场、关键词强调、节奏稳定",
        generated_by_codex_skill=False,
        save_path="信息动效",
        shotlist_position=shot_id,
    )


def _visual_form(text: str) -> str:
    if _has_any(text, ["图表", "数据"]):
        return "数据图表"
    if _has_any(text, ["流程", "逻辑"]):
        return "商业逻辑流程图"
    if _has_any(text, ["清单", "三点", "判断"]):
        return "判断框架卡片"
    if _has_any(text, ["为什么", "标题"]):
        return "动态标题页"
    return "信息卡片"


def _avatar_state(text: str) -> str:
    if _has_any(text, ["为什么", "开头", "抛问题"]):
        return "正在讲解"
    if _has_any(text, ["重点", "真正", "核心"]):
        return "强调重点"
    if _has_any(text, ["总结", "结论"]):
        return "总结收束"
    return "轻微动作"


def _join_notes(*parts: str) -> str:
    return "；".join(part.strip() for part in parts if part and part.strip())


def _has_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)
