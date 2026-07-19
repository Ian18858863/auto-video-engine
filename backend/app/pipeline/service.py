from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.ai.providers.base import AIProvider
from app.ai.providers.fake import FakeProvider
from app.core.config import get_settings, resolve_configured_path
from app.materials.paths import PROJECT_DIRS, ProjectMaterialPaths
from app.materials.planner import (
    GraphicTask,
    MaterialPlan,
    MaterialTask,
    ShotSegment,
    build_material_plan,
    material_task_id_for_shot,
    validate_material_task_id,
)
from app.models import HotItem, PipelineStep, ProductionPipeline, ScriptAsset, TopicIdea
from app.pipeline.exporter import (
    export_project_files,
    graphic_task_to_dict,
    material_task_to_dict,
    write_edit_manifest_json,
    write_script_md,
    write_shotlist_csv,
)
from app.pipeline.editing_export_service import build_editing_export_package
from app.pipeline.completion_policy import (
    PipelineCompletionDecision,
    evaluate_pipeline_completion,
    step_output_id,
)
from app.pipeline.models import (
    PIPELINE_STEP_NAMES,
    PipelineStepResult,
    ProductionPipelineResult,
)
from app.pipeline.project_identity import (
    begin_prepared_project_child_directory_use,
    begin_prepared_project_directory_use,
    prepare_pipeline_project_directory,
    prepared_project_output_path,
    resolve_prepared_project_directory,
)
from app.pipeline.quality_contract_service import ensure_default_quality_contract
from app.pipeline.material_selection_invalidation import (
    material_input_sha256_for_script,
    material_selection_history,
)
from app.pipeline.script_service import ScriptGenerationResult, generate_script_for_topic
from app.pipeline.script_versioning import ScriptVersionError, ScriptVersionLedger
from app.pipeline.workspace_service import script_use_avatar
from app.topic_ideas.service import generate_topic_ideas_for_hotspot


class PipelineHotspotNotFoundError(ValueError):
    pass


class PipelineFilteredHotspotError(ValueError):
    pass


class PipelineNotFoundError(ValueError):
    pass


class FakeProviderRequiredError(ValueError):
    pass


class PipelineStepConflictError(ValueError):
    pass


class PipelineStepCommitUnknownError(RuntimeError):
    pass


@dataclass(slots=True)
class PipelineDependencies:
    topic_generator: Callable = generate_topic_ideas_for_hotspot
    script_generator: Callable = generate_script_for_topic
    material_planner: Callable = build_material_plan
    exporter: Callable = export_project_files
    editing_exporter: Callable = build_editing_export_package
    script_exporter: Callable = write_script_md
    shotlist_exporter: Callable = write_shotlist_csv
    manifest_exporter: Callable = write_edit_manifest_json


@dataclass(frozen=True, slots=True)
class _ApprovedShotPlanBinding:
    shot_plan: tuple[dict[str, Any], ...] = ()
    version_id: str = ""
    content_hash: str = ""
    input_hash: str = ""
    snapshot_hash: str = ""
    shot_plan_hash: str = ""
    approval_event_hash: str = ""
    approval_evidence_hash: str = ""


def build_project_from_hotspot(
    session: Session,
    hotspot_id: int,
    *,
    project_name: str = "",
    selected_topic_id: int | None = None,
    stop_after: str | None = None,
    material_root: Path | None = None,
    project_root: Path | None = None,
    provider: AIProvider | None = None,
    dependencies: PipelineDependencies | None = None,
) -> ProductionPipelineResult:
    hot_item = session.get(HotItem, hotspot_id)
    if hot_item is None:
        raise PipelineHotspotNotFoundError(f"热点不存在：{hotspot_id}")
    if hot_item.is_filtered:
        raise PipelineFilteredHotspotError(
            f"该热点已过滤，不建议创建项目：{hot_item.filter_reason or '规则过滤'}"
        )
    provider = _require_fake_provider(provider)
    pipeline = _create_pipeline(
        session,
        hot_item,
        _safe_project_name(project_name or hot_item.title),
        provider,
    )
    resolved_material_root = resolve_configured_path(
        material_root if material_root is not None else get_settings().material_root_dir
    )
    resolved_project_root = (
        resolve_configured_path(project_root)
        if project_root is not None
        else (
            resolve_configured_path(material_root) / "项目"
            if material_root is not None
            else resolve_configured_path(get_settings().project_root_dir)
        )
    )
    return _execute_pipeline(
        session,
        pipeline,
        selected_topic_id=selected_topic_id,
        stop_after=stop_after,
        material_root=resolved_material_root,
        project_root=resolved_project_root,
        provider=provider,
        dependencies=dependencies or PipelineDependencies(),
        force=False,
    )


def get_pipeline_status(
    session: Session,
    pipeline_id: int,
) -> ProductionPipelineResult:
    pipeline = _get_pipeline(session, pipeline_id)
    return _build_result(pipeline)


def get_pipeline_completion_status(
    session: Session,
    pipeline_id: int,
    *,
    system_drive: str | None = None,
) -> dict[str, Any]:
    """Return the derived completion decision without changing pipeline state.

    The existing pipeline status endpoint is intentionally kept compatible with
    its historical payload.  This narrower read model gives the finished page a
    stable place to display the five-gate release decision while preserving the
    rule that a stored ``ProductionPipeline.status`` value is never treated as
    proof of completion.
    """

    pipeline = _get_pipeline(session, pipeline_id)
    decision = evaluate_pipeline_completion(pipeline, system_drive=system_drive)
    evidence = getattr(pipeline, "completion_evidence", None)
    evidence_present = isinstance(evidence, dict) and bool(evidence)
    if decision.is_complete:
        quality_gate_state = "passed"
    elif evidence_present:
        quality_gate_state = "blocked"
    else:
        quality_gate_state = "not_evaluated"
    payload: dict[str, Any] = {
        "pipeline_id": pipeline.id,
        "status": decision.status,
        "is_complete": decision.is_complete,
        "completion_reason_code": decision.reason_code,
        "message": decision.message,
        "next_action": decision.next_action,
        "next_surface": decision.next_surface,
        "current_step": decision.current_step,
        "needs_confirmation": decision.needs_confirmation,
        "evidence_present": evidence_present,
        "quality_gate_state": quality_gate_state,
    }
    # The completion decision has already revalidated both files, hashes, and
    # project-relative paths.  Returning those verified references lets the
    # finished page reconstruct its approved read model after a process restart
    # without exposing physical D: paths or trusting stale browser state.
    if decision.is_complete and isinstance(evidence, dict):
        for key in ("artifact_relative_path", "quality_report_relative_path"):
            value = evidence.get(key)
            if isinstance(value, str) and value.strip():
                payload[key] = value
    return payload


