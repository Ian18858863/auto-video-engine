from __future__ import annotations

import copy
import shutil
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_configured_path
from app.materials.asset_paths import AutoMaterialPaths
from app.materials.end_to_end_video import (
    _frames_to_video,
    _safe_filename,
    _write_motion_frames,
)
from app.materials.official_capture import (
    OfficialPageCaptureService,
    build_official_capture_plan,
)
from app.materials.pexels import PexelsDownloadService
from app.materials.provenance import (
    classify_capture_provenance,
    is_http_source_url,
    validate_execution_provenance,
)
from app.materials.paths import PROJECT_DIRS, ProjectMaterialPaths
from app.materials.source_clients import CoverrDownloadService, PixabayDownloadService
from app.materials.stock_rights_evidence import (
    StockRightsEvidenceError,
    normalise_stock_rights_evidence,
)
from app.models import PipelineStep, ProductionPipeline
from app.pipeline.material_candidate_contract import CandidateEntry, CandidateSet
from app.pipeline.material_selection_invalidation import (
    MaterialSelectionDependencyError,
    assert_material_selection_dependency_current,
)
from app.pipeline.project_identity import (
    PreparedProjectChildDirectoryUse,
    ProjectIdentityError,
    begin_prepared_project_child_directory_use,
    begin_prepared_project_directory_use,
    prepared_project_output_path,
    resolve_prepared_project_directory,
)
from app.pipeline.service import PipelineNotFoundError


MATERIAL_STEP_NAME = "生成素材任务"
WAITING_STATUSES = {"待执行", "等待", "未开始", "待搜索"}
RESUMABLE_STATUSES = {
    "进行中",
    "官方页面录屏中",
    "官方页面录屏失败",
    "Pexels下载中",
    "Pexels下载失败",
    "Pixabay下载中",
    "Pixabay下载失败",
    "Coverr下载中",
    "Coverr下载失败",
    "信息动效生成中",
}
PRIMARY_SOURCE_TIMEOUT_SECONDS = 180
MaterialProgressCallback = Callable[[str, str], None]


class MaterialTaskConflictError(ValueError):
    """The requested task transition conflicts with its current durable state."""


class MaterialTaskCommitUnknownError(RuntimeError):
    """The database may have committed; callers must reconcile before retrying."""


def _reject_stale_material_selection(
    payload: dict[str, Any],
    pipeline: ProductionPipeline,
) -> None:
    try:
        assert_material_selection_dependency_current(payload, pipeline.script_asset)
    except MaterialSelectionDependencyError as exc:
        raise MaterialTaskConflictError(
            f"旧素材任务和锁定不能继续执行：{exc}"
        ) from exc


@dataclass(slots=True)
class MaterialExecutionResult:
    success: bool
    status: str
    material_path: str = ""
    material_type: str = ""
    source_type: str = ""
    reason: str = ""
    evidence_kind: str = ""
    source_url: str = ""
    is_locally_generated: bool = False
    rights_evidence: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        validate_execution_provenance(
            success=self.success,
            evidence_kind=self.evidence_kind,
            source_url=self.source_url,
            is_locally_generated=self.is_locally_generated,
        )
        if self.rights_evidence is not None:
            try:
                canonical = normalise_stock_rights_evidence(self.rights_evidence)
            except StockRightsEvidenceError as exc:
                raise ValueError("material rights_evidence is invalid") from exc
            if self.source_url != canonical["asset_page_url"]:
                raise ValueError(
                    "material source_url must match rights_evidence asset_page_url"
                )
            self.rights_evidence = canonical


@dataclass(slots=True)
class MaterialCandidateBatchResult:
    """Parent-published 3–5 candidate rows awaiting Ian's explicit choice."""

    success: bool
    status: str
    candidates: tuple[CandidateEntry, ...]
    reason: str = ""

    def __post_init__(self) -> None:
        if self.success is not True:
            raise ValueError("candidate batch result must be successful")
        if not isinstance(self.status, str) or not self.status.strip():
            raise ValueError("candidate batch status is required")
        if type(self.candidates) is not tuple:
            raise TypeError("candidate batch candidates must be a tuple")
        if not isinstance(self.reason, str):
            raise TypeError("candidate batch reason must be a string")
        # This validates the 3–5 range, unique IDs, two real sources, existing
        # preview files and all candidate provenance fields without persisting
        # a second mutable representation.
        CandidateSet.from_candidates("published-candidate-batch", self.candidates)


