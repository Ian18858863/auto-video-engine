from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

from app.materials.asset_paths import AutoMaterialPaths


@dataclass(slots=True)
class OfficialCapturePlan:
    url: str
    source_name: str
    page_title: str
    output_dir: Path
    need_scroll_recording: bool = True
    need_first_screen_screenshot: bool = False
    need_full_page_screenshot: bool = False
    scroll_recording_seconds: int = 6
    scroll_recording_fps: int = 15
    content_selectors: list[str] = field(default_factory=list)
    hide_selectors: list[str] = field(default_factory=list)
    filename_prefix: str = ""


@dataclass(slots=True)
class OfficialCaptureResult:
    success: bool
    scroll_recording_path: str = ""
    first_screen_screenshot_path: str = ""
    full_page_screenshot_path: str = ""
    reason: str = ""
    files: list[str] = field(default_factory=list)
    used_placeholder: bool = False


class OfficialPageCaptureService:
    def __init__(
        self,
        *,
        paths: AutoMaterialPaths | None = None,
        use_playwright: bool = True,
        fallback_to_placeholder: bool = False,
        navigation_timeout_ms: int = 30_000,
        operation_timeout_ms: int = 12_000,
        profile_dir: Path | None = None,
        downloads_dir: Path | None = None,
    ):
        self.paths = paths or AutoMaterialPaths()
        self.use_playwright = use_playwright
        self.fallback_to_placeholder = fallback_to_placeholder
        self.navigation_timeout_ms = navigation_timeout_ms
        self.operation_timeout_ms = operation_timeout_ms
        self.profile_dir = profile_dir
        self.downloads_dir = downloads_dir

    def capture(self, plan: OfficialCapturePlan) -> OfficialCaptureResult:
        if not str(plan.url or "").strip():
            return OfficialCaptureResult(success=False, reason="缺少官方页面 URL。")
        plan.output_dir.mkdir(parents=True, exist_ok=True)
        frame_dir = plan.output_dir / f"{plan.filename_prefix}_frames"
        frame_dir.mkdir(parents=True, exist_ok=True)
        first_screen_path = (
            plan.output_dir / f"{plan.filename_prefix}_首屏截图.png"
            if plan.need_first_screen_screenshot
            else None
        )
        full_page_path = (
            plan.output_dir / f"{plan.filename_prefix}_整页截图.png"
            if plan.need_full_page_screenshot
            else None
        )
        recording_path = (
            plan.output_dir / f"{plan.filename_prefix}_慢速滚动录屏.mp4"
            if plan.need_scroll_recording
            else None
        )

        used_placeholder = False
        placeholder_reason = ""
        try:
            if self.use_playwright:
                self._capture_with_playwright(
                    plan,
                    frame_dir=frame_dir,
                    first_screen_path=first_screen_path,
                    full_page_path=full_page_path,
                )
            else:
                raise RuntimeError("Playwright 已禁用。")
        except Exception as exc:
            if not self.fallback_to_placeholder:
                return OfficialCaptureResult(
                    success=False,
                    reason=f"官方页面录屏失败：{exc}",
                )
            used_placeholder = True
            placeholder_reason = str(exc)
            _write_placeholder_scroll_frames(
                frame_dir,
                plan.page_title,
                plan.source_name,
                frame_count=plan.scroll_recording_seconds * plan.scroll_recording_fps,
            )

        files: list[str] = []
        if recording_path:
            try:
                _frames_to_video(
                    frame_dir / "official_%03d.png",
                    recording_path,
                    duration_seconds=plan.scroll_recording_seconds,
                    fps=plan.scroll_recording_fps,
                )
                files.append(str(recording_path))
            except Exception as exc:
                return OfficialCaptureResult(
                    success=False,
                    reason=f"官方页面录屏合成失败：{exc}",
                )
        for path in [first_screen_path, full_page_path]:
            if path and path.is_file():
                files.append(str(path))
        return OfficialCaptureResult(
            success=True,
            scroll_recording_path=str(recording_path or ""),
            first_screen_screenshot_path=str(first_screen_path or ""),
            full_page_screenshot_path=str(full_page_path or ""),
            reason=(
                f"页面抓取失败，已生成本地解释卡慢速滚动录屏：{placeholder_reason}"
                if used_placeholder
                else "来源页面慢速滚动录屏已生成。"
            ),
            files=files,
            used_placeholder=used_placeholder,
        )

    def _capture_with_playwright(
        self,
        plan: OfficialCapturePlan,
        *,
        frame_dir: Path,
        first_screen_path: Path | None,
        full_page_path: Path | None,
    ) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = None
            context = None
            try:
                if self.profile_dir is not None:
                    self.profile_dir.mkdir(parents=True, exist_ok=True)
                    if self.downloads_dir is None:
                        raise RuntimeError("Playwright downloads directory is not configured.")
                    self.downloads_dir.mkdir(parents=True, exist_ok=True)
                    context = playwright.chromium.launch_persistent_context(
                        user_data_dir=str(self.profile_dir),
                        headless=True,
                        accept_downloads=True,
                        downloads_path=str(self.downloads_dir),
                        viewport={"width": 1080, "height": 1920},
                        device_scale_factor=1,
                    )
                    page = context.pages[0] if context.pages else context.new_page()
                else:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(
                        viewport={"width": 1080, "height": 1920},
                        device_scale_factor=1,
                    )
                page.set_default_navigation_timeout(self.navigation_timeout_ms)
                page.set_default_timeout(self.operation_timeout_ms)
                page.goto(plan.url, wait_until="domcontentloaded", timeout=self.navigation_timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=4_000)
                except Exception:
                    pass
                self._hide_noise(page, plan)
                self._focus_content(page, plan)
                if first_screen_path:
                    page.screenshot(path=str(first_screen_path), full_page=False, timeout=self.operation_timeout_ms)
                if full_page_path:
                    page.screenshot(path=str(full_page_path), full_page=True, timeout=self.operation_timeout_ms)
                self._write_scroll_frames(page, frame_dir, plan)
            finally:
                if context:
                    context.close()
                elif browser:
                    browser.close()

    def _hide_noise(self, page, plan: OfficialCapturePlan) -> None:
        selectors = [*plan.hide_selectors, "[style*='position: fixed']", "[style*='position:fixed']"]
        css = ",".join(selectors)
        if css:
            page.add_style_tag(
                content=(
                    f"{css} {{ display:none!important; visibility:hidden!important; }}"
                    " html { scroll-behavior:auto!important; }"
                    " body { overflow-x:hidden!important; }"
                )
            )

    def _focus_content(self, page, plan: OfficialCapturePlan) -> None:
        selector_list = plan.content_selectors or ["article", "main", "[role=main]"]
        page.evaluate(
            """
            (selectors) => {
              const target = selectors.map((selector) => document.querySelector(selector)).find(Boolean);
              if (!target) return;
              target.scrollIntoView({block: 'start'});
            }
            """,
            selector_list,
        )

    def _write_scroll_frames(self, page, frame_dir: Path, plan: OfficialCapturePlan) -> None:
        frame_count = max(
            24,
            min(150, int(plan.scroll_recording_seconds) * int(plan.scroll_recording_fps)),
        )
        max_scroll = int(
            page.evaluate(
                "() => Math.max(0, document.documentElement.scrollHeight - window.innerHeight)"
            )
            or 0
        )
        positions = _scroll_positions(max_scroll, frame_count)
        for index, scroll_y in enumerate(positions):
            page.evaluate("(y) => window.scrollTo(0, y)", scroll_y)
            page.wait_for_timeout(35)
            page.screenshot(
                path=str(frame_dir / f"official_{index:03d}.png"),
                full_page=False,
                timeout=self.operation_timeout_ms,
            )


