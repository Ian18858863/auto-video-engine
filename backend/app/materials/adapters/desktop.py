from __future__ import annotations

import ctypes
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import Protocol

from app.materials.adapters.analysis import CandidateScreenshotAnalyzer
from app.materials.adapters.config import (
    DesktopPoint,
    JianyingAdapterConfig,
    ScreenRegion,
)
from app.materials.adapters.errors import DesktopAutomationError
from app.materials.assistant import DownloadResult
from app.materials.scoring import CandidateMaterial


PARTIAL_SUFFIXES = {".tmp", ".part", ".download", ".crdownload"}


class DesktopAutomationBackend(Protocol):
    def find_window(self, title: str) -> bool: ...

    def focus_window(self, title: str) -> bool: ...

    def launch(self, executable: Path, arguments: list[str]) -> bool: ...

    def wait_for_window(self, title: str, timeout_seconds: float) -> bool: ...

    def click(self, point: DesktopPoint) -> None: ...

    def paste_text(self, text: str) -> None: ...

    def press(self, key: str) -> None: ...

    def screenshot(self, region: ScreenRegion, destination: Path) -> None: ...

    def locate_on_screen(
        self,
        template_path: Path,
        confidence: float,
    ) -> DesktopPoint | None: ...

    def sleep(self, seconds: float) -> None: ...

    def screen_size(self) -> tuple[int, int]: ...

    def window_region(self, title: str) -> ScreenRegion | None: ...


