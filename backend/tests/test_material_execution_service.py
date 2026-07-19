from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.pipeline.material_execution_service import (
    AutoMaterialTaskExecutor,
    MaterialExecutionResult,
    MaterialTaskConflictError,
    reconcile_material_task,
    run_next_material_task,
    skip_material_task,
)
from app.pipeline.material_package_service import (
    get_material_package,
    lock_material_candidate,
    save_material_candidate_pool,
)
from app.materials.asset_paths import AutoMaterialPaths
from app.materials.official_capture import OfficialCaptureResult
from app.materials.paths import ProjectMaterialPaths
from app.materials.pexels import PexelsDownloadItem, PexelsSampleDownloadResult
from app.materials.source_clients import MaterialSourceItem
from app.pipeline.service import build_project_from_hotspot
from app.models import PipelineStep, ProductionPipeline
from app.pipeline.project_identity import ProjectIdentityError
from app.main import app, skip_pipeline_material_task
from tests.test_production_pipeline import (
    add_hot_item,
    make_session,
    prepared_material_root,
)


@dataclass(slots=True)
class _SuccessfulExecutor:
    output_path: Path

    def execute(self, task: dict, *, project_name: str) -> MaterialExecutionResult:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text("fake material", encoding="utf-8")
        return MaterialExecutionResult(
            success=True,
            status="已完成",
            material_path=str(self.output_path),
            material_type="Pexels视频",
            source_type="Pexels",
            evidence_kind="atmosphere",
            source_url="https://videos.pexels.com/example.mp4",
            is_locally_generated=False,
            reason="按关键词匹配成功。",
        )


class _FailingExecutor:
    def execute(self, task: dict, *, project_name: str) -> MaterialExecutionResult:
        return MaterialExecutionResult(
            success=False,
            status="失败",
            source_url="https://example.com/attempted-source",
            reason="Pexels 没有找到合适的视频素材。",
        )


class _ExplodingExecutor:
    def execute(self, task: dict, *, project_name: str) -> MaterialExecutionResult:
        raise RuntimeError("execution exploded")


@dataclass(slots=True)
class _SpoofLabelExecutor:
    output_path: Path

    def execute(self, task: dict, *, project_name: str) -> MaterialExecutionResult:
        self.output_path.write_bytes(b"local explanation")
        return MaterialExecutionResult(
            success=True,
            status="已完成",
            material_path=str(self.output_path),
            material_type="官方证据画面",
            source_type="伪标签",
            evidence_kind="explanation",
            source_url="",
            is_locally_generated=True,
            reason="本地解释卡。",
        )


def _build_pipeline(session: Session, tmp_path: Path) -> int:
    hot_item = add_hot_item(session)
    result = build_project_from_hotspot(
        session,
        hot_item.id,
        material_root=prepared_material_root(tmp_path),
    )
    return result.pipeline_id


def test_run_next_material_task_marks_success_and_advances_waiting_task(tmp_path):
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)
        result = run_next_material_task(
            session,
            pipeline_id,
            executor=_SuccessfulExecutor(tmp_path / "素材" / "视频.mp4"),
        )

        assert result["executed_task"]["status"] == "已完成"
        assert result["executed_task"]["material_path"].endswith("视频.mp4")
        assert result["executed_task"]["source_type"] == "Pexels"
        assert result["executed_task"]["evidence_kind"] == "atmosphere"
        assert result["executed_task"]["source_url"] == "https://videos.pexels.com/example.mp4"
        assert result["executed_task"]["is_locally_generated"] is False
        assert result["tasks"][0]["status"] == "已完成"
        assert result["tasks"][1]["status"] == "待执行"
        assert all(task["status"] == "等待" for task in result["tasks"][2:])


def test_run_next_material_task_records_failure_reason_and_advances_queue(tmp_path):
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)
        result = run_next_material_task(
            session,
            pipeline_id,
            executor=_FailingExecutor(),
        )

        assert result["executed_task"]["status"] == "失败"
        assert "Pexels 没有找到合适的视频素材" in result["executed_task"]["reject_reason"]
        assert result["tasks"][0]["status"] == "失败"
        assert result["executed_task"]["evidence_kind"] == ""
        assert result["executed_task"]["source_url"] == "https://example.com/attempted-source"
        assert result["executed_task"]["is_locally_generated"] is False
        assert result["tasks"][1]["status"] == "待执行"


def test_reconcile_material_task_requeues_failed_shot_before_retry(tmp_path):
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)
        failed = run_next_material_task(
            session,
            pipeline_id,
            executor=_FailingExecutor(),
        )
        assert failed["executed_task"]["status"] == "失败"

        retried = reconcile_material_task(
            session,
            pipeline_id,
            "shot-01",
            executor=_SuccessfulExecutor(tmp_path / "素材" / "reconciled.mp4"),
        )

        assert retried["executed_task"]["shot_id"] == "shot-01"
        assert retried["executed_task"]["status"] == "已完成"
        assert retried["executed_task"]["material_path"].endswith("reconciled.mp4")
        assert "失败" in retried["executed_task"]["status_history"]
        assert "待执行" in retried["executed_task"]["status_history"]


