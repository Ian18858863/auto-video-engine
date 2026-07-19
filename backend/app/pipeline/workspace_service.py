from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings, resolve_configured_path
from app.materials.planner import material_task_id_for_shot
from app.models import HotItem, PipelineStep, ProductionPipeline, ScriptAsset, TopicIdea
from app.pipeline.completion_policy import evaluate_pipeline_completion
from app.pipeline.material_candidate_preview_service import public_candidate_pool
from app.pipeline.material_selection_invalidation import (
    invalidate_material_selection_payload,
    material_input_sha256,
    material_input_sha256_for_script,
)
from app.pipeline.script_asset_ownership import ensure_pipeline_script_asset_owned
from app.pipeline.script_versioning import ScriptVersionError, ScriptVersionLedger
from app.pipeline.shot_director import build_shot_plan


class PipelineWorkspaceNotFoundError(ValueError):
    pass


PRESENTATION_OPTIONS_TYPE = "presentation_options"

_ACTIVE_EXECUTION_STATES = frozenset({"active", "claimed", "running", "publishing"})
_ACTIVE_MATERIAL_STATUSES = frozenset(
    {
        "进行中",
        "官方页面录屏中",
        "Pexels下载中",
        "Pixabay下载中",
        "Coverr下载中",
        "信息动效生成中",
        "发布收口中",
    }
)
_BLOCKED_MATERIAL_STATUSES = frozenset({"被阻止"})
_LEGACY_BLOCKED_MATERIAL_STATUS = "需要人工确认"
_PUBLIC_EXECUTION_STATES = frozenset(
    {
        "idle",
        "none",
        "active",
        "claimed",
        "running",
        "publishing",
        "blocked",
        "finished",
        "succeeded",
        "failed",
        "terminated",
    }
)
_PUBLICATION_PHASES = frozenset(
    {
        "idle",
        "none",
        "preparing",
        "rolled_back",
        "db_committed",
        "committed",
        "audit_required",
    }
)
_PUBLIC_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PUBLIC_AVATAR_TASK_FIELDS = frozenset(
    {
        "shot_id",
        "required",
        "enabled",
        "position",
        "state",
        "avoid_subtitle",
        "avoid_chart",
        "lip_sync",
        "material_path",
        "generated",
        "disabled_reason",
    }
)


@dataclass(slots=True)
class ScriptWorkspaceResult:
    pipeline_id: int
    status: str
    completion_reason_code: str
    next_surface: str
    current_step: str
    needs_confirmation: bool
    next_suggestion: str
    provider: str
    project_name: str
    project_key: str
    project_dir: str
    legacy_project_dir: str | None
    hotspot: dict[str, Any] = field(default_factory=dict)
    topic: dict[str, Any] = field(default_factory=dict)
    script: dict[str, Any] = field(default_factory=dict)
    material_tasks: list[dict[str, Any]] = field(default_factory=list)
    graphic_tasks: list[dict[str, Any]] = field(default_factory=list)
    avatar_tasks: list[dict[str, Any]] = field(default_factory=list)
    exports: dict[str, dict[str, Any]] = field(default_factory=dict)
    script_factory: dict[str, Any] = field(default_factory=dict)
    script_versions: list[dict[str, Any]] = field(default_factory=list)
    shot_plan: list[dict[str, Any]] = field(default_factory=list)
    active_script_version_id: str = ""
    script_state_sha256: str = ""
    material_selection_history: list[dict[str, Any]] = field(default_factory=list)


def get_script_workspace(
    session: Session,
    pipeline_id: int,
    *,
    project_root: Path | None = None,
) -> ScriptWorkspaceResult:
    pipeline = (
        session.query(ProductionPipeline)
        .options(
            selectinload(ProductionPipeline.steps),
            selectinload(ProductionPipeline.script_asset),
        )
        .filter(ProductionPipeline.id == pipeline_id)
        .one_or_none()
    )
    if pipeline is None:
        raise PipelineWorkspaceNotFoundError(f"流程不存在：{pipeline_id}")

    hotspot = session.get(HotItem, pipeline.hotspot_id)
    topic = (
        session.get(TopicIdea, pipeline.topic_idea_id)
        if pipeline.topic_idea_id is not None
        else None
    )
    script = pipeline.script_asset
    decision = evaluate_pipeline_completion(pipeline)
    use_avatar = script_use_avatar(script)
    material_tasks: list[dict[str, Any]] = []
    graphic_tasks: list[dict[str, Any]] = []
    avatar_tasks: list[dict[str, Any]] = []
    exports: dict[str, dict[str, Any]] = {}
    script_factory: dict[str, Any] = {}
    script_versions: list[dict[str, Any]] = []
    shot_plan: list[dict[str, Any]] = []
    active_script_version_id = ""
    script_state_sha256 = ""
    material_selection_history: list[dict[str, Any]] = []
    if decision.reason_code != "preparation_invalid":
        steps = {step.name: step for step in pipeline.steps}
        material_payload = _step_output(steps, "生成素材任务")
        graphic_payload = _step_output(steps, "生成 Codex 图文任务")
        avatar_payload = _step_output(steps, "生成数字人字段")
        material_tasks = _serialize_material_tasks(
            material_payload,
            use_avatar=use_avatar,
            pipeline_id=pipeline.id,
        )
        material_selection_history = _serialize_material_selection_history(
            material_payload
        )
        graphic_source = (
            _task_dicts(graphic_payload, "graphic_tasks")
            if "graphic_tasks" in graphic_payload
            else _task_dicts(material_payload, "graphic_task_candidates")
        )
        graphic_tasks = [_serialize_graphic_task(item) for item in graphic_source]
        avatar_tasks = [
            _serialize_avatar_task(item, use_avatar=use_avatar)
            for item in _task_dicts(avatar_payload, "avatar_tasks")
        ]
        exports = _serialize_exports(steps)
        script_factory = _serialize_script_factory(steps)
        script_versions, shot_plan, active_script_version_id = _script_version_surface(
            pipeline,
            script,
            project_root=project_root,
        )
        script_state_sha256 = script_workspace_state_sha256(pipeline, script)

    return ScriptWorkspaceResult(
        pipeline_id=pipeline.id,
        status=decision.status,
        completion_reason_code=decision.reason_code,
        next_surface=decision.next_surface,
        current_step=decision.current_step,
        needs_confirmation=decision.needs_confirmation,
        next_suggestion=decision.next_action,
        provider=pipeline.provider,
        project_name=pipeline.project_name,
        project_key=pipeline.project_key,
        project_dir=pipeline.project_dir,
        legacy_project_dir=pipeline.legacy_project_dir,
        hotspot=_serialize_hotspot(hotspot),
        topic=_serialize_topic(topic),
        script=_serialize_script(topic, script),
        material_tasks=material_tasks,
        graphic_tasks=graphic_tasks,
        avatar_tasks=avatar_tasks,
        exports=exports,
        script_factory=script_factory,
        script_versions=script_versions,
        shot_plan=shot_plan,
        active_script_version_id=active_script_version_id,
        script_state_sha256=script_state_sha256,
        material_selection_history=material_selection_history,
    )