def continue_pipeline(
    session: Session,
    pipeline_id: int,
    *,
    force: bool = False,
    stop_after: str | None = None,
    material_root: Path | None = None,
    project_root: Path | None = None,
    provider: AIProvider | None = None,
    dependencies: PipelineDependencies | None = None,
) -> ProductionPipelineResult:
    pipeline = _get_pipeline(session, pipeline_id)
    _ensure_pipeline_steps(session, pipeline)
    provider = _require_fake_provider(provider or FakeProvider(model=pipeline.model))
    if pipeline.provider != "fake":
        raise FakeProviderRequiredError("本轮只允许 fake provider，已阻止真实 AI 调用。")
    if force:
        _reset_all_steps(session, pipeline)
    else:
        _invalidate_stale_downstream_steps(session, pipeline)
    resolved_material_root = resolve_configured_path(
        material_root if material_root is not None else get_settings().material_root_dir
    )
    resolved_project_root = (
        resolve_configured_path(project_root)
        if project_root is not None
        else (
            resolve_configured_path(material_root) / "项目"
            if material_root is not None
            else resolve_configured_path(get_settings().project_root_dir)
        )
    )
    return _execute_pipeline(
        session,
        pipeline,
        selected_topic_id=pipeline.topic_idea_id,
        stop_after=stop_after,
        material_root=resolved_material_root,
        project_root=resolved_project_root,
        provider=provider,
        dependencies=dependencies or PipelineDependencies(),
        force=force,
    )


def serialize_pipeline_result(result: ProductionPipelineResult) -> dict:
    return {
        "pipeline_id": result.pipeline_id,
        "hotspot_id": result.hotspot_id,
        "topic_idea_id": result.topic_idea_id,
        "script_asset_id": result.script_asset_id,
        "project_name": result.project_name,
        "project_key": result.project_key,
        "project_dir": result.project_dir,
        "legacy_project_dir": result.legacy_project_dir,
        "status": result.status,
        "completion_reason_code": result.completion_reason_code,
        "next_surface": result.next_surface,
        "current_step": result.current_step,
        "provider": result.provider,
        "model": result.model,
        "steps": [
            {
                "step_name": step.name,
                "name": step.name,
                "status": step.status,
                "started_at": _isoformat(step.started_at),
                "finished_at": _isoformat(step.finished_at),
                "error_message": step.error_message,
                "error": step.error_message,
                "output_id": step.output_id,
                "output_path": step.output_path,
                "output_summary": step.output_summary,
            }
            for step in result.steps
        ],
        "material_tasks": result.material_tasks,
        "graphic_tasks": result.graphic_tasks,
        "exports": result.exports,
        "generated_files": result.generated_files,
        "failure_reasons": result.failure_reasons,
        "needs_confirmation": result.needs_confirmation,
        "next_suggestion": result.next_suggestion,
    }


