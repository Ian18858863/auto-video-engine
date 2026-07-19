from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.materials.adapters.config import (
    DesktopPoint,
    DesktopUIConfig,
    JianyingAdapterConfig,
    ScreenRegion,
)
from app.materials.adapters.desktop import (
    DesktopAutomationError,
    DesktopJianyingAdapter,
    DownloadDirectoryWatcher,
)
from app.materials.adapters.factory import build_jianying_adapter
from app.materials.adapters.fake import FakeJianyingAdapter
from app.materials.scoring import CandidateMaterial
from app.materials.paths import ProjectMaterialPaths


@pytest.fixture
def allow_test_storage_path(monkeypatch):
    monkeypatch.setattr(
        "app.materials.adapters.desktop._validate_storage_path",
        lambda path, label: None,
    )
from app.materials.planner import MaterialTask
from app.materials.assistant import JianyingMaterialAssistant


class FakeDesktopBackend:
    def __init__(
        self,
        *,
        window_exists: bool = False,
        launch_success: bool = True,
        wait_success: bool = True,
        located_download_button: DesktopPoint | None = None,
    ):
        self.window_exists = window_exists
        self.launch_success = launch_success
        self.wait_success = wait_success
        self.located_download_button = located_download_button
        self.actions: list[tuple] = []

    def find_window(self, title: str) -> bool:
        self.actions.append(("find_window", title))
        return self.window_exists

    def focus_window(self, title: str) -> bool:
        self.actions.append(("focus_window", title))
        return self.window_exists

    def launch(self, executable: Path, arguments: list[str]) -> bool:
        self.actions.append(("launch", executable, arguments))
        return self.launch_success

    def wait_for_window(self, title: str, timeout_seconds: float) -> bool:
        self.actions.append(("wait_for_window", title, timeout_seconds))
        return self.wait_success

    def click(self, point: DesktopPoint) -> None:
        self.actions.append(("click", point))

    def paste_text(self, text: str) -> None:
        self.actions.append(("paste_text", text))

    def press(self, key: str) -> None:
        self.actions.append(("press", key))

    def screenshot(self, region: ScreenRegion, destination: Path) -> None:
        self.actions.append(("screenshot", region, destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake-png")

    def locate_on_screen(
        self,
        template_path: Path,
        confidence: float,
    ) -> DesktopPoint | None:
        self.actions.append(("locate_on_screen", template_path, confidence))
        return self.located_download_button

    def sleep(self, seconds: float) -> None:
        self.actions.append(("sleep", seconds))


class FakeCandidateAnalyzer:
    def analyze(
        self,
        screenshot_path: Path,
        keyword: str,
        candidate_index: int,
    ) -> CandidateMaterial:
        return CandidateMaterial(
            material_id=f"{keyword}-{candidate_index}",
            title=f"{keyword} 科技办公候选 {candidate_index}",
            description="干净稳定的商业科技画面",
            keywords=[keyword, "科技", "办公", "商业"],
            source="剪映素材",
            license_type="member",
            quality_flags=["clean", "sharp", "stable"],
            local_path=str(screenshot_path),
            orientation="portrait",
        )


def make_config(tmp_path: Path, executable: Path) -> JianyingAdapterConfig:
    return JianyingAdapterConfig(
        adapter_type="desktop",
        desktop_app_path=executable,
        download_dir=tmp_path / "剪映下载",
        material_root_dir=tmp_path / "短视频工厂",
        project_root_dir=tmp_path / "短视频工厂" / "项目",
        max_candidates_per_search=3,
        min_material_score=60,
        max_downloads_per_task=1,
        candidate_screenshot_dir=Path("素材信息/候选截图"),
        ui=DesktopUIConfig(
            window_title="剪映专业版",
            search_box=DesktopPoint(320, 120),
            candidate_regions=[
                ScreenRegion(100, 200, 240, 150),
                ScreenRegion(360, 200, 240, 150),
                ScreenRegion(620, 200, 240, 150),
            ],
            candidate_click_points=[
                DesktopPoint(220, 275),
                DesktopPoint(480, 275),
                DesktopPoint(740, 275),
            ],
            download_button_point=DesktopPoint(980, 720),
        ),
    )


def test_settings_default_to_desktop_adapter(monkeypatch):
    for variable in (
        "JIANYING_ADAPTER_TYPE",
        "JIANYING_DESKTOP_APP_PATH",
        "EXPORT_DIR",
        "MATERIAL_ROOT_DIR",
        "PROJECT_ROOT_DIR",
        "AUTO_MATERIAL_ROOT_DIR",
        "JIANYING_DOWNLOAD_DIR",
        "JIANYING_DESKTOP_UI_CONFIG_PATH",
        "MAX_CANDIDATES_PER_SEARCH",
        "MIN_MATERIAL_SCORE",
    ):
        monkeypatch.delenv(variable, raising=False)
    settings = Settings(_env_file=None)

    assert settings.jianying_adapter_type == "desktop"
    assert settings.jianying_desktop_app_path == Path("")
    assert settings.export_dir == Path("D:/短视频工厂/exports")
    assert settings.material_root_dir == Path("D:/短视频工厂")
    assert settings.project_root_dir == Path("D:/短视频工厂/项目")
    assert settings.jianying_download_dir == Path("D:/短视频工厂/备选素材/旧剪映下载")
    assert settings.jianying_desktop_ui_config_path == Path(
        "./config/jianying_ui_profile.yaml"
    )
    assert settings.max_candidates_per_search == 5
    assert settings.min_material_score == 60


def test_factory_keeps_fake_adapter_for_tests(tmp_path):
    config = JianyingAdapterConfig(
        adapter_type="fake",
        desktop_app_path=tmp_path / "missing.exe",
        download_dir=tmp_path / "剪映下载",
        material_root_dir=tmp_path / "短视频工厂",
        project_root_dir=tmp_path / "短视频工厂" / "项目",
    )

    adapter = build_jianying_adapter(config)

    assert isinstance(adapter, FakeJianyingAdapter)


def test_missing_desktop_app_path_returns_clear_error(tmp_path):
    config = make_config(tmp_path, tmp_path / "missing.exe")
    adapter = DesktopJianyingAdapter(
        config=config,
        backend=FakeDesktopBackend(),
        analyzer=FakeCandidateAnalyzer(),
        project_name="测试项目",
    )

    with pytest.raises(DesktopAutomationError, match="找不到剪映专业版"):
        adapter.ensure_started()


def test_launch_failure_returns_clear_error_without_clicking(tmp_path):
    executable = tmp_path / "JianyingPro.exe"
    executable.write_bytes(b"fake")
    backend = FakeDesktopBackend(launch_success=False)
    adapter = DesktopJianyingAdapter(
        config=make_config(tmp_path, executable),
        backend=backend,
        analyzer=FakeCandidateAnalyzer(),
        project_name="测试项目",
    )

    with pytest.raises(DesktopAutomationError, match="启动剪映专业版失败"):
        adapter.ensure_started()

    assert not any(action[0] == "click" for action in backend.actions)


def test_missing_search_box_stops_without_unknown_click(tmp_path, allow_test_storage_path):
    executable = tmp_path / "JianyingPro.exe"
    executable.write_bytes(b"fake")
    config = make_config(tmp_path, executable)
    config.ui.search_box = None
    backend = FakeDesktopBackend(window_exists=True)
    adapter = DesktopJianyingAdapter(
        config=config,
        backend=backend,
        analyzer=FakeCandidateAnalyzer(),
        project_name="测试项目",
    )

    with pytest.raises(DesktopAutomationError, match="搜索框"):
        adapter.search(["AI工具"])

    assert not any(action[0] == "click" for action in backend.actions)


def test_desktop_adapter_refuses_c_drive_storage_without_clicking(tmp_path):
    executable = tmp_path / "JianyingPro.exe"
    executable.write_bytes(b"fake")
    config = make_config(tmp_path, executable)
    config.project_root_dir = Path("C:/短视频工厂/项目")
    backend = FakeDesktopBackend(window_exists=True)
    adapter = DesktopJianyingAdapter(
        config=config,
        backend=backend,
        analyzer=FakeCandidateAnalyzer(),
        project_name="测试项目",
    )

    with pytest.raises(DesktopAutomationError, match="不会写入 C 盘"):
        adapter.search(["AI工具"])

    assert not any(action[0] == "click" for action in backend.actions)


def test_desktop_search_screenshots_first_candidates_to_chinese_directory(
    tmp_path,
    allow_test_storage_path,
):
    executable = tmp_path / "JianyingPro.exe"
    executable.write_bytes(b"fake")
    backend = FakeDesktopBackend(window_exists=True)
    adapter = DesktopJianyingAdapter(
        config=make_config(tmp_path, executable),
        backend=backend,
        analyzer=FakeCandidateAnalyzer(),
        project_name="测试项目",
    )

    candidates = adapter.search(["AI工具"])

    assert len(candidates) == 3
    assert any(action[:2] == ("paste_text", "AI工具") for action in backend.actions)
    screenshots = [action[2] for action in backend.actions if action[0] == "screenshot"]
    assert len(screenshots) == 3
    assert all("素材信息" in str(path) and "候选截图" in str(path) for path in screenshots)
    assert all(path.exists() for path in screenshots)


def test_download_button_not_recognized_returns_error_without_unknown_click(
    tmp_path,
    allow_test_storage_path,
):
    executable = tmp_path / "JianyingPro.exe"
    executable.write_bytes(b"fake")
    config = make_config(tmp_path, executable)
    config.ui.download_button_point = None
    config.ui.download_button_template = tmp_path / "download-button.png"
    config.ui.download_button_template.write_bytes(b"fake")
    backend = FakeDesktopBackend(window_exists=True, located_download_button=None)
    adapter = DesktopJianyingAdapter(
        config=config,
        backend=backend,
        analyzer=FakeCandidateAnalyzer(),
        project_name="测试项目",
    )
    candidate = adapter.search(["AI工具"])[0]
    click_count_before = sum(action[0] == "click" for action in backend.actions)

    result = adapter.download(candidate, config.download_dir)

    assert result.success is False
    assert "下载按钮" in result.error_message
    assert sum(action[0] == "click" for action in backend.actions) == click_count_before + 1


def test_download_watcher_detects_new_completed_file(tmp_path):
    download_dir = tmp_path / "剪映下载"
    download_dir.mkdir()
    watcher = DownloadDirectoryWatcher(
        download_dir=download_dir,
        timeout_seconds=1,
        poll_interval_seconds=0.01,
        stable_checks=2,
    )
    before = watcher.snapshot()
    completed = download_dir / "素材.mp4"
    completed.write_bytes(b"complete")

    detected = watcher.wait_for_new_file(before)

    assert detected == completed


def test_download_watcher_ignores_partial_files(tmp_path):
    download_dir = tmp_path / "剪映下载"
    download_dir.mkdir()
    watcher = DownloadDirectoryWatcher(
        download_dir=download_dir,
        timeout_seconds=0.05,
        poll_interval_seconds=0.01,
        stable_checks=2,
    )
    before = watcher.snapshot()
    (download_dir / "素材.mp4.tmp").write_bytes(b"partial")

    with pytest.raises(DesktopAutomationError, match="下载完成"):
        watcher.wait_for_new_file(before)


def test_material_assistant_records_desktop_status_transitions(tmp_path):
    executable = tmp_path / "JianyingPro.exe"
    executable.write_bytes(b"fake")
    config = make_config(tmp_path, executable)
    adapter = FakeJianyingAdapter()
    paths = ProjectMaterialPaths(config.material_root_dir, "测试项目")
    assistant = JianyingMaterialAssistant(paths=paths, client=adapter)
    task = MaterialTask(
        shot_id="shot-01",
        start="00:05",
        end="00:12",
        voiceover="讲清楚 AI 工具如何帮助企业提升效率",
        recommended_visual="科技办公",
        material_type="剪映素材",
        source_priority=["剪映素材"],
        jianying_keywords=["AI工具"],
        needs_local_library=False,
        needs_jianying=True,
        needs_codex_graphic=False,
        needs_codex_chart=False,
        needs_codex_info_card=False,
        can_use_ai_motion=False,
    )

    assistant.process_task(task)

    assert task.status_history == [
        "剪映启动中",
        "剪映搜索中",
        "已找到候选",
        "候选筛选中",
        "筛选通过",
        "下载中",
        "已下载",
    ]
