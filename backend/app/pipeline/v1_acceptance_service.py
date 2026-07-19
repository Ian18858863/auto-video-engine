from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import resolve_configured_path
from app.content_quality.service import check_pipeline_script
from app.hotspots.pipeline import HotspotPipeline
from app.hotspots.schemas import HotspotCandidate
from app.models import ContentQualityCheck, ProductionPipeline, ScriptAsset, TopicIdea
from app.pipeline.material_package_service import rebuild_export_file
from app.pipeline.schemas import ManualV1RunRequest
from app.pipeline.script_asset_ownership import ensure_pipeline_script_asset_owned
from app.pipeline.service import (
    build_project_from_hotspot,
    get_pipeline_status,
    serialize_pipeline_result,
)


@dataclass(slots=True)
class QualityRepairResult:
    quality_record: ContentQualityCheck
    repair_attempts: int

    @property
    def quality_check(self) -> dict[str, Any]:
        return self.quality_record.result


@dataclass(slots=True)
class ManualV1RunResult:
    message: str
    pipeline_id: int
    hotspot_id: int
    topic_idea_id: int | None
    script_asset_id: int | None
    project_name: str
    project_key: str
    project_dir: str
    legacy_project_dir: str | None
    provider: str
    model: str
    quality_check: dict[str, Any]
    quality_check_id: int
    repair_attempts: int
    exports: dict[str, str]
    pipeline: dict[str, Any]
    completed: bool
    real_ai_called: bool = False

    @property
    def links(self) -> dict[str, int | None]:
        return {
            "project_pipeline_id": self.pipeline_id,
            "workspace_pipeline_id": self.pipeline_id,
            "material_package_pipeline_id": self.pipeline_id,
            "content_quality_pipeline_id": self.pipeline_id,
            "hotspot_id": self.hotspot_id,
            "topic_idea_id": self.topic_idea_id,
        }


def run_manual_v1_acceptance(
    session: Session,
    request: ManualV1RunRequest,
    *,
    material_root: Path,
    project_root: Path | None = None,
) -> ManualV1RunResult:
    approved_project_root = resolve_configured_path(
        project_root or (resolve_configured_path(material_root) / "项目")
    )
    material_root = resolve_configured_path(material_root)
    hot_item, _created = HotspotPipeline(session).import_manual(
        HotspotCandidate(
            source_platform=request.source_platform or "手动跑通测试",
            source_type="manual",
            title=request.title,
            url=_manual_url(request),
            published_at=datetime.now(timezone.utc),
            summary=request.summary,
            metrics={"notes": request.notes, "manual_v1_acceptance": True},
            tags=_clean_tags(request.tags),
            category=request.category or "AI / 科技趋势 / 商业逻辑",
            data_origin="manual_v1_test",
            origin_note="V1 手动跑通测试数据，不进入正式选题推荐。",
        )
    )
    pipeline_result = build_project_from_hotspot(
        session,
        hot_item.id,
        project_name=request.title,
        material_root=material_root,
        project_root=approved_project_root,
    )
    repair = check_and_repair_pipeline_script(session, pipeline_result.pipeline_id)
    _rebuild_exports_if_ready(
        session,
        pipeline_result.pipeline_id,
        project_root=approved_project_root,
    )
    status = get_pipeline_status(session, pipeline_result.pipeline_id)
    payload = serialize_pipeline_result(status)
    completed = status.completion_reason_code == "approved_artifact_verified"
    return ManualV1RunResult(
        message=(
            "已完成 V1 测试项目生成。"
            if completed
            else (
                "V1 准备包已生成；请到成片页检查文件和验收状态。"
                "五门报告与批准入口仍属于 Phase 1。"
            )
        ),
        pipeline_id=status.pipeline_id,
        hotspot_id=status.hotspot_id,
        topic_idea_id=status.topic_idea_id,
        script_asset_id=status.script_asset_id,
        project_name=status.project_name,
        project_key=status.project_key,
        project_dir=status.project_dir,
        legacy_project_dir=status.legacy_project_dir,
        provider=status.provider,
        model=status.model,
        quality_check=repair.quality_check,
        quality_check_id=repair.quality_record.id,
        repair_attempts=repair.repair_attempts,
        exports=status.exports,
        pipeline=payload,
        completed=completed,
        real_ai_called=False,
    )


def check_and_repair_pipeline_script(
    session: Session,
    pipeline_id: int,
    *,
    max_attempts: int = 2,
) -> QualityRepairResult:
    record = check_pipeline_script(session, pipeline_id)
    attempts = 0
    while record.result.get("publish_suggestion") != "可以发布" and attempts < max_attempts:
        _repair_pipeline_script(session, pipeline_id, record.result)
        attempts += 1
        record = check_pipeline_script(session, pipeline_id)

    if record.result.get("publish_suggestion") != "可以发布":
        pipeline = session.get(ProductionPipeline, pipeline_id)
        if pipeline is not None:
            pipeline.status = "需要人工确认"
            session.add(pipeline)
            session.commit()

    return QualityRepairResult(quality_record=record, repair_attempts=attempts)