def _execute_pipeline(
    session: Session,
    pipeline: ProductionPipeline,
    *,
    selected_topic_id: int | None,
    stop_after: str | None,
    material_root: Path,
    project_root: Path,
    provider: AIProvider,
    dependencies: PipelineDependencies,
    force: bool,
) -> ProductionPipelineResult:
    _ensure_pipeline_steps(session, pipeline)
    pipeline.status = "进行中"
    session.add(pipeline)
    session.commit()

    steps = {step.name: step for step in pipeline.steps}
    hot_item = session.get(HotItem, pipeline.hotspot_id)
    if hot_item is None:
        raise PipelineHotspotNotFoundError(f"热点不存在：{pipeline.hotspot_id}")

    candidate_ideas = [] if force else _load_candidate_ideas(session, pipeline, steps)
    topic_idea = (
        None
        if force
        else session.get(TopicIdea, pipeline.topic_idea_id)
        if pipeline.topic_idea_id
        else None
    )
    script_asset = (
        None
        if force
        else session.get(ScriptAsset, pipeline.script_asset_id)
        if pipeline.script_asset_id
        else None
    )
    material_plan = None if force else _load_material_plan(steps)
    project_paths = _load_project_paths(
        pipeline,
        material_root=material_root,
        project_root=project_root,
    )

    step = steps["生成候选选题"]
    if _should_run(step, force):
        try:
            _start_step(session, step)
            topic_result = dependencies.topic_generator(
                session,
                pipeline.hotspot_id,
                provider=provider,
            )
            candidate_ideas = topic_result.ideas
            _complete_step(
                session,
                step,
                {
                    "candidate_count": len(candidate_ideas),
                    "candidate_ids": [idea.id for idea in candidate_ideas],
                },
            )
        except Exception as exc:
            candidate_ideas = []
            _fail_step(session, step, exc)
    if _pause_requested(session, pipeline, "生成候选选题", stop_after):
        return _build_result(pipeline)

    step = steps["选择候选选题"]
    if _should_run(step, force):
        if not candidate_ideas:
            _needs_confirmation(session, step, "候选选题尚未生成")
        else:
            try:
                _start_step(session, step)
                topic_idea = _select_topic(session, candidate_ideas, selected_topic_id)
                pipeline.topic_idea_id = topic_idea.id
                session.add(pipeline)
                _complete_step(session, step, {"topic_idea_id": topic_idea.id})
            except Exception as exc:
                topic_idea = None
                pipeline.topic_idea_id = None
                session.add(pipeline)
                session.commit()
                _fail_step(session, step, exc)
    else:
        topic_idea = (
            session.get(TopicIdea, pipeline.topic_idea_id)
            if pipeline.topic_idea_id
            else topic_idea
        )
    if _pause_requested(session, pipeline, "选择候选选题", stop_after):
        return _build_result(pipeline)

    step = steps["生成脚本素材"]
    if _should_run(step, force):
        if topic_idea is None:
            _needs_confirmation(session, step, "尚未选择候选选题")
        else:
            try:
                _start_step(session, step)
                script_result: ScriptGenerationResult = dependencies.script_generator(
                    session,
                    topic_idea,
                    provider,
                )
                script_asset = script_result.script_asset
                pipeline.script_asset_id = script_asset.id
                session.add(pipeline)
                _complete_step(
                    session,
                    step,
                    {
                        "script_asset_id": script_asset.id,
                        "from_cache": script_result.from_cache,
                    },
                )
            except Exception as exc:
                script_asset = None
                pipeline.script_asset_id = None
                session.add(pipeline)
                session.commit()
                _fail_step(session, step, exc)
    else:
        script_asset = (
            session.get(ScriptAsset, pipeline.script_asset_id)
            if pipeline.script_asset_id
            else script_asset
        )
    if _pause_requested(session, pipeline, "生成脚本素材", stop_after):
        return _build_result(pipeline)

    step = steps["生成素材任务"]
    if _should_run(step, force):
        if script_asset is None:
            _needs_confirmation(session, step, "脚本素材尚未生成")
        else:
            try:
                expected_material_payload = copy.deepcopy(step.output_summary or {})
                prior_selection_history = material_selection_history(
                    expected_material_payload
                    if isinstance(expected_material_payload, dict)
                    else {}
                )
                _start_step(session, step)
                shot_plan_binding = _load_approved_shot_plan(
                    pipeline,
                    project_root=project_root,
                )
                approved_shot_plan = [dict(item) for item in shot_plan_binding.shot_plan]
                material_plan = dependencies.material_planner(
                    _script_to_shots(script_asset, shot_plan=approved_shot_plan),
                    project_name=pipeline.project_name,
                )
                use_avatar = script_use_avatar(script_asset)
                material_task_ids: set[str] = set()
                for task_index, task in enumerate(material_plan.material_tasks):
                    _attach_hotspot_source_to_material_task(task, hot_item)
                    if not task.material_task_id:
                        task.material_task_id = material_task_id_for_shot(task.shot_id)
                    task.material_task_id = validate_material_task_id(
                        task.material_task_id
                    )
                    normalized_task_id = task.material_task_id.casefold()
                    if normalized_task_id in material_task_ids:
                        raise ValueError("素材任务 material_task_id 必须在同一步骤内唯一。")
                    material_task_ids.add(normalized_task_id)
                    task.status = "待执行" if task_index == 0 else "等待"
                    task.status_history = [task.status]
                    task.avatar_required = use_avatar
                material_output = {
                    "project_name": pipeline.project_name,
                    "project_key": pipeline.project_key,
                    "script_version_id": shot_plan_binding.version_id,
                    "script_version_hash": shot_plan_binding.content_hash,
                    "script_version_input_hash": shot_plan_binding.input_hash,
                    "script_snapshot_hash": shot_plan_binding.snapshot_hash,
                    "shot_plan_hash": shot_plan_binding.shot_plan_hash,
                    "approval_event_hash": shot_plan_binding.approval_event_hash,
                    "approval_evidence_hash": shot_plan_binding.approval_evidence_hash,
                    "material_input_sha256": material_input_sha256_for_script(script_asset),
                    "shot_plan_count": len(approved_shot_plan),
                    "shot_plan": approved_shot_plan,
                    "material_task_count": len(material_plan.material_tasks),
                    "material_tasks": [
                        asdict(task) for task in material_plan.material_tasks
                    ],
                    "graphic_task_candidates": [
                        asdict(task) for task in material_plan.graphic_tasks
                    ],
                }
                if prior_selection_history:
                    material_output["material_selection_history"] = prior_selection_history
                _complete_material_step_with_cas(
                    session,
                    step,
                    expected_payload=expected_material_payload,
                    output_summary=material_output,
                )
            except (PipelineStepConflictError, PipelineStepCommitUnknownError):
                material_plan = None
                raise
            except Exception as exc:
                material_plan = None
                _fail_step(session, step, exc)
    else:
        if script_asset is None:
            _fail_step(session, step, ValueError("脚本素材尚未生成"))
            return _build_result(pipeline)
        try:
            shot_plan_binding = _load_approved_shot_plan(
                pipeline,
                project_root=project_root,
            )
            expected_material_payload = copy.deepcopy(step.output_summary or {})
            if shot_plan_binding.version_id:
                current_material_hash = material_input_sha256_for_script(script_asset)
                if (
                    expected_material_payload.get("material_input_sha256")
                    != current_material_hash
                ):
                    raise ScriptVersionError(
                        "已完成素材计划绑定的脚本输入已变化，必须重新生成素材任务"
                    )
                stored_shot_plan = expected_material_payload.get("shot_plan")
                if stored_shot_plan != [dict(item) for item in shot_plan_binding.shot_plan]:
                    raise ScriptVersionError(
                        "已完成素材计划与当前批准 ShotPlan 不一致，必须重新生成素材任务"
                    )
                rebound_payload = copy.deepcopy(expected_material_payload)
                rebound_payload.update(
                    {
                        "script_version_id": shot_plan_binding.version_id,
                        "script_version_hash": shot_plan_binding.content_hash,
                        "script_version_input_hash": shot_plan_binding.input_hash,
                        "script_snapshot_hash": shot_plan_binding.snapshot_hash,
                        "shot_plan_hash": shot_plan_binding.shot_plan_hash,
                        "approval_event_hash": shot_plan_binding.approval_event_hash,
                        "approval_evidence_hash": shot_plan_binding.approval_evidence_hash,
                    }
                )
                if rebound_payload != expected_material_payload:
                    _rebind_completed_material_step_with_cas(
                        session,
                        step,
                        expected_payload=expected_material_payload,
                        output_summary=rebound_payload,
                    )
            material_plan = _load_material_plan(steps)
        except Exception as exc:
            material_plan = None
            _fail_step(session, step, exc)
            return _build_result(pipeline)
    if _pause_requested(session, pipeline, "生成素材任务", stop_after):
        return _build_result(pipeline)

    step = steps["生成 Codex 图文任务"]
    if _should_run(step, force):
        if material_plan is None:
            _needs_confirmation(session, step, "素材任务尚未生成")
        else:
            _start_step(session, step)
            _complete_step(
                session,
                step,
                {
                    "graphic_task_count": len(material_plan.graphic_tasks),
                    "graphic_tasks": [
                        asdict(task) for task in material_plan.graphic_tasks
                    ],
                    "codex_skill_executed": False,
                },
            )
    if _pause_requested(session, pipeline, "生成 Codex 图文任务", stop_after):
        return _build_result(pipeline)

    step = steps["生成数字人字段"]
    if _should_run(step, force):
        if material_plan is None:
            _needs_confirmation(session, step, "素材任务尚未生成")
        else:
            _start_step(session, step)
            use_avatar = script_use_avatar(script_asset)
            avatar_tasks = [
                {
                    "shot_id": task.shot_id,
                    "required": bool(use_avatar and task.avatar_required),
                    "enabled": use_avatar,
                    "position": task.avatar_position,
                    "state": task.avatar_state,
                    "avoid_subtitle": task.avatar_avoid_subtitle,
                    "avoid_chart": task.avatar_avoid_chart,
                    "lip_sync": task.avatar_lip_sync,
                    "material_path": task.avatar_material_path,
                    "generated": False,
                    "disabled_reason": "" if use_avatar else "本期选择不添加数字人",
                }
                for task in material_plan.material_tasks
            ]
            _complete_step(
                session,
                step,
                {
                    "avatar_task_count": len(avatar_tasks),
                    "avatar_enabled": use_avatar,
                    "avatar_tasks": avatar_tasks,
                    "avatar_generation_executed": False,
                },
            )
    if _pause_requested(session, pipeline, "生成数字人字段", stop_after):
        return _build_result(pipeline)

    step = steps["创建中文项目目录"]
    if _should_run(step, force):
        try:
            _start_step(session, step)
            prepared_dir = prepare_pipeline_project_directory(
                pipeline,
                project_root=project_root,
            )
            project_paths = ProjectMaterialPaths(
                root=material_root,
                project_name=pipeline.project_name,
                project_key=pipeline.project_key,
                project_root_dir=project_root,
            )
            if project_paths.project_dir != prepared_dir:
                raise RuntimeError("项目身份目录解析结果不一致。")
            directory_use = begin_prepared_project_directory_use(
                pipeline,
                project_root=project_root,
            )
            project_paths.ensure_shared_directories()
            child_uses = [
                begin_prepared_project_child_directory_use(
                    pipeline,
                    project_root=project_root,
                    relative_path=dirname,
                    create=True,
                )
                for dirname in PROJECT_DIRS
            ]
            project_paths.ensure_directories()
            for child_use in child_uses:
                child_use.verify()
            directory_use.verify()
            session.add(pipeline)
            quality_contract = ensure_default_quality_contract(
                session,
                pipeline.id,
                project_root=project_root,
            )
            _complete_step(
                session,
                step,
                {
                    "project_dir": pipeline.project_dir,
                    "output_path": pipeline.project_dir,
                    "quality_contract_relative_path": quality_contract.relative_path,
                    "quality_contract_sha256": quality_contract.sha256,
                },
            )
        except Exception as exc:
            project_paths = None
            _fail_step(session, step, exc)
    else:
        project_paths = _load_project_paths(
            pipeline,
            material_root=material_root,
            project_root=project_root,
        )
    if _pause_requested(session, pipeline, "创建中文项目目录", stop_after):
        return _build_result(pipeline)

    step = steps["生成剪辑清单"]
    if _should_run(step, force):
        if project_paths is None:
            _needs_confirmation(session, step, "中文项目目录尚未创建")
        else:
            try:
                _start_step(session, step)
                package = dependencies.editing_exporter(
                    session,
                    pipeline.id,
                    project_root=project_root,
                )
                _complete_step(
                    session,
                    step,
                    {
                        "status": package.status,
                        "timeline_segment_count": len(package.timeline),
                        "version_count": len(package.versions),
                        "output_path": package.generated_files.get(
                            "timeline_json",
                            "",
                        ),
                        "generated_files": package.generated_files,
                        "export_task": package.export_task,
                    },
                )
            except Exception as exc:
                _fail_step(session, step, exc)
    if _pause_requested(session, pipeline, "生成剪辑清单", stop_after):
        return _build_result(pipeline)

    step = steps["导出 script.md"]
    if _should_run(step, force):
        if project_paths is None:
            _needs_confirmation(session, step, "中文项目目录尚未创建")
        else:
            try:
                _start_step(session, step)
                directory_use = begin_prepared_project_directory_use(
                    pipeline,
                    project_root=project_root,
                )
                info_use = begin_prepared_project_child_directory_use(
                    pipeline,
                    project_root=project_root,
                    relative_path="素材信息",
                    create=False,
                )
                expected_path = prepared_project_output_path(info_use, "script.md")
                path = dependencies.script_exporter(
                    project_paths.project_dir,
                    topic_idea,
                    script_asset,
                    material_plan.material_tasks if material_plan else [],
                )
                if Path(path) != expected_path:
                    raise RuntimeError("script.md 导出路径越过项目身份目录。")
                prepared_project_output_path(info_use, "script.md", must_exist=True)
                info_use.verify()
                directory_use.verify()
                _complete_step(
                    session,
                    step,
                    {"output_path": str(path), "file_type": "script_md"},
                )
            except Exception as exc:
                _fail_step(session, step, exc)
    if _pause_requested(session, pipeline, "导出 script.md", stop_after):
        return _build_result(pipeline)

    step = steps["导出 shotlist.csv"]
    if _should_run(step, force):
        if project_paths is None:
            _needs_confirmation(session, step, "中文项目目录尚未创建")
        else:
            try:
                _start_step(session, step)
                directory_use = begin_prepared_project_directory_use(
                    pipeline,
                    project_root=project_root,
                )
                info_use = begin_prepared_project_child_directory_use(
                    pipeline,
                    project_root=project_root,
                    relative_path="素材信息",
                    create=False,
                )
                expected_path = prepared_project_output_path(info_use, "shotlist.csv")
                path = dependencies.shotlist_exporter(
                    project_paths.project_dir,
                    material_plan.material_tasks if material_plan else [],
                )
                if Path(path) != expected_path:
                    raise RuntimeError("shotlist.csv 导出路径越过项目身份目录。")
                prepared_project_output_path(info_use, "shotlist.csv", must_exist=True)
                info_use.verify()
                directory_use.verify()
                _complete_step(
                    session,
                    step,
                    {"output_path": str(path), "file_type": "shotlist_csv"},
                )
            except Exception as exc:
                _fail_step(session, step, exc)
    if _pause_requested(session, pipeline, "导出 shotlist.csv", stop_after):
        return _build_result(pipeline)

    step = steps["导出 edit_manifest.json"]
    if _should_run(step, force):
        if project_paths is None:
            _needs_confirmation(session, step, "中文项目目录尚未创建")
        else:
            try:
                _start_step(session, step)
                directory_use = begin_prepared_project_directory_use(
                    pipeline,
                    project_root=project_root,
                )
                info_use = begin_prepared_project_child_directory_use(
                    pipeline,
                    project_root=project_root,
                    relative_path="素材信息",
                    create=False,
                )
                expected_path = prepared_project_output_path(
                    info_use,
                    "edit_manifest.json",
                )
                path = dependencies.manifest_exporter(
                    project_paths.project_dir,
                    pipeline_id=pipeline.id,
                    provider=provider.name,
                    model=provider.model,
                    topic_idea=topic_idea,
                    script_asset=script_asset,
                    material_tasks=material_plan.material_tasks if material_plan else [],
                    graphic_tasks=material_plan.graphic_tasks if material_plan else [],
                )
                if Path(path) != expected_path:
                    raise RuntimeError("edit_manifest.json 导出路径越过项目身份目录。")
                prepared_project_output_path(
                    info_use,
                    "edit_manifest.json",
                    must_exist=True,
                )
                info_use.verify()
                directory_use.verify()
                _complete_step(
                    session,
                    step,
                    {"output_path": str(path), "file_type": "edit_manifest_json"},
                )
            except Exception as exc:
                _fail_step(session, step, exc)

    _finalize_pipeline(session, pipeline)
    return _build_result(pipeline)


