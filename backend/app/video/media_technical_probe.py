"""Strict audio/subtitle evidence for the isolated real-media slice."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Mapping

from app.audio.subtitle_service import SubtitleSegment, validate_subtitle_segments


class MediaTechnicalProbeError(RuntimeError):
    """Raised when audio/subtitle evidence cannot be established."""


@dataclass(frozen=True, slots=True)
class MediaTechnicalCheck:
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
class MediaTechnicalReport:
    audio_path: Path
    subtitle_path: Path
    is_valid: bool
    checks: tuple[MediaTechnicalCheck, ...]
    audio_duration_seconds: float | None
    audio_codec: str
    audio_sample_rate: int | None
    audio_channels: int | None
    subtitle_cue_count: int
    probe_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "audio_path": str(self.audio_path),
            "subtitle_path": str(self.subtitle_path),
            "is_valid": self.is_valid,
            "checks": [item.to_dict() for item in self.checks],
            "audio_duration_seconds": self.audio_duration_seconds,
            "audio_codec": self.audio_codec,
            "audio_sample_rate": self.audio_sample_rate,
            "audio_channels": self.audio_channels,
            "subtitle_cue_count": self.subtitle_cue_count,
            "probe_source": self.probe_source,
        }


def probe_audio_subtitles(
    audio_path: Path | str,
    subtitle_path: Path | str,
    *,
    ffprobe_executable: Path | str | None = None,
    ffmpeg_executable: Path | str | None = None,
    timeout_seconds: float = 30.0,
) -> MediaTechnicalReport:
    """Probe/decode a WAV plus SRT and enforce cue ≤ measured audio duration."""

    audio = _require_d_file(audio_path, "音频")
    subtitles = _require_d_file(subtitle_path, "字幕")
    if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
        raise MediaTechnicalProbeError("media probe timeout must be positive")
    probe_exe = str(ffprobe_executable or shutil.which("ffprobe") or "").strip()
    ffmpeg_exe = str(ffmpeg_executable or _default_ffmpeg()).strip()
    if not probe_exe:
        raise MediaTechnicalProbeError("ffprobe executable is unavailable")
    if not ffmpeg_exe:
        raise MediaTechnicalProbeError("ffmpeg executable is unavailable")

    checks: list[MediaTechnicalCheck] = []
    audio_duration: float | None = None
    codec = ""
    sample_rate: int | None = None
    channels: int | None = None
    try:
        payload = _probe_audio_json(
            audio,
            probe_exe,
            timeout_seconds=float(timeout_seconds),
        )
        streams = payload.get("streams")
        if not isinstance(streams, list):
            raise MediaTechnicalProbeError("ffprobe audio streams missing")
        stream = next(
            (item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "audio"),
            None,
        )
        if stream is None:
            raise MediaTechnicalProbeError("缺少音频流")
        codec = str(stream.get("codec_name") or "")
        sample_rate = _positive_int(stream.get("sample_rate"))
        channels = _positive_int(stream.get("channels"))
        format_data = payload.get("format") if isinstance(payload.get("format"), Mapping) else {}
        audio_duration = _first_float(stream.get("duration"), format_data.get("duration"))
        checks.append(MediaTechnicalCheck("audio_stream", True, True, "存在可读音频流"))
        checks.append(
            MediaTechnicalCheck(
                "audio_duration",
                True,
                audio_duration is not None and audio_duration > 0,
                f"音频时长 {audio_duration}s",
            )
        )
        checks.append(
            MediaTechnicalCheck(
                "audio_parameters",
                True,
                bool(codec and sample_rate and channels),
                f"codec={codec or '未知'}, sample_rate={sample_rate}, channels={channels}",
            )
        )
    except (MediaTechnicalProbeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        checks.append(MediaTechnicalCheck("audio_probe", True, False, str(exc)))

    try:
        _run_command(
            (
                ffmpeg_exe,
                "-v",
                "error",
                "-i",
                str(audio),
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ),
            timeout_seconds=float(timeout_seconds),
        )
        checks.append(MediaTechnicalCheck("audio_decode", True, True, "FFmpeg 音频解码通过"))
    except MediaTechnicalProbeError as exc:
        checks.append(MediaTechnicalCheck("audio_decode", True, False, str(exc)))

    cues: list[SubtitleSegment] = []
    try:
        cues = _parse_srt(subtitles.read_text(encoding="utf-8"))
        if audio_duration is None:
            raise ValueError("无法以音频时长校验字幕")
        validate_subtitle_segments(cues, audio_duration)
        checks.append(
            MediaTechnicalCheck(
                "subtitle_timing",
                True,
                True,
                f"{len(cues)} 条字幕，末尾不超过音频时长",
            )
        )
    except (OSError, UnicodeError, ValueError) as exc:
        checks.append(MediaTechnicalCheck("subtitle_timing", True, False, str(exc)))

    return MediaTechnicalReport(
        audio_path=audio,
        subtitle_path=subtitles,
        is_valid=all(item.passed for item in checks if item.required),
        checks=tuple(checks),
        audio_duration_seconds=audio_duration,
        audio_codec=codec,
        audio_sample_rate=sample_rate,
        audio_channels=channels,
        subtitle_cue_count=len(cues),
        probe_source=probe_exe,
    )


def _probe_audio_json(path: Path, executable: str, *, timeout_seconds: float) -> Mapping[str, Any]:
    raw = _run_command(
        (
            executable,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ),
        timeout_seconds=timeout_seconds,
    )
    payload = json.loads(raw.decode("utf-8", errors="strict"))
    if not isinstance(payload, Mapping):
        raise ValueError("ffprobe JSON root is not an object")
    return payload


def _run_command(command: tuple[str, ...], *, timeout_seconds: float) -> bytes:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise MediaTechnicalProbeError(f"media command failed to run: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or b"").decode("utf-8", errors="replace")
        raise MediaTechnicalProbeError(detail.strip()[-800:] or "media command returned non-zero")
    return completed.stdout or b""


_SRT_TIME = re.compile(
    r"^(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2}),(?P<ms>\d{3})$"
)


def _parse_srt(text: str) -> list[SubtitleSegment]:
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip()) if text.strip() else []
    result: list[SubtitleSegment] = []
    for index, block in enumerate(blocks, start=1):
        lines = [line.strip("\ufeff") for line in block.splitlines()]
        if len(lines) < 3:
            raise ValueError("SRT cue block is incomplete")
        timing = lines[1].split(" --> ", 1)
        if len(timing) != 2:
            raise ValueError("SRT timing line is invalid")
        start = _parse_srt_time(timing[0].strip())
        end = _parse_srt_time(timing[1].strip())
        body = "\n".join(lines[2:]).strip()
        if not body:
            raise ValueError("SRT cue text is empty")
        result.append(SubtitleSegment(index=index, start_seconds=start, end_seconds=end, text=body))
    return result


def _parse_srt_time(value: str) -> float:
    match = _SRT_TIME.fullmatch(value)
    if match is None:
        raise ValueError("SRT timestamp is invalid")
    return (
        int(match.group("h")) * 3600
        + int(match.group("m")) * 60
        + int(match.group("s"))
        + int(match.group("ms")) / 1000
    )


def _require_d_file(value: Path | str, label: str) -> Path:
    path = Path(value)
    raw = str(path).replace("/", "\\")
    if not path.is_absolute() or len(raw) < 2 or raw[0].upper() != "D" or raw[1] != ":":
        raise MediaTechnicalProbeError(f"{label}必须位于 D 盘")
    if path.is_symlink():
        raise MediaTechnicalProbeError(f"{label}不能是符号链接")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MediaTechnicalProbeError(f"{label}不存在") from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise MediaTechnicalProbeError(f"{label}必须是普通文件")
    resolved_raw = str(resolved).replace("/", "\\")
    if len(resolved_raw) < 2 or resolved_raw[0].upper() != "D" or resolved_raw[1] != ":":
        raise MediaTechnicalProbeError(f"{label}解析后越出 D 盘")
    return resolved


def _default_ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


def _first_float(*values: Any) -> float | None:
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number >= 0:
            return number
    return None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


__all__ = [
    "MediaTechnicalCheck",
    "MediaTechnicalProbeError",
    "MediaTechnicalReport",
    "probe_audio_subtitles",
]