class MaterialTaskExecutor(Protocol):
    def execute(self, task: dict, *, project_name: str) -> MaterialExecutionResult:
        ...


class AutoMaterialTaskExecutor:
    def __init__(
        self,
        *,
        paths: AutoMaterialPaths | None = None,
        project_paths: ProjectMaterialPaths | None = None,
        project_directory_uses: dict[str, PreparedProjectChildDirectoryUse] | None = None,
        primary_timeout_seconds: int = PRIMARY_SOURCE_TIMEOUT_SECONDS,
        progress_callback: MaterialProgressCallback | None = None,
        coverr_enabled: bool = False,
    ):
        self.paths = paths or AutoMaterialPaths(root=get_settings().auto_material_root_dir)
        self.project_paths = project_paths
        self.project_directory_uses = project_directory_uses or {}
        self.primary_timeout_seconds = primary_timeout_seconds
        self.progress_callback = progress_callback
        self.coverr_enabled = coverr_enabled is True

    def execute(self, task: dict, *, project_name: str) -> MaterialExecutionResult:
        self.paths.ensure_directories()
        if self.project_paths is not None:
            self.project_paths.ensure_directories()
            for directory_use in self.project_directory_uses.values():
                directory_use.verify()
        failed_reasons: list[str] = []

        if task.get("needs_official_capture"):
            self._emit_progress("官方页面录屏中", "正在尝试官方页面录屏，超过 3 分钟会自动转 Pexels。")
            official_result = self._run_with_timeout(
                "官方页面录屏",
                lambda: self._try_official_capture(task),
            )
            if official_result.success:
                return self._store_project_material(official_result, task)
            official_reason = official_result.reason or "官方页面录屏失败。"
            failed_reasons.append(official_reason)
            self._emit_progress("官方页面录屏失败", official_reason)

        if task.get("needs_pexels"):
            self._emit_progress("Pexels下载中", "正在尝试 Pexels 下载，超过 3 分钟会自动转信息动效。")
            pexels_result = self._run_with_timeout(
                "Pexels 下载",
                lambda: self._try_pexels(task),
            )
            if pexels_result.success:
                return self._store_project_material(pexels_result, task)
            pexels_reason = pexels_result.reason or "Pexels 下载失败。"
            failed_reasons.append(pexels_reason)
            self._emit_progress("Pexels下载失败", pexels_reason)

            self._emit_progress("Pixabay下载中", "Pexels 不可用，正在尝试 Pixabay。")
            pixabay_result = self._run_with_timeout(
                "Pixabay 下载",
                lambda: self._try_pixabay(task),
            )
            if pixabay_result.success:
                return self._store_project_material(pixabay_result, task)
            pixabay_reason = pixabay_result.reason or "Pixabay 下载失败。"
            failed_reasons.append(pixabay_reason)
            self._emit_progress("Pixabay下载失败", pixabay_reason)

            if self.coverr_enabled:
                self._emit_progress("Coverr下载中", "Pixabay 不可用，正在尝试 Coverr。")
                coverr_result = self._run_with_timeout(
                    "Coverr 下载",
                    lambda: self._try_coverr(task),
                )
                if coverr_result.success:
                    return self._store_project_material(coverr_result, task)
                coverr_reason = coverr_result.reason or "Coverr 下载失败。"
                failed_reasons.append(coverr_reason)
                self._emit_progress("Coverr下载失败", coverr_reason)

        backup_result = self._try_backup_material(task)
        if backup_result.success:
            if failed_reasons:
                backup_result.reason = (
                    "主素材通道失败后，已使用备选素材。失败原因："
                    + "；".join(failed_reasons)
                )
            return self._store_project_material(backup_result, task)

        if task.get("needs_info_motion"):
            self._emit_progress("信息动效生成中", "主素材通道不可用，正在使用信息动效兜底。")
            motion_result = self._try_info_motion(task, project_name=project_name)
            if motion_result.success and failed_reasons:
                motion_result.reason = (
                    "主素材通道失败后，已使用信息动效兜底。失败原因："
                    + "；".join(failed_reasons)
                )
            return self._store_project_material(motion_result, task)

        return MaterialExecutionResult(
            success=False,
            status="失败",
            source_url=str(task.get("official_source_url") or "").strip(),
            reason="；".join(failed_reasons) or "该镜头没有可执行的素材来源。",
        )

    def _emit_progress(self, status: str, reason: str = "") -> None:
        if self.progress_callback is not None:
            self.progress_callback(status, reason)

    def _run_with_timeout(
        self,
        source_label: str,
        operation,
    ) -> MaterialExecutionResult:
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(operation)
        try:
            return future.result(timeout=self.primary_timeout_seconds)
        except FutureTimeoutError:
            return MaterialExecutionResult(
                success=False,
                status="失败",
                material_type=source_label,
                source_type=source_label,
                reason=f"{source_label}超过 3 分钟仍未完成，已自动跳过该通道。",
            )
        except Exception as exc:
            return MaterialExecutionResult(
                success=False,
                status="失败",
                material_type=source_label,
                source_type=source_label,
                reason=f"{source_label}执行失败：{exc}",
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _try_official_capture(self, task: dict) -> MaterialExecutionResult:
        url = str(task.get("official_source_url") or "").strip()
        if not url:
            return MaterialExecutionResult(
                success=False,
                status="失败",
                material_type="官方页面录屏",
                source_type="官方页面",
                reason="缺少新闻原文 URL，已跳过官方页面录屏通道。",
            )
        if not is_http_source_url(url):
            return MaterialExecutionResult(
                success=False,
                status="失败",
                material_type="来源页面录屏",
                source_type="来源页面",
                source_url=url,
                reason="来源页面录屏只接受有效的 http/https URL，已在抓取前拒绝。",
            )
        plan = build_official_capture_plan(
            url=url,
            source_name=str(
                task.get("official_source_name")
                or task.get("source_platform")
                or task.get("source_type")
                or "官方页面"
            ),
            page_title=str(
                task.get("official_page_title")
                or task.get("recommended_visual")
                or task.get("voiceover")
                or "官方页面"
            ),
            paths=self.paths,
        )
        capture = OfficialPageCaptureService(paths=self.paths).capture(plan)
        if not capture.success:
            return MaterialExecutionResult(
                success=False,
                status="失败",
                material_type="官方页面录屏",
                source_type="官方页面",
                source_url=url,
                reason=capture.reason or "官方页面录屏失败。",
            )
        provenance = classify_capture_provenance(
            url,
            used_placeholder=capture.used_placeholder,
        )
        is_explanation = provenance.evidence_kind == "explanation"
        return MaterialExecutionResult(
            success=True,
            status="已完成",
            material_path=capture.scroll_recording_path,
            material_type="解释卡" if is_explanation else "来源页面录屏",
            source_type="本地生成" if is_explanation else "来源页面",
            evidence_kind=provenance.evidence_kind,
            source_url=provenance.source_url,
            is_locally_generated=provenance.is_locally_generated,
            reason=capture.reason or "来源页面慢速滚动录屏已生成。",
        )

    def _try_pexels(self, task: dict) -> MaterialExecutionResult:
        keyword = _pexels_query(task)
        result = PexelsDownloadService(paths=self.paths).download_one_video_and_one_photo(
            keyword,
            per_page=get_settings().pexels_max_candidates_per_keyword,
        )
        if result.video.status == "已下载":
            return MaterialExecutionResult(
                success=True,
                status="已完成",
                material_path=result.video.save_path,
                material_type="Pexels视频",
                source_type="Pexels",
                evidence_kind="atmosphere",
                source_url=result.video.source_url,
                is_locally_generated=False,
                rights_evidence=result.video.rights_evidence or None,
                reason=f"按关键词「{keyword}」下载到 Pexels 视频。",
            )
        if result.photo.status == "已下载":
            return MaterialExecutionResult(
                success=True,
                status="已完成",
                material_path=result.photo.save_path,
                material_type="Pexels图片",
                source_type="Pexels",
                evidence_kind="atmosphere",
                source_url=result.photo.source_url,
                is_locally_generated=False,
                rights_evidence=result.photo.rights_evidence or None,
                reason=f"按关键词「{keyword}」未找到视频，下载到 Pexels 图片。",
            )
        reasons = [
            item.reason
            for item in [result.video, result.photo]
            if item.reason
        ]
        return MaterialExecutionResult(
            success=False,
            status="失败",
            material_type="Pexels",
            source_type="Pexels",
            source_url=next(
                (
                    item.source_url
                    for item in (result.video, result.photo)
                    if item.source_url
                ),
                "",
            ),
            reason="；".join(reasons) or "Pexels 没有找到合适素材。",
        )

    def _try_pixabay(self, task: dict) -> MaterialExecutionResult:
        keyword = _pexels_query(task)
        result = PixabayDownloadService(paths=self.paths).download_best(
            keyword,
            per_page=get_settings().pixabay_max_candidates_per_keyword,
        )
        if result.status == "已下载":
            return MaterialExecutionResult(
                success=True,
                status="已完成",
                material_path=result.save_path,
                material_type="Pixabay视频" if result.asset_type == "video" else "Pixabay图片",
                source_type="Pixabay",
                evidence_kind="atmosphere",
                source_url=result.source_url,
                is_locally_generated=False,
                rights_evidence=result.rights_evidence or None,
                reason=f"按关键词「{keyword}」下载到 Pixabay 素材。",
            )
        return MaterialExecutionResult(
            success=False,
            status="失败",
            material_type="Pixabay",
            source_type="Pixabay",
            source_url=result.source_url,
            reason=result.reason or "Pixabay 没有找到合适素材。",
        )

    def _try_coverr(self, task: dict) -> MaterialExecutionResult:
        keyword = _pexels_query(task)
        result = CoverrDownloadService(paths=self.paths).download_best(
            keyword,
            per_page=get_settings().coverr_max_candidates_per_keyword,
        )
        if result.status == "已下载":
            return MaterialExecutionResult(
                success=True,
                status="已完成",
                material_path=result.save_path,
                material_type="Coverr视频",
                source_type="Coverr",
                evidence_kind="atmosphere",
                source_url=result.source_url,
                is_locally_generated=False,
                reason=f"按关键词「{keyword}」下载到 Coverr 视频。",
            )
        return MaterialExecutionResult(
            success=False,
            status="失败",
            material_type="Coverr",
            source_type="Coverr",
            source_url=result.source_url,
            reason=result.reason or "Coverr 没有找到合适素材。",
        )

    def _try_backup_material(self, task: dict) -> MaterialExecutionResult:
        if self.project_paths is None:
            return MaterialExecutionResult(success=False, status="失败", reason="没有项目路径，无法读取备选素材。")
        backup_dir = self.project_paths.backup_material_dir
        if not backup_dir.is_dir():
            return MaterialExecutionResult(success=False, status="失败", reason="未找到 D:/短视频工厂/备选素材。")
        allowed = {".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp"}
        candidates = [
            item
            for item in backup_dir.rglob("*")
            if item.is_file() and item.suffix.lower() in allowed
        ]
        if not candidates:
            return MaterialExecutionResult(success=False, status="失败", reason="备选素材文件夹为空。")
        chosen = sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True)[0]
        return MaterialExecutionResult(
            success=True,
            status="已完成",
            material_path=str(chosen),
            material_type="备选素材",
            source_type="备选素材",
            evidence_kind="atmosphere",
            source_url="",
            is_locally_generated=False,
            reason=f"自动素材源不可用，已使用备选素材：{chosen.name}",
        )

    def _try_info_motion(
        self,
        task: dict,
        *,
        project_name: str,
    ) -> MaterialExecutionResult:
        now = datetime.now()
        title = str(task.get("recommended_visual") or task.get("voiceover") or project_name)
        stable_project_identity = (
            self.project_paths.project_key
            if self.project_paths is not None and self.project_paths.project_key
            else project_name
        )
        base_name = _safe_filename(
            f"{stable_project_identity}_{task.get('shot_id', 'shot')}_"
            f"{now:%Y%m%d_%H%M%S_%f}_{uuid4().hex[:8]}"
        )
        frame_dir = self.paths.generated_motion_dir / base_name / "frames"
        frame_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.paths.generated_motion_dir / f"{base_name}.mp4"
        try:
            _write_motion_frames(frame_dir, project_name, title, prefix="motion")
            _frames_to_video(frame_dir / "motion_%03d.png", output_path, duration_seconds=6)
        except Exception as exc:
            return MaterialExecutionResult(
                success=False,
                status="失败",
                material_type="信息动效",
                source_type="信息动效",
                reason=f"信息动效生成失败：{exc}",
            )
        return MaterialExecutionResult(
            success=True,
            status="已完成",
            material_path=str(output_path),
            material_type="信息动效",
            source_type="自动生成",
            evidence_kind="explanation",
            source_url="",
            is_locally_generated=True,
            reason="Pexels 不可用或信息更适合动效表达，已生成基础信息动效。",
        )

    def _store_project_material(
        self,
        result: MaterialExecutionResult,
        task: dict,
    ) -> MaterialExecutionResult:
        if self.project_paths is None or not result.material_path:
            return result
        source_path = Path(result.material_path)
        if not source_path.is_file():
            return result

        self.project_paths.ensure_directories()
        source_dir = self._project_source_dir(result)

        shot_id = str(task.get("shot_id") or "shot")
        material_label = str(result.material_type or result.source_type or "素材")
        filename = _safe_project_filename(
            f"{shot_id}_{material_label}_{source_path.stem}"
        ) + (source_path.suffix or ".mp4")

        if self.project_paths.project_key is None:
            source_dir.mkdir(parents=True, exist_ok=True)
            self.project_paths.all_materials_dir.mkdir(parents=True, exist_ok=True)
            source_target = _unique_path(source_dir / filename)
            if source_path.resolve() != source_target.resolve():
                shutil.copy2(source_path, source_target)
            all_target = _unique_path(self.project_paths.all_materials_dir / filename)
            if source_target.resolve() != all_target.resolve():
                shutil.copy2(source_target, all_target)
            reason_suffix = f"已归入项目全部素材：{all_target}"
            reason = f"{result.reason} {reason_suffix}".strip()
            return replace(result, material_path=str(all_target), reason=reason)

        source_use = self._project_directory_use(source_dir)
        all_use = self._project_directory_use(self.project_paths.all_materials_dir)

        source_target = _safe_unique_project_output_path(source_use, filename)
        all_target = _safe_unique_project_output_path(all_use, filename)
        try:
            if source_path.resolve() != source_target.resolve():
                shutil.copy2(source_path, source_target)
            prepared_project_output_path(source_use, source_target.name, must_exist=True)

            if source_target.resolve() != all_target.resolve():
                shutil.copy2(source_target, all_target)
            prepared_project_output_path(all_use, all_target.name, must_exist=True)
            source_use.verify()
            all_use.verify()
        except BaseException:
            for target, guarded_use in (
                (all_target, all_use),
                (source_target, source_use),
            ):
                try:
                    if target.exists():
                        prepared_project_output_path(
                            guarded_use,
                            target.name,
                            must_exist=True,
                        )
                        target.unlink()
                except (OSError, ValueError, ProjectIdentityError):
                    pass
            raise

        reason_suffix = f"已归入项目全部素材：{all_target}"
        reason = f"{result.reason} {reason_suffix}".strip()
        return replace(result, material_path=str(all_target), reason=reason)

    def _project_source_dir(self, result: MaterialExecutionResult) -> Path:
        label = f"{result.material_type} {result.source_type}"
        if result.evidence_kind == "direct_evidence":
            return self.project_paths.official_recording_dir  # type: ignore[union-attr]
        if result.evidence_kind == "explanation":
            return self.project_paths.info_motion_dir  # type: ignore[union-attr]
        if "Pexels" in label or "Pixabay" in label or "Coverr" in label or "备选素材" in label:
            return self.project_paths.pexels_material_dir  # type: ignore[union-attr]
        if "信息动效" in label or "自动生成" in label:
            return self.project_paths.info_motion_dir  # type: ignore[union-attr]
        return self.project_paths.all_materials_dir  # type: ignore[union-attr]

    def _project_directory_use(
        self,
        path: Path,
    ) -> PreparedProjectChildDirectoryUse:
        directory_use = self.project_directory_uses.get(path.name)
        if directory_use is None or directory_use.path != path:
            raise ValueError(f"项目素材目录未通过身份验证：{path}")
        directory_use.verify()
        return directory_use