def _create_pipeline(
    session: Session,
    hot_item: HotItem,
    project_name: str,
    provider: AIProvider,
) -> ProductionPipeline:
    pipeline = ProductionPipeline(
        hotspot_id=hot_item.id,
        project_name=project_name,
        status="进行中",
        provider=provider.name,
        model=provider.model,
    )
    pipeline.steps = [
        PipelineStep(name=name, step_order=index, status="未开始")
        for index, name in enumerate(PIPELINE_STEP_NAMES, start=1)
    ]
    session.add(pipeline)
    session.commit()
    session.refresh(pipeline)
    return pipeline


def _get_pipeline(session: Session, pipeline_id: int) -> ProductionPipeline:
    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None:
        raise PipelineNotFoundError(f"流程不存在：{pipeline_id}")
    return pipeline


def _ensure_pipeline_steps(session: Session, pipeline: ProductionPipeline) -> None:
    legacy_names = {
        "候选选题": "生成候选选题",
        "脚本素材": "生成脚本素材",
        "素材任务": "生成素材任务",
        "Codex图文任务": "生成 Codex 图文任务",
        "数字人字段": "生成数字人字段",
        "项目目录": "创建中文项目目录",
    }
    for step in list(pipeline.steps):
        if step.name in legacy_names:
            step.name = legacy_names[step.name]
            session.add(step)

    old_export = next((step for step in pipeline.steps if step.name == "文件导出"), None)
    existing = {step.name: step for step in pipeline.steps if step.name != "文件导出"}
    if old_export is not None:
        session.delete(old_export)

    for order, name in enumerate(PIPELINE_STEP_NAMES, start=1):
        step = existing.get(name)
        if step is None:
            step = PipelineStep(
                pipeline_id=pipeline.id,
                name=name,
                step_order=order,
                status="未开始",
            )
            if name == "选择候选选题" and pipeline.topic_idea_id:
                step.status = "已完成"
                step.output_summary = {"topic_idea_id": pipeline.topic_idea_id}
            if old_export is not None and name.startswith("导出 "):
                key = {
                    "导出 script.md": "script_md",
                    "导出 shotlist.csv": "shotlist_csv",
                    "导出 edit_manifest.json": "edit_manifest_json",
                }[name]
                path = old_export.output_summary.get(key, "")
                step.status = old_export.status if path else "未开始"
                step.error = old_export.error if not path else ""
                step.started_at = old_export.started_at
                step.ended_at = old_export.ended_at
                step.output_summary = (
                    {"output_path": path, "file_type": key} if path else {}
                )
            session.add(step)
        else:
            step.step_order = order
            session.add(step)
    session.commit()
    session.refresh(pipeline)


