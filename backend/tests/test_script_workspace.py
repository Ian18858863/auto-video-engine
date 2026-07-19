from __future__ import annotations

from copy import deepcopy
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models import (
    AiGeneration,
    HotItem,
    PipelineStep,
    ProductionPipeline,
    ScriptAsset,
)
from app.pipeline.material_package_service import (
    get_material_package,
    lock_material_candidate,
    rebuild_export_file,
    save_material_candidate_pool,
    update_material_task_status,
)
from app.pipeline.material_execution_service import (
    MaterialTaskConflictError,
    skip_material_task,
)
from app.pipeline.project_identity import ProjectIdentityError
from app.pipeline.rewrite_service import rewrite_script_part
from app.pipeline.schemas import ManualV1RunRequest
from app.pipeline.script_versioning import ScriptVersionError, ScriptVersionLedger
from app.pipeline.service import build_project_from_hotspot
from app.pipeline.v1_acceptance_service import run_manual_v1_acceptance
from app.pipeline.workspace_service import (
    PipelineWorkspaceNotFoundError,
    get_script_version_detail,
    get_script_workspace,
    serialize_script_workspace,
    update_script_presentation_options,
    update_script_workspace,
)


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def prepared_material_root(tmp_path: Path) -> Path:
    root = tmp_path / "短视频工厂"
    (root / "项目").mkdir(parents=True, exist_ok=True)
    return root


