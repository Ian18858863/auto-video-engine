from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.providers.base import AIProvider
from app.ai.schemas import AIRequest, AIResponse
from app.core.database import Base, get_db
import app.main as main_module
from app.main import app
from app.models import (
    AiGeneration,
    HotItem,
    PipelineStep,
    ProductionPipeline,
    ScriptAsset,
    TopicIdea,
)
from app.materials.paths import ProjectMaterialPaths
from app.pipeline.service import (
    FakeProviderRequiredError,
    PipelineDependencies,
    PipelineNotFoundError,
    PipelineStepCommitUnknownError,
    PipelineStepConflictError,
    build_project_from_hotspot,
    continue_pipeline,
    get_pipeline_status,
)
from app.pipeline.models import PIPELINE_STEP_NAMES
import app.pipeline.service as pipeline_service
from app.pipeline.workspace_service import update_script_workspace
from app.pipeline.v1_acceptance_service import (
    ManualV1RunRequest,
    check_and_repair_pipeline_script,
    run_manual_v1_acceptance,
)


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def prepared_material_root(tmp_path: Path) -> Path:
    root = tmp_path / "短视频工厂"
    (root / "项目").mkdir(parents=True, exist_ok=True)
    return root


def add_hot_item(session: Session) -> HotItem:
    item = HotItem(
        source_platform="手动导入",
        source_type="manual",
        title="AI 工具开始进入企业采购流程",
        url="https://example.com/pipeline-ai-buying",
        published_at=datetime.now(timezone.utc),
        summary="企业开始把 AI 工具放进真实预算，需要看懂背后的采购逻辑。",
        metrics={"rank": 1},
        tags=["AI", "工具", "商业"],
        category="AI",
        hot_score=88,
        topic_score=92,
        save_score=85,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def test_build_project_from_hotspot_with_fake_provider_exports_complete_package(tmp_path):
    with make_session() as session:
        hot_item = add_hot_item(session)

        result = build_project_from_hotspot(
            session,
            hot_item.id,
            material_root=prepared_material_root(tmp_path),
        )

        assert result.provider == "fake"
        assert result.status == "需要人工确认"
        assert result.completion_reason_code == "approval_missing"
        assert result.current_step == "成片验收"
        assert result.next_surface == "finished"
        assert session.query(ProductionPipeline).count() == 1
        assert session.query(PipelineStep).count() == len(PIPELINE_STEP_NAMES)
        assert session.query(TopicIdea).count() == 4
        assert session.query(ScriptAsset).count() == 1
        ai_generations = session.query(AiGeneration).all()
        assert len(ai_generations) == 2
        assert all(generation.provider == "fake" for generation in ai_generations)
        assert all(generation.output_content["is_fake"] is True for generation in ai_generations)
        assert all(step.status == "已完成" for step in result.steps)
        assert result.material_tasks
        assert result.graphic_tasks
        assert any(task["是否需要 Pexels"] for task in result.material_tasks)
        assert any(task["是否需要信息动效"] for task in result.material_tasks)
        assert result.material_tasks[0]["素材状态"] == "待执行"
        assert all(task["素材状态"] == "等待" for task in result.material_tasks[1:])
        assert all(task["是否已调用 Codex skill"] is False for task in result.graphic_tasks)
        assert all(task["数字人位置"] == "右下角" for task in result.material_tasks)
        assert all(task["数字人素材已生成"] is False for task in result.material_tasks)

        project_dir = Path(result.project_dir)
        for dirname in [
            "Pexels素材",
            "官方录屏",
            "信息动效",
            "全部素材",
            "素材信息",
        ]:
            assert (project_dir / dirname).is_dir()
        for dirname in [
            "原始素材",
            "官方证据画面",
            "PPT动态图文",
            "数字人素材",
            "本地素材匹配",
            "已选用素材",
            "已拒绝素材",
            "剪映素材",
        ]:
            assert not (project_dir / dirname).exists()

        script_path = project_dir / "素材信息" / "script.md"
        shotlist_path = project_dir / "素材信息" / "shotlist.csv"
        manifest_path = project_dir / "素材信息" / "edit_manifest.json"
        timeline_path = project_dir / "素材信息" / "timeline.json"
        srt_path = project_dir / "素材信息" / "subtitles.srt"
        rough_cut_path = project_dir / "素材信息" / "rough_cut_plan.json"
        assert script_path.is_file()
        assert shotlist_path.is_file()
        assert manifest_path.is_file()
        assert timeline_path.is_file()
        assert srt_path.is_file()
        assert rough_cut_path.is_file()

        script_text = script_path.read_text(encoding="utf-8")
        assert script_text.startswith("# ")
        assert "AI 工具" in script_text
        assert "## 口播稿" in script_text
        assert "## 分镜" in script_text
        assert "## 字幕" in script_text
        assert "## 素材说明" in script_text
        assert "## 数字人说明" in script_text

        with shotlist_path.open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        assert rows
        assert rows[0]["是否需要 Codex 图文"] in {"是", "否"}
        assert rows[0]["是否需要数字人"] == "是"
        assert rows[0]["素材状态"] == "待执行"
        assert rows[1]["素材状态"] == "等待"

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["pipeline"]["provider"] == "fake"
        assert manifest["pipeline"]["real_ai_called"] is False
        assert manifest["pipeline"]["jianying_download_executed"] is False
        assert manifest["pipeline"]["codex_skill_executed"] is False
        assert manifest["shots"]
        assert manifest["graphic_tasks"]
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        assert timeline["timeline"]
        rough_cut = json.loads(rough_cut_path.read_text(encoding="utf-8"))
        assert len(rough_cut["versions"]) == 1
        assert rough_cut["recommended_template"]["name"] == "智能推荐模板"


def test_projects_from_same_hotspot_own_distinct_editable_script_assets(tmp_path):
    with make_session() as session:
        hot_item = add_hot_item(session)
        material_root = prepared_material_root(tmp_path)

        first = build_project_from_hotspot(
            session,
            hot_item.id,
            project_name="shared-topic-project-a",
            material_root=material_root,
        )
        second = build_project_from_hotspot(
            session,
            hot_item.id,
            project_name="shared-topic-project-b",
            material_root=material_root,
        )

        assert first.topic_idea_id == second.topic_idea_id
        assert first.script_asset_id != second.script_asset_id
        assert session.query(ScriptAsset).count() == 2
        assert session.query(AiGeneration).count() == 2

        first_script = session.get(ScriptAsset, first.script_asset_id)
        second_script = session.get(ScriptAsset, second.script_asset_id)
        assert first_script is not None
        assert second_script is not None
        first_voiceover = first_script.voiceover

        second_pipeline = session.get(ProductionPipeline, second.pipeline_id)
        second_pipeline.script_asset_id = first_script.id
        second_script_step = (
            session.query(PipelineStep)
            .filter(PipelineStep.pipeline_id == second.pipeline_id)
            .filter(PipelineStep.name == "生成脚本素材")
            .one()
        )
        second_script_step.output_summary = {
            **dict(second_script_step.output_summary or {}),
            "script_asset_id": first_script.id,
        }
        session.add_all([second_pipeline, second_script_step])
        session.commit()

        updated = update_script_workspace(
            session,
            second.pipeline_id,
            {
                "voiceover": "project-b-only-edit",
                "storyboard": list(first_script.storyboard or []),
                "subtitles": list(first_script.subtitles or []),
                "title_options": list(first_script.title_options or []),
                "cover_copy": list(first_script.cover_copy or []),
                "publish_copy": dict(first_script.publish_copy or {}),
            },
        )

        assert (
            session.get(ScriptAsset, first.script_asset_id).voiceover
            == first_voiceover
        )
        second_pipeline = session.get(ProductionPipeline, second.pipeline_id)
        assert second_pipeline.script_asset_id != first.script_asset_id
        assert second_pipeline.script_asset_id != second.script_asset_id
        assert updated.script["voiceover"] == "project-b-only-edit"
        assert (
            session.get(ScriptAsset, second_pipeline.script_asset_id).voiceover
            == "project-b-only-edit"
        )
        assert (
            second_script_step.output_summary["script_asset_id"]
            == second_pipeline.script_asset_id
        )


def test_same_title_pipelines_use_distinct_project_key_directories_and_rename_is_stable(
    tmp_path,
):
    material_root = tmp_path / "短视频工厂"
    (material_root / "项目").mkdir(parents=True)
    with make_session() as session:
        first_hotspot = add_hot_item(session)
        second_hotspot = HotItem(
            source_platform="手动导入",
            source_type="manual",
            title=first_hotspot.title,
            url="https://example.com/pipeline-ai-buying-second",
            category="AI",
        )
        session.add(second_hotspot)
        session.commit()
        first = build_project_from_hotspot(
            session,
            first_hotspot.id,
            project_name="同名项目",
            material_root=material_root,
            stop_after="创建中文项目目录",
        )
        second = build_project_from_hotspot(
            session,
            second_hotspot.id,
            project_name="同名项目",
            material_root=material_root,
            stop_after="创建中文项目目录",
        )

        assert first.project_key != second.project_key
        assert Path(first.project_dir).name == first.project_key
        assert Path(second.project_dir).name == second.project_key
        assert first.project_dir != second.project_dir

        first_pipeline = session.get(ProductionPipeline, first.pipeline_id)
        original_directory = first.project_dir
        first_pipeline.project_name = "改名后只改变展示标题"
        session.commit()
        forced = continue_pipeline(
            session,
            first.pipeline_id,
            force=True,
            material_root=material_root,
            stop_after="创建中文项目目录",
        )
        assert forced.project_key == first.project_key
        assert forced.project_dir == original_directory


def test_material_tasks_carry_hotspot_source_for_official_capture(tmp_path):
    with make_session() as session:
        hot_item = add_hot_item(session)

        result = build_project_from_hotspot(
            session,
            hot_item.id,
            material_root=prepared_material_root(tmp_path),
        )

        raw_step = (
            session.query(PipelineStep)
            .filter(PipelineStep.pipeline_id == result.pipeline_id)
            .filter(PipelineStep.name == "生成素材任务")
            .one()
        )
        official_tasks = [
            task
            for task in raw_step.output_summary["material_tasks"]
            if task["needs_official_capture"]
        ]

        assert raw_step.output_summary["project_name"] == result.project_name
        assert raw_step.output_summary["project_key"] == result.project_key
        assert official_tasks
        assert all(task["official_source_url"] == hot_item.url for task in official_tasks)
        assert all(task["official_source_name"] == hot_item.source_platform for task in official_tasks)
        assert all(task["official_page_title"] == hot_item.title for task in official_tasks)


def test_pipeline_can_pause_after_script_generation(tmp_path):
    with make_session() as session:
        hot_item = add_hot_item(session)

        result = build_project_from_hotspot(
            session,
            hot_item.id,
            material_root=prepared_material_root(tmp_path),
            stop_after="生成脚本素材",
        )

        statuses = {step.name: step.status for step in result.steps}
        assert result.status == "进行中"
        assert statuses["生成候选选题"] == "已完成"
        assert statuses["选择候选选题"] == "已完成"
        assert statuses["生成脚本素材"] == "已完成"
        assert statuses["生成素材任务"] == "未开始"
        assert statuses["导出 edit_manifest.json"] == "未开始"
        assert result.project_dir == ""


def test_material_planning_failure_is_recorded_and_export_still_runs(tmp_path):
    def fail_material_planner(*args, **kwargs):
        raise RuntimeError("素材任务测试失败")

    with make_session() as session:
        hot_item = add_hot_item(session)
        dependencies = PipelineDependencies(material_planner=fail_material_planner)

        result = build_project_from_hotspot(
            session,
            hot_item.id,
            material_root=prepared_material_root(tmp_path),
            dependencies=dependencies,
        )

        statuses = {step.name: step.status for step in result.steps}
        errors = {step.name: step.error for step in result.steps}
        assert result.status == "失败"
        assert statuses["生成素材任务"] == "失败"
        assert "素材任务测试失败" in errors["生成素材任务"]
        assert statuses["生成 Codex 图文任务"] == "需要人工确认"
        assert statuses["生成数字人字段"] == "需要人工确认"
        assert statuses["创建中文项目目录"] == "已完成"
        assert statuses["生成剪辑清单"] == "已完成"
        assert statuses["导出 edit_manifest.json"] == "已完成"
        assert (Path(result.project_dir) / "素材信息" / "edit_manifest.json").is_file()


def test_material_plan_cas_rejects_result_when_script_changes_during_planning(tmp_path):
    database_path = tmp_path / "D" / "material-plan-cas.sqlite3"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    material_root = prepared_material_root(tmp_path)
    project_root = material_root / "项目"
    session = factory()
    try:
        hot_item = add_hot_item(session)
        built = build_project_from_hotspot(
            session,
            hot_item.id,
            material_root=material_root,
            project_root=project_root,
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        assert material_step.output_summary.get("material_tasks")
        material_step.status = "未开始"
        session.add(material_step)
        session.commit()
        real_planner = PipelineDependencies().material_planner

        def planner_with_concurrent_script_edit(*args, **kwargs):
            from app.pipeline.workspace_service import (
                get_script_workspace,
                update_script_workspace,
            )

            with factory() as concurrent_session:
                workspace = get_script_workspace(concurrent_session, built.pipeline_id)
                update_script_workspace(
                    concurrent_session,
                    built.pipeline_id,
                    {
                        **workspace.script,
                        "voiceover": f"{workspace.script['voiceover']}\n规划期间的新修改。",
                    },
                )
            return real_planner(*args, **kwargs)

        with pytest.raises(PipelineStepConflictError, match="旧规划未提交"):
            continue_pipeline(
                session,
                built.pipeline_id,
                material_root=material_root,
                project_root=project_root,
                dependencies=PipelineDependencies(
                    material_planner=planner_with_concurrent_script_edit
                ),
            )

        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        assert material_step.output_summary["material_selection_state"] == "stale"
        assert "规划期间的新修改" in pipeline.script_asset.voiceover
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (PipelineStepConflictError("素材规划冲突"), 409),
        (PipelineStepCommitUnknownError("素材规划结果待核对"), 503),
    ],
)
def test_continue_route_exposes_material_plan_cas_result(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_status: int,
):
    def override_get_db():
        yield object()

    def fail_continue(*args, **kwargs):
        raise error

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(main_module, "continue_pipeline", fail_continue)
    try:
        response = TestClient(app).post("/api/pipeline/999/continue")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == expected_status
    assert response.json()["detail"] == str(error)


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (PipelineStepConflictError("素材规划冲突"), 409),
        (PipelineStepCommitUnknownError("素材规划结果待核对"), 503),
    ],
)
def test_script_accept_route_exposes_material_plan_cas_result(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_status: int,
):
    def override_get_db():
        yield object()

    def fail_accept(*args, **kwargs):
        raise error

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(main_module, "accept_script_work_order", fail_accept)
    try:
        response = TestClient(app).post("/api/pipeline/999/script-work-order/accept")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == expected_status
    assert response.json()["detail"] == str(error)