def _require_fake_provider(provider: AIProvider | None) -> AIProvider:
    provider = provider or FakeProvider()
    if provider.name != "fake":
        raise FakeProviderRequiredError("本轮只允许 fake provider，已阻止真实 AI 调用。")
    return provider


def _select_topic(
    session: Session,
    ideas: list[TopicIdea],
    selected_topic_id: int | None,
) -> TopicIdea:
    if not ideas:
        raise ValueError("没有可用的候选选题。")
    selected = next(
        (idea for idea in ideas if selected_topic_id is not None and idea.id == selected_topic_id),
        None,
    )
    if selected_topic_id is not None and selected is None:
        existing = session.get(TopicIdea, selected_topic_id)
        if existing is None or existing.hot_item_id != ideas[0].hot_item_id:
            raise ValueError(f"指定候选选题不存在：{selected_topic_id}")
        selected = existing
    selected = selected or ideas[0]
    hotspot_ideas = (
        session.query(TopicIdea)
        .filter(TopicIdea.hot_item_id == selected.hot_item_id)
        .all()
    )
    for idea in hotspot_ideas:
        idea.is_selected = idea.id == selected.id
        session.add(idea)
    session.commit()
    session.refresh(selected)
    return selected


def _load_candidate_ideas(
    session: Session,
    pipeline: ProductionPipeline,
    steps: dict[str, PipelineStep],
) -> list[TopicIdea]:
    ids = steps["生成候选选题"].output_summary.get("candidate_ids", [])
    ideas = [session.get(TopicIdea, idea_id) for idea_id in ids]
    valid = [idea for idea in ideas if idea is not None]
    if valid:
        return valid
    return (
        session.query(TopicIdea)
        .filter(TopicIdea.hot_item_id == pipeline.hotspot_id)
        .order_by(TopicIdea.id)
        .all()
    )


