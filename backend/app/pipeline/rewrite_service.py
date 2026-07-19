from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.account_style.service import get_account_style_context
from app.models import ProductionPipeline, ScriptAsset, TopicIdea
from app.pipeline.material_selection_invalidation import material_input_sha256_for_script
from app.pipeline.script_asset_ownership import ensure_pipeline_script_asset_owned
from app.pipeline.script_versioning import ScriptVersionError, ScriptVersionLedger
from app.pipeline.workspace_service import (
    finalize_script_draft_operation,
    reset_downstream_after_script_edit,
    script_use_avatar,
)
from app.script_engine.service import get_script_engine_context


class ScriptRewriteNotFoundError(ValueError):
    pass


REWRITE_PART_LABELS = {
    "voiceover": "口播稿",
    "storyboard": "分镜脚本",
    "hook": "开头",
    "ending": "结尾",
    "title": "标题",
    "cover": "封面文案",
    "subtitles": "字幕",
    "publish_copy": "发布文案",
}


@dataclass(slots=True)
class ScriptRewriteResult:
    pipeline: ProductionPipeline
    script_asset: ScriptAsset
    part: str
    used_account_style: bool
    used_script_engine: bool
    real_ai_called: bool = False


def rewrite_script_part(
    session: Session,
    pipeline_id: int,
    part: str,
) -> ScriptRewriteResult:
    if part not in REWRITE_PART_LABELS:
        raise ValueError("不支持的重写类型。")
    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None or pipeline.script_asset_id is None:
        raise ScriptRewriteNotFoundError(f"流程没有可重写的脚本：{pipeline_id}")
    script_asset = session.get(ScriptAsset, pipeline.script_asset_id)
    if script_asset is None:
        raise ScriptRewriteNotFoundError(f"脚本素材不存在：{pipeline.script_asset_id}")
    script_asset = ensure_pipeline_script_asset_owned(session, pipeline, script_asset)
    topic = session.get(TopicIdea, pipeline.topic_idea_id) if pipeline.topic_idea_id else None
    account_style = get_account_style_context(session)
    samples = get_script_engine_context(session)
    sample_count = len(samples.get("samples", []))
    before_material_input_sha256 = material_input_sha256_for_script(script_asset)

    if part == "voiceover":
        script_asset.voiceover = _rewrite_voiceover(
            topic,
            account_style,
            samples,
            segment_count=len(script_asset.storyboard or []),
        )
        script_asset.subtitles = _subtitles_from_voiceover(script_asset.voiceover)
        script_asset.storyboard = _storyboard_from_subtitles(
            script_asset.subtitles,
            list(script_asset.storyboard or []),
        )
    elif part == "storyboard":
        script_asset.storyboard = _rewrite_storyboard(script_asset)
        script_asset.subtitles = _subtitles_from_storyboard(script_asset.storyboard)
        script_asset.voiceover = "\n".join(
            str(item.get("voiceover", "")).strip()
            for item in script_asset.storyboard
            if str(item.get("voiceover", "")).strip()
        )
    elif part == "hook":
        line = _rewrite_line(topic, account_style, samples, part)
        storyboard = list(script_asset.storyboard or [])
        if storyboard:
            storyboard[0]["voiceover"] = line
        script_asset.storyboard = storyboard
        script_asset.voiceover = _replace_first_sentence(script_asset.voiceover, line)
    elif part == "ending":
        line = _rewrite_line(topic, account_style, samples, part)
        storyboard = list(script_asset.storyboard or [])
        if storyboard:
            storyboard[-1]["voiceover"] = line
        script_asset.storyboard = storyboard
        script_asset.voiceover = f"{script_asset.voiceover.rstrip()}\n{line}".strip()
    elif part == "title":
        script_asset.title_options = [
            {
                "type": "风格库重写",
                "title": _rewrite_title(topic),
                "risk": "低",
                "platform": "通用",
            }
        ]
    elif part == "cover":
        line = _rewrite_line(topic, account_style, samples, part)
        script_asset.cover_copy = [
            {"type": "风格库重写", "text": line, "risk": "低"}
        ] + list(script_asset.cover_copy or [])[:4]
    elif part == "subtitles":
        line = _rewrite_line(topic, account_style, samples, part)
        script_asset.subtitles = _subtitles_from_voiceover(script_asset.voiceover or line)
    elif part == "publish_copy":
        script_asset.publish_copy = {
            "通用": _rewrite_publish_copy(topic),
        }

    after_material_input_sha256 = material_input_sha256_for_script(script_asset)
    try:
        version_ledger = ScriptVersionLedger(
            pipeline,
            project_root=Path(pipeline.project_dir).parent,
        )
        current_versions = version_ledger.list()
        current_sources = list(current_versions[-1].fact_sources) if current_versions else []
        previous_approved_ids = [
            item.version_id for item in current_versions if item.status == "approved"
        ]
        script_version = version_ledger.create(
            {
                "voiceover": script_asset.voiceover,
                "storyboard": script_asset.storyboard,
                "subtitles": script_asset.subtitles,
                "animation_prompts": script_asset.animation_prompts,
                "title_options": script_asset.title_options,
                "cover_copy": script_asset.cover_copy,
                "publish_copy": script_asset.publish_copy,
                "use_avatar": script_use_avatar(script_asset),
            },
            fact_sources=current_sources,
            source=f"local_rewrite:{part}",
        )
        operation_id = uuid4().hex
        draft_operation = version_ledger.begin_operation(
            operation_id=operation_id,
            kind="draft",
            version_id=script_version.version_id,
            context={
                "version_content_hash": script_version.content_hash,
                "version_input_hash": script_version.input_hash,
                "version_snapshot_hash": script_version.snapshot_hash,
                "version_shot_plan_hash": script_version.shot_plan_hash,
                "previous_approved_version_ids": previous_approved_ids,
                "draft_source": f"local_rewrite:{part}",
            },
        )
    except ScriptVersionError as exc:
        raise ScriptVersionError(f"局部重写版本保存失败，未覆盖当前脚本：{exc}") from exc

    material_contract_changed = (
        before_material_input_sha256 != after_material_input_sha256
    )
    reset_downstream_after_script_edit(
        session,
        pipeline,
        material_contract_changed=material_contract_changed,
        target_script_version_id=script_version.version_id,
        target_script_version_hash=script_version.content_hash,
        target_material_input_sha256=after_material_input_sha256,
    )
    script_step = next(
        (step for step in pipeline.steps if step.name == "生成脚本素材"),
        None,
    )
    if script_step is not None:
        script_output = dict(script_step.output_summary or {})
        factory_state = dict(script_output.get("script_factory") or {})
        factory_state.update(
            {
                "status": "draft_edited",
                "accepted": False,
                "script_version_id": script_version.version_id,
                "script_version_hash": script_version.content_hash,
                "script_version_input_hash": script_version.input_hash,
                "script_version_snapshot_hash": script_version.snapshot_hash,
                "script_version_shot_plan_hash": script_version.shot_plan_hash,
                "script_result_hash": "",
                "import_operation_id": "",
                "draft_operation_id": operation_id,
                "approval_operation_id": "",
                "approval_event_hash": "",
                "approval_evidence_hash": "",
                "approval_quality_hash": "",
                "next_suggestion": "局部重写已形成新草稿，需重新检查并批准。",
            }
        )
        script_output["script_factory"] = factory_state
        script_step.output_summary = script_output
        session.add(script_step)
    session.add(script_asset)
    pipeline.status = "进行中"
    session.add(pipeline)
    session.commit()
    finalize_script_draft_operation(
        version_ledger,
        draft_operation.operation_id,
        reason="当前脚本已产生新的局部重写草稿，旧脚本批准立即失效。",
    )
    session.refresh(script_asset)
    return ScriptRewriteResult(
        pipeline=pipeline,
        script_asset=script_asset,
        part=part,
        used_account_style=True,
        used_script_engine=sample_count > 0,
    )


