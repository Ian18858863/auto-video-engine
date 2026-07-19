from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_configured_path
from app.materials.planner import GraphicTask, MaterialTask, MaterialPlan
from app.materials.stock_rights_evidence import (
    StockRightsEvidenceError,
    review_stock_rights_evidence,
)
from app.models import PipelineStep, ProductionPipeline, ScriptAsset, TopicIdea
from app.pipeline.exporter import (
    write_edit_manifest_json,
    write_script_md,
    write_shotlist_csv,
)
from app.pipeline.editing_export_service import build_editing_export_package
from app.pipeline.material_candidate_contract import (
    CandidateContractError,
    CandidateLockedError,
    CandidateSet,
    lock_candidate,
    reselect_candidate,
    reject_candidate,
    replace_candidates,
    update_candidate_rights_evidence,
)
from app.pipeline.material_candidate_gate import assert_pipeline_candidate_review_ready
from app.pipeline.material_candidate_media import (
    admit_candidate_rows,
    revalidate_candidate_media,
)
from app.pipeline.material_selection_invalidation import (
    MaterialSelectionDependencyError,
    assert_material_selection_dependency_current,
)
from app.pipeline.service import PipelineNotFoundError
from app.pipeline.project_identity import (
    begin_prepared_project_child_directory_use,
    begin_prepared_project_directory_use,
    prepared_project_output_path,
)
from app.pipeline.workspace_service import get_script_workspace, serialize_script_workspace


VALID_MATERIAL_STATUSES = {
    "未开始",
    "等待",
    "待执行",
    "进行中",
    "发布收口中",
    "失败",
    "被阻止",
    "需要人工确认",
    "已完成",
    "已拒绝",
    "已跳过",
}


class CandidatePoolConflictError(CandidateContractError):
    """The candidate mutation was built from a stale whole-step snapshot.

    Candidate pools share the material step's JSON document with execution
    state.  A plain ORM assignment would let a stale browser request silently
    overwrite a newer lock or refresh.  Callers must reload the package and
    explicitly retry instead.
    """

    status_code = 409


class CandidatePoolCommitUnknownError(RuntimeError):
    """The database commit acknowledgement was not safely knowable.

    A caller must preserve any newly written filesystem evidence and reconcile
    it against a fresh database read instead of deleting a file that may have
    been referenced by a commit which actually succeeded.
    """

    status_code = 503


class MaterialExportCommitUnknownError(RuntimeError):
    """An export file exists but its database commit acknowledgement is unknown."""

    status_code = 503


def get_material_package(session: Session, pipeline_id: int) -> dict[str, Any]:
    workspace = serialize_script_workspace(get_script_workspace(session, pipeline_id))
    return {
        **workspace,
        "package_note": "素材库管理官方页面、Pexels、Pixabay、本地/程序化素材与候选审核；图库素材必须完成本次用途清权后才能进入后续导出。",
    }