def _load_material_plan(steps: dict[str, PipelineStep]) -> MaterialPlan | None:
    step = steps.get("生成素材任务")
    payload = step.output_summary if step is not None else {}
    if not isinstance(payload, dict):
        return None
    material_payloads = payload.get("material_tasks", [])
    graphic_payloads = payload.get("graphic_task_candidates", [])
    if not isinstance(material_payloads, list) or not material_payloads:
        return None
    material_tasks = []
    for item in material_payloads:
        if not isinstance(item, dict):
            continue
        try:
            material_tasks.append(MaterialTask(**item))
        except (TypeError, ValueError):
            continue
    graphic_tasks = []
    if isinstance(graphic_payloads, list):
        for item in graphic_payloads:
            if not isinstance(item, dict):
                continue
            try:
                graphic_tasks.append(GraphicTask(**item))
            except (TypeError, ValueError):
                continue
    if not material_tasks:
        return None
    return MaterialPlan(
        project_name="",
        material_tasks=material_tasks,
        graphic_tasks=graphic_tasks,
    )


def _attach_hotspot_source_to_material_task(task: MaterialTask, hot_item: HotItem) -> None:
    if task.needs_official_capture and not task.official_source_url:
        task.official_source_url = hot_item.url or ""
    if not task.official_source_name:
        task.official_source_name = hot_item.source_platform or "官方页面"
    if not task.official_page_title:
        task.official_page_title = hot_item.title or task.recommended_visual or "官方页面"


def _load_project_paths(
    pipeline: ProductionPipeline,
    *,
    material_root: Path,
    project_root: Path,
) -> ProjectMaterialPaths | None:
    if not pipeline.project_dir:
        return None
    resolved = resolve_prepared_project_directory(
        pipeline,
        project_root=project_root,
    )
    paths = ProjectMaterialPaths(
        root=material_root,
        project_name=pipeline.project_name,
        project_key=pipeline.project_key,
        project_root_dir=project_root,
    )
    if paths.project_dir != resolved:
        raise RuntimeError("项目身份目录解析结果不一致。")
    return paths


def _load_approved_shot_plan(
    pipeline: ProductionPipeline,
    *,
    project_root: Path,
) -> _ApprovedShotPlanBinding:
    """Read the approved immutable ShotPlan at the material boundary."""

    # Legacy/generated scripts have no prepared project directory yet.  They
    # intentionally continue through the existing storyboard path.
    if not str(getattr(pipeline, "project_dir", "") or "").strip():
        return _ApprovedShotPlanBinding()
    ledger = ScriptVersionLedger(pipeline, project_root=project_root)
    snapshots = ledger.list()
    if not snapshots:
        return _ApprovedShotPlanBinding()
    current_state = _pipeline_current_script_state(pipeline)
    current_version_id = str(current_state.get("script_version_id") or "").strip()
    if current_version_id:
        snapshot = next(
            (item for item in snapshots if item.version_id == current_version_id),
            None,
        )
        if snapshot is None:
            raise ScriptVersionError("当前脚本引用的版本不存在，素材阶段已阻断")
        if snapshot.status != "approved":
            raise ScriptVersionError("当前脚本版本尚未批准，不能进入素材阶段")
    else:
        snapshot = next(
            (item for item in reversed(snapshots) if item.status == "approved"),
            None,
        )
        if snapshot is None:
            raise ScriptVersionError("脚本已有版本但没有可用批准，不能进入素材阶段")
    if snapshot.integrity_status != "full_envelope_verified":
        raise ScriptVersionError(
            "旧版脚本没有 ShotPlan 完整哈希，必须重新导入并批准后才能进入素材阶段"
        )
    if snapshot.approval_integrity_status != "snapshot_hash_bound":
        raise ScriptVersionError(
            "脚本批准没有绑定完整快照哈希，必须重新批准后才能进入素材阶段"
        )
    if snapshot.source == "script_factory_import" and not snapshot.approval_evidence_hash:
        raise ScriptVersionError(
            "脚本批准没有绑定质量与结果证据，必须重新验收后才能进入素材阶段"
        )
    if current_version_id:
        result_hash = str((snapshot.input_evidence or {}).get("result_sha256") or "")
        anchors = {
            "script_version_hash": snapshot.content_hash,
            "script_version_input_hash": snapshot.input_hash,
            "script_version_snapshot_hash": snapshot.snapshot_hash,
            "script_version_shot_plan_hash": snapshot.shot_plan_hash,
            "script_result_hash": result_hash,
            "approval_event_hash": snapshot.approval_event_hash,
            "approval_evidence_hash": snapshot.approval_evidence_hash,
        }
        for label, expected in anchors.items():
            if not expected or str(current_state.get(label) or "") != expected:
                raise ScriptVersionError(
                    f"当前脚本状态的 {label} 与批准快照不一致，素材阶段已阻断"
                )
        quality_hash = str(current_state.get("approval_quality_hash") or "")
        if len(quality_hash) != 64:
            raise ScriptVersionError("当前脚本状态缺少批准质量证据哈希，素材阶段已阻断")
        expected_approval_evidence_hash = _canonical_sha256(
            {
                "script_result_hash": result_hash,
                "quality_check_hash": quality_hash,
            }
        )
        if expected_approval_evidence_hash != snapshot.approval_evidence_hash:
            raise ScriptVersionError("批准质量证据与批准事件哈希不一致，素材阶段已阻断")
    return _ApprovedShotPlanBinding(
        shot_plan=tuple(dict(item) for item in snapshot.shot_plan),
        version_id=snapshot.version_id,
        content_hash=snapshot.content_hash,
        input_hash=snapshot.input_hash,
        snapshot_hash=snapshot.snapshot_hash,
        shot_plan_hash=snapshot.shot_plan_hash,
        approval_event_hash=snapshot.approval_event_hash,
        approval_evidence_hash=snapshot.approval_evidence_hash,
    )


def _pipeline_current_script_state(pipeline: ProductionPipeline) -> dict[str, Any]:
    for step in getattr(pipeline, "steps", []) or []:
        if getattr(step, "name", "") != "生成脚本素材":
            continue
        output = step.output_summary if isinstance(step.output_summary, dict) else {}
        state = output.get("script_factory") or output.get("script_work_order") or {}
        if isinstance(state, Mapping):
            return dict(state)
    return {}


