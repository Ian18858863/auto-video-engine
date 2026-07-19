"""Read-only FFmpeg/ffprobe technical checks for rendered video artifacts.

The probe is intentionally not a completion or publishing decision.  It
answers only whether a media file is structurally readable and matches the
requested technical envelope.  Visual narration, facts, rights, subtitles,
and Ian's approval remain separate gates.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class VideoTechnicalProbeError(RuntimeError):
    """A probe cannot safely establish a technical result."""


@dataclass(frozen=True, slots=True)
class VideoTechnicalExpectation:
    width: int = 1080
    height: int = 1920
    fps: float = 30.0
    fps_tolerance: float = 0.05
    min_duration_seconds: float = 0.1
    max_duration_seconds: float | None = None
    require_audio: bool = False
    require_subtitles: bool = False
    required_pixel_format: str = "yuv420p"
    allowed_video_codecs: tuple[str, ...] = ("h264", "hevc")


@dataclass(frozen=True, slots=True)
class TechnicalCheck:
    name: str
    required: bool
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class VideoTechnicalReport:
    path: Path
    is_valid: bool
    checks: tuple[TechnicalCheck, ...]
    width: int | None
    height: int | None
    fps: float | None
    duration_seconds: float | None
    video_codec: str
    pixel_format: str
    audio_streams: int
    subtitle_streams: int
    probe_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "is_valid": self.is_valid,
            "checks": [item.to_dict() for item in self.checks],
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "duration_seconds": self.duration_seconds,
            "video_codec": self.video_codec,
            "pixel_format": self.pixel_format,
            "audio_streams": self.audio_streams,
            "subtitle_streams": self.subtitle_streams,
            "probe_source": self.probe_source,
        }


def probe_video(
    path: Path | str,
    *,
    expected: VideoTechnicalExpectation | None = None,
    ffprobe_executable: Path | str | None = None,
    ffmpeg_executable: Path | str | None = None,
    timeout_seconds: float = 30.0,
) -> VideoTechnicalReport:
    """Probe one existing D: media file without writing beside it."""

    media_path = _require_d_media(path)
    expectation = expected or VideoTechnicalExpectation()
    _validate_expectation(expectation)
    if not math.isfinite(float(timeout_seconds)) or float(timeout_seconds) <= 0:
        raise VideoTechnicalProbeError("technical probe timeout must be positive")

    probe_exe = str(ffprobe_executable or shutil.which("ffprobe") or "").strip()
    if not probe_exe:
        raise VideoTechnicalProbeError("ffprobe executable is unavailable")
    ffmpeg_exe = str(ffmpeg_executable or _default_ffmpeg()).strip()
    if not ffmpeg_exe:
        raise VideoTechnicalProbeError("ffmpeg executable is unavailable")

    probe_command = (
        probe_exe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(media_path),
    )
    try:
        raw = _run_probe_command(probe_command, timeout_seconds=float(timeout_seconds))
        payload = json.loads(raw.decode("utf-8", errors="strict"))
        if not isinstance(payload, Mapping):
            raise ValueError("ffprobe JSON root is not an object")
    except (VideoTechnicalProbeError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return _failed_report(media_path, f"probe: {exc}")

    streams = payload.get("streams")
    if not isinstance(streams, list):
        return _failed_report(media_path, "probe: streams missing")
    video = next((item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "video"), None)
    audio_count = sum(
        1 for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "audio"
    )
    subtitle_count = sum(
        1
        for item in streams
        if isinstance(item, Mapping) and item.get("codec_type") == "subtitle"
    )
    format_data = payload.get("format") if isinstance(payload.get("format"), Mapping) else {}
    width = _int_or_none(video.get("width") if video else None)
    height = _int_or_none(video.get("height") if video else None)
    fps = _parse_rate(video.get("avg_frame_rate") if video else None)
    duration = _first_float(
        video.get("duration") if video else None,
        format_data.get("duration"),
    )
    codec = str(video.get("codec_name") or "") if video else ""
    pixel_format = str(video.get("pix_fmt") or "") if video else ""

    checks = [
        TechnicalCheck("video_stream", True, video is not None, "存在视频流" if video else "缺少视频流"),
        TechnicalCheck(
            "dimensions",
            True,
            width == expectation.width and height == expectation.height,
            f"实际 {width}x{height}，期望 {expectation.width}x{expectation.height}",
        ),
        TechnicalCheck(
            "fps",
            True,
            fps is not None and abs(fps - expectation.fps) <= expectation.fps_tolerance,
            f"实际 {fps}fps，期望 {expectation.fps}±{expectation.fps_tolerance}",
        ),
        TechnicalCheck(
            "duration",
            True,
            duration is not None
            and duration >= expectation.min_duration_seconds
            and (
                expectation.max_duration_seconds is None
                or duration <= expectation.max_duration_seconds
            ),
            f"实际 {duration}s，允许范围 {expectation.min_duration_seconds}–{expectation.max_duration_seconds or '∞'}s",
        ),
        TechnicalCheck(
            "codec",
            True,
            codec in expectation.allowed_video_codecs,
            f"实际 {codec or '未知'}，允许 {', '.join(expectation.allowed_video_codecs)}",
        ),
        TechnicalCheck(
            "pixel_format",
            True,
            pixel_format == expectation.required_pixel_format,
            f"实际 {pixel_format or '未知'}，期望 {expectation.required_pixel_format}",
        ),
        TechnicalCheck(
            "audio",
            expectation.require_audio,
            (audio_count > 0) if expectation.require_audio else True,
            f"音频流 {audio_count} 条" + ("（未要求）" if not expectation.require_audio else ""),
        ),
    ]
    decode_check = TechnicalCheck("decode", True, True, "尚未执行")
    checks.append(
        TechnicalCheck(
            "subtitles",
            expectation.require_subtitles,
            (subtitle_count > 0) if expectation.require_subtitles else True,
            f"subtitle streams={subtitle_count}",
        )
    )
    decode_command = (
        ffmpeg_exe,
        "-v",
        "error",
        "-i",
        str(media_path),
        "-map",
        "0:v:0",
        "-f",
        "null",
        "-",
    )
    try:
        _run_decode_command(decode_command, timeout_seconds=float(timeout_seconds))
    except VideoTechnicalProbeError as exc:
        decode_check = TechnicalCheck("decode", True, False, str(exc))
    else:
        decode_check = TechnicalCheck("decode", True, True, "FFmpeg 解码检查通过")
    checks.append(decode_check)
    return VideoTechnicalReport(
        path=media_path,
        is_valid=all(item.passed for item in checks if item.required),
        checks=tuple(checks),
        width=width,
        height=height,
        fps=fps,
        duration_seconds=duration,
        video_codec=codec,
        pixel_format=pixel_format,
        audio_streams=audio_count,
        subtitle_streams=subtitle_count,
        probe_source=probe_exe,
    )


def _default_ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


def _run_probe_command(command: tuple[str, ...], *, timeout_seconds: float) -> bytes:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise VideoTechnicalProbeError(f"ffprobe failed to run: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or b"").decode("utf-8", errors="replace")
        raise VideoTechnicalProbeError(detail.strip()[-800:] or "ffprobe returned non-zero")
    return completed.stdout or b""


def _run_decode_command(command: tuple[str, ...], *, timeout_seconds: float) -> None:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise VideoTechnicalProbeError(f"ffmpeg decode failed to run: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or b"").decode("utf-8", errors="replace")
        raise VideoTechnicalProbeError(detail.strip()[-800:] or "ffmpeg decode returned non-zero")


def _failed_report(path: Path, detail: str) -> VideoTechnicalReport:
    return VideoTechnicalReport(
        path=path,
        is_valid=False,
        checks=(TechnicalCheck("probe", True, False, detail),),
        width=None,
        height=None,
        fps=None,
        duration_seconds=None,
        video_codec="",
        pixel_format="",
        audio_streams=0,
        subtitle_streams=0,
        probe_source="",
    )


def _require_d_media(value: Path | str) -> Path:
    path = Path(value)
    raw = str(path).replace("/", "\\")
    if not path.is_absolute() or len(raw) < 2 or raw[1].upper() != ":" or raw[0].upper() != "D":
        raise VideoTechnicalProbeError("media path must be on D 盘")
    if path.is_symlink():
        raise VideoTechnicalProbeError("media path must not be a symlink")
    _assert_safe_ancestors(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise VideoTechnicalProbeError("media file does not exist") from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise VideoTechnicalProbeError("media path must be a regular file")
    resolved_raw = str(resolved).replace("/", "\\")
    if len(resolved_raw) < 2 or resolved_raw[0].upper() != "D" or resolved_raw[1] != ":":
        raise VideoTechnicalProbeError("resolved media path escaped D 盘")
    return resolved


def _validate_expectation(expected: VideoTechnicalExpectation) -> None:
    if type(expected.width) is not int or expected.width < 16:
        raise VideoTechnicalProbeError("expected width is invalid")
    if type(expected.height) is not int or expected.height < 16:
        raise VideoTechnicalProbeError("expected height is invalid")
    if not math.isfinite(expected.fps) or expected.fps <= 0:
        raise VideoTechnicalProbeError("expected fps is invalid")
    if not math.isfinite(expected.fps_tolerance) or expected.fps_tolerance < 0:
        raise VideoTechnicalProbeError("expected fps tolerance is invalid")
    if not math.isfinite(expected.min_duration_seconds) or expected.min_duration_seconds < 0:
        raise VideoTechnicalProbeError("expected minimum duration is invalid")
    if expected.max_duration_seconds is not None and (
        not math.isfinite(expected.max_duration_seconds)
        or expected.max_duration_seconds < expected.min_duration_seconds
    ):
        raise VideoTechnicalProbeError("expected maximum duration is invalid")


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _first_float(*values: Any) -> float | None:
    for value in values:
        try:
            result = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(result) and result >= 0:
            return result
    return None


def _parse_rate(value: Any) -> float | None:
    if isinstance(value, str) and "/" in value:
        numerator, denominator = value.split("/", 1)
        try:
            result = float(numerator) / float(denominator)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    else:
        result = _first_float(value)
    return result if result is not None and math.isfinite(result) and result > 0 else None


def _assert_safe_ancestors(path: Path) -> None:
    current = path
    while True:
        if current.exists():
            try:
                if current.is_symlink():
                    raise VideoTechnicalProbeError("media path contains a symlink ancestor")
                is_junction = getattr(current, "is_junction", None)
                if callable(is_junction) and is_junction():
                    raise VideoTechnicalProbeError("media path contains a junction ancestor")
                if getattr(current.lstat(), "st_file_attributes", 0) & 0x0400:
                    raise VideoTechnicalProbeError("media path contains a reparse ancestor")
            except OSError as exc:
                raise VideoTechnicalProbeError("media path attributes are unreadable") from exc
        parent = current.parent
        if parent == current:
            return
        current = parent