def test_reconcile_material_task_retries_the_requested_shot_not_an_earlier_waiting_shot(
    tmp_path,
):
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)
        step = (
            session.query(PipelineStep)
            .filter(
                PipelineStep.pipeline_id == pipeline_id,
                PipelineStep.name == "生成素材任务",
            )
            .one()
        )
        payload = deepcopy(step.output_summary)
        tasks = payload["material_tasks"]
        tasks[0]["status"] = "待执行"
        tasks[0]["material_path"] = ""
        tasks[1]["status"] = "失败"
        tasks[1]["material_path"] = ""
        tasks[1]["reject_reason"] = "上次获取失败"
        step.output_summary = payload
        session.add(step)
        session.commit()

        retried = reconcile_material_task(
            session,
            pipeline_id,
            tasks[1]["shot_id"],
            executor=_SuccessfulExecutor(tmp_path / "素材" / "requested-shot.mp4"),
        )

        assert retried["executed_task"]["shot_id"] == tasks[1]["shot_id"]
        assert retried["executed_task"]["status"] == "已完成"
        assert retried["tasks"][0]["status"] == "待执行"
        assert retried["tasks"][0]["material_path"] == ""


def test_reconcile_material_task_rejects_a_locked_successful_shot(tmp_path):
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)
        run_next_material_task(
            session,
            pipeline_id,
            executor=_SuccessfulExecutor(tmp_path / "素材" / "locked.mp4"),
        )

        with pytest.raises(ValueError, match="已完成|锁定"):
            reconcile_material_task(
                session,
                pipeline_id,
                "shot-01",
                executor=_SuccessfulExecutor(tmp_path / "素材" / "should-not-run.mp4"),
            )


def test_material_recovery_route_is_registered_for_the_visible_material_page():
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/reconcile-and-retry" in paths


def test_run_next_material_task_restores_original_payload_on_execution_exception(tmp_path):
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)
        step = (
            session.query(PipelineStep)
            .filter(PipelineStep.pipeline_id == pipeline_id)
            .filter(PipelineStep.name == "生成素材任务")
            .one()
        )
        before = deepcopy(step.output_summary)

        with pytest.raises(RuntimeError, match="execution exploded"):
            run_next_material_task(
                session,
                pipeline_id,
                executor=_ExplodingExecutor(),
            )

        session.refresh(step)
        assert step.output_summary == before


def test_spoofed_official_label_cannot_override_persisted_provenance(tmp_path):
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)

        result = run_next_material_task(
            session,
            pipeline_id,
            executor=_SpoofLabelExecutor(tmp_path / "spoof.mp4"),
        )

        task = result["executed_task"]
        assert task["material_type"] == "官方证据画面"
        assert task["evidence_kind"] == "explanation"
        assert task["source_url"] == ""
        assert task["is_locally_generated"] is True


def test_run_next_material_task_resumes_stuck_official_capture_status(tmp_path):
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)
        step = (
            session.query(PipelineStep)
            .filter(PipelineStep.pipeline_id == pipeline_id)
            .filter(PipelineStep.name == "生成素材任务")
            .one()
        )
        payload = deepcopy(step.output_summary)
        payload["material_tasks"][0]["status"] = "官方页面录屏中"
        payload["material_tasks"][0]["notes"] = "上次官方页面录屏请求中断。"
        payload["material_tasks"][0]["status_history"] = ["进行中", "官方页面录屏中"]
        step.output_summary = payload
        session.add(step)
        session.commit()

        result = run_next_material_task(
            session,
            pipeline_id,
            executor=_SuccessfulExecutor(tmp_path / "素材" / "恢复后素材.mp4"),
        )

        assert result["executed_task"]["shot_id"] == "shot-01"
        assert result["executed_task"]["status"] == "已完成"
        assert result["executed_task"]["material_path"].endswith("恢复后素材.mp4")
        assert result["tasks"][0]["status"] == "已完成"
        assert result["tasks"][1]["status"] == "待执行"


def test_run_next_material_task_resumes_stuck_pexels_status(tmp_path):
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)
        step = (
            session.query(PipelineStep)
            .filter(PipelineStep.pipeline_id == pipeline_id)
            .filter(PipelineStep.name == "生成素材任务")
            .one()
        )
        payload = deepcopy(step.output_summary)
        payload["material_tasks"][0]["status"] = "Pexels下载中"
        payload["material_tasks"][0]["notes"] = "上次 Pexels 下载请求中断。"
        payload["material_tasks"][0]["status_history"] = ["进行中", "Pexels下载中"]
        step.output_summary = payload
        session.add(step)
        session.commit()

        result = run_next_material_task(
            session,
            pipeline_id,
            executor=_SuccessfulExecutor(tmp_path / "素材" / "pexels恢复.mp4"),
        )

        assert result["executed_task"]["shot_id"] == "shot-01"
        assert result["executed_task"]["status"] == "已完成"
        assert result["executed_task"]["material_path"].endswith("pexels恢复.mp4")
        assert result["tasks"][0]["status"] == "已完成"
        assert result["tasks"][1]["status"] == "待执行"