class DesktopJianyingAdapter:
    def __init__(
        self,
        config: JianyingAdapterConfig,
        backend: DesktopAutomationBackend,
        analyzer: CandidateScreenshotAnalyzer,
        project_name: str,
    ):
        self.config = config
        self.backend = backend
        self.analyzer = analyzer
        self.project_name = project_name
        self._candidate_click_points: dict[str, DesktopPoint] = {}
        self._candidate_download_points: dict[str, DesktopPoint] = {}

    def ensure_started(self) -> None:
        title = self.config.ui.window_title
        if self.backend.find_window(title):
            if not self.backend.focus_window(title):
                raise DesktopAutomationError("剪映专业版已打开，但窗口聚焦失败。")
            return

        executable = self.config.desktop_app_path
        if not executable.exists():
            raise DesktopAutomationError(f"找不到剪映专业版：{executable}")
        if not self.backend.launch(executable, ["--src3"]):
            raise DesktopAutomationError("启动剪映专业版失败。")
        if not self.backend.wait_for_window(title, self.config.launch_timeout_seconds):
            raise DesktopAutomationError("剪映专业版启动后未检测到主窗口。")
        if not self.backend.focus_window(title):
            raise DesktopAutomationError("剪映专业版窗口聚焦失败。")

    def search(self, keywords: list[str]) -> list[CandidateMaterial]:
        _validate_storage_path(self.config.project_root_dir, "项目素材目录")
        _validate_storage_path(self.config.download_dir, "剪映下载目录")
        self._validate_calibrated_display()
        self.ensure_started()
        if not keywords:
            raise DesktopAutomationError("没有可搜索的剪映关键词。")
        if self.config.ui.search_box is None:
            raise DesktopAutomationError("未配置剪映搜索框，已停止，避免点击未知位置。")
        if not self.config.ui.candidate_regions:
            raise DesktopAutomationError("未配置候选素材区域，无法安全截图。")

        keyword = keywords[0]
        self.backend.click(self.config.ui.search_box)
        self.backend.press("ctrl+a")
        self.backend.paste_text(keyword)
        self.backend.press("enter")
        self.backend.sleep(self.config.ui.search_wait_seconds)

        screenshot_dir = (
            self.config.project_root_dir
            / self.project_name
            / self.config.candidate_screenshot_dir
        )
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        candidates: list[CandidateMaterial] = []
        limit = min(
            self.config.max_candidates_per_search,
            len(self.config.ui.candidate_regions),
        )
        for index, region in enumerate(self.config.ui.candidate_regions[:limit], start=1):
            screenshot_path = screenshot_dir / f"{keyword}_候选_{index:02d}.png"
            self.backend.screenshot(region, screenshot_path)
            candidate = self.analyzer.analyze(screenshot_path, keyword, index)
            candidates.append(candidate)
            if index <= len(self.config.ui.candidate_click_points):
                self._candidate_click_points[candidate.material_id] = (
                    self.config.ui.candidate_click_points[index - 1]
                )
            if index <= len(self.config.ui.candidate_download_regions):
                self._candidate_download_points[candidate.material_id] = _region_center(
                    self.config.ui.candidate_download_regions[index - 1]
                )
        return candidates

    def download(
        self,
        candidate: CandidateMaterial,
        download_dir: Path,
    ) -> DownloadResult:
        try:
            _validate_storage_path(download_dir, "剪映下载目录")
        except DesktopAutomationError as exc:
            return DownloadResult(success=False, error_message=str(exc))
        direct_download_point = self._candidate_download_points.get(candidate.material_id)
        if direct_download_point is not None:
            watcher = DownloadDirectoryWatcher(
                download_dir=download_dir,
                timeout_seconds=self.config.download_timeout_seconds,
            )
            before = watcher.snapshot()
            self.backend.click(direct_download_point)
            try:
                downloaded_path = watcher.wait_for_new_file(before)
            except DesktopAutomationError as exc:
                return DownloadResult(success=False, error_message=str(exc))
            return DownloadResult(success=True, downloaded_path=downloaded_path)

        click_point = self._candidate_click_points.get(candidate.material_id)
        if click_point is None:
            return DownloadResult(
                success=False,
                error_message="候选素材点击位置未识别，已停止，避免点击未知位置。",
            )

        self.backend.click(click_point)
        self.backend.sleep(self.config.ui.detail_wait_seconds)
        download_button = self._resolve_download_button()
        if download_button is None:
            return DownloadResult(
                success=False,
                error_message="下载按钮识别失败，已停止，未点击未知按钮。",
            )

        watcher = DownloadDirectoryWatcher(
            download_dir=download_dir,
            timeout_seconds=self.config.download_timeout_seconds,
        )
        before = watcher.snapshot()
        self.backend.click(download_button)
        try:
            downloaded_path = watcher.wait_for_new_file(before)
        except DesktopAutomationError as exc:
            return DownloadResult(success=False, error_message=str(exc))
        return DownloadResult(success=True, downloaded_path=downloaded_path)

    def _resolve_download_button(self) -> DesktopPoint | None:
        if self.config.ui.download_button_template is not None:
            template = self.config.ui.download_button_template
            if not template.exists():
                return None
            return self.backend.locate_on_screen(
                template,
                self.config.ui.image_confidence,
            )
        return self.config.ui.download_button_point

    def _validate_calibrated_display(self) -> None:
        screen_size = getattr(self.backend, "screen_size", None)
        if not callable(screen_size):
            return
        expected = (self.config.ui.screen_width, self.config.ui.screen_height)
        if not all(expected):
            return
        actual = screen_size()
        if actual != expected:
            raise DesktopAutomationError(
                "当前屏幕分辨率与剪映校准配置不一致，已停止点击。"
                f"校准值为 {expected[0]}x{expected[1]}，当前为 {actual[0]}x{actual[1]}，"
                "请重新运行剪映界面校准工具。"
            )


class DownloadDirectoryWatcher:
    def __init__(
        self,
        download_dir: Path,
        timeout_seconds: float = 120,
        poll_interval_seconds: float = 0.5,
        stable_checks: int = 3,
    ):
        self.download_dir = download_dir
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.stable_checks = stable_checks

    def snapshot(self) -> set[Path]:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        return {path.resolve() for path in self.download_dir.iterdir() if path.is_file()}

    def wait_for_new_file(self, before: set[Path]) -> Path:
        deadline = time.monotonic() + self.timeout_seconds
        stable_sizes: dict[Path, tuple[int, int]] = {}
        while time.monotonic() < deadline:
            for path in self.download_dir.iterdir():
                if not path.is_file() or path.resolve() in before:
                    continue
                if path.suffix.lower() in PARTIAL_SUFFIXES:
                    continue
                size = path.stat().st_size
                previous_size, count = stable_sizes.get(path, (-1, 0))
                count = count + 1 if size == previous_size and size > 0 else 1
                stable_sizes[path] = (size, count)
                if count >= self.stable_checks:
                    return path
            time.sleep(self.poll_interval_seconds)
        raise DesktopAutomationError("等待剪映下载完成超时，未移动任何文件。")