def build_official_capture_plan(
    *,
    url: str,
    source_name: str,
    page_title: str,
    paths: AutoMaterialPaths | None = None,
    captured_at: datetime | None = None,
) -> OfficialCapturePlan:
    paths = paths or AutoMaterialPaths()
    captured_at = captured_at or datetime.now()
    clean_source = _safe_name(source_name or "未知来源")
    clean_title = _safe_name(page_title or "官方页面")
    return OfficialCapturePlan(
        url=url,
        source_name=source_name,
        page_title=page_title,
        output_dir=paths.official_page_dir / clean_source,
        content_selectors=["article", "main", "[role=main]", ".content", ".article"],
        hide_selectors=[
            "aside",
            ".ad",
            ".ads",
            ".advertisement",
            ".banner",
            ".sidebar",
            ".rightbar",
            ".right-bar",
            ".leftbar",
            ".left-bar",
            ".recommend",
            ".related",
            ".cookie",
            ".cookies",
            ".privacy",
            ".newsletter",
            ".popup",
            ".modal",
            "[class*=ad-]",
            "[id*=ad-]",
            "[class*=advert]",
            "[id*=advert]",
            "[class*=sidebar]",
            "[id*=sidebar]",
            "[class*=popup]",
            "[id*=popup]",
            "iframe[src*=ad]",
        ],
        filename_prefix=f"{clean_title}_{captured_at:%Y%m%d_%H%M%S}",
    )


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\r\n\t]+', "_", value).strip(" ._")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80] or "未命名"