def run_next_material_task(
    session: Session,
    pipeline_id: int,
    *,
    executor: MaterialTaskExecutor | None = None,
    material_root: Path | None = None,
    project_root: Path | None = None,
    target_shot_id: str | None = None,
) -> dict:
    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None:
        raise PipelineNotFoundError(f"流程不存在：{pipeline_id}")
    step = _material_step(pipeline)
    payload = dict(step.output_summary or {})
    _reject_stale_material_selection(payload, pipeline)
    tasks = [
        dict(item)
        for item in payload.get("material_tasks", [])
        if isinstance(item, dict)
    ]
    if not tasks:
        raise ValueError("素材任务尚未生成。")

    if target_shot_id is None:
        task_index = _current_task_index(tasks)
    else:
        task_index = next(
            (
                index
                for index, task in enumerate(tasks)
                if task.get("shot_id") == target_shot_id
            ),
            None,
        )
        if task_index is None:
            raise ValueError(f"素材任务不存在：{target_shot_id}")
        target_task = tasks[task_index]
        target_status = str(target_task.get("status") or "")
        if target_task.get("material_path") or target_status not in (
            WAITING_STATUSES | RESUMABLE_STATUSES
        ):
            raise MaterialTaskConflictError(
                f"指定素材任务当前不可执行：{target_status or '未知'}"
            )
    if task_index is None:
        return {
            "pipeline_id": pipeline_id,
            "message": "没有等待执行的素材任务。",
            "executed_task": None,
            "tasks": tasks,
        }
    uses_auto_executor = executor is None or isinstance(
        executor,
        AutoMaterialTaskExecutor,
    )
    validated_project_paths = None
    directory_use = None
    project_directory_uses: dict[str, PreparedProjectChildDirectoryUse] = {}
    if uses_auto_executor:
        validated_project_paths = _project_paths_for_pipeline(
            pipeline,
            material_root=material_root,
            project_root=project_root,
        )
        approved_project_root = resolve_configured_path(
            project_root or get_settings().project_root_dir
        )
        directory_use = begin_prepared_project_directory_use(
            pipeline,
            project_root=approved_project_root,
        )
        project_directory_uses = {
            dirname: begin_prepared_project_child_directory_use(
                pipeline,
                project_root=approved_project_root,
                relative_path=dirname,
                create=False,
            )
            for dirname in PROJECT_DIRS
        }

    original_payload = copy.deepcopy(payload)
    expected_payload = copy.deepcopy(payload)
    task = tasks[task_index]
    task["status"] = "进行中"
    _append_history(task, "进行中")
    tasks[task_index] = task
    payload["material_tasks"] = tasks
    _persist_material_step_payload(
        session,
        step,
        expected_payload=expected_payload,
        replacement_payload=payload,
    )

    def persist_progress(status: str, reason: str = "") -> None:
        progress_expected_payload = copy.deepcopy(payload)
        task["status"] = status
        if reason:
            task["notes"] = reason
        _append_history(task, status)
        tasks[task_index] = task
        payload["material_tasks"] = tasks
        _persist_material_step_payload(
            session,
            step,
            expected_payload=progress_expected_payload,
            replacement_payload=payload,
        )

    material_executor = executor
    if material_executor is None:
        material_executor = AutoMaterialTaskExecutor(
            progress_callback=persist_progress,
            project_paths=validated_project_paths,
            project_directory_uses=project_directory_uses,
        )
    elif isinstance(material_executor, AutoMaterialTaskExecutor):
        material_executor.progress_callback = persist_progress
        # An injected Auto executor may carry stale/title-derived paths. The
        # pipeline identity is authoritative for every real filesystem write.
        material_executor.project_paths = validated_project_paths
        material_executor.project_directory_uses = project_directory_uses

    try:
        result = material_executor.execute(
            task,
            project_name=pipeline.project_name,
        )
        for child_use in project_directory_uses.values():
            child_use.verify()
        if directory_use is not None:
            directory_use.verify()
        validate_execution_provenance(
            success=result.success,
            evidence_kind=result.evidence_kind,
            source_url=result.source_url,
            is_locally_generated=result.is_locally_generated,
        )
    except (MaterialTaskConflictError, MaterialTaskCommitUnknownError):
        raise
    except BaseException:
        _persist_material_step_payload(
            session,
            step,
            expected_payload=payload,
            replacement_payload=original_payload,
        )
        raise
    final_expected_payload = copy.deepcopy(payload)
    task["status"] = result.status
    task["material_path"] = result.material_path
    task["material_type"] = result.material_type or task.get("material_type", "")
    task["source_type"] = result.source_type or task.get("source_type", "")
    task["evidence_kind"] = result.evidence_kind
    task["source_url"] = result.source_url
    task["is_locally_generated"] = result.is_locally_generated
    if result.rights_evidence is None:
        task.pop("rights_evidence", None)
    else:
        task["rights_evidence"] = copy.deepcopy(result.rights_evidence)
    task["notes"] = result.reason if result.success else ""
    task["reject_reason"] = "" if result.success else result.reason
    _append_history(task, result.status)
    tasks[task_index] = task

    _activate_next_waiting_task(tasks, after_index=task_index)
    payload["material_tasks"] = tasks
    _persist_material_step_payload(
        session,
        step,
        expected_payload=final_expected_payload,
        replacement_payload=payload,
    )

    return {
        "pipeline_id": pipeline_id,
        "message": "素材任务已完成" if result.success else "素材任务失败，已记录原因",
        "executed_task": task,
        "tasks": tasks,
    }