def add_hotspot(session: Session) -> HotItem:
    item = HotItem(
        source_platform="Hacker News",
        source_type="community",
        title="AI 工具进入企业采购流程",
        url="https://example.com/script-workspace",
        published_at=datetime.now(timezone.utc),
        summary="企业开始把 AI 工具放进真实预算。",
        tags=["AI", "商业"],
        category="AI",
        hot_score=88,
        topic_score=95,
        save_score=90,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def _pipeline_storage_bytes(session: Session, pipeline_id: int) -> bytes:
    session.expire_all()
    pipeline = session.get(ProductionPipeline, pipeline_id)
    payload = {
        "pipeline": {
            column.name: getattr(pipeline, column.name)
            for column in ProductionPipeline.__table__.columns
        },
        "steps": [
            {
                column.name: getattr(step, column.name)
                for column in PipelineStep.__table__.columns
            }
            for step in sorted(pipeline.steps, key=lambda item: item.id)
        ],
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def test_workspace_collects_script_tasks_avatar_and_exports_without_ai_call(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        generation_count = session.query(AiGeneration).count()

        workspace = get_script_workspace(session, built.pipeline_id)

        assert workspace.project_name
        assert workspace.hotspot["title"] == hotspot.title
        assert workspace.topic["title"]
        assert workspace.script["voiceover"]
        assert workspace.script["storyboard"]
        assert workspace.script["subtitles"]
        assert workspace.script["title_options"]
        assert workspace.script["cover_copy"]
        assert workspace.script["publish_copy"]
        assert workspace.material_tasks
        assert all(task["material_task_id"] for task in workspace.material_tasks)
        assert workspace.graphic_tasks
        assert workspace.avatar_tasks
        assert all(task["position"] == "右下角" for task in workspace.avatar_tasks)
        assert workspace.exports["script_md"]["generated"] is True
        assert workspace.exports["shotlist_csv"]["generated"] is True
        assert workspace.exports["edit_manifest_json"]["generated"] is True
        assert workspace.provider == "fake"
        assert session.query(AiGeneration).count() == generation_count


def test_workspace_supports_partial_pipeline_without_generating_more_content(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
            stop_after="生成脚本素材",
        )
        generation_count = session.query(AiGeneration).count()

        workspace = get_script_workspace(session, built.pipeline_id)

        assert workspace.script["voiceover"]
        assert workspace.material_tasks == []
        assert workspace.graphic_tasks == []
        assert workspace.avatar_tasks == []
        assert workspace.exports["script_md"]["generated"] is False
        assert session.query(AiGeneration).count() == generation_count


def test_workspace_serializes_only_public_active_material_execution_metadata(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        step = next(item for item in pipeline.steps if item.name == "生成素材任务")
        payload = dict(step.output_summary)
        tasks = [dict(item) for item in payload["material_tasks"]]
        task_id = tasks[0]["material_task_id"]
        execution_id = str(uuid4())
        attempt_id = str(uuid4())
        tasks[0].update(
            {
                "status": "官方页面录屏中",
                "api_key": "sk-task-canary",
                "job_handle": 8675309,
            }
        )
        payload.update(
            {
                "material_tasks": tasks,
                "execution_id": execution_id,
                "active_material_task_id": task_id,
                "active_execution_id": execution_id,
                "active_attempt_id": attempt_id,
                "execution_state": "running",
                "publication_phase": "preparing",
                "reason_code": "recovery_pending",
                "retry_available": True,
                "active_control_id": "control-secret-canary",
                "active_job_instance_id": "job-secret-canary",
                "mutex_handle": 424242,
                "api_key": "sk-step-canary",
                "recovery_ticket": {"ticket_id": "ticket-secret-canary"},
            }
        )
        step.output_summary = payload
        session.commit()
        before = _pipeline_storage_bytes(session, pipeline.id)

        serialized = serialize_script_workspace(
            get_script_workspace(session, pipeline.id)
        )

        active = serialized["material_tasks"][0]
        waiting = serialized["material_tasks"][1]
        assert active["execution_id"] == execution_id
        assert active["active_attempt_id"] == attempt_id
        assert active["execution_state"] == "running"
        assert active["publication_phase"] == "preparing"
        assert active["reason_code"] == "recovery_pending"
        assert active["recovery_action_available"] is True
        assert active["retry_available"] is False
        assert waiting["execution_id"] == ""
        assert waiting["active_attempt_id"] == ""
        assert waiting["recovery_action_available"] is False

        public_json = json.dumps(serialized, ensure_ascii=False, sort_keys=True)
        for private_value in (
            "sk-task-canary",
            "sk-step-canary",
            "control-secret-canary",
            "job-secret-canary",
            "ticket-secret-canary",
        ):
            assert private_value not in public_json
        for private_key in (
            "active_control_id",
            "active_job_instance_id",
            "mutex_handle",
            "job_handle",
            "api_key",
            "recovery_ticket",
        ):
            assert private_key not in public_json
        assert _pipeline_storage_bytes(session, pipeline.id) == before


def test_workspace_does_not_echo_token_shaped_execution_secrets(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        step = next(item for item in pipeline.steps if item.name == "生成素材任务")
        payload = dict(step.output_summary)
        tasks = [dict(item) for item in payload["material_tasks"]]
        task_id = tasks[0]["material_task_id"]
        execution_canary = "sk_execution_secret_canary"
        attempt_canary = "sk_attempt_secret_canary"
        tasks[0]["status"] = "被阻止"
        payload.update(
            {
                "material_tasks": tasks,
                "active_material_task_id": task_id,
                "execution_id": execution_canary,
                "active_execution_id": execution_canary,
                "active_attempt_id": attempt_canary,
                "active_control_id": "control-01",
                "active_job_instance_id": "job-01",
                "execution_state": "blocked",
                "publication_phase": "audit_required",
                "retry_available": True,
            }
        )
        step.output_summary = payload
        session.commit()

        serialized = serialize_script_workspace(
            get_script_workspace(session, pipeline.id)
        )

        task = serialized["material_tasks"][0]
        assert task["execution_id"] == ""
        assert task["active_attempt_id"] == ""
        assert task["retry_available"] is False
        public_json = json.dumps(serialized, ensure_ascii=False, sort_keys=True)
        assert execution_canary not in public_json
        assert attempt_canary not in public_json


def test_workspace_avatar_tasks_use_a_public_field_whitelist(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        step = next(item for item in pipeline.steps if item.name == "生成数字人字段")
        payload = dict(step.output_summary)
        tasks = [dict(item) for item in payload["avatar_tasks"]]
        tasks[0].update(
            {
                "api_key": "sk-avatar-secret-canary",
                "job_handle": 8675309,
                "future_internal_field": "must-not-cross-the-api",
            }
        )
        payload["avatar_tasks"] = tasks
        step.output_summary = payload
        session.commit()

        serialized = serialize_script_workspace(
            get_script_workspace(session, pipeline.id)
        )

        avatar = serialized["avatar_tasks"][0]
        assert avatar["shot_id"]
        assert avatar["position"] == "右下角"
        assert "api_key" not in avatar
        assert "job_handle" not in avatar
        assert "future_internal_field" not in avatar
        assert "sk-avatar-secret-canary" not in json.dumps(
            serialized,
            ensure_ascii=False,
            sort_keys=True,
        )


@pytest.mark.parametrize("retry_available", [False, True])
def test_workspace_expresses_blocked_material_recovery_fail_closed(
    tmp_path,
    retry_available,
):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        step = next(item for item in pipeline.steps if item.name == "生成素材任务")
        payload = dict(step.output_summary)
        tasks = [dict(item) for item in payload["material_tasks"]]
        task_id = tasks[0]["material_task_id"]
        execution_id = str(uuid4())
        attempt_id = str(uuid4())
        tasks[0].update(
            {
                "status": "需要人工确认",
                "reject_reason": "",
                "notes": "",
            }
        )
        payload.update(
            {
                "material_tasks": tasks,
                "execution_id": execution_id,
                "active_material_task_id": task_id,
                "active_execution_id": execution_id,
                "active_attempt_id": attempt_id,
                "execution_state": "blocked",
                "publication_phase": "audit_required",
                "reason_code": "publication_audit_required",
                "blocked_reason": "发布证据还不能安全核实，需要人工处理。",
                "retry_available": retry_available,
                "active_control_id": "control-01" if retry_available else None,
                "active_job_instance_id": "job-01" if retry_available else None,
            }
        )
        step.output_summary = payload
        session.commit()

        task = get_script_workspace(session, pipeline.id).material_tasks[0]

        assert task["status"] == "被阻止"
        assert task["reason_code"] == "publication_audit_required"
        assert task["reject_reason"] == "发布证据还不能安全核实，需要人工处理。"
        assert task["execution_id"] == execution_id
        assert task["active_attempt_id"] == attempt_id
        assert task["publication_phase"] == "audit_required"
        assert task["retry_available"] is retry_available
        assert task["recovery_action_available"] is False


def test_workspace_missing_pipeline_returns_clear_error():
    with make_session() as session:
        try:
            get_script_workspace(session, 999)
        except PipelineWorkspaceNotFoundError as exc:
            assert "流程不存在" in str(exc)
        else:
            raise AssertionError("缺少流程时应返回明确错误")


def test_workspace_api_is_read_only(tmp_path, monkeypatch):
    material_root = prepared_material_root(tmp_path)
    from app.core.config import get_settings

    monkeypatch.setenv("PROJECT_ROOT_DIR", str(material_root / "项目"))
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        hotspot = add_hotspot(setup_session)
        built = build_project_from_hotspot(
            setup_session,
            hotspot.id,
            material_root=material_root,
        )
        pipeline_id = built.pipeline_id
        generation_count = setup_session.query(AiGeneration).count()

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.get(f"/api/pipeline/{pipeline_id}/workspace")
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert response.status_code == 200
    data = response.json()
    assert data["pipeline_id"] == pipeline_id
    assert data["provider"] == "fake"
    assert data["material_tasks"]
    with testing_session_local() as verify_session:
        assert verify_session.query(AiGeneration).count() == generation_count


def test_script_version_detail_returns_verified_public_snapshot(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        ledger = ScriptVersionLedger(
            pipeline,
            project_root=Path(pipeline.project_dir).parent,
        )
        snapshot = ledger.create(
            {
                "voiceover": "第一版可核验口播。",
                "storyboard": [
                    {
                        "shot_id": "shot-01",
                        "voiceover": "第一版可核验口播。",
                        "visual_intent": "标题卡",
                    }
                ],
                "subtitles": [],
            },
            fact_sources=[
                {
                    "title": "公开来源",
                    "url": "https://example.com/evidence",
                    "used_for": "核验背景事实",
                }
            ],
            source="test_fixture",
        )

        detail = get_script_version_detail(session, pipeline.id, snapshot.version_id)

        assert detail["version_id"] == snapshot.version_id
        assert detail["content_hash"] == snapshot.content_hash
        assert detail["payload"]["voiceover"] == "第一版可核验口播。"
        assert detail["fact_sources"][0]["url"] == "https://example.com/evidence"
        assert detail["shot_plan"]
        assert detail["schema_version"] == 2
        assert detail["integrity_status"] == "full_envelope_verified"
        assert len(detail["shot_plan_hash"]) == 64
        assert len(detail["snapshot_hash"]) == 64
        assert "project_dir" not in json.dumps(detail, ensure_ascii=False)


def test_script_version_reads_fail_closed_after_snapshot_tampering(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        ledger = ScriptVersionLedger(
            pipeline,
            project_root=Path(pipeline.project_dir).parent,
        )
        snapshot = ledger.create(
            {"voiceover": "未被篡改的版本。", "storyboard": [], "subtitles": []},
            source="test_fixture",
        )
        snapshot_path = (
            Path(pipeline.project_dir)
            / "脚本版本"
            / "versions"
            / f"{snapshot.version_id}.json"
        )
        raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
        raw["payload"]["voiceover"] = "篡改后的版本。"
        snapshot_path.write_text(
            json.dumps(raw, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

        with pytest.raises(ScriptVersionError, match="哈希不匹配"):
            get_script_version_detail(session, pipeline.id, snapshot.version_id)
        with pytest.raises(ScriptVersionError, match="哈希不匹配"):
            get_script_workspace(session, pipeline.id)


@pytest.mark.parametrize(
    "corruption",
    ["missing_step", "duplicate_step", "non_dict_summary"],
)
def test_workspace_reports_invalid_preparation_without_mutating_or_crashing(
    tmp_path,
    corruption,
):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        pipeline.status = "已完成"
        if corruption == "missing_step":
            session.delete(pipeline.steps[-1])
        elif corruption == "duplicate_step":
            first = pipeline.steps[0]
            session.add(
                PipelineStep(
                    pipeline_id=pipeline.id,
                    name=first.name,
                    step_order=len(pipeline.steps) + 1,
                    status=first.status,
                    error="",
                    output_summary=dict(first.output_summary),
                )
            )
        else:
            pipeline.steps[-1].output_summary = []
        session.commit()

        before = [
            (step.id, step.name, step.step_order, step.status, step.output_summary)
            for step in session.get(ProductionPipeline, pipeline.id).steps
        ]
        workspace = get_script_workspace(session, pipeline.id)
        serialized = serialize_script_workspace(workspace)

        assert workspace.status == "需要人工确认"
        assert workspace.completion_reason_code == "preparation_invalid"
        assert workspace.next_surface == "project"
        assert workspace.current_step == "项目检查"
        assert workspace.needs_confirmation is True
        assert workspace.next_suggestion == "打开项目页检查生产步骤"
        assert "completion_evidence" not in serialized
        assert "sha256" not in json.dumps(serialized, ensure_ascii=False).lower()

        session.expire_all()
        persisted = session.get(ProductionPipeline, pipeline.id)
        after = [
            (step.id, step.name, step.step_order, step.status, step.output_summary)
            for step in persisted.steps
        ]
        assert persisted.status == "已完成"
        assert after == before


def test_workspace_ignores_duplicate_material_step_payload_without_writing(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        session.add(
            PipelineStep(
                pipeline_id=pipeline.id,
                name="生成素材任务",
                step_order=len(pipeline.steps) + 1,
                status="已完成",
                error="",
                output_summary={"material_tasks": 1},
            )
        )
        session.commit()

        before = _pipeline_storage_bytes(session, pipeline.id)
        workspace = get_script_workspace(session, pipeline.id)

        assert workspace.completion_reason_code == "preparation_invalid"
        assert workspace.material_tasks == []
        assert workspace.graphic_tasks == []
        assert workspace.avatar_tasks == []
        assert workspace.exports == {}
        assert workspace.script_factory == {}
        assert _pipeline_storage_bytes(session, pipeline.id) == before


@pytest.mark.parametrize(
    ("payload_case", "result_field"),
    [
        ("material_tasks", "material_tasks"),
        ("graphic_tasks", "graphic_tasks"),
        ("graphic_task_candidates", "graphic_tasks"),
        ("avatar_tasks", "avatar_tasks"),
    ],
)
def test_workspace_treats_non_list_task_payload_as_empty(
    tmp_path,
    payload_case,
    result_field,
):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        steps = {step.name: step for step in pipeline.steps}

        if payload_case == "material_tasks":
            step = steps["生成素材任务"]
            payload = dict(step.output_summary)
            payload["material_tasks"] = 1
        elif payload_case == "graphic_tasks":
            step = steps["生成 Codex 图文任务"]
            payload = dict(step.output_summary)
            payload["graphic_tasks"] = 1
        elif payload_case == "graphic_task_candidates":
            step = steps["生成 Codex 图文任务"]
            payload = dict(step.output_summary)
            payload.pop("graphic_tasks", None)
            material_step = steps["生成素材任务"]
            material_payload = dict(material_step.output_summary)
            material_payload["graphic_task_candidates"] = 1
            material_step.output_summary = material_payload
        else:
            step = steps["生成数字人字段"]
            payload = dict(step.output_summary)
            payload["avatar_tasks"] = 1
        step.output_summary = payload
        session.commit()

        before = _pipeline_storage_bytes(session, pipeline.id)
        workspace = get_script_workspace(session, pipeline.id)

        assert getattr(workspace, result_field) == []
        assert _pipeline_storage_bytes(session, pipeline.id) == before


def test_manual_v1_acceptance_requires_verified_artifact_before_completed(tmp_path):
    with make_session() as session:
        result = run_manual_v1_acceptance(
            session,
            ManualV1RunRequest(
                title="AI 搜索正在改变普通人获取信息的方式",
                summary="AI 搜索替代传统搜索，影响平台流量、广告模式和内容生态。",
                source_platform="手动跑通测试",
                category="AI / 科技趋势 / 商业逻辑",
                tags=["AI", "搜索", "商业逻辑"],
            ),
            material_root=prepared_material_root(tmp_path),
        )

        assert result.completed is False
        assert result.message == (
            "V1 准备包已生成；请到成片页检查文件和验收状态。"
            "五门报告与批准入口仍属于 Phase 1。"
        )
        assert result.pipeline["completion_reason_code"] == "approval_missing"


def test_rewrite_script_part_uses_local_style_without_real_ai(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        generation_count = session.query(AiGeneration).count()

        result = rewrite_script_part(session, built.pipeline_id, "hook")
        workspace = get_script_workspace(session, built.pipeline_id)

        assert result.used_account_style is True
        assert result.real_ai_called is False
        assert "别只看热闹" in workspace.script["voiceover"]
        assert session.query(AiGeneration).count() == generation_count


def test_rewrite_voiceover_and_storyboard_keep_subtitles_in_sync(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        generation_count = session.query(AiGeneration).count()
        script = session.get(ScriptAsset, built.script_asset_id)
        script.storyboard = [
            {
                "shot_id": f"shot-{index + 1:02d}",
                "start": str(index * 5),
                "end": str(index * 5 + 5),
                "voiceover": f"原第{index + 1}段",
                "recommended_visual": f"原第{index + 1}段画面",
            }
            for index in range(12)
        ]
        script.subtitles = [
            {
                "start": str(index * 5),
                "end": str(index * 5 + 5),
                "text": f"原第{index + 1}段",
            }
            for index in range(12)
        ]
        script.voiceover = "\n".join(f"原第{index + 1}段" for index in range(12))
        session.add(script)
        session.commit()

        voiceover_result = rewrite_script_part(session, built.pipeline_id, "voiceover")
        workspace = get_script_workspace(session, built.pipeline_id)

        assert voiceover_result.part == "voiceover"
        assert voiceover_result.real_ai_called is False
        assert workspace.script["voiceover"]
        assert workspace.script["subtitles"]
        assert workspace.script["storyboard"]
        assert len(workspace.script["storyboard"]) == 12
        assert len(workspace.script["subtitles"]) == 12
        assert workspace.script["subtitles"][0]["text"] == workspace.script["storyboard"][0]["voiceover"]

        storyboard_result = rewrite_script_part(session, built.pipeline_id, "storyboard")
        workspace = get_script_workspace(session, built.pipeline_id)

        assert storyboard_result.part == "storyboard"
        assert storyboard_result.real_ai_called is False
        assert workspace.script["storyboard"][0]["recommended_visual"]
        assert workspace.script["subtitles"][0]["text"] == workspace.script["storyboard"][0]["voiceover"]
        assert session.query(AiGeneration).count() == generation_count


def test_rewrite_title_and_publish_copy_are_isolated_single_field_updates(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        generation_count = session.query(AiGeneration).count()
        before = get_script_workspace(session, built.pipeline_id)
        original_titles = list(before.script["title_options"])

        publish_result = rewrite_script_part(session, built.pipeline_id, "publish_copy")
        after_publish = get_script_workspace(session, built.pipeline_id)

        assert publish_result.part == "publish_copy"
        assert publish_result.real_ai_called is False
        assert after_publish.script["title_options"] == original_titles
        assert list(after_publish.script["publish_copy"].keys()) == ["通用"]
        assert "\n" not in after_publish.script["publish_copy"]["通用"]

        publish_copy = dict(after_publish.script["publish_copy"])
        title_result = rewrite_script_part(session, built.pipeline_id, "title")
        after_title = get_script_workspace(session, built.pipeline_id)

        assert title_result.part == "title"
        assert title_result.real_ai_called is False
        assert len(after_title.script["title_options"]) == 1
        assert after_title.script["title_options"][0]["title"]
        assert after_title.script["publish_copy"] == publish_copy
        assert session.query(AiGeneration).count() == generation_count


def test_voiceover_rewrite_marks_existing_material_lock_stale(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        payload = dict(material_step.output_summary)
        shot_id = payload["material_tasks"][0]["shot_id"]
        payload["candidate_pool"] = {
            shot_id: {
                "shot_id": shot_id,
                "revision": 1,
                "candidates": [
                    {
                        "candidate_id": f"cand-{index}",
                        "source": source,
                        "preview_path": f"素材信息/候选预览/cand-{index}.jpg",
                        "url": f"https://example.test/rewrite/{index}",
                        "rights": "commercial-use terms captured",
                        "evidence_kind": "atmosphere",
                        "status": "locked" if index == 1 else "candidate",
                        "reason": "approved before rewrite",
                    }
                    for index, source in enumerate(
                        ("Pexels", "Pixabay", "官方页面"),
                        start=1,
                    )
                ],
            }
        }
        payload["candidate_selection_history"] = {
            shot_id: [
                {
                    "event": "candidate_locked",
                    "shot_id": shot_id,
                    "selected_candidate_id": "cand-1",
                    "reason": "approved before rewrite",
                }
            ]
        }
        material_step.output_summary = payload
        session.add(material_step)
        session.commit()

        rewrite_script_part(session, built.pipeline_id, "voiceover")

        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        stale_payload = material_step.output_summary

        assert material_step.status == "未开始"
        assert stale_payload["material_selection_state"] == "stale"
        assert stale_payload["candidate_pool"][shot_id]["stale"] is True
        assert (
            stale_payload["material_selection_history"][-1]["locked_selections"][0][
                "candidate_id"
            ]
            == "cand-1"
        )


def test_title_rewrite_keeps_material_lock_current_and_byte_equivalent(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        payload = dict(material_step.output_summary)
        shot_id = payload["material_tasks"][0]["shot_id"]
        payload["candidate_pool"] = {
            shot_id: {
                "shot_id": shot_id,
                "revision": 1,
                "candidates": [
                    {
                        "candidate_id": "cand-1",
                        "source": "Pexels",
                        "preview_path": "素材信息/候选预览/cand-1.jpg",
                        "url": "https://example.test/title-rewrite/1",
                        "rights": "commercial-use terms captured",
                        "evidence_kind": "atmosphere",
                        "status": "locked",
                        "reason": "approved before title rewrite",
                    }
                ],
            }
        }
        material_step.output_summary = payload
        session.add(material_step)
        session.commit()
        before_payload = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        before_status = material_step.status

        rewrite_script_part(session, built.pipeline_id, "title")

        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        after_payload = json.dumps(
            material_step.output_summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        assert material_step.status == before_status
        assert after_payload == before_payload
        assert "material_selection_state" not in material_step.output_summary


def test_update_script_workspace_saves_manual_edits_and_resets_downstream(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        generation_count = session.query(AiGeneration).count()

        workspace = update_script_workspace(
            session,
            built.pipeline_id,
            {
                "voiceover": "我手动改过的口播稿。",
                "storyboard": [
                    {
                        "shot_id": "shot-01",
                        "start": "00:00",
                        "end": "00:03",
                        "voiceover": "我手动改过的口播稿。",
                        "recommended_visual": "标题页",
                    }
                ],
                "subtitles": [
                    {"start": "00:00", "end": "00:03", "text": "我手动改过的口播稿。"}
                ],
                "title_options": [{"title": "手动标题"}],
                "cover_copy": [{"text": "手动封面"}],
                "publish_copy": {"抖音": "手动发布文案"},
            },
        )

        session.expire_all()
        script = session.get(ScriptAsset, built.script_asset_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        statuses = {step.name: step.status for step in pipeline.steps}

        assert workspace.script["voiceover"] == "我手动改过的口播稿。"
        assert script.voiceover == "我手动改过的口播稿。"
        assert script.title_options[0]["title"] == "手动标题"
        assert statuses["生成素材任务"] == "未开始"
        assert statuses["生成 Codex 图文任务"] == "未开始"
        assert statuses["生成数字人字段"] == "未开始"
        assert statuses["生成剪辑清单"] == "未开始"
        assert statuses["导出 script.md"] == "未开始"
        assert statuses["创建中文项目目录"] == "已完成"
        assert session.query(AiGeneration).count() == generation_count


def test_manual_script_edit_marks_locked_material_stale_and_preserves_audit_history(
    tmp_path,
):
    material_root = prepared_material_root(tmp_path)
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=material_root,
        )
        package = get_material_package(session, built.pipeline_id)
        shot_id = package["material_tasks"][0]["shot_id"]
        candidates = []
        for index, source in enumerate(("Pexels", "Pixabay", "官方页面"), start=1):
            preview = tmp_path / f"stale-selection-{index}.jpg"
            Image.new("RGB", (12, 8), (index * 40, 90, 140)).save(
                preview,
                format="JPEG",
            )
            candidates.append(
                {
                    "candidate_id": f"cand-{index}",
                    "source": source,
                    "preview_path": str(preview),
                    "url": f"https://example.test/stale-selection/{index}",
                    "rights": "commercial-use terms captured",
                    "evidence_kind": "atmosphere",
                    "reason": "matches the approved narration",
                }
            )
        save_material_candidate_pool(
            session,
            built.pipeline_id,
            shot_id,
            candidates,
        )
        lock_material_candidate(
            session,
            built.pipeline_id,
            shot_id,
            "cand-1",
            reason="approved against the current script",
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        before = dict(material_step.output_summary)
        before_tasks = [dict(item) for item in before["material_tasks"]]
        before_tasks[0]["material_path"] = r"D:\legacy-absolute\shot-01.mp4"
        before_tasks[0]["notes"] = r"旧说明指向 D:\legacy-absolute\shot-01.mp4"
        before["material_tasks"] = before_tasks
        before["candidate_generation_history"] = [
            {
                "event": "candidate_batch_generated",
                "shot_id": shot_id,
                "entry_sha256": "a" * 64,
            }
        ]
        material_step.output_summary = before
        session.add(material_step)
        session.commit()

        workspace = get_script_workspace(session, built.pipeline_id)
        update_script_workspace(
            session,
            built.pipeline_id,
            {
                **workspace.script,
                "voiceover": f"{workspace.script['voiceover']}\n这是一条上游修改。",
            },
        )

        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        payload = material_step.output_summary
        pool = payload["candidate_pool"][shot_id]
        locked = next(
            item for item in pool["candidates"] if item["candidate_id"] == "cand-1"
        )
        history = payload["material_selection_history"]
        event = history[-1]
        public_package = get_material_package(session, built.pipeline_id)
        public_task = next(
            item for item in public_package["material_tasks"] if item["shot_id"] == shot_id
        )

        assert material_step.status == "未开始"
        assert payload["material_selection_state"] == "stale"
        assert pool["stale"] is True
        assert pool["stale_reason_code"] == "upstream_script_changed"
        assert locked["status"] == "locked"
        assert public_task["candidate_pool"]["stale"] is True
        assert event["event"] == "material_selection_stale"
        assert event["reason_code"] == "upstream_script_changed"
        assert event["locked_selections"][0]["candidate_id"] == "cand-1"
        assert event["candidate_selection_history"][shot_id][-1]["event"] == "candidate_locked"
        assert event["candidate_generation_history"][-1]["event"] == "candidate_batch_generated"
        assert len(event["snapshot_sha256"]) == 64
        assert len(event["entry_sha256"]) == 64
        serialized_event = json.dumps(event, ensure_ascii=False)
        assert "preview_path" not in serialized_event
        assert "D:\\legacy-absolute" not in serialized_event
        public_history = public_package["material_selection_history"]
        assert public_history[-1]["locked_selections"][0]["candidate_id"] == "cand-1"
        assert "candidate_pool_snapshot" not in public_history[-1]
        assert "D:\\legacy-absolute" not in json.dumps(
            public_history, ensure_ascii=False
        )

        immutable_event = deepcopy(event)
        latest_workspace = get_script_workspace(session, built.pipeline_id)
        update_script_workspace(
            session,
            built.pipeline_id,
            {
                **latest_workspace.script,
                "voiceover": f"{latest_workspace.script['voiceover']}\n第二次上游修改。",
            },
        )
        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        assert material_step.output_summary["material_selection_history"] == [
            immutable_event
        ]

        with pytest.raises(MaterialTaskConflictError, match="上游脚本"):
            skip_material_task(session, built.pipeline_id, shot_id)
        with pytest.raises(ValueError, match="过期"):
            rebuild_export_file(
                session,
                built.pipeline_id,
                "edit_manifest_json",
                project_root=material_root / "项目",
            )


def test_manual_title_only_edit_does_not_invalidate_material_selection(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        payload = deepcopy(material_step.output_summary)
        shot_id = payload["material_tasks"][0]["shot_id"]
        payload["candidate_pool"] = {
            shot_id: {
                "shot_id": shot_id,
                "revision": 1,
                "candidates": [
                    {
                        "candidate_id": "cand-1",
                        "source": "Pexels",
                        "preview_path": "素材信息/候选预览/cand-1.jpg",
                        "url": "https://example.test/manual-title/1",
                        "rights": "commercial-use terms captured",
                        "evidence_kind": "atmosphere",
                        "status": "locked",
                        "reason": "approved before title edit",
                    }
                ],
            }
        }
        material_step.output_summary = payload
        session.add(material_step)
        session.commit()
        before_status = material_step.status
        before_payload = deepcopy(payload)
        workspace = get_script_workspace(session, built.pipeline_id)

        update_script_workspace(
            session,
            built.pipeline_id,
            {
                **workspace.script,
                "title_options": [{"title": "只改发布标题"}],
            },
        )

        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )

        assert material_step.status == before_status
        assert material_step.output_summary == before_payload
        assert "material_selection_state" not in material_step.output_summary


def test_script_invalidation_cas_preserves_a_concurrent_candidate_lock(tmp_path):
    database_path = tmp_path / "req009-concurrent.sqlite3"
    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_a = session_factory()
    session_b = session_factory()
    try:
        hotspot = add_hotspot(session_a)
        built = build_project_from_hotspot(
            session_a,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        stale_pipeline = session_a.get(ProductionPipeline, built.pipeline_id)
        stale_material_step = next(
            step for step in stale_pipeline.steps if step.name == "生成素材任务"
        )
        shot_id = stale_material_step.output_summary["material_tasks"][0]["shot_id"]
        session_a.commit()

        candidates = []
        for index, source in enumerate(("Pexels", "Pixabay", "官方页面"), start=1):
            preview = tmp_path / f"concurrent-lock-{index}.jpg"
            Image.new("RGB", (12, 8), (index * 30, 100, 150)).save(
                preview,
                format="JPEG",
            )
            candidates.append(
                {
                    "candidate_id": f"cand-{index}",
                    "source": source,
                    "preview_path": str(preview),
                    "url": f"https://example.test/concurrent-lock/{index}",
                    "rights": "commercial-use terms captured",
                    "evidence_kind": "atmosphere",
                    "reason": "candidate discovered concurrently",
                }
            )
        save_material_candidate_pool(
            session_b,
            built.pipeline_id,
            shot_id,
            candidates,
        )
        lock_material_candidate(
            session_b,
            built.pipeline_id,
            shot_id,
            "cand-2",
            reason="lock committed while the script page was stale",
        )

        stale_workspace = get_script_workspace(session_a, built.pipeline_id)
        update_script_workspace(
            session_a,
            built.pipeline_id,
            {
                **stale_workspace.script,
                "voiceover": f"{stale_workspace.script['voiceover']}\n并发上游修改。",
            },
        )

        session_a.expire_all()
        pipeline = session_a.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        payload = material_step.output_summary
        stale_pool = payload["candidate_pool"][shot_id]
        locked = next(
            item for item in stale_pool["candidates"] if item["status"] == "locked"
        )

        assert locked["candidate_id"] == "cand-2"
        assert stale_pool["stale"] is True
        assert (
            payload["material_selection_history"][-1]["locked_selections"][0][
                "candidate_id"
            ]
            == "cand-2"
        )
    finally:
        session_b.close()
        session_a.close()
        engine.dispose()


def test_workspace_can_disable_avatar_without_removing_avatar_interface(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )
        generation_count = session.query(AiGeneration).count()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        material_payload_before = deepcopy(material_step.output_summary)

        workspace = update_script_presentation_options(
            session,
            built.pipeline_id,
            use_avatar=False,
        )

        assert workspace.script["use_avatar"] is False
        assert workspace.avatar_tasks
        assert all(task["required"] is False for task in workspace.avatar_tasks)
        assert all(task["enabled"] is False for task in workspace.avatar_tasks)
        assert all(task["needs_avatar"] is False for task in workspace.material_tasks)
        assert session.query(AiGeneration).count() == generation_count
        session.refresh(material_step)
        assert material_step.output_summary == material_payload_before

        workspace = update_script_presentation_options(
            session,
            built.pipeline_id,
            use_avatar=True,
        )

        assert workspace.script["use_avatar"] is True
        assert all(task["required"] is True for task in workspace.avatar_tasks)
        assert all(task["needs_avatar"] is True for task in workspace.material_tasks)
        assert session.query(AiGeneration).count() == generation_count
        session.refresh(material_step)
        assert material_step.output_summary == material_payload_before


def test_material_package_can_update_task_status_and_rebuild_exports(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )

        package = get_material_package(session, built.pipeline_id)
        shot_id = package["material_tasks"][0]["shot_id"]
        updated = update_material_task_status(
            session,
            built.pipeline_id,
            shot_id,
            "需要人工确认",
        )
        export = rebuild_export_file(
            session,
            built.pipeline_id,
            "edit_manifest_json",
            project_root=prepared_material_root(tmp_path) / "项目",
        )

        assert updated["status"] == "需要人工确认"
        assert export["path"].endswith("edit_manifest.json")
        assert get_material_package(session, built.pipeline_id)["package_note"]


def test_material_package_reconstruction_and_exports_preserve_provenance(tmp_path):
    material_root = prepared_material_root(tmp_path)
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=material_root,
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        step = next(item for item in pipeline.steps if item.name == "生成素材任务")
        payload = dict(step.output_summary)
        tasks = [dict(item) for item in payload["material_tasks"]]
        tasks[0].update(
            {
                "material_type": "Pexels视频",
                "source_type": "Pexels",
                "evidence_kind": "atmosphere",
                "source_url": "https://videos.pexels.com/example.mp4",
                "is_locally_generated": False,
            }
        )
        payload["material_tasks"] = tasks
        step.output_summary = payload
        session.add(step)
        session.commit()

        package = get_material_package(session, built.pipeline_id)
        csv_export = rebuild_export_file(
            session,
            built.pipeline_id,
            "shotlist_csv",
            project_root=material_root / "项目",
        )
        json_export = rebuild_export_file(
            session,
            built.pipeline_id,
            "edit_manifest_json",
            project_root=material_root / "项目",
        )

        assert package["material_tasks"][0]["evidence_kind"] == "atmosphere"
        assert package["material_tasks"][0]["source_url"].startswith("https://")
        assert package["material_tasks"][0]["is_locally_generated"] is False
        with Path(csv_export["path"]).open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as file:
            csv_row = next(csv.DictReader(file))
        manifest = json.loads(Path(json_export["path"]).read_text(encoding="utf-8"))
        json_row = manifest["shots"][0]

        assert csv_row["evidence_kind"] == "atmosphere"
        assert csv_row["source_url"] == "https://videos.pexels.com/example.mp4"
        assert csv_row["is_locally_generated"] == "false"
        assert json_row["evidence_kind"] == "atmosphere"
        assert json_row["source_url"] == "https://videos.pexels.com/example.mp4"
        assert json_row["is_locally_generated"] is False


def test_candidate_pool_without_locked_choice_blocks_export_until_review(tmp_path):
    material_root = prepared_material_root(tmp_path)
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=material_root,
        )
        package = get_material_package(session, built.pipeline_id)
        shot_ids = [task["shot_id"] for task in package["material_tasks"]]
        first_shot_id = shot_ids[0]
        for shot_index, shot_id in enumerate(shot_ids, start=1):
            candidates = []
            for index, source in enumerate(
                ("Pexels", "Pixabay", "官方页面"),
                start=1,
            ):
                preview = tmp_path / f"candidate-{shot_index}-{index}.jpg"
                Image.new(
                    "RGB",
                    (12, 8),
                    (shot_index * 20 % 255, index * 50, 120),
                ).save(preview, format="JPEG")
                candidates.append(
                    {
                        "candidate_id": f"cand-{shot_index}-{index}",
                        "source": source,
                        "preview_path": str(preview),
                        "url": (
                            f"https://example.test/candidate/{shot_index}/{index}"
                        ),
                        "rights": "commercial-use terms captured",
                        "evidence_kind": "atmosphere",
                        "reason": "matches the shot intent",
                    }
                )
            save_material_candidate_pool(
                session,
                built.pipeline_id,
                shot_id,
                candidates,
            )
            if shot_id != first_shot_id:
                lock_material_candidate(
                    session,
                    built.pipeline_id,
                    shot_id,
                    f"cand-{shot_index}-1",
                    reason="其他镜头已确认",
                )

        with pytest.raises(ValueError, match="锁定"):
            rebuild_export_file(
                session,
                built.pipeline_id,
                "edit_manifest_json",
                project_root=material_root / "项目",
            )

        lock_material_candidate(
            session,
            built.pipeline_id,
            first_shot_id,
            "cand-1-1",
            reason="Ian 已确认这个候选",
        )
        export = rebuild_export_file(
            session,
            built.pipeline_id,
            "edit_manifest_json",
            project_root=material_root / "项目",
        )
        assert export["path"].endswith("edit_manifest.json")


def test_material_package_rebuild_rejects_spoofed_project_directory(tmp_path):
    material_root = prepared_material_root(tmp_path)
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=material_root,
        )
        hostile = tmp_path / "hostile-export-directory"
        hostile.mkdir()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        pipeline.project_dir = str(hostile)
        session.commit()

        with pytest.raises(ProjectIdentityError, match="project_dir|canonical"):
            rebuild_export_file(
                session,
                built.pipeline_id,
                "script_md",
                project_root=material_root / "项目",
            )
        assert not list(hostile.iterdir())
