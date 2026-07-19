import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from threading import Thread

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session, sessionmaker

from app.account_style.service import (
    get_account_style_library,
    reset_account_style_library,
    save_account_style_library,
)
from app.automation.hotspot_service import (
    get_auto_hotspot_state,
    list_all_source_run_statuses,
    list_source_run_statuses,
    run_auto_hotspot_intake,
    run_daily_hotspot_refresh,
    serialize_auto_hotspot_result,
    serialize_source_status,
)
from app.capabilities import build_provider_statuses, build_skill_statuses
from app.content_quality.schemas import ManualQualityCheckRequest
from app.content_quality.service import (
    PipelineScriptNotFoundError,
    QualityCheckPayload,
    check_manual_content,
    check_pipeline_script,
    serialize_quality_check,
)
from app.copywriting_samples.schemas import CopywritingSamplePayload
from app.copywriting_samples.service import (
    CopywritingSampleNotFoundError,
    create_copywriting_sample,
    delete_copywriting_sample,
    get_copywriting_sample,
    list_copywriting_samples,
    serialize_copywriting_sample,
    serialize_copywriting_sample_list,
    update_copywriting_sample,
)
from app.script_engine.service import (
    ScriptEngineSampleNotFoundError,
    create_script_engine_sample,
    delete_script_engine_sample,
    get_script_engine_sample,
    list_script_engine_samples,
    serialize_script_engine_sample,
    serialize_script_engine_sample_list,
    update_script_engine_sample,
)
from app.core.database import (
    SessionLocal,
    engine,
    get_db,
    get_sqlite_database_path,
)
from app.core.config import get_settings, resolve_configured_path
from app.core.database_preflight import audit_database_identity
from app.core.schema_migrations import ensure_schema_ready
from app.pipeline.material_execution_runtime import (
    build_material_execution_runtime,
    material_execution_runtime_lifespan,
)
from app.hotspots.list_service import (
    get_hotspot,
    list_hotspots,
    serialize_hotspot,
    serialize_hotspot_list,
)
from app.models import ProductionPipeline
from app.hotspots.api_schemas import (
    AutoHotspotRunRequest,
    BrowserHotspotRunRequest,
    HotspotFetchRequest,
    ManualHotspotImportRequest,
)
from app.hotspots.browser_collection import (
    PlaywrightBrowserHotspotCollector,
    run_browser_hotspot_collection,
)
from app.hotspots.browser_login import (
    login_sources_from_config,
    open_browser_hotspot_login_browser_process,
)
from app.hotspots.config import load_source_config
from app.hotspots.pipeline import HotspotPipeline
from app.hotspots.schemas import HotspotCandidate, PipelineRunResult
from app.hotspots.sources.factory import build_hotspot_sources
from app.materials.paths import resolve_portable_material_root
from app.materials.end_to_end_video import EndToEndVideoService
from app.pipeline.list_service import list_pipelines, serialize_pipeline_list
from app.pipeline.schemas import (
    BuildProjectRequest,
    ContinuePipelineRequest,
    PipelineVoiceoverRequest,
    QualityApprovalRequest,
    QualityReportRequest,
    ScriptPresentationOptionsRequest,
    ScriptVersionRestoreRequest,
    ManualV1RunRequest,
    ScriptWorkspaceUpdateRequest,
)
from app.pipeline.service import (
    FakeProviderRequiredError,
    PipelineFilteredHotspotError,
    PipelineHotspotNotFoundError,
    PipelineNotFoundError,
    PipelineStepCommitUnknownError,
    PipelineStepConflictError,
    build_project_from_hotspot,
    continue_pipeline,
    get_pipeline_completion_status,
    get_pipeline_status,
    serialize_pipeline_result,
)
from app.pipeline.v1_acceptance_service import (
    run_manual_v1_acceptance,
    serialize_manual_v1_result,
)
from app.pipeline.workspace_service import (
    PipelineWorkspaceNotFoundError,
    get_script_version_detail,
    get_script_workspace,
    serialize_script_workspace,
    update_script_presentation_options,
    update_script_workspace,
)
from app.pipeline.script_versioning import ScriptVersionError
from app.pipeline.script_work_order_service import (
    ScriptWorkOrderCommitUnknownError,
    ScriptWorkOrderConflictError,
    ScriptWorkOrderNotFoundError,
    ScriptWorkOrderReconcileError,
    ScriptWorkOrderValidationError,
    accept_script_work_order,
    build_script_work_order,
    dispatch_script_work_order,
    import_script_work_order_result,
    reconcile_pending_script_version_operations,
    restore_script_version_as_draft,
    serialize_script_version_restore_result,
    serialize_script_work_order_result,
)
from app.pipeline.audio_service import (
    PipelineAudioNotFoundError,
    generate_pipeline_voiceover_audio,
)
from app.pipeline.audio_subtitle_artifact_service import (
    AudioSubtitleArtifactError,
    build_pipeline_audio_subtitle_artifact,
)
from app.pipeline.timeline_media_service import (
    TimelineMediaServiceError,
    bind_pipeline_audio_subtitle_artifact,
    render_pipeline_timeline_media,
)
from app.pipeline.quality_report_service import (
    QualityApprovalCommitUnknownError,
    QualityApprovalConflictError,
    QualityApprovalReconcileError,
    QualityReportError,
    approve_quality_report,
    list_quality_artifact_candidates,
    stream_quality_artifact,
    write_quality_report,
)
from app.pipeline.quality_contract_service import (
    QualityContractError,
    ensure_default_quality_contract,
)
from app.pipeline.release_package_service import (
    ReleasePackageBlockedError,
    ReleasePackageCommitUnknownError,
    ReleasePackageConflictError,
    ReleasePackageError,
    create_release_package,
)
from app.pipeline.material_package_service import (
    CandidatePoolCommitUnknownError,
    CandidatePoolConflictError,
    MaterialExportCommitUnknownError,
    get_material_package,
    lock_material_candidate,
    reselect_material_candidate,
    reject_material_candidate,
    review_material_candidate_rights,
    rebuild_export_file,
    replace_material_candidate_pool,
    save_material_candidate_pool,
    update_material_task_status,
)
from app.pipeline.material_candidate_contract import (
    CandidateContractError,
    CandidateLockedError,
    CandidateNotFoundError,
)
from app.pipeline.material_candidate_preview_service import (
    CandidatePreviewError,
    CandidatePreviewNotFoundError,
    replace_candidate_with_upload,
    resolve_candidate_preview,
)
from app.pipeline.material_execution_service import (
    MaterialTaskCommitUnknownError,
    MaterialTaskConflictError,
    reconcile_material_task,
    run_next_material_task,
    skip_material_task,
)
from app.pipeline.editing_export_service import (
    EditingExportNotFoundError,
    build_editing_export_package,
    serialize_editing_export_package,
)
from app.pipeline.rewrite_service import (
    ScriptRewriteNotFoundError,
    rewrite_script_part,
    serialize_rewrite_result,
)
from app.publishing.schemas import (
    VideoPostCreateRequest,
    VideoPostUpdateRequest,
    VideoSnapshotCreateRequest,
)
from app.publishing.service import (
    PipelineNotFoundError as PublishingPipelineNotFoundError,
    VideoPostNotFoundError,
    add_video_snapshot,
    create_video_post,
    get_video_post,
    list_video_posts,
    serialize_video_post,
    serialize_video_post_list,
    update_video_post,
)
from app.reviews.service import (
    get_video_review,
    list_video_reviews,
    serialize_video_review,
    serialize_video_review_list,
)
from app.reviews.weekly_service import build_weekly_review_report
from app.seed.service import clear_demo_data, seed_demo_data
from app.today.service import (
    TodayTaskNotDismissibleError,
    TodayTaskNotFoundError,
    get_today_tasks,
    mark_today_task_done,
    serialize_today_dashboard,
)
from app.topic_ideas.service import (
    FilteredHotspotError,
    HotspotNotFoundError,
    generate_topic_ideas_for_hotspot,
    serialize_topic_idea,
)
from app.topic_ideas.filter_settings import (
    get_topic_filter_settings,
    save_topic_filter_settings,
)
from app.topic_ideas.pool_service import (
    get_topic_idea,
    list_topic_ideas,
    select_topic_idea,
    set_topic_event_selection,
    serialize_topic_idea_list,
    serialize_topic_idea_pool_item,
)
from app.topic_ideas.local_generator import (
    compose_collection_from_events,
    refresh_recommendation_buckets,
    update_topic_recommendation_bucket,
)