def serialize_script_workspace(result: ScriptWorkspaceResult) -> dict[str, Any]:
    payload = {
        "pipeline_id": result.pipeline_id,
        "status": result.status,
        "completion_reason_code": result.completion_reason_code,
        "next_surface": result.next_surface,
        "current_step": result.current_step,
        "needs_confirmation": result.needs_confirmation,
        "next_suggestion": result.next_suggestion,
        "provider": result.provider,
        "project_name": result.project_name,
        "project_key": result.project_key,
        "project_dir": result.project_dir,
        "legacy_project_dir": result.legacy_project_dir,
        "hotspot": result.hotspot,
        "topic": result.topic,
        "script": result.script,
        "material_tasks": result.material_tasks,
        "graphic_tasks": result.graphic_tasks,
        "avatar_tasks": result.avatar_tasks,
        "exports": result.exports,
        "script_factory": result.script_factory,
        "script_versions": result.script_versions,
        "shot_plan": result.shot_plan,
        "active_script_version_id": result.active_script_version_id,
        "material_selection_history": result.material_selection_history,
    }
    if result.script_state_sha256:
        payload["script_state_sha256"] = result.script_state_sha256
    return payload


def get_script_version_detail(
    session: Session,
    pipeline_id: int,
    version_id: str,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Return one hash-verified immutable script snapshot without local paths."""

    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None:
        raise PipelineWorkspaceNotFoundError(f"流程不存在：{pipeline_id}")
    if not pipeline.project_dir:
        raise PipelineWorkspaceNotFoundError(f"流程还没有脚本版本：{pipeline_id}")

    ledger = ScriptVersionLedger(
        pipeline,
        project_root=_script_version_project_root(pipeline, project_root),
    )
    snapshots = ledger.list()
    snapshot = next(
        (item for item in snapshots if item.version_id == str(version_id or "").strip()),
        None,
    )
    if snapshot is None:
        raise PipelineWorkspaceNotFoundError(f"脚本版本不存在：{version_id}")
    return snapshot.public_dict()


def _script_version_surface(
    pipeline: ProductionPipeline,
    script: ScriptAsset | None,
    *,
    project_root: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Load immutable version metadata and the active semantic ShotPlan.

    Legacy projects may not have the D: version directory yet.  They still get
    a read-only local ShotPlan preview, while only an explicit Script Factory
    import creates a durable version.
    """

    versions = []
    if pipeline.project_dir:
        ledger = ScriptVersionLedger(
            pipeline,
            project_root=_script_version_project_root(pipeline, project_root),
        )
        snapshots = ledger.list()
        versions = [
            {
                "version_id": item.version_id,
                "version_number": item.version_number,
                "input_hash": item.input_hash,
                "content_hash": item.content_hash,
                "schema_version": item.schema_version,
                "shot_plan_hash": item.shot_plan_hash,
                "snapshot_hash": item.snapshot_hash,
                "integrity_status": item.integrity_status,
                "approval_integrity_status": item.approval_integrity_status,
                "approval_event_hash": item.approval_event_hash,
                "approval_evidence_hash": item.approval_evidence_hash,
                "status": item.status,
                "source": item.source,
                "created_at": item.created_at,
                "approved_at": item.approved_at,
                "invalidated_at": item.invalidated_at,
                "invalidated_reason": item.invalidated_reason,
                "shot_count": len(item.shot_plan),
            }
            for item in snapshots
        ]
        current_version_id = _current_script_version_id(pipeline)
        if current_version_id:
            active = next(
                (item for item in snapshots if item.version_id == current_version_id),
                None,
            )
            if active is None:
                raise ScriptVersionError("当前脚本引用的版本不存在")
        else:
            active = next(
                (item for item in reversed(snapshots) if item.status == "approved"),
                snapshots[-1] if snapshots else None,
            )
        if active is not None:
            return versions, [dict(item) for item in active.shot_plan], active.version_id

    if script is None:
        return versions, [], ""
    try:
        preview_payload = {
            "voiceover": script.voiceover,
            "storyboard": script.storyboard,
            "subtitles": script.subtitles,
        }
        return versions, build_shot_plan(preview_payload), ""
    except (TypeError, ValueError):
        return versions, [], ""


def _current_script_version_id(pipeline: ProductionPipeline) -> str:
    script_step = next(
        (step for step in pipeline.steps if step.name == "生成脚本素材"),
        None,
    )
    output = script_step.output_summary if script_step is not None else None
    if not isinstance(output, dict):
        return ""
    state = output.get("script_factory") or output.get("script_work_order") or {}
    if not isinstance(state, dict):
        return ""
    return str(state.get("script_version_id") or "").strip()


def script_workspace_state_sha256(
    pipeline: ProductionPipeline,
    script: ScriptAsset | None,
) -> str:
    """Hash every mutable script surface used by restore conflict checks.

    This token is intentionally broader than the current ScriptVersion id.  It
    also covers presentation options and the complete script-step state so a
    stale page cannot silently overwrite a newer manual or background change.
    """

    script_step = next(
        (step for step in pipeline.steps if step.name == "生成脚本素材"),
        None,
    )
    value = {
        "pipeline_script_asset_id": pipeline.script_asset_id,
        "script_asset": _script_asset_state(script),
        "script_step": {
            "id": script_step.id if script_step is not None else None,
            "status": script_step.status if script_step is not None else "",
            "error": script_step.error if script_step is not None else "",
            "output_summary": (
                copy.deepcopy(script_step.output_summary or {})
                if script_step is not None
                else {}
            ),
        },
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _script_asset_state(script: ScriptAsset | None) -> dict[str, Any]:
    if script is None:
        return {
            "id": None,
            "voiceover": "",
            "storyboard": [],
            "subtitles": [],
            "animation_prompts": [],
            "title_options": [],
            "cover_copy": [],
            "publish_copy": {},
            "edit_checklist": [],
        }
    return {
        "id": script.id,
        "voiceover": script.voiceover,
        "storyboard": copy.deepcopy(script.storyboard or []),
        "subtitles": copy.deepcopy(script.subtitles or []),
        "animation_prompts": copy.deepcopy(script.animation_prompts or []),
        "title_options": copy.deepcopy(script.title_options or []),
        "cover_copy": copy.deepcopy(script.cover_copy or []),
        "publish_copy": copy.deepcopy(script.publish_copy or {}),
        "edit_checklist": copy.deepcopy(script.edit_checklist or []),
    }


def _script_version_project_root(
    pipeline: ProductionPipeline,
    project_root: Path | None,
) -> Path:
    if project_root is not None:
        return resolve_configured_path(project_root)
    if not pipeline.project_dir:
        return resolve_configured_path(get_settings().project_root_dir)
    return Path(pipeline.project_dir).parent


def update_script_workspace(
    session: Session,
    pipeline_id: int,
    payload: dict[str, Any],
    *,
    project_root: Path | None = None,
) -> ScriptWorkspaceResult:
    pipeline = (
        session.query(ProductionPipeline)
        .options(selectinload(ProductionPipeline.steps))
        .filter(ProductionPipeline.id == pipeline_id)
        .one_or_none()
    )
    if pipeline is None:
        raise PipelineWorkspaceNotFoundError(f"流程不存在：{pipeline_id}")
    if pipeline.script_asset_id is None:
        raise PipelineWorkspaceNotFoundError(f"流程还没有脚本素材：{pipeline_id}")

    script = session.get(ScriptAsset, pipeline.script_asset_id)
    if script is None:
        raise PipelineWorkspaceNotFoundError(f"脚本素材不存在：{pipeline.script_asset_id}")
    script = ensure_pipeline_script_asset_owned(session, pipeline, script)

    before_material_input_sha256 = material_input_sha256_for_script(script)
    voiceover = str(payload.get("voiceover", ""))
    storyboard = _list_of_dicts(payload.get("storyboard"), "分镜脚本")
    subtitles = _list_of_dicts(payload.get("subtitles"), "字幕内容")
    title_options = _list_of_dicts(payload.get("title_options"), "标题选项")
    cover_copy = _list_of_dicts(payload.get("cover_copy"), "封面文案")
    publish_copy = _dict_payload(payload.get("publish_copy"), "发布文案")
    next_use_avatar = bool(payload.get("use_avatar", script_use_avatar(script)))
    next_material_input_sha256 = material_input_sha256(
        voiceover=voiceover,
        storyboard=storyboard,
        animation_prompts=script.animation_prompts,
        use_avatar=next_use_avatar,
    )
    next_version_payload = {
        "voiceover": voiceover,
        "storyboard": storyboard,
        "subtitles": subtitles,
        "animation_prompts": script.animation_prompts,
        "title_options": title_options,
        "cover_copy": cover_copy,
        "publish_copy": publish_copy,
        "use_avatar": next_use_avatar,
    }
    current_version_payload = {
        "voiceover": script.voiceover,
        "storyboard": script.storyboard,
        "subtitles": script.subtitles,
        "animation_prompts": script.animation_prompts,
        "title_options": script.title_options,
        "cover_copy": script.cover_copy,
        "publish_copy": script.publish_copy,
        "use_avatar": script_use_avatar(script),
    }
    if next_version_payload == current_version_payload:
        return get_script_workspace(
            session,
            pipeline_id,
            project_root=project_root,
        )

    try:
        version_ledger = ScriptVersionLedger(
            pipeline,
            project_root=_script_version_project_root(pipeline, project_root),
        )
        current_versions = version_ledger.list()
        current_sources = list(current_versions[-1].fact_sources) if current_versions else []
        previous_approved_ids = [
            item.version_id for item in current_versions if item.status == "approved"
        ]
        script_version = version_ledger.create(
            next_version_payload,
            fact_sources=current_sources,
            source="manual_script_edit",
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
                "draft_source": "manual_script_edit",
            },
        )
    except ScriptVersionError as exc:
        raise ScriptVersionError(
            f"脚本版本保存失败，未覆盖当前脚本：{exc}"
        ) from exc

    script.voiceover = voiceover
    script.storyboard = storyboard
    script.subtitles = subtitles
    script.title_options = title_options
    script.cover_copy = cover_copy
    script.publish_copy = publish_copy
    if payload.get("use_avatar") is not None:
        set_script_use_avatar(script, bool(payload.get("use_avatar")))

    session.add(script)
    _reset_downstream_after_script_edit(
        session,
        pipeline,
        material_contract_changed=(
            before_material_input_sha256 != next_material_input_sha256
        ),
        target_script_version_id=script_version.version_id,
        target_script_version_hash=script_version.content_hash,
        target_material_input_sha256=next_material_input_sha256,
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
                "next_suggestion": "脚本已修改，需重新检查事实并批准新版本。",
            }
        )
        script_output["script_factory"] = factory_state
        script_step.output_summary = script_output
        session.add(script_step)
    pipeline.status = "进行中"
    session.add(pipeline)
    session.commit()
    finalize_script_draft_operation(
        version_ledger,
        draft_operation.operation_id,
        reason="当前脚本已产生新的手动草稿，旧脚本批准立即失效。",
    )
    return get_script_workspace(
        session,
        pipeline_id,
        project_root=project_root,
    )