def serialize_rewrite_result(result: ScriptRewriteResult) -> dict[str, Any]:
    return {
        "pipeline_id": result.pipeline.id,
        "script_asset_id": result.script_asset.id,
        "part": result.part,
        "part_label": REWRITE_PART_LABELS[result.part],
        "used_account_style": result.used_account_style,
        "used_copywriting_samples": result.used_script_engine,
        "used_script_engine": result.used_script_engine,
        "real_ai_called": result.real_ai_called,
        "message": "已用账号风格库和脚本引擎完成本地重写。",
    }


def _rewrite_line(
    topic: TopicIdea | None,
    account_style: dict[str, Any],
    samples: dict[str, Any],
    part: str,
) -> str:
    title = topic.title if topic else "这个趋势"
    structure = _first_structure(account_style)
    sample_hint = _sample_hint(samples)
    if part == "hook":
        return f"这件事别只看热闹，真正值得看的，是它背后的{structure}。"
    if part == "ending":
        return "普通人不用急着跟风，先记住这个判断标准：它到底解决谁的问题，又靠什么赚钱。"
    if part == "title":
        return f"{title}背后的商业逻辑"
    if part == "cover":
        return "别只看热点，看懂背后的逻辑"
    if part == "subtitles":
        return f"{title}｜用大白话讲清楚｜{sample_hint}"
    return f"{title}，我更建议你先看懂背后的逻辑，再判断要不要行动。"