def _shot_plan_item_to_segment(item: Mapping[str, Any]) -> ShotSegment:
    refs = [dict(ref) for ref in item.get("source_refs", []) if isinstance(ref, Mapping)]
    first_url = next(
        (
            str(ref.get("url") or "").strip()
            for ref in refs
            if str(ref.get("url") or "").strip()
        ),
        "",
    )
    material_requirements = _dict_value(item.get("material_requirements"))
    if first_url and not material_requirements.get("official_source_url"):
        material_requirements["official_source_url"] = first_url
    visual_intent = str(item.get("visual_intent") or "").strip()
    visual_type = str(item.get("visual_type") or "").strip()
    return ShotSegment(
        shot_id=str(item.get("shot_id") or "").strip(),
        beat_id=str(item.get("beat_id") or "").strip(),
        beat_type=str(item.get("beat_type") or "").strip(),
        start=str(item.get("start") or ""),
        end=str(item.get("end") or ""),
        voiceover=str(item.get("voiceover") or ""),
        recommended_visual=visual_intent or visual_type,
        source_priority=_string_list(item.get("source_priority")),
        asset_keywords=_string_list(item.get("asset_keywords")),
        material_requirements=material_requirements,
        backup_visual=str(item.get("backup_visual") or ""),
        failure_fallback=str(item.get("failure_fallback") or ""),
        official_source_url=first_url,
        matching_reason=str(item.get("viewer_takeaway") or ""),
        viewer_takeaway=str(item.get("viewer_takeaway") or ""),
        visual_intent=visual_intent,
        evidence_kind=str(item.get("evidence_kind") or ""),
        source_refs=[dict(ref) for ref in refs],
        acceptance_conditions=_string_list(item.get("acceptance_conditions")),
        forbidden_items=_string_list(item.get("forbidden_items")),
        director_version=str(item.get("director_version") or ""),
        subject=str(item.get("subject") or ""),
        action=str(item.get("action") or ""),
        location=str(item.get("location") or ""),
        time_context=str(item.get("time_context") or ""),
        composition=str(item.get("composition") or ""),
        visual_type=visual_type,
        sound_notes=str(item.get("sound_notes") or ""),
    )


def _script_to_shots(
    script_asset: ScriptAsset,
    *,
    shot_plan: Sequence[Mapping[str, Any]] | None = None,
) -> list[ShotSegment]:
    if shot_plan:
        shots = [
            _shot_plan_item_to_segment(item)
            for item in shot_plan
            if isinstance(item, Mapping)
        ]
        if not shots:
            raise ValueError("ShotPlan 缺少可用分镜。")
        return shots
    shots = [
        ShotSegment(
            start=str(item.get("start", "")),
            end=str(item.get("end", "")),
            voiceover=str(item.get("voiceover", "")),
            recommended_visual=str(item.get("recommended_visual", "")),
            source_priority=_string_list(item.get("source_priority")),
            asset_keywords=_string_list(item.get("asset_keywords")),
            material_requirements=_dict_value(item.get("material_requirements")),
            backup_visual=str(item.get("backup_visual", "")),
            failure_fallback=str(item.get("failure_fallback", "")),
            official_source_url=str(item.get("official_source_url", "")),
            matching_reason=str(item.get("matching_reason", "")),
        )
        for item in script_asset.storyboard
        if isinstance(item, dict)
    ]
    if not shots:
        raise ValueError("脚本缺少可用分镜。")
    return shots


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _should_run(step: PipelineStep, force: bool) -> bool:
    return force or step.status != "已完成"


def _start_step(session: Session, step: PipelineStep) -> None:
    step.status = "进行中"
    step.started_at = datetime.now(timezone.utc)
    step.ended_at = None
    step.error = ""
    session.add(step)
    session.commit()


def _complete_step(
    session: Session,
    step: PipelineStep,
    output_summary: dict,
) -> None:
    step.status = "已完成"
    step.output_summary = output_summary
    step.error = ""
    step.ended_at = datetime.now(timezone.utc)
    session.add(step)
    session.commit()


def _complete_material_step_with_cas(
    session: Session,
    step: PipelineStep,
    *,
    expected_payload: dict[str, Any],
    output_summary: dict[str, Any],
) -> None:
    """Commit a completed material plan only if its upstream snapshot survived."""

    with session.no_autoflush:
        result = session.execute(
            update(PipelineStep)
            .where(
                PipelineStep.id == step.id,
                PipelineStep.pipeline_id == step.pipeline_id,
                PipelineStep.output_summary == expected_payload,
                PipelineStep.status == "进行中",
            )
            .values(
                status="已完成",
                output_summary=copy.deepcopy(output_summary),
                error="",
                ended_at=datetime.now(timezone.utc),
            )
            .execution_options(synchronize_session=False)
        )
    if result.rowcount == 0:
        session.rollback()
        raise PipelineStepConflictError(
            "素材规划期间上游或候选状态已经变化，旧规划未提交。"
        )
    if result.rowcount != 1:
        session.rollback()
        raise PipelineStepCommitUnknownError(
            "素材规划提交结果未知，请刷新项目后核对。"
        )
    try:
        session.commit()
    except BaseException as exc:
        try:
            session.rollback()
        except BaseException:
            pass
        raise PipelineStepCommitUnknownError(
            "素材规划提交结果未知，请刷新项目后核对。"
        ) from exc
    session.expire(step)


def _rebind_completed_material_step_with_cas(
    session: Session,
    step: PipelineStep,
    *,
    expected_payload: dict[str, Any],
    output_summary: dict[str, Any],
) -> None:
    """Atomically rebind an unchanged material plan to a new approved snapshot."""

    with session.no_autoflush:
        result = session.execute(
            update(PipelineStep)
            .where(
                PipelineStep.id == step.id,
                PipelineStep.pipeline_id == step.pipeline_id,
                PipelineStep.output_summary == expected_payload,
                PipelineStep.status == "已完成",
            )
            .values(
                output_summary=copy.deepcopy(output_summary),
                error="",
                ended_at=datetime.now(timezone.utc),
            )
            .execution_options(synchronize_session=False)
        )
    if result.rowcount == 0:
        session.rollback()
        raise PipelineStepConflictError(
            "素材计划重绑定期间候选或上游状态已经变化，旧绑定未提交。"
        )
    if result.rowcount != 1:
        session.rollback()
        raise PipelineStepCommitUnknownError(
            "素材计划重绑定提交结果未知，请刷新项目后核对。"
        )
    try:
        session.commit()
    except BaseException as exc:
        try:
            session.rollback()
        except BaseException:
            pass
        raise PipelineStepCommitUnknownError(
            "素材计划重绑定提交结果未知，请刷新项目后核对。"
        ) from exc
    session.expire(step)


def _fail_step(session: Session, step: PipelineStep, exc: Exception) -> None:
    step.status = "失败"
    step.error = str(exc)
    step.ended_at = datetime.now(timezone.utc)
    session.add(step)
    session.commit()