def update_script_presentation_options(
    session: Session,
    pipeline_id: int,
    *,
    use_avatar: bool,
    project_root: Path | None = None,
) -> ScriptWorkspaceResult:
    pipeline = (
        session.query(ProductionPipeline)
        .options(selectinload(ProductionPipeline.steps))
        .filter(ProductionPipeline.id == pipeline_id)
        .one_or_none()
    )
    if pipeline is None:
        raise PipelineWorkspaceNotFoundError(f"流程不存在：{pipeline_id}")
    if pipeline.script_asset_id is None:
        raise PipelineWorkspaceNotFoundError(f"流程还没有脚本素材：{pipeline_id}")

    script = session.get(ScriptAsset, pipeline.script_asset_id)
    if script is None:
        raise PipelineWorkspaceNotFoundError(f"脚本素材不存在：{pipeline.script_asset_id}")
    script = ensure_pipeline_script_asset_owned(session, pipeline, script)
    set_script_use_avatar(script, use_avatar)
    _apply_avatar_choice_to_existing_steps(pipeline, use_avatar)
    _reset_steps_after_avatar_choice_change(pipeline)
    session.add(script)
    session.add(pipeline)
    session.commit()
    return get_script_workspace(
        session,
        pipeline_id,
        project_root=project_root,
    )