def _rewrite_voiceover(
    topic: TopicIdea | None,
    account_style: dict[str, Any],
    samples: dict[str, Any],
    segment_count: int = 0,
) -> str:
    title = topic.title if topic else "这件事"
    structure = _first_structure(account_style)
    sample_hint = _sample_hint(samples)
    count = max(segment_count, 4)
    templates = [
        f"今天这件事，别只看标题，核心是{title}。",
        "换成人话说，它影响的是普通人接下来怎么判断新工具、新平台和新机会。",
        f"我建议你按这个框架看：{structure}。",
        f"最后记住一点：{sample_hint}，但不要照搬任何单一案例。",
    ]
    return "\n".join(
        templates[index] if index < len(templates) else f"第{index + 1}段继续围绕{title}，讲清楚它对普通人的实际影响。"
        for index in range(count)
    )


def _rewrite_title(topic: TopicIdea | None) -> str:
    title = topic.title if topic else "今天这件事"
    return f"{title}，普通人该怎么看"


def _rewrite_publish_copy(topic: TopicIdea | None) -> str:
    title = topic.title if topic else "这件事"
    return f"今天用大白话讲清楚{title}背后的变化，只拆逻辑，不做投资建议。"


def _storyboard_from_subtitles(
    subtitles: list[dict[str, str]],
    existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, subtitle in enumerate(subtitles):
        previous = existing[index] if index < len(existing) else {}
        rows.append(
            {
                "shot_id": previous.get("shot_id") or f"shot-{index + 1:02d}",
                "start": subtitle.get("start", ""),
                "end": subtitle.get("end", ""),
                "voiceover": subtitle.get("text", ""),
                "recommended_visual": previous.get("recommended_visual")
                or _visual_for_segment(index),
            }
        )
    return rows


def _rewrite_storyboard(script_asset: ScriptAsset) -> list[dict[str, Any]]:
    storyboard = list(script_asset.storyboard or [])
    if not storyboard:
        storyboard = _storyboard_from_subtitles(
            _subtitles_from_voiceover(script_asset.voiceover or ""),
            [],
        )
    rewritten: list[dict[str, Any]] = []
    for index, item in enumerate(storyboard):
        rewritten.append(
            {
                **item,
                "shot_id": item.get("shot_id") or f"shot-{index + 1:02d}",
                "recommended_visual": _visual_for_segment(index),
            }
        )
    return rewritten


def _subtitles_from_storyboard(storyboard: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "start": str(item.get("start", "")),
            "end": str(item.get("end", "")),
            "text": str(item.get("voiceover", "")),
        }
        for item in storyboard
        if str(item.get("voiceover", "")).strip()
    ]


def _visual_for_segment(index: int) -> str:
    visuals = [
        "大标题开场，突出核心事件",
        "三点拆解卡片，展示普通人该看什么",
        "趋势变化时间线，强调前后对比",
        "风险提醒卡片，收束成判断标准",
    ]
    return visuals[index % len(visuals)]


def _first_structure(account_style: dict[str, Any]) -> str:
    structures = account_style.get("structure_preferences", [])
    if structures and isinstance(structures[0], dict):
        return str(structures[0].get("framework") or "商业逻辑")
    return "商业逻辑"


def _sample_hint(samples: dict[str, Any]) -> str:
    sample_items = samples.get("samples", [])
    if sample_items and isinstance(sample_items[0], dict):
        return str(sample_items[0].get("reusable_points") or "参考样本节奏")[:24]
    return "不复制原文，只参考表达方式"


def _replace_first_sentence(text: str, replacement: str) -> str:
    if not text.strip():
        return replacement
    separators = ["。", "！", "？", "\n"]
    indices = [text.find(separator) for separator in separators if text.find(separator) >= 0]
    if not indices:
        return f"{replacement}\n{text}"
    index = min(indices) + 1
    return f"{replacement}{text[index:]}"


def _subtitles_from_voiceover(text: str) -> list[dict[str, str]]:
    chunks = [chunk.strip() for chunk in text.replace("\n", "。").split("。") if chunk.strip()]
    return [
        {
            "start": str(index * 5),
            "end": str(index * 5 + 5),
            "text": chunk,
        }
        for index, chunk in enumerate(chunks)
    ]
