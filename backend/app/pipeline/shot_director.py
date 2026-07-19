"""Deterministic semantic Shot Director for the script-to-material boundary.

The first release deliberately keeps this local and inspectable.  A model or
external Skill may later propose the same contract, but it must return this
shape and may not become the project's source of truth.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import re
from typing import Any


class ShotDirectorError(ValueError):
    """Raised when a script cannot be converted into a safe ShotPlan."""


_BEAT_TYPES = frozenset(
    {
        "hook",
        "context",
        "evidence",
        "data",
        "explanation",
        "example",
        "comparison",
        "transition",
        "conclusion",
    }
)
_ROLE_ALIASES = {
    "开头": "hook",
    "钩子": "hook",
    "事实": "evidence",
    "证据": "evidence",
    "数据": "data",
    "解释": "explanation",
    "案例": "example",
    "例子": "example",
    "对比": "comparison",
    "转折": "transition",
    "结论": "conclusion",
    "收束": "conclusion",
}
_ROLE_HINTS = (
    ("hook", ("开头", "钩子", "先别", "问题是", "你可能")),
    ("evidence", ("事实", "证据", "来源", "官方", "报告", "原始")),
    ("data", ("数据", "数字", "增长", "下降", "比例", "图表")),
    ("comparison", ("对比", "区别", "而不是", "相比", "vs", " versus ")),
    ("example", ("案例", "例子", "比如", "举个")),
    ("conclusion", ("结论", "所以", "最后", "总结", "记住", "行动")),
    ("transition", ("但是", "不过", "转折", "接下来", "再看")),
    ("explanation", ("原因", "怎么", "为什么", "解释", "意味着", "理解")),
)


def build_shot_plan(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build a reviewable semantic plan from a Script Factory payload.

    Adjacent storyboard rows with the same semantic role are merged.  A row
    containing explicit ``semantic_beats`` is expanded, but punctuation never
    creates a beat by itself.  This is intentionally deterministic so Ian can
    compare a rerun and see exactly what changed.
    """

    if not isinstance(payload, Mapping):
        raise ShotDirectorError("Shot Director 输入必须是对象")
    raw_storyboard = payload.get("storyboard")
    if not isinstance(raw_storyboard, Sequence) or isinstance(
        raw_storyboard, (str, bytes, bytearray)
    ):
        raise ShotDirectorError("Shot Director 缺少 storyboard 列表")

    expanded: list[tuple[dict[str, Any], bool, int]] = []
    for source_index, raw in enumerate(raw_storyboard, start=1):
        if not isinstance(raw, Mapping):
            raise ShotDirectorError(f"第 {source_index} 个分镜不是对象")
        item = {str(k): deepcopy(v) for k, v in raw.items()}
        semantic_beats = item.get("semantic_beats")
        if semantic_beats is None:
            expanded.append((item, False, source_index))
            continue
        if not isinstance(semantic_beats, Sequence) or isinstance(
            semantic_beats, (str, bytes, bytearray)
        ) or not semantic_beats:
            raise ShotDirectorError(f"第 {source_index} 个 semantic_beats 无效")
        for beat_index, raw_beat in enumerate(semantic_beats, start=1):
            if not isinstance(raw_beat, Mapping):
                raise ShotDirectorError(
                    f"第 {source_index} 个 semantic beat {beat_index} 不是对象"
                )
            merged = dict(item)
            merged.pop("semantic_beats", None)
            merged.update({str(k): deepcopy(v) for k, v in raw_beat.items()})
            merged["_semantic_parent_count"] = len(semantic_beats)
            expanded.append((merged, True, source_index))

    groups: list[list[tuple[dict[str, Any], bool, int]]] = []
    for item, explicit, source_index in expanded:
        role = _normalise_role(item)
        group_key = str(item.get("semantic_group") or item.get("beat_group") or role)
        has_semantic_annotation = bool(
            item.get("semantic_group")
            or item.get("beat_group")
            or item.get("semantic_role")
            or item.get("beat_type")
            or item.get("role")
        )
        if explicit:
            group_key = f"explicit:{source_index}:{len(groups)}"
        if groups and not explicit:
            previous_item, previous_explicit, _ = groups[-1][-1]
            previous_role = _normalise_role(previous_item)
            previous_key = str(
                previous_item.get("semantic_group")
                or previous_item.get("beat_group")
                or previous_role
            )
            previous_has_annotation = bool(
                previous_item.get("semantic_group")
                or previous_item.get("beat_group")
                or previous_item.get("semantic_role")
                or previous_item.get("beat_type")
                or previous_item.get("role")
            )
            if (
                not previous_explicit
                and previous_role == role
                and previous_key == group_key
                and (
                    role != "context"
                    or (has_semantic_annotation and previous_has_annotation)
                )
            ):
                groups[-1].append((item, explicit, source_index))
                continue
        groups.append([(item, explicit, source_index)])

    result: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        result.append(_build_plan_item(group, index))
    return result


