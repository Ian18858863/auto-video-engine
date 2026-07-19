"""Isolated full-media adapter for video + voiceover + subtitle tracks.

The legacy export route is intentionally untouched.  This adapter consumes an
immutable TimelineSpec snapshot, verifies the referenced artifact hashes, runs
the existing visual pilot, then muxes real WAV/SRT tracks with explicit FFmpeg
maps and writes technical evidence into a new D: version directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping
from uuid import uuid4
import wave

import imageio_ffmpeg

from app.video.media_technical_probe import MediaTechnicalReport, probe_audio_subtitles
from app.video.technical_probe import VideoTechnicalExpectation, VideoTechnicalReport, probe_video
from app.video.timeline_renderer import (
    TimelineRenderError,
    TimelineRenderResult,
    render_timeline_visual_pilot,
)
from app.pipeline.timeline_spec import TimelineSpecError, TimelineSpecSnapshot, load_timeline_spec_snapshot
from app.pipeline.project_identity import ProjectIdentityError, _reject_reparse_components


class TimelineMediaRenderError(RuntimeError):
    """Raised when the isolated full-media adapter cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class TimelineMediaRenderPlan:
    version_id: str
    version_dir: Path
    visual_root: Path
    visual_output_path: Path
    audio_path: Path
    subtitle_path: Path
    output_path: Path
    output_part_path: Path
    report_path: Path
    target_duration_seconds: float
    audio_duration_seconds: float
    ffmpeg_executable: str
    ffprobe_executable: str
    mux_command: tuple[str, ...]
    artifact_hashes: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "mode": "full_media_isolated",
            "output_path": str(self.output_path),
            "report_path": str(self.report_path),
            "target_duration_seconds": self.target_duration_seconds,
            "audio_duration_seconds": self.audio_duration_seconds,
            "ffmpeg_executable": self.ffmpeg_executable,
            "ffprobe_executable": self.ffprobe_executable,
            "mux_command": list(self.mux_command),
            "artifact_hashes": dict(self.artifact_hashes),
            "scope": {
                "video": True,
                "voiceover": True,
                "subtitles": True,
                "background_music": False,
                "publishing": False,
            },
        }


@dataclass(frozen=True, slots=True)
class TimelineMediaRenderResult:
    version_id: str
    timeline_version_id: str
    timeline_input_hash: str
    output_path: Path
    report_path: Path
    visual_render_manifest_path: Path
    artifact_sha256: str
    reused_segment_ids: tuple[str, ...]
    rebuilt_segment_ids: tuple[str, ...]
    source_timeline_version_id: str | None
    video_report: VideoTechnicalReport
    media_report: MediaTechnicalReport

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "timeline_version_id": self.timeline_version_id,
            "timeline_input_hash": self.timeline_input_hash,
            "mode": "full_media_isolated",
            "output_path": str(self.output_path),
            "report_path": str(self.report_path),
            "visual_render_manifest_path": str(self.visual_render_manifest_path),
            "artifact_sha256": self.artifact_sha256,
            "reused_segment_ids": list(self.reused_segment_ids),
            "rebuilt_segment_ids": list(self.rebuilt_segment_ids),
            "source_timeline_version_id": self.source_timeline_version_id,
            "video_report": self.video_report.to_dict(),
            "media_report": self.media_report.to_dict(),
        }


