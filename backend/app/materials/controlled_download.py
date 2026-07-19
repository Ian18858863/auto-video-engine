from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.materials.assistant import JianyingAutomationClient
from app.materials.paths import ProjectMaterialPaths
from app.materials.planner import MaterialTask
from app.materials.scoring import (
    CandidateMaterial,
    is_download_allowed,
    score_candidate,
)


@dataclass(frozen=True, slots=True)
class CandidateReview:
    license_type: str
    quality_ok: bool
    risk_found: bool
    orientation: str = "unknown"
    subject_centered: bool = False
    key_info_safe: bool = False
    background_suitable: bool = False
    overlay_safe: bool = False


class CandidateReviewer(Protocol):
    def review(self, candidate: CandidateMaterial) -> CandidateReview: ...


@dataclass(slots=True)
class ControlledCandidateResult:
    material_id: str
    screenshot_path: str
    total_score: float
    portrait_fit_score: float
    license_type: str
    allowed: bool
    rejection_reason: str = ""


@dataclass(slots=True)
class ControlledDownloadResult:
    status: str
    keyword: str
    candidates: list[ControlledCandidateResult] = field(default_factory=list)
    downloaded_path: str = ""
    failure_reason: str = ""
    report_path: str = ""


class ControlledDownloadRunner:
    def __init__(
        self,
        adapter: JianyingAutomationClient,
        paths: ProjectMaterialPaths,
        reviewer: CandidateReviewer,
        min_material_score: float = 60.0,
    ):
        self.adapter = adapter
        self.paths = paths
        self.reviewer = reviewer
        self.min_material_score = min_material_score

    def run(
        self,
        *,
        keyword: str,
        max_candidates: int = 3,
        max_downloads: int = 1,
        confirm_download: bool = False,
    ) -> ControlledDownloadResult:
        keyword = keyword.strip()
        if not keyword:
            raise ValueError("搜索关键词不能为空。")
        if max_downloads != 1:
            raise ValueError("受控测试每次只允许下载 1 个素材。")
        candidate_limit = min(max(int(max_candidates), 1), 3)

        self.paths.ensure_directories()
        candidates = self.adapter.search([keyword])[:candidate_limit]
        task = _build_test_task(keyword)
        records: list[ControlledCandidateResult] = []
        passed: list[tuple[CandidateMaterial, float]] = []

        for candidate in candidates:
            reviewed = _apply_review(candidate, self.reviewer.review(candidate))
            score = score_candidate(task, reviewed, existing_keywords=[])
            allowed = is_download_allowed(score, self.min_material_score)
            reason = "" if allowed else score.reason
            records.append(
                ControlledCandidateResult(
                    material_id=reviewed.material_id,
                    screenshot_path=reviewed.local_path,
                    total_score=score.total_score,
                    portrait_fit_score=score.portrait_fit_score,
                    license_type=reviewed.license_type,
                    allowed=allowed,
                    rejection_reason=reason,
                )
            )
            if allowed:
                passed.append((reviewed, score.total_score))

        if not passed:
            result = ControlledDownloadResult(
                status="需要人工确认",
                keyword=keyword,
                candidates=records,
                failure_reason="没有通过授权、画质、风险和评分检查的候选素材。",
            )
            return self._save_report(result)

        if not confirm_download:
            result = ControlledDownloadResult(
                status="预演完成",
                keyword=keyword,
                candidates=records,
                failure_reason="未提供明确下载确认，本次没有点击下载。",
            )
            return self._save_report(result)

        selected, _score = max(passed, key=lambda item: item[1])
        download = self.adapter.download(selected, self.paths.jianying_download_dir)
        if not download.success or download.downloaded_path is None:
            result = ControlledDownloadResult(
                status="下载失败",
                keyword=keyword,
                candidates=records,
                failure_reason=download.error_message or "剪映下载失败。",
            )
            return self._save_report(result)

        result = ControlledDownloadResult(
            status="已下载",
            keyword=keyword,
            candidates=records,
            downloaded_path=str(download.downloaded_path),
        )
        return self._save_report(result)

    def _save_report(
        self,
        result: ControlledDownloadResult,
    ) -> ControlledDownloadResult:
        report_dir = self.paths.project_dir / "素材信息"
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = report_dir / f"剪映受控下载测试_{stamp}.json"
        result.report_path = str(report_path)
        report_path.write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result


def _apply_review(
    candidate: CandidateMaterial,
    review: CandidateReview,
) -> CandidateMaterial:
    license_type = (
        review.license_type if review.license_type in {"free", "member"} else "unknown"
    )
    quality_flags = list(candidate.quality_flags)
    if review.quality_ok:
        for flag in ("clean", "sharp", "stable"):
            if flag not in quality_flags:
                quality_flags.append(flag)
    else:
        quality_flags.append("low_quality")
    risk_flags = list(candidate.risk_flags)
    if review.risk_found:
        risk_flags.append("人工确认存在风险")
    return replace(
        candidate,
        license_type=license_type,
        quality_flags=quality_flags,
        risk_flags=risk_flags,
        orientation=review.orientation,
        subject_centered=review.subject_centered,
        key_info_safe=review.key_info_safe,
        background_suitable=review.background_suitable,
        overlay_safe=review.overlay_safe,
    )


def _build_test_task(keyword: str) -> MaterialTask:
    return MaterialTask(
        shot_id="jianying-smoke-test",
        start="00:00",
        end="00:05",
        voiceover=f"受控测试关键词：{keyword}",
        recommended_visual=keyword,
        material_type="剪映素材",
        source_priority=["剪映素材"],
        jianying_keywords=[keyword],
        needs_local_library=False,
        needs_jianying=True,
        needs_codex_graphic=False,
        needs_codex_chart=False,
        needs_codex_info_card=False,
        can_use_ai_motion=False,
    )