def _build_plan_item(
    group: list[tuple[dict[str, Any], bool, int]],
    index: int,
) -> dict[str, Any]:
    first = group[0][0]
    role = _normalise_role(first)
    voiceover = "".join(
        _text(item.get("voiceover") or item.get("text")) for item, _, _ in group
    ).strip()
    if not voiceover:
        voiceover = _text(first.get("narration"))
    start = _time_seconds(first.get("start"), default=0.0)
    end = max(
        _time_seconds(item.get("end"), default=start)
        for item, _, _ in group
    )
    if end <= start:
        end = start + max(1.0, min(12.0, len(voiceover) / 5 or 1.0))
    duration = max(0.5, end - start)
    material = first.get("material_requirements")
    material_requirements = dict(material) if isinstance(material, Mapping) else {}
    source_refs = _source_refs(group, material_requirements)
    explicit_takeaway = _text(first.get("viewer_takeaway"))
    takeaway = explicit_takeaway or _takeaway(first, role)
    visual_intent = _joined_text(group, "visual_intent") or _joined_text(
        group, "recommended_visual"
    )
    forbidden = _list_text(
        first.get("forbidden_items")
        or first.get("禁用项")
        or first.get("avoid")
    )
    if not forbidden:
        forbidden = ["不得用无法支持该事实的相似画面"] if role == "evidence" else []
    acceptance = _list_text(first.get("acceptance_conditions"))
    if not acceptance:
        acceptance = _default_acceptance(role, source_refs, visual_intent)
    shot_id = _text(first.get("shot_id")) or f"shot-{index:02d}"
    source_shot_ids = [
        _text(item.get("shot_id")) or f"source-{source_index:02d}"
        for item, _, source_index in group
    ]
    return {
        "shot_id": shot_id,
        "beat_id": f"beat-{index:02d}",
        "beat_type": role,
        "source_shot_ids": source_shot_ids,
        "viewer_takeaway": takeaway,
        "evidence_kind": _evidence_kind(role, source_refs, material_requirements),
        "subject": _text(first.get("subject") or first.get("主体")),
        "action": _text(first.get("action") or first.get("动作")),
        "location": _text(first.get("location") or first.get("地点")),
        "time_context": _text(first.get("time") or first.get("时间")),
        "composition": _text(first.get("composition") or first.get("构图")),
        "visual_type": _visual_type(first, role),
        "visual_intent": visual_intent,
        "start": _format_seconds(start),
        "end": _format_seconds(end),
        "duration_seconds": round(duration, 3),
        "subtitle_load": len(voiceover),
        "voiceover": voiceover,
        "sound_notes": _text(first.get("sound_notes") or first.get("声音提示")),
        "material_requirements": material_requirements,
        "asset_keywords": _list_text(first.get("asset_keywords")),
        "source_refs": source_refs,
        "source_priority": _list_text(first.get("source_priority")),
        "backup_visual": _text(first.get("backup_visual")),
        "failure_fallback": _text(first.get("failure_fallback")),
        "forbidden_items": forbidden,
        "acceptance_conditions": acceptance,
        "director_version": "semantic-director-v1",
    }