class PyAutoGuiDesktopBackend:
    def __init__(self):
        try:
            import pyautogui
            import pyperclip
        except ImportError as exc:
            raise DesktopAutomationError(
                "缺少桌面自动化依赖，请安装 pyautogui、Pillow、pyperclip。"
            ) from exc
        self.pyautogui = pyautogui
        self.pyperclip = pyperclip
        self._last_window_handle: int | None = None

    def find_window(self, title: str) -> bool:
        self._last_window_handle = _find_window_handle(title)
        return self._last_window_handle is not None

    def focus_window(self, title: str) -> bool:
        handle = self._last_window_handle or _find_window_handle(title)
        if handle is None:
            return False
        ctypes.windll.user32.ShowWindow(handle, 9)
        return bool(ctypes.windll.user32.SetForegroundWindow(handle))

    def launch(self, executable: Path, arguments: list[str]) -> bool:
        try:
            subprocess.Popen([str(executable), *arguments])
        except OSError:
            return False
        return True

    def wait_for_window(self, title: str, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.find_window(title):
                return True
            time.sleep(0.5)
        return False

    def click(self, point: DesktopPoint) -> None:
        self.pyautogui.click(point.x, point.y)

    def paste_text(self, text: str) -> None:
        self.pyperclip.copy(text)
        self.pyautogui.hotkey("ctrl", "v")

    def press(self, key: str) -> None:
        if "+" in key:
            self.pyautogui.hotkey(*key.split("+"))
        else:
            self.pyautogui.press(key)

    def screenshot(self, region: ScreenRegion, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        image = self.pyautogui.screenshot(
            region=(region.x, region.y, region.width, region.height)
        )
        image.save(destination)

    def locate_on_screen(
        self,
        template_path: Path,
        confidence: float,
    ) -> DesktopPoint | None:
        try:
            point = self.pyautogui.locateCenterOnScreen(
                str(template_path),
                confidence=confidence,
            )
        except (NotImplementedError, TypeError):
            point = self.pyautogui.locateCenterOnScreen(str(template_path))
        if point is None:
            return None
        return DesktopPoint(int(point.x), int(point.y))

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def screen_size(self) -> tuple[int, int]:
        size = self.pyautogui.size()
        return int(size.width), int(size.height)

    def window_region(self, title: str) -> ScreenRegion | None:
        handle = self._last_window_handle or _find_window_handle(title)
        if handle is None:
            return None
        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(handle, ctypes.byref(rect)):
            return None
        return ScreenRegion(
            x=int(rect.left),
            y=int(rect.top),
            width=int(rect.right - rect.left),
            height=int(rect.bottom - rect.top),
        )


def _find_window_handle(title_fragment: str) -> int | None:
    if not hasattr(ctypes, "windll"):
        return None
    handles: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _lparam):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
        if title_fragment.lower() in buffer.value.lower():
            handles.append(int(hwnd))
            return False
        return True

    ctypes.windll.user32.EnumWindows(callback_type(callback), 0)
    return handles[0] if handles else None


def _validate_storage_path(path: Path, label: str) -> None:
    drive = _storage_drive(path)
    if not path.is_absolute() and drive is None:
        raise DesktopAutomationError(f"{label}必须使用绝对路径，已停止自动化。")
    if drive == "C:":
        raise DesktopAutomationError(
            f"{label}配置在 C 盘，已停止；系统不会写入 C 盘，请手动配置其他磁盘。"
        )
    if drive == "D:" and not Path(f"{drive[0]}:/").exists():
        raise DesktopAutomationError(
            f"D 盘不存在，无法使用{label}；请先手动配置路径，系统不会改写到 C 盘。"
        )


def _storage_drive(path: Path) -> str | None:
    native_drive = path.drive.upper()
    if native_drive:
        return native_drive
    normalized = str(path).replace("\\", "/")
    if len(normalized) >= 3 and normalized[1:3] == ":/":
        return normalized[:2].upper()
    return None


def _region_center(region: ScreenRegion) -> DesktopPoint:
    return DesktopPoint(
        x=region.x + region.width // 2,
        y=region.y + region.height // 2,
    )
