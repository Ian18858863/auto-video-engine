from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from app.materials.local_library import LocalMaterialLibrary
from app.materials.paths import ProjectMaterialPaths
from app.materials.planner import MaterialTask
from app.materials.scoring import CandidateMaterial, CandidateScore, is_download_allowed, score_candidate


@dataclass(slots=True)
class DownloadResult:
    success: bool
    downloaded_path: Path | None = None
    error_message: str = ""


class JianyingAutomationClient(Protocol):
    def search(self, keywords: list[str]) -> list[CandidateMaterial]:
        raise NotImplementedError

    def download(self, candidate: CandidateMaterial, download_dir: Path) -> DownloadResult:
        raise NotImplementedError


@dataclass(slots=True)
class RejectedCandidate:
    material_id: str
    reason: str
    score: float


@dataclass(slots=True)
class MaterialProcessResult:
    task_id: str
    status: str
    selected_material_path: str | None = None
    selected_candidate_id: str = ""
    selected_score: float = 0.0
    failure_reason: str = ""
    rejected_candidates: list[RejectedCandidate] = field(default_factory=list)


class JianyingMaterialAssistant:
    def __init__(
        self,
        paths: ProjectMaterialPaths,
        client: JianyingAutomationClient,
        local_library: LocalMaterialLibrary | None = None,
        min_material_score: float = 60.0,
        max_candidates_per_search: int = 5,
        max_downloads_per_task: int = 1,
    ):
        self.paths = paths
        self.client = client
        self.local_library = local_library
        self.min_material_score = min_material_score
        self.max_candidates_per_search = max_candidates_per_search
        self.max_downloads_per_task = max_downloads_per_task

    def process_task(
        self,
        task: MaterialTask,
        candidates: list[CandidateMaterial] | None = None,
        existing_keywords: list[str] | None = None,
    ) -> MaterialProcessResult:
        self.paths.ensure_directories()
        existing_keywords = existing_keywords or []
        local_match = self._try_local_match(task)
        if local_match is not None:
            return local_match
        _set_task_status(task, "剪映启动中")
        _set_task_status(task, "剪映搜索中")
        try:
            candidates = (
                candidates
                if candidates is not None
                else self.client.search(task.jianying_keywords)
            )
        except RuntimeError as exc:
            _set_task_status(task, "需要人工确认")
            task.notes = str(exc)
            return MaterialProcessResult(
                task_id=task.shot_id,
                status=task.status,
                failure_reason=str(exc),
            )
        if candidates:
            _set_task_status(task, "已找到候选")
        _set_task_status(task, "候选筛选中")
        scored = [
            (candidate, score_candidate(task, candidate, existing_keywords))
            for candidate in candidates[: self.max_candidates_per_search]
        ]
        passed = [
            (candidate, score)
            for candidate, score in scored
            if is_download_allowed(score, self.min_material_score)
        ]
        rejected = [
            RejectedCandidate(candidate.material_id, score.reason, score.total_score)
            for candidate, score in scored
            if not is_download_allowed(score, self.min_material_score)
        ]

        if not passed:
            _set_task_status(task, "筛选不通过")
            _set_task_status(task, "需要人工确认")
            return MaterialProcessResult(
                task_id=task.shot_id,
                status=task.status,
                rejected_candidates=rejected,
            )

        selected_candidate, selected_score = sorted(
            passed,
            key=lambda pair: pair[1].total_score,
            reverse=True,
        )[0]
        _set_task_status(task, "筛选通过")
        _set_task_status(task, "下载中")
        download = self.client.download(selected_candidate, self.paths.jianying_download_dir)
        if not download.success or download.downloaded_path is None:
            _set_task_status(task, "下载失败")
            task.notes = download.error_message
            return MaterialProcessResult(
                task_id=task.shot_id,
                status=task.status,
                selected_candidate_id=selected_candidate.material_id,
                selected_score=selected_score.total_score,
                failure_reason=download.error_message,
                rejected_candidates=rejected,
            )

        target = self._move_downloaded_file(task, selected_candidate, Path(download.downloaded_path))
        _set_task_status(task, "已下载")
        task.material_path = str(target)
        task.source_type = "Pexels素材"
        task.match_score = selected_score.total_score
        task.evidence_kind = "atmosphere"
        task.source_url = ""
        task.is_locally_generated = False
        return MaterialProcessResult(
            task_id=task.shot_id,
            status=task.status,
            selected_material_path=str(target),
            selected_candidate_id=selected_candidate.material_id,
            selected_score=selected_score.total_score,
            rejected_candidates=rejected,
        )

    def _try_local_match(self, task: MaterialTask) -> MaterialProcessResult | None:
        if self.local_library is None or not task.needs_local_library:
            return None
        match = self.local_library.find_best_match(task)
        if match is None:
            return None
        material, score = match
        if score < 70:
            return None
        _set_task_status(task, "本地已匹配")
        task.material_path = material.path
        task.source_type = "备选素材"
        task.match_score = score
        task.evidence_kind = "atmosphere"
        task.source_url = ""
        task.is_locally_generated = False
        self.local_library.record_use(
            material,
            self.paths.project_name,
            datetime.now(timezone.utc).isoformat(),
        )
        return MaterialProcessResult(
            task_id=task.shot_id,
            status=task.status,
            selected_material_path=material.path,
            selected_candidate_id=material.name,
            selected_score=score,
        )

    def _move_downloaded_file(
        self,
        task: MaterialTask,
        candidate: CandidateMaterial,
        source_path: Path,
    ) -> Path:
        self.paths.ensure_directories()
        source_dir = self.paths.pexels_material_dir
        all_dir = self.paths.all_materials_dir
        source_dir.mkdir(parents=True, exist_ok=True)
        all_dir.mkdir(parents=True, exist_ok=True)
        suffix = source_path.suffix or ".mp4"
        keyword = _safe_name((task.jianying_keywords or candidate.keywords or ["素材"])[0])
        filename = f"{_time_prefix(task.start)}_素材_{keyword}{suffix}"
        source_target = _unique_path(source_dir / filename)
        if source_path.resolve() != source_target.resolve():
            shutil.move(str(source_path), str(source_target))
        all_target = _unique_path(all_dir / filename)
        if source_target.resolve() != all_target.resolve():
            shutil.copy2(str(source_target), str(all_target))
        return all_target


def _time_prefix(value: str) -> str:
    return value.replace(":", "-")


def _safe_name(value: str) -> str:
    unsafe = '<>:"/\\|?*'
    name = "".join("_" if char in unsafe else char for char in value)
    return name.strip()[:30] or "素材"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法生成不重复素材文件名：{path}")


def _set_task_status(task: MaterialTask, status: str) -> None:
    task.status = status
    if not task.status_history or task.status_history[-1] != status:
        task.status_history.append(status)