def reset_downstream_after_script_edit(
    session: Session,
    pipeline: ProductionPipeline,
    *,
    material_contract_changed: bool,
    target_script_version_id: str,
    target_script_version_hash: str,
    target_material_input_sha256: str,
) -> None:
    _reset_downstream_after_script_edit(
        session,
        pipeline,
        material_contract_changed=material_contract_changed,
        target_script_version_id=target_script_version_id,
        target_script_version_hash=target_script_version_hash,
        target_material_input_sha256=target_material_input_sha256,
    )


def finalize_script_draft_operation(
    ledger: ScriptVersionLedger,
    operation_id: str,
    *,
    reason: str,
) -> None:
    """Close a draft intent only after its database transaction committed."""

    operation = next(
        (
            item
            for item in ledger.list_operations()
            if item.operation_id == operation_id
        ),
        None,
    )
    if operation is None or operation.kind != "draft":
        raise ScriptVersionError("脚本草稿 operation 不存在或类型不一致")
    if operation.status != "pending":
        if operation.status == "completed":
            return
        raise ScriptVersionError("脚本草稿 operation 已以非完成状态结束")
    previous_ids = operation.context.get("previous_approved_version_ids")
    if not isinstance(previous_ids, list) or any(
        not isinstance(item, str) or not item.strip() for item in previous_ids
    ):
        raise ScriptVersionError("脚本草稿 operation 的历史批准列表无效")
    invalidated: list[str] = []
    for version_id in sorted(set(previous_ids)):
        if version_id == operation.version_id:
            continue
        previous = ledger.get(version_id)
        if previous.status == "approved":
            ledger.invalidate(
                version_id,
                reason=reason,
                operation_id=operation.operation_id,
            )
        if ledger.get(version_id).status == "invalidated":
            invalidated.append(version_id)
    ledger.finish_operation(
        operation.operation_id,
        outcome="completed",
        result={
            "database_state": "matched",
            "previous_approvals_invalidated": invalidated,
        },
    )


def _step_output(steps: dict, name: str) -> dict[str, Any]:
    step = steps.get(name)
    output = step.output_summary if step is not None else None
    return output if isinstance(output, dict) else {}


