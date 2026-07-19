from __future__ import annotations

from pathlib import Path

from app.core.config import Settings, get_settings, resolve_configured_path
from app.materials.adapters.analysis import ConservativeScreenshotAnalyzer
from app.materials.adapters.browser import BrowserJianyingAdapter
from app.materials.adapters.config import (
    JianyingAdapterConfig,
    load_desktop_ui_config,
    load_desktop_ui_profile,
)
from app.materials.adapters.desktop import DesktopJianyingAdapter, PyAutoGuiDesktopBackend
from app.materials.adapters.fake import FakeJianyingAdapter


def adapter_config_from_settings(
    settings: Settings | None = None,
    *,
    require_profile: bool = True,
) -> JianyingAdapterConfig:
    settings = settings or get_settings()
    ui = (
        load_desktop_ui_profile(
            resolve_configured_path(settings.jianying_desktop_ui_config_path)
        )
        if require_profile and settings.jianying_adapter_type == "desktop"
        else load_desktop_ui_config(
            resolve_configured_path(settings.jianying_desktop_ui_config_path)
        )
    )
    return JianyingAdapterConfig(
        adapter_type=settings.jianying_adapter_type,
        desktop_app_path=settings.jianying_desktop_app_path,
        download_dir=settings.jianying_download_dir,
        material_root_dir=settings.material_root_dir,
        project_root_dir=settings.project_root_dir,
        max_candidates_per_search=settings.max_candidates_per_search,
        min_material_score=settings.min_material_score,
        max_downloads_per_task=settings.max_downloads_per_task,
        candidate_screenshot_dir=settings.candidate_screenshot_dir,
        launch_timeout_seconds=settings.jianying_launch_timeout_seconds,
        download_timeout_seconds=settings.jianying_download_timeout_seconds,
        ui=ui,
    )


def build_jianying_adapter(
    config: JianyingAdapterConfig | None = None,
    *,
    project_name: str = "未命名项目",
):
    config = config or adapter_config_from_settings()
    adapter_type = (config.adapter_type or "desktop").strip().lower()
    if adapter_type == "fake":
        return FakeJianyingAdapter()
    if adapter_type == "browser":
        return BrowserJianyingAdapter()
    if adapter_type != "desktop":
        raise ValueError(f"未知剪映适配器类型：{adapter_type}")

    screenshot_dir = (
        config.project_root_dir
        / project_name
        / config.candidate_screenshot_dir
    )
    analyzer = ConservativeScreenshotAnalyzer(
        screenshot_dir / "识别缓存.json"
    )
    return DesktopJianyingAdapter(
        config=config,
        backend=PyAutoGuiDesktopBackend(),
        analyzer=analyzer,
        project_name=project_name,
    )