def build_timeline_media_render_plan(
    snapshot: TimelineSpecSnapshot | Mapping[str, Any],
    *,
    project_dir: Path | str,
    output_root: Path | str,
    ffmpeg_executable: Path | str | None = None,
    ffprobe_executable: Path | str | None = None,
    duration_tolerance_seconds: float = 0.15,
) -> TimelineMediaRenderPlan:
    normalized = _validated_snapshot(snapshot)
    project = _require_d_directory(project_dir, "项目目录", must_exist=True)
    root = _require_d_directory(output_root, "成片输出根目录", must_exist=False)
    if not math.isfinite(float(duration_tolerance_seconds)) or duration_tolerance_seconds < 0:
        raise TimelineMediaRenderError("duration_tolerance_invalid: 音视频时长容差无效")
    artifacts = normalized.artifacts
    voiceover = artifacts.get("voiceover") if isinstance(artifacts, Mapping) else None
    subtitles = artifacts.get("subtitles") if isinstance(artifacts, Mapping) else None
    if not isinstance(voiceover, Mapping) or not isinstance(subtitles, Mapping):
        raise TimelineMediaRenderError(
            "audio_subtitle_artifacts_missing: TimelineSpec 缺少 voiceover/subtitles artifact"
        )
    audio = _resolve_artifact(project, voiceover, "voiceover")
    subtitle = _resolve_artifact(project, subtitles, "subtitles")
    audio_duration = _measure_wav(audio)
    target_duration = _timeline_duration(normalized)
    if abs(audio_duration - target_duration) > float(duration_tolerance_seconds):
        raise TimelineMediaRenderError(
            "duration_mismatch: "
            f"音频 {audio_duration:.3f}s 与时间线 {target_duration:.3f}s 不一致"
        )
    executable = str(ffmpeg_executable or imageio_ffmpeg.get_ffmpeg_exe()).strip()
    if not executable:
        raise TimelineMediaRenderError("ffmpeg_unavailable: 没有可用 FFmpeg")
    probe_executable = _resolve_ffprobe_executable(
        ffprobe_executable,
        ffmpeg_executable=executable,
    )
    token = f"media-{normalized.version_id}-{uuid4().hex[:12]}"
    version_dir = root / token
    visual_root = version_dir / "visual-root"
    visual_output = visual_root / normalized.version_id / "rendered.mp4"
    output = version_dir / "rendered.mp4"
    output_part = version_dir / "rendered.part.mp4"
    report = version_dir / "technical-report.json"
    _validate_future(version_dir, "成片版本目录")
    _validate_future(output, "成片输出")
    _validate_future(output_part, "成片临时输出")
    _validate_future(report, "技术报告")
    target_text = _format_seconds(target_duration)
    mux = (
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(visual_output),
        "-i",
        str(audio),
        "-f",
        "srt",
        "-i",
        str(subtitle),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map",
        "2:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-c:s",
        "mov_text",
        "-metadata:s:s:0",
        "language=chi",
        "-t",
        target_text,
        "-movflags",
        "+faststart",
        "-n",
        str(output_part),
    )
    return TimelineMediaRenderPlan(
        version_id=token,
        version_dir=version_dir,
        visual_root=visual_root,
        visual_output_path=visual_output,
        audio_path=audio,
        subtitle_path=subtitle,
        output_path=output,
        output_part_path=output_part,
        report_path=report,
        target_duration_seconds=target_duration,
        audio_duration_seconds=audio_duration,
        ffmpeg_executable=executable,
        ffprobe_executable=probe_executable,
        mux_command=mux,
        artifact_hashes={
            "voiceover": _sha256_file(audio),
            "subtitles": _sha256_file(subtitle),
        },
    )


