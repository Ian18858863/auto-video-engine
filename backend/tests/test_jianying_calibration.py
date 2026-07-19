from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from app.materials.adapters.config import (
    DesktopPoint,
    DesktopUIConfig,
    ScreenRegion,
    load_desktop_ui_profile,
    save_desktop_ui_profile,
)
from app.materials.adapters.desktop import DesktopAutomationError
from app.materials.controlled_download import (
    CandidateReview,
    ControlledDownloadRunner,
)
from app.materials.paths import ProjectMaterialPaths
from app.materials.scoring import CandidateMaterial


class FakeControlledAdapter:
    def __init__(self, candidates: list[CandidateMaterial]):
        self.candidates = candidates
        self.search_calls: list[list[str]] = []
        self.download_calls: list[str] = []

    def search(self, keywords: list[str]) -> list[CandidateMaterial]:
        self.search_calls.append(keywords)
        return self.candidates

    def download(self, candidate: CandidateMaterial, download_dir: Path):
        from app.materials.assistant import DownloadResult

        self.download_calls.append(candidate.material_id)
        download_dir.mkdir(parents=True, exist_ok=True)
        path = download_dir / f"{candidate.material_id}.mp4"
        path.write_bytes(b"video")
        return DownloadResult(success=True, downloaded_path=path)


class FakeReviewer:
    def __init__(self, reviews: dict[str, CandidateReview]):
        self.reviews = reviews

    def review(self, candidate: CandidateMaterial) -> CandidateReview:
        return self.reviews.get(
            candidate.material_id,
            CandidateReview(license_type="unknown", quality_ok=False, risk_found=False),
        )


def complete_profile() -> DesktopUIConfig:
    return DesktopUIConfig(
        window_title="剪映专业版",
        search_box=DesktopPoint(300, 120),
        search_results_region=ScreenRegion(100, 180, 900, 500),
        candidate_regions=[
            ScreenRegion(120, 220, 240, 160),
            ScreenRegion(380, 220, 240, 160),
            ScreenRegion(640, 220, 240, 160),
        ],
        candidate_click_points=[
            DesktopPoint(240, 300),
            DesktopPoint(500, 300),
            DesktopPoint(760, 300),
        ],
        candidate_download_regions=[
            ScreenRegion(310, 340, 30, 30),
            ScreenRegion(570, 340, 30, 30),
            ScreenRegion(830, 340, 30, 30),
        ],
        download_status_region=ScreenRegion(850, 700, 220, 80),
        screenshot_region=ScreenRegion(0, 0, 1920, 1080),
        calibrated_at="2026-06-15T10:00:00+08:00",
        screen_width=1920,
        screen_height=1080,
        scale_ratio=1.25,
    )


def test_ui_profile_round_trip_preserves_calibration_metadata(tmp_path):
    profile_path = tmp_path / "jianying_ui_profile.yaml"
    expected = complete_profile()

    save_desktop_ui_profile(profile_path, expected)
    loaded = load_desktop_ui_profile(profile_path)

    assert loaded == expected


def test_missing_ui_profile_requires_calibration(tmp_path):
    with pytest.raises(DesktopAutomationError, match="请先运行剪映界面校准工具"):
        load_desktop_ui_profile(tmp_path / "missing.yaml")


def test_incomplete_ui_profile_reports_missing_coordinates(tmp_path):
    profile_path = tmp_path / "jianying_ui_profile.yaml"
    incomplete = replace(complete_profile(), search_box=None)
    save_desktop_ui_profile(profile_path, incomplete)

    with pytest.raises(DesktopAutomationError, match="搜索框"):
        load_desktop_ui_profile(profile_path)


def test_download_status_region_is_optional_because_file_watcher_is_authoritative(
    tmp_path,
):
    profile_path = tmp_path / "jianying_ui_profile.yaml"
    profile = replace(complete_profile(), download_status_region=None)
    save_desktop_ui_profile(profile_path, profile)

    loaded = load_desktop_ui_profile(profile_path)

    assert loaded.download_status_region is None


def candidate(index: int) -> CandidateMaterial:
    return CandidateMaterial(
        material_id=f"candidate-{index}",
        title=f"财经新闻 商业办公候选 {index}",
        description="清楚稳定的财经科技素材",
        keywords=["财经新闻", "商业", "办公"],
        source="剪映素材",
        license_type="unknown",
        quality_flags=[],
        local_path=f"D:/候选_{index}.png",
    )