def _scroll_positions(max_scroll: int, frame_count: int) -> list[int]:
    if frame_count <= 1 or max_scroll <= 0:
        return [0 for _ in range(max(1, frame_count))]
    positions: list[int] = []
    for index in range(frame_count):
        progress = index / (frame_count - 1)
        eased = progress * progress * (3 - 2 * progress)
        positions.append(round(max_scroll * eased))
    return positions


def _frames_to_video(
    pattern: Path,
    output_path: Path,
    *,
    duration_seconds: int,
    fps: int = 15,
) -> None:
    frame_count = len(list(pattern.parent.glob("official_*.png")))
    if frame_count <= 0:
        raise RuntimeError("没有生成官方页面录屏帧。")
    input_fps = max(1, min(int(fps), round(frame_count / max(1, duration_seconds))))
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-framerate",
        str(input_fps),
        "-i",
        str(pattern),
        "-t",
        str(duration_seconds),
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,fps=30,format=yuv420p",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-b:v",
        "12M",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, timeout=max(30, duration_seconds + 20))
    if result.returncode != 0:
        output = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace")
        raise RuntimeError((output or "ffmpeg 合成失败").strip()[-800:])


def _write_placeholder_scroll_frames(
    frame_dir: Path,
    page_title: str,
    source_name: str,
    *,
    frame_count: int = 90,
) -> None:
    frame_dir.mkdir(parents=True, exist_ok=True)
    font_title = _font(46)
    font_body = _font(30)
    total_frames = max(24, frame_count)
    for index in range(total_frames):
        image = Image.new("RGB", (1080, 1920), (242, 247, 248))
        draw = ImageDraw.Draw(image)
        progress = index / max(1, total_frames - 1)
        eased = progress * progress * (3 - 2 * progress)
        y_offset = 120 - int(eased * 760)
        draw.rounded_rectangle((72, 92 + y_offset, 1008, 420 + y_offset), radius=30, fill=(255, 255, 255), outline=(205, 220, 226), width=3)
        draw.text((116, 135 + y_offset), _wrap(page_title or "解释卡", 16), font=font_title, fill=(16, 35, 45), spacing=10)
        draw.text((116, 320 + y_offset), f"来源线索：{source_name or '未提供'}", font=font_body, fill=(70, 91, 102))
        for row in range(6):
            block_y = 520 + y_offset + row * 220
            draw.rounded_rectangle((84, block_y, 996, block_y + 148), radius=22, fill=(255, 255, 255), outline=(218, 228, 232))
            draw.text((126, block_y + 35), f"解释段落 {row + 1}：标题、时间、来源线索和核心信息。", font=font_body, fill=(42, 61, 72))
        image.save(frame_dir / f"official_{index:03d}.png")


def _wrap(text: str, width: int) -> str:
    return "\n".join(text[index : index + width] for index in range(0, len(text), width))


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ]:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()