class _TrackingAutoExecutor(AutoMaterialTaskExecutor):
    def __init__(self, tmp_path: Path, *, coverr_enabled: bool = False):
        super().__init__(
            paths=AutoMaterialPaths(root=tmp_path / "自动素材"),
            coverr_enabled=coverr_enabled,
        )
        self.calls: list[str] = []
        self.output_path = tmp_path / "素材" / "信息动效.mp4"

    def _try_official_capture(self, task: dict) -> MaterialExecutionResult:
        self.calls.append("官方页面录屏")
        return MaterialExecutionResult(
            success=False,
            status="失败",
            material_type="官方页面录屏",
            source_type="官方页面",
            reason="官方页面录屏超时，已自动跳过。",
        )

    def _try_pexels(self, task: dict) -> MaterialExecutionResult:
        self.calls.append("Pexels")
        return MaterialExecutionResult(
            success=False,
            status="失败",
            material_type="Pexels",
            source_type="Pexels",
            reason="Pexels 3 分钟内没有下载到合格素材。",
        )

    def _try_pixabay(self, task: dict) -> MaterialExecutionResult:
        self.calls.append("Pixabay")
        return MaterialExecutionResult(
            success=False,
            status="失败",
            material_type="Pixabay",
            source_type="Pixabay",
            reason="Pixabay 没有找到合适素材。",
        )

    def _try_coverr(self, task: dict) -> MaterialExecutionResult:
        self.calls.append("Coverr")
        return MaterialExecutionResult(
            success=False,
            status="失败",
            material_type="Coverr",
            source_type="Coverr",
            reason="Coverr 暂时不可用。",
        )

    def _try_info_motion(
        self,
        task: dict,
        *,
        project_name: str,
    ) -> MaterialExecutionResult:
        self.calls.append("信息动效")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text("fake motion", encoding="utf-8")
        return MaterialExecutionResult(
            success=True,
            status="已完成",
            material_path=str(self.output_path),
            material_type="信息动效",
            source_type="自动生成",
            evidence_kind="explanation",
            source_url="",
            is_locally_generated=True,
            reason="已使用信息动效兜底。",
        )


def test_auto_executor_uses_info_motion_only_after_primary_sources_fail(tmp_path):
    executor = _TrackingAutoExecutor(tmp_path)
    result = executor.execute(
        {
            "shot_id": "shot-01",
            "needs_official_capture": True,
            "needs_pexels": True,
            "needs_info_motion": True,
            "official_source_url": "https://example.com/news",
            "recommended_visual": "官方页面慢速滚动",
        },
        project_name="测试项目",
    )

    assert result.success is True
    assert result.source_type == "自动生成"
    assert result.material_type == "信息动效"
    assert executor.calls == ["官方页面录屏", "Pexels", "Pixabay", "信息动效"]
    assert "官方页面录屏超时" in result.reason
    assert "Pexels 3 分钟内没有下载到合格素材" in result.reason


def test_auto_executor_uses_coverr_only_when_explicitly_enabled(tmp_path):
    executor = _TrackingAutoExecutor(tmp_path, coverr_enabled=True)

    result = executor.execute(
        {
            "shot_id": "shot-01",
            "needs_official_capture": True,
            "needs_pexels": True,
            "needs_info_motion": True,
            "official_source_url": "https://example.com/news",
            "recommended_visual": "官方页面慢速滚动",
        },
        project_name="测试项目",
    )

    assert result.success is True
    assert executor.calls == [
        "官方页面录屏",
        "Pexels",
        "Pixabay",
        "Coverr",
        "信息动效",
    ]


def test_info_motion_intermediates_use_stable_project_key_not_shared_title(
    tmp_path,
    monkeypatch,
):
    def fake_frames(_frame_dir, _project_name, _title, *, prefix):
        assert prefix == "motion"

    def fake_video(_pattern, output_path, *, duration_seconds):
        assert duration_seconds == 6
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake")

    monkeypatch.setattr(
        "app.pipeline.material_execution_service._write_motion_frames",
        fake_frames,
    )
    monkeypatch.setattr(
        "app.pipeline.material_execution_service._frames_to_video",
        fake_video,
    )
    auto_paths = AutoMaterialPaths(root=tmp_path / "自动素材")
    first = AutoMaterialTaskExecutor(
        paths=auto_paths,
        project_paths=ProjectMaterialPaths(
            root=tmp_path / "短视频工厂",
            project_name="同名项目",
            project_key="p-1-same-title",
            project_root_dir=tmp_path / "项目",
        ),
    )
    second = AutoMaterialTaskExecutor(
        paths=auto_paths,
        project_paths=ProjectMaterialPaths(
            root=tmp_path / "短视频工厂",
            project_name="同名项目",
            project_key="p-2-same-title",
            project_root_dir=tmp_path / "项目",
        ),
    )

    first_result = first._try_info_motion(
        {"shot_id": "shot-01", "recommended_visual": "同一画面"},
        project_name="同名项目",
    )
    second_result = second._try_info_motion(
        {"shot_id": "shot-01", "recommended_visual": "同一画面"},
        project_name="同名项目",
    )

    assert first_result.material_path != second_result.material_path
    assert "p-1-same-title" in Path(first_result.material_path).name
    assert "p-2-same-title" in Path(second_result.material_path).name
    assert first_result.evidence_kind == "explanation"
    assert first_result.source_url == ""
    assert first_result.is_locally_generated is True