def _task_dicts(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _serialize_material_selection_history(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_history = payload.get("material_selection_history")
    if not isinstance(raw_history, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in raw_history:
        if not isinstance(raw, dict):
            continue
        locked_rows = raw.get("locked_selections")
        locked = []
        if isinstance(locked_rows, list):
            locked = [
                {
                    "shot_id": str(item.get("shot_id") or ""),
                    "candidate_id": str(item.get("candidate_id") or ""),
                    "reason": str(item.get("reason") or ""),
                    "source": str(item.get("source") or ""),
                    "media_sha256": str(item.get("media_sha256") or ""),
                }
                for item in locked_rows
                if isinstance(item, dict)
            ]
        result.append(
            {
                "event": str(raw.get("event") or ""),
                "reason_code": str(raw.get("reason_code") or ""),
                "reason": str(raw.get("reason") or ""),
                "stale_at": str(raw.get("stale_at") or ""),
                "source_script_version_id": str(
                    raw.get("source_script_version_id") or ""
                ),
                "target_script_version_id": str(
                    raw.get("target_script_version_id") or ""
                ),
                "locked_selections": locked,
                "entry_sha256": str(raw.get("entry_sha256") or ""),
            }
        )
    return result


def _serialize_hotspot(hotspot: HotItem | None) -> dict[str, Any]:
    if hotspot is None:
        return {"id": None, "title": "", "summary": ""}
    return {
        "id": hotspot.id,
        "title": hotspot.title,
        "summary": hotspot.summary,
    }


def _serialize_topic(topic: TopicIdea | None) -> dict[str, Any]:
    if topic is None:
        return {
            "id": None,
            "title": "",
            "core_view": "",
            "recommended_format": "",
            "recommended_duration": "",
            "risk_note": "",
            "hook": "",
        }
    return {
        "id": topic.id,
        "title": topic.title,
        "core_view": topic.core_view,
        "recommended_format": topic.recommended_format,
        "recommended_duration": topic.recommended_duration,
        "risk_note": topic.risk_note,
        "hook": topic.hook,
    }


def _serialize_script(
    topic: TopicIdea | None,
    script: ScriptAsset | None,
) -> dict[str, Any]:
    return {
        "id": script.id if script is not None else None,
        "title": topic.title if topic is not None else "",
        "voiceover": script.voiceover if script is not None else "",
        "storyboard": script.storyboard if script is not None else [],
        "subtitles": script.subtitles if script is not None else [],
        "title_options": script.title_options if script is not None else [],
        "cover_copy": script.cover_copy if script is not None else [],
        "publish_copy": script.publish_copy if script is not None else {},
        "use_avatar": script_use_avatar(script),
    }


def _serialize_material_tasks(
    payload: dict[str, Any],
    *,
    use_avatar: bool,
    pipeline_id: int | None = None,
) -> list[dict[str, Any]]:
    items = _task_dicts(payload, "material_tasks")
    target_index = _material_execution_target_index(payload, items)
    raw_candidate_pools = payload.get("candidate_pool")
    candidate_pools: dict[str, Any] = {}
    if isinstance(raw_candidate_pools, dict):
        # A single serialized CandidateSet is accepted for early/migration
        # payloads; the durable shape is a shot-id keyed mapping.
        if "shot_id" in raw_candidate_pools and "candidates" in raw_candidate_pools:
            pool_shot_id = raw_candidate_pools.get("shot_id")
            if isinstance(pool_shot_id, str) and pool_shot_id.strip():
                candidate_pools[pool_shot_id] = raw_candidate_pools
        else:
            candidate_pools = raw_candidate_pools
    return [
        _serialize_material_task(
            item,
            use_avatar=use_avatar,
            execution_payload=payload if index == target_index else None,
            candidate_pool=(
                candidate_pools.get(str(item.get("shot_id") or ""))
                if candidate_pools
                else None
            ),
            pipeline_id=pipeline_id,
        )
        for index, item in enumerate(items)
    ]


def _serialize_material_task(
    item: dict[str, Any],
    *,
    use_avatar: bool,
    execution_payload: dict[str, Any] | None = None,
    candidate_pool: dict[str, Any] | None = None,
    pipeline_id: int | None = None,
) -> dict[str, Any]:
    asset_keywords = item.get("asset_keywords") or item.get("jianying_keywords") or []
    shot_id = str(item.get("shot_id") or "").strip()
    material_task_id = str(item.get("material_task_id") or "").strip()
    if not material_task_id and shot_id:
        material_task_id = material_task_id_for_shot(shot_id)
    execution = _serialize_material_execution_metadata(item, execution_payload)
    status = _material_task_status(item, execution)
    reject_reason = item.get("reject_reason", "")
    notes = item.get("notes", "")
    if status == "被阻止" and not reject_reason and not notes:
        reject_reason = execution["blocked_reason"]
    serialized = {
        "material_task_id": material_task_id,
        "shot_id": shot_id,
        "time_range": f"{item.get('start', '')}-{item.get('end', '')}".strip("-"),
        "voiceover": item.get("voiceover", ""),
        "recommended_visual": item.get("recommended_visual", ""),
        "material_type": item.get("material_type", ""),
        "jianying_keywords": item.get("jianying_keywords", []),
        "asset_keywords": asset_keywords,
        "source_priority": item.get("source_priority", []),
        "needs_official_capture": bool(item.get("needs_official_capture", False)),
        "needs_pexels": bool(item.get("needs_pexels", False)),
        "needs_info_motion": bool(item.get("needs_info_motion", False)),
        "needs_backup_asset": bool(item.get("needs_backup_asset", False)),
        "needs_codex_graphic": bool(item.get("needs_codex_graphic", False)),
        "needs_avatar": bool(use_avatar and item.get("avatar_required", False)),
        "status": status,
        "material_path": item.get("material_path", ""),
        "source_type": item.get("source_type", ""),
        "evidence_kind": item.get("evidence_kind", ""),
        "source_url": item.get("source_url", ""),
        "is_locally_generated": bool(item.get("is_locally_generated", False)),
        "beat_id": item.get("beat_id", ""),
        "beat_type": item.get("beat_type", ""),
        "viewer_takeaway": item.get("viewer_takeaway", ""),
        "visual_intent": item.get("visual_intent", ""),
        "source_refs": item.get("source_refs", []),
        "acceptance_conditions": item.get("acceptance_conditions", []),
        "forbidden_items": item.get("forbidden_items", []),
        "director_version": item.get("director_version", ""),
        "subject": item.get("subject", ""),
        "action": item.get("action", ""),
        "location": item.get("location", ""),
        "time_context": item.get("time_context", ""),
        "composition": item.get("composition", ""),
        "visual_type": item.get("visual_type", ""),
        "sound_notes": item.get("sound_notes", ""),
        "reject_reason": reject_reason,
        "notes": notes,
        "status_history": item.get("status_history", []),
        "reason_code": execution["reason_code"],
        "execution_id": execution["execution_id"],
        "execution_state": execution["execution_state"],
        "active_attempt_id": execution["active_attempt_id"],
        "publication_phase": execution["publication_phase"],
        "retry_available": execution["retry_available"],
        "recovery_action_available": execution["recovery_action_available"],
    }
    serialized_pool = _serialize_candidate_pool(candidate_pool)
    if serialized_pool is not None:
        if pipeline_id is not None:
            serialized_pool = public_candidate_pool(
                serialized_pool,
                pipeline_id=pipeline_id,
                shot_id=shot_id,
            )
        serialized["candidate_pool"] = serialized_pool
    return serialized


def _serialize_candidate_pool(value: Any) -> dict[str, Any] | None:
    """Expose a defensive candidate-pool snapshot on its owning task.

    Read paths stay tolerant of legacy/malformed payloads: an invalid pool is
    omitted rather than fabricated or allowed to alter the existing task
    shape.  Write paths use ``CandidateSet`` and therefore remain strict.
    """

    if not isinstance(value, dict):
        return None
    candidates = value.get("candidates")
    if not isinstance(candidates, list):
        return None
    result = copy.deepcopy(value)
    result["candidates"] = [item for item in candidates if isinstance(item, dict)]
    if "shot_id" not in result or not isinstance(result.get("shot_id"), str):
        return None
    if "revision" not in result or type(result.get("revision")) is not int:
        result["revision"] = 0
    locked_id = result.get("locked_candidate_id")
    if not isinstance(locked_id, str) or not locked_id:
        locked_id = next(
            (
                item.get("candidate_id")
                for item in result["candidates"]
                if item.get("status") == "locked" and item.get("candidate_id")
            ),
            None,
        )
    result["locked_candidate_id"] = locked_id
    return result


def _material_execution_target_index(
    payload: dict[str, Any],
    items: list[dict[str, Any]],
) -> int | None:
    active_task_id = _safe_public_identifier(payload.get("active_material_task_id"))
    if active_task_id:
        matches = [
            index
            for index, item in enumerate(items)
            if active_task_id in _material_task_public_identities(item)
        ]
        if len(matches) == 1:
            return matches[0]

    status_matches = [
        index
        for index, item in enumerate(items)
        if (
            _safe_public_text(item.get("status"), max_length=64)
            in _ACTIVE_MATERIAL_STATUSES | _BLOCKED_MATERIAL_STATUSES
            or (
                _safe_public_text(item.get("status"), max_length=64)
                == _LEGACY_BLOCKED_MATERIAL_STATUS
                and payload.get("execution_state") == "blocked"
            )
        )
    ]
    if len(status_matches) == 1:
        return status_matches[0]
    return None


def _material_task_public_identities(item: dict[str, Any]) -> set[str]:
    shot_id = _safe_public_identifier(item.get("shot_id"))
    material_task_id = _safe_public_identifier(item.get("material_task_id"))
    if not material_task_id and shot_id:
        material_task_id = _safe_public_identifier(material_task_id_for_shot(shot_id))
    return {value for value in (shot_id, material_task_id) if value}


def _serialize_material_execution_metadata(
    item: dict[str, Any],
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    control = payload or {}
    execution_state = _safe_public_enum(
        _first_present(control, item, "execution_state"),
        _PUBLIC_EXECUTION_STATES,
    )
    publication_phase = _safe_public_enum(
        _first_present(control, item, "publication_phase"),
        _PUBLICATION_PHASES,
    )
    task_status = _safe_public_text(item.get("status"), max_length=64)
    blocked = execution_state == "blocked" or task_status in _BLOCKED_MATERIAL_STATUSES
    recovery_action_available = not blocked and (
        execution_state in _ACTIVE_EXECUTION_STATES
        or task_status in _ACTIVE_MATERIAL_STATUSES
        or publication_phase == "db_committed"
    )
    blocked_reason = _safe_public_text(
        _first_present(control, item, "blocked_reason"),
        max_length=2000,
    )
    retry_available = _retry_is_safe(control, item, blocked=blocked)
    return {
        "reason_code": _safe_reason_code(_first_present(control, item, "reason_code")),
        "execution_id": _safe_execution_identifier(
            _first_non_empty(
                control.get("active_execution_id"),
                control.get("execution_id"),
                item.get("execution_id"),
            )
        ),
        "execution_state": execution_state,
        "active_attempt_id": _safe_execution_identifier(
            _first_non_empty(control.get("active_attempt_id"), item.get("active_attempt_id"))
        ),
        "publication_phase": publication_phase,
        "retry_available": retry_available,
        "recovery_action_available": recovery_action_available,
        "blocked_reason": blocked_reason,
    }


def _retry_is_safe(
    control: dict[str, Any],
    item: dict[str, Any],
    *,
    blocked: bool,
) -> bool:
    if not blocked or _first_present(control, item, "retry_available") is not True:
        return False
    required_identifiers = (
        _first_non_empty(
            control.get("active_execution_id"),
            control.get("execution_id"),
            item.get("execution_id"),
        ),
        _first_non_empty(control.get("active_attempt_id"), item.get("active_attempt_id")),
        control.get("active_control_id"),
        control.get("active_job_instance_id"),
    )
    return all(
        _safe_execution_identifier(value)
        for value in required_identifiers[:2]
    ) and all(_safe_public_identifier(value) for value in required_identifiers[2:])


def _material_task_status(item: dict[str, Any], execution: dict[str, Any]) -> str:
    status = _safe_public_text(item.get("status"), max_length=64) or "等待"
    if execution["execution_state"] == "blocked":
        return "被阻止"
    if (
        execution["execution_state"] == "publishing"
        or execution["publication_phase"] == "db_committed"
    ):
        return "发布收口中"
    if (
        execution["execution_state"] in _ACTIVE_EXECUTION_STATES
        and status not in _ACTIVE_MATERIAL_STATUSES
    ):
        return "进行中"
    return status


def _first_present(primary: dict[str, Any], fallback: dict[str, Any], key: str) -> Any:
    return primary[key] if key in primary else fallback.get(key)


def _first_non_empty(*values: Any) -> Any:
    return next((value for value in values if value not in (None, "")), "")


def _safe_public_identifier(value: Any) -> str:
    if type(value) is not str:
        return ""
    candidate = value.strip()
    if _PUBLIC_IDENTIFIER_PATTERN.fullmatch(candidate) is None:
        return ""
    return candidate


def _safe_execution_identifier(value: Any) -> str:
    """Expose only canonical UUIDs for execution/attempt identities.

    The generic identifier helper is intentionally broader because shot and
    task IDs are human-facing tokens.  Execution IDs, however, are ownership
    capabilities in the control plane; returning an arbitrary token-shaped
    value could echo a secret or a forged recovery identity into the UI.
    """

    if type(value) is not str:
        return ""
    candidate = value.strip()
    try:
        if str(UUID(candidate)) != candidate:
            return ""
    except ValueError:
        return ""
    return candidate


def _safe_reason_code(value: Any) -> str:
    if type(value) is not str:
        return ""
    candidate = value.strip()
    if _REASON_CODE_PATTERN.fullmatch(candidate) is None:
        return ""
    return candidate


def _safe_public_enum(value: Any, allowed: frozenset[str]) -> str:
    return value if type(value) is str and value in allowed else ""


def _safe_public_text(value: Any, *, max_length: int) -> str:
    if type(value) is not str:
        return ""
    candidate = value.strip()
    if len(candidate) > max_length or any(ord(char) < 32 for char in candidate):
        return ""
    return candidate


def _serialize_avatar_task(item: dict[str, Any], *, use_avatar: bool) -> dict[str, Any]:
    # Avatar payloads are persisted JSON assembled by several upstream
    # steps.  Never pass unknown keys through the workspace boundary: a
    # future step may contain API keys, handles, or internal file metadata.
    serialized = {
        key: item[key]
        for key in _PUBLIC_AVATAR_TASK_FIELDS
        if key in item
    }
    serialized["required"] = bool(use_avatar and item.get("required", False))
    serialized["enabled"] = use_avatar
    if not use_avatar:
        serialized["disabled_reason"] = "本期选择不添加数字人"
    return serialized


def _serialize_graphic_task(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "shot_id": item.get("shot_id", ""),
        "time_range": item.get("time_range", ""),
        "visual_form": item.get("visual_form", ""),
        "core_message": item.get("core_message", ""),
        "keywords": item.get("keywords", []),
        "status": "未开始",
        "save_path": item.get("save_path", ""),
        "codex_skill_executed": bool(item.get("generated_by_codex_skill", False)),
    }


def _serialize_exports(steps: dict) -> dict[str, dict[str, Any]]:
    exports = {}
    for step_name, key in [
        ("生成剪辑清单", "timeline_json"),
        ("导出 script.md", "script_md"),
        ("导出 shotlist.csv", "shotlist_csv"),
        ("导出 edit_manifest.json", "edit_manifest_json"),
    ]:
        payload = _step_output(steps, step_name)
        path = str(payload.get("output_path", "") or "")
        exports[key] = {
            "generated": bool(path),
            "path": path,
            "exists": _path_is_file(path),
        }
        if step_name == "生成剪辑清单":
            generated_files = payload.get("generated_files", {})
            if isinstance(generated_files, dict):
                for file_key, file_path in generated_files.items():
                    path_value = str(file_path or "")
                    exports[str(file_key)] = {
                        "generated": bool(path_value),
                        "path": path_value,
                        "exists": _path_is_file(path_value),
                    }
    return exports


def _serialize_script_factory(steps: dict) -> dict[str, Any]:
    payload = _step_output(steps, "生成脚本素材")
    raw = payload.get("script_factory") or payload.get("script_work_order") or {}
    if not isinstance(raw, dict):
        raw = {}
    result_path = str(raw.get("result_path", "") or "")
    result_exists = _path_is_file(result_path)
    status = str(raw.get("status") or "not_built")
    if result_exists and status in {
        "built",
        "manual_dispatch_required",
        "dispatched",
    }:
        status = "result_found"
    return {
        "status": status,
        "job_id": str(raw.get("job_id", "") or ""),
        "job_dir": str(raw.get("job_dir", "") or ""),
        "file_path": str(raw.get("file_path") or raw.get("prompt_path") or ""),
        "prompt_path": str(raw.get("prompt_path") or raw.get("file_path") or ""),
        "context_path": str(raw.get("context_path", "") or ""),
        "template_path": str(raw.get("template_path", "") or ""),
        "result_path": result_path,
        "result_exists": result_exists,
        "real_ai_called": bool(raw.get("real_ai_called", False)),
        "failure_reason": str(raw.get("failure_reason", "") or ""),
        "next_suggestion": str(raw.get("next_suggestion", "") or ""),
        "accepted": status == "accepted",
        "dispatch_supported": bool(raw.get("dispatch_supported", False)),
        "thread_id": str(raw.get("thread_id", "") or ""),
        "manual_prompt": str(raw.get("manual_prompt", "") or ""),
        "script_version_id": str(raw.get("script_version_id", "") or ""),
        "script_version_hash": str(raw.get("script_version_hash", "") or ""),
        "script_version_input_hash": str(
            raw.get("script_version_input_hash", "") or ""
        ),
        "script_version_snapshot_hash": str(
            raw.get("script_version_snapshot_hash", "") or ""
        ),
        "script_version_shot_plan_hash": str(
            raw.get("script_version_shot_plan_hash", "") or ""
        ),
        "script_result_hash": str(raw.get("script_result_hash", "") or ""),
        "approval_event_hash": str(raw.get("approval_event_hash", "") or ""),
        "approval_evidence_hash": str(
            raw.get("approval_evidence_hash", "") or ""
        ),
        "approval_quality_hash": str(raw.get("approval_quality_hash", "") or ""),
        "import_operation_id": str(raw.get("import_operation_id", "") or ""),
        "draft_operation_id": str(raw.get("draft_operation_id", "") or ""),
        "restore_operation_id": str(raw.get("restore_operation_id", "") or ""),
        "approval_operation_id": str(raw.get("approval_operation_id", "") or ""),
        "restored_from_version_id": str(
            raw.get("restored_from_version_id", "") or ""
        ),
        "restored_from_content_hash": str(
            raw.get("restored_from_content_hash", "") or ""
        ),
        "restored_from_snapshot_hash": str(
            raw.get("restored_from_snapshot_hash", "") or ""
        ),
        "ready_for_review": status in {"imported", "accepted", "blocked"},
    }


def _path_is_file(path: str) -> bool:
    if not path:
        return False
    try:
        return Path(path).is_file()
    except (OSError, ValueError):
        return False


def _list_of_dicts(value: Any, label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label}必须是列表。")
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{label}里的每一项都必须是对象。")
        items.append(dict(item))
    return items


def _dict_payload(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是对象。")
    return dict(value)


def script_use_avatar(script: ScriptAsset | None) -> bool:
    if script is None:
        return True
    for item in script.edit_checklist or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == PRESENTATION_OPTIONS_TYPE:
            return bool(item.get("use_avatar", True))
    return True


def set_script_use_avatar(script: ScriptAsset, use_avatar: bool) -> None:
    items = [
        dict(item)
        for item in (script.edit_checklist or [])
        if isinstance(item, dict) and item.get("type") != PRESENTATION_OPTIONS_TYPE
    ]
    items.insert(
        0,
        {
            "type": PRESENTATION_OPTIONS_TYPE,
            "use_avatar": use_avatar,
            "voice_mode": "digital_human" if use_avatar else "voiceover_only",
        },
    )
    script.edit_checklist = items


def _apply_avatar_choice_to_existing_steps(
    pipeline: ProductionPipeline,
    use_avatar: bool,
) -> None:
    steps = {step.name: step for step in pipeline.steps}
    avatar_step = steps.get("生成数字人字段")
    if avatar_step is not None and isinstance(avatar_step.output_summary, dict):
        payload = dict(avatar_step.output_summary)
        tasks = [
            _with_avatar_task_required(item, use_avatar)
            for item in payload.get("avatar_tasks", [])
            if isinstance(item, dict)
        ]
        payload["avatar_enabled"] = use_avatar
        payload["avatar_tasks"] = tasks
        payload["avatar_task_count"] = len(tasks)
        avatar_step.output_summary = payload


def _with_avatar_task_required(item: dict[str, Any], use_avatar: bool) -> dict[str, Any]:
    updated = dict(item)
    updated["required"] = use_avatar
    updated["enabled"] = use_avatar
    if not use_avatar:
        updated["disabled_reason"] = "本期选择不添加数字人"
    else:
        updated.pop("disabled_reason", None)
    return updated


def _reset_steps_after_avatar_choice_change(pipeline: ProductionPipeline) -> None:
    for step in pipeline.steps:
        if step.name not in {
            "生成剪辑清单",
            "导出 script.md",
            "导出 shotlist.csv",
            "导出 edit_manifest.json",
        }:
            continue
        step.status = "未开始"
        step.error = ""
        step.output_summary = {}
        step.started_at = None
        step.ended_at = None


def _reset_downstream_after_script_edit(
    session: Session,
    pipeline: ProductionPipeline,
    *,
    material_contract_changed: bool,
    target_script_version_id: str,
    target_script_version_hash: str,
    target_material_input_sha256: str,
) -> None:
    stale_step_names = {
        "生成素材任务",
        "生成 Codex 图文任务",
        "生成数字人字段",
        "生成剪辑清单",
        "导出 script.md",
        "导出 shotlist.csv",
        "导出 edit_manifest.json",
    }
    for step in pipeline.steps:
        if step.name not in stale_step_names:
            continue
        if step.name == "生成素材任务":
            if not material_contract_changed:
                continue
            _invalidate_material_step_with_cas(
                session,
                step,
                target_script_version_id=target_script_version_id,
                target_script_version_hash=target_script_version_hash,
                target_material_input_sha256=target_material_input_sha256,
            )
            continue
        step.status = "未开始"
        step.error = ""
        step.output_summary = {}
        step.started_at = None
        step.ended_at = None


def _invalidate_material_step_with_cas(
    session: Session,
    step: PipelineStep,
    *,
    target_script_version_id: str,
    target_script_version_hash: str,
    target_material_input_sha256: str,
) -> None:
    """Merge the newest candidate lock into a short, whole-step CAS."""

    for _attempt in range(3):
        with session.no_autoflush:
            expected_payload = session.execute(
                select(PipelineStep.output_summary).where(PipelineStep.id == step.id)
            ).scalar_one()
            replacement_payload = invalidate_material_selection_payload(
                expected_payload if isinstance(expected_payload, dict) else {},
                target_script_version_id=target_script_version_id,
                target_script_version_hash=target_script_version_hash,
                target_material_input_sha256=target_material_input_sha256,
            )
            result = session.execute(
                update(PipelineStep)
                .where(
                    PipelineStep.id == step.id,
                    PipelineStep.pipeline_id == step.pipeline_id,
                    PipelineStep.output_summary == expected_payload,
                )
                .values(
                    status="未开始",
                    error="",
                    output_summary=replacement_payload,
                    started_at=None,
                    ended_at=None,
                )
                .execution_options(synchronize_session=False)
            )
        if result.rowcount == 1:
            session.expire(step)
            return
        if result.rowcount not in {0, 1}:
            raise RuntimeError("素材选择失效提交结果未知，请刷新项目后核对。")
    raise RuntimeError("素材选择刚被其他操作修改，请刷新后重试脚本保存。")