def reconcile_material_task(
    session: Session,
    pipeline_id: int,
    shot_id: str,
    *,
    executor: MaterialTaskExecutor | None = None,
    material_root: Path | None = None,
    project_root: Path | None = None,
) -> dict:
    """Put one failed/stuck task back in the queue, then retry it once.

    This is the compatibility seam for the existing material page.  It does
    not invent a second execution state machine: the durable execution lane
    remains the long-term owner, while the legacy service reuses
    ``run_next_material_task`` until the application lifespan is wired to that
    lane.  A task with a published path is deliberately never overwritten.
    """

    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None:
        raise PipelineNotFoundError(f"流程不存在：{pipeline_id}")
    step = _material_step(pipeline)
    payload = dict(step.output_summary or {})
    _reject_stale_material_selection(payload, pipeline)
    tasks = [
        dict(item)
        for item in payload.get("material_tasks", [])
        if isinstance(item, dict)
    ]
    if not tasks:
        raise ValueError("素材任务尚未生成。")

    task_index = next(
        (index for index, task in enumerate(tasks) if task.get("shot_id") == shot_id),
        None,
    )
    if task_index is None:
        raise ValueError(f"素材任务不存在：{shot_id}")

    task = tasks[task_index]
    if task.get("material_path"):
        raise MaterialTaskConflictError(
            "该素材任务已经有成品路径，不能覆盖已完成或已锁定素材。"
        )
    status = str(task.get("status") or "")
    if status not in (RESUMABLE_STATUSES | {"失败", "被阻止"}):
        raise MaterialTaskConflictError(f"当前状态不可恢复：{status or '未知'}")

    expected_payload = copy.deepcopy(payload)
    task["status"] = "待执行"
    task["reject_reason"] = ""
    task["notes"] = "已人工确认恢复，重新进入素材队列。"
    _append_history(task, "待执行")
    tasks[task_index] = task
    payload["material_tasks"] = tasks
    _persist_material_step_payload(
        session,
        step,
        expected_payload=expected_payload,
        replacement_payload=payload,
    )

    return run_next_material_task(
        session,
        pipeline_id,
        executor=executor,
        material_root=material_root,
        project_root=project_root,
        target_shot_id=shot_id,
    )