def render_timeline_media(
    snapshot: TimelineSpecSnapshot | Mapping[str, Any],
    *,
    project_dir: Path | str,
    output_root: Path | str,
    ffmpeg_executable: Path | str | None = None,
    ffprobe_executable: Path | str | None = None,
    timeout_seconds: float = 180.0,
    duration_tolerance_seconds: float = 0.15,
    reuse_from_visual_manifest: Path | str | None = None,
) -> TimelineMediaRenderResult:
    if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
        raise TimelineMediaRenderError("timeout_invalid: FFmpeg 超时时间必须为正数")
    normalized = _validated_snapshot(snapshot)
    plan = build_timeline_media_render_plan(
        normalized,
        project_dir=project_dir,
        output_root=output_root,
        ffmpeg_executable=ffmpeg_executable,
        ffprobe_executable=ffprobe_executable,
        duration_tolerance_seconds=duration_tolerance_seconds,
    )
    _prepare_exclusive(plan)
    try:
        visual: TimelineRenderResult = render_timeline_visual_pilot(
            normalized,
            output_root=plan.visual_root,
            ffmpeg_executable=plan.ffmpeg_executable,
            timeout_seconds=float(timeout_seconds),
            reuse_from_manifest=reuse_from_visual_manifest,
        )
        if visual.output_path != plan.visual_output_path:
            raise TimelineMediaRenderError("visual_output_unexpected: 视觉试点输出路径不一致")
        _run_command(plan.mux_command, cwd=plan.version_dir, timeout_seconds=float(timeout_seconds))
        _assert_file(plan.output_part_path, "成片临时输出")
        _publish_exclusive(plan.output_part_path, plan.output_path)
        _assert_file(plan.output_path, "成片输出")
        media_report = probe_audio_subtitles(
            plan.audio_path,
            plan.subtitle_path,
            ffprobe_executable=plan.ffprobe_executable,
            ffmpeg_executable=plan.ffmpeg_executable,
            timeout_seconds=float(timeout_seconds),
        )
        settings = normalized.settings
        video_report = probe_video(
            plan.output_path,
            expected=VideoTechnicalExpectation(
                width=int(settings["width"]),
                height=int(settings["height"]),
                fps=float(settings["fps"]),
                require_audio=True,
                require_subtitles=True,
                max_duration_seconds=plan.target_duration_seconds + duration_tolerance_seconds,
            ),
            ffprobe_executable=plan.ffprobe_executable,
            ffmpeg_executable=plan.ffmpeg_executable,
            timeout_seconds=float(timeout_seconds),
        )
        artifact_sha256 = _sha256_file(plan.output_path)
        visual_manifest_sha256 = _sha256_file(visual.render_manifest_path)
        report_payload = {
            "schema_version": 1,
            "version_id": plan.version_id,
            "timeline_version_id": normalized.version_id,
            "timeline_input_hash": normalized.input_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": "full_media_isolated",
            "artifact_hashes": dict(plan.artifact_hashes),
            "artifact_sha256": artifact_sha256,
            "visual_render_manifest_sha256": visual_manifest_sha256,
            "visual_rebuild": {
                "source_timeline_version_id": visual.source_version_id,
                "reused_segment_ids": list(visual.reused_segment_ids),
                "rebuilt_segment_ids": list(visual.rebuilt_segment_ids),
            },
            "video": video_report.to_dict(),
            "audio_subtitles": media_report.to_dict(),
            "publish_supported": False,
        }
        _write_exclusive_json(plan.report_path, report_payload)
        return TimelineMediaRenderResult(
            version_id=plan.version_id,
            timeline_version_id=normalized.version_id,
            timeline_input_hash=normalized.input_hash,
            output_path=plan.output_path,
            report_path=plan.report_path,
            visual_render_manifest_path=visual.render_manifest_path,
            artifact_sha256=artifact_sha256,
            reused_segment_ids=visual.reused_segment_ids,
            rebuilt_segment_ids=visual.rebuilt_segment_ids,
            source_timeline_version_id=visual.source_version_id,
            video_report=video_report,
            media_report=media_report,
        )
    except (TimelineRenderError, OSError, subprocess.SubprocessError) as exc:
        raise TimelineMediaRenderError(f"media_render_failed: {exc}") from exc


def _validated_snapshot(snapshot: TimelineSpecSnapshot | Mapping[str, Any]) -> TimelineSpecSnapshot:
    try:
        raw = snapshot.to_dict() if isinstance(snapshot, TimelineSpecSnapshot) else snapshot
        return load_timeline_spec_snapshot(raw)
    except (TimelineSpecError, TypeError, ValueError) as exc:
        raise TimelineMediaRenderError(f"timeline_invalid: {exc}") from exc