def _build_internal_material_execution_runtime():
    """Assemble the route-free owner runtime from server-owned D: settings."""

    settings = get_settings()
    runtime_session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )
    return build_material_execution_runtime(
        engine=engine,
        session_factory=runtime_session_factory,
        material_root=resolve_configured_path(settings.auto_material_root_dir),
        project_root=resolve_configured_path(settings.project_root_dir),
    )


def _run_startup_script_version_recovery() -> dict:
    """Reconcile D-only ScriptVersion intents before requests can continue."""

    settings = get_settings()
    db = SessionLocal()
    try:
        return reconcile_pending_script_version_operations(
            db,
            project_root=resolve_configured_path(settings.project_root_dir),
        )
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        database_path = get_sqlite_database_path(engine)
    except ValueError as exc:
        raise RuntimeError(f"生产运行数据库配置无效，启动已停止：{exc}") from exc
    preflight = audit_database_identity(database_path)
    if preflight.blockers:
        raise RuntimeError(
            "数据库启动预检未通过。"
            f"规范路径：{database_path}；"
            "问题：" + "；".join(preflight.blockers)
        )
    for warning in preflight.warnings:
        warnings.warn(f"数据库预检警告：{warning}", RuntimeWarning, stacklevel=2)
    ensure_schema_ready(database_path)
    script_version_recovery_report = _run_startup_script_version_recovery()
    for blocker in script_version_recovery_report.get("blocked", []):
        warnings.warn(
            "脚本版本启动对账仍有阻断："
            f"pipeline={blocker.get('pipeline_id')}; "
            f"operation={blocker.get('operation_id') or 'unknown'}; "
            f"reason={blocker.get('reason')}",
            RuntimeWarning,
            stacklevel=2,
        )
    _run_startup_hotspot_check_if_enabled()
    async with material_execution_runtime_lifespan(
        _build_internal_material_execution_runtime
    ) as material_runtime:
        # Internal observability only.  No public/legacy route is switched to
        # this runtime by installing it on app.state.
        app.state.material_execution_runtime = material_runtime
        app.state.material_restart_recovery_report = (
            material_runtime.startup_recovery_report
        )
        app.state.script_version_recovery_report = script_version_recovery_report
        try:
            yield
        finally:
            if hasattr(app.state, "script_version_recovery_report"):
                del app.state.script_version_recovery_report
            if hasattr(app.state, "material_restart_recovery_report"):
                del app.state.material_restart_recovery_report
            if hasattr(app.state, "material_execution_runtime"):
                del app.state.material_execution_runtime