def skip_material_task(
    session: Session,
    pipeline_id: int,
    shot_id: str,
    *,
    reason: str = "手动跳过：这条素材任务暂时不执行，继续处理下一条。",
) -> dict:
    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None:
        raise PipelineNotFoundError(f"流程不存在：{pipeline_id}")
    step = _material_step(pipeline)
    payload = dict(step.output_summary or {})
    _reject_stale_material_selection(payload, pipeline)
    tasks = [
        dict(item)
        for item in payload.get("material_tasks", [])
        if isinstance(item, dict)
    ]
    if not tasks:
        raise ValueError("素材任务尚未生成。")

    task_index = next(
        (index for index, task in enumerate(tasks) if task.get("shot_id") == shot_id),
        None,
    )
    if task_index is None:
        raise ValueError(f"素材任务不存在：{shot_id}")

    task = tasks[task_index]
    status = str(task.get("status") or "")
    if task.get("material_path"):
        raise MaterialTaskConflictError("该素材任务已经有成品路径，不可跳过。")
    if status not in (WAITING_STATUSES | {"失败", "已拒绝"}):
        raise MaterialTaskConflictError(f"当前状态不可跳过：{status or '未知'}")
    expected_payload = copy.deepcopy(payload)
    task["status"] = "已跳过"
    task["reject_reason"] = reason
    task["notes"] = ""
    _append_history(task, "已跳过")
    tasks[task_index] = task

    if not any(item.get("status") == "进行中" for item in tasks):
        _activate_next_waiting_task(tasks, after_index=task_index)

    payload["material_tasks"] = tasks
    _persist_material_step_payload(
        session,
        step,
        expected_payload=expected_payload,
        replacement_payload=payload,
    )

    return {
        "pipeline_id": pipeline_id,
        "message": "已跳过当前素材任务，并继续下一条。",
        "executed_task": task,
        "tasks": tasks,
    }