class NonFakeProvider(AIProvider):
    name = "real-provider"
    model = "paid-model"

    def generate(self, request: AIRequest) -> AIResponse:
        raise AssertionError("真实 provider 不应被调用")


def test_pipeline_rejects_non_fake_provider_before_any_ai_call(tmp_path):
    with make_session() as session:
        hot_item = add_hot_item(session)

        with pytest.raises(FakeProviderRequiredError, match="只允许 fake provider"):
            build_project_from_hotspot(
                session,
                hot_item.id,
                material_root=prepared_material_root(tmp_path),
                provider=NonFakeProvider(),
            )

        assert session.query(ProductionPipeline).count() == 0


def test_build_project_api_uses_fake_provider_and_returns_exports(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        hot_item_id = add_hot_item(setup_session).id

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    material_root = prepared_material_root(tmp_path)
    monkeypatch.setenv("MATERIAL_ROOT_DIR", str(material_root))
    monkeypatch.setenv("PROJECT_ROOT_DIR", str(material_root / "项目"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(
            f"/api/pipeline/hotspots/{hot_item_id}/build-project",
            json={"project_name": "AI采购逻辑"},
        )
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert response.status_code == 200
    data = response.json()
    assert data["hotspot_id"] == hot_item_id
    assert data["provider"] == "fake"
    assert data["status"] == "需要人工确认"
    assert data["completion_reason_code"] == "approval_missing"
    assert data["current_step"] == "成片验收"
    assert data["next_surface"] == "finished"
    assert "completion_evidence" not in data
    assert Path(data["exports"]["script_md"]).is_file()
    assert Path(data["exports"]["shotlist_csv"]).is_file()
    assert Path(data["exports"]["edit_manifest_json"]).is_file()


def test_manual_v1_acceptance_imports_hotspot_runs_quality_and_exports(tmp_path):
    with make_session() as session:
        result = run_manual_v1_acceptance(
            session,
            ManualV1RunRequest(
                title="AI 搜索正在改变普通人获取信息的方式",
                summary=(
                    "越来越多用户开始用 AI 搜索替代传统搜索引擎，信息获取方式正在变化，"
                    "背后涉及平台流量、广告模式和内容生态变化。"
                ),
                source_platform="手动跑通测试",
                url="",
                category="AI / 科技趋势 / 商业逻辑",
                tags=["AI", "科技趋势", "商业逻辑"],
            ),
            material_root=prepared_material_root(tmp_path),
        )

        assert result.message == (
            "V1 准备包已生成；请到成片页检查文件和验收状态。"
            "五门报告与批准入口仍属于 Phase 1。"
        )
        assert result.completed is False
        assert result.provider == "fake"
        assert result.real_ai_called is False
        assert result.hotspot_id is not None
        assert result.pipeline["status"] == "需要人工确认"
        assert result.pipeline["completion_reason_code"] == "approval_missing"
        assert result.pipeline["current_step"] == "成片验收"
        assert result.quality_check["publish_suggestion"] == "可以发布"
        assert result.repair_attempts == 0
        assert Path(result.exports["script_md"]).is_file()
        assert Path(result.exports["shotlist_csv"]).is_file()
        assert Path(result.exports["edit_manifest_json"]).is_file()
        assert Path(result.exports["timeline_json"]).is_file()
        assert Path(result.exports["subtitles_srt"]).is_file()
        assert result.links["project_pipeline_id"] == result.pipeline_id
        assert session.query(AiGeneration).count() == 2


def test_manual_v1_acceptance_api_uses_fake_provider_and_material_root(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    material_root = prepared_material_root(tmp_path)
    monkeypatch.setenv("MATERIAL_ROOT_DIR", str(material_root))
    monkeypatch.setenv("PROJECT_ROOT_DIR", str(material_root / "项目"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post(
            "/api/pipeline/manual-v1-test",
            json={
                "title": "AI 搜索正在改变普通人获取信息的方式",
                "summary": "AI 搜索替代传统搜索，影响平台流量、广告模式和内容生态。",
                "source_platform": "手动跑通测试",
                "category": "AI / 科技趋势 / 商业逻辑",
                "tags": ["AI", "搜索", "商业逻辑"],
            },
        )
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert response.status_code == 200
    data = response.json()
    assert data["message"] == (
        "V1 准备包已生成；请到成片页检查文件和验收状态。"
        "五门报告与批准入口仍属于 Phase 1。"
    )
    assert data["completed"] is False
    assert data["provider"] == "fake"
    assert data["real_ai_called"] is False
    assert data["quality_check"]["local_rules_only"] is True
    assert Path(data["exports"]["script_md"]).is_file()
    assert str(tmp_path / "短视频工厂") in data["project_dir"]


def test_manual_v1_acceptance_returns_partial_result_when_project_dir_fails(
    tmp_path,
    monkeypatch,
):
    def fail_directories(self):
        raise RuntimeError("目录创建失败")

    monkeypatch.setattr(ProjectMaterialPaths, "ensure_directories", fail_directories)
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
        assert result.exports == {}
        assert result.pipeline["status"] == "失败"
        assert result.pipeline["completion_reason_code"] == "step_failed"
        assert result.pipeline["current_step"] == "创建中文项目目录"
        assert any(
            failure["step_name"] == "创建中文项目目录"
            and "目录创建失败" in failure["reason"]
            for failure in result.pipeline["failure_reasons"]
        )


def test_quality_repair_cleans_banned_terms_and_records_attempts(tmp_path):
    with make_session() as session:
        hot_item = add_hot_item(session)
        built = build_project_from_hotspot(
            session,
            hot_item.id,
            material_root=prepared_material_root(tmp_path),
        )
        script = session.get(ScriptAsset, built.script_asset_id)
        assert script is not None
        original_text = "大家都知道，这个风口炸裂，是财富密码，现在可以买，马上上车。"
        script.voiceover = original_text
        script.storyboard = [
            {
                "start": "00:00",
                "end": "00:05",
                "voiceover": script.voiceover,
                "recommended_visual": "标题卡片",
            }
        ]
        script.subtitles = [{"start": "00:00", "end": "00:05", "text": script.voiceover}]
        session.add(script)
        session.commit()

        result = check_and_repair_pipeline_script(session, built.pipeline_id, max_attempts=2)

        assert result.repair_attempts >= 1
        assert result.quality_check["risk_level"] != "高"
        assert "马上上车" not in result.quality_check["matched_banned_expressions"]
        assert session.get(ScriptAsset, built.script_asset_id).voiceover != original_text


def test_pipeline_status_query_returns_steps_files_failures_and_next_suggestion(tmp_path):
    with make_session() as session:
        hot_item = add_hot_item(session)
        built = build_project_from_hotspot(
            session,
            hot_item.id,
            material_root=prepared_material_root(tmp_path),
            stop_after="生成脚本素材",
        )

        status = get_pipeline_status(session, built.pipeline_id)

        assert status.pipeline_id == built.pipeline_id
        assert status.hotspot_id == hot_item.id
        assert status.topic_idea_id is not None
        assert status.script_asset_id is not None
        assert status.status == "进行中"
        assert len(status.steps) == len(PIPELINE_STEP_NAMES)
        assert status.generated_files == {}
        assert status.failure_reasons == []
        assert status.needs_confirmation is False
        assert status.completion_reason_code == "preparation_incomplete"
        assert status.current_step == "生成素材任务"
        assert status.next_surface == "project"
        assert status.next_suggestion == "打开项目页继续生产"
        script_step = next(step for step in status.steps if step.name == "生成脚本素材")
        assert script_step.started_at is not None
        assert script_step.finished_at is not None
        assert script_step.output_id == status.script_asset_id


def test_continue_pipeline_resumes_from_next_step_without_repeating_ai(tmp_path):
    with make_session() as session:
        hot_item = add_hot_item(session)
        built = build_project_from_hotspot(
            session,
            hot_item.id,
            material_root=prepared_material_root(tmp_path),
            stop_after="生成脚本素材",
        )
        generation_count = session.query(AiGeneration).count()
        script_count = session.query(ScriptAsset).count()
        completed_timestamps = {
            step.name: step.finished_at
            for step in get_pipeline_status(session, built.pipeline_id).steps
            if step.status == "已完成"
        }

        continued = continue_pipeline(
            session,
            built.pipeline_id,
            material_root=prepared_material_root(tmp_path),
        )

        assert continued.status == "需要人工确认"
        assert continued.completion_reason_code == "approval_missing"
        assert session.query(AiGeneration).count() == generation_count
        assert session.query(ScriptAsset).count() == script_count
        timestamps_after = {
            step.name: step.finished_at
            for step in get_pipeline_status(session, built.pipeline_id).steps
            if step.name in completed_timestamps
        }
        assert timestamps_after == completed_timestamps
        assert Path(continued.exports["edit_manifest_json"]).is_file()


def test_failed_step_can_be_retried_from_pipeline_id(tmp_path):
    def fail_material_planner(*args, **kwargs):
        raise RuntimeError("素材任务暂时失败")

    with make_session() as session:
        hot_item = add_hot_item(session)
        failed = build_project_from_hotspot(
            session,
            hot_item.id,
            material_root=prepared_material_root(tmp_path),
            dependencies=PipelineDependencies(material_planner=fail_material_planner),
        )
        failed_status = get_pipeline_status(session, failed.pipeline_id)
        failed_step = next(
            step for step in failed_status.steps if step.name == "生成素材任务"
        )
        assert failed_step.status == "失败"
        assert "素材任务暂时失败" in failed_step.error_message

        retried = continue_pipeline(
            session,
            failed.pipeline_id,
            material_root=prepared_material_root(tmp_path),
        )

        assert retried.status == "需要人工确认"
        assert retried.completion_reason_code == "approval_missing"
        retried_status = get_pipeline_status(session, failed.pipeline_id)
        assert all(step.status == "已完成" for step in retried_status.steps)
        assert retried_status.failure_reasons == []
        assert retried_status.needs_confirmation is True


def test_continue_completed_pipeline_does_not_repeat_completed_steps(tmp_path):
    with make_session() as session:
        hot_item = add_hot_item(session)
        built = build_project_from_hotspot(
            session,
            hot_item.id,
            material_root=prepared_material_root(tmp_path),
        )
        before = get_pipeline_status(session, built.pipeline_id)
        generation_count = session.query(AiGeneration).count()
        step_times = {step.name: step.finished_at for step in before.steps}

        continued = continue_pipeline(
            session,
            built.pipeline_id,
            material_root=prepared_material_root(tmp_path),
        )

        assert continued.status == "需要人工确认"
        assert session.query(AiGeneration).count() == generation_count
        after = get_pipeline_status(session, built.pipeline_id)
        assert {step.name: step.finished_at for step in after.steps} == step_times


def test_force_continue_reexecutes_steps_but_keeps_fake_ai_cache(tmp_path):
    with make_session() as session:
        hot_item = add_hot_item(session)
        built = build_project_from_hotspot(
            session,
            hot_item.id,
            material_root=prepared_material_root(tmp_path),
        )
        before = get_pipeline_status(session, built.pipeline_id)
        generation_count = session.query(AiGeneration).count()
        first_finished_at = before.steps[0].finished_at

        forced = continue_pipeline(
            session,
            built.pipeline_id,
            force=True,
            material_root=prepared_material_root(tmp_path),
        )

        assert forced.status == "需要人工确认"
        assert session.query(AiGeneration).count() == generation_count
        after = get_pipeline_status(session, built.pipeline_id)
        assert after.steps[0].finished_at is not None
        assert after.steps[0].finished_at >= first_finished_at


def test_missing_pipeline_returns_clear_error():
    with make_session() as session:
        with pytest.raises(PipelineNotFoundError, match="流程不存在"):
            get_pipeline_status(session, 999)
        with pytest.raises(PipelineNotFoundError, match="流程不存在"):
            continue_pipeline(session, 999)


def test_pipeline_status_and_continue_api(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    with testing_session_local() as setup_session:
        hot_item = add_hot_item(setup_session)
        built = build_project_from_hotspot(
            setup_session,
            hot_item.id,
            material_root=prepared_material_root(tmp_path),
            stop_after="生成脚本素材",
        )
        pipeline_id = built.pipeline_id

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    material_root = tmp_path / "短视频工厂"
    monkeypatch.setenv("MATERIAL_ROOT_DIR", str(material_root))
    monkeypatch.setenv("PROJECT_ROOT_DIR", str(material_root / "项目"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        status_response = client.get(f"/api/pipeline/{pipeline_id}")
        continue_response = client.post(f"/api/pipeline/{pipeline_id}/continue")
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert status_response.status_code == 200
    assert status_response.json()["needs_confirmation"] is False
    assert continue_response.status_code == 200
    assert continue_response.json()["status"] == "需要人工确认"
    assert continue_response.json()["completion_reason_code"] == "approval_missing"


def test_pipeline_detail_rederives_failed_status_without_mutating_stored_status(tmp_path):
    with make_session() as session:
        hot_item = add_hot_item(session)
        built = build_project_from_hotspot(
            session,
            hot_item.id,
            material_root=prepared_material_root(tmp_path),
            stop_after="生成脚本素材",
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        pipeline.status = "已完成"
        failed_step = next(step for step in pipeline.steps if step.name == "生成脚本素材")
        failed_step.status = "失败"
        failed_step.error = "脚本损坏"
        session.commit()

        result = get_pipeline_status(session, pipeline.id)

        assert result.status == "失败"
        assert result.completion_reason_code == "step_failed"
        assert result.current_step == "生成脚本素材"
        assert result.next_surface == "project"
        assert result.needs_confirmation is True
        session.expire_all()
        assert session.get(ProductionPipeline, pipeline.id).status == "已完成"


def test_pipeline_detail_marks_missing_step_invalid_without_repairing_it(tmp_path):
    with make_session() as session:
        hot_item = add_hot_item(session)
        built = build_project_from_hotspot(
            session,
            hot_item.id,
            material_root=prepared_material_root(tmp_path),
            stop_after="生成脚本素材",
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        session.delete(pipeline.steps[-1])
        session.commit()
        before_count = session.query(PipelineStep).filter_by(pipeline_id=pipeline.id).count()

        result = get_pipeline_status(session, pipeline.id)

        assert result.status == "需要人工确认"
        assert result.completion_reason_code == "preparation_invalid"
        assert result.current_step == "项目检查"
        assert result.next_surface == "project"
        assert session.query(PipelineStep).filter_by(pipeline_id=pipeline.id).count() == before_count


def test_finalize_pipeline_evaluates_once_and_persists_decision_status(tmp_path, monkeypatch):
    with make_session() as session:
        hot_item = add_hot_item(session)
        built = build_project_from_hotspot(
            session,
            hot_item.id,
            material_root=prepared_material_root(tmp_path),
            stop_after="生成脚本素材",
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        calls = []

        class Decision:
            status = "失败"

        def fake_evaluate(candidate):
            calls.append(candidate.id)
            return Decision()

        monkeypatch.setattr(pipeline_service, "evaluate_pipeline_completion", fake_evaluate)

        decision = pipeline_service._finalize_pipeline(session, pipeline)

        assert calls == [pipeline.id]
        assert decision.status == "失败"
        session.expire_all()
        assert session.get(ProductionPipeline, pipeline.id).status == "失败"