def _normalise_role(item: Mapping[str, Any]) -> str:
    raw = _text(item.get("semantic_role") or item.get("beat_type") or item.get("role"))
    if raw in _BEAT_TYPES:
        return raw
    if raw in _ROLE_ALIASES:
        return _ROLE_ALIASES[raw]
    haystack = " ".join(
        _text(item.get(key))
        for key in ("voiceover", "text", "visual_intent", "recommended_visual")
    ).casefold()
    for role, hints in _ROLE_HINTS:
        if any(hint.casefold() in haystack for hint in hints):
            return role
    return "context"


def _evidence_kind(
    role: str,
    source_refs: list[dict[str, str]],
    material_requirements: Mapping[str, Any],
) -> str:
    if role == "evidence" or source_refs or material_requirements.get("official_source_url"):
        return "direct_evidence"
    if role in {"data", "explanation", "comparison", "example"}:
        return "explanation"
    return "atmosphere"


def _visual_type(item: Mapping[str, Any], role: str) -> str:
    explicit = _text(item.get("visual_type") or item.get("画面类型"))
    if explicit:
        return explicit
    if item.get("needs_info_motion") or role in {"data", "explanation", "comparison"}:
        return "信息动效"
    if item.get("official_source_url") or isinstance(item.get("material_requirements"), Mapping) and item[
        "material_requirements"
    ].get("official_source_url"):
        return "官方证据画面"
    if role == "hook":
        return "标题/钩子画面"
    return "解释或气氛画面"


def _takeaway(item: Mapping[str, Any], role: str) -> str:
    visual = _text(item.get("visual_intent") or item.get("recommended_visual"))
    if visual:
        return visual
    voiceover = _text(item.get("voiceover") or item.get("text"))
    if voiceover:
        return voiceover[:80]
    return f"让观众完成 {role} 理解"


def _default_acceptance(role: str, source_refs: list[dict[str, str]], visual: str) -> list[str]:
    if role == "evidence" or source_refs:
        return ["画面必须能回到记录的来源页或原始证据", "不得用气氛素材冒充事实证明"]
    if visual:
        return ["画面要直接解释这一镜的观众任务", "字幕与主体保持安全区和可读性"]
    return ["画面与口播语义一致", "不得用无关重复素材填充时长"]


def _source_refs(
    group: list[tuple[dict[str, Any], bool, int]],
    material_requirements: Mapping[str, Any],
) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    urls: list[str] = []
    for item, _, _ in group:
        for key in ("source_url", "official_source_url", "source_page_url"):
            value = _text(item.get(key))
            if value and value not in urls:
                urls.append(value)
        nested = item.get("material_requirements")
        if isinstance(nested, Mapping):
            value = _text(nested.get("official_source_url"))
            if value and value not in urls:
                urls.append(value)
    value = _text(material_requirements.get("official_source_url"))
    if value and value not in urls:
        urls.append(value)
    return [{"url": url, "kind": "source"} for url in urls]


def _joined_text(group: list[tuple[dict[str, Any], bool, int]], key: str) -> str:
    values = [_text(item.get(key)) for item, _, _ in group]
    return "；".join(value for value in values if value)


def _list_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return []
    return [text for item in value if (text := _text(item))]


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _time_seconds(value: Any, *, default: float) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    raw = _text(value)
    if not raw:
        return default
    parts = raw.split(":")
    try:
        if len(parts) == 2:
            minutes, seconds = parts
            return max(0.0, int(minutes) * 60 + float(seconds))
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return max(0.0, int(hours) * 3600 + int(minutes) * 60 + float(seconds))
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return default


def _format_seconds(value: float) -> str:
    minutes = int(value // 60)
    seconds = value - minutes * 60
    return f"{minutes:02d}:{seconds:05.2f}"


__all__ = ["ShotDirectorError", "build_shot_plan"]