def _material_step(pipeline: ProductionPipeline) -> PipelineStep:
    step = next((item for item in pipeline.steps if item.name == MATERIAL_STEP_NAME), None)
    if step is None:
        raise ValueError("流程缺少素材任务步骤。")
    return step


def _persist_material_step_payload(
    session: Session,
    step: PipelineStep,
    *,
    expected_payload: dict,
    replacement_payload: dict,
) -> None:
    """Persist one short material-step transition without overwriting a peer write."""

    pipeline_id = step.pipeline_id
    if pipeline_id is None:
        raise MaterialTaskConflictError("素材步骤缺少项目标识。")
    with session.no_autoflush:
        result = session.execute(
            update(PipelineStep)
            .where(
                PipelineStep.id == step.id,
                PipelineStep.pipeline_id == pipeline_id,
                PipelineStep.output_summary == copy.deepcopy(expected_payload),
            )
            .values(output_summary=copy.deepcopy(replacement_payload))
            .execution_options(synchronize_session=False)
        )
    rowcount = result.rowcount
    if not isinstance(rowcount, int) or rowcount < 0 or rowcount > 1:
        session.rollback()
        raise MaterialTaskCommitUnknownError(
            "素材任务提交结果未知，请先刷新核对后再操作。"
        )
    if rowcount == 0:
        session.rollback()
        raise MaterialTaskConflictError("素材任务状态已变化，请刷新后重试。")
    try:
        session.commit()
    except BaseException as exc:
        try:
            session.rollback()
        except BaseException:
            pass
        raise MaterialTaskCommitUnknownError(
            "素材任务提交确认丢失，请先刷新核对后再操作。"
        ) from exc
    session.expire(step, ["output_summary"])


