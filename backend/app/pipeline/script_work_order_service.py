from __future__ import annotations

import hashlib
import json
import os
import re
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session, selectinload

from app.account_style.service import get_account_style_context
from app.core.config import get_settings, resolve_configured_path
from app.core.runtime_identity import require_non_system_drive
from app.content_quality.service import check_pipeline_script, serialize_quality_check
from app.models import HotItem, PipelineStep, ProductionPipeline, ScriptAsset, TopicIdea
from app.pipeline.project_identity import (
    PreparedProjectChildDirectoryUse,
    ProjectIdentityError,
    begin_prepared_project_child_directory_use,
    prepared_project_output_path,
    write_prepared_project_files,
)
from app.pipeline.script_asset_ownership import ensure_pipeline_script_asset_owned
from app.pipeline.script_versioning import (
    ScriptVersionError,
    ScriptVersionLedger,
    ScriptVersionOperation,
    ScriptVersionSnapshot,
)
from app.pipeline.shot_director import build_shot_plan
from app.pipeline.material_selection_invalidation import (
    material_input_sha256,
    material_input_sha256_for_script,
)
from app.pipeline.workspace_service import (
    reset_downstream_after_script_edit,
    script_use_avatar,
    script_workspace_state_sha256,
)
from app.script_engine.service import get_script_engine_context


PROMPT_FILE = "script_factory_prompt.md"
CONTEXT_FILE = "script_factory_context.json"
TEMPLATE_FILE = "script_factory_result_template.json"
RESULT_FILE = "script_factory_result.json"
FACTORY_DIRNAME = "脚本工厂"
_JOB_ID_PATTERN = re.compile(
    r"^pipeline-(?P<pipeline_id>[1-9][0-9]*)-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$"
)


class ScriptWorkOrderNotFoundError(ValueError):
    pass


class ScriptWorkOrderValidationError(ValueError):
    pass


class ScriptWorkOrderConflictError(ScriptWorkOrderValidationError):
    """A known CAS loss; the caller must reload before another submission."""


class ScriptWorkOrderCommitUnknownError(RuntimeError):
    """The database may have committed; operation reconciliation is required."""


class ScriptWorkOrderReconcileError(RuntimeError):
    """A durable operation exists but cannot yet be resolved safely."""


@dataclass(slots=True)
class ScriptWorkOrderResult:
    pipeline_id: int
    script_asset_id: int | None
    status: str
    job_id: str = ""
    job_dir: str = ""
    file_path: str = ""
    prompt_path: str = ""
    context_path: str = ""
    template_path: str = ""
    result_path: str = ""
    real_ai_called: bool = False
    failure_reason: str = ""
    next_suggestion: str = ""
    quality_check: dict[str, Any] | None = None
    accepted: bool = False
    dispatch_supported: bool = False
    thread_id: str = ""
    manual_prompt: str = ""


@dataclass(frozen=True, slots=True)
class ScriptVersionRestoreResult:
    operation_id: str
    source_version_id: str
    restored_version_id: str
    restored_version_number: int
    status: str = "draft"
    idempotent: bool = False