@pytest.mark.parametrize(
    "source_url",
    [
        "file:///D:/Temp/explanation.html",
        "ftp://example.com/news",
        "D:/Temp/explanation.html",
    ],
)
def test_official_capture_rejects_non_http_sources_before_capture_or_files(
    tmp_path,
    monkeypatch,
    source_url,
):
    auto_root = tmp_path / "自动素材"

    class CaptureMustNotStart:
        def __init__(self, **_kwargs):
            raise AssertionError("unsupported source reached official capture")

    monkeypatch.setattr(
        "app.pipeline.material_execution_service.OfficialPageCaptureService",
        CaptureMustNotStart,
    )
    executor = AutoMaterialTaskExecutor(paths=AutoMaterialPaths(root=auto_root))

    result = executor._try_official_capture(
        {
            "official_source_url": source_url,
            "official_source_name": "Unsupported",
        }
    )

    assert result.success is False
    assert result.evidence_kind == ""
    assert result.source_url == source_url
    assert result.is_locally_generated is False
    assert "http" in result.reason.lower()
    assert not auto_root.exists()


def test_real_https_capture_is_direct_evidence_and_preserves_url(
    tmp_path,
    monkeypatch,
):
    source_url = "https://example.com/news/ai-search"
    clip = tmp_path / "capture.mp4"
    clip.write_bytes(b"captured page")

    class SuccessfulCapture:
        def __init__(self, **_kwargs):
            pass

        def capture(self, _plan):
            return OfficialCaptureResult(
                success=True,
                scroll_recording_path=str(clip),
                reason="来源页面已抓取。",
                files=[str(clip)],
            )

    monkeypatch.setattr(
        "app.pipeline.material_execution_service.OfficialPageCaptureService",
        SuccessfulCapture,
    )
    executor = AutoMaterialTaskExecutor(
        paths=AutoMaterialPaths(root=tmp_path / "自动素材")
    )

    result = executor._try_official_capture(
        {
            "official_source_url": source_url,
            "official_source_name": "Example News",
        }
    )

    assert result.success is True
    assert result.evidence_kind == "direct_evidence"
    assert result.source_url == source_url
    assert result.is_locally_generated is False
    assert "官方证据" not in result.material_type


def test_https_placeholder_capture_is_local_explanation_not_direct_evidence(
    tmp_path,
    monkeypatch,
):
    source_url = "https://example.com/news/ai-search"
    clip = tmp_path / "placeholder.mp4"
    clip.write_bytes(b"placeholder")

    class PlaceholderCapture:
        def __init__(self, **_kwargs):
            pass

        def capture(self, _plan):
            return SimpleNamespace(
                success=True,
                scroll_recording_path=str(clip),
                first_screen_screenshot_path="",
                full_page_screenshot_path="",
                reason="页面抓取失败，已生成本地解释卡。",
                files=[str(clip)],
                used_placeholder=True,
            )

    monkeypatch.setattr(
        "app.pipeline.material_execution_service.OfficialPageCaptureService",
        PlaceholderCapture,
    )
    executor = AutoMaterialTaskExecutor(
        paths=AutoMaterialPaths(root=tmp_path / "自动素材")
    )

    result = executor._try_official_capture(
        {"official_source_url": source_url, "official_source_name": "Example News"}
    )

    assert result.success is True
    assert result.evidence_kind == "explanation"
    assert result.source_url == source_url
    assert result.is_locally_generated is True
    assert "官方证据" not in result.material_type


def test_stock_source_results_are_atmosphere_and_preserve_available_urls(
    tmp_path,
    monkeypatch,
):
    pexels_url = "https://videos.pexels.com/example.mp4"
    pixabay_url = "https://pixabay.com/videos/example-1/"
    coverr_url = "https://coverr.co/videos/example"

    class PexelsService:
        def __init__(self, **_kwargs):
            pass

        def download_one_video_and_one_photo(self, _keyword, *, per_page):
            assert per_page > 0
            return PexelsSampleDownloadResult(
                keyword="technology",
                success=False,
                video=PexelsDownloadItem(
                    asset_type="video",
                    status="已下载",
                    save_path=str(tmp_path / "pexels.mp4"),
                    source_url=pexels_url,
                ),
                photo=PexelsDownloadItem(asset_type="photo", status="失败"),
                generated_at=datetime.now(),
            )

    class PixabayService:
        def __init__(self, **_kwargs):
            pass

        def download_best(self, _keyword, *, per_page):
            assert per_page > 0
            return MaterialSourceItem(
                source_name="Pixabay",
                asset_type="video",
                status="已下载",
                save_path=str(tmp_path / "pixabay.mp4"),
                source_url=pixabay_url,
            )

    class CoverrService:
        def __init__(self, **_kwargs):
            pass

        def download_best(self, _keyword, *, per_page):
            assert per_page > 0
            return MaterialSourceItem(
                source_name="Coverr",
                asset_type="video",
                status="已下载",
                save_path=str(tmp_path / "coverr.mp4"),
                source_url=coverr_url,
            )

    monkeypatch.setattr(
        "app.pipeline.material_execution_service.PexelsDownloadService",
        PexelsService,
    )
    monkeypatch.setattr(
        "app.pipeline.material_execution_service.PixabayDownloadService",
        PixabayService,
    )
    monkeypatch.setattr(
        "app.pipeline.material_execution_service.CoverrDownloadService",
        CoverrService,
    )
    executor = AutoMaterialTaskExecutor(
        paths=AutoMaterialPaths(root=tmp_path / "自动素材")
    )
    task = {"asset_keywords": ["technology"]}

    results = [
        executor._try_pexels(task),
        executor._try_pixabay(task),
        executor._try_coverr(task),
    ]

    assert [result.evidence_kind for result in results] == [
        "atmosphere",
        "atmosphere",
        "atmosphere",
    ]
    assert [result.source_url for result in results] == [
        pexels_url,
        pixabay_url,
        coverr_url,
    ]
    assert all(result.is_locally_generated is False for result in results)