def _current_task_index(tasks: list[dict]) -> int | None:
    for index, task in enumerate(tasks):
        if task.get("status") in RESUMABLE_STATUSES and not task.get("material_path"):
            return index
    for index, task in enumerate(tasks):
        if task.get("status") in WAITING_STATUSES:
            return index
    return None


def _activate_next_waiting_task(tasks: list[dict], *, after_index: int) -> None:
    for task in tasks[after_index + 1 :]:
        if task.get("status") in WAITING_STATUSES:
            task["status"] = "待执行"
            _append_history(task, "待执行")
            return


def _append_history(task: dict, status: str) -> None:
    history = list(task.get("status_history") or [])
    if not history or history[-1] != status:
        history.append(status)
    task["status_history"] = history[-20:]


def _pexels_query(task: dict) -> str:
    keywords = task.get("asset_keywords") or task.get("jianying_keywords") or []
    joined = " ".join(str(item) for item in keywords)
    text = f"{joined} {task.get('recommended_visual', '')} {task.get('voiceover', '')}"
    mapping = [
        ("AI", "artificial intelligence technology office"),
        ("工具", "software productivity office"),
        ("科技", "technology office"),
        ("办公", "modern office"),
        ("商业", "business meeting"),
        ("会议", "business meeting"),
        ("财经", "financial news office"),
        ("数据", "data analysis dashboard"),
        ("消费", "shopping consumer lifestyle"),
        ("城市", "city office"),
        ("企业", "business team office"),
        ("采购", "business procurement meeting"),
        ("效率", "productivity office"),
    ]
    selected: list[str] = []
    for token, query in mapping:
        if token in text and query not in selected:
            selected.append(query)
    return " ".join(selected[:2]) or "technology business office"