def build_script_work_order(
    session: Session,
    pipeline_id: int,
    *,
    project_root: Path | None = None,
) -> ScriptWorkOrderResult:
    pipeline = _get_pipeline(session, pipeline_id)
    topic = _get_topic(session, pipeline)
    hotspot = session.get(HotItem, pipeline.hotspot_id)
    script = _get_script(session, pipeline)
    factory_use = _project_factory_directory_use(
        pipeline,
        project_root=project_root,
        create=True,
    )
    factory_root = factory_use.path
    approved_project_root = resolve_configured_path(
        project_root or get_settings().project_root_dir
    )
    job_id = _new_job_id(pipeline.id)
    job_use = begin_prepared_project_child_directory_use(
        pipeline,
        project_root=approved_project_root,
        relative_path=Path("素材信息") / FACTORY_DIRNAME / job_id,
        create=True,
    )
    job_dir = job_use.path
    if job_dir.parent != factory_root:
        raise ScriptWorkOrderValidationError("脚本工厂任务目录与当前项目身份不一致。")

    context = _build_context(
        pipeline=pipeline,
        hotspot=hotspot,
        topic=topic,
        script=script,
        account_style=get_account_style_context(session),
        script_engine=get_script_engine_context(session),
        job_id=job_id,
        job_dir=job_dir,
    )
    template = _result_template(pipeline.id, job_id)
    prompt = _render_prompt(job_dir)

    try:
        written = write_prepared_project_files(
            job_use,
            {
                PROMPT_FILE: prompt.encode("utf-8"),
                CONTEXT_FILE: json.dumps(
                    context,
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
                TEMPLATE_FILE: json.dumps(
                    template,
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
            },
        )
    except ProjectIdentityError as exc:
        try:
            job_use.verify()
            if not any(job_dir.iterdir()):
                job_dir.rmdir()
        except (OSError, ProjectIdentityError):
            pass
        raise ScriptWorkOrderValidationError(
            f"脚本工厂任务包写入失败：{exc}"
        ) from exc
    prompt_path = written[PROMPT_FILE]
    context_path = written[CONTEXT_FILE]
    template_path = written[TEMPLATE_FILE]
    result_path = prepared_project_output_path(job_use, RESULT_FILE)
    input_evidence = _factory_input_evidence(job_use, job_id=job_id)
    job_use.verify()
    factory_use.verify()

    _record_factory_state(
        pipeline,
        {
            "status": "built",
            "job_id": job_id,
            "job_dir": str(job_dir),
            "file_path": str(prompt_path),
            "prompt_path": str(prompt_path),
            "context_path": str(context_path),
            "template_path": str(template_path),
            "result_path": str(result_path),
            "input_evidence": input_evidence,
            "real_ai_called": False,
            "dispatch_supported": False,
            "thread_id": "",
            "failure_reason": "",
            "next_suggestion": (
                "任务包已生成。下一步尝试派发 Codex 专门线程；若失败，就手动新建 Codex 对话。"
            ),
            "updated_at": _now_iso(),
        },
    )
    pipeline.status = "需要人工确认"
    session.add(pipeline)
    session.commit()

    return _result_from_state(pipeline, "built")


def dispatch_script_work_order(
    session: Session,
    pipeline_id: int,
    *,
    project_root: Path | None = None,
) -> ScriptWorkOrderResult:
    pipeline = _get_pipeline(session, pipeline_id)
    state = _current_factory_state(pipeline)
    factory_use = _project_factory_directory_use(
        pipeline,
        project_root=project_root,
        create=False,
    )
    job_use = _state_job_directory_use(
        pipeline,
        state,
        project_root=resolve_configured_path(
            project_root or get_settings().project_root_dir
        ),
        expected_factory_root=factory_use.path,
    )
    prompt_path = prepared_project_output_path(
        job_use,
        PROMPT_FILE,
        must_exist=True,
    )
    manual_prompt = _manual_dispatch_prompt(prompt_path)
    job_use.verify()
    factory_use.verify()
    failure_reason = (
        "后端网页不能直接新建 Codex Desktop 对话；本阶段必须由当前 Codex Agent "
        "或 Ian 手动开一个新 Codex 对话。"
    )
    state.update(
        {
            "status": "manual_dispatch_required",
            "dispatch_supported": False,
            "thread_id": "",
            "manual_prompt": manual_prompt,
            "failure_reason": failure_reason,
            "next_suggestion": (
                "复制下面内容到新的 Codex 对话，等待它写入 script_factory_result.json 后再导入。"
            ),
            "updated_at": _now_iso(),
        }
    )
    _record_factory_state(pipeline, state)
    pipeline.status = "需要人工确认"
    session.add(pipeline)
    session.commit()
    return _result_from_state(pipeline, "manual_dispatch_required")


def import_script_work_order_result(
    session: Session,
    pipeline_id: int,
    payload: dict[str, Any] | None = None,
    *,
    project_root: Path | None = None,
) -> ScriptWorkOrderResult:
    pipeline = _get_pipeline(session, pipeline_id)
    approved_project_root = resolve_configured_path(
        project_root or get_settings().project_root_dir
    )
    recovery_report = reconcile_pending_script_version_operations(
        session,
        project_root=approved_project_root,
        pipeline_id=pipeline_id,
    )
    _raise_on_script_recovery_blockers(recovery_report)
    pipeline = _get_pipeline(session, pipeline_id)
    _get_topic(session, pipeline)
    state = _current_factory_state(pipeline)
    script_step = _step_by_name(pipeline, "生成脚本素材")
    if script_step is None:
        raise ScriptWorkOrderValidationError("流程缺少“生成脚本素材”步骤。")
    expected_script_output = copy.deepcopy(script_step.output_summary or {})
    factory_use = _project_factory_directory_use(
        pipeline,
        project_root=project_root,
        create=False,
    )
    job_use = _state_job_directory_use(
        pipeline,
        state,
        project_root=approved_project_root,
        expected_factory_root=factory_use.path,
    )
    try:
        result_path = prepared_project_output_path(
            job_use,
            RESULT_FILE,
            must_exist=payload is None,
        )
    except ProjectIdentityError as exc:
        if payload is None:
            raise ScriptWorkOrderValidationError(
                f"还没有找到 Codex 结果文件，或结果路径不安全：{exc}"
            ) from exc
        raise ScriptWorkOrderValidationError(
            f"Codex 结果文件路径不安全：{exc}"
        ) from exc
    if payload is None:
        try:
            raw_payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ScriptWorkOrderValidationError(
                f"Codex 结果文件不是合法 JSON：{exc.msg}"
            ) from exc
    else:
        raw_payload = payload

    normalized = _validate_factory_result(
        raw_payload,
        expected_pipeline_id=pipeline.id,
        expected_job_id=str(state.get("job_id") or ""),
    )
    script_result_hash = _factory_result_hash(normalized)
    input_evidence = _factory_input_evidence(
        job_use,
        job_id=str(state.get("job_id") or ""),
    )
    _verify_factory_input_evidence(state, input_evidence)
    version_input_evidence = _script_version_input_evidence(
        input_evidence,
        result_hash=script_result_hash,
    )
    try:
        version_ledger = ScriptVersionLedger(
            pipeline,
            project_root=approved_project_root,
        )
        version_payload = {
            "voiceover": normalized["voiceover"],
            "storyboard": normalized["storyboard"],
            "subtitles": normalized["subtitles"],
            "animation_prompts": normalized["next_material_brief"],
            "title_options": normalized["title_options"],
            "cover_copy": normalized["cover_copy"],
            "publish_copy": normalized["publish_copy"],
        }
        previous_approved_ids = [
            item.version_id
            for item in version_ledger.list()
            if item.status == "approved"
        ]
        script_version = version_ledger.create(
            version_payload,
            fact_sources=normalized["fact_check"].get("sources", []),
            source="script_factory_import",
            input_evidence=version_input_evidence,
        )
        if script_version.status == "approved":
            already_current = all(
                (
                    str(state.get("status") or "") == "accepted",
                    str(state.get("script_version_id") or "")
                    == script_version.version_id,
                    str(state.get("script_version_hash") or "")
                    == script_version.content_hash,
                    str(state.get("script_version_input_hash") or "")
                    == script_version.input_hash,
                    str(state.get("script_version_snapshot_hash") or "")
                    == script_version.snapshot_hash,
                    str(state.get("script_version_shot_plan_hash") or "")
                    == script_version.shot_plan_hash,
                    str(state.get("script_result_hash") or "") == script_result_hash,
                )
            )
            if already_current:
                return _result_from_state(pipeline, "accepted", accepted=True)
            raise ScriptVersionError(
                "导入结果命中已批准版本，但数据库锚点不一致，请先完成对账"
            )
        operation_id = uuid4().hex
        import_operation = version_ledger.begin_operation(
            operation_id=operation_id,
            kind="import",
            version_id=script_version.version_id,
            context={
                "version_content_hash": script_version.content_hash,
                "version_input_hash": script_version.input_hash,
                "version_snapshot_hash": script_version.snapshot_hash,
                "version_shot_plan_hash": script_version.shot_plan_hash,
                "script_result_hash": script_result_hash,
                "previous_approved_version_ids": previous_approved_ids,
            },
        )
    except ScriptVersionError as exc:
        raise ScriptWorkOrderValidationError(
            f"脚本版本保存失败，未进入素材阶段：{exc}"
        ) from exc
    if payload is not None:
        result_path.write_text(
            json.dumps(raw_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        prepared_project_output_path(job_use, RESULT_FILE, must_exist=True)
    job_use.verify()
    factory_use.verify()
    script = _ensure_script_asset(session, pipeline)
    before_material_input_sha256 = material_input_sha256_for_script(script)
    next_material_input_sha256 = material_input_sha256(
        voiceover=normalized["voiceover"],
        storyboard=normalized["storyboard"],
        animation_prompts=normalized["next_material_brief"],
        use_avatar=script_use_avatar(script),
    )
    script.voiceover = normalized["voiceover"]
    script.storyboard = normalized["storyboard"]
    script.subtitles = normalized["subtitles"]
    script.animation_prompts = normalized["next_material_brief"]
    script.title_options = normalized["title_options"]
    script.cover_copy = normalized["cover_copy"]
    script.publish_copy = normalized["publish_copy"]
    session.add(script)

    pipeline.script_asset_id = script.id
    pipeline.status = "进行中"
    reset_downstream_after_script_edit(
        session,
        pipeline,
        material_contract_changed=(
            before_material_input_sha256 != next_material_input_sha256
        ),
        target_script_version_id=script_version.version_id,
        target_script_version_hash=script_version.content_hash,
        target_material_input_sha256=next_material_input_sha256,
    )
    state.update(
        {
            "status": "imported",
            "script_asset_id": script.id,
            "script_version_id": script_version.version_id,
            "script_version_hash": script_version.content_hash,
            "script_version_input_hash": script_version.input_hash,
            "script_version_snapshot_hash": script_version.snapshot_hash,
            "script_version_shot_plan_hash": script_version.shot_plan_hash,
            "script_result_hash": script_result_hash,
            "import_operation_id": operation_id,
            "shot_plan_count": len(script_version.shot_plan),
            "result_path": str(result_path),
            "real_ai_called": False,
            "failure_reason": "",
            "next_suggestion": "脚本结果已导入。下一步自动验收；通过后只推进到素材任务。",
            "fact_source_count": len(normalized["fact_check"].get("sources", [])),
            "unresolved_fact_count": len(
                normalized["fact_check"].get("unresolved_facts", [])
            ),
            "updated_at": _now_iso(),
        }
    )
    try:
        _commit_factory_state_with_cas(
            session,
            pipeline,
            state,
            expected_output=expected_script_output,
            pipeline_status="进行中",
        )
    except (ScriptWorkOrderValidationError, ScriptWorkOrderCommitUnknownError) as exc:
        outcome = _reconcile_operation_or_raise(
            session,
            pipeline_id=pipeline_id,
            project_root=approved_project_root,
            operation=import_operation,
        )
        if outcome != "completed":
            raise exc
    else:
        outcome = _reconcile_operation_or_raise(
            session,
            pipeline_id=pipeline_id,
            project_root=approved_project_root,
            operation=import_operation,
        )
        if outcome != "completed":
            raise ScriptWorkOrderValidationError(
                "脚本导入数据库已提交，但版本 operation 尚未完成对账。"
            )
    pipeline = _get_pipeline(session, pipeline_id)
    script = _get_script(session, pipeline)
    session.refresh(script)
    return _result_from_state(pipeline, "imported")


def restore_script_version_as_draft(
    session: Session,
    pipeline_id: int,
    source_version_id: str,
    *,
    operation_id: str,
    source_content_hash: str,
    source_snapshot_hash: str,
    expected_current_version_id: str,
    expected_current_content_hash: str,
    expected_current_snapshot_hash: str,
    expected_script_state_sha256: str,
    project_root: Path | None = None,
) -> ScriptVersionRestoreResult:
    """Copy one immutable historical version into a new, unapproved draft.

    The historical snapshot is never made current or modified in place.  The
    database switch is guarded by the current ScriptVersion anchors, and the
    durable restore intent makes the file/SQLite boundary startup-recoverable.
    """

    clean_source_version_id = str(source_version_id or "").strip()
    if not clean_source_version_id:
        raise ScriptWorkOrderValidationError("必须选择要恢复的历史脚本版本。")
    clean_operation_id = _require_restore_operation_id(operation_id)
    clean_expected_state_sha256 = _require_sha256(
        expected_script_state_sha256,
        label="脚本状态",
    )
    approved_project_root = resolve_configured_path(
        project_root or get_settings().project_root_dir
    )
    pipeline = _get_pipeline(session, pipeline_id)
    recovery_report = reconcile_pending_script_version_operations(
        session,
        project_root=approved_project_root,
        pipeline_id=pipeline_id,
    )
    _raise_on_script_recovery_blockers(recovery_report)
    pipeline = _get_pipeline(session, pipeline_id)
    ledger = ScriptVersionLedger(pipeline, project_root=approved_project_root)

    existing_operation = next(
        (
            item
            for item in ledger.list_operations()
            if item.operation_id == clean_operation_id
        ),
        None,
    )
    if existing_operation is not None:
        _verify_restore_request_matches_operation(
            existing_operation,
            source_version_id=clean_source_version_id,
            source_content_hash=source_content_hash,
            source_snapshot_hash=source_snapshot_hash,
            expected_current_version_id=expected_current_version_id,
            expected_current_content_hash=expected_current_content_hash,
            expected_current_snapshot_hash=expected_current_snapshot_hash,
            expected_script_state_sha256=clean_expected_state_sha256,
        )
        outcome = existing_operation.status
        if outcome == "pending":
            outcome = _reconcile_operation_or_raise(
                session,
                pipeline_id=pipeline_id,
                project_root=approved_project_root,
                operation=existing_operation,
            )
        if outcome != "completed":
            raise ScriptWorkOrderConflictError(
                "这次历史版本恢复没有提交成功，请刷新后使用新的操作重新恢复。"
            )
        target = ledger.get(existing_operation.version_id)
        return ScriptVersionRestoreResult(
            operation_id=existing_operation.operation_id,
            source_version_id=clean_source_version_id,
            restored_version_id=target.version_id,
            restored_version_number=target.version_number,
            idempotent=True,
        )

    script_step = _step_by_name(pipeline, "生成脚本素材")
    if script_step is None:
        raise ScriptWorkOrderValidationError("流程缺少“生成脚本素材”步骤。")
    expected_script_output = copy.deepcopy(script_step.output_summary or {})
    state = _script_version_state_for_recovery(pipeline)
    snapshots = ledger.list()
    source = next(
        (item for item in snapshots if item.version_id == clean_source_version_id),
        None,
    )
    if source is None:
        raise ScriptWorkOrderNotFoundError(
            f"历史脚本版本不存在：{clean_source_version_id}"
        )
    _verify_restore_anchor(
        source,
        content_hash=source_content_hash,
        snapshot_hash=source_snapshot_hash,
        label="历史脚本版本",
    )

    current_version_id = str(state.get("script_version_id") or "").strip()
    current = next(
        (item for item in snapshots if item.version_id == current_version_id),
        None,
    )
    if current is None:
        raise ScriptWorkOrderConflictError(
            "当前脚本版本无法核对，请刷新脚本页后再恢复。"
        )
    if source.version_id == current.version_id:
        raise ScriptWorkOrderConflictError("所选版本已经是当前脚本，不需要恢复。")
    if current.version_id != str(expected_current_version_id or "").strip():
        raise ScriptWorkOrderConflictError(
            "当前脚本版本已变化，请刷新脚本页后重新选择历史版本。"
        )
    _verify_restore_anchor(
        current,
        content_hash=expected_current_content_hash,
        snapshot_hash=expected_current_snapshot_hash,
        label="当前脚本版本",
    )

    script = _ensure_script_asset(session, pipeline)
    actual_script_state_sha256 = script_workspace_state_sha256(pipeline, script)
    if actual_script_state_sha256 != clean_expected_state_sha256:
        raise ScriptWorkOrderConflictError(
            "脚本或展示设置已变化，请刷新脚本页后重新恢复历史版本。"
        )
    restored_payload, fallback_fields = _restored_script_payload(
        source,
        fallback_script=script,
    )
    expected_script_asset_values = _script_asset_values(script)
    next_use_avatar = bool(restored_payload["use_avatar"])
    replacement_script_asset_values = _script_asset_values_from_payload(
        restored_payload,
        current_edit_checklist=expected_script_asset_values["edit_checklist"],
    )
    previous_approved_ids = [
        item.version_id for item in snapshots if item.status == "approved"
    ]
    restore_input_evidence = copy.deepcopy(source.input_evidence or {})
    restore_input_evidence["restore_lineage"] = {
        "operation_id": clean_operation_id,
        "source_version_id": source.version_id,
        "source_content_hash": source.content_hash,
        "source_snapshot_hash": source.snapshot_hash,
        "source_schema_version": source.schema_version,
        "fallback_fields": fallback_fields,
    }
    target = _reuse_or_create_restore_target(
        ledger,
        snapshots=snapshots,
        operation_id=clean_operation_id,
        payload=restored_payload,
        fact_sources=source.fact_sources,
        source_version_id=source.version_id,
        input_evidence=restore_input_evidence,
    )
    restore_operation = ledger.begin_operation(
        operation_id=clean_operation_id,
        kind="restore",
        version_id=target.version_id,
        context={
            "version_content_hash": target.content_hash,
            "version_input_hash": target.input_hash,
            "version_snapshot_hash": target.snapshot_hash,
            "version_shot_plan_hash": target.shot_plan_hash,
            "previous_approved_version_ids": previous_approved_ids,
            "restored_from_version_id": source.version_id,
            "restored_from_content_hash": source.content_hash,
            "restored_from_snapshot_hash": source.snapshot_hash,
            "expected_current_version_id": current.version_id,
            "expected_current_content_hash": current.content_hash,
            "expected_current_snapshot_hash": current.snapshot_hash,
            "expected_script_state_sha256": clean_expected_state_sha256,
            "source_schema_version": source.schema_version,
            "source_integrity_status": source.integrity_status,
            "fallback_fields": fallback_fields,
        },
    )

    before_material_input_sha256 = material_input_sha256_for_script(script)
    next_material_input_sha256 = material_input_sha256(
        voiceover=str(restored_payload.get("voiceover") or ""),
        storyboard=list(restored_payload.get("storyboard") or []),
        animation_prompts=list(restored_payload.get("animation_prompts") or []),
        use_avatar=next_use_avatar,
    )
    material_contract_changed = (
        before_material_input_sha256 != next_material_input_sha256
    )
    expected_downstream_steps = _capture_restore_downstream_steps(pipeline)
    try:
        reset_downstream_after_script_edit(
            session,
            pipeline,
            material_contract_changed=material_contract_changed,
            target_script_version_id=target.version_id,
            target_script_version_hash=target.content_hash,
            target_material_input_sha256=next_material_input_sha256,
        )
    except RuntimeError as exc:
        session.rollback()
        outcome = _reconcile_operation_or_raise(
            session,
            pipeline_id=pipeline_id,
            project_root=approved_project_root,
            operation=restore_operation,
        )
        if outcome != "completed":
            raise ScriptWorkOrderConflictError(str(exc)) from exc
    with session.no_autoflush:
        replacement_downstream_steps = _capture_restore_downstream_steps(pipeline)
    state.update(
        {
            "status": "draft_edited",
            "accepted": False,
            "script_asset_id": script.id,
            "script_version_id": target.version_id,
            "script_version_hash": target.content_hash,
            "script_version_input_hash": target.input_hash,
            "script_version_snapshot_hash": target.snapshot_hash,
            "script_version_shot_plan_hash": target.shot_plan_hash,
            "script_result_hash": "",
            "import_operation_id": "",
            "draft_operation_id": "",
            "restore_operation_id": restore_operation.operation_id,
            "approval_operation_id": "",
            "approval_event_hash": "",
            "approval_evidence_hash": "",
            "approval_quality_hash": "",
            "restored_from_version_id": source.version_id,
            "restored_from_content_hash": source.content_hash,
            "restored_from_snapshot_hash": source.snapshot_hash,
            "shot_plan_count": len(target.shot_plan),
            "failure_reason": "",
            "next_suggestion": (
                "历史版本已复制为新草稿。请重新生成任务包、核对事实并验收；"
                "验收前不能进入素材。"
            ),
            "updated_at": _now_iso(),
        }
    )
    try:
        _commit_restore_state_with_cas(
            session,
            pipeline,
            state,
            expected_output=expected_script_output,
            script=script,
            expected_script_asset_values=expected_script_asset_values,
            replacement_script_asset_values=replacement_script_asset_values,
            expected_downstream_steps=expected_downstream_steps,
            replacement_downstream_steps=replacement_downstream_steps,
            material_contract_changed=material_contract_changed,
            pipeline_status="进行中",
        )
    except (ScriptWorkOrderValidationError, ScriptWorkOrderCommitUnknownError) as exc:
        outcome = _reconcile_operation_or_raise(
            session,
            pipeline_id=pipeline_id,
            project_root=approved_project_root,
            operation=restore_operation,
        )
        if outcome != "completed":
            raise exc
    else:
        outcome = _reconcile_operation_or_raise(
            session,
            pipeline_id=pipeline_id,
            project_root=approved_project_root,
            operation=restore_operation,
        )
        if outcome != "completed":
            raise ScriptWorkOrderReconcileError(
                "历史脚本恢复已提交，但 operation 尚未完成对账。"
            )

    return ScriptVersionRestoreResult(
        operation_id=restore_operation.operation_id,
        source_version_id=source.version_id,
        restored_version_id=target.version_id,
        restored_version_number=target.version_number,
    )


def accept_script_work_order(
    session: Session,
    pipeline_id: int,
    *,
    project_root: Path | None = None,
) -> ScriptWorkOrderResult:
    pipeline = _get_pipeline(session, pipeline_id)
    approved_project_root = resolve_configured_path(
        project_root or get_settings().project_root_dir
    )
    recovery_report = reconcile_pending_script_version_operations(
        session,
        project_root=approved_project_root,
        pipeline_id=pipeline_id,
    )
    _raise_on_script_recovery_blockers(recovery_report)
    pipeline = _get_pipeline(session, pipeline_id)
    script = _get_script(session, pipeline)
    state = _current_factory_state(pipeline)
    script_step = _step_by_name(pipeline, "生成脚本素材")
    if script_step is None:
        raise ScriptWorkOrderValidationError("流程缺少“生成脚本素材”步骤。")
    expected_script_output = copy.deepcopy(script_step.output_summary or {})
    factory_use = _project_factory_directory_use(
        pipeline,
        project_root=project_root,
        create=False,
    )
    job_use = _state_job_directory_use(
        pipeline,
        state,
        project_root=approved_project_root,
        expected_factory_root=factory_use.path,
    )
    result_payload = _load_factory_result_payload(
        state,
        expected_factory_root=factory_use.path,
        pipeline_id=pipeline.id,
        job_use=job_use,
    )
    current_input_evidence = _factory_input_evidence(
        job_use,
        job_id=str(state.get("job_id") or ""),
    )
    result_hash = _factory_result_hash(result_payload)
    version_input_evidence = _script_version_input_evidence(
        current_input_evidence,
        result_hash=result_hash,
    )
    _verify_factory_input_evidence(state, current_input_evidence)
    job_use.verify()
    factory_use.verify()
    version_id = str(state.get("script_version_id") or "")
    if not version_id:
        raise ScriptWorkOrderValidationError(
            "脚本通过验收但没有可批准的 ScriptVersion，请重新导入结果。"
        )
    try:
        version_ledger = ScriptVersionLedger(
            pipeline,
            project_root=approved_project_root,
        )
        version_snapshot = version_ledger.get(version_id)
        _verify_current_factory_version(
            script=script,
            state=state,
            result_payload=result_payload,
            version_snapshot=version_snapshot,
            current_input_evidence=version_input_evidence,
            current_result_hash=result_hash,
        )
    except ScriptVersionError as exc:
        raise ScriptWorkOrderValidationError(
            f"脚本版本验收失败，未进入素材阶段：{exc}"
        ) from exc
    quality_record = check_pipeline_script(session, pipeline_id)
    quality = serialize_quality_check(quality_record)
    block_reason = _acceptance_block_reason(result_payload, quality)

    # Quality checks can take time and commit their own evidence record.  Re-read
    # every acceptance input afterwards, before creating an approval event.
    session.expire_all()
    pipeline = _get_pipeline(session, pipeline_id)
    script = _get_script(session, pipeline)
    script_step = _step_by_name(pipeline, "生成脚本素材")
    if script_step is None or script_step.output_summary != expected_script_output:
        raise ScriptWorkOrderValidationError(
            "脚本验收期间项目状态发生并发变化，旧结果未批准，请刷新后重试。"
        )
    state = _current_factory_state(pipeline)
    factory_use = _project_factory_directory_use(
        pipeline,
        project_root=project_root,
        create=False,
    )
    job_use = _state_job_directory_use(
        pipeline,
        state,
        project_root=approved_project_root,
        expected_factory_root=factory_use.path,
    )
    result_payload = _load_factory_result_payload(
        state,
        expected_factory_root=factory_use.path,
        pipeline_id=pipeline.id,
        job_use=job_use,
    )
    current_input_evidence = _factory_input_evidence(
        job_use,
        job_id=str(state.get("job_id") or ""),
    )
    _verify_factory_input_evidence(state, current_input_evidence)
    result_hash = _factory_result_hash(result_payload)
    version_input_evidence = _script_version_input_evidence(
        current_input_evidence,
        result_hash=result_hash,
    )
    version_ledger = ScriptVersionLedger(
        pipeline,
        project_root=approved_project_root,
    )
    try:
        version_snapshot = version_ledger.get(version_id)
        _verify_current_factory_version(
            script=script,
            state=state,
            result_payload=result_payload,
            version_snapshot=version_snapshot,
            current_input_evidence=version_input_evidence,
            current_result_hash=result_hash,
        )
    except ScriptVersionError as exc:
        raise ScriptWorkOrderValidationError(
            f"脚本验收期间版本发生变化，旧结果未批准：{exc}"
        ) from exc

    if block_reason:
        state.update(
            {
                "status": "blocked",
                "quality_check_id": quality_record.id,
                "quality_risk_level": quality.get("risk_level", ""),
                "failure_reason": block_reason,
                "next_suggestion": "脚本验收未通过，不进入素材。请让 Codex 专门线程修正结果后重新导入。",
                "updated_at": _now_iso(),
            }
        )
        _commit_factory_state_with_cas(
            session,
            pipeline,
            state,
            expected_output=expected_script_output,
            pipeline_status="需要人工确认",
        )
        return _result_from_state(
            _get_pipeline(session, pipeline_id),
            "blocked",
            quality_check=quality,
            accepted=False,
        )

    approval_quality_hash = _canonical_sha256(
        {
            key: value
            for key, value in quality.items()
            if key not in {"id", "created_at"}
        }
    )
    approval_evidence = {
        "script_result_hash": result_hash,
        "quality_check_hash": approval_quality_hash,
    }
    approval_evidence_hash = _canonical_sha256(approval_evidence)
    if version_snapshot.status == "approved":
        existing_operation_id = str(state.get("approval_operation_id") or "")
        existing_operation = next(
            (
                item
                for item in version_ledger.list_operations()
                if item.operation_id == existing_operation_id
            ),
            None,
        )
        existing_events = (
            version_ledger.events_for_operation(existing_operation_id)
            if existing_operation is not None
            else []
        )
        if not (
            existing_operation is not None
            and existing_operation.status == "completed"
            and str(existing_operation.context.get("script_result_hash") or "")
            == result_hash
            and str(existing_operation.context.get("approval_quality_hash") or "")
            == approval_quality_hash
            and str(existing_operation.context.get("approval_evidence_hash") or "")
            == approval_evidence_hash
            and _approval_state_matches_operation(
                state,
                version_snapshot,
                existing_operation,
                [
                    event
                    for event in existing_events
                    if event.get("event") == "approved"
                    and event.get("version_id") == version_id
                ],
            )
        ):
            raise ScriptWorkOrderValidationError(
                "脚本版本已显示批准，但 operation 或数据库证据不完整，禁止重复提交。"
            )
    else:
        operation_id = uuid4().hex
        previous_approved_ids = [
            item.version_id
            for item in version_ledger.list()
            if item.status == "approved" and item.version_id != version_id
        ]
        try:
            approval_operation = version_ledger.begin_operation(
                operation_id=operation_id,
                kind="approve",
                version_id=version_id,
                context={
                    "version_content_hash": version_snapshot.content_hash,
                    "version_input_hash": version_snapshot.input_hash,
                    "version_snapshot_hash": version_snapshot.snapshot_hash,
                    "version_shot_plan_hash": version_snapshot.shot_plan_hash,
                    "script_result_hash": result_hash,
                    "approval_quality_hash": approval_quality_hash,
                    "approval_evidence_hash": approval_evidence_hash,
                    "previous_approved_version_ids": previous_approved_ids,
                },
            )
            approved_version = version_ledger.approve(
                version_id,
                approval_evidence=approval_evidence,
                operation_id=operation_id,
            )
        except ScriptVersionError as exc:
            raise ScriptWorkOrderValidationError(
                f"脚本版本批准失败，未进入素材阶段：{exc}"
            ) from exc

        state.update(
            {
                "status": "accepted",
                "quality_check_id": quality_record.id,
                "quality_risk_level": quality.get("risk_level", ""),
                "approval_operation_id": operation_id,
                "approval_event_hash": approved_version.approval_event_hash,
                "approval_evidence_hash": approved_version.approval_evidence_hash,
                "approval_quality_hash": approval_quality_hash,
                "failure_reason": "",
                "next_suggestion": "脚本已通过验收，已推进到素材任务。",
                "updated_at": _now_iso(),
            }
        )
        try:
            _commit_factory_state_with_cas(
                session,
                pipeline,
                state,
                expected_output=expected_script_output,
                pipeline_status="进行中",
            )
        except (ScriptWorkOrderValidationError, ScriptWorkOrderCommitUnknownError) as exc:
            outcome = _reconcile_operation_or_raise(
                session,
                pipeline_id=pipeline_id,
                project_root=approved_project_root,
                operation=approval_operation,
            )
            if outcome != "completed":
                raise exc
        else:
            outcome = _reconcile_operation_or_raise(
                session,
                pipeline_id=pipeline_id,
                project_root=approved_project_root,
                operation=approval_operation,
            )
            if outcome != "completed":
                raise ScriptWorkOrderValidationError(
                    "脚本批准数据库已提交，但版本 operation 尚未完成对账。"
                )

    from app.pipeline.service import continue_pipeline

    continue_pipeline(
        session,
        pipeline_id,
        stop_after="生成素材任务",
        material_root=Path(get_settings().material_root_dir),
        project_root=approved_project_root,
    )
    return _result_from_state(
        _get_pipeline(session, pipeline_id),
        "accepted",
        quality_check=quality,
        accepted=True,
    )


def reconcile_pending_script_version_operations(
    session: Session,
    *,
    project_root: Path,
    pipeline_id: int | None = None,
) -> dict[str, Any]:
    """Resolve durable ScriptVersion file/DB intents without repeating work."""

    approved_root = resolve_configured_path(project_root)
    query = session.query(ProductionPipeline).options(
        selectinload(ProductionPipeline.steps)
    )
    if pipeline_id is not None:
        query = query.filter(ProductionPipeline.id == pipeline_id)
    pipelines = query.order_by(ProductionPipeline.id.asc()).all()
    report: dict[str, Any] = {
        "scanned_pipelines": len(pipelines),
        "scanned_operations": 0,
        "completed": 0,
        "compensated": 0,
        "abandoned": 0,
        "blocked": [],
    }
    for pipeline in pipelines:
        expected_project_dir = approved_root / str(pipeline.project_key or "")
        try:
            stored_project_dir = Path(str(pipeline.project_dir or "")).resolve(
                strict=False
            )
            expected_resolved = expected_project_dir.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            report["blocked"].append(
                {
                    "pipeline_id": pipeline.id,
                    "operation_id": "",
                    "reason": f"项目目录无法核对：{exc}",
                }
            )
            continue
        operations_dir = expected_resolved / "脚本版本" / "operations"
        if stored_project_dir != expected_resolved or not operations_dir.exists():
            continue
        try:
            ledger = ScriptVersionLedger(pipeline, project_root=approved_root)
            pending = ledger.pending_operations()
        except ScriptVersionError as exc:
            report["blocked"].append(
                {
                    "pipeline_id": pipeline.id,
                    "operation_id": "",
                    "reason": str(exc)[:500],
                }
            )
            continue
        for operation in pending:
            report["scanned_operations"] += 1
            try:
                outcome = _reconcile_script_version_operation(
                    session,
                    pipeline_id=pipeline.id,
                    project_root=approved_root,
                    operation=operation,
                )
            except (ScriptVersionError, ScriptWorkOrderValidationError) as exc:
                report["blocked"].append(
                    {
                        "pipeline_id": pipeline.id,
                        "operation_id": operation.operation_id,
                        "reason": str(exc)[:500],
                    }
                )
                continue
            if outcome in {"completed", "compensated", "abandoned"}:
                report[outcome] += 1
            else:
                report["blocked"].append(
                    {
                        "pipeline_id": pipeline.id,
                        "operation_id": operation.operation_id,
                        "reason": "脚本版本 operation 仍待核对",
                    }
                )
    return report


def _raise_on_script_recovery_blockers(report: dict[str, Any]) -> None:
    blockers = report.get("blocked")
    if not isinstance(blockers, list) or not blockers:
        return
    first = blockers[0] if isinstance(blockers[0], dict) else {}
    reason = str(first.get("reason") or "脚本版本 operation 无法自动核对")
    raise ScriptWorkOrderReconcileError(
        f"脚本版本存在待核对的持久操作，已阻止继续提交：{reason}"
    )


def _reconcile_operation_or_raise(
    session: Session,
    *,
    pipeline_id: int,
    project_root: Path,
    operation: ScriptVersionOperation,
) -> str:
    try:
        return _reconcile_script_version_operation(
            session,
            pipeline_id=pipeline_id,
            project_root=project_root,
            operation=operation,
        )
    except (ScriptVersionError, ScriptWorkOrderValidationError) as exc:
        raise ScriptWorkOrderReconcileError(
            "脚本版本提交结果无法安全对账，已阻止继续进入素材。"
        ) from exc


def _reconcile_script_version_operation(
    session: Session,
    *,
    pipeline_id: int,
    project_root: Path,
    operation: ScriptVersionOperation,
) -> str:
    session.expire_all()
    pipeline = _get_pipeline(session, pipeline_id)
    ledger = ScriptVersionLedger(pipeline, project_root=project_root)
    current_operation = next(
        (
            item
            for item in ledger.list_operations()
            if item.operation_id == operation.operation_id
        ),
        None,
    )
    if current_operation is None:
        raise ScriptVersionError("脚本版本 operation intent 在恢复期间消失")
    if current_operation.status != "pending":
        return current_operation.status
    snapshot = ledger.get(current_operation.version_id)
    _verify_operation_snapshot_context(snapshot, current_operation.context)
    state = _script_version_state_for_recovery(pipeline)

    if current_operation.kind == "restore":
        source_version_id = str(
            current_operation.context.get("restored_from_version_id") or ""
        )
        source = ledger.get(source_version_id)
        _verify_restore_source_context(source, current_operation.context)
        script = _get_script(session, pipeline)
        if _restore_state_matches_operation(
            state,
            script,
            snapshot,
            current_operation,
        ):
            invalidated_ids = _invalidate_previous_approvals(
                ledger,
                current_operation,
                reason="历史脚本已恢复为新草稿，旧脚本批准立即失效。",
            )
            ledger.finish_operation(
                current_operation.operation_id,
                outcome="completed",
                result={
                    "database_state": "matched",
                    "source_version_id": source.version_id,
                    "restored_version_id": snapshot.version_id,
                    "previous_approvals_invalidated": invalidated_ids,
                },
            )
            return "completed"
        if (
            str(state.get("restore_operation_id") or "")
            == current_operation.operation_id
            or str(state.get("script_version_id") or "")
            == current_operation.version_id
        ):
            raise ScriptVersionError(
                "历史脚本恢复已写入部分数据库锚点，但内容不一致，必须人工对账。"
            )
        ledger.finish_operation(
            current_operation.operation_id,
            outcome="abandoned",
            result={"database_state": "not_committed"},
        )
        return "abandoned"

    if current_operation.kind == "draft":
        script = _get_script(session, pipeline)
        if _draft_state_matches_operation(
            state,
            script,
            snapshot,
            current_operation,
        ):
            invalidated_ids = _invalidate_previous_approvals(
                ledger,
                current_operation,
                reason="当前脚本草稿已提交，旧脚本批准立即失效。",
            )
            ledger.finish_operation(
                current_operation.operation_id,
                outcome="completed",
                result={
                    "database_state": "matched",
                    "previous_approvals_invalidated": invalidated_ids,
                },
            )
            return "completed"
        if (
            str(state.get("draft_operation_id") or "")
            == current_operation.operation_id
            or str(state.get("script_version_id") or "")
            == current_operation.version_id
        ):
            raise ScriptVersionError(
                "脚本草稿数据库已指向当前 operation，但内容或版本锚点不一致"
            )
        ledger.finish_operation(
            current_operation.operation_id,
            outcome="abandoned",
            result={"database_state": "not_committed"},
        )
        return "abandoned"

    if current_operation.kind == "import":
        if _import_state_matches_operation(state, snapshot, current_operation):
            invalidated_ids = _invalidate_previous_approvals(
                ledger,
                current_operation,
                reason="新的 Script Factory 草稿已提交，旧脚本批准立即失效。",
            )
            ledger.finish_operation(
                current_operation.operation_id,
                outcome="completed",
                result={
                    "database_state": "matched",
                    "previous_approvals_invalidated": invalidated_ids,
                },
            )
            return "completed"
        if str(state.get("import_operation_id") or "") == current_operation.operation_id:
            raise ScriptVersionError(
                "脚本导入数据库记录了当前 operation_id，但版本锚点不一致"
            )
        ledger.finish_operation(
            current_operation.operation_id,
            outcome="abandoned",
            result={"database_state": "not_committed"},
        )
        return "abandoned"

    if current_operation.kind != "approve":
        raise ScriptVersionError("当前脚本版本 operation 类型尚不支持自动恢复")

    target_approval_events = [
        event
        for event in ledger.events_for_operation(current_operation.operation_id)
        if event.get("event") == "approved"
        and event.get("version_id") == current_operation.version_id
    ]
    if _approval_state_matches_operation(
        state,
        snapshot,
        current_operation,
        target_approval_events,
    ):
        if snapshot.status != "approved":
            raise ScriptVersionError("数据库已接受脚本，但对应批准事件已经失效")
        invalidated_ids = _invalidate_previous_approvals(
            ledger,
            current_operation,
            reason="新脚本版本已通过验收并取代原批准版本。",
        )
        ledger.finish_operation(
            current_operation.operation_id,
            outcome="completed",
            result={
                "database_state": "matched",
                "previous_approvals_invalidated": invalidated_ids,
            },
        )
        return "completed"

    if str(state.get("approval_operation_id") or "") == current_operation.operation_id:
        raise ScriptVersionError(
            "脚本批准数据库记录了当前 operation_id，但批准锚点不一致"
        )
    if (
        str(state.get("status") or "") == "accepted"
        and str(state.get("script_version_id") or "") == current_operation.version_id
    ):
        raise ScriptVersionError("当前版本已有另一份数据库批准记录，不能自动补偿")

    if target_approval_events and snapshot.status == "approved":
        ledger.invalidate(
            current_operation.version_id,
            reason="脚本批准事件未匹配数据库提交，启动对账已补偿撤销。",
            operation_id=current_operation.operation_id,
        )
    outcome = "compensated" if target_approval_events else "abandoned"
    ledger.finish_operation(
        current_operation.operation_id,
        outcome=outcome,
        result={
            "database_state": "not_committed",
            "approval_event_found": bool(target_approval_events),
        },
    )
    return outcome


def _verify_operation_snapshot_context(
    snapshot: ScriptVersionSnapshot,
    context: dict[str, Any],
) -> None:
    anchors = {
        "version_content_hash": snapshot.content_hash,
        "version_input_hash": snapshot.input_hash,
        "version_snapshot_hash": snapshot.snapshot_hash,
        "version_shot_plan_hash": snapshot.shot_plan_hash,
    }
    for label, expected in anchors.items():
        if not expected or str(context.get(label) or "") != expected:
            raise ScriptVersionError(f"脚本版本 operation 的 {label} 锚点不一致")


def _require_restore_operation_id(value: Any) -> str:
    operation_id = str(value or "").strip()
    if re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
        raise ScriptWorkOrderValidationError(
            "历史版本恢复 operation_id 必须是 32 位小写十六进制。"
        )
    return operation_id


def _require_sha256(value: Any, *, label: str) -> str:
    digest = str(value or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ScriptWorkOrderValidationError(f"{label}哈希必须是 64 位小写十六进制。")
    return digest


def _verify_restore_request_matches_operation(
    operation: ScriptVersionOperation,
    *,
    source_version_id: str,
    source_content_hash: str,
    source_snapshot_hash: str,
    expected_current_version_id: str,
    expected_current_content_hash: str,
    expected_current_snapshot_hash: str,
    expected_script_state_sha256: str,
) -> None:
    context = operation.context
    requested = {
        "restored_from_version_id": str(source_version_id or "").strip(),
        "restored_from_content_hash": str(source_content_hash or "").strip(),
        "restored_from_snapshot_hash": str(source_snapshot_hash or "").strip(),
        "expected_current_version_id": str(expected_current_version_id or "").strip(),
        "expected_current_content_hash": str(
            expected_current_content_hash or ""
        ).strip(),
        "expected_current_snapshot_hash": str(
            expected_current_snapshot_hash or ""
        ).strip(),
        "expected_script_state_sha256": str(
            expected_script_state_sha256 or ""
        ).strip(),
    }
    if operation.kind != "restore" or any(
        str(context.get(key) or "") != expected for key, expected in requested.items()
    ):
        raise ScriptWorkOrderConflictError(
            "同一 operation_id 已绑定另一份历史脚本恢复请求。"
        )


def _verify_restore_anchor(
    snapshot: ScriptVersionSnapshot,
    *,
    content_hash: str,
    snapshot_hash: str,
    label: str,
) -> None:
    requested_content_hash = _require_sha256(content_hash, label=f"{label}内容")
    requested_snapshot_hash = str(snapshot_hash or "").strip()
    if requested_snapshot_hash and re.fullmatch(
        r"[0-9a-f]{64}", requested_snapshot_hash
    ) is None:
        raise ScriptWorkOrderValidationError(
            f"{label}快照哈希必须为空或 64 位小写十六进制。"
        )
    if (
        snapshot.content_hash != requested_content_hash
        or snapshot.snapshot_hash != requested_snapshot_hash
    ):
        raise ScriptWorkOrderConflictError(
            f"{label}已变化，请刷新脚本页后重新选择。"
        )


def _restored_script_payload(
    source: ScriptVersionSnapshot,
    *,
    fallback_script: ScriptAsset,
) -> tuple[dict[str, Any], list[str]]:
    fallback = {
        "voiceover": fallback_script.voiceover,
        "storyboard": copy.deepcopy(fallback_script.storyboard or []),
        "subtitles": copy.deepcopy(fallback_script.subtitles or []),
        "animation_prompts": copy.deepcopy(
            fallback_script.animation_prompts or []
        ),
        "title_options": copy.deepcopy(fallback_script.title_options or []),
        "cover_copy": copy.deepcopy(fallback_script.cover_copy or []),
        "publish_copy": copy.deepcopy(fallback_script.publish_copy or {}),
        "use_avatar": script_use_avatar(fallback_script),
    }
    restored = copy.deepcopy(source.payload)
    fallback_fields: list[str] = []
    for field_name, fallback_value in fallback.items():
        if field_name not in restored:
            restored[field_name] = fallback_value
            fallback_fields.append(field_name)

    if not isinstance(restored.get("voiceover"), str):
        raise ScriptVersionError("历史脚本口播字段不是文本，不能安全恢复")
    for field_name, label in (
        ("storyboard", "分镜"),
        ("subtitles", "字幕"),
        ("animation_prompts", "动画提示"),
        ("title_options", "标题"),
        ("cover_copy", "封面文案"),
    ):
        value = restored.get(field_name)
        if not isinstance(value, list) or any(
            not isinstance(item, dict) for item in value
        ):
            raise ScriptVersionError(f"历史脚本{label}字段无效，不能安全恢复")
    if not isinstance(restored.get("publish_copy"), dict):
        raise ScriptVersionError("历史脚本发布文案字段无效，不能安全恢复")
    if not isinstance(restored.get("use_avatar"), bool):
        raise ScriptVersionError("历史脚本展示设置无效，不能安全恢复")
    return restored, fallback_fields


def _reuse_or_create_restore_target(
    ledger: ScriptVersionLedger,
    *,
    snapshots: list[ScriptVersionSnapshot],
    operation_id: str,
    payload: dict[str, Any],
    fact_sources: tuple[dict[str, Any], ...],
    source_version_id: str,
    input_evidence: dict[str, Any],
) -> ScriptVersionSnapshot:
    orphan_candidates: list[ScriptVersionSnapshot] = []
    for snapshot in snapshots:
        evidence = snapshot.input_evidence or {}
        lineage = evidence.get("restore_lineage")
        if (
            isinstance(lineage, dict)
            and str(lineage.get("operation_id") or "") == operation_id
        ):
            orphan_candidates.append(snapshot)
    if len(orphan_candidates) > 1:
        raise ScriptWorkOrderReconcileError(
            "同一恢复 operation_id 找到多个孤儿脚本版本，必须人工核对。"
        )
    if orphan_candidates:
        target = orphan_candidates[0]
        if not all(
            (
                target.status == "draft",
                target.source == f"restore:{source_version_id}",
                target.payload == payload,
                list(target.fact_sources) == list(fact_sources),
                target.input_evidence == input_evidence,
            )
        ):
            raise ScriptWorkOrderReconcileError(
                "孤儿恢复版本与当前请求不一致，禁止自动重新挂接。"
            )
        return target
    return ledger.create(
        payload,
        fact_sources=fact_sources,
        source=f"restore:{source_version_id}",
        input_evidence=input_evidence,
    )


def _script_asset_values(script: ScriptAsset) -> dict[str, Any]:
    return {
        "voiceover": script.voiceover,
        "storyboard": copy.deepcopy(script.storyboard or []),
        "subtitles": copy.deepcopy(script.subtitles or []),
        "animation_prompts": copy.deepcopy(script.animation_prompts or []),
        "title_options": copy.deepcopy(script.title_options or []),
        "cover_copy": copy.deepcopy(script.cover_copy or []),
        "publish_copy": copy.deepcopy(script.publish_copy or {}),
        "edit_checklist": copy.deepcopy(script.edit_checklist or []),
    }


def _script_asset_values_from_payload(
    payload: dict[str, Any],
    *,
    current_edit_checklist: list[dict[str, Any]],
) -> dict[str, Any]:
    edit_checklist = [
        copy.deepcopy(item)
        for item in current_edit_checklist
        if isinstance(item, dict) and item.get("type") != "presentation_options"
    ]
    use_avatar = bool(payload["use_avatar"])
    edit_checklist.insert(
        0,
        {
            "type": "presentation_options",
            "use_avatar": use_avatar,
            "voice_mode": "digital_human" if use_avatar else "voiceover_only",
        },
    )
    return {
        "voiceover": str(payload["voiceover"]),
        "storyboard": copy.deepcopy(payload["storyboard"]),
        "subtitles": copy.deepcopy(payload["subtitles"]),
        "animation_prompts": copy.deepcopy(payload["animation_prompts"]),
        "title_options": copy.deepcopy(payload["title_options"]),
        "cover_copy": copy.deepcopy(payload["cover_copy"]),
        "publish_copy": copy.deepcopy(payload["publish_copy"]),
        "edit_checklist": edit_checklist,
    }


def _script_version_state_for_recovery(
    pipeline: ProductionPipeline,
) -> dict[str, Any]:
    step = _step_by_name(pipeline, "生成脚本素材")
    if step is None:
        raise ScriptVersionError("流程缺少生成脚本素材步骤，无法恢复版本 operation")
    output = step.output_summary if isinstance(step.output_summary, dict) else {}
    state = output.get("script_factory") or output.get("script_work_order") or {}
    if not isinstance(state, dict):
        raise ScriptVersionError("脚本版本数据库状态不是对象")
    return dict(state)


def _operation_previous_approved_ids(operation: ScriptVersionOperation) -> list[str]:
    raw = operation.context.get("previous_approved_version_ids")
    if not isinstance(raw, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw
    ):
        raise ScriptVersionError("脚本版本 operation 的历史批准列表无效")
    return sorted(set(raw))


def _invalidate_previous_approvals(
    ledger: ScriptVersionLedger,
    operation: ScriptVersionOperation,
    *,
    reason: str,
) -> list[str]:
    invalidated: list[str] = []
    for version_id in _operation_previous_approved_ids(operation):
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
    return invalidated


def _import_state_matches_operation(
    state: dict[str, Any],
    snapshot: ScriptVersionSnapshot,
    operation: ScriptVersionOperation,
) -> bool:
    context = operation.context
    return all(
        (
            str(state.get("import_operation_id") or "") == operation.operation_id,
            str(state.get("script_version_id") or "") == snapshot.version_id,
            str(state.get("script_version_hash") or "") == snapshot.content_hash,
            str(state.get("script_version_input_hash") or "") == snapshot.input_hash,
            str(state.get("script_version_snapshot_hash") or "")
            == snapshot.snapshot_hash,
            str(state.get("script_version_shot_plan_hash") or "")
            == snapshot.shot_plan_hash,
            str(state.get("script_result_hash") or "")
            == str(context.get("script_result_hash") or ""),
            str(state.get("status") or "") in {"imported", "accepted"},
        )
    )


def _verify_restore_source_context(
    source: ScriptVersionSnapshot,
    context: dict[str, Any],
) -> None:
    expected = {
        "restored_from_version_id": source.version_id,
        "restored_from_content_hash": source.content_hash,
        "restored_from_snapshot_hash": source.snapshot_hash,
        "source_schema_version": source.schema_version,
        "source_integrity_status": source.integrity_status,
    }
    if any(context.get(key) != value for key, value in expected.items()):
        raise ScriptVersionError("历史脚本恢复 operation 的来源血缘锚点不一致")
    state_hash = str(context.get("expected_script_state_sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", state_hash) is None:
        raise ScriptVersionError("历史脚本恢复 operation 缺少有效的原始状态哈希")
    fallback_fields = context.get("fallback_fields")
    if not isinstance(fallback_fields, list) or any(
        not isinstance(item, str) for item in fallback_fields
    ):
        raise ScriptVersionError("历史脚本恢复 operation 的字段补全记录无效")


def _restore_state_matches_operation(
    state: dict[str, Any],
    script: ScriptAsset,
    snapshot: ScriptVersionSnapshot,
    operation: ScriptVersionOperation,
) -> bool:
    context = operation.context
    try:
        expected_script_values = _script_asset_values_from_payload(
            snapshot.payload,
            current_edit_checklist=copy.deepcopy(script.edit_checklist or []),
        )
    except (KeyError, TypeError, ValueError):
        return False
    return all(
        (
            str(state.get("status") or "") == "draft_edited",
            state.get("accepted") is False,
            str(state.get("restore_operation_id") or "")
            == operation.operation_id,
            str(state.get("script_version_id") or "") == snapshot.version_id,
            str(state.get("script_version_hash") or "") == snapshot.content_hash,
            str(state.get("script_version_input_hash") or "")
            == snapshot.input_hash,
            str(state.get("script_version_snapshot_hash") or "")
            == snapshot.snapshot_hash,
            str(state.get("script_version_shot_plan_hash") or "")
            == snapshot.shot_plan_hash,
            str(state.get("restored_from_version_id") or "")
            == str(context.get("restored_from_version_id") or ""),
            str(state.get("restored_from_content_hash") or "")
            == str(context.get("restored_from_content_hash") or ""),
            str(state.get("restored_from_snapshot_hash") or "")
            == str(context.get("restored_from_snapshot_hash") or ""),
            str(state.get("approval_operation_id") or "") == "",
            str(state.get("approval_event_hash") or "") == "",
            str(state.get("approval_evidence_hash") or "") == "",
            str(state.get("approval_quality_hash") or "") == "",
            _script_asset_values(script) == expected_script_values,
        )
    )


def _draft_state_matches_operation(
    state: dict[str, Any],
    script: ScriptAsset,
    snapshot: ScriptVersionSnapshot,
    operation: ScriptVersionOperation,
) -> bool:
    current_payload = {
        "voiceover": script.voiceover,
        "storyboard": script.storyboard,
        "subtitles": script.subtitles,
        "animation_prompts": script.animation_prompts,
        "title_options": script.title_options,
        "cover_copy": script.cover_copy,
        "publish_copy": script.publish_copy,
        "use_avatar": script_use_avatar(script),
    }
    return all(
        (
            str(state.get("status") or "") == "draft_edited",
            str(state.get("draft_operation_id") or "")
            == operation.operation_id,
            str(state.get("script_version_id") or "") == snapshot.version_id,
            str(state.get("script_version_hash") or "") == snapshot.content_hash,
            str(state.get("script_version_input_hash") or "") == snapshot.input_hash,
            str(state.get("script_version_snapshot_hash") or "")
            == snapshot.snapshot_hash,
            str(state.get("script_version_shot_plan_hash") or "")
            == snapshot.shot_plan_hash,
            snapshot.payload == current_payload,
        )
    )


def _approval_state_matches_operation(
    state: dict[str, Any],
    snapshot: ScriptVersionSnapshot,
    operation: ScriptVersionOperation,
    approval_events: list[dict[str, Any]],
) -> bool:
    if len(approval_events) != 1:
        return False
    event = approval_events[0]
    context = operation.context
    return all(
        (
            str(state.get("status") or "") == "accepted",
            str(state.get("script_version_id") or "") == snapshot.version_id,
            str(state.get("script_version_hash") or "") == snapshot.content_hash,
            str(state.get("script_version_input_hash") or "") == snapshot.input_hash,
            str(state.get("script_version_snapshot_hash") or "")
            == snapshot.snapshot_hash,
            str(state.get("script_version_shot_plan_hash") or "")
            == snapshot.shot_plan_hash,
            str(state.get("script_result_hash") or "")
            == str(context.get("script_result_hash") or ""),
            str(state.get("approval_operation_id") or "")
            == operation.operation_id,
            str(state.get("approval_event_hash") or "")
            == str(event.get("event_hash") or ""),
            str(state.get("approval_evidence_hash") or "")
            == str(context.get("approval_evidence_hash") or ""),
            str(state.get("approval_quality_hash") or "")
            == str(context.get("approval_quality_hash") or ""),
            snapshot.approval_event_hash == str(event.get("event_hash") or ""),
            snapshot.approval_evidence_hash
            == str(context.get("approval_evidence_hash") or ""),
        )
    )


def _verify_current_factory_version(
    *,
    script: ScriptAsset,
    state: dict[str, Any],
    result_payload: dict[str, Any],
    version_snapshot: ScriptVersionSnapshot,
    current_input_evidence: dict[str, Any],
    current_result_hash: str,
) -> None:
    if version_snapshot.source != "script_factory_import":
        raise ScriptVersionError("当前草稿不是本次脚本工厂导入版本，请重新生成任务包")
    if version_snapshot.status == "invalidated":
        raise ScriptVersionError("当前脚本版本已经失效")
    if version_snapshot.integrity_status != "full_envelope_verified":
        raise ScriptVersionError("当前脚本版本没有完整快照哈希")
    state_hash = str(state.get("script_version_hash") or "")
    if not state_hash or state_hash != version_snapshot.content_hash:
        raise ScriptVersionError("脚本工厂状态与当前版本哈希不一致")
    anchors = {
        "script_version_input_hash": ("版本输入哈希", version_snapshot.input_hash),
        "script_version_snapshot_hash": ("版本快照哈希", version_snapshot.snapshot_hash),
        "script_version_shot_plan_hash": ("ShotPlan 镜头哈希", version_snapshot.shot_plan_hash),
        "script_result_hash": ("脚本结果哈希", current_result_hash),
    }
    for label, (description, expected) in anchors.items():
        if not expected or str(state.get(label) or "") != expected:
            raise ScriptVersionError(f"脚本工厂状态的{description}与当前版本不一致")
    if version_snapshot.input_evidence != current_input_evidence:
        raise ScriptVersionError(
            "脚本工厂任务输入证据哈希已变化，疑似篡改，请重新生成任务包"
        )

    current_payload = {
        "voiceover": script.voiceover,
        "storyboard": script.storyboard,
        "subtitles": script.subtitles,
        "animation_prompts": script.animation_prompts,
        "title_options": script.title_options,
        "cover_copy": script.cover_copy,
        "publish_copy": script.publish_copy,
    }
    if version_snapshot.payload != current_payload:
        raise ScriptVersionError("当前脚本内容与待批准版本不一致")

    fact_check = result_payload.get("fact_check")
    result_sources = fact_check.get("sources") if isinstance(fact_check, dict) else None
    if list(version_snapshot.fact_sources) != list(result_sources or []):
        raise ScriptVersionError("脚本事实来源与待批准版本不一致")

    derived_shot_plan = build_shot_plan(version_snapshot.payload)
    if (
        list(version_snapshot.shot_plan) != derived_shot_plan
        or version_snapshot.shot_plan_hash != _canonical_sha256(derived_shot_plan)
    ):
        raise ScriptVersionError("ShotPlan 与当前脚本重新推导的镜头计划不一致")


def serialize_script_work_order_result(result: ScriptWorkOrderResult) -> dict[str, Any]:
    return {
        "pipeline_id": result.pipeline_id,
        "script_asset_id": result.script_asset_id,
        "status": result.status,
        "job_id": result.job_id,
        "job_dir": result.job_dir,
        "file_path": result.file_path,
        "prompt_path": result.prompt_path,
        "context_path": result.context_path,
        "template_path": result.template_path,
        "result_path": result.result_path,
        "real_ai_called": result.real_ai_called,
        "failure_reason": result.failure_reason,
        "next_suggestion": result.next_suggestion,
        "quality_check": result.quality_check,
        "accepted": result.accepted,
        "dispatch_supported": result.dispatch_supported,
        "thread_id": result.thread_id,
        "manual_prompt": result.manual_prompt,
    }


def serialize_script_version_restore_result(
    result: ScriptVersionRestoreResult,
) -> dict[str, Any]:
    return {
        "operation_id": result.operation_id,
        "source_version_id": result.source_version_id,
        "restored_version_id": result.restored_version_id,
        "restored_version_number": result.restored_version_number,
        "status": result.status,
        "idempotent": result.idempotent,
    }


def _get_pipeline(session: Session, pipeline_id: int) -> ProductionPipeline:
    pipeline = (
        session.query(ProductionPipeline)
        .options(selectinload(ProductionPipeline.steps))
        .filter(ProductionPipeline.id == pipeline_id)
        .one_or_none()
    )
    if pipeline is None:
        raise ScriptWorkOrderNotFoundError(f"流程不存在：{pipeline_id}")
    return pipeline


def _get_topic(session: Session, pipeline: ProductionPipeline) -> TopicIdea:
    if pipeline.topic_idea_id is None:
        raise ScriptWorkOrderNotFoundError(f"流程还没有选题：{pipeline.id}")
    topic = session.get(TopicIdea, pipeline.topic_idea_id)
    if topic is None:
        raise ScriptWorkOrderNotFoundError(f"选题不存在：{pipeline.topic_idea_id}")
    return topic


def _get_script(session: Session, pipeline: ProductionPipeline) -> ScriptAsset:
    if pipeline.script_asset_id is None:
        raise ScriptWorkOrderNotFoundError(f"流程还没有脚本素材：{pipeline.id}")
    script = session.get(ScriptAsset, pipeline.script_asset_id)
    if script is None:
        raise ScriptWorkOrderNotFoundError(f"脚本素材不存在：{pipeline.script_asset_id}")
    return script


def _ensure_script_asset(session: Session, pipeline: ProductionPipeline) -> ScriptAsset:
    if pipeline.script_asset_id is not None:
        existing = session.get(ScriptAsset, pipeline.script_asset_id)
        if existing is not None:
            return ensure_pipeline_script_asset_owned(session, pipeline, existing)
    topic = _get_topic(session, pipeline)
    script = ScriptAsset(topic_idea_id=topic.id)
    session.add(script)
    session.flush()
    pipeline.script_asset_id = script.id
    return script


def _project_factory_root(
    pipeline: ProductionPipeline,
    *,
    project_root: Path | None = None,
    create: bool = True,
) -> Path:
    return _project_factory_directory_use(
        pipeline,
        project_root=project_root,
        create=create,
    ).path


def _project_factory_directory_use(
    pipeline: ProductionPipeline,
    *,
    project_root: Path | None = None,
    create: bool,
) -> PreparedProjectChildDirectoryUse:
    if not pipeline.project_dir:
        raise ScriptWorkOrderNotFoundError(
            f"流程还没有中文项目目录：{pipeline.id}。请先执行到“创建中文项目目录”。"
        )
    approved_project_root = resolve_configured_path(
        project_root or get_settings().project_root_dir
    )
    try:
        factory_use = begin_prepared_project_child_directory_use(
            pipeline,
            project_root=approved_project_root,
            relative_path=Path("素材信息") / FACTORY_DIRNAME,
            create=create,
        )
    except (ProjectIdentityError, ValueError) as exc:
        raise ScriptWorkOrderValidationError(
            f"脚本工厂项目身份目录验证失败：{exc}"
        ) from exc
    _require_script_factory_path(factory_use.path, action="写入")
    factory_use.verify()
    return factory_use


def _build_context(
    *,
    pipeline: ProductionPipeline,
    hotspot: HotItem | None,
    topic: TopicIdea,
    script: ScriptAsset,
    account_style: dict[str, Any],
    script_engine: dict[str, Any],
    job_id: str,
    job_dir: Path,
) -> dict[str, Any]:
    return {
        "pipeline": {
            "id": pipeline.id,
            "project_name": pipeline.project_name,
            "project_dir": pipeline.project_dir,
            "provider": pipeline.provider,
            "real_ai_called": False,
        },
        "script_factory_job": {
            "job_id": job_id,
            "job_dir": str(job_dir),
            "result_file": str(job_dir / RESULT_FILE),
            "codex_thread_input": "新建一个专门 Codex 对话，把 script_factory_prompt.md 作为第一条消息。",
        },
        "storage": {
            "must_stay_on_drive": job_dir.drive.upper(),
            "forbidden_drive": str(os.environ.get("SystemDrive") or "").upper(),
            "large_outputs_allowed_under": str(job_dir),
        },
        "hotspot": {
            "title": hotspot.title if hotspot else "",
            "summary": hotspot.summary if hotspot else "",
            "url": hotspot.url if hotspot else "",
            "tags": hotspot.tags if hotspot else [],
            "source_platform": hotspot.source_platform if hotspot else "",
        },
        "topic": {
            "title": topic.title,
            "core_view": topic.core_view,
            "hook": topic.hook,
            "recommended_format": topic.recommended_format,
            "recommended_duration": topic.recommended_duration or "按选题决定",
            "risk_note": topic.risk_note,
        },
        "current_script": {
            "voiceover": script.voiceover,
            "storyboard": script.storyboard,
            "subtitles": script.subtitles,
            "title_options": script.title_options,
            "cover_copy": script.cover_copy,
            "publish_copy": script.publish_copy,
        },
        "account_style": account_style,
        "script_engine": script_engine,
        "quality_rules": {
            "must_use_plain_language": True,
            "must_have_clear_judgement": True,
            "must_have_collect_value": True,
            "must_record_fact_sources": True,
            "forbidden": ["投资建议", "稳赚", "必涨", "无来源事实"],
        },
    }


def _result_template(pipeline_id: int, job_id: str) -> dict[str, Any]:
    return {
        "meta": {
            "pipeline_id": pipeline_id,
            "job_id": job_id,
            "duration_strategy": "按选题决定",
            "target_duration_range": "",
            "status": "ready_for_import",
            "real_ai_called": False,
        },
        "fact_check": {
            "sources": [
                {"title": "", "url": "", "used_for": ""},
            ],
            "unresolved_facts": [],
        },
        "voiceover": "",
        "storyboard": [
            {
                "shot_id": "shot-01",
                "start": "00:00",
                "end": "00:05",
                "voiceover": "",
                "visual_intent": "",
                "material_requirements": {
                    "keywords": [],
                    "official_source_url": "",
                },
                "semantic_beats": [
                    {
                        "shot_id": "shot-01-beat-01",
                        "start": "00:00",
                        "end": "00:05",
                        "voiceover": "",
                        "semantic_role": "hook",
                        "semantic_group": "opening",
                        "viewer_takeaway": "",
                        "visual_intent": "",
                        "subject": "",
                        "action": "",
                        "location": "",
                        "time": "",
                        "composition": "",
                        "visual_type": "",
                        "sound_notes": "",
                        "source_url": "",
                        "material_requirements": {
                            "keywords": [],
                            "official_source_url": "",
                        },
                        "asset_keywords": [],
                        "source_priority": [],
                        "forbidden_items": [],
                        "acceptance_conditions": [],
                    }
                ],
                "source_priority": ["官方录屏", "信息动效", "Pexels视频", "备选素材"],
                "backup_visual": "",
                "failure_fallback": "",
            }
        ],
        "subtitles": [{"start": "00:00", "end": "00:05", "text": ""}],
        "title_options": [{"title": "", "type": ""}],
        "cover_copy": [{"text": ""}],
        "publish_copy": {"通用": ""},
        "quality_self_check": {
            "plain_language": False,
            "judgement": "",
            "collect_value": "",
            "risk_level": "",
            "risk_notes": [],
            "banned_expression_check": [],
        },
        "next_material_brief": [
            {
                "shot_id": "shot-01",
                "material_keywords": [],
                "source_priority": [],
                "matching_reason": "",
            }
        ],
    }


def _render_prompt(job_dir: Path) -> str:
    return "\n".join(
        [
            "# Auto Video Engine 脚本工厂任务",
            "",
            "你现在不是在当前主聊天里随便改一句文案，而是在新建一个专门 Codex 对话里完成脚本生产包。",
            "",
            "## 输入文件",
            "",
            f"- 任务上下文：`{job_dir / CONTEXT_FILE}`",
            f"- 输出模板：`{job_dir / TEMPLATE_FILE}`",
            "",
            "## 必须输出",
            "",
            f"- 只在同一个任务目录写入 `{RESULT_FILE}`。",
            "- 不要修改代码、数据库、配置、测试或其他项目文件。",
            "- 不要调用外部 AI API。",
            "- 允许网页核查事实；用到的 URL 必须写进 `fact_check.sources`。",
            "- 不确定的事实写进 `fact_check.unresolved_facts`，不要硬编。",
            "",
            "## 输出要求",
            "",
            "- `voiceover` 是完整口播稿。",
            "- `storyboard` 每个镜头必须有素材来源优先级、备用画面、失败兜底。",
            "- `semantic_beats` 按观众理解任务拆分，不得按标点机械切镜；每个 beat 使用稳定 shot_id。",
            "- 每个 semantic beat 必须填写不重叠的 start/end、semantic_role、viewer_takeaway、画面主体、动作、地点、时间、构图、visual_type、sound_notes。",
            "- 每个 semantic beat 必须填写 forbidden_items 和 acceptance_conditions；证据镜头还必须写 source_url。",
            "- `next_material_brief` 要能直接变成素材任务。",
            "- `quality_self_check` 要说明大白话、判断、收藏价值和风险。",
            "",
            "完成后只保存结果文件，不需要在对话里长篇解释。",
        ]
    )


def _validate_factory_result(
    payload: Any,
    *,
    expected_pipeline_id: int,
    expected_job_id: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ScriptWorkOrderValidationError("导入内容必须是 JSON 对象。")
    voiceover = str(payload.get("voiceover") or "").strip()
    if not voiceover:
        raise ScriptWorkOrderValidationError("口播稿不能为空。")
    meta = _optional_dict(payload.get("meta"), "元信息")
    if meta.get("pipeline_id") != expected_pipeline_id:
        raise ScriptWorkOrderValidationError("结果 meta.pipeline_id 与当前项目不一致。")
    if meta.get("job_id") != expected_job_id:
        raise ScriptWorkOrderValidationError("结果 meta.job_id 与当前脚本任务不一致。")

    next_material_brief = _optional_list_of_dicts(
        payload.get("next_material_brief"),
        "素材任务 brief",
    )
    brief_by_shot = {
        str(item.get("shot_id", "") or ""): item for item in next_material_brief
    }
    storyboard = _normalize_storyboard(
        _required_list_of_dicts(payload.get("storyboard"), "分镜脚本"),
        brief_by_shot,
    )
    subtitles = _required_list_of_dicts(payload.get("subtitles"), "字幕内容")
    fact_check = _optional_dict(payload.get("fact_check"), "事实核查")
    fact_check["sources"] = _optional_list_of_dicts(
        fact_check.get("sources"),
        "事实来源",
    )
    fact_check["unresolved_facts"] = _optional_list(
        fact_check.get("unresolved_facts"),
        "未解决事实",
    )
    return {
        "meta": meta,
        "fact_check": fact_check,
        "voiceover": voiceover,
        "storyboard": storyboard,
        "subtitles": subtitles,
        "title_options": _optional_list_of_dicts(payload.get("title_options"), "标题"),
        "cover_copy": _optional_list_of_dicts(payload.get("cover_copy"), "封面文案"),
        "publish_copy": _optional_dict(payload.get("publish_copy"), "发布文案"),
        "quality_self_check": _optional_dict(
            payload.get("quality_self_check"),
            "脚本自检",
        ),
        "next_material_brief": next_material_brief,
    }


def _normalize_storyboard(
    storyboard: list[dict[str, Any]],
    brief_by_shot: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(storyboard, start=1):
        shot_id = str(item.get("shot_id") or f"shot-{index:02d}")
        brief = brief_by_shot.get(shot_id, {})
        visual = str(
            item.get("recommended_visual") or item.get("visual_intent") or ""
        ).strip()
        if not visual:
            raise ScriptWorkOrderValidationError(f"第 {index} 个分镜缺少画面意图。")
        material_requirements = _optional_dict(
            item.get("material_requirements"),
            "素材需求",
        )
        keywords = _string_list(
            material_requirements.get("keywords")
            or brief.get("material_keywords")
            or item.get("asset_keywords")
        )
        source_priority = _string_list(
            item.get("source_priority") or brief.get("source_priority")
        )
        normalized_item = {
            "shot_id": shot_id,
            "start": str(item.get("start") or ""),
            "end": str(item.get("end") or ""),
            "voiceover": str(item.get("voiceover") or ""),
            "recommended_visual": visual,
            "visual_intent": visual,
            "material_requirements": material_requirements,
            "asset_keywords": keywords,
            "source_priority": source_priority,
            "backup_visual": str(item.get("backup_visual") or ""),
            "failure_fallback": str(item.get("failure_fallback") or ""),
            "matching_reason": str(brief.get("matching_reason") or ""),
            "official_source_url": str(
                material_requirements.get("official_source_url") or ""
            ),
        }
        normalized_item.update(_semantic_extras(item))
        if "semantic_beats" in item:
            normalized_item["semantic_beats"] = _normalize_semantic_beats(
                item.get("semantic_beats"),
                storyboard_index=index,
            )
        normalized.append(normalized_item)
    return normalized


_SEMANTIC_TEXT_FIELDS = (
    "semantic_role",
    "semantic_group",
    "beat_type",
    "beat_group",
    "role",
    "viewer_takeaway",
    "subject",
    "action",
    "location",
    "time",
    "time_context",
    "composition",
    "visual_type",
    "sound_notes",
    "source_url",
    "source_page_url",
    "narration",
    "text",
)


def _semantic_extras(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in _SEMANTIC_TEXT_FIELDS:
        if key in item:
            result[key] = str(item.get(key) or "").strip()
    for key in ("forbidden_items", "acceptance_conditions"):
        if key in item:
            result[key] = _string_list(item.get(key))
    if "needs_info_motion" in item:
        result["needs_info_motion"] = bool(item.get("needs_info_motion"))
    return result


def _normalize_semantic_beats(
    value: Any,
    *,
    storyboard_index: int,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ScriptWorkOrderValidationError(
            f"第 {storyboard_index} 个分镜的 semantic_beats 必须是非空列表。"
        )
    result: list[dict[str, Any]] = []
    for beat_index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ScriptWorkOrderValidationError(
                f"第 {storyboard_index} 个分镜的第 {beat_index} 个 semantic beat 必须是对象。"
            )
        beat: dict[str, Any] = {}
        for key in (
            "shot_id",
            "start",
            "end",
            "voiceover",
            "recommended_visual",
            "visual_intent",
            "backup_visual",
            "failure_fallback",
            "official_source_url",
        ):
            if key in raw:
                beat[key] = str(raw.get(key) or "").strip()
        beat.update(_semantic_extras(raw))
        for key in ("asset_keywords", "source_priority"):
            if key in raw:
                beat[key] = _string_list(raw.get(key))
        if "material_requirements" in raw:
            beat["material_requirements"] = _optional_dict(
                raw.get("material_requirements"),
                f"第 {storyboard_index} 个分镜的第 {beat_index} 个 semantic beat 素材需求",
            )
        result.append(beat)
    return result


def _required_list_of_dicts(value: Any, label: str) -> list[dict[str, Any]]:
    items = _optional_list_of_dicts(value, label)
    if not items:
        raise ScriptWorkOrderValidationError(f"{label}不能为空。")
    return items


def _optional_list_of_dicts(value: Any, label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ScriptWorkOrderValidationError(f"{label}必须是列表。")
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ScriptWorkOrderValidationError(f"{label}里的每一项都必须是对象。")
        items.append(dict(item))
    return items


def _optional_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ScriptWorkOrderValidationError(f"{label}必须是列表。")
    return list(value)


def _optional_dict(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ScriptWorkOrderValidationError(f"{label}必须是 JSON 对象。")
    return dict(value)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _acceptance_block_reason(
    result_payload: dict[str, Any],
    quality: dict[str, Any],
) -> str:
    fact_check = _optional_dict(result_payload.get("fact_check"), "事实核查")
    sources = _optional_list_of_dicts(fact_check.get("sources"), "事实来源")
    source_urls = [str(item.get("url") or "").strip() for item in sources]
    if not any(source_urls):
        return "事实来源不足：Codex 结果必须至少提供一个可核查 URL。"
    unresolved = _optional_list(fact_check.get("unresolved_facts"), "未解决事实")
    if unresolved:
        return "存在未解决事实，不能进入素材流程。"
    self_check = _optional_dict(result_payload.get("quality_self_check"), "脚本自检")
    if str(self_check.get("risk_level", "")) == "高":
        return "脚本自检为高风险，不能进入素材流程。"
    if not _quality_allows_material_flow(quality):
        return str(quality.get("max_issue") or "脚本质量验收未通过。")
    return ""


def _load_factory_result_payload(
    state: dict[str, Any],
    *,
    expected_factory_root: Path,
    pipeline_id: int,
    job_use: PreparedProjectChildDirectoryUse,
) -> dict[str, Any]:
    expected_job_dir = _state_job_dir(
        state,
        expected_factory_root=expected_factory_root,
        pipeline_id=pipeline_id,
    )
    if job_use.path != expected_job_dir:
        raise ScriptWorkOrderValidationError("脚本工厂任务目录与当前项目身份不一致。")
    result_path = prepared_project_output_path(
        job_use,
        RESULT_FILE,
        must_exist=True,
    )
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ScriptWorkOrderValidationError(
            f"Codex 结果文件不是合法 JSON：{exc.msg}"
        ) from exc
    normalized = _validate_factory_result(
        payload,
        expected_pipeline_id=pipeline_id,
        expected_job_id=str(state.get("job_id") or ""),
    )
    job_use.verify()
    return normalized


def _factory_input_evidence(
    job_use: PreparedProjectChildDirectoryUse,
    *,
    job_id: str,
) -> dict[str, Any]:
    files: dict[str, str] = {}
    for filename in (PROMPT_FILE, CONTEXT_FILE, TEMPLATE_FILE):
        try:
            path = prepared_project_output_path(
                job_use,
                filename,
                must_exist=True,
            )
            files[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, ProjectIdentityError) as exc:
            raise ScriptWorkOrderValidationError(
                f"脚本工厂任务输入证据无法读取：{filename}"
            ) from exc
    job_use.verify()
    return {
        "kind": "script_factory_work_order",
        "job_id": job_id,
        "files": files,
    }


def _script_version_input_evidence(
    task_input_evidence: dict[str, Any],
    *,
    result_hash: str,
) -> dict[str, Any]:
    if len(result_hash) != 64:
        raise ScriptWorkOrderValidationError("脚本工厂结果哈希无效。")
    evidence = copy.deepcopy(task_input_evidence)
    evidence["result_sha256"] = result_hash
    return evidence


def _factory_result_hash(result_payload: dict[str, Any]) -> str:
    return _canonical_sha256(result_payload)


def _canonical_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ScriptWorkOrderValidationError("脚本工厂证据无法生成稳定哈希。") from exc
    return hashlib.sha256(encoded).hexdigest()


def _verify_factory_input_evidence(
    state: dict[str, Any],
    current_input_evidence: dict[str, Any],
) -> None:
    expected = state.get("input_evidence")
    if not isinstance(expected, dict):
        raise ScriptWorkOrderValidationError(
            "旧脚本工厂任务包没有输入证据哈希，请重新生成任务包。"
        )
    if expected != current_input_evidence:
        raise ScriptWorkOrderValidationError(
            "脚本工厂任务输入证据哈希已变化，疑似篡改，请重新生成任务包。"
        )


def _record_factory_state(
    pipeline: ProductionPipeline,
    factory_payload: dict[str, Any],
) -> None:
    step = _step_by_name(pipeline, "生成脚本素材")
    if step is None:
        return
    output = dict(step.output_summary or {})
    if pipeline.script_asset_id is not None:
        output["script_asset_id"] = pipeline.script_asset_id
    output["script_factory"] = dict(factory_payload)
    output["script_work_order"] = dict(factory_payload)
    step.status = "已完成"
    step.error = ""
    step.output_summary = output
    step.ended_at = datetime.now(timezone.utc)


def _commit_factory_state_with_cas(
    session: Session,
    pipeline: ProductionPipeline,
    factory_payload: dict[str, Any],
    *,
    expected_output: dict[str, Any],
    pipeline_status: str,
) -> None:
    """Commit only the short final state change after long validation work."""

    step = _step_by_name(pipeline, "生成脚本素材")
    if step is None:
        raise ScriptWorkOrderValidationError("流程缺少“生成脚本素材”步骤。")
    replacement = copy.deepcopy(expected_output)
    if pipeline.script_asset_id is not None:
        replacement["script_asset_id"] = pipeline.script_asset_id
    replacement["script_factory"] = copy.deepcopy(factory_payload)
    replacement["script_work_order"] = copy.deepcopy(factory_payload)
    with session.no_autoflush:
        step_result = session.execute(
            update(PipelineStep)
            .where(
                PipelineStep.id == step.id,
                PipelineStep.pipeline_id == pipeline.id,
                PipelineStep.output_summary == expected_output,
            )
            .values(
                status="已完成",
                error="",
                output_summary=replacement,
                ended_at=datetime.now(timezone.utc),
            )
            .execution_options(synchronize_session=False)
        )
        pipeline_result = session.execute(
            update(ProductionPipeline)
            .where(ProductionPipeline.id == pipeline.id)
            .values(status=pipeline_status)
            .execution_options(synchronize_session=False)
        )
    if step_result.rowcount == 0:
        session.rollback()
        raise ScriptWorkOrderConflictError(
            "脚本验收提交时检测到并发变化，旧批准未提交，请刷新后重试。"
        )
    if step_result.rowcount != 1 or pipeline_result.rowcount != 1:
        session.rollback()
        raise ScriptWorkOrderCommitUnknownError(
            "脚本验收提交结果未知，禁止进入素材，请刷新后核对。"
        )
    try:
        session.commit()
    except BaseException as exc:
        try:
            session.rollback()
        except BaseException:
            pass
        raise ScriptWorkOrderCommitUnknownError(
            "脚本验收提交结果未知，禁止进入素材，请刷新后核对。"
        ) from exc
    session.expire_all()


_RESTORE_DOWNSTREAM_STEP_NAMES = frozenset(
    {
        "生成素材任务",
        "生成 Codex 图文任务",
        "生成数字人字段",
        "生成剪辑清单",
        "导出 script.md",
        "导出 shotlist.csv",
        "导出 edit_manifest.json",
    }
)


def _capture_restore_downstream_steps(
    pipeline: ProductionPipeline,
) -> dict[int, dict[str, Any]]:
    return {
        step.id: {
            "name": step.name,
            "status": step.status,
            "error": step.error,
            "output_summary": copy.deepcopy(step.output_summary or {}),
            "started_at": step.started_at,
            "ended_at": step.ended_at,
        }
        for step in pipeline.steps
        if step.name in _RESTORE_DOWNSTREAM_STEP_NAMES
    }


def _commit_restore_state_with_cas(
    session: Session,
    pipeline: ProductionPipeline,
    factory_payload: dict[str, Any],
    *,
    expected_output: dict[str, Any],
    script: ScriptAsset,
    expected_script_asset_values: dict[str, Any],
    replacement_script_asset_values: dict[str, Any],
    expected_downstream_steps: dict[int, dict[str, Any]],
    replacement_downstream_steps: dict[int, dict[str, Any]],
    material_contract_changed: bool,
    pipeline_status: str,
) -> None:
    """Atomically restore ScriptAsset, script pointer and downstream states.

    Snapshot creation and hashing stay outside this gate.  Only these final
    compare-and-swap updates and the SQLite commit happen inside it.
    """

    step = _step_by_name(pipeline, "生成脚本素材")
    if step is None:
        raise ScriptWorkOrderValidationError("流程缺少“生成脚本素材”步骤。")
    replacement_output = copy.deepcopy(expected_output)
    replacement_output["script_asset_id"] = script.id
    replacement_output["script_factory"] = copy.deepcopy(factory_payload)
    replacement_output["script_work_order"] = copy.deepcopy(factory_payload)
    expected_pipeline_status = pipeline.status
    downstream_by_id = {item.id: item for item in pipeline.steps}

    try:
        with session.no_autoflush:
            script_result = session.execute(
                update(ScriptAsset)
                .where(
                    ScriptAsset.id == script.id,
                    ScriptAsset.topic_idea_id == script.topic_idea_id,
                    ScriptAsset.voiceover
                    == expected_script_asset_values["voiceover"],
                    ScriptAsset.storyboard
                    == expected_script_asset_values["storyboard"],
                    ScriptAsset.subtitles
                    == expected_script_asset_values["subtitles"],
                    ScriptAsset.animation_prompts
                    == expected_script_asset_values["animation_prompts"],
                    ScriptAsset.title_options
                    == expected_script_asset_values["title_options"],
                    ScriptAsset.cover_copy
                    == expected_script_asset_values["cover_copy"],
                    ScriptAsset.publish_copy
                    == expected_script_asset_values["publish_copy"],
                    ScriptAsset.edit_checklist
                    == expected_script_asset_values["edit_checklist"],
                )
                .values(**replacement_script_asset_values)
                .execution_options(synchronize_session=False)
            )
            step_result = session.execute(
                update(PipelineStep)
                .where(
                    PipelineStep.id == step.id,
                    PipelineStep.pipeline_id == pipeline.id,
                    PipelineStep.output_summary == expected_output,
                )
                .values(
                    status="已完成",
                    error="",
                    output_summary=replacement_output,
                    ended_at=datetime.now(timezone.utc),
                )
                .execution_options(synchronize_session=False)
            )

            downstream_results = []
            for step_id, expected in expected_downstream_steps.items():
                replacement = replacement_downstream_steps.get(step_id)
                if replacement is None or replacement == expected:
                    continue
                if expected["name"] == "生成素材任务" and material_contract_changed:
                    # This whole-step CAS already ran inside the same transaction.
                    continue
                step_model = downstream_by_id.get(step_id)
                if step_model is None:
                    raise ScriptWorkOrderCommitUnknownError(
                        "脚本恢复时下游步骤集合发生变化，必须重新核对。"
                    )
                session.expire(step_model)
                downstream_results.append(
                    session.execute(
                        update(PipelineStep)
                        .where(
                            PipelineStep.id == step_id,
                            PipelineStep.pipeline_id == pipeline.id,
                            PipelineStep.status == expected["status"],
                            PipelineStep.error == expected["error"],
                            PipelineStep.output_summary
                            == expected["output_summary"],
                            PipelineStep.started_at == expected["started_at"],
                            PipelineStep.ended_at == expected["ended_at"],
                        )
                        .values(
                            status=replacement["status"],
                            error=replacement["error"],
                            output_summary=replacement["output_summary"],
                            started_at=replacement["started_at"],
                            ended_at=replacement["ended_at"],
                        )
                        .execution_options(synchronize_session=False)
                    )
                )

            pipeline_result = session.execute(
                update(ProductionPipeline)
                .where(
                    ProductionPipeline.id == pipeline.id,
                    ProductionPipeline.script_asset_id == script.id,
                    ProductionPipeline.status == expected_pipeline_status,
                )
                .values(status=pipeline_status)
                .execution_options(synchronize_session=False)
            )
    except (ScriptWorkOrderValidationError, ScriptWorkOrderCommitUnknownError):
        session.rollback()
        raise
    except BaseException as exc:
        try:
            session.rollback()
        except BaseException:
            pass
        raise ScriptWorkOrderCommitUnknownError(
            "脚本恢复提交结果未知，禁止继续进入素材，请刷新后核对。"
        ) from exc

    known_results = [script_result, step_result, pipeline_result, *downstream_results]
    if any(result.rowcount == 0 for result in known_results):
        session.rollback()
        raise ScriptWorkOrderConflictError(
            "脚本恢复提交时检测到并发变化，当前批准未撤销，请刷新后重试。"
        )
    if any(result.rowcount != 1 for result in known_results):
        session.rollback()
        raise ScriptWorkOrderCommitUnknownError(
            "脚本恢复提交结果未知，禁止继续进入素材，请刷新后核对。"
        )
    try:
        session.commit()
    except BaseException as exc:
        try:
            session.rollback()
        except BaseException:
            pass
        raise ScriptWorkOrderCommitUnknownError(
            "脚本恢复提交结果未知，禁止继续进入素材，请刷新后核对。"
        ) from exc
    session.expire_all()


def _current_factory_state(pipeline: ProductionPipeline) -> dict[str, Any]:
    step = _step_by_name(pipeline, "生成脚本素材")
    if step is None:
        raise ScriptWorkOrderNotFoundError("流程缺少“生成脚本素材”步骤。")
    output = step.output_summary or {}
    state = output.get("script_factory") or output.get("script_work_order") or {}
    if not isinstance(state, dict) or not state.get("job_dir"):
        raise ScriptWorkOrderValidationError("还没有脚本工厂任务包，请先生成。")
    return dict(state)


def _state_job_dir(
    state: dict[str, Any],
    *,
    expected_factory_root: Path,
    pipeline_id: int,
) -> Path:
    job_id = str(state.get("job_id") or "")
    match = _JOB_ID_PATTERN.fullmatch(job_id)
    if match is None or int(match.group("pipeline_id")) != pipeline_id:
        raise ScriptWorkOrderValidationError("脚本工厂 job_id 与当前项目身份不一致。")
    expected_job_dir = _require_script_factory_path(
        expected_factory_root / job_id,
        action="读取",
    )
    stored_value = str(state.get("job_dir") or "")
    if not stored_value:
        raise ScriptWorkOrderValidationError("脚本工厂任务目录丢失，请重新生成任务包。")
    stored_job_dir = _require_script_factory_path(Path(stored_value), action="读取")
    if stored_job_dir != expected_job_dir:
        raise ScriptWorkOrderValidationError(
            "脚本工厂任务目录与当前项目身份不一致，已拒绝读取。"
        )
    if not expected_job_dir.is_dir():
        raise ScriptWorkOrderValidationError(
            "脚本工厂任务目录不存在或已被替换，请重新生成任务包。"
        )
    return expected_job_dir


def _state_job_directory_use(
    pipeline: ProductionPipeline,
    state: dict[str, Any],
    *,
    project_root: Path,
    expected_factory_root: Path,
) -> PreparedProjectChildDirectoryUse:
    expected_job_dir = _state_job_dir(
        state,
        expected_factory_root=expected_factory_root,
        pipeline_id=pipeline.id,
    )
    try:
        job_use = begin_prepared_project_child_directory_use(
            pipeline,
            project_root=project_root,
            relative_path=(
                Path("素材信息") / FACTORY_DIRNAME / str(state.get("job_id") or "")
            ),
            create=False,
        )
    except (ProjectIdentityError, ValueError) as exc:
        raise ScriptWorkOrderValidationError(
            f"脚本工厂任务目录身份验证失败：{exc}"
        ) from exc
    if job_use.path != expected_job_dir:
        raise ScriptWorkOrderValidationError("脚本工厂任务目录与当前项目身份不一致。")
    return job_use


def _result_from_state(
    pipeline: ProductionPipeline,
    status: str,
    *,
    quality_check: dict[str, Any] | None = None,
    accepted: bool = False,
) -> ScriptWorkOrderResult:
    state = _current_factory_state(pipeline)
    return ScriptWorkOrderResult(
        pipeline_id=pipeline.id,
        script_asset_id=pipeline.script_asset_id,
        status=status,
        job_id=str(state.get("job_id", "")),
        job_dir=str(state.get("job_dir", "")),
        file_path=str(state.get("file_path") or state.get("prompt_path") or ""),
        prompt_path=str(state.get("prompt_path") or state.get("file_path") or ""),
        context_path=str(state.get("context_path", "")),
        template_path=str(state.get("template_path", "")),
        result_path=str(state.get("result_path", "")),
        real_ai_called=bool(state.get("real_ai_called", False)),
        failure_reason=str(state.get("failure_reason", "")),
        next_suggestion=str(state.get("next_suggestion", "")),
        quality_check=quality_check,
        accepted=accepted,
        dispatch_supported=bool(state.get("dispatch_supported", False)),
        thread_id=str(state.get("thread_id", "")),
        manual_prompt=str(state.get("manual_prompt", "")),
    )


def _manual_dispatch_prompt(prompt_path: Path) -> str:
    return "\n".join(
        [
            "复制下面内容到新的 Codex 对话。",
            "",
            f"请读取并执行这个脚本工厂任务：`{prompt_path}`",
            "",
            "限制：不要改代码、不要写 C 盘、不要调用外部 AI API。",
            f"完成后只写结果文件：`{prompt_path.parent / RESULT_FILE}`",
            "",
            prompt_path.read_text(encoding="utf-8"),
        ]
    )


def _step_by_name(pipeline: ProductionPipeline, name: str) -> PipelineStep | None:
    return next((step for step in pipeline.steps if step.name == name), None)


def _quality_allows_material_flow(quality: dict[str, Any]) -> bool:
    return (
        quality.get("risk_level") != "高"
        and quality.get("publish_suggestion") != "不建议发布"
        and int(quality.get("total_score", 0) or 0) >= 60
    )


def _require_script_factory_path(path: Path, *, action: str) -> Path:
    try:
        return require_non_system_drive(path)
    except ValueError as exc:
        drive_name = path.drive.rstrip(":").upper()
        if drive_name:
            reason = f"脚本工厂禁止{action} {drive_name} 盘，请配置非系统盘绝对路径。"
        else:
            reason = f"脚本工厂{action}路径必须是带盘符的非系统盘绝对路径。"
        raise ScriptWorkOrderValidationError(reason) from exc


def _new_job_id(pipeline_id: int) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"pipeline-{pipeline_id}-{stamp}-{uuid4().hex[:8]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
