from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.materials.adapters.errors import DesktopAutomationError


@dataclass(frozen=True, slots=True)
class DesktopPoint:
    x: int
    y: int


@dataclass(frozen=True, slots=True)
class ScreenRegion:
    x: int
    y: int
    width: int
    height: int


@dataclass(slots=True)
class DesktopUIConfig:
    window_title: str = "剪映专业版"
    search_box: DesktopPoint | None = None
    search_results_region: ScreenRegion | None = None
    candidate_regions: list[ScreenRegion] = field(default_factory=list)
    candidate_click_points: list[DesktopPoint] = field(default_factory=list)
    candidate_download_regions: list[ScreenRegion] = field(default_factory=list)
    download_button_template: Path | None = None
    download_button_point: DesktopPoint | None = None
    download_status_region: ScreenRegion | None = None
    screenshot_region: ScreenRegion | None = None
    calibrated_at: str = ""
    screen_width: int = 0
    screen_height: int = 0
    scale_ratio: float = 1.0
    image_confidence: float = 0.85
    search_wait_seconds: float = 3.0
    detail_wait_seconds: float = 1.0


@dataclass(slots=True)
class JianyingAdapterConfig:
    adapter_type: str = "desktop"
    desktop_app_path: Path = Path("")
    download_dir: Path = Path("D:/短视频工厂/素材库/剪映下载")
    material_root_dir: Path = Path("D:/短视频工厂")
    project_root_dir: Path = Path("D:/短视频工厂/项目")
    max_candidates_per_search: int = 5
    min_material_score: float = 60.0
    max_downloads_per_task: int = 1
    candidate_screenshot_dir: Path = Path("素材信息/候选截图")
    launch_timeout_seconds: float = 30.0
    download_timeout_seconds: float = 120.0
    ui: DesktopUIConfig = field(default_factory=DesktopUIConfig)


def load_desktop_ui_config(path: Path) -> DesktopUIConfig:
    if not path.exists():
        return DesktopUIConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _profile_from_raw(raw)


def load_desktop_ui_profile(path: Path) -> DesktopUIConfig:
    if not path.exists():
        raise DesktopAutomationError(
            f"未找到剪映界面校准文件：{path}。请先运行剪映界面校准工具。"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise DesktopAutomationError(f"读取剪映界面校准文件失败：{exc}") from exc
    profile = _profile_from_raw(raw)
    _validate_profile(profile)
    return profile


def save_desktop_ui_profile(path: Path, profile: DesktopUIConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "window_title": profile.window_title,
        "search_box": _point_dict(profile.search_box),
        "search_results_region": _region_dict(profile.search_results_region),
        "candidate_regions": [_region_dict(item) for item in profile.candidate_regions],
        "candidate_click_points": [
            _point_dict(item) for item in profile.candidate_click_points
        ],
        "candidate_download_regions": [
            _region_dict(item) for item in profile.candidate_download_regions
        ],
        "download_button_template": (
            str(profile.download_button_template)
            if profile.download_button_template is not None
            else None
        ),
        "download_button_point": _point_dict(profile.download_button_point),
        "download_status_region": _region_dict(profile.download_status_region),
        "screenshot_region": _region_dict(profile.screenshot_region),
        "last_calibrated_at": profile.calibrated_at,
        "screen_resolution": {
            "width": profile.screen_width,
            "height": profile.screen_height,
        },
        "scale_ratio": profile.scale_ratio,
        "image_confidence": profile.image_confidence,
        "search_wait_seconds": profile.search_wait_seconds,
        "detail_wait_seconds": profile.detail_wait_seconds,
    }
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _profile_from_raw(raw: dict[str, Any]) -> DesktopUIConfig:
    resolution = raw.get("screen_resolution") or {}
    return DesktopUIConfig(
        window_title=str(raw.get("window_title", "剪映专业版")),
        search_box=_point(raw.get("search_box")),
        search_results_region=_region(raw.get("search_results_region")),
        candidate_regions=[
            region
            for item in raw.get("candidate_regions", [])
            if (region := _region(item)) is not None
        ],
        candidate_click_points=[
            point
            for item in raw.get("candidate_click_points", [])
            if (point := _point(item)) is not None
        ],
        candidate_download_regions=[
            region
            for item in raw.get("candidate_download_regions", [])
            if (region := _region(item)) is not None
        ],
        download_button_template=_optional_path(raw.get("download_button_template")),
        download_button_point=_point(raw.get("download_button_point")),
        download_status_region=_region(raw.get("download_status_region")),
        screenshot_region=_region(raw.get("screenshot_region")),
        calibrated_at=str(raw.get("last_calibrated_at", "")),
        screen_width=int(resolution.get("width", 0) or 0),
        screen_height=int(resolution.get("height", 0) or 0),
        scale_ratio=float(raw.get("scale_ratio", 1.0) or 1.0),
        image_confidence=float(raw.get("image_confidence", 0.85)),
        search_wait_seconds=float(raw.get("search_wait_seconds", 3.0)),
        detail_wait_seconds=float(raw.get("detail_wait_seconds", 1.0)),
    )


def _validate_profile(profile: DesktopUIConfig) -> None:
    missing: list[str] = []
    if profile.search_box is None:
        missing.append("搜索框")
    if profile.search_results_region is None:
        missing.append("搜索结果区域")
    if not profile.candidate_regions:
        missing.append("候选素材卡片区域")
    if len(profile.candidate_download_regions) < len(profile.candidate_regions):
        missing.append("每个候选素材的下载按钮区域")
    if profile.screenshot_region is None:
        missing.append("截图区域")
    if missing:
        raise DesktopAutomationError(
            "剪映界面校准坐标不完整：" + "、".join(missing) + "。请重新运行校准工具。"
        )


def _point(value: Any) -> DesktopPoint | None:
    if not isinstance(value, dict) or "x" not in value or "y" not in value:
        return None
    return DesktopPoint(x=int(value["x"]), y=int(value["y"]))


def _region(value: Any) -> ScreenRegion | None:
    required = {"x", "y", "width", "height"}
    if not isinstance(value, dict) or not required.issubset(value):
        return None
    return ScreenRegion(
        x=int(value["x"]),
        y=int(value["y"]),
        width=int(value["width"]),
        height=int(value["height"]),
    )


def _optional_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    return Path(text) if text else None


def _point_dict(value: DesktopPoint | None) -> dict[str, int] | None:
    if value is None:
        return None
    return {"x": value.x, "y": value.y}


def _region_dict(value: ScreenRegion | None) -> dict[str, int] | None:
    if value is None:
        return None
    return {
        "x": value.x,
        "y": value.y,
        "width": value.width,
        "height": value.height,
    }