def _project_paths_for_pipeline(
    pipeline: ProductionPipeline,
    *,
    material_root: Path | None = None,
    project_root: Path | None = None,
) -> ProjectMaterialPaths:
    settings = get_settings()
    approved_project_root = resolve_configured_path(
        project_root or settings.project_root_dir
    )
    resolved = resolve_prepared_project_directory(
        pipeline,
        project_root=approved_project_root,
    )
    paths = ProjectMaterialPaths(
        root=resolve_configured_path(material_root or settings.material_root_dir),
        project_name=pipeline.project_name,
        project_key=pipeline.project_key,
        project_root_dir=approved_project_root,
    )
    if paths.project_dir != resolved:
        raise RuntimeError("项目身份目录解析结果不一致。")
    return paths


def _safe_project_filename(value: str) -> str:
    unsafe = '<>:"/\\|?*'
    name = "".join("_" if char in unsafe else char for char in value)
    name = "_".join(part for part in name.strip().split() if part)
    return name[:80] or "素材"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法生成不重复素材文件名：{path}")


def _safe_unique_project_output_path(
    directory_use: PreparedProjectChildDirectoryUse,
    filename: str,
) -> Path:
    candidate = prepared_project_output_path(directory_use, filename)
    for index in range(2, 1000):
        if not candidate.exists():
            return candidate
        candidate = prepared_project_output_path(
            directory_use,
            f"{Path(filename).stem}_{index}{Path(filename).suffix}",
        )
    raise RuntimeError(f"无法生成安全的不重复素材文件名：{filename}")
