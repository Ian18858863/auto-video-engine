from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import app.main as main_module
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models import AiGeneration, HotItem, ProductionPipeline, ScriptAsset
from app.pipeline.project_identity import PROJECT_REFERENCE_FILENAME
from app.pipeline.rewrite_service import rewrite_script_part
from app.pipeline import script_work_order_service as script_work_order_module
from app.pipeline import service as pipeline_service
from app.pipeline import workspace_service as workspace_service_module
from app.pipeline.script_work_order_service import (
    ScriptWorkOrderCommitUnknownError,
    ScriptWorkOrderConflictError,
    ScriptWorkOrderReconcileError,
    ScriptWorkOrderValidationError,
    _project_factory_root,
    _state_job_dir,
    accept_script_work_order,
    build_script_work_order,
    dispatch_script_work_order,
    import_script_work_order_result,
    reconcile_pending_script_version_operations,
    restore_script_version_as_draft,
)
from app.pipeline.service import build_project_from_hotspot, continue_pipeline
from app.pipeline.workspace_service import (
    get_script_workspace,
    script_workspace_state_sha256,
    update_script_workspace,
)
from app.pipeline.script_versioning import ScriptVersionError, ScriptVersionLedger


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


@pytest.fixture
def d_material_root(monkeypatch, tmp_path: Path):
    root = tmp_path / "短视频工厂"
    (root / "项目").mkdir(parents=True, exist_ok=True)
    from app.core.config import get_settings

    monkeypatch.setenv("MATERIAL_ROOT_DIR", str(root))
    monkeypatch.setenv("PROJECT_ROOT_DIR", str(root / "项目"))
    get_settings.cache_clear()
    try:
        yield root
    finally:
        get_settings.cache_clear()