def test_successful_execution_result_requires_a_success_evidence_kind(tmp_path):
    with pytest.raises(ValueError, match="evidence_kind"):
        MaterialExecutionResult(
            success=True,
            status="已完成",
            material_path=str(tmp_path / "material.mp4"),
            evidence_kind="",
        )


class _TimeoutThenMotionExecutor(AutoMaterialTaskExecutor):
    def __init__(self, tmp_path: Path):
        super().__init__(
            paths=AutoMaterialPaths(root=tmp_path / "自动素材"),
            primary_timeout_seconds=0.01,
        )
        self.output_path = tmp_path / "素材" / "兜底动效.mp4"

    def _try_official_capture(self, task: dict) -> MaterialExecutionResult:
        time.sleep(0.2)
        return MaterialExecutionResult(success=False, status="失败", reason="不应等到这里")

    def _try_pexels(self, task: dict) -> MaterialExecutionResult:
        time.sleep(0.2)
        return MaterialExecutionResult(success=False, status="失败", reason="不应等到这里")

    def _try_pixabay(self, task: dict) -> MaterialExecutionResult:
        return MaterialExecutionResult(success=False, status="失败", reason="Pixabay 跳过")

    def _try_coverr(self, task: dict) -> MaterialExecutionResult:
        return MaterialExecutionResult(success=False, status="失败", reason="Coverr 跳过")

    def _try_info_motion(
        self,
        task: dict,
        *,
        project_name: str,
    ) -> MaterialExecutionResult:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text("fake motion", encoding="utf-8")
        return MaterialExecutionResult(
            success=True,
            status="已完成",
            material_path=str(self.output_path),
            material_type="信息动效",
            source_type="自动生成",
            evidence_kind="explanation",
            source_url="",
            is_locally_generated=True,
            reason="已使用信息动效兜底。",
        )


def test_run_next_material_task_records_source_attempts_before_fallback(tmp_path):
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)
        step = (
            session.query(PipelineStep)
            .filter(PipelineStep.pipeline_id == pipeline_id)
            .filter(PipelineStep.name == "生成素材任务")
            .one()
        )
        payload = deepcopy(step.output_summary)
        payload["material_tasks"][0]["needs_official_capture"] = True
        payload["material_tasks"][0]["needs_pexels"] = True
        payload["material_tasks"][0]["needs_info_motion"] = True
        payload["material_tasks"][0]["official_source_url"] = "https://example.com/news"
        step.output_summary = payload
        session.add(step)
        session.commit()

        result = run_next_material_task(
            session,
            pipeline_id,
            executor=_TimeoutThenMotionExecutor(tmp_path),
            material_root=prepared_material_root(tmp_path),
            project_root=prepared_material_root(tmp_path) / "项目",
        )

        task = result["executed_task"]
        assert task["status"] == "已完成"
        assert task["source_type"] == "自动生成"
        assert task["status_history"] == [
            "待执行",
            "进行中",
            "官方页面录屏中",
            "官方页面录屏失败",
            "Pexels下载中",
            "Pexels下载失败",
            "Pixabay下载中",
            "Pixabay下载失败",
            "信息动效生成中",
            "已完成",
        ]
        assert "官方页面录屏超过 3 分钟仍未完成" in task["notes"]
        assert "Pexels 下载超过 3 分钟仍未完成" in task["notes"]


def test_auto_material_execution_rejects_spoofed_project_dir_before_state_change(
    tmp_path,
):
    material_root = prepared_material_root(tmp_path)
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        step = next(
            item for item in pipeline.steps if item.name == "生成素材任务"
        )
        before = deepcopy(step.output_summary)
        hostile = tmp_path / "hostile-material-directory"
        hostile.mkdir()
        pipeline.project_dir = str(hostile)
        session.commit()

        with pytest.raises(ProjectIdentityError, match="project_dir|canonical"):
            run_next_material_task(
                session,
                pipeline_id,
                executor=_TimeoutThenMotionExecutor(tmp_path),
                material_root=material_root,
                project_root=material_root / "项目",
            )
        session.refresh(step)
        assert step.output_summary == before
        assert not list(hostile.iterdir())