def test_controlled_download_rejects_unknown_license_and_does_not_download(tmp_path):
    adapter = FakeControlledAdapter([candidate(index) for index in range(1, 5)])
    runner = ControlledDownloadRunner(
        adapter=adapter,
        paths=ProjectMaterialPaths(tmp_path / "短视频工厂", "受控测试"),
        reviewer=FakeReviewer({}),
        min_material_score=60,
    )

    result = runner.run(keyword="财经新闻", confirm_download=True)

    assert result.status == "需要人工确认"
    assert len(result.candidates) == 3
    assert adapter.download_calls == []
    assert all(item.rejection_reason for item in result.candidates)


def test_controlled_download_processes_three_candidates_and_downloads_only_one(tmp_path):
    candidates = [candidate(index) for index in range(1, 5)]
    adapter = FakeControlledAdapter(candidates)
    reviewer = FakeReviewer(
        {
            item.material_id: CandidateReview(
                license_type="member",
                quality_ok=True,
                risk_found=False,
                orientation="portrait",
            )
            for item in candidates
        }
    )
    runner = ControlledDownloadRunner(
        adapter=adapter,
        paths=ProjectMaterialPaths(tmp_path / "短视频工厂", "受控测试"),
        reviewer=reviewer,
        min_material_score=60,
    )

    result = runner.run(
        keyword="财经新闻",
        max_candidates=3,
        max_downloads=1,
        confirm_download=True,
    )

    assert result.status == "已下载"
    assert len(result.candidates) == 3
    assert len(adapter.download_calls) == 1
    assert result.downloaded_path


def test_controlled_download_without_explicit_confirmation_is_preview_only(tmp_path):
    item = candidate(1)
    adapter = FakeControlledAdapter([item])
    runner = ControlledDownloadRunner(
        adapter=adapter,
        paths=ProjectMaterialPaths(tmp_path / "短视频工厂", "受控测试"),
        reviewer=FakeReviewer(
            {
                item.material_id: CandidateReview(
                    license_type="free",
                    quality_ok=True,
                    risk_found=False,
                    orientation="portrait",
                )
            }
        ),
        min_material_score=60,
    )

    result = runner.run(keyword="财经新闻", confirm_download=False)

    assert result.status == "预演完成"
    assert adapter.download_calls == []


def test_controlled_download_prefers_portrait_over_crop_safe_landscape(tmp_path):
    portrait = candidate(1)
    landscape = candidate(2)
    adapter = FakeControlledAdapter([landscape, portrait])
    runner = ControlledDownloadRunner(
        adapter=adapter,
        paths=ProjectMaterialPaths(tmp_path / "短视频工厂", "受控测试"),
        reviewer=FakeReviewer(
            {
                portrait.material_id: CandidateReview(
                    license_type="member",
                    quality_ok=True,
                    risk_found=False,
                    orientation="portrait",
                ),
                landscape.material_id: CandidateReview(
                    license_type="member",
                    quality_ok=True,
                    risk_found=False,
                    orientation="landscape",
                    subject_centered=True,
                    key_info_safe=True,
                    background_suitable=True,
                    overlay_safe=True,
                ),
            }
        ),
        min_material_score=60,
    )

    result = runner.run(keyword="财经新闻", confirm_download=True)

    assert result.status == "已下载"
    assert adapter.download_calls == [portrait.material_id]
    scores = {item.material_id: item.portrait_fit_score for item in result.candidates}
    assert scores[portrait.material_id] > scores[landscape.material_id]


def test_controlled_download_rejects_landscape_that_cannot_be_safely_cropped(
    tmp_path,
):
    landscape = candidate(1)
    adapter = FakeControlledAdapter([landscape])
    runner = ControlledDownloadRunner(
        adapter=adapter,
        paths=ProjectMaterialPaths(tmp_path / "短视频工厂", "受控测试"),
        reviewer=FakeReviewer(
            {
                landscape.material_id: CandidateReview(
                    license_type="free",
                    quality_ok=True,
                    risk_found=False,
                    orientation="landscape",
                    subject_centered=False,
                    key_info_safe=False,
                    background_suitable=False,
                    overlay_safe=False,
                )
            }
        ),
        min_material_score=60,
    )

    result = runner.run(keyword="财经新闻", confirm_download=True)

    assert result.status == "需要人工确认"
    assert adapter.download_calls == []
    assert result.candidates[0].portrait_fit_score == 0
    assert "竖屏" in result.candidates[0].rejection_reason


@pytest.mark.parametrize(
    "module_name",
    [
        "app.materials.calibrate_jianying",
        "app.materials.test_jianying_download",
    ],
)
def test_jianying_commands_can_show_help_from_project_root(module_name):
    result = subprocess.run(
        [sys.executable, "-m", module_name, "--help"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "剪映" in result.stdout