def add_hotspot(session: Session) -> HotItem:
    item = HotItem(
        source_platform="Hacker News",
        source_type="community",
        title="AI 工具进入企业采购流程",
        url="https://example.com/script-work-order",
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


def build_pipeline(session: Session, material_root: Path):
    hotspot = add_hotspot(session)
    return build_project_from_hotspot(
        session,
        hotspot.id,
        material_root=material_root,
        stop_after="创建中文项目目录",
    )


def valid_factory_result(*, pipeline_id: int, job_id: str) -> dict:
    return {
        "meta": {
            "pipeline_id": pipeline_id,
            "job_id": job_id,
            "duration_strategy": "按选题决定",
            "target_duration_range": "35-45 秒",
            "status": "ready_for_import",
            "real_ai_called": False,
        },
        "fact_check": {
            "sources": [
                {
                    "title": "企业 AI 采购趋势参考",
                    "url": "https://example.com/source",
                    "used_for": "确认企业把 AI 工具放入预算的背景",
                }
            ],
            "unresolved_facts": [],
        },
        "voiceover": (
            "换成人话说，这不是热闹，而是企业开始认真花预算。\n"
            "普通人只需要看三件事：谁付钱，省什么成本，能不能持续用。"
        ),
        "storyboard": [
            {
                "shot_id": "shot-01",
                "start": "00:00",
                "end": "00:05",
                "voiceover": "换成人话说，这不是热闹，而是企业开始认真花预算。",
                "visual_intent": "动态标题页，突出企业预算和 AI 工具采购。",
                "material_requirements": {
                    "keywords": ["AI工具", "企业采购"],
                    "official_source_url": "https://example.com/source",
                },
                "source_priority": ["官方录屏", "信息动效", "Pexels视频"],
                "backup_visual": "用预算清单信息卡片替代真实页面。",
                "failure_fallback": "找不到官方页面时使用信息动效。",
            },
            {
                "shot_id": "shot-02",
                "start": "00:05",
                "end": "00:12",
                "voiceover": "普通人只需要看三件事：谁付钱，省什么成本，能不能持续用。",
                "visual_intent": "三点判断清单信息卡。",
                "material_requirements": {
                    "keywords": ["商业逻辑", "效率提升"],
                },
                "source_priority": ["信息动效", "备选素材"],
                "backup_visual": "判断框架卡片。",
                "failure_fallback": "素材失败时保留纯信息卡片。",
            },
        ],
        "subtitles": [
            {
                "start": "00:00",
                "end": "00:05",
                "text": "换成人话说，这不是热闹，而是企业开始认真花预算。",
            },
            {
                "start": "00:05",
                "end": "00:12",
                "text": "普通人只需要看三件事：谁付钱，省什么成本，能不能持续用。",
            },
        ],
        "title_options": [{"title": "AI 工具真正进入企业预算了", "type": "判断型"}],
        "cover_copy": [{"text": "别只看热闹，看预算"}],
        "publish_copy": {"通用": "这条视频只拆商业逻辑，不做投资建议。"},
        "quality_self_check": {
            "plain_language": True,
            "judgement": "企业 AI 工具采购正在从试用走向预算化。",
            "collect_value": "高",
            "risk_level": "低",
            "risk_notes": [],
            "banned_expression_check": [],
        },
        "next_material_brief": [
            {
                "shot_id": "shot-01",
                "material_keywords": ["AI工具", "企业采购"],
                "source_priority": ["官方录屏", "信息动效", "Pexels视频"],
                "matching_reason": "开头需要可信来源和预算感。",
            },
            {
                "shot_id": "shot-02",
                "material_keywords": ["商业逻辑", "效率提升"],
                "source_priority": ["信息动效", "备选素材"],
                "matching_reason": "用判断清单解释核心观点。",
            },
        ],
    }


def write_factory_result(job_dir: Path, payload: dict | None = None) -> Path:
    result_path = job_dir / "script_factory_result.json"
    if payload is None:
        match = re.fullmatch(
            r"pipeline-(?P<pipeline_id>[1-9][0-9]*)-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}",
            job_dir.name,
        )
        assert match is not None
        payload = valid_factory_result(
            pipeline_id=int(match.group("pipeline_id")),
            job_id=job_dir.name,
        )
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result_path


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_build_script_factory_package_writes_three_files_on_d_drive_without_ai_call(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        generation_count = session.query(AiGeneration).count()

        result = build_script_work_order(session, built.pipeline_id)

        job_dir = Path(result.job_dir)
        assert result.status == "built"
        assert result.real_ai_called is False
        assert result.dispatch_supported is False
        assert job_dir.drive.upper() == "D:"
        assert job_dir.parent.name == "脚本工厂"
        assert (job_dir / "script_factory_prompt.md").is_file()
        assert (job_dir / "script_factory_context.json").is_file()
        assert (job_dir / "script_factory_result_template.json").is_file()
        assert not (job_dir / "script_factory_result.json").exists()

        prompt = (job_dir / "script_factory_prompt.md").read_text(encoding="utf-8")
        context = json.loads(
            (job_dir / "script_factory_context.json").read_text(encoding="utf-8")
        )
        template = json.loads(
            (job_dir / "script_factory_result_template.json").read_text(
                encoding="utf-8"
            )
        )
        assert "新建一个专门 Codex 对话" in prompt
        assert "script_factory_result.json" in prompt
        assert "semantic_beats" in prompt
        assert context["storage"]["must_stay_on_drive"] == "D:"
        assert context["pipeline"]["real_ai_called"] is False
        assert template["storyboard"][0]["material_requirements"]
        semantic_template = template["storyboard"][0]["semantic_beats"][0]
        assert semantic_template["viewer_takeaway"] == ""
        assert semantic_template["forbidden_items"] == []
        assert semantic_template["acceptance_conditions"] == []
        assert session.query(AiGeneration).count() == generation_count


def test_script_factory_rejects_tampered_project_reference_marker(d_material_root):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        marker = Path(built.project_dir) / PROJECT_REFERENCE_FILENAME
        marker.write_text('{"project_key":"tampered"}', encoding="utf-8")

        with pytest.raises(
            ScriptWorkOrderValidationError,
            match="身份|reference|marker|tamper",
        ):
            build_script_work_order(session, built.pipeline_id)


def test_script_factory_rejects_reparse_child_before_files_or_state_change(
    d_material_root,
    monkeypatch,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        info_dir = Path(pipeline.project_dir) / "素材信息"
        step = next(item for item in pipeline.steps if item.name == "生成脚本素材")
        before = deepcopy(step.output_summary)
        identity = __import__("app.pipeline.project_identity", fromlist=["_is_reparse"])
        original_probe = identity._is_reparse

        def simulated_reparse(path: Path) -> bool:
            return Path(path) == info_dir or original_probe(path)

        monkeypatch.setattr(identity, "_is_reparse", simulated_reparse)

        with pytest.raises(
            ScriptWorkOrderValidationError,
            match="身份|reparse|junction|child",
        ):
            build_script_work_order(session, built.pipeline_id)

        session.refresh(step)
        assert step.output_summary == before
        assert not (info_dir / "脚本工厂").exists()


def test_script_factory_batch_failure_leaves_no_partial_job_or_state_change(
    d_material_root,
    monkeypatch,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        info_dir = Path(pipeline.project_dir) / "素材信息"
        step = next(item for item in pipeline.steps if item.name == "生成脚本素材")
        before = deepcopy(step.output_summary)
        identity = __import__("app.pipeline.project_identity", fromlist=["os"])
        original_replace = identity.os.replace
        commit_count = 0

        def fail_second_commit(source, destination):
            nonlocal commit_count
            if ".tmp-" in Path(source).name:
                commit_count += 1
                if commit_count == 2:
                    raise OSError("injected second-file commit failure")
            return original_replace(source, destination)

        monkeypatch.setattr(identity.os, "replace", fail_second_commit)

        with pytest.raises(ScriptWorkOrderValidationError, match="任务包写入失败"):
            build_script_work_order(session, built.pipeline_id)

        session.refresh(step)
        assert step.output_summary == before
        factory_root = info_dir / "脚本工厂"
        assert not factory_root.exists() or not any(factory_root.rglob("*"))


def test_build_script_factory_rejects_c_drive_project_dir(tmp_path):
    (tmp_path / "项目").mkdir()
    with make_session() as session:
        built = build_pipeline(session, tmp_path)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        pipeline.project_dir = "C:/forbidden/auto-video-engine/project"
        session.commit()

        with pytest.raises(
            ScriptWorkOrderValidationError,
            match="身份|project_dir|canonical",
        ):
            build_script_work_order(
                session,
                built.pipeline_id,
                project_root=tmp_path / "项目",
            )


def test_project_factory_root_rejects_unbound_stored_directory(tmp_path):
    project_root = tmp_path / "项目"
    project_root.mkdir()
    pipeline = ProductionPipeline(
        id=999,
        project_name="unbound",
        project_key="p-999-unbound",
        project_dir=str(tmp_path / "attacker"),
    )

    with pytest.raises(ScriptWorkOrderValidationError, match="身份|canonical"):
        _project_factory_root(pipeline, project_root=project_root)


def test_script_factory_state_is_bound_to_expected_factory_root(tmp_path):
    factory_root = tmp_path / "脚本工厂"
    job_id = "pipeline-1-20260101-000000-deadbeef"
    job_dir = factory_root / job_id
    job_dir.mkdir(parents=True)

    assert _state_job_dir(
        {"job_id": job_id, "job_dir": str(job_dir)},
        expected_factory_root=factory_root,
        pipeline_id=1,
    ) == job_dir

    other_root = tmp_path / "other"
    other_root.mkdir()
    with pytest.raises(ScriptWorkOrderValidationError, match="身份不一致"):
        _state_job_dir(
            {"job_id": job_id, "job_dir": str(job_dir)},
            expected_factory_root=other_root,
            pipeline_id=1,
        )


def test_dispatch_reports_manual_required_instead_of_fake_background_thread(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_script_work_order(session, built.pipeline_id)

        result = dispatch_script_work_order(session, built.pipeline_id)

        assert result.status == "manual_dispatch_required"
        assert result.dispatch_supported is False
        assert result.thread_id == ""
        assert "后端网页不能直接新建 Codex Desktop 对话" in result.failure_reason
        assert "复制下面内容到新的 Codex 对话" in result.manual_prompt
        assert "script_factory_prompt.md" in result.manual_prompt


def test_workspace_exposes_script_factory_state_and_result_file_presence(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)

        workspace = get_script_workspace(session, built.pipeline_id)

        assert workspace.script_factory["status"] == "built"
        assert workspace.script_factory["job_dir"] == build_result.job_dir
        assert workspace.script_factory["result_exists"] is False
        assert workspace.script_factory["ready_for_review"] is False

        write_factory_result(Path(build_result.job_dir))
        workspace = get_script_workspace(session, built.pipeline_id)

        assert workspace.script_factory["status"] == "result_found"
        assert workspace.script_factory["result_exists"] is True
        assert workspace.script_factory["ready_for_review"] is False


def test_import_result_file_updates_script_and_resets_downstream_with_material_brief(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(build_result.job_dir))
        generation_count = session.query(AiGeneration).count()

        result = import_script_work_order_result(session, built.pipeline_id)
        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        script = session.get(ScriptAsset, pipeline.script_asset_id)
        statuses = {step.name: step.status for step in pipeline.steps}

        assert result.status == "imported"
        assert result.real_ai_called is False
        assert Path(result.result_path).name == "script_factory_result.json"
        assert "认真花预算" in script.voiceover
        assert script.storyboard[0]["recommended_visual"].startswith("动态标题页")
        assert script.storyboard[0]["source_priority"] == [
            "官方录屏",
            "信息动效",
            "Pexels视频",
        ]
        assert script.storyboard[0]["asset_keywords"] == ["AI工具", "企业采购"]
        assert statuses["生成脚本素材"] == "已完成"
        assert statuses["生成素材任务"] == "未开始"
        assert session.query(AiGeneration).count() == generation_count


def test_import_result_creates_persistent_script_version_and_semantic_shot_plan(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(build_result.job_dir))

        import_script_work_order_result(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(
            pipeline,
            project_root=d_material_root / "项目",
        )
        versions = ledger.list()
        assert len(versions) == 1
        assert versions[0].shot_plan
        assert versions[0].shot_plan[0]["beat_type"] in {
            "hook",
            "evidence",
            "explanation",
            "context",
            "conclusion",
        }

        workspace = get_script_workspace(session, built.pipeline_id)
        assert workspace.script_versions[0]["version_id"] == versions[0].version_id
        assert workspace.shot_plan[0]["beat_id"] == versions[0].shot_plan[0]["beat_id"]


def test_import_preserves_explicit_semantic_beats_through_material_tasks(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        payload = valid_factory_result(
            pipeline_id=built.pipeline_id,
            job_id=build_result.job_id,
        )
        payload["storyboard"][0]["semantic_beats"] = [
            {
                "shot_id": "shot-01-proof",
                "start": "00:00",
                "end": "00:03",
                "voiceover": "企业已经把 AI 工具写进真实预算。",
                "semantic_role": "evidence",
                "semantic_group": "opening-proof",
                "viewer_takeaway": "看懂企业正在为 AI 工具真实付费",
                "visual_intent": "展示官方报告里的企业预算证据",
                "subject": "企业 AI 预算表",
                "action": "高亮预算项目和金额",
                "location": "官方报告页面",
                "time": "2026 年",
                "composition": "来源标题在上，预算数据居中，出处固定在底部",
                "visual_type": "官方证据画面",
                "sound_notes": "数据出现时使用一次轻提示音",
                "source_url": "https://example.com/source",
                "asset_keywords": ["企业AI预算", "采购报告"],
                "source_priority": ["官方录屏", "信息动效"],
                "forbidden_items": ["不得用普通办公画面冒充预算证据"],
                "acceptance_conditions": ["画面必须清楚显示来源和预算字段"],
                "material_requirements": {
                    "keywords": ["企业AI预算", "采购报告"],
                    "official_source_url": "https://example.com/source",
                },
            },
            {
                "shot_id": "shot-01-explain",
                "start": "00:03",
                "end": "00:05",
                "voiceover": "这意味着采购从试用进入正式流程。",
                "semantic_role": "explanation",
                "viewer_takeaway": "理解预算化和试用的区别",
                "visual_intent": "用两阶段流程图解释试用到采购",
                "subject": "采购流程图",
                "action": "从试用节点推进到正式采购节点",
                "location": "信息动效画布",
                "time": "当前",
                "composition": "左右两阶段对比",
                "visual_type": "信息动效",
                "sound_notes": "流程推进时使用短促转场音",
                "asset_keywords": ["采购流程", "试用转采购"],
                "source_priority": ["信息动效"],
                "forbidden_items": ["不得继续重复预算表截图"],
                "acceptance_conditions": ["两阶段关系必须一眼可懂"],
            },
        ]
        write_factory_result(Path(build_result.job_dir), payload)

        import_script_work_order_result(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(
            pipeline,
            project_root=d_material_root / "项目",
        )
        version = ledger.list()[0]

        stored_beats = version.payload["storyboard"][0]["semantic_beats"]
        assert [item["shot_id"] for item in stored_beats] == [
            "shot-01-proof",
            "shot-01-explain",
        ]
        assert len(version.shot_plan) == 3
        proof = version.shot_plan[0]
        explanation = version.shot_plan[1]
        assert proof["beat_type"] == "evidence"
        assert proof["viewer_takeaway"] == "看懂企业正在为 AI 工具真实付费"
        assert proof["subject"] == "企业 AI 预算表"
        assert proof["action"] == "高亮预算项目和金额"
        assert proof["location"] == "官方报告页面"
        assert proof["time_context"] == "2026 年"
        assert proof["composition"] == "来源标题在上，预算数据居中，出处固定在底部"
        assert proof["sound_notes"] == "数据出现时使用一次轻提示音"
        assert proof["forbidden_items"] == ["不得用普通办公画面冒充预算证据"]
        assert proof["acceptance_conditions"] == ["画面必须清楚显示来源和预算字段"]
        assert explanation["start"] == "00:03.00"
        assert explanation["end"] == "00:05.00"

        accepted = accept_script_work_order(session, built.pipeline_id)
        assert accepted.accepted is True
        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        material_task = material_step.output_summary["material_tasks"][0]
        assert material_task["shot_id"] == "shot-01-proof"
        assert material_task["subject"] == "企业 AI 预算表"
        assert material_task["action"] == "高亮预算项目和金额"
        assert material_task["composition"] == "来源标题在上，预算数据居中，出处固定在底部"
        assert material_task["forbidden_items"] == ["不得用普通办公画面冒充预算证据"]
        assert material_task["acceptance_conditions"] == ["画面必须清楚显示来源和预算字段"]


def test_import_result_file_accepts_utf8_bom(d_material_root):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        result_path = Path(build_result.job_dir) / "script_factory_result.json"
        result_path.write_text(
            json.dumps(
                valid_factory_result(
                    pipeline_id=built.pipeline_id,
                    job_id=build_result.job_id,
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8-sig",
        )

        result = import_script_work_order_result(session, built.pipeline_id)

        assert result.status == "imported"


def test_import_missing_result_file_returns_chinese_reason_and_keeps_script(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        old_script = session.get(ScriptAsset, built.script_asset_id)
        old_voiceover = old_script.voiceover
        build_script_work_order(session, built.pipeline_id)

        with pytest.raises(
            ScriptWorkOrderValidationError,
            match="还没有找到 Codex 结果文件",
        ):
            import_script_work_order_result(session, built.pipeline_id)

        session.expire_all()
        script = session.get(ScriptAsset, built.script_asset_id)
        assert script.voiceover == old_voiceover


def test_import_reconstructs_job_directory_from_bound_job_id(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        first = build_script_work_order(session, built.pipeline_id)
        second = build_script_work_order(session, built.pipeline_id)
        write_factory_result(
            Path(first.job_dir),
            valid_factory_result(
                pipeline_id=built.pipeline_id,
                job_id=second.job_id,
            ),
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        script = session.get(ScriptAsset, pipeline.script_asset_id)
        old_voiceover = script.voiceover
        step = next(item for item in pipeline.steps if item.name == "生成脚本素材")
        output = dict(step.output_summary or {})
        state = dict(output["script_factory"])
        state["job_dir"] = first.job_dir
        output["script_factory"] = dict(state)
        output["script_work_order"] = dict(state)
        step.output_summary = output
        session.add(step)
        session.commit()

        with pytest.raises(ScriptWorkOrderValidationError, match="job|任务目录|身份"):
            import_script_work_order_result(session, built.pipeline_id)

        session.expire_all()
        assert session.get(ScriptAsset, pipeline.script_asset_id).voiceover == old_voiceover


@pytest.mark.parametrize(
    ("pipeline_offset", "job_id"),
    [
        (1, None),
        (0, "pipeline-999-20260101-000000-deadbeef"),
    ],
)
def test_import_rejects_result_meta_not_bound_to_current_pipeline_and_job(
    d_material_root,
    pipeline_offset: int,
    job_id: str | None,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        payload = valid_factory_result(
            pipeline_id=built.pipeline_id + pipeline_offset,
            job_id=job_id or build_result.job_id,
        )
        write_factory_result(Path(build_result.job_dir), payload)
        script = session.get(ScriptAsset, built.script_asset_id)
        old_voiceover = script.voiceover

        with pytest.raises(ScriptWorkOrderValidationError, match="pipeline|job|项目|任务"):
            import_script_work_order_result(session, built.pipeline_id)

        session.expire_all()
        assert session.get(ScriptAsset, built.script_asset_id).voiceover == old_voiceover


def test_import_invalid_result_file_keeps_existing_script_unchanged(d_material_root):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        old_script = session.get(ScriptAsset, built.script_asset_id)
        old_voiceover = old_script.voiceover
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(
            Path(build_result.job_dir),
            {"voiceover": "", "storyboard": [], "subtitles": []},
        )

        with pytest.raises(ScriptWorkOrderValidationError, match="口播稿不能为空"):
            import_script_work_order_result(session, built.pipeline_id)

        session.expire_all()
        script = session.get(ScriptAsset, built.script_asset_id)
        assert script.voiceover == old_voiceover


def test_accept_blocks_missing_fact_sources_before_material_flow(d_material_root):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        payload = valid_factory_result(
            pipeline_id=built.pipeline_id,
            job_id=build_result.job_id,
        )
        payload["fact_check"]["sources"] = []
        write_factory_result(Path(build_result.job_dir), payload)
        import_script_work_order_result(session, built.pipeline_id)

        result = accept_script_work_order(session, built.pipeline_id)
        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        statuses = {step.name: step.status for step in pipeline.steps}

        assert result.status == "blocked"
        assert result.accepted is False
        assert "事实来源不足" in result.failure_reason
        assert statuses["生成素材任务"] == "未开始"


def test_accept_passes_quality_and_continues_to_material_tasks(d_material_root):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(build_result.job_dir))
        import_script_work_order_result(session, built.pipeline_id)

        result = accept_script_work_order(session, built.pipeline_id)
        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        statuses = {step.name: step.status for step in pipeline.steps}
        material_step = next(step for step in pipeline.steps if step.name == "生成素材任务")
        first_task = material_step.output_summary["material_tasks"][0]

        assert result.status == "accepted"
        assert result.accepted is True
        assert result.real_ai_called is False
        assert result.quality_check["local_rules_only"] is True
        assert statuses["生成素材任务"] == "已完成"
        assert first_task["source_priority"] == ["官方录屏", "信息动效", "Pexels视频"]
        assert first_task["asset_keywords"] == ["AI工具", "企业采购"]
        assert material_step.output_summary["shot_plan_count"] == 2
        assert material_step.output_summary["script_version_id"]
        assert first_task["beat_id"] == "beat-01"
        assert first_task["director_version"] == "semantic-director-v1"
        workspace = get_script_workspace(session, built.pipeline_id)
        assert workspace.material_tasks[0]["viewer_takeaway"]
        assert workspace.material_tasks[0]["acceptance_conditions"]


def test_accept_marks_imported_script_version_approved_without_mutating_snapshot(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(build_result.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        before = ledger.list()[0]

        result = accept_script_work_order(session, built.pipeline_id)

        assert result.accepted is True
        after = ledger.get(before.version_id)
        assert after.status == "approved"
        assert after.content_hash == before.content_hash
        assert after.payload == before.payload


def test_manual_script_edit_creates_current_draft_and_invalidates_prior_approval(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(build_result.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        accept_script_work_order(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        approved = ledger.latest_approved()
        assert approved is not None

        update_script_workspace(
            session,
            built.pipeline_id,
            {
                "voiceover": "手动改过的口播",
                "storyboard": [
                    {
                        "shot_id": "shot-01",
                        "start": "00:00",
                        "end": "00:04",
                        "voiceover": "手动改过的口播",
                        "visual_intent": "新的解释画面",
                    }
                ],
                "subtitles": [{"start": "00:00", "end": "00:04", "text": "手动改过的口播"}],
                "title_options": [],
                "cover_copy": [],
                "publish_copy": {},
            },
        )

        versions = ledger.list()
        assert len(versions) == 2
        assert ledger.get(approved.version_id).status == "invalidated"
        assert ledger.latest_approved() is None
        assert versions[-1].status == "draft"
        assert versions[-1].payload["voiceover"] == "手动改过的口播"
        workspace = get_script_workspace(session, built.pipeline_id)
        assert workspace.active_script_version_id == versions[-1].version_id
        assert workspace.shot_plan == [dict(item) for item in versions[-1].shot_plan]
        with pytest.raises(
            ScriptWorkOrderValidationError,
            match="不是本次脚本工厂导入版本",
        ):
            accept_script_work_order(session, built.pipeline_id)
        assert ledger.get(approved.version_id).status == "invalidated"
        assert ledger.get(versions[-1].version_id).status == "draft"


def test_startup_recovery_finishes_database_committed_manual_draft(
    d_material_root,
    monkeypatch,
):
    class SimulatedProcessCrash(BaseException):
        pass

    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(build_result.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        accept_script_work_order(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        approved = ledger.latest_approved()
        assert approved is not None
        real_finalize = workspace_service_module.finalize_script_draft_operation

        def crash_before_draft_outcome(*_args, **_kwargs):
            raise SimulatedProcessCrash("database committed before draft outcome")

        monkeypatch.setattr(
            workspace_service_module,
            "finalize_script_draft_operation",
            crash_before_draft_outcome,
        )
        with pytest.raises(SimulatedProcessCrash):
            update_script_workspace(
                session,
                built.pipeline_id,
                {
                    "voiceover": "崩溃恢复后的手动草稿",
                    "storyboard": [
                        {
                            "shot_id": "shot-01",
                            "start": "00:00",
                            "end": "00:04",
                            "voiceover": "崩溃恢复后的手动草稿",
                            "visual_intent": "恢复验证画面",
                        }
                    ],
                    "subtitles": [
                        {
                            "start": "00:00",
                            "end": "00:04",
                            "text": "崩溃恢复后的手动草稿",
                        }
                    ],
                    "title_options": [],
                    "cover_copy": [],
                    "publish_copy": {},
                },
            )
        monkeypatch.setattr(
            workspace_service_module,
            "finalize_script_draft_operation",
            real_finalize,
        )

        session.rollback()
        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        assert ledger.get(approved.version_id).status == "approved"
        pending = ledger.pending_operations()
        assert len(pending) == 1
        assert pending[0].kind == "draft"

        monkeypatch.setattr(
            main_module,
            "SessionLocal",
            sessionmaker(
                bind=session.get_bind(),
                autoflush=False,
                autocommit=False,
            ),
        )
        report = main_module._run_startup_script_version_recovery()
        assert report["completed"] == 1
        assert report["blocked"] == []
        assert ledger.get(approved.version_id).status == "invalidated"
        assert ledger.get(pending[0].version_id).status == "draft"
        assert ledger.pending_operations() == []


def test_new_import_invalidates_old_approval_before_new_version_is_accepted(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        first_job = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(first_job.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        accept_script_work_order(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        first_approved = ledger.latest_approved()
        assert first_approved is not None

        second_job = build_script_work_order(session, built.pipeline_id)
        second_payload = valid_factory_result(
            pipeline_id=built.pipeline_id,
            job_id=second_job.job_id,
        )
        second_payload["voiceover"] += "\n第二版补充了一个可核验结论。"
        write_factory_result(Path(second_job.job_dir), second_payload)
        import_script_work_order_result(session, built.pipeline_id)

        after_import = ledger.list()
        assert len(after_import) == 2
        assert ledger.get(first_approved.version_id).status == "invalidated"
        assert ledger.latest_approved() is None
        assert after_import[-1].status == "draft"

        accept_script_work_order(session, built.pipeline_id)

        after_accept = ledger.list()
        assert ledger.get(first_approved.version_id).status == "invalidated"
    assert after_accept[-1].status == "approved"


def test_local_rewrite_invalidates_prior_approval_immediately(d_material_root):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(build_result.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        accept_script_work_order(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        approved = ledger.latest_approved()
        assert approved is not None

        rewrite_script_part(session, built.pipeline_id, "title")

        versions = ledger.list()
        assert ledger.get(approved.version_id).status == "invalidated"
        assert ledger.latest_approved() is None
        assert versions[-1].status == "draft"
        assert versions[-1].source == "local_rewrite:title"


def test_factory_approval_binds_prompt_context_and_template_hashes(d_material_root):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        job_dir = Path(build_result.job_dir)
        write_factory_result(job_dir)
        import_script_work_order_result(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        snapshot = ledger.list()[-1]

        evidence = snapshot.input_evidence
        assert evidence["kind"] == "script_factory_work_order"
        assert evidence["job_id"] == build_result.job_id
        assert set(evidence["files"]) == {
            "script_factory_prompt.md",
            "script_factory_context.json",
            "script_factory_result_template.json",
        }
        assert all(len(value) == 64 for value in evidence["files"].values())
        assert str(job_dir) not in json.dumps(evidence, ensure_ascii=False)

        context_path = job_dir / "script_factory_context.json"
        context_path.write_text(
            context_path.read_text(encoding="utf-8") + "\n ",
            encoding="utf-8",
        )

        with pytest.raises(
            ScriptWorkOrderValidationError,
            match="任务输入|输入证据|哈希|篡改",
        ):
            accept_script_work_order(session, built.pipeline_id)

        assert ledger.get(snapshot.version_id).status == "draft"


def test_factory_import_rejects_task_input_tampering_before_snapshot_creation(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        job_dir = Path(build_result.job_dir)
        write_factory_result(job_dir)
        prompt_path = job_dir / "script_factory_prompt.md"
        prompt_path.write_text(
            prompt_path.read_text(encoding="utf-8") + "\n被插入的额外指令",
            encoding="utf-8",
        )

        with pytest.raises(
            ScriptWorkOrderValidationError,
            match="任务输入|输入证据|哈希|篡改",
        ):
            import_script_work_order_result(session, built.pipeline_id)

        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        assert ledger.list() == []


def test_identical_manual_save_does_not_create_draft_or_revoke_approval(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(build_result.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        accept_script_work_order(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        assert pipeline is not None
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        approved = ledger.latest_approved()
        assert approved is not None
        workspace = get_script_workspace(session, built.pipeline_id)

        update_script_workspace(
            session,
            built.pipeline_id,
            {
                "voiceover": workspace.script["voiceover"],
                "storyboard": workspace.script["storyboard"],
                "subtitles": workspace.script["subtitles"],
                "title_options": workspace.script["title_options"],
                "cover_copy": workspace.script["cover_copy"],
                "publish_copy": workspace.script["publish_copy"],
                "use_avatar": workspace.script["use_avatar"],
            },
        )

        assert len(ledger.list()) == 1
        assert ledger.get(approved.version_id).status == "approved"


def test_new_factory_import_keeps_old_lock_as_stale_history_then_regenerates_current_plan(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        first_job = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(first_job.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        accept_script_work_order(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        first_payload = deepcopy(material_step.output_summary)
        first_version_id = first_payload["script_version_id"]
        shot_id = first_payload["material_tasks"][0]["shot_id"]
        first_payload["candidate_pool"] = {
            shot_id: {
                "shot_id": shot_id,
                "revision": 1,
                "candidates": [
                    {
                        "candidate_id": f"cand-{index}",
                        "source": source,
                        "preview_path": f"素材信息/候选预览/cand-{index}.jpg",
                        "url": f"https://example.test/factory-candidate/{index}",
                        "rights": "commercial-use terms captured",
                        "evidence_kind": "atmosphere",
                        "status": "locked" if index == 1 else "candidate",
                        "reason": "approved for the first script",
                    }
                    for index, source in enumerate(
                        ("Pexels", "Pixabay", "官方页面"),
                        start=1,
                    )
                ],
            }
        }
        first_payload["candidate_selection_history"] = {
            shot_id: [
                {
                    "event": "candidate_locked",
                    "shot_id": shot_id,
                    "previous_candidate_id": None,
                    "selected_candidate_id": "cand-1",
                    "revision_from": 0,
                    "revision_to": 1,
                    "reason": "approved for the first script",
                    "created_at": "2026-07-19T00:00:00+00:00",
                }
            ]
        }
        first_payload["candidate_generation_history"] = [
            {
                "event": "candidate_batch_generated",
                "shot_id": shot_id,
                "entry_sha256": "b" * 64,
            }
        ]
        material_step.output_summary = first_payload
        session.add(material_step)
        session.commit()

        second_job = build_script_work_order(session, built.pipeline_id)
        second_payload = valid_factory_result(
            pipeline_id=built.pipeline_id,
            job_id=second_job.job_id,
        )
        second_payload["voiceover"] = second_payload["voiceover"].replace(
            "这不是热闹",
            "这不是一次普通更新",
        )
        second_payload["storyboard"][0]["voiceover"] = (
            "换成人话说，这不是一次普通更新，而是企业开始认真花预算。"
        )
        write_factory_result(Path(second_job.job_dir), second_payload)

        import_script_work_order_result(session, built.pipeline_id)

        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        stale_payload = material_step.output_summary
        stale_pool = stale_payload["candidate_pool"][shot_id]
        stale_event = stale_payload["material_selection_history"][-1]

        assert material_step.status == "未开始"
        assert stale_payload["material_selection_state"] == "stale"
        assert stale_pool["stale"] is True
        assert stale_event["source_script_version_id"] == first_version_id
        assert stale_event["locked_selections"][0]["candidate_id"] == "cand-1"

        accept_script_work_order(session, built.pipeline_id)

        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        current_payload = material_step.output_summary
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        current_approved = ledger.latest_approved()

        assert material_step.status == "已完成"
        assert current_approved is not None
        assert current_payload["script_version_id"] == current_approved.version_id
        assert current_payload["script_version_id"] != first_version_id
        assert "candidate_pool" not in current_payload
        assert current_payload["material_selection_history"][-1] == stale_event


def test_completed_material_step_blocks_when_current_script_version_is_draft(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(build_result.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        accept_script_work_order(session, built.pipeline_id)
        workspace = get_script_workspace(session, built.pipeline_id)

        update_script_workspace(
            session,
            built.pipeline_id,
            {
                "voiceover": workspace.script["voiceover"],
                "storyboard": workspace.script["storyboard"],
                "subtitles": workspace.script["subtitles"],
                "title_options": [{"title": "只改标题的新草稿", "type": "判断型"}],
                "cover_copy": workspace.script["cover_copy"],
                "publish_copy": workspace.script["publish_copy"],
                "use_avatar": workspace.script["use_avatar"],
            },
        )
        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        assert material_step.status == "已完成"

        continue_pipeline(
            session,
            built.pipeline_id,
            stop_after="生成素材任务",
            material_root=d_material_root,
            project_root=d_material_root / "项目",
        )

        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        assert material_step.status == "失败"
        assert "当前脚本版本尚未批准" in material_step.error


def test_same_material_contract_new_approval_rebinds_completed_material_plan(
    d_material_root,
    monkeypatch,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        first_job = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(first_job.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        accept_script_work_order(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        first_output = deepcopy(material_step.output_summary)
        first_version_id = first_output["script_version_id"]

        second_job = build_script_work_order(session, built.pipeline_id)
        second_payload = valid_factory_result(
            pipeline_id=built.pipeline_id,
            job_id=second_job.job_id,
        )
        second_payload["title_options"] = [
            {"title": "素材不变但标题更新", "type": "判断型"}
        ]
        write_factory_result(Path(second_job.job_dir), second_payload)
        import_script_work_order_result(session, built.pipeline_id)

        real_dependencies = pipeline_service.PipelineDependencies

        def fail_if_replanned(*args, **kwargs):
            raise AssertionError("same material contract must rebind, not regenerate")

        monkeypatch.setattr(
            pipeline_service,
            "PipelineDependencies",
            lambda: real_dependencies(material_planner=fail_if_replanned),
        )
        accept_script_work_order(session, built.pipeline_id)

        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        current_output = material_step.output_summary
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        approved = ledger.latest_approved()

        assert approved is not None
        assert approved.version_id != first_version_id
        anchor_fields = {
            "script_version_id",
            "script_version_hash",
            "script_version_input_hash",
            "script_snapshot_hash",
            "shot_plan_hash",
            "approval_event_hash",
            "approval_evidence_hash",
        }
        assert {
            key: value for key, value in current_output.items() if key not in anchor_fields
        } == {
            key: value for key, value in first_output.items() if key not in anchor_fields
        }
        assert current_output["script_version_id"] == approved.version_id
        assert current_output["script_version_hash"] == approved.content_hash
        assert current_output["script_snapshot_hash"] == approved.snapshot_hash
        assert current_output["shot_plan_hash"] == approved.shot_plan_hash
        assert current_output["approval_event_hash"] == approved.approval_event_hash
        assert current_output["approval_evidence_hash"] == approved.approval_evidence_hash


def test_factory_import_anchors_full_result_and_snapshot_hashes_in_db_state(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(build_result.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        script_step = next(
            step for step in pipeline.steps if step.name == "生成脚本素材"
        )
        state = script_step.output_summary["script_factory"]
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        snapshot = ledger.get(state["script_version_id"])

        assert state["script_result_hash"] == snapshot.input_evidence["result_sha256"]
        assert state["script_version_input_hash"] == snapshot.input_hash
        assert state["script_version_snapshot_hash"] == snapshot.snapshot_hash
        assert state["script_version_shot_plan_hash"] == snapshot.shot_plan_hash


@pytest.mark.parametrize(
    "anchor_field",
    [
        "script_result_hash",
        "script_version_input_hash",
        "script_version_snapshot_hash",
        "script_version_shot_plan_hash",
    ],
)
def test_accept_rejects_tampered_db_version_anchor(
    d_material_root,
    anchor_field,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(build_result.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        script_step = next(
            step for step in pipeline.steps if step.name == "生成脚本素材"
        )
        output = deepcopy(script_step.output_summary)
        state = deepcopy(output["script_factory"])
        version_id = state["script_version_id"]
        state[anchor_field] = "0" * 64
        output["script_factory"] = state
        output["script_work_order"] = deepcopy(state)
        script_step.output_summary = output
        session.add(script_step)
        session.commit()

        with pytest.raises(
            ScriptWorkOrderValidationError,
            match="结果|输入|快照|ShotPlan|哈希|不一致",
        ):
            accept_script_work_order(session, built.pipeline_id)

        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        assert ledger.get(version_id).status == "draft"
        assert material_step.status == "未开始"


def test_accept_rejects_quality_evidence_changed_after_import(d_material_root):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        result_path = write_factory_result(Path(build_result.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payload["quality_self_check"]["judgement"] = "导入后被替换的判断"
        result_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        with pytest.raises(
            ScriptWorkOrderValidationError,
            match="结果|证据|哈希|篡改",
        ):
            accept_script_work_order(session, built.pipeline_id)


def test_accept_rejects_rehashed_shot_plan_that_differs_from_director_output(
    d_material_root,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(build_result.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        snapshot = ledger.list()[-1]
        version_path = (
            Path(pipeline.project_dir)
            / "脚本版本"
            / "versions"
            / f"{snapshot.version_id}.json"
        )
        raw = json.loads(version_path.read_text(encoding="utf-8"))
        raw["shot_plan"][0]["viewer_takeaway"] = "被重算哈希掩盖的非法镜头改动"
        raw["shot_plan_hash"] = canonical_sha256(raw["shot_plan"])
        raw["snapshot_hash"] = canonical_sha256(
            {
                key: raw[key]
                for key in (
                    "schema_version",
                    "pipeline_id",
                    "project_key",
                    "version_id",
                    "version_number",
                    "input_hash",
                    "content_hash",
                    "payload",
                    "fact_sources",
                    "input_evidence",
                    "shot_plan",
                    "shot_plan_hash",
                    "source",
                    "created_at",
                )
            }
        )
        version_path.write_text(
            json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

        with pytest.raises(
            ScriptWorkOrderValidationError,
            match="ShotPlan|快照|哈希|镜头",
        ):
            accept_script_work_order(session, built.pipeline_id)


def test_accept_cas_conflict_compensates_new_approval(
    d_material_root,
    monkeypatch,
):
    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(build_result.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        script_step = next(
            step for step in pipeline.steps if step.name == "生成脚本素材"
        )
        version_id = script_step.output_summary["script_factory"]["script_version_id"]
        original_commit = script_work_order_module._commit_factory_state_with_cas

        def change_state_then_commit(
            db: Session,
            pipeline: ProductionPipeline,
            factory_payload: dict,
            *,
            expected_output: dict,
            pipeline_status: str,
        ):
            script_step = next(
                step for step in pipeline.steps if step.name == "生成脚本素材"
            )
            output = deepcopy(script_step.output_summary)
            output["concurrent_operation_marker"] = "changed-during-quality"
            script_step.output_summary = output
            db.add(script_step)
            db.commit()
            return original_commit(
                db,
                pipeline,
                factory_payload,
                expected_output=expected_output,
                pipeline_status=pipeline_status,
            )

        monkeypatch.setattr(
            script_work_order_module,
            "_commit_factory_state_with_cas",
            change_state_then_commit,
        )

        with pytest.raises(
            ScriptWorkOrderValidationError,
            match="并发|变化|冲突|重试",
        ):
            accept_script_work_order(session, built.pipeline_id)

        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        script_step = next(
            step for step in pipeline.steps if step.name == "生成脚本素材"
        )
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        event_types = {
            raw["event"]
            for path in (
                Path(pipeline.project_dir) / "脚本版本" / "events"
            ).glob("event-*.json")
            if (raw := json.loads(path.read_text(encoding="utf-8")))["version_id"]
            == version_id
        }

        assert ledger.latest_approved() is None
        assert ledger.get(version_id).status == "invalidated"
        assert script_step.output_summary["concurrent_operation_marker"] == (
            "changed-during-quality"
        )
        assert material_step.status == "未开始"
        assert event_types == {"approved", "invalidated"}


def test_startup_recovery_compensates_approval_event_without_database_commit(
    d_material_root,
    monkeypatch,
):
    class SimulatedProcessCrash(BaseException):
        pass

    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(build_result.job_dir))
        import_script_work_order_result(session, built.pipeline_id)

        def crash_before_database_commit(*_args, **_kwargs):
            raise SimulatedProcessCrash("process stopped after approval event")

        monkeypatch.setattr(
            script_work_order_module,
            "_commit_factory_state_with_cas",
            crash_before_database_commit,
        )
        with pytest.raises(SimulatedProcessCrash):
            accept_script_work_order(session, built.pipeline_id)

        session.rollback()
        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        pending = ledger.pending_operations()
        assert len(pending) == 1
        assert pending[0].kind == "approve"

        report = reconcile_pending_script_version_operations(
            session,
            project_root=d_material_root / "项目",
            pipeline_id=built.pipeline_id,
        )
        repeated = reconcile_pending_script_version_operations(
            session,
            project_root=d_material_root / "项目",
            pipeline_id=built.pipeline_id,
        )

        assert report["compensated"] == 1
        assert report["blocked"] == []
        assert repeated["scanned_operations"] == 0
        assert ledger.pending_operations() == []
        assert ledger.get(pending[0].version_id).status == "invalidated"
        operation_dir = Path(pipeline.project_dir) / "脚本版本" / "operations"
        assert len(
            list(
                operation_dir.glob(
                    f"operation-{pending[0].operation_id}-outcome-*.json"
                )
            )
        ) == 1
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        assert material_step.status == "未开始"


def test_startup_recovery_completes_database_committed_approval_and_retry_is_idempotent(
    d_material_root,
    monkeypatch,
):
    class SimulatedProcessCrash(BaseException):
        pass

    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        build_result = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(build_result.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        real_finish = ScriptVersionLedger.finish_operation

        def crash_before_operation_outcome(self, operation_id, *, outcome, result=None):
            raise SimulatedProcessCrash("database committed before outbox outcome")

        monkeypatch.setattr(
            ScriptVersionLedger,
            "finish_operation",
            crash_before_operation_outcome,
        )
        with pytest.raises(SimulatedProcessCrash):
            accept_script_work_order(session, built.pipeline_id)
        monkeypatch.setattr(ScriptVersionLedger, "finish_operation", real_finish)

        session.rollback()
        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        script_step = next(
            step for step in pipeline.steps if step.name == "生成脚本素材"
        )
        state = script_step.output_summary["script_factory"]
        assert state["status"] == "accepted"
        assert len(state["approval_operation_id"]) == 32

        report = reconcile_pending_script_version_operations(
            session,
            project_root=d_material_root / "项目",
            pipeline_id=built.pipeline_id,
        )
        assert report["completed"] == 1
        assert report["blocked"] == []

        result = accept_script_work_order(session, built.pipeline_id)
        assert result.accepted is True
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        approval_events = [
            event
            for operation in ledger.list_operations()
            for event in ledger.events_for_operation(operation.operation_id)
            if event.get("event") == "approved"
        ]
        assert len(approval_events) == 1
        assert ledger.pending_operations() == []
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        assert material_step.status == "已完成"


def test_startup_recovery_finishes_import_invalidation_after_database_commit(
    d_material_root,
    monkeypatch,
):
    class SimulatedProcessCrash(BaseException):
        pass

    with make_session() as session:
        built = build_pipeline(session, d_material_root)
        first_build = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(first_build.job_dir))
        import_script_work_order_result(session, built.pipeline_id)
        accept_script_work_order(session, built.pipeline_id)
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        previous = ledger.latest_approved()
        assert previous is not None

        second_build = build_script_work_order(session, built.pipeline_id)
        write_factory_result(Path(second_build.job_dir))
        real_invalidate = ScriptVersionLedger.invalidate

        def crash_before_previous_approval_invalidation(
            self,
            version_id,
            *,
            reason,
            operation_id="",
        ):
            if version_id == previous.version_id and operation_id:
                raise SimulatedProcessCrash("database committed before invalidation")
            return real_invalidate(
                self,
                version_id,
                reason=reason,
                operation_id=operation_id,
            )

        monkeypatch.setattr(
            ScriptVersionLedger,
            "invalidate",
            crash_before_previous_approval_invalidation,
        )
        with pytest.raises(SimulatedProcessCrash):
            import_script_work_order_result(session, built.pipeline_id)
        monkeypatch.setattr(ScriptVersionLedger, "invalidate", real_invalidate)

        session.rollback()
        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        script_step = next(
            step for step in pipeline.steps if step.name == "生成脚本素材"
        )
        current_version_id = script_step.output_summary["script_factory"][
            "script_version_id"
        ]
        assert current_version_id != previous.version_id

        report = reconcile_pending_script_version_operations(
            session,
            project_root=d_material_root / "项目",
            pipeline_id=built.pipeline_id,
        )
        assert report["completed"] == 1
        assert report["blocked"] == []
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        assert ledger.get(previous.version_id).status == "invalidated"
        assert ledger.get(current_version_id).status == "draft"
        assert ledger.pending_operations() == []


def _prepare_two_approved_script_versions(session: Session, material_root: Path):
    built = build_pipeline(session, material_root)

    first_build = build_script_work_order(session, built.pipeline_id)
    write_factory_result(Path(first_build.job_dir))
    import_script_work_order_result(session, built.pipeline_id)
    accept_script_work_order(session, built.pipeline_id)
    pipeline = session.get(ProductionPipeline, built.pipeline_id)
    ledger = ScriptVersionLedger(pipeline, project_root=material_root / "项目")
    first = ledger.latest_approved()
    assert first is not None

    second_build = build_script_work_order(session, built.pipeline_id)
    second_payload = valid_factory_result(
        pipeline_id=built.pipeline_id,
        job_id=second_build.job_id,
    )
    second_payload["voiceover"] = (
        "第二版把重点改成采购落地，而不是只谈预算。\n"
        "现在要看部署范围、复购和实际节省的成本。"
    )
    second_payload["storyboard"][0]["voiceover"] = (
        "第二版把重点改成采购落地，而不是只谈预算。"
    )
    second_payload["subtitles"][0]["text"] = (
        "第二版把重点改成采购落地，而不是只谈预算。"
    )
    write_factory_result(Path(second_build.job_dir), second_payload)
    import_script_work_order_result(session, built.pipeline_id)
    accept_script_work_order(session, built.pipeline_id)

    session.expire_all()
    pipeline = session.get(ProductionPipeline, built.pipeline_id)
    ledger = ScriptVersionLedger(pipeline, project_root=material_root / "项目")
    current = ledger.latest_approved()
    assert current is not None
    assert current.version_id != first.version_id
    script = session.get(ScriptAsset, pipeline.script_asset_id)
    return (
        built.pipeline_id,
        ledger.get(first.version_id),
        current,
        script_workspace_state_sha256(pipeline, script),
    )


def _restore_call(
    session: Session,
    material_root: Path,
    *,
    pipeline_id: int,
    source,
    current,
    expected_script_state_sha256: str,
    operation_id: str,
):
    return restore_script_version_as_draft(
        session,
        pipeline_id,
        source.version_id,
        operation_id=operation_id,
        source_content_hash=source.content_hash,
        source_snapshot_hash=source.snapshot_hash,
        expected_current_version_id=current.version_id,
        expected_current_content_hash=current.content_hash,
        expected_current_snapshot_hash=current.snapshot_hash,
        expected_script_state_sha256=expected_script_state_sha256,
        project_root=material_root / "项目",
    )


def test_restore_history_creates_new_draft_preserves_snapshots_and_retries_idempotently(
    d_material_root,
):
    with make_session() as session:
        pipeline_id, source, current, state_sha256 = _prepare_two_approved_script_versions(
            session,
            d_material_root,
        )
        source_path = (
            Path(session.get(ProductionPipeline, pipeline_id).project_dir)
            / "脚本版本"
            / "versions"
            / f"{source.version_id}.json"
        )
        source_bytes = source_path.read_bytes()
        operation_id = "a" * 32

        restored = _restore_call(
            session,
            d_material_root,
            pipeline_id=pipeline_id,
            source=source,
            current=current,
            expected_script_state_sha256=state_sha256,
            operation_id=operation_id,
        )
        repeated = _restore_call(
            session,
            d_material_root,
            pipeline_id=pipeline_id,
            source=source,
            current=current,
            expected_script_state_sha256=state_sha256,
            operation_id=operation_id,
        )
        with pytest.raises(ScriptWorkOrderConflictError, match="operation_id"):
            restore_script_version_as_draft(
                session,
                pipeline_id,
                source.version_id,
                operation_id=operation_id,
                source_content_hash="9" * 64,
                source_snapshot_hash=source.snapshot_hash,
                expected_current_version_id=current.version_id,
                expected_current_content_hash=current.content_hash,
                expected_current_snapshot_hash=current.snapshot_hash,
                expected_script_state_sha256=state_sha256,
                project_root=d_material_root / "项目",
            )

        session.expire_all()
        pipeline = session.get(ProductionPipeline, pipeline_id)
        ledger = ScriptVersionLedger(
            pipeline,
            project_root=d_material_root / "项目",
        )
        versions = ledger.list()
        target = ledger.get(restored.restored_version_id)
        script = session.get(ScriptAsset, pipeline.script_asset_id)
        script_step = next(
            step for step in pipeline.steps if step.name == "生成脚本素材"
        )
        state = script_step.output_summary["script_factory"]
        restore_operations = [
            item for item in ledger.list_operations() if item.kind == "restore"
        ]

        assert restored.idempotent is False
        assert repeated.idempotent is True
        assert repeated.restored_version_id == restored.restored_version_id
        assert len(versions) == 3
        assert target.version_number == 3
        assert target.status == "draft"
        assert {
            key: target.payload[key] for key in source.payload
        } == source.payload
        assert target.payload["use_avatar"] is True
        assert target.fact_sources == source.fact_sources
        assert target.source == f"restore:{source.version_id}"
        assert ledger.get(current.version_id).status == "invalidated"
        assert source_path.read_bytes() == source_bytes
        assert script.voiceover == source.payload["voiceover"]
        assert state["status"] == "draft_edited"
        assert state["script_version_id"] == target.version_id
        assert state["restore_operation_id"] == operation_id
        assert state["restored_from_version_id"] == source.version_id
        assert len(restore_operations) == 1
        assert restore_operations[0].status == "completed"
        assert ledger.pending_operations() == []


def test_restore_rejects_stale_source_or_current_anchors_without_side_effects(
    d_material_root,
):
    with make_session() as session:
        pipeline_id, source, current, state_sha256 = _prepare_two_approved_script_versions(
            session,
            d_material_root,
        )
        pipeline = session.get(ProductionPipeline, pipeline_id)
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        version_count = len(ledger.list())
        operation_count = len(ledger.list_operations())

        with pytest.raises(ScriptWorkOrderConflictError, match="历史脚本版本.*变化"):
            restore_script_version_as_draft(
                session,
                pipeline_id,
                source.version_id,
                operation_id="b" * 32,
                source_content_hash="0" * 64,
                source_snapshot_hash=source.snapshot_hash,
                expected_current_version_id=current.version_id,
                expected_current_content_hash=current.content_hash,
                expected_current_snapshot_hash=current.snapshot_hash,
                expected_script_state_sha256=state_sha256,
                project_root=d_material_root / "项目",
            )

        with pytest.raises(ScriptWorkOrderConflictError, match="当前脚本版本.*变化"):
            restore_script_version_as_draft(
                session,
                pipeline_id,
                source.version_id,
                operation_id="c" * 32,
                source_content_hash=source.content_hash,
                source_snapshot_hash=source.snapshot_hash,
                expected_current_version_id=current.version_id,
                expected_current_content_hash="1" * 64,
                expected_current_snapshot_hash=current.snapshot_hash,
                expected_script_state_sha256=state_sha256,
                project_root=d_material_root / "项目",
            )

        with pytest.raises(ScriptWorkOrderConflictError, match="展示设置已变化"):
            restore_script_version_as_draft(
                session,
                pipeline_id,
                source.version_id,
                operation_id="7" * 32,
                source_content_hash=source.content_hash,
                source_snapshot_hash=source.snapshot_hash,
                expected_current_version_id=current.version_id,
                expected_current_content_hash=current.content_hash,
                expected_current_snapshot_hash=current.snapshot_hash,
                expected_script_state_sha256="2" * 64,
                project_root=d_material_root / "项目",
            )

        session.expire_all()
        pipeline = session.get(ProductionPipeline, pipeline_id)
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        state = next(
            step for step in pipeline.steps if step.name == "生成脚本素材"
        ).output_summary["script_factory"]
        assert state["script_version_id"] == current.version_id
        assert ledger.get(current.version_id).status == "approved"
        assert len(ledger.list()) == version_count
        assert len(ledger.list_operations()) == operation_count


def test_restore_rejects_tampered_history_and_keeps_current_approval(
    d_material_root,
):
    with make_session() as session:
        pipeline_id, source, current, state_sha256 = _prepare_two_approved_script_versions(
            session,
            d_material_root,
        )
        pipeline = session.get(ProductionPipeline, pipeline_id)
        source_path = (
            Path(pipeline.project_dir)
            / "脚本版本"
            / "versions"
            / f"{source.version_id}.json"
        )
        raw = json.loads(source_path.read_text(encoding="utf-8"))
        raw["payload"]["voiceover"] = "篡改后的历史脚本"
        source_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        with pytest.raises(ScriptVersionError, match="哈希|快照"):
            _restore_call(
                session,
                d_material_root,
                pipeline_id=pipeline_id,
                source=source,
                current=current,
                expected_script_state_sha256=state_sha256,
                operation_id="d" * 32,
            )

        session.expire_all()
        pipeline = session.get(ProductionPipeline, pipeline_id)
        state = next(
            step for step in pipeline.steps if step.name == "生成脚本素材"
        ).output_summary["script_factory"]
        assert state["script_version_id"] == current.version_id


def test_restore_cas_loss_abandons_operation_without_invalidating_current_approval(
    d_material_root,
    monkeypatch,
):
    with make_session() as session:
        pipeline_id, source, current, state_sha256 = _prepare_two_approved_script_versions(
            session,
            d_material_root,
        )

        def lose_restore_cas(db, *_args, **_kwargs):
            db.rollback()
            raise ScriptWorkOrderConflictError("restore CAS lost")

        monkeypatch.setattr(
            script_work_order_module,
            "_commit_restore_state_with_cas",
            lose_restore_cas,
        )
        with pytest.raises(ScriptWorkOrderConflictError, match="restore CAS lost"):
            _restore_call(
                session,
                d_material_root,
                pipeline_id=pipeline_id,
                source=source,
                current=current,
                expected_script_state_sha256=state_sha256,
                operation_id="e" * 32,
            )

        session.expire_all()
        pipeline = session.get(ProductionPipeline, pipeline_id)
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        state = next(
            step for step in pipeline.steps if step.name == "生成脚本素材"
        ).output_summary["script_factory"]
        restore_operations = [
            item for item in ledger.list_operations() if item.kind == "restore"
        ]
        assert state["script_version_id"] == current.version_id
        assert ledger.get(current.version_id).status == "approved"
        assert len(restore_operations) == 1
        assert restore_operations[0].status == "abandoned"
        assert ledger.pending_operations() == []


def test_restore_reuses_orphan_snapshot_after_crash_before_intent(
    d_material_root,
    monkeypatch,
):
    class SimulatedProcessCrash(BaseException):
        pass

    with make_session() as session:
        pipeline_id, source, current, state_sha256 = (
            _prepare_two_approved_script_versions(session, d_material_root)
        )
        real_begin_operation = ScriptVersionLedger.begin_operation
        crashed = False

        def crash_once_before_intent(self, **kwargs):
            nonlocal crashed
            if kwargs.get("operation_id") == "6" * 32 and not crashed:
                crashed = True
                raise SimulatedProcessCrash("snapshot durable before intent")
            return real_begin_operation(self, **kwargs)

        monkeypatch.setattr(
            ScriptVersionLedger,
            "begin_operation",
            crash_once_before_intent,
        )
        with pytest.raises(SimulatedProcessCrash):
            _restore_call(
                session,
                d_material_root,
                pipeline_id=pipeline_id,
                source=source,
                current=current,
                expected_script_state_sha256=state_sha256,
                operation_id="6" * 32,
            )
        monkeypatch.setattr(
            ScriptVersionLedger,
            "begin_operation",
            real_begin_operation,
        )

        pipeline = session.get(ProductionPipeline, pipeline_id)
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        orphan_targets = [
            item
            for item in ledger.list()
            if (item.input_evidence or {}).get("restore_lineage", {}).get(
                "operation_id"
            )
            == "6" * 32
        ]
        assert len(orphan_targets) == 1
        assert not [item for item in ledger.list_operations() if item.kind == "restore"]
        assert ledger.get(current.version_id).status == "approved"

        restored = _restore_call(
            session,
            d_material_root,
            pipeline_id=pipeline_id,
            source=source,
            current=current,
            expected_script_state_sha256=state_sha256,
            operation_id="6" * 32,
        )
        assert restored.restored_version_id == orphan_targets[0].version_id
        assert len(ledger.list()) == 3
        assert ledger.list_operations()[-1].status == "completed"


def test_startup_recovery_finishes_committed_restore_after_invalidation_crash(
    d_material_root,
    monkeypatch,
):
    class SimulatedProcessCrash(BaseException):
        pass

    with make_session() as session:
        pipeline_id, source, current, state_sha256 = _prepare_two_approved_script_versions(
            session,
            d_material_root,
        )
        real_invalidate = ScriptVersionLedger.invalidate

        def crash_before_previous_approval_invalidation(
            self,
            version_id,
            *,
            reason,
            operation_id="",
        ):
            if version_id == current.version_id and operation_id == "f" * 32:
                raise SimulatedProcessCrash("restore committed before invalidation")
            return real_invalidate(
                self,
                version_id,
                reason=reason,
                operation_id=operation_id,
            )

        monkeypatch.setattr(
            ScriptVersionLedger,
            "invalidate",
            crash_before_previous_approval_invalidation,
        )
        with pytest.raises(SimulatedProcessCrash):
            _restore_call(
                session,
                d_material_root,
                pipeline_id=pipeline_id,
                source=source,
                current=current,
                expected_script_state_sha256=state_sha256,
                operation_id="f" * 32,
            )
        monkeypatch.setattr(ScriptVersionLedger, "invalidate", real_invalidate)

        session.rollback()
        session.expire_all()
        pipeline = session.get(ProductionPipeline, pipeline_id)
        state = next(
            step for step in pipeline.steps if step.name == "生成脚本素材"
        ).output_summary["script_factory"]
        restored_version_id = state["script_version_id"]
        assert state["restore_operation_id"] == "f" * 32

        report = reconcile_pending_script_version_operations(
            session,
            project_root=d_material_root / "项目",
            pipeline_id=pipeline_id,
        )
        assert report["completed"] == 1
        assert report["blocked"] == []
        ledger = ScriptVersionLedger(pipeline, project_root=d_material_root / "项目")
        assert ledger.get(current.version_id).status == "invalidated"
        assert ledger.get(restored_version_id).status == "draft"
        assert ledger.pending_operations() == []


def test_restore_script_version_api_returns_new_workspace_and_rejects_bad_shape(
    d_material_root,
):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        pipeline_id, source, current, state_sha256 = (
            _prepare_two_approved_script_versions(setup_session, d_material_root)
        )

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/pipeline/{pipeline_id}/script-versions/{source.version_id}/restore",
            json={
                "operation_id": "8" * 32,
                "source_content_hash": source.content_hash,
                "source_snapshot_hash": source.snapshot_hash,
                "expected_current_version_id": current.version_id,
                "expected_current_content_hash": current.content_hash,
                "expected_current_snapshot_hash": current.snapshot_hash,
                "expected_script_state_sha256": state_sha256,
            },
        )
        invalid_response = client.post(
            f"/api/pipeline/{pipeline_id}/script-versions/{source.version_id}/restore",
            json={"operation_id": "NOT-VALID"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["operation"]["operation_id"] == "8" * 32
    assert data["operation"]["source_version_id"] == source.version_id
    assert data["operation"]["status"] == "draft"
    assert data["workspace"]["active_script_version_id"] == data["operation"][
        "restored_version_id"
    ]
    assert data["workspace"]["script_factory"]["status"] == "draft_edited"
    assert len(data["workspace"]["script_state_sha256"]) == 64
    assert invalid_response.status_code == 422


def test_script_work_order_api_returns_v2_status_contract(d_material_root):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        built = build_pipeline(setup_session, d_material_root)
        pipeline_id = built.pipeline_id

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        build_response = client.post(
            f"/api/pipeline/{pipeline_id}/script-work-order/build"
        )
        dispatch_response = client.post(
            f"/api/pipeline/{pipeline_id}/script-work-order/dispatch"
        )
        build_data = build_response.json()
        write_factory_result(Path(build_data["job_dir"]))
        import_response = client.put(
            f"/api/pipeline/{pipeline_id}/script-work-order/import"
        )
        accept_response = client.post(
            f"/api/pipeline/{pipeline_id}/script-work-order/accept"
        )
    finally:
        app.dependency_overrides.clear()

    assert build_response.status_code == 200
    assert dispatch_response.status_code == 200
    assert import_response.status_code == 200
    assert accept_response.status_code == 200
    for response in [
        build_response,
        dispatch_response,
        import_response,
        accept_response,
    ]:
        data = response.json()
        assert data["status"]
        assert data["job_dir"]
        assert data["file_path"]
        assert data["real_ai_called"] is False
        assert "failure_reason" in data
        assert data["next_suggestion"]
    assert dispatch_response.json()["status"] == "manual_dispatch_required"
    assert accept_response.json()["accepted"] is True


@pytest.mark.parametrize(
    ("method", "route", "target_name", "error", "expected_status"),
    [
        (
            "put",
            "/api/pipeline/1/script-work-order/import",
            "import_script_work_order_result",
            ScriptWorkOrderConflictError("known CAS conflict"),
            409,
        ),
        (
            "put",
            "/api/pipeline/1/script-work-order/import",
            "import_script_work_order_result",
            ScriptWorkOrderCommitUnknownError("commit acknowledgement unknown"),
            503,
        ),
        (
            "post",
            "/api/pipeline/1/script-work-order/accept",
            "accept_script_work_order",
            ScriptWorkOrderReconcileError("operation still requires reconciliation"),
            503,
        ),
    ],
)
def test_script_work_order_api_maps_conflict_and_reconciliation_statuses(
    monkeypatch,
    method,
    route,
    target_name,
    error,
    expected_status,
):
    def override_get_db():
        yield object()

    def fail_submission(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(main_module, target_name, fail_submission)
    app.dependency_overrides[get_db] = override_get_db
    try:
        response = getattr(TestClient(app), method)(route)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == expected_status
    assert response.json()["detail"] == str(error)