def _needs_confirmation(session: Session, step: PipelineStep, reason: str) -> None:
    step.status = "需要人工确认"
    step.error = reason
    step.ended_at = datetime.now(timezone.utc)
    session.add(step)
    session.commit()


def _pause_requested(
    session: Session,
    pipeline: ProductionPipeline,
    current_step: str,
    stop_after: str | None,
) -> bool:
    if stop_after != current_step:
        return False
    pipeline.status = "需要人工确认"
    session.add(pipeline)
    session.commit()
    return True


def _reset_all_steps(session: Session, pipeline: ProductionPipeline) -> None:
    pipeline.topic_idea_id = None
    pipeline.script_asset_id = None
    for step in pipeline.steps:
        _reset_step(step)
        session.add(step)
    session.add(pipeline)
    session.commit()


def _invalidate_stale_downstream_steps(
    session: Session,
    pipeline: ProductionPipeline,
) -> None:
    step_map = {step.name: step for step in pipeline.steps}
    first_problem = next(
        (
            name
            for name in PIPELINE_STEP_NAMES
            if step_map[name].status in {"失败", "需要人工确认"}
        ),
        None,
    )
    if first_problem is None:
        return
    dependent_steps = {
        "生成候选选题": PIPELINE_STEP_NAMES[1:],
        "选择候选选题": PIPELINE_STEP_NAMES[2:],
        "生成脚本素材": [
            "生成素材任务",
            "生成 Codex 图文任务",
            "生成数字人字段",
            "生成剪辑清单",
            "导出 script.md",
            "导出 shotlist.csv",
            "导出 edit_manifest.json",
        ],
        "生成素材任务": [
            "生成 Codex 图文任务",
            "生成数字人字段",
            "生成剪辑清单",
            "导出 script.md",
            "导出 shotlist.csv",
            "导出 edit_manifest.json",
        ],
        "生成 Codex 图文任务": ["生成剪辑清单", "导出 edit_manifest.json"],
        "生成数字人字段": [
            "生成剪辑清单",
            "导出 script.md",
            "导出 edit_manifest.json",
        ],
        "创建中文项目目录": [
            "生成剪辑清单",
            "导出 script.md",
            "导出 shotlist.csv",
            "导出 edit_manifest.json",
        ],
        "生成剪辑清单": [],
        "导出 script.md": [],
        "导出 shotlist.csv": [],
        "导出 edit_manifest.json": [],
    }
    for name in dependent_steps[first_problem]:
        if step_map[name].status == "已完成":
            _reset_step(step_map[name])
            session.add(step_map[name])
    session.commit()


def _reset_step(step: PipelineStep) -> None:
    step.status = "未开始"
    step.error = ""
    step.output_summary = {}
    step.started_at = None
    step.ended_at = None


def _finalize_pipeline(
    session: Session,
    pipeline: ProductionPipeline,
) -> PipelineCompletionDecision:
    for step in pipeline.steps:
        session.refresh(step)
    decision = evaluate_pipeline_completion(pipeline)
    pipeline.status = decision.status
    session.add(pipeline)
    session.commit()
    session.refresh(pipeline)
    return decision


def _build_result(
    pipeline: ProductionPipeline,
) -> ProductionPipelineResult:
    decision = evaluate_pipeline_completion(pipeline)
    steps = sorted(pipeline.steps, key=_step_sort_key)
    step_map = {step.name: step for step in steps}
    material_plan = _load_material_plan(step_map)
    generated_files = _generated_files(step_map)
    failures = [
        {"step_name": step.name, "reason": step.error}
        for step in steps
        if step.status == "失败" and step.error
    ]
    result_steps = [
        PipelineStepResult(
            name=step.name,
            status=step.status,
            started_at=step.started_at,
            finished_at=step.ended_at,
            error_message=step.error,
            output_id=step_output_id(step),
            output_path=str(
                step.output_summary.get("output_path", "") or ""
                if isinstance(step.output_summary, dict)
                else ""
            ),
            output_summary=(
                step.output_summary if isinstance(step.output_summary, dict) else {}
            ),
        )
        for step in steps
    ]
    return ProductionPipelineResult(
        pipeline_id=pipeline.id,
        hotspot_id=pipeline.hotspot_id,
        topic_idea_id=pipeline.topic_idea_id,
        script_asset_id=pipeline.script_asset_id,
        project_name=pipeline.project_name,
        project_key=pipeline.project_key,
        project_dir=pipeline.project_dir,
        legacy_project_dir=pipeline.legacy_project_dir,
        status=decision.status,
        completion_reason_code=decision.reason_code,
        next_surface=decision.next_surface,
        current_step=decision.current_step,
        provider=pipeline.provider,
        model=pipeline.model,
        steps=result_steps,
        material_tasks=[
            material_task_to_dict(task)
            for task in (material_plan.material_tasks if material_plan else [])
        ],
        graphic_tasks=[
            graphic_task_to_dict(task)
            for task in (material_plan.graphic_tasks if material_plan else [])
        ],
        exports=generated_files,
        generated_files=generated_files,
        failure_reasons=failures,
        needs_confirmation=decision.needs_confirmation,
        next_suggestion=decision.next_action,
    )


def _generated_files(steps: dict[str, PipelineStep]) -> dict[str, str]:
    files = {}
    for step_name, key in [
        ("生成剪辑清单", "timeline_json"),
        ("导出 script.md", "script_md"),
        ("导出 shotlist.csv", "shotlist_csv"),
        ("导出 edit_manifest.json", "edit_manifest_json"),
    ]:
        step = steps.get(step_name)
        payload = step.output_summary if step is not None else {}
        if not isinstance(payload, dict):
            continue
        if step_name == "生成剪辑清单":
            generated_files = payload.get("generated_files", {})
            if isinstance(generated_files, dict):
                for file_key, file_path in generated_files.items():
                    path_value = str(file_path or "")
                    if path_value:
                        files[str(file_key)] = path_value
        path = str(payload.get("output_path", "") or "")
        if path:
            files[key] = path
    return files


def _step_sort_key(step: object) -> tuple[int, int]:
    order = getattr(step, "step_order", None)
    return (0, order) if type(order) is int else (1, 0)


def _safe_project_name(value: str) -> str:
    unsafe = '<>:"/\\|?*'
    cleaned = "".join("_" if char in unsafe else char for char in value).strip(" .")
    return cleaned[:60] or "未命名项目"


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