def _resolve_artifact(project: Path, entry: Mapping[str, Any], label: str) -> Path:
    relative = str(entry.get("project_relative_path") or "").replace("\\", "/")
    if not relative or relative.startswith("/") or ":" in relative:
        raise TimelineMediaRenderError(f"{label}_path_invalid: 必须是项目相对路径")
    parts = [part for part in relative.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise TimelineMediaRenderError(f"{label}_path_invalid: 不能越出项目目录")
    candidate = project / Path(*parts)
    try:
        _reject_reparse_components(project, label="project directory")
        _reject_reparse_components(candidate, label=f"{label} artifact")
    except (OSError, ProjectIdentityError) as exc:
        raise TimelineMediaRenderError(f"{label}_path_invalid: path reparse component") from exc
    path = candidate.resolve(strict=True)
    try:
        path.relative_to(project.resolve(strict=True))
    except ValueError as exc:
        raise TimelineMediaRenderError(f"{label}_path_invalid: 越出项目目录") from exc
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise TimelineMediaRenderError(f"{label}_path_invalid: 不是普通文件")
    expected = str(entry.get("sha256") or "").casefold()
    actual = _sha256_file(path)
    if expected != actual:
        raise TimelineMediaRenderError(f"{label}_hash_mismatch: artifact sha256 不匹配")
    return path


def _measure_wav(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
    except (OSError, EOFError, wave.Error) as exc:
        raise TimelineMediaRenderError(f"audio_invalid: WAV 无法解码：{exc}") from exc
    if frames <= 0 or rate <= 0:
        raise TimelineMediaRenderError("audio_invalid: WAV 参数无效")
    duration = frames / float(rate)
    if not math.isfinite(duration) or duration <= 0:
        raise TimelineMediaRenderError("audio_invalid: WAV 时长无效")
    return duration


def _timeline_duration(snapshot: TimelineSpecSnapshot) -> float:
    if not snapshot.segments:
        raise TimelineMediaRenderError("timeline_empty: 时间线为空")
    try:
        duration = float(snapshot.segments[-1]["end_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TimelineMediaRenderError("timeline_duration_invalid: 时间线时长无效") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise TimelineMediaRenderError("timeline_duration_invalid: 时间线时长无效")
    return duration


def _require_d_directory(value: Path | str, label: str, *, must_exist: bool) -> Path:
    path = Path(value)
    raw = str(path).replace("/", "\\")
    if not path.is_absolute() or len(raw) < 2 or raw[0].upper() != "D" or raw[1] != ":":
        raise TimelineMediaRenderError(f"d_drive_required: {label}必须位于 D 盘")
    if path.is_symlink():
        raise TimelineMediaRenderError(f"path_reparse: {label}不能是符号链接")
    try:
        _reject_reparse_components(path, label=label)
    except (OSError, ProjectIdentityError) as exc:
        raise TimelineMediaRenderError(f"path_reparse: {label} contains a reparse component") from exc
    resolved = path.resolve(strict=must_exist)
    if len(str(resolved)) < 2 or str(resolved)[0].upper() != "D" or str(resolved)[1] != ":":
        raise TimelineMediaRenderError(f"d_drive_required: {label}解析后不在 D 盘")
    if must_exist and (not resolved.is_dir() or resolved.is_symlink()):
        raise TimelineMediaRenderError(f"directory_invalid: {label}不是普通目录")
    return resolved


def _validate_future(path: Path, label: str) -> None:
    if path.exists() and (path.is_symlink() or not path.is_file() and not path.is_dir()):
        raise TimelineMediaRenderError(f"output_invalid: {label}已存在但类型不正确")


def _prepare_exclusive(plan: TimelineMediaRenderPlan) -> None:
    if plan.version_dir.exists():
        if any(plan.version_dir.iterdir()):
            raise TimelineMediaRenderError("output_exists: 成片版本目录拒绝复用")
    else:
        plan.version_dir.mkdir(parents=True, exist_ok=False)


def _run_command(command: tuple[str, ...], *, cwd: Path, timeout_seconds: float) -> None:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise TimelineMediaRenderError(f"ffmpeg_failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or b"").decode("utf-8", errors="replace")
        raise TimelineMediaRenderError(f"ffmpeg_failed: {detail[-1200:] or '命令失败'}")


def _assert_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise TimelineMediaRenderError(f"output_invalid: {label}不是有效普通文件")


def _publish_exclusive(part: Path, output: Path) -> None:
    if output.exists():
        raise TimelineMediaRenderError("output_exists: 拒绝覆盖已有成片")
    try:
        os.rename(part, output)
    except OSError as exc:
        raise TimelineMediaRenderError("output_publish_failed: 原子发布失败") from exc


def _write_exclusive_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise TimelineMediaRenderError("output_exists: 技术报告拒绝覆盖") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_ffprobe_executable(
    requested: Path | str | None,
    *,
    ffmpeg_executable: str,
) -> str:
    value = str(requested or "").strip()
    if value:
        return value
    discovered = shutil.which("ffprobe")
    if discovered:
        return discovered
    ffmpeg_path = Path(ffmpeg_executable)
    sibling = ffmpeg_path.with_name(f"ffprobe{ffmpeg_path.suffix}")
    if sibling.exists():
        return str(sibling)
    # The media probe will emit the actionable unavailable-tool error.
    return "ffprobe"


def _format_seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


__all__ = [
    "TimelineMediaRenderError",
    "TimelineMediaRenderPlan",
    "TimelineMediaRenderResult",
    "build_timeline_media_render_plan",
    "render_timeline_media",
]