def test_auto_material_execution_rejects_reparse_child_before_state_change(
    tmp_path,
    monkeypatch,
):
    material_root = prepared_material_root(tmp_path)
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        step = next(item for item in pipeline.steps if item.name == "生成素材任务")
        before = deepcopy(step.output_summary)
        pexels_dir = Path(pipeline.project_dir) / "Pexels素材"
        identity = __import__("app.pipeline.project_identity", fromlist=["_is_reparse"])
        original_probe = identity._is_reparse

        def simulated_reparse(path: Path) -> bool:
            return Path(path) == pexels_dir or original_probe(path)

        monkeypatch.setattr(identity, "_is_reparse", simulated_reparse)

        with pytest.raises(ProjectIdentityError, match="reparse|junction|child"):
            run_next_material_task(
                session,
                pipeline_id,
                executor=_TimeoutThenMotionExecutor(tmp_path),
                material_root=material_root,
                project_root=material_root / "项目",
            )

        session.refresh(step)
        assert step.output_summary == before


class _PixabayFallbackExecutor(AutoMaterialTaskExecutor):
    def __init__(self, tmp_path: Path):
        super().__init__(paths=AutoMaterialPaths(root=tmp_path / "自动素材"))
        self.calls: list[str] = []
        self.output_path = tmp_path / "素材" / "pixabay.mp4"

    def _try_pexels(self, task: dict) -> MaterialExecutionResult:
        self.calls.append("Pexels")
        return MaterialExecutionResult(
            success=False,
            status="失败",
            material_type="Pexels",
            source_type="Pexels",
            reason="Pexels 没有找到合适素材。",
        )

    def _try_pixabay(self, task: dict) -> MaterialExecutionResult:
        self.calls.append("Pixabay")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text("fake pixabay", encoding="utf-8")
        return MaterialExecutionResult(
            success=True,
            status="已完成",
            material_path=str(self.output_path),
            material_type="Pixabay视频",
            source_type="Pixabay",
            evidence_kind="atmosphere",
            source_url="https://pixabay.com/videos/example-1/",
            is_locally_generated=False,
            reason="Pixabay 下载成功。",
        )

    def _try_coverr(self, task: dict) -> MaterialExecutionResult:
        self.calls.append("Coverr")
        return MaterialExecutionResult(success=False, status="失败", reason="不应该调用 Coverr")


def test_auto_executor_uses_pixabay_after_pexels_fails(tmp_path):
    executor = _PixabayFallbackExecutor(tmp_path)

    result = executor.execute(
        {
            "shot_id": "shot-01",
            "needs_pexels": True,
            "needs_info_motion": True,
            "recommended_visual": "科技办公背景",
        },
        project_name="测试项目",
    )

    assert result.success is True
    assert result.source_type == "Pixabay"
    assert result.material_type == "Pixabay视频"
    assert executor.calls == ["Pexels", "Pixabay"]


class _SuccessfulOfficialExecutor(AutoMaterialTaskExecutor):
    def __init__(self, tmp_path: Path):
        super().__init__(paths=AutoMaterialPaths(root=tmp_path / "自动素材"))
        self.calls: list[str] = []
        self.output_path = tmp_path / "素材" / "官方页面.mp4"

    def _try_official_capture(self, task: dict) -> MaterialExecutionResult:
        self.calls.append("官方页面录屏")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text("fake official recording", encoding="utf-8")
        return MaterialExecutionResult(
            success=True,
            status="已完成",
            material_path=str(self.output_path),
            material_type="来源页面录屏",
            source_type="来源页面",
            evidence_kind="direct_evidence",
            source_url="https://example.com/news",
            is_locally_generated=False,
            reason="来源页面慢速滚动录屏已生成。",
        )

    def _try_pexels(self, task: dict) -> MaterialExecutionResult:
        self.calls.append("Pexels")
        return MaterialExecutionResult(success=False, status="失败", reason="不应该调用 Pexels")


def test_auto_executor_returns_official_capture_before_fallback_sources(tmp_path):
    executor = _SuccessfulOfficialExecutor(tmp_path)
    result = executor.execute(
        {
            "shot_id": "shot-01",
            "needs_official_capture": True,
            "needs_pexels": True,
            "needs_info_motion": True,
            "official_source_url": "https://example.com/news",
            "source_platform": "Example News",
            "recommended_visual": "官方页面慢速滚动",
        },
        project_name="测试项目",
    )

    assert result.success is True
    assert result.source_type == "来源页面"
    assert result.material_type == "来源页面录屏"
    assert result.evidence_kind == "direct_evidence"
    assert result.source_url == "https://example.com/news"
    assert result.material_path.endswith("官方页面.mp4")
    assert executor.calls == ["官方页面录屏"]


class _ProjectPexelsExecutor(AutoMaterialTaskExecutor):
    def __init__(self, tmp_path: Path):
        self.raw_path = tmp_path / "临时素材" / "pexels.mp4"
        super().__init__(
            paths=AutoMaterialPaths(root=tmp_path / "自动素材"),
            project_paths=ProjectMaterialPaths(
                root=tmp_path / "短视频工厂",
                project_name="测试项目",
            ),
        )

    def _try_pexels(self, task: dict) -> MaterialExecutionResult:
        self.raw_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_path.write_text("fake pexels video", encoding="utf-8")
        return MaterialExecutionResult(
            success=True,
            status="已完成",
            material_path=str(self.raw_path),
            material_type="Pexels视频",
            source_type="Pexels",
            evidence_kind="atmosphere",
            source_url="https://videos.pexels.com/example.mp4",
            is_locally_generated=False,
            reason="按关键词匹配成功。",
        )


