from __future__ import annotations

import argparse
import ctypes
import sys
import time
from datetime import datetime
from pathlib import Path
from tkinter import Button, Canvas, Label, Tk, messagebox

from PIL import Image, ImageTk

from app.materials.adapters.analysis import ConservativeScreenshotAnalyzer
from app.materials.adapters.config import (
    DesktopPoint,
    DesktopUIConfig,
    ScreenRegion,
    save_desktop_ui_profile,
)
from app.materials.adapters.desktop import (
    DesktopAutomationError,
    DesktopJianyingAdapter,
    PyAutoGuiDesktopBackend,
)
from app.materials.adapters.factory import adapter_config_from_settings


class ScreenshotCalibrationWizard:
    def __init__(
        self,
        screenshot_path: Path,
        window_region: ScreenRegion,
        candidate_count: int,
        screen_size: tuple[int, int],
        scale_ratio: float,
    ):
        self.screenshot_path = screenshot_path
        self.window_region = window_region
        self.candidate_count = candidate_count
        self.screen_size = screen_size
        self.scale_ratio = scale_ratio
        self.values: dict[str, object] = {}
        self.cancelled = False
        self.drag_start: tuple[int, int] | None = None
        self.preview_shape: int | None = None
        self.step_index = 0
        self.steps = self._build_steps()

        self.root = Tk()
        self.root.title("剪映界面校准")
        self.root.protocol("WM_DELETE_WINDOW", self._cancel)
        self.root.attributes("-topmost", True)

        original = Image.open(screenshot_path)
        max_width = max(800, self.root.winfo_screenwidth() - 120)
        max_height = max(500, self.root.winfo_screenheight() - 180)
        self.display_scale = min(
            max_width / original.width,
            max_height / original.height,
            1.0,
        )
        display_size = (
            max(1, int(original.width * self.display_scale)),
            max(1, int(original.height * self.display_scale)),
        )
        display_image = original.resize(display_size, Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(display_image)

        self.instruction = Label(
            self.root,
            text="",
            font=("Microsoft YaHei UI", 12),
            anchor="w",
            padx=12,
            pady=8,
        )
        self.instruction.pack(fill="x")
        self.canvas = Canvas(
            self.root,
            width=display_size[0],
            height=display_size[1],
            cursor="crosshair",
            highlightthickness=0,
        )
        self.canvas.pack()
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

        Button(self.root, text="取消校准", command=self._cancel).pack(pady=8)
        self._show_step()

    def run(self) -> DesktopUIConfig:
        self.root.mainloop()
        if self.cancelled or self.step_index < len(self.steps):
            raise DesktopAutomationError("剪映界面校准已取消，没有修改配置文件。")

        candidate_regions = [
            self.values[f"candidate_region_{index}"]
            for index in range(1, self.candidate_count + 1)
        ]
        download_regions = [
            self.values[f"candidate_download_region_{index}"]
            for index in range(1, self.candidate_count + 1)
        ]
        return DesktopUIConfig(
            window_title="剪映专业版",
            search_box=self.values["search_box"],
            search_results_region=self.values["search_results_region"],
            candidate_regions=candidate_regions,
            candidate_click_points=[_region_center(item) for item in candidate_regions],
            candidate_download_regions=download_regions,
            download_status_region=self.values.get("download_status_region"),
            screenshot_region=self.window_region,
            calibrated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            screen_width=self.screen_size[0],
            screen_height=self.screen_size[1],
            scale_ratio=self.scale_ratio,
        )

    def _build_steps(self) -> list[tuple[str, str, str]]:
        steps = [
            ("point", "请点击剪映搜索框中央。", "search_box"),
            ("region", "请拖选完整的搜索结果区域。", "search_results_region"),
        ]
        for index in range(1, self.candidate_count + 1):
            steps.append(
                (
                    "region",
                    f"请拖选第 {index} 个候选素材卡片。",
                    f"candidate_region_{index}",
                )
            )
        for index in range(1, self.candidate_count + 1):
            steps.append(
                (
                    "region",
                    f"请拖选第 {index} 个候选素材卡片上的下载按钮。",
                    f"candidate_download_region_{index}",
                )
            )
        return steps

    def _show_step(self) -> None:
        if self.step_index >= len(self.steps):
            self.root.quit()
            self.root.destroy()
            return
        _kind, text, _key = self.steps[self.step_index]
        self.instruction.config(
            text=f"{self.step_index + 1}/{len(self.steps)}  {text}"
        )

    def _on_press(self, event) -> None:
        kind, _text, _key = self.steps[self.step_index]
        if kind == "region":
            self.drag_start = (event.x, event.y)

    def _on_drag(self, event) -> None:
        kind, _text, _key = self.steps[self.step_index]
        if kind != "region" or self.drag_start is None:
            return
        if self.preview_shape is not None:
            self.canvas.delete(self.preview_shape)
        self.preview_shape = self.canvas.create_rectangle(
            self.drag_start[0],
            self.drag_start[1],
            event.x,
            event.y,
            outline="#00d084",
            width=3,
        )

    def _on_release(self, event) -> None:
        kind, _text, key = self.steps[self.step_index]
        if kind == "point":
            self.values[key] = self._screen_point(event.x, event.y)
            self.canvas.create_oval(
                event.x - 5,
                event.y - 5,
                event.x + 5,
                event.y + 5,
                fill="#ffcc00",
                outline="",
            )
        else:
            if self.drag_start is None:
                return
            x1, y1 = self.drag_start
            if abs(event.x - x1) < 8 or abs(event.y - y1) < 8:
                messagebox.showwarning("区域太小", "请重新拖选一个完整区域。")
                return
            self.values[key] = self._screen_region(x1, y1, event.x, event.y)
            self.drag_start = None
            self.preview_shape = None
        self.step_index += 1
        self._show_step()

    def _screen_point(self, x: int, y: int) -> DesktopPoint:
        return DesktopPoint(
            x=self.window_region.x + round(x / self.display_scale),
            y=self.window_region.y + round(y / self.display_scale),
        )

    def _screen_region(self, x1: int, y1: int, x2: int, y2: int) -> ScreenRegion:
        left, right = sorted((x1, x2))
        top, bottom = sorted((y1, y2))
        return ScreenRegion(
            x=self.window_region.x + round(left / self.display_scale),
            y=self.window_region.y + round(top / self.display_scale),
            width=round((right - left) / self.display_scale),
            height=round((bottom - top) / self.display_scale),
        )

    def _cancel(self) -> None:
        self.cancelled = True
        self.root.quit()
        self.root.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="剪映专业版界面校准工具")
    parser.add_argument(
        "--profile",
        default="config/jianying_ui_profile.yaml",
        help="校准配置保存路径",
    )
    parser.add_argument(
        "--candidates",
        type=int,
        default=3,
        choices=range(3, 6),
        metavar="3-5",
        help="校准候选素材数量，默认 3",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    profile_path = Path(args.profile)
    config = adapter_config_from_settings(require_profile=False)
    backend = PyAutoGuiDesktopBackend()
    analyzer = ConservativeScreenshotAnalyzer(
        config.project_root_dir / "剪映界面校准" / "识别缓存.json"
    )
    adapter = DesktopJianyingAdapter(
        config=config,
        backend=backend,
        analyzer=analyzer,
        project_name="剪映界面校准",
    )

    try:
        adapter.ensure_started()
        _show_preparation_message()
        if not backend.focus_window(config.ui.window_title):
            raise DesktopAutomationError("无法重新聚焦剪映专业版窗口。")
        time.sleep(1.5)
        window_region = backend.window_region(config.ui.window_title)
        if window_region is None:
            raise DesktopAutomationError("无法读取剪映专业版窗口区域。")

        screenshot_path = profile_path.with_suffix(".png")
        backend.screenshot(window_region, screenshot_path)
        wizard = ScreenshotCalibrationWizard(
            screenshot_path=screenshot_path,
            window_region=window_region,
            candidate_count=args.candidates,
            screen_size=backend.screen_size(),
            scale_ratio=_windows_scale_ratio(),
        )
        profile = wizard.run()
        save_desktop_ui_profile(profile_path, profile)
    except DesktopAutomationError as exc:
        print(f"校准失败：{exc}", file=sys.stderr)
        return 1

    print(f"校准完成：{profile_path.resolve()}")
    return 0


def _show_preparation_message() -> None:
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    messagebox.showinfo(
        "开始剪映校准",
        "请先在剪映中进入一个项目，并打开素材库搜索页面。\n"
        "准备好后点击“确定”，工具会聚焦剪映并截图，不会自动点击素材。",
        parent=root,
    )
    root.destroy()


def _windows_scale_ratio() -> float:
    try:
        return round(ctypes.windll.user32.GetDpiForSystem() / 96, 2)
    except (AttributeError, OSError):
        return 1.0


def _region_center(region: ScreenRegion) -> DesktopPoint:
    return DesktopPoint(
        x=region.x + region.width // 2,
        y=region.y + region.height // 2,
    )


if __name__ == "__main__":
    raise SystemExit(main())