def serialize_manual_v1_result(result: ManualV1RunResult) -> dict[str, Any]:
    return {
        "message": result.message,
        "pipeline_id": result.pipeline_id,
        "hotspot_id": result.hotspot_id,
        "topic_idea_id": result.topic_idea_id,
        "script_asset_id": result.script_asset_id,
        "project_name": result.project_name,
        "project_key": result.project_key,
        "project_dir": result.project_dir,
        "legacy_project_dir": result.legacy_project_dir,
        "provider": result.provider,
        "model": result.model,
        "quality_check": {
            "id": result.quality_check_id,
            **result.quality_check,
        },
        "repair_attempts": result.repair_attempts,
        "exports": result.exports,
        "pipeline": result.pipeline,
        "links": result.links,
        "completed": result.completed,
        "real_ai_called": result.real_ai_called,
        "provider_notice": "未配置真实 AI，当前使用 fake provider 测试。",
    }


def _repair_pipeline_script(
    session: Session,
    pipeline_id: int,
    quality_result: dict[str, Any],
) -> None:
    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None or pipeline.script_asset_id is None:
        return
    script = session.get(ScriptAsset, pipeline.script_asset_id)
    if script is None:
        return
    script = ensure_pipeline_script_asset_owned(session, pipeline, script)

    matched = list(quality_result.get("matched_banned_expressions") or [])
    terms = list(quality_result.get("professional_terms_need_explanation") or [])
    script.voiceover = _repair_text(script.voiceover, matched, terms)
    script.storyboard = [
        {
            **item,
            "voiceover": _repair_text(str(item.get("voiceover", "")), matched, terms),
        }
        for item in script.storyboard
        if isinstance(item, dict)
    ]
    script.subtitles = [
        {
            **item,
            "text": _repair_text(str(item.get("text", "")), matched, terms),
        }
        for item in script.subtitles
        if isinstance(item, dict)
    ]
    script.title_options = [
        _repair_mapping(item, matched, terms, keys=("title", "text"))
        for item in script.title_options
        if isinstance(item, dict)
    ]
    script.cover_copy = [
        _repair_mapping(item, matched, terms, keys=("title", "text"))
        for item in script.cover_copy
        if isinstance(item, dict)
    ]
    script.publish_copy = {
        key: _repair_text(str(value), matched, terms)
        for key, value in (script.publish_copy or {}).items()
    }
    session.add(script)
    session.commit()


def _repair_mapping(
    item: dict[str, Any],
    matched: list[str],
    terms: list[str],
    *,
    keys: tuple[str, ...],
) -> dict[str, Any]:
    repaired = dict(item)
    for key in keys:
        if key in repaired:
            repaired[key] = _repair_text(str(repaired[key]), matched, terms)
    return repaired


def _repair_text(text: str, matched: list[str], terms: list[str]) -> str:
    repaired = str(text or "")
    replacements = {
        "震惊": "值得注意",
        "炸裂": "讨论度很高",
        "全网疯传": "讨论度变高",
        "赶紧收藏": "可以先记一下",
        "普通人必须抓住": "普通人可以先看懂",
        "错过就晚了": "不用急着跟风",
        "财富密码": "商业逻辑",
        "稳赚": "不代表确定收益",
        "必涨": "不做涨跌判断",
        "马上上车": "先观察",
        "现在可以买": "这里不做买卖建议",
        "买入": "关注变化",
        "卖出": "关注变化",
        "荐股": "商业逻辑拆解",
        "荐币": "商业逻辑拆解",
        "推荐基金": "商业逻辑拆解",
        "大家都知道": "如果你没时间看完整背景",
        "显而易见": "更值得关注的是",
        "很明显": "更值得关注的是",
        "这不用多说": "这里先讲清楚",
        "众所周知": "先把背景讲清楚",
    }
    for phrase in matched:
        repaired = repaired.replace(phrase, replacements.get(phrase, ""))
    for source, target in replacements.items():
        repaired = repaired.replace(source, target)

    repaired = " ".join(repaired.split())
    if terms and "说白了" not in repaired:
        repaired = (
            f"{repaired} 说白了，这里只拆它靠什么赚钱、谁受影响、普通人怎么判断。"
        ).strip()
    if not any(marker in repaired for marker in ["判断标准", "清单", "框架", "避坑"]):
        repaired = f"{repaired} 普通人可以把它当成一个判断标准，而不是行动建议。".strip()
    if "不构成投资建议" not in repaired and any(
        phrase in matched for phrase in ["现在可以买", "马上上车", "买入", "卖出", "稳赚", "必涨"]
    ):
        repaired = f"{repaired} 这不是投资建议。".strip()
    return repaired


def _rebuild_exports_if_ready(
    session: Session,
    pipeline_id: int,
    *,
    project_root: Path,
) -> None:
    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None or not pipeline.project_dir:
        return
    directory_step = next(
        (step for step in pipeline.steps if step.name == "创建中文项目目录"),
        None,
    )
    if directory_step is None or directory_step.status != "已完成":
        return
    for export_type in (
        "timeline_json",
        "subtitles_srt",
        "rough_cut_plan_json",
        "edit_notes_md",
        "script_md",
        "shotlist_csv",
        "edit_manifest_json",
    ):
        rebuild_export_file(
            session,
            pipeline_id,
            export_type,
            project_root=project_root,
        )


def _manual_url(request: ManualV1RunRequest) -> str:
    if request.url.strip():
        return request.url.strip()
    digest = sha1(
        f"{request.title}|{request.summary}|{request.source_platform}".encode("utf-8")
    ).hexdigest()[:12]
    return f"https://manual.local/trend-video-factory/{digest}"


def _clean_tags(tags: list[str]) -> list[str]:
    cleaned = [str(tag).strip() for tag in tags if str(tag).strip()]
    return list(dict.fromkeys(cleaned))[:5]