def test_auto_executor_copies_material_to_project_source_dir_and_all_materials(tmp_path):
    executor = _ProjectPexelsExecutor(tmp_path)

    result = executor.execute(
        {
            "shot_id": "shot-01",
            "needs_pexels": True,
            "asset_keywords": ["科技办公"],
            "recommended_visual": "科技办公背景",
        },
        project_name="测试项目",
    )

    all_material_path = Path(result.material_path)
    source_material_path = (
        tmp_path
        / "短视频工厂"
        / "项目"
        / "测试项目"
        / "Pexels素材"
        / all_material_path.name
    )
    assert result.success is True
    assert all_material_path.parent.name == "全部素材"
    assert all_material_path.is_file()
    assert source_material_path.is_file()
    assert "已归入项目全部素材" in result.reason


def test_skip_material_task_marks_task_skipped_and_advances_queue(tmp_path):
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)
        result = skip_material_task(session, pipeline_id, "shot-01")

        assert result["executed_task"]["status"] == "已跳过"
        assert "手动跳过" in result["executed_task"]["reject_reason"]
        assert result["tasks"][0]["status"] == "已跳过"
        assert result["tasks"][1]["status"] == "待执行"


def test_skip_material_task_rejects_a_completed_material_without_mutation(tmp_path):
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)
        completed = run_next_material_task(
            session,
            pipeline_id,
            executor=_SuccessfulExecutor(tmp_path / "素材" / "completed.mp4"),
        )["executed_task"]
        step = (
            session.query(PipelineStep)
            .filter(
                PipelineStep.pipeline_id == pipeline_id,
                PipelineStep.name == "生成素材任务",
            )
            .one()
        )
        before = deepcopy(step.output_summary)

        with pytest.raises(ValueError, match="成品路径|不可跳过"):
            skip_material_task(session, pipeline_id, completed["shot_id"])

        session.refresh(step)
        assert step.output_summary == before


def test_skip_route_reports_a_completed_material_as_a_conflict(tmp_path):
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)
        completed = run_next_material_task(
            session,
            pipeline_id,
            executor=_SuccessfulExecutor(tmp_path / "素材" / "completed-route.mp4"),
        )["executed_task"]

        with pytest.raises(HTTPException) as exc_info:
            skip_pipeline_material_task(
                pipeline_id,
                completed["shot_id"],
                db=session,
            )

        assert exc_info.value.status_code == 409


def test_skip_route_reports_unknown_commit_acknowledgement_as_service_unavailable(
    tmp_path,
    monkeypatch,
):
    with make_session() as session:
        pipeline_id = _build_pipeline(session, tmp_path)
        shot_id = get_material_package(session, pipeline_id)["material_tasks"][0][
            "shot_id"
        ]
        real_commit = session.commit

        def commit_then_lose_acknowledgement():
            real_commit()
            raise OSError("simulated acknowledgement loss")

        monkeypatch.setattr(session, "commit", commit_then_lose_acknowledgement)
        with pytest.raises(HTTPException) as exc_info:
            skip_pipeline_material_task(pipeline_id, shot_id, db=session)

        assert exc_info.value.status_code == 503


def test_skip_material_task_cannot_overwrite_a_concurrent_candidate_lock(tmp_path):
    database_path = tmp_path / "D" / "material-task-cas.sqlite3"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_a = factory()
    session_b = factory()
    try:
        pipeline_id = _build_pipeline(session_a, tmp_path)
        shot_id = get_material_package(session_a, pipeline_id)["material_tasks"][0][
            "shot_id"
        ]
        candidates = []
        for index in range(3):
            preview = tmp_path / f"candidate-{index + 1}.png"
            Image.new("RGB", (12, 8), (40 * (index + 1), 96, 160)).save(preview)
            candidates.append(
                {
                    "candidate_id": f"cand-{index + 1}",
                    "source": "owned-fixture-a" if index < 2 else "owned-fixture-b",
                    "preview_path": str(preview),
                    "url": "",
                    "rights": "self-owned",
                    "evidence_kind": "self_owned",
                    "status": "candidate",
                    "reason": "isolated concurrency fixture",
                }
            )
        save_material_candidate_pool(session_a, pipeline_id, shot_id, candidates)

        stale_pipeline = session_b.get(ProductionPipeline, pipeline_id)
        assert stale_pipeline is not None
        stale_step = next(
            step for step in stale_pipeline.steps if step.name == "生成素材任务"
        )
        _ = deepcopy(stale_step.output_summary)

        locked = lock_material_candidate(
            session_a,
            pipeline_id,
            shot_id,
            "cand-1",
            reason="concurrent review lock",
        )
        assert locked["locked_candidate_id"] == "cand-1"

        with pytest.raises(MaterialTaskConflictError):
            skip_material_task(session_b, pipeline_id, shot_id)

        session_b.expire_all()
        current = get_material_package(session_b, pipeline_id)
        current_task = next(
            task for task in current["material_tasks"] if task["shot_id"] == shot_id
        )
        assert current_task["candidate_pool"]["locked_candidate_id"] == "cand-1"
        assert current_task["status"] != "已跳过"
    finally:
        session_a.close()
        session_b.close()
        engine.dispose()