def rebuild_export_file(
    session: Session,
    pipeline_id: int,
    export_type: str,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    pipeline = _get_pipeline(session, pipeline_id)
    if not pipeline.project_dir:
        raise ValueError("项目目录尚未创建，不能重新导出。")
    approved_project_root = resolve_configured_path(
        project_root or get_settings().project_root_dir
    )
    directory_use = begin_prepared_project_directory_use(
        pipeline,
        project_root=approved_project_root,
    )
    project_dir = directory_use.path
    info_use = begin_prepared_project_child_directory_use(
        pipeline,
        project_root=approved_project_root,
        relative_path="素材信息",
        create=False,
    )
    topic = session.get(TopicIdea, pipeline.topic_idea_id) if pipeline.topic_idea_id else None
    script = session.get(ScriptAsset, pipeline.script_asset_id) if pipeline.script_asset_id else None
    plan = _load_material_plan_from_pipeline(pipeline)
    material_tasks = plan.material_tasks if plan else []
    graphic_tasks = plan.graphic_tasks if plan else []

    if export_type in {
        "shotlist_csv",
        "edit_manifest_json",
        "timeline_json",
        "rough_cut_plan_json",
        "subtitles_srt",
        "edit_notes_md",
    }:
        assert_pipeline_candidate_review_ready(
            pipeline,
            [task.shot_id for task in material_tasks],
        )

    if export_type == "script_md":
        expected_path = prepared_project_output_path(info_use, "script.md")
        path = write_script_md(project_dir, topic, script, material_tasks)
        step_name = "导出 script.md"
    elif export_type == "shotlist_csv":
        expected_path = prepared_project_output_path(info_use, "shotlist.csv")
        path = write_shotlist_csv(project_dir, material_tasks)
        step_name = "导出 shotlist.csv"
    elif export_type == "edit_manifest_json":
        expected_path = prepared_project_output_path(info_use, "edit_manifest.json")
        path = write_edit_manifest_json(
            project_dir,
            pipeline_id=pipeline.id,
            provider=pipeline.provider,
            model=pipeline.model,
            topic_idea=topic,
            script_asset=script,
            material_tasks=material_tasks,
            graphic_tasks=graphic_tasks,
        )
        step_name = "导出 edit_manifest.json"
    elif export_type in {
        "timeline_json",
        "rough_cut_plan_json",
        "subtitles_srt",
        "edit_notes_md",
    }:
        package = build_editing_export_package(
            session,
            pipeline_id,
            project_root=approved_project_root,
        )
        generated_path = package.generated_files.get(export_type, "")
        if not generated_path:
            raise ValueError("剪辑清单尚未生成。")
        path = Path(generated_path)
        expected_path = prepared_project_output_path(
            info_use,
            path.name,
            must_exist=True,
        )
        step_name = "生成剪辑清单"
    else:
        raise ValueError("不支持的导出文件类型。")

    if Path(path) != expected_path:
        raise ValueError("导出文件路径越过项目身份目录。")
    prepared_project_output_path(info_use, expected_path.name, must_exist=True)
    info_use.verify()
    directory_use.verify()
    step = _step_by_name(pipeline, step_name)
    step.status = "已完成"
    step.error = ""
    if step_name == "生成剪辑清单":
        step.output_summary = {
            "status": package.status,
            "timeline_segment_count": len(package.timeline),
            "version_count": len(package.versions),
            "output_path": package.generated_files.get("timeline_json", ""),
            "generated_files": package.generated_files,
            "export_task": package.export_task,
        }
    else:
        step.output_summary = {"output_path": str(path), "file_type": export_type}
    session.add(step)
    try:
        session.commit()
    except BaseException as exc:
        try:
            session.rollback()
        except BaseException:
            pass
        raise MaterialExportCommitUnknownError(
            "material export commit acknowledgement is unknown; reconcile before retry"
        ) from exc
    return {"export_type": export_type, "path": str(path), "message": "已重新导出"}


def update_material_task_status(
    session: Session,
    pipeline_id: int,
    shot_id: str,
    status: str,
) -> dict[str, Any]:
    if status not in VALID_MATERIAL_STATUSES:
        raise ValueError("不支持的素材任务状态。")
    pipeline = _get_pipeline(session, pipeline_id)
    step = _step_by_name(pipeline, "生成素材任务")
    expected_payload = copy.deepcopy(step.output_summary or {})
    payload = copy.deepcopy(expected_payload)
    try:
        assert_material_selection_dependency_current(payload, pipeline.script_asset)
    except MaterialSelectionDependencyError as exc:
        raise CandidateContractError(
            f"旧素材任务不能更新状态：{exc}"
        ) from exc
    tasks = [
        dict(item)
        for item in payload.get("material_tasks", [])
        if isinstance(item, dict)
    ]
    updated = False
    for task in tasks:
        if task.get("shot_id") == shot_id:
            task["status"] = status
            history = list(task.get("status_history") or [])
            history.append(status)
            task["status_history"] = history[-10:]
            updated = True
            break
    if not updated:
        raise ValueError(f"素材任务不存在：{shot_id}")
    payload["material_tasks"] = tasks
    with session.no_autoflush:
        cas_result = session.execute(
            update(PipelineStep)
            .where(
                PipelineStep.id == step.id,
                PipelineStep.pipeline_id == pipeline_id,
                PipelineStep.output_summary == expected_payload,
            )
            .values(output_summary=copy.deepcopy(payload))
            .execution_options(synchronize_session=False)
        )
    if cas_result.rowcount == 0:
        session.rollback()
        raise CandidatePoolConflictError(
            "素材任务在状态保存前已变化，请刷新后重试。"
        )
    if cas_result.rowcount != 1:
        session.rollback()
        raise CandidatePoolCommitUnknownError(
            "素材任务状态提交结果未知，请刷新后核对。"
        )
    try:
        session.commit()
    except BaseException as exc:
        try:
            session.rollback()
        except BaseException:
            pass
        raise CandidatePoolCommitUnknownError(
            "素材任务状态提交结果未知，请刷新后核对。"
        ) from exc
    return {"pipeline_id": pipeline_id, "shot_id": shot_id, "status": status}


def save_material_candidate_pool(
    session: Session,
    pipeline_id: int,
    shot_id: str,
    candidates: Any,
) -> dict[str, Any]:
    """Create or refresh one shot's reviewable candidate pool.

    Candidate pools live beside (rather than inside) ``material_tasks`` in
    the material step payload.  Keeping the pool in a separate payload key
    means the legacy ``MaterialTask`` constructor can continue to read old
    tasks without seeing unknown candidate fields.  A refresh always goes
    through the immutable contract transition, so a locked row cannot be
    replaced or removed silently.
    """

    return _save_material_candidate_pool(
        session,
        pipeline_id,
        shot_id,
        candidates,
        trust_rights_evidence=False,
    )


def save_server_material_candidate_pool(
    session: Session,
    pipeline_id: int,
    shot_id: str,
    candidates: Any,
) -> dict[str, Any]:
    """Persist worker-owned candidates while preserving validated evidence.

    This function is deliberately not exposed as a browser route.  Provider
    evidence may enter through this server-owned seam only after the worker
    protocol and publisher have validated it.
    """

    return _save_material_candidate_pool(
        session,
        pipeline_id,
        shot_id,
        candidates,
        trust_rights_evidence=True,
    )


def _save_material_candidate_pool(
    session: Session,
    pipeline_id: int,
    shot_id: str,
    candidates: Any,
    *,
    trust_rights_evidence: bool,
) -> dict[str, Any]:
    pipeline, step, payload = _candidate_pool_context(session, pipeline_id, shot_id)
    _ = pipeline
    pools = _candidate_pool_map(payload)
    existing_value = pools.get(shot_id)
    admitted = admit_candidate_rows(
        candidates,
        trust_rights_evidence=trust_rights_evidence,
    )
    if existing_value is None:
        state = CandidateSet.from_candidates(shot_id, admitted)
    else:
        existing = CandidateSet.from_dict(existing_value)
        state = replace_candidates(existing, admitted)
    return _persist_candidate_pool(session, step, payload, state, pipeline_id=pipeline_id)


def review_material_candidate_rights(
    session: Session,
    pipeline_id: int,
    shot_id: str,
    candidate_id: str,
    *,
    expected_revision: int,
    status: str,
    intended_use: str,
    identifiable_people: str,
    brands_or_logos: str,
    property_or_architecture: str,
    music_or_third_party_audio: str,
    third_party_clearance: str,
    restriction_notes: Any,
    actor: str = "Ian",
) -> dict[str, Any]:
    """Record a server-timestamped per-use review without trusting base facts."""

    if type(expected_revision) is not int or expected_revision < 0:
        raise CandidateContractError(
            "expected revision must be a non-negative integer"
        )
    if actor != "Ian":
        raise CandidateContractError("only Ian can review material rights")
    _pipeline, step, payload = _candidate_pool_context(
        session,
        pipeline_id,
        shot_id,
    )
    state = _candidate_pool_state(payload, shot_id)
    if state is None:
        raise CandidateContractError(
            f"candidate pool not found for shot {shot_id!r}"
        )
    if state.revision != expected_revision:
        raise CandidatePoolConflictError(
            f"candidate pool revision changed from {expected_revision} to {state.revision}"
        )
    current = state.get(candidate_id)
    if current.rights_evidence is None:
        raise CandidateContractError(
            "candidate has no structured stock rights evidence to review"
        )
    reviewed_at = datetime.now(timezone.utc).isoformat()
    previous_status = current.rights_evidence["rights_review"]["status"]
    try:
        reviewed = review_stock_rights_evidence(
            current.rights_evidence,
            status=status,
            intended_use=intended_use,
            identifiable_people=identifiable_people,
            brands_or_logos=brands_or_logos,
            property_or_architecture=property_or_architecture,
            music_or_third_party_audio=music_or_third_party_audio,
            third_party_clearance=third_party_clearance,
            restriction_notes=restriction_notes,
            reviewed_by="Ian",
            reviewed_at=reviewed_at,
        )
    except StockRightsEvidenceError as exc:
        raise CandidateContractError(f"rights review is invalid: {exc}") from exc
    next_state = update_candidate_rights_evidence(
        state,
        candidate_id,
        reviewed,
    )
    event = {
        "event": "candidate_rights_reviewed",
        "shot_id": shot_id,
        "candidate_id": candidate_id,
        "status_from": previous_status,
        "status_to": reviewed["rights_review"]["status"],
        "revision_from": state.revision,
        "revision_to": next_state.revision,
        "actor": "Ian",
        "created_at": reviewed_at,
    }
    return _persist_candidate_pool(
        session,
        step,
        payload,
        next_state,
        pipeline_id=pipeline_id,
        rights_review_event=event,
    )


def replace_material_candidate_pool(
    session: Session,
    pipeline_id: int,
    shot_id: str,
    candidates: Any,
) -> dict[str, Any]:
    """Replace an existing candidate pool while preserving any lock."""

    pipeline, step, payload = _candidate_pool_context(session, pipeline_id, shot_id)
    _ = pipeline
    pools = _candidate_pool_map(payload)
    existing_value = pools.get(shot_id)
    if existing_value is None:
        raise CandidateContractError(f"candidate pool not found for shot {shot_id!r}")
    existing = CandidateSet.from_dict(existing_value)
    state = replace_candidates(existing, admit_candidate_rows(candidates))
    return _persist_candidate_pool(session, step, payload, state, pipeline_id=pipeline_id)


def lock_material_candidate(
    session: Session,
    pipeline_id: int,
    shot_id: str,
    candidate_id: str,
    *,
    reason: str = "",
) -> dict[str, Any]:
    """Lock one candidate through the pure contract transition."""

    pipeline, step, payload = _candidate_pool_context(session, pipeline_id, shot_id)
    _ = pipeline
    state = _candidate_pool_state(payload, shot_id)
    if state is None:
        raise CandidateContractError(f"candidate pool not found for shot {shot_id!r}")
    revalidate_candidate_media(state.get(candidate_id))
    next_state = lock_candidate(state, candidate_id, reason=reason)
    selection_event = None
    if next_state.revision != state.revision:
        selection_event = {
            "event": "candidate_locked",
            "shot_id": shot_id,
            "previous_candidate_id": state.locked_candidate_id,
            "selected_candidate_id": next_state.locked_candidate_id,
            "revision_from": state.revision,
            "revision_to": next_state.revision,
            "reason": reason.strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    return _persist_candidate_pool(
        session,
        step,
        payload,
        next_state,
        pipeline_id=pipeline_id,
        selection_event=selection_event,
    )


def reselect_material_candidate(
    session: Session,
    pipeline_id: int,
    shot_id: str,
    candidate_id: str,
    *,
    expected_revision: int,
    reason: str,
    actor: str = "Ian",
) -> dict[str, Any]:
    """Explicitly move a reviewed lock and append an audit event."""

    if type(expected_revision) is not int or expected_revision < 0:
        raise CandidateContractError("expected revision must be a non-negative integer")
    if actor != "Ian":
        raise CandidateContractError("only Ian can reselect an approved candidate")
    _pipeline, step, payload = _candidate_pool_context(session, pipeline_id, shot_id)
    state = _candidate_pool_state(payload, shot_id)
    if state is None:
        raise CandidateContractError(f"candidate pool not found for shot {shot_id!r}")
    if state.revision != expected_revision:
        raise CandidatePoolConflictError(
            f"candidate pool revision changed from {expected_revision} to {state.revision}"
        )
    revalidate_candidate_media(state.get(candidate_id))
    previous_candidate_id = state.locked_candidate_id
    next_state = reselect_candidate(state, candidate_id, reason=reason)
    event = {
        "event": "candidate_reselected",
        "shot_id": shot_id,
        "previous_candidate_id": previous_candidate_id,
        "selected_candidate_id": next_state.locked_candidate_id,
        "revision_from": state.revision,
        "revision_to": next_state.revision,
        "reason": reason.strip(),
        "actor": "Ian",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return _persist_candidate_pool(
        session,
        step,
        payload,
        next_state,
        pipeline_id=pipeline_id,
        selection_event=event,
    )


def reject_material_candidate(
    session: Session,
    pipeline_id: int,
    shot_id: str,
    candidate_id: str,
    *,
    reason: str,
) -> dict[str, Any]:
    """Reject one candidate through the pure contract transition."""

    pipeline, step, payload = _candidate_pool_context(session, pipeline_id, shot_id)
    _ = pipeline
    state = _candidate_pool_state(payload, shot_id)
    if state is None:
        raise CandidateContractError(f"candidate pool not found for shot {shot_id!r}")
    next_state = reject_candidate(state, candidate_id, reason=reason)
    return _persist_candidate_pool(session, step, payload, next_state, pipeline_id=pipeline_id)


# Product-language aliases kept intentionally explicit for callers that do
# not use the longer material-prefixed names.
save_candidate_pool = save_material_candidate_pool
replace_candidate_pool = replace_material_candidate_pool


def _candidate_pool_context(
    session: Session,
    pipeline_id: int,
    shot_id: str,
) -> tuple[ProductionPipeline, PipelineStep, dict[str, Any]]:
    pipeline = _get_pipeline(session, pipeline_id)
    step = _step_by_name(pipeline, "生成素材任务")
    payload = copy.deepcopy(step.output_summary or {})
    if not isinstance(payload, dict):
        payload = {}
    try:
        assert_material_selection_dependency_current(payload, pipeline.script_asset)
    except MaterialSelectionDependencyError as exc:
        raise CandidateContractError(
            f"旧候选池仅保留为历史：{exc}"
        ) from exc
    tasks = payload.get("material_tasks")
    if not isinstance(tasks, list) or not any(
        isinstance(item, dict) and item.get("shot_id") == shot_id for item in tasks
    ):
        raise CandidateContractError(f"material task not found for shot {shot_id!r}")
    return pipeline, step, payload


def _candidate_pool_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return a defensive canonical pool map from a step payload."""

    raw = payload.get("candidate_pool")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CandidateContractError("candidate_pool must be an object")
    # Accept a single serialized CandidateSet as a migration convenience.
    if "shot_id" in raw and "candidates" in raw:
        shot_id = raw.get("shot_id")
        if not isinstance(shot_id, str) or not shot_id.strip():
            raise CandidateContractError("candidate_pool shot_id is required")
        return {shot_id: copy.deepcopy(raw)}
    result: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise CandidateContractError("candidate_pool keys must be shot IDs")
        if not isinstance(value, dict):
            raise CandidateContractError("candidate_pool values must be objects")
        result[key] = copy.deepcopy(value)
    return result


def _candidate_pool_state(
    payload: dict[str, Any],
    shot_id: str,
) -> CandidateSet | None:
    value = _candidate_pool_map(payload).get(shot_id)
    if value is None:
        return None
    state = CandidateSet.from_dict(value)
    if state.shot_id != shot_id:
        raise CandidateContractError("candidate pool shot_id does not match route")
    return state


def _candidate_pool_response(
    state: CandidateSet,
    *,
    pipeline_id: int | None = None,
) -> dict[str, Any]:
    result = state.to_dict()
    result["locked_candidate_id"] = state.locked_candidate_id
    if pipeline_id is not None:
        # Keep mutation responses equivalent to the workspace read response.
        # The browser receives a route URL and media hint, never a physical
        # Windows path.  Import lazily to keep this legacy service's module
        # graph cycle-free during application startup.
        from app.pipeline.material_candidate_preview_service import public_candidate_pool

        result = public_candidate_pool(
            result,
            pipeline_id=pipeline_id,
            shot_id=state.shot_id,
        )
    return result


def _persist_candidate_pool(
    session: Session,
    step: PipelineStep,
    payload: dict[str, Any],
    state: CandidateSet,
    *,
    pipeline_id: int | None = None,
    selection_event: dict[str, Any] | None = None,
    rights_review_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Keep the exact snapshot that the caller read.  The candidate transition
    # helpers return a new immutable CandidateSet, so ``payload`` is still the
    # pre-mutation JSON document at this point.
    expected_payload = copy.deepcopy(payload)
    pools = _candidate_pool_map(payload)
    pools[state.shot_id] = state.to_dict()
    payload["candidate_pool"] = pools
    if selection_event is not None:
        raw_history = payload.get("candidate_selection_history")
        if raw_history is None:
            history_map: dict[str, Any] = {}
        elif isinstance(raw_history, dict):
            history_map = copy.deepcopy(raw_history)
        else:
            raise CandidateContractError("candidate_selection_history must be an object")
        shot_history = history_map.get(state.shot_id)
        if shot_history is None:
            events: list[Any] = []
        elif isinstance(shot_history, list):
            events = copy.deepcopy(shot_history)
        else:
            raise CandidateContractError("candidate selection history must be a list")
        events.append(copy.deepcopy(selection_event))
        history_map[state.shot_id] = events[-100:]
        payload["candidate_selection_history"] = history_map
    if rights_review_event is not None:
        raw_history = payload.get("candidate_rights_review_history")
        if raw_history is None:
            review_history_map: dict[str, Any] = {}
        elif isinstance(raw_history, dict):
            review_history_map = copy.deepcopy(raw_history)
        else:
            raise CandidateContractError(
                "candidate rights review history must be an object"
            )
        raw_events = review_history_map.get(state.shot_id)
        if raw_events is None:
            review_events: list[Any] = []
        elif isinstance(raw_events, list):
            review_events = copy.deepcopy(raw_events)
        else:
            raise CandidateContractError(
                "candidate rights review history must be a list"
            )
        review_events.append(copy.deepcopy(rights_review_event))
        review_history_map[state.shot_id] = review_events[-100:]
        payload["candidate_rights_review_history"] = review_history_map
    # Build/validate the public response before committing.  If URL
    # serialization fails, no database row or uploaded file has become
    # durable yet, so the caller can safely roll back its owned filesystem
    # artifact.
    response = _candidate_pool_response(state, pipeline_id=pipeline_id)
    if selection_event is not None:
        response["selection_event"] = copy.deepcopy(selection_event)
    if rights_review_event is not None:
        response["rights_review_event"] = copy.deepcopy(rights_review_event)
    pipeline_key = pipeline_id if pipeline_id is not None else step.pipeline_id
    if pipeline_key is None:
        raise CandidatePoolConflictError("candidate pool pipeline identity is missing")

    # Use the same whole-blob JSON compare-and-swap protocol as the material
    # execution claim store.  This is intentionally a single UPDATE guarded by
    # the exact expected JSON, followed by a normal commit; no merge is
    # attempted when another request wins the race.
    with session.no_autoflush:
        cas_result = session.execute(
            update(PipelineStep)
            .where(
                PipelineStep.id == step.id,
                PipelineStep.pipeline_id == pipeline_key,
                PipelineStep.output_summary == expected_payload,
            )
            .values(output_summary=copy.deepcopy(payload))
            .execution_options(synchronize_session=False)
        )
    raw_rowcount = cas_result.rowcount
    if not isinstance(raw_rowcount, int) or raw_rowcount < 0 or raw_rowcount > 1:
        try:
            session.rollback()
        except BaseException:
            pass
        raise CandidatePoolCommitUnknownError(
            "candidate pool CAS outcome is unknown; reconcile before cleanup"
        )
    if raw_rowcount == 0:
        session.rollback()
        raise CandidatePoolConflictError(
            f"candidate pool changed before revision {state.revision} could be saved"
        )
    try:
        session.commit()
    except BaseException as exc:
        try:
            session.rollback()
        except BaseException:
            pass
        raise CandidatePoolCommitUnknownError(
            "candidate pool commit acknowledgement is unknown; reconcile before cleanup"
        ) from exc
    # Expire after commit so callers using a long-lived session load the value
    # that was actually written only if they access it later.
    # A second database round trip here could fail after a successful commit
    # and make an upload handler delete a now-referenced file.
    session.expire(step, ["output_summary"])
    return response


def _get_pipeline(session: Session, pipeline_id: int) -> ProductionPipeline:
    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None:
        raise PipelineNotFoundError(f"流程不存在：{pipeline_id}")
    return pipeline


def _step_by_name(pipeline: ProductionPipeline, name: str) -> PipelineStep:
    step = next((item for item in pipeline.steps if item.name == name), None)
    if step is None:
        raise ValueError(f"流程步骤不存在：{name}")
    return step


def _load_material_plan_from_pipeline(pipeline: ProductionPipeline) -> MaterialPlan | None:
    step = next((item for item in pipeline.steps if item.name == "生成素材任务"), None)
    payload = step.output_summary if step else {}
    material_payloads = payload.get("material_tasks", []) if isinstance(payload, dict) else []
    graphic_payloads = payload.get("graphic_task_candidates", []) if isinstance(payload, dict) else []
    if not isinstance(material_payloads, list) or not material_payloads:
        return None
    return MaterialPlan(
        project_name=pipeline.project_name,
        material_tasks=[
            MaterialTask(**item) for item in material_payloads if isinstance(item, dict)
        ],
        graphic_tasks=[
            GraphicTask(**item) for item in graphic_payloads if isinstance(item, dict)
        ],
    )