app = FastAPI(title="Auto Video Engine", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": "Auto Video Engine"}


@app.get("/api/settings/runtime-status")
def runtime_settings_status() -> dict:
    """Expose read-only, server-resolved capability and storage truth.

    The settings page must not infer readiness from environment-variable names.
    This endpoint never returns secrets and never performs a network call or a
    write probe; it reports what the current process can safely claim.
    """

    settings = get_settings()

    def path_status(key: str, value: object) -> dict[str, object]:
        try:
            resolved = resolve_configured_path(value)  # type: ignore[arg-type]
            exists = resolved.exists()
            return {
                "key": key,
                "path": str(resolved),
                "drive": resolved.drive.upper(),
                "is_system_drive": resolved.drive.upper() == "C:",
                "exists": exists,
                "parent_exists": resolved.parent.exists(),
                "status": "ready" if exists or resolved.parent.exists() else "missing",
            }
        except (OSError, ValueError, TypeError) as exc:
            return {
                "key": key,
                "path": "",
                "drive": "",
                "is_system_drive": False,
                "exists": False,
                "parent_exists": False,
                "status": "blocked",
                "reason": str(exc),
            }

    provider = str(settings.ai_provider or "fake").strip().lower() or "fake"
    compatible = {
        "openai-compatible",
        "openai",
        "deepseek",
        "kimi",
        "tongyi",
        "qwen",
        "doubao",
    }
    if provider == "fake":
        provider_status = {
            "name": provider,
            "status": "test_only",
            "production_allowed": False,
            "verified": False,
            "reason": "fake provider 仅用于系统检查和隔离测试，不作为生产脚本来源",
            "secret_configured": False,
        }
    elif provider in compatible:
        missing = [
            key
            for key, value in (
                ("AI_BASE_URL", settings.ai_base_url),
                ("AI_API_KEY", settings.ai_api_key),
                ("AI_MODEL", settings.ai_model),
            )
            if not str(value or "").strip()
        ]
        provider_status = {
            "name": provider,
            "status": "configured_unverified" if not missing else "blocked_missing_config",
            # A populated endpoint/key is only configuration evidence.  Keep
            # the production gate closed until an explicit provider smoke
            # check records a verified capability result.
            "production_allowed": False,
            "verified": False,
            "reason": (
                "配置已存在，但尚未通过真实 provider smoke 验证"
                if not missing
                else f"缺少配置：{', '.join(missing)}"
            ),
            "secret_configured": bool(str(settings.ai_api_key or "").strip()),
        }
    else:
        provider_status = {
            "name": provider,
            "status": "blocked_unknown_provider",
            "production_allowed": False,
            "verified": False,
            "reason": "当前 provider 不在允许的适配器白名单内",
            "secret_configured": bool(str(settings.ai_api_key or "").strip()),
        }

    path_values = {
        "DATA_DIR": settings.data_dir,
        "EXPORT_DIR": settings.export_dir,
        "BREAKDOWN_DIR": settings.breakdown_dir,
        "SOURCE_CONFIG_PATH": settings.source_config_path,
        "MATERIAL_ROOT_DIR": settings.material_root_dir,
        "PROJECT_ROOT_DIR": settings.project_root_dir,
        "JIANYING_DOWNLOAD_DIR": settings.jianying_download_dir,
        "CANDIDATE_SCREENSHOT_DIR": settings.candidate_screenshot_dir,
    }
    return {
        "provider": provider_status,
        "providers": build_provider_statuses(settings, provider_status),
        "skills": build_skill_statuses(settings.codex_skills_root_dir),
        "paths": [path_status(key, value) for key, value in path_values.items()],
        "capabilities": {
            "modern_render": {
                "status": "local_real",
                "production_allowed": True,
                "reason": "项目内 TimelineSpec/FFmpeg 草稿可生成，仍需质量门和 Ian 批准",
            },
            "legacy_export": {
                "status": "compatibility_draft",
                "production_allowed": False,
                "reason": "旧一镜一素材链仅保留兼容草稿语义",
            },
            "external_publish": {
                "status": "deferred",
                "production_allowed": False,
                "reason": "系统不自动登录或公开发布到外部平台",
            },
        },
        "storage_policy": {
            "system_drive": "C:",
            "payload_drive_required": "non-system drive (normally D:)",
            "runtime_paths_are_resolved": True,
        },
    }


@app.post("/api/demo-data/seed")
def seed_local_demo_data(db: Session = Depends(get_db)) -> dict:
    return seed_demo_data(
        db,
        material_root=_api_material_root(),
        project_root=_api_project_root(),
    )


@app.delete("/api/demo-data/clear")
def clear_local_demo_data(db: Session = Depends(get_db)) -> dict:
    return clear_demo_data(db)


@app.get("/api/account-style")
def get_account_style(db: Session = Depends(get_db)) -> dict:
    return get_account_style_library(db)


@app.put("/api/account-style")
def save_account_style(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    return save_account_style_library(db, payload)


@app.post("/api/account-style/reset")
def reset_account_style(db: Session = Depends(get_db)) -> dict:
    return reset_account_style_library(db)


@app.get("/api/copywriting-samples")
def list_copywriting_sample_library(
    sample_type: str | None = None,
    scene: str | None = None,
    content_direction: str | None = None,
    tag: str | None = None,
    keyword: str | None = None,
    enabled_only: bool = False,
    key_reference_only: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    return serialize_copywriting_sample_list(
        list_copywriting_samples(
            db,
            sample_type=sample_type,
            scene=scene,
            content_direction=content_direction,
            tag=tag,
            keyword=keyword,
            enabled_only=enabled_only,
            key_reference_only=key_reference_only,
            page=page,
            page_size=page_size,
        )
    )


@app.get("/api/script-engine")
def list_script_engine_library(
    sample_type: str | None = None,
    scene: str | None = None,
    content_direction: str | None = None,
    tag: str | None = None,
    keyword: str | None = None,
    enabled_only: bool = False,
    key_reference_only: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    return serialize_script_engine_sample_list(
        list_script_engine_samples(
            db,
            sample_type=sample_type,
            scene=scene,
            content_direction=content_direction,
            tag=tag,
            keyword=keyword,
            enabled_only=enabled_only,
            key_reference_only=key_reference_only,
            page=page,
            page_size=page_size,
        )
    )


@app.post("/api/script-engine")
def create_script_engine_item(
    request: CopywritingSamplePayload,
    db: Session = Depends(get_db),
) -> dict:
    return serialize_script_engine_sample(create_script_engine_sample(db, request))


@app.get("/api/script-engine/{sample_id}")
def get_script_engine_item(
    sample_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        item = get_script_engine_sample(db, sample_id)
    except ScriptEngineSampleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_script_engine_sample(item)


@app.put("/api/script-engine/{sample_id}")
def update_script_engine_item(
    sample_id: int,
    request: CopywritingSamplePayload,
    db: Session = Depends(get_db),
) -> dict:
    try:
        item = update_script_engine_sample(db, sample_id, request)
    except ScriptEngineSampleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_script_engine_sample(item)


@app.delete("/api/script-engine/{sample_id}")
def delete_script_engine_item(
    sample_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        delete_script_engine_sample(db, sample_id)
    except ScriptEngineSampleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": True, "id": sample_id}


@app.post("/api/copywriting-samples")
def create_copywriting_sample_item(
    request: CopywritingSamplePayload,
    db: Session = Depends(get_db),
) -> dict:
    return serialize_copywriting_sample(create_copywriting_sample(db, request))


@app.get("/api/copywriting-samples/{sample_id}")
def get_copywriting_sample_item(
    sample_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        item = get_copywriting_sample(db, sample_id)
    except CopywritingSampleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_copywriting_sample(item)


@app.put("/api/copywriting-samples/{sample_id}")
def update_copywriting_sample_item(
    sample_id: int,
    request: CopywritingSamplePayload,
    db: Session = Depends(get_db),
) -> dict:
    try:
        item = update_copywriting_sample(db, sample_id, request)
    except CopywritingSampleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_copywriting_sample(item)


@app.delete("/api/copywriting-samples/{sample_id}")
def delete_copywriting_sample_item(
    sample_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        delete_copywriting_sample(db, sample_id)
    except CopywritingSampleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": True, "id": sample_id}


@app.post("/api/content-quality/manual/check")
def check_manual_content_quality(
    request: ManualQualityCheckRequest,
    db: Session = Depends(get_db),
) -> dict:
    record = check_manual_content(
        db,
        QualityCheckPayload(
            checked_text=request.text,
            source_title=request.source_title,
            source_type=request.source_type,
            pipeline_id=request.pipeline_id,
            script_asset_id=request.script_asset_id,
        ),
    )
    return serialize_quality_check(record)


@app.post("/api/content-quality/pipeline/{pipeline_id}/check")
def check_pipeline_content_quality(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        record = check_pipeline_script(db, pipeline_id)
    except PipelineScriptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_quality_check(record)


@app.get("/api/today/tasks")
def get_today_task_dashboard(db: Session = Depends(get_db)) -> dict:
    return serialize_today_dashboard(get_today_tasks(db))


@app.post("/api/today/tasks/{task_id}/complete")
def complete_today_task(task_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        handled_ids = mark_today_task_done(db, task_id)
    except TodayTaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TodayTaskNotDismissibleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task_id, "handled_task_ids": handled_ids}


@app.get("/api/automation/sources/status")
def get_automation_source_statuses(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    statuses = list_all_source_run_statuses(
        db, resolve_configured_path(settings.source_config_path)
    )
    state = get_auto_hotspot_state(db)
    return {
        "items": [serialize_source_status(status) for status in statuses],
        "source_count": len(statuses),
        "archived_expired_count": int(state.get("archived_expired_count") or 0),
        "daily_refresh_date": state.get("daily_refresh_date"),
        "daily_refresh_completed_at": state.get("daily_refresh_completed_at"),
        "manual_import_is_fallback": True,
        "real_ai_called": False,
    }


@app.post("/api/automation/hotspots/run")
def run_automation_hotspots(
    request: AutoHotspotRunRequest = AutoHotspotRunRequest(),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    result = run_auto_hotspot_intake(
        db,
        resolve_configured_path(settings.source_config_path),
        source_names=request.source_names,
        force=request.force,
        generate_topics=request.generate_topics,
        max_hotspots_for_topics=request.max_hotspots_for_topics,
        interval_hours=settings.auto_hotspot_interval_hours,
    )
    return serialize_auto_hotspot_result(result)


@app.post("/api/automation/browser-hotspots/run")
def run_browser_hotspots(
    request: BrowserHotspotRunRequest = BrowserHotspotRunRequest(),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    config = load_source_config(resolve_configured_path(settings.source_config_path))
    names = {name.strip().lower() for name in request.source_names if name.strip()}
    sources = [
        source
        for source in config.browser_hotspot_sources
        if not names
        or source.name.lower() in names
        or any(token in source.name.lower() for token in names)
    ]
    collector = PlaywrightBrowserHotspotCollector(
        profile_dir=resolve_configured_path(settings.browser_hotspot_profile_dir),
        headless=settings.browser_hotspot_headless,
        max_items=settings.browser_hotspot_max_items,
    )
    result = run_browser_hotspot_collection(
        db,
        sources=sources,
        collector=collector,
        csv_path=resolve_configured_path(settings.browser_hotspot_csv_path),
        force=request.force,
    )
    return {
        "source_count": result.source_count,
        "fetched_count": result.fetched_count,
        "saved_count": result.saved_count,
        "duplicate_count": result.duplicate_count,
        "csv_written_count": result.csv_written_count,
        "failed_sources": result.failed_sources,
        "needs_login_sources": result.needs_login_sources,
        "skipped_sources": result.skipped_sources,
        "csv_path": result.csv_path,
        "message": result.message,
        "safety_note": "低频采集；不绕过验证码；不下载视频；登录过期请重新登录 Chrome。",
    }


@app.post("/api/automation/browser-hotspots/login-browser")
def open_browser_hotspots_login_browser() -> dict:
    settings = get_settings()
    config = load_source_config(resolve_configured_path(settings.source_config_path))
    sources = login_sources_from_config(config)
    try:
        launch_result = open_browser_hotspot_login_browser_process(
            profile_dir=resolve_configured_path(settings.browser_hotspot_profile_dir),
            sources=sources,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "message": launch_result["message"],
        "pid": launch_result["pid"],
        "sources": sources,
        "profile_dir": str(resolve_configured_path(settings.browser_hotspot_profile_dir)),
        "real_collection_started": False,
        "note": "这个入口只用于登录，不采集、不下载、不绕过验证码。登录完成后请关闭采集专用 Chrome，再运行采集。",
    }


@app.get("/api/hotspots")
def list_hotspot_pool(
    category: str | None = None,
    source_platform: str | None = None,
    unfiltered_only: bool = False,
    sort_by: str = Query(
        default="topic_score",
        pattern="^(hot_score|topic_score|save_score|risk_score|freshness_score|published_at)$",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    result = list_hotspots(
        db,
        category=category,
        source_platform=source_platform,
        unfiltered_only=unfiltered_only,
        sort_by=sort_by,
        page=page,
        page_size=page_size,
    )
    return serialize_hotspot_list(result)


@app.post("/api/hotspots/import")
def import_manual_hotspot(
    request: ManualHotspotImportRequest,
    db: Session = Depends(get_db),
) -> dict:
    candidate = HotspotCandidate(
        source_platform=request.source_platform or "手动导入",
        source_type="manual",
        title=request.title,
        url=request.url,
        published_at=request.published_at,
        summary=request.summary,
        metrics={"notes": request.notes} if request.notes else {},
        tags=request.tags[:5],
        category=request.category or "其他",
        data_origin="manual",
        origin_note="手动导入备用入口",
    )
    item, created = HotspotPipeline(db).import_manual(candidate)
    return {
        "created": created,
        "message": "手动热点已保存" if created else "重复链接，已使用已有热点",
        "item": serialize_hotspot(get_hotspot(db, item.id)),
    }


@app.post("/api/hotspots/fetch")
def fetch_hotspot_sources(
    request: HotspotFetchRequest = HotspotFetchRequest(),
    db: Session = Depends(get_db),
) -> dict:
    sources = build_hotspot_sources(
        get_settings().source_config_path,
        source_names=request.source_names,
    )
    if not sources:
        result = PipelineRunResult()
    else:
        result = HotspotPipeline(db).run_sources(sources)
    return {
        "source_count": len(sources),
        "fetched_count": result.fetched_count,
        "saved_count": result.saved_count,
        "duplicate_count": result.duplicate_count,
        "filtered_count": result.filtered_count,
        "failed_sources": result.failed_sources,
        "rate_limited_sources": result.rate_limited_sources,
        "message": "抓取完成，失败来源已记录" if result.failed_sources else "抓取完成",
    }


@app.get("/api/pipeline/{pipeline_id}/editing-export")
def get_pipeline_editing_export(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        package = build_editing_export_package(db, pipeline_id, write_files=False)
    except EditingExportNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_editing_export_package(package)


@app.get("/api/pipeline/{pipeline_id}/timeline-spec")
def get_pipeline_timeline_spec(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Expose the current project TimelineSpec without creating a new version."""

    try:
        package = build_editing_export_package(db, pipeline_id, write_files=False)
    except EditingExportNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "pipeline_id": pipeline_id,
        "timeline_spec": package.timeline_spec,
        "timeline": package.timeline,
    }


@app.post("/api/pipeline/{pipeline_id}/editing-export/rebuild")
def rebuild_pipeline_editing_export(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        project_root = _api_project_root()
        # Preflight the reviewed candidate contract and write the current
        # TimelineSpec first.  A Script Factory acceptance deliberately
        # invalidates every downstream legacy export; finish those durable
        # project files here so the modern editing path and the completion
        # policy cannot disagree about whether production is prepared.
        build_editing_export_package(
            db,
            pipeline_id,
            write_files=True,
            project_root=project_root,
        )
        continued = continue_pipeline(
            db,
            pipeline_id,
            material_root=_api_material_root(),
            project_root=project_root,
        )
        if continued.failure_reasons:
            failures = "；".join(
                f"{item['step_name']}：{item['reason']}"
                for item in continued.failure_reasons
            )
            raise HTTPException(
                status_code=409,
                detail=f"剪辑方案已保存，但正式项目导出未完成：{failures}",
            )
        package = build_editing_export_package(
            db,
            pipeline_id,
            write_files=True,
            project_root=project_root,
        )
    except EditingExportNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidateContractError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FakeProviderRequiredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_editing_export_package(package)


@app.post("/api/editing-export/end-to-end-test")
def run_end_to_end_editing_test(request: dict = Body(default_factory=dict)) -> dict:
    topic = str(
        request.get("topic") or "AI 搜索正在改变普通人获取信息的方式"
    ).strip()
    if not topic:
        topic = "AI 搜索正在改变普通人获取信息的方式"
    try:
        return EndToEndVideoService().run_test_export(topic=topic)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"一分钟测试成片生成失败：{exc}") from exc


@app.post("/api/pipeline/{pipeline_id}/finished-video/export")
def export_pipeline_finished_video(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return EndToEndVideoService().run_pipeline_export(db, pipeline_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"项目成片生成失败：{exc}") from exc


@app.get("/api/editing-export/end-to-end-test/latest")
def get_latest_end_to_end_editing_test() -> dict:
    return EndToEndVideoService().latest_report()


@app.get("/api/editing-export/finished-videos")
def list_finished_videos() -> dict:
    return {
        "items": EndToEndVideoService().list_finished_videos(),
        "note": "成片页只展示最终 MP4 管理，不展示脚本、字幕和素材匹配明细。",
    }


@app.get("/api/pipeline/{pipeline_id}/finished-videos")
def list_pipeline_finished_videos(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """List legacy finished-video drafts belonging to one project only."""

    if db.get(ProductionPipeline, pipeline_id) is None:
        raise HTTPException(status_code=404, detail=f"项目流程不存在：{pipeline_id}")
    return {
        "pipeline_id": pipeline_id,
        "items": EndToEndVideoService().list_finished_videos(pipeline_id=pipeline_id),
        "note": "项目成片列表只展示当前项目的兼容草稿；现代成片仍需单独验收。",
    }


@app.get("/api/editing-export/finished-videos/{video_id}/video")
def preview_finished_video(
    video_id: str,
    pipeline_id: int | None = Query(None),
) -> FileResponse:
    try:
        service = EndToEndVideoService()
        service.validate_finished_video_pipeline(video_id, pipeline_id)
        path = service.video_file_path(video_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.post("/api/editing-export/finished-videos/{video_id}/open-folder")
def open_finished_video_folder(
    video_id: str,
    pipeline_id: int | None = Query(None),
) -> dict:
    try:
        service = EndToEndVideoService()
        service.validate_finished_video_pipeline(video_id, pipeline_id)
        return service.open_finished_video_folder(video_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/editing-export/finished-videos/{video_id}/export")
def export_finished_video(
    video_id: str,
    pipeline_id: int | None = Query(None),
) -> dict:
    try:
        service = EndToEndVideoService()
        service.validate_finished_video_pipeline(video_id, pipeline_id)
        return service.export_finished_video(video_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/editing-export/finished-videos/{video_id}")
def delete_finished_video(
    video_id: str,
    pipeline_id: int | None = Query(None),
) -> dict:
    try:
        service = EndToEndVideoService()
        service.validate_finished_video_pipeline(video_id, pipeline_id)
        return service.delete_finished_video(video_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/hotspots/{hotspot_id}")
def get_hotspot_detail(
    hotspot_id: int,
    db: Session = Depends(get_db),
) -> dict:
    item = get_hotspot(db, hotspot_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"热点不存在：{hotspot_id}")
    return serialize_hotspot(item)


@app.post("/api/hotspots/{hotspot_id}/topic-ideas/generate")
def generate_topic_ideas(hotspot_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        result = generate_topic_ideas_for_hotspot(db, hotspot_id)
    except HotspotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FilteredHotspotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "hotspot_id": result.hotspot_id,
        "from_cache": result.from_cache,
        "ideas": [serialize_topic_idea(idea) for idea in result.ideas],
    }


@app.get("/api/topic-filter-settings")
def read_topic_filter_settings(db: Session = Depends(get_db)) -> dict:
    return get_topic_filter_settings(db)


@app.put("/api/topic-filter-settings")
def update_topic_filter_settings(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    try:
        settings = save_topic_filter_settings(db, payload)
        refresh_recommendation_buckets(
            db,
            today_limit=int(settings.get("current_period_limit", 5)),
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return settings


@app.get("/api/topic-ideas")
def list_topic_idea_pool(
    hotspot_id: int | None = None,
    recommendation_bucket: str | None = Query(default=None, pattern="^(today|backup)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    result = list_topic_ideas(
        db,
        hotspot_id=hotspot_id,
        recommendation_bucket=recommendation_bucket,
        page=page,
        page_size=page_size,
    )
    return serialize_topic_idea_list(result)


@app.get("/api/topic-ideas/{topic_idea_id}")
def get_topic_idea_detail(
    topic_idea_id: int,
    db: Session = Depends(get_db),
) -> dict:
    item = get_topic_idea(db, topic_idea_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"候选选题不存在：{topic_idea_id}")
    return serialize_topic_idea_pool_item(item)


@app.post("/api/topic-ideas/{topic_idea_id}/recommendation")
def update_topic_idea_recommendation(
    topic_idea_id: int,
    request: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    bucket = str(request.get("bucket", "backup"))
    item = update_topic_recommendation_bucket(db, topic_idea_id, bucket)
    if item is None:
        raise HTTPException(status_code=404, detail=f"候选选题不存在：{topic_idea_id}")
    pool_item = get_topic_idea(db, topic_idea_id)
    if pool_item is None:
        raise HTTPException(status_code=404, detail=f"候选选题不存在：{topic_idea_id}")
    return serialize_topic_idea_pool_item(pool_item)


@app.post("/api/topic-ideas/{topic_idea_id}/select")
def choose_topic_idea(
    topic_idea_id: int,
    db: Session = Depends(get_db),
) -> dict:
    item = select_topic_idea(db, topic_idea_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"候选选题不存在：{topic_idea_id}")
    return serialize_topic_idea_pool_item(item)


@app.post("/api/topic-ideas/{topic_idea_id}/event-selection")
def update_topic_event_selection(
    topic_idea_id: int,
    request: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    item = set_topic_event_selection(
        db,
        topic_idea_id,
        selected=bool(request.get("selected", True)),
    )
    if item is None:
        raise HTTPException(status_code=404, detail=f"候选事件不存在：{topic_idea_id}")
    return serialize_topic_idea_pool_item(item)


@app.post("/api/topic-ideas/compose-collection")
def compose_topic_idea_collection(
    request: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    raw_ids = request.get("topic_idea_ids", [])
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="topic_idea_ids 必须是列表。")
    try:
        topic_idea_ids = [int(item_id) for item_id in raw_ids]
        collection = compose_collection_from_events(db, topic_idea_ids)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    item = get_topic_idea(db, collection.id)
    if item is None:
        raise HTTPException(status_code=404, detail="本期合集生成失败。")
    return serialize_topic_idea_pool_item(item)


@app.post("/api/topic-ideas/{topic_idea_id}/generate-script")
def generate_topic_idea_script(
    topic_idea_id: int,
    db: Session = Depends(get_db),
) -> dict:
    item = get_topic_idea(db, topic_idea_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"候选选题不存在：{topic_idea_id}")
    try:
        result = build_project_from_hotspot(
            db,
            item.hot_item_id,
            project_name=item.title,
            selected_topic_id=item.id,
            stop_after="创建中文项目目录",
            material_root=_api_material_root(),
            project_root=_api_project_root(),
        )
    except PipelineHotspotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PipelineFilteredHotspotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FakeProviderRequiredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_pipeline_result(result)


@app.post("/api/pipeline/hotspots/{hotspot_id}/build-project")
def build_production_project(
    hotspot_id: int,
    request: BuildProjectRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = build_project_from_hotspot(
            db,
            hotspot_id,
            project_name=request.project_name,
            selected_topic_id=request.selected_topic_id,
            stop_after=request.stop_after,
            material_root=_api_material_root(),
            project_root=_api_project_root(),
        )
    except PipelineHotspotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PipelineFilteredHotspotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FakeProviderRequiredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_pipeline_result(result)


@app.post("/api/pipeline/manual-v1-test")
def run_manual_v1_test(
    request: ManualV1RunRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = run_manual_v1_acceptance(
            db,
            request,
            material_root=_api_material_root(),
            project_root=_api_project_root(),
        )
    except PipelineFilteredHotspotError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"V1 跑通测试失败：{exc}") from exc
    return serialize_manual_v1_result(result)


@app.get("/api/pipeline")
def list_production_pipelines(
    status: str | None = None,
    failed_only: bool = False,
    needs_confirmation_only: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    result = list_pipelines(
        db,
        status=status,
        failed_only=failed_only,
        needs_confirmation_only=needs_confirmation_only,
        page=page,
        page_size=page_size,
    )
    return serialize_pipeline_list(result)


@app.get("/api/pipeline/{pipeline_id}")
def get_production_pipeline(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = get_pipeline_status(db, pipeline_id)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_pipeline_result(result)


@app.get("/api/pipeline/{pipeline_id}/completion-status")
def get_production_pipeline_completion_status(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return get_pipeline_completion_status(db, pipeline_id)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/pipeline/{pipeline_id}/workspace")
def get_pipeline_script_workspace(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = get_script_workspace(
            db,
            pipeline_id,
            project_root=_api_project_root(),
        )
    except PipelineWorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScriptVersionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return serialize_script_workspace(result)


@app.get("/api/pipeline/{pipeline_id}/script-versions/{version_id}")
def get_pipeline_script_version_detail(
    pipeline_id: int,
    version_id: str,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return get_script_version_detail(
            db,
            pipeline_id,
            version_id,
            project_root=_api_project_root(),
        )
    except PipelineWorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScriptVersionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/pipeline/{pipeline_id}/script-versions/{version_id}/restore")
def restore_pipeline_script_version_as_draft(
    pipeline_id: int,
    version_id: str,
    request: ScriptVersionRestoreRequest,
    db: Session = Depends(get_db),
) -> dict:
    project_root = _api_project_root()
    try:
        result = restore_script_version_as_draft(
            db,
            pipeline_id,
            version_id,
            operation_id=request.operation_id,
            source_content_hash=request.source_content_hash,
            source_snapshot_hash=request.source_snapshot_hash,
            expected_current_version_id=request.expected_current_version_id,
            expected_current_content_hash=request.expected_current_content_hash,
            expected_current_snapshot_hash=request.expected_current_snapshot_hash,
            expected_script_state_sha256=request.expected_script_state_sha256,
            project_root=project_root,
        )
        workspace = get_script_workspace(
            db,
            pipeline_id,
            project_root=project_root,
        )
    except ScriptWorkOrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScriptWorkOrderConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ScriptWorkOrderCommitUnknownError, ScriptWorkOrderReconcileError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ScriptVersionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ScriptWorkOrderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "operation": serialize_script_version_restore_result(result),
        "workspace": serialize_script_workspace(workspace),
    }


@app.put("/api/pipeline/{pipeline_id}/workspace/script")
def update_pipeline_script_workspace(
    pipeline_id: int,
    request: ScriptWorkspaceUpdateRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = update_script_workspace(
            db,
            pipeline_id,
            request.model_dump(),
            project_root=_api_project_root(),
        )
    except PipelineWorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScriptVersionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_script_workspace(result)


@app.put("/api/pipeline/{pipeline_id}/workspace/presentation")
def update_pipeline_script_presentation_options(
    pipeline_id: int,
    request: ScriptPresentationOptionsRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = update_script_presentation_options(
            db,
            pipeline_id,
            use_avatar=request.use_avatar,
            project_root=_api_project_root(),
        )
    except PipelineWorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_script_workspace(result)


@app.post("/api/pipeline/{pipeline_id}/script-work-order/build")
def build_pipeline_script_work_order(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = build_script_work_order(
            db,
            pipeline_id,
            project_root=_api_project_root(),
        )
    except ScriptWorkOrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScriptWorkOrderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_script_work_order_result(result)


@app.post("/api/pipeline/{pipeline_id}/script-work-order/dispatch")
def dispatch_pipeline_script_work_order(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = dispatch_script_work_order(
            db,
            pipeline_id,
            project_root=_api_project_root(),
        )
    except ScriptWorkOrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScriptWorkOrderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_script_work_order_result(result)


@app.put("/api/pipeline/{pipeline_id}/script-work-order/import")
def import_pipeline_script_work_order_result(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = import_script_work_order_result(
            db,
            pipeline_id,
            project_root=_api_project_root(),
        )
    except ScriptWorkOrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScriptWorkOrderConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ScriptWorkOrderCommitUnknownError, ScriptWorkOrderReconcileError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ScriptWorkOrderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_script_work_order_result(result)


@app.post("/api/pipeline/{pipeline_id}/script-work-order/accept")
def accept_pipeline_script_work_order(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = accept_script_work_order(
            db,
            pipeline_id,
            project_root=_api_project_root(),
        )
    except ScriptWorkOrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PipelineStepConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PipelineStepCommitUnknownError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ScriptWorkOrderConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ScriptWorkOrderCommitUnknownError, ScriptWorkOrderReconcileError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ScriptWorkOrderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_script_work_order_result(result)


@app.post("/api/pipeline/{pipeline_id}/voiceover/generate")
def generate_pipeline_voiceover(
    pipeline_id: int,
    request: PipelineVoiceoverRequest = PipelineVoiceoverRequest(),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return generate_pipeline_voiceover_audio(
            db,
            pipeline_id,
            provider=request.provider,
        )
    except PipelineAudioNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/pipeline/{pipeline_id}/rewrite")
def rewrite_pipeline_script_part(
    pipeline_id: int,
    request: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = rewrite_script_part(db, pipeline_id, str(request.get("part", "")))
    except ScriptRewriteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_rewrite_result(result)


@app.get("/api/pipeline/{pipeline_id}/material-package")
def get_pipeline_material_package(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return get_material_package(db, pipeline_id)
    except PipelineWorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put(
    "/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidate-pool",
)
@app.put("/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidates")
def save_pipeline_material_candidate_pool(
    pipeline_id: int,
    shot_id: str,
    request: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return save_material_candidate_pool(
            db,
            pipeline_id,
            shot_id,
            request.get("candidates"),
        )
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidateLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CandidatePoolCommitUnknownError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except CandidatePoolConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CandidateContractError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidate-pool/{candidate_id}/lock",
)
@app.post(
    "/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidates/{candidate_id}/lock",
)
def lock_pipeline_material_candidate(
    pipeline_id: int,
    shot_id: str,
    candidate_id: str,
    request: dict = Body(default_factory=dict),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return lock_material_candidate(
            db,
            pipeline_id,
            shot_id,
            candidate_id,
            reason=str(request.get("reason", "")),
        )
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidateLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CandidatePoolCommitUnknownError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except CandidatePoolConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CandidateContractError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidate-pool/{candidate_id}/reselect",
)
def reselect_pipeline_material_candidate(
    pipeline_id: int,
    shot_id: str,
    candidate_id: str,
    request: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    """Explicitly move a reviewed lock; ordinary replace remains forbidden."""

    try:
        if set(request) != {"reason", "expected_revision"}:
            raise CandidateContractError(
                "reselect request requires only reason and expected_revision"
            )
        return reselect_material_candidate(
            db,
            pipeline_id,
            shot_id,
            candidate_id,
            expected_revision=request.get("expected_revision"),
            reason=str(request.get("reason", "")),
            actor="Ian",
        )
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidatePoolCommitUnknownError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (CandidatePoolConflictError, CandidateLockedError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CandidateContractError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidate-pool/{candidate_id}/rights-review",
)
def review_pipeline_material_candidate_rights(
    pipeline_id: int,
    shot_id: str,
    candidate_id: str,
    request: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    """Review one persisted provider record; base licence facts stay server-owned."""

    expected_fields = {
        "expected_revision",
        "status",
        "intended_use",
        "identifiable_people",
        "brands_or_logos",
        "property_or_architecture",
        "music_or_third_party_audio",
        "third_party_clearance",
        "restriction_notes",
    }
    try:
        if set(request) != expected_fields:
            raise CandidateContractError(
                "rights review request fields are invalid"
            )
        return review_material_candidate_rights(
            db,
            pipeline_id,
            shot_id,
            candidate_id,
            expected_revision=request.get("expected_revision"),
            status=request.get("status"),
            intended_use=request.get("intended_use"),
            identifiable_people=request.get("identifiable_people"),
            brands_or_logos=request.get("brands_or_logos"),
            property_or_architecture=request.get("property_or_architecture"),
            music_or_third_party_audio=request.get(
                "music_or_third_party_audio"
            ),
            third_party_clearance=request.get("third_party_clearance"),
            restriction_notes=request.get("restriction_notes"),
            actor="Ian",
        )
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidatePoolCommitUnknownError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (CandidatePoolConflictError, CandidateLockedError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CandidateContractError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidate-pool/{candidate_id}/reject",
)
@app.post(
    "/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidates/{candidate_id}/reject",
)
def reject_pipeline_material_candidate(
    pipeline_id: int,
    shot_id: str,
    candidate_id: str,
    request: dict = Body(default_factory=dict),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return reject_material_candidate(
            db,
            pipeline_id,
            shot_id,
            candidate_id,
            reason=str(request.get("reason", "")),
        )
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidateLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CandidatePoolCommitUnknownError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except CandidatePoolConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CandidateContractError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidate-pool/{candidate_id}/preview"
)
def preview_pipeline_material_candidate(
    pipeline_id: int,
    shot_id: str,
    candidate_id: str,
    db: Session = Depends(get_db),
) -> FileResponse:
    """Serve a validated local preview without exposing its physical path."""

    try:
        preview = resolve_candidate_preview(db, pipeline_id, shot_id, candidate_id)
    except CandidatePreviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (CandidatePreviewError, CandidateContractError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(
        preview.path,
        media_type=preview.media_type,
        filename=preview.filename,
        content_disposition_type="inline",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/pipeline/{pipeline_id}/voiceover/artifact")
def build_pipeline_voiceover_artifact(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Create one measured, append-only WAV/SRT artifact version.

    This route is intentionally separate from the legacy fixed-name audio
    endpoint.  It returns project-relative references only and does not mark a
    pipeline complete or publish a video.
    """

    try:
        artifact = build_pipeline_audio_subtitle_artifact(
            db,
            pipeline_id,
            project_root=_api_project_root(),
        )
        pipeline = db.get(ProductionPipeline, pipeline_id)
        if pipeline is None or not pipeline.project_dir:
            raise AudioSubtitleArtifactError("项目目录不存在，无法返回音频素材引用")
        return artifact.to_public_dict(project_dir=Path(pipeline.project_dir))
    except AudioSubtitleArtifactError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipeline/{pipeline_id}/timeline-media/bind")
def bind_pipeline_timeline_media(
    pipeline_id: int,
    request: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    """Bind an explicit measured audio/subtitle version to TimelineSpec.

    This is an append-only draft operation.  It never marks completion and it
    never calls the legacy finished-video exporter.
    """

    artifact_id = str(request.get("artifact_id") or "").strip()
    if not artifact_id:
        raise HTTPException(status_code=400, detail="artifact_id is required")
    try:
        binding = bind_pipeline_audio_subtitle_artifact(
            db,
            pipeline_id,
            artifact_id,
            project_root=_api_project_root(),
            timeline_version_id=(
                str(request.get("timeline_version_id") or "").strip() or None
            ),
        )
        pipeline = db.get(ProductionPipeline, pipeline_id)
        if pipeline is None or not pipeline.project_dir:
            raise TimelineMediaServiceError("pipeline project directory is missing")
        return binding.to_dict(project_dir=Path(pipeline.project_dir))
    except TimelineMediaServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipeline/{pipeline_id}/timeline-media/render")
def render_pipeline_timeline_media_draft(
    pipeline_id: int,
    request: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    """Render one isolated full-media draft from an explicit artifact.

    The response is a project-relative technical result.  Publishing and
    completion approval remain separate gates.
    """

    artifact_id = str(request.get("artifact_id") or "").strip()
    if not artifact_id:
        raise HTTPException(status_code=400, detail="artifact_id is required")
    try:
        run = render_pipeline_timeline_media(
            db,
            pipeline_id,
            artifact_id,
            project_root=_api_project_root(),
            timeline_version_id=(
                str(request.get("timeline_version_id") or "").strip() or None
            ),
        )
        pipeline = db.get(ProductionPipeline, pipeline_id)
        if pipeline is None or not pipeline.project_dir:
            raise TimelineMediaServiceError("pipeline project directory is missing")
        return run.to_dict(project_dir=Path(pipeline.project_dir))
    except TimelineMediaServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/pipeline/{pipeline_id}/quality-report/candidates")
def list_pipeline_quality_artifact_candidates(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """List recent modern renderer MP4s using project-relative references."""

    try:
        candidates = list_quality_artifact_candidates(
            db,
            pipeline_id,
            project_root=_api_project_root(),
        )
        return {
            "pipeline_id": pipeline_id,
            "items": [item.to_dict() for item in candidates],
            "publish_supported": False,
        }
    except QualityReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/pipeline/{pipeline_id}/quality-contract")
def get_pipeline_quality_contract(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Read or safely freeze the immutable R1 contract for one project."""

    try:
        return ensure_default_quality_contract(
            db,
            pipeline_id,
            project_root=_api_project_root(),
        ).to_dict()
    except QualityContractError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/pipeline/{pipeline_id}/quality-report/preview")
def preview_pipeline_quality_artifact(
    pipeline_id: int,
    artifact_relative_path: str = Query(..., min_length=1, max_length=400),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Stream one guarded project-local MP4 for the human review gate."""

    try:
        filename, stream = stream_quality_artifact(
            db,
            pipeline_id,
            artifact_relative_path,
            project_root=_api_project_root(),
        )
    except QualityReportError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # The browser does not need a filename for playback.  Omitting it also
    # avoids reflecting an untrusted project filename into Content-Disposition.
    _ = filename
    return StreamingResponse(
        stream,
        media_type="video/mp4",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/pipeline/{pipeline_id}/quality-report")
def create_pipeline_quality_report(
    pipeline_id: int,
    request: QualityReportRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Write an immutable five-gate report for one project-local MP4."""

    artifact_relative_path = request.artifact_relative_path.strip()
    try:
        report = write_quality_report(
            db,
            pipeline_id,
            artifact_relative_path,
            manual_gates=(
                {
                    name: gate.model_dump()
                    for name, gate in (request.gates or {}).items()
                }
                or None
            ),
            quality_contract_review=(
                request.quality_contract_review.model_dump()
                if request.quality_contract_review is not None
                else None
            ),
            project_root=_api_project_root(),
        )
        return report.to_dict()
    except QualityReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipeline/{pipeline_id}/quality-report/approve")
def approve_pipeline_quality_report(
    pipeline_id: int,
    request: QualityApprovalRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Create completion evidence only after an explicit Ian approval."""

    try:
        result = approve_quality_report(
            db,
            pipeline_id,
            artifact_relative_path=request.artifact_relative_path.strip(),
            quality_report_relative_path=request.quality_report_relative_path.strip(),
            approved_by=request.approved_by.strip(),
            approved_at=(request.approved_at.strip() if request.approved_at else None),
            project_root=_api_project_root(),
        )
        return result.to_dict()
    except QualityApprovalConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except QualityApprovalCommitUnknownError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except QualityApprovalReconcileError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except QualityReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipeline/{pipeline_id}/release-package")
def create_pipeline_release_package(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Promote an Ian-approved project artifact into a local release package.

    This is deliberately separate from the historical finished-video export
    route.  It never publishes to an external platform and returns only
    project-relative paths and hashes.
    """

    try:
        result = create_release_package(
            db,
            pipeline_id,
            project_root=_api_project_root(),
        )
        return result.to_dict()
    except ReleasePackageBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReleasePackageConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReleasePackageCommitUnknownError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ReleasePackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidate-pool/{candidate_id}/replace"
)
def replace_pipeline_material_candidate(
    pipeline_id: int,
    shot_id: str,
    candidate_id: str,
    preview: UploadFile = File(...),
    replacement_candidate_id: str = Form(""),
    source: str = Form(...),
    url: str = Form(""),
    rights: str = Form(...),
    evidence_kind: str = Form(...),
    reason: str = Form(...),
    db: Session = Depends(get_db),
) -> dict:
    """Replace one unlocked candidate with an operator-uploaded preview."""

    try:
        return replace_candidate_with_upload(
            db,
            pipeline_id,
            shot_id,
            candidate_id,
            upload=preview.file,
            filename=preview.filename or "",
            content_type=preview.content_type,
            replacement_candidate_id=replacement_candidate_id,
            source=source,
            url=url,
            rights=rights,
            evidence_kind=evidence_kind,
            reason=reason,
        )
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidatePreviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidatePoolCommitUnknownError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except CandidatePoolConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (CandidatePreviewError, CandidateLockedError, CandidateContractError) as exc:
        raise HTTPException(status_code=409 if isinstance(exc, CandidateLockedError) else 400, detail=str(exc)) from exc
    finally:
        try:
            preview.file.close()
        except Exception:
            pass


@app.post("/api/pipeline/{pipeline_id}/exports/rebuild")
def rebuild_pipeline_export(
    pipeline_id: int,
    request: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return rebuild_export_file(db, pipeline_id, str(request.get("export_type", "")))
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MaterialExportCommitUnknownError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/status")
def update_pipeline_material_task_status(
    pipeline_id: int,
    shot_id: str,
    request: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return update_material_task_status(
            db,
            pipeline_id,
            shot_id,
            str(request.get("status", "")),
        )
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidatePoolCommitUnknownError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (CandidatePoolConflictError, CandidateContractError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipeline/{pipeline_id}/material-tasks/run-next")
def run_next_pipeline_material_task(
    pipeline_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return run_next_material_task(db, pipeline_id)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MaterialTaskCommitUnknownError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MaterialTaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/reconcile-and-retry")
def reconcile_pipeline_material_task(
    pipeline_id: int,
    shot_id: str,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return reconcile_material_task(db, pipeline_id, shot_id)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MaterialTaskCommitUnknownError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MaterialTaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/skip")
def skip_pipeline_material_task(
    pipeline_id: int,
    shot_id: str,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return skip_material_task(db, pipeline_id, shot_id)
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MaterialTaskCommitUnknownError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MaterialTaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pipeline/{pipeline_id}/continue")
def continue_production_pipeline(
    pipeline_id: int,
    request: ContinuePipelineRequest = ContinuePipelineRequest(),
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = continue_pipeline(
            db,
            pipeline_id,
            force=request.force,
            stop_after=request.stop_after,
            material_root=_api_material_root(),
            project_root=_api_project_root(),
        )
    except PipelineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PipelineStepConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PipelineStepCommitUnknownError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FakeProviderRequiredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_pipeline_result(result)


@app.get("/api/video-posts")
def list_published_videos(
    platform: str | None = None,
    pipeline_id: int | None = None,
    status: str | None = None,
    sort_by: str = Query(
        default="published_at",
        pattern="^(published_at|views|favorites|shares)$",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    return serialize_video_post_list(
        list_video_posts(
            db,
            platform=platform,
            pipeline_id=pipeline_id,
            status=status,
            sort_by=sort_by,
            page=page,
            page_size=page_size,
        )
    )


@app.post("/api/video-posts")
def create_published_video(
    request: VideoPostCreateRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        item = create_video_post(db, request.model_dump())
    except PublishingPipelineNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_video_post(item)


@app.get("/api/video-posts/{post_id}")
def get_published_video(
    post_id: int,
    db: Session = Depends(get_db),
) -> dict:
    item = get_video_post(db, post_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"发布记录不存在：{post_id}")
    return serialize_video_post(item)


@app.put("/api/video-posts/{post_id}")
def edit_published_video(
    post_id: int,
    request: VideoPostUpdateRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        item = update_video_post(db, post_id, request.model_dump(exclude_unset=True))
    except VideoPostNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PublishingPipelineNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_video_post(item)


@app.post("/api/video-posts/{post_id}/snapshots")
def create_video_data_snapshot(
    post_id: int,
    request: VideoSnapshotCreateRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        item = add_video_snapshot(db, post_id, request.model_dump())
    except VideoPostNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_video_post(item)


@app.get("/api/reviews")
def list_review_reports(
    platform: str | None = None,
    pipeline_id: int | None = None,
    published_from: date | None = None,
    published_to: date | None = None,
    judgement: str | None = None,
    sort_by: str = Query(
        default="published_at",
        pattern="^(published_at|favorite_rate|share_rate|follower_rate|view_velocity)$",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    start = (
        datetime.combine(published_from, time.min, tzinfo=timezone.utc)
        if published_from
        else None
    )
    end = (
        datetime.combine(published_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
        - timedelta(microseconds=1)
        if published_to
        else None
    )
    return serialize_video_review_list(
        list_video_reviews(
            db,
            platform=platform,
            pipeline_id=pipeline_id,
            published_from=start,
            published_to=end,
            judgement=judgement,
            sort_by=sort_by,
            page=page,
            page_size=page_size,
        )
    )


@app.get("/api/reviews/{video_post_id}")
def get_review_report(
    video_post_id: int,
    db: Session = Depends(get_db),
) -> dict:
    item = get_video_review(db, video_post_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"发布记录不存在：{video_post_id}")
    return serialize_video_review(item)


@app.get("/api/reviews/weekly/report")
def get_weekly_review_report(
    pipeline_id: int | None = None,
    db: Session = Depends(get_db),
) -> dict:
    return build_weekly_review_report(db, pipeline_id=pipeline_id)


def _api_material_root() -> Path:
    settings = get_settings()
    return resolve_portable_material_root(
        resolve_configured_path(settings.material_root_dir),
        fallback_root=resolve_configured_path(settings.export_dir) / "短视频工厂",
    )


def _api_project_root() -> Path:
    # Project identity has its own approved root. It must never be inferred
    # from material_root because deployments may configure them separately.
    return resolve_configured_path(get_settings().project_root_dir)


def _run_startup_hotspot_check_if_enabled() -> None:
    settings = get_settings()
    if not settings.auto_hotspot_on_startup:
        return

    def runner() -> None:
        db = SessionLocal()
        try:
            run_daily_hotspot_refresh(
                db,
                resolve_configured_path(settings.source_config_path),
                generate_topics=True,
                max_hotspots_for_topics=settings.auto_hotspot_max_topics,
                interval_hours=settings.auto_hotspot_interval_hours,
            )
        finally:
            db.close()

    Thread(target=runner, name="auto-hotspot-startup-check", daemon=True).start()