def test_reconcile_material_task_cannot_overwrite_a_concurrent_candidate_lock(tmp_path):
    database_path = tmp_path / "D" / "material-retry-cas.sqlite3"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_a = factory()
    session_b = factory()
    try:
        pipeline_id = _build_pipeline(session_a, tmp_path)
        package = get_material_package(session_a, pipeline_id)
        shot_id = package["material_tasks"][0]["shot_id"]
        step_a = (
            session_a.query(PipelineStep)
            .filter(
                PipelineStep.pipeline_id == pipeline_id,
                PipelineStep.name == "生成素材任务",
            )
            .one()
        )
        payload = deepcopy(step_a.output_summary)
        payload["material_tasks"][0]["status"] = "失败"
        payload["material_tasks"][0]["material_path"] = ""
        payload["material_tasks"][0]["reject_reason"] = "上次获取失败"
        step_a.output_summary = payload
        session_a.add(step_a)
        session_a.commit()

        candidates = []
        for index in range(3):
            preview = tmp_path / f"retry-candidate-{index + 1}.png"
            Image.new("RGB", (12, 8), (32 * (index + 1), 104, 168)).save(preview)
            candidates.append(
                {
                    "candidate_id": f"retry-cand-{index + 1}",
                    "source": "owned-retry-a" if index < 2 else "owned-retry-b",
                    "preview_path": str(preview),
                    "url": "",
                    "rights": "self-owned",
                    "evidence_kind": "self_owned",
                    "status": "candidate",
                    "reason": "isolated retry concurrency fixture",
                }
            )
        save_material_candidate_pool(session_a, pipeline_id, shot_id, candidates)

        stale_pipeline = session_b.get(ProductionPipeline, pipeline_id)
        assert stale_pipeline is not None
        stale_step = next(
            step for step in stale_pipeline.steps if step.name == "生成素材任务"
        )
        _ = deepcopy(stale_step.output_summary)

        locked = lock_material_candidate(
            session_a,
            pipeline_id,
            shot_id,
            "retry-cand-1",
            reason="concurrent review lock",
        )
        assert locked["locked_candidate_id"] == "retry-cand-1"

        with pytest.raises(MaterialTaskConflictError):
            reconcile_material_task(
                session_b,
                pipeline_id,
                shot_id,
                executor=_SuccessfulExecutor(tmp_path / "素材" / "must-not-run.mp4"),
            )

        assert not (tmp_path / "素材" / "must-not-run.mp4").exists()
        session_b.expire_all()
        current = get_material_package(session_b, pipeline_id)
        current_task = next(
            task for task in current["material_tasks"] if task["shot_id"] == shot_id
        )
        assert current_task["candidate_pool"]["locked_candidate_id"] == "retry-cand-1"
        assert current_task["status"] == "失败"
    finally:
        session_a.close()
        session_b.close()
        engine.dispose()


def test_run_next_material_task_final_commit_cannot_overwrite_a_concurrent_lock(
    tmp_path,
):
    database_path = tmp_path / "D" / "material-run-cas.sqlite3"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    review_session = factory()
    runner_session = factory()
    try:
        pipeline_id = _build_pipeline(review_session, tmp_path)
        shot_id = get_material_package(review_session, pipeline_id)["material_tasks"][0][
            "shot_id"
        ]
        candidates = []
        for index in range(3):
            preview = tmp_path / f"run-candidate-{index + 1}.png"
            Image.new("RGB", (12, 8), (24 * (index + 1), 112, 176)).save(preview)
            candidates.append(
                {
                    "candidate_id": f"run-cand-{index + 1}",
                    "source": "owned-run-a" if index < 2 else "owned-run-b",
                    "preview_path": str(preview),
                    "url": "",
                    "rights": "self-owned",
                    "evidence_kind": "self_owned",
                    "status": "candidate",
                    "reason": "isolated run concurrency fixture",
                }
            )
        save_material_candidate_pool(review_session, pipeline_id, shot_id, candidates)

        output_path = tmp_path / "素材" / "concurrent-result.mp4"

        class LockDuringExecution:
            def execute(self, task: dict, *, project_name: str) -> MaterialExecutionResult:
                review_session.expire_all()
                locked = lock_material_candidate(
                    review_session,
                    pipeline_id,
                    shot_id,
                    "run-cand-1",
                    reason="locked while material executor was running",
                )
                assert locked["locked_candidate_id"] == "run-cand-1"
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text("fake material", encoding="utf-8")
                return MaterialExecutionResult(
                    success=True,
                    status="已完成",
                    material_path=str(output_path),
                    material_type="Pexels视频",
                    source_type="Pexels",
                    evidence_kind="atmosphere",
                    source_url="https://videos.pexels.com/example.mp4",
                    is_locally_generated=False,
                    reason="按关键词匹配成功。",
                )

        with pytest.raises(MaterialTaskConflictError):
            run_next_material_task(
                runner_session,
                pipeline_id,
                executor=LockDuringExecution(),
            )

        assert output_path.exists()
        runner_session.expire_all()
        current = get_material_package(runner_session, pipeline_id)
        current_task = next(
            task for task in current["material_tasks"] if task["shot_id"] == shot_id
        )
        assert current_task["candidate_pool"]["locked_candidate_id"] == "run-cand-1"
        assert current_task["status"] != "已完成"
    finally:
        review_session.close()
        runner_session.close()
        engine.dispose()
