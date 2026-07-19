"""Fail-closed FFmpeg adapter for a versioned :class:`TimelineSpec`.

This module is a deliberately isolated pilot.  It consumes a validated
TimelineSpec snapshot and renders visual clips only.  It does not update a
pipeline step, candidate state, completion evidence, or the legacy export
service.  Audio, subtitles, visual review, and publishing remain separate
product gates until their contracts are implemented.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import imageio_ffmpeg

from app.pipeline.timeline_spec import (
    TimelineSpecError,
    TimelineSpecSnapshot,
    load_timeline_spec_snapshot,
)


_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".mkv"})
_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_EPSILON = 0.001


class TimelineRenderError(RuntimeError):
    """A render plan or isolated pilot execution was rejected."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class TimelineRenderSegment:
    clip_id: str
    segment_id: str
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    material_path: Path
    stage_path: Path
    asset_kind: str
    material_sha256: str
    render_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "segment_id": self.segment_id,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "duration_seconds": self.duration_seconds,
            "material_path": str(self.material_path),
            "stage_path": str(self.stage_path),
            "asset_kind": self.asset_kind,
            "material_sha256": self.material_sha256,
            "render_fingerprint": self.render_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class TimelineRenderPlan:
    version_id: str
    mode: str
    output_path: Path
    output_part_path: Path
    version_dir: Path
    staging_dir: Path
    manifest_path: Path
    plan_path: Path
    render_manifest_path: Path
    ffmpeg_executable: str
    settings: dict[str, Any]
    segments: tuple[TimelineRenderSegment, ...]
    segment_commands: tuple[tuple[str, ...], ...]
    concat_command: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "mode": self.mode,
            "output_path": str(self.output_path),
            "output_part_path": str(self.output_part_path),
            "version_dir": str(self.version_dir),
            "staging_dir": str(self.staging_dir),
            "manifest_path": str(self.manifest_path),
            "plan_path": str(self.plan_path),
            "render_manifest_path": str(self.render_manifest_path),
            "ffmpeg_executable": self.ffmpeg_executable,
            "settings": dict(self.settings),
            "segments": [item.to_dict() for item in self.segments],
            "segment_commands": [list(command) for command in self.segment_commands],
            "concat_command": list(self.concat_command),
            "audio": {"supported": False, "reason": "visual_only_pilot"},
            "subtitles": {"supported": False, "reason": "visual_only_pilot"},
            "publishing": {"supported": False, "reason": "isolated_pilot"},
        }


@dataclass(frozen=True, slots=True)
class TimelineRenderResult:
    version_id: str
    mode: str
    output_path: Path
    manifest_path: Path
    plan_path: Path
    render_manifest_path: Path
    reused_segment_ids: tuple[str, ...]
    rebuilt_segment_ids: tuple[str, ...]
    source_version_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "mode": self.mode,
            "output_path": str(self.output_path),
            "manifest_path": str(self.manifest_path),
            "plan_path": str(self.plan_path),
            "render_manifest_path": str(self.render_manifest_path),
            "reused_segment_ids": list(self.reused_segment_ids),
            "rebuilt_segment_ids": list(self.rebuilt_segment_ids),
            "source_version_id": self.source_version_id,
        }


@dataclass(frozen=True, slots=True)
class _ReusableSegment:
    segment_id: str
    render_fingerprint: str
    stage_path: Path
    stage_sha256: str


@dataclass(frozen=True, slots=True)
class _ReuseManifest:
    version_id: str
    segments: dict[str, _ReusableSegment]


def build_timeline_render_plan(
    snapshot: TimelineSpecSnapshot | Mapping[str, Any],
    *,
    output_root: Path | str,
    ffmpeg_executable: Path | str | None = None,
    require_candidate_refs: bool = True,
) -> TimelineRenderPlan:
    """Build a dry-run visual render plan without creating or changing files.

    The input is revalidated even when it is already a ``TimelineSpecSnapshot``
    so a caller cannot mutate the object around the hash contract.  Every
    material and every output path must resolve to the physical D: volume.
    """

    normalized = _validated_snapshot(snapshot)
    root = _require_d_directory(output_root, label="输出根目录", must_exist=False)
    version_dir = root / normalized.version_id
    staging_dir = version_dir / "staging"
    output_path = version_dir / "rendered.mp4"
    # Keep a real media suffix so FFmpeg can infer the MP4 muxer.  The
    # ``.part`` marker remains before the suffix and is removed only after the
    # exclusive publish rename.
    output_part_path = version_dir / "rendered.part.mp4"
    manifest_path = version_dir / "concat.txt"
    plan_path = version_dir / "plan.json"
    render_manifest_path = version_dir / "render-manifest.json"
    _validate_future_directory(version_dir, label="试点版本目录")
    _validate_future_directory(staging_dir, label="试点暂存目录")
    _validate_d_output(output_path, label="成片输出")
    _validate_d_output(output_part_path, label="成片临时输出")
    _validate_d_output(render_manifest_path, label="分段渲染清单")

    settings = _render_settings(normalized.settings)
    segments = _render_segments(
        normalized,
        staging_dir=staging_dir,
        version_dir=version_dir,
        settings=settings,
        require_candidate_refs=require_candidate_refs,
    )
    executable = str(ffmpeg_executable or imageio_ffmpeg.get_ffmpeg_exe()).strip()
    if not executable:
        raise TimelineRenderError("ffmpeg_unavailable", "没有可用的 FFmpeg 可执行文件")

    filter_graph = _vertical_filter(settings)
    quality_args = _quality_args(settings)
    segment_commands: list[tuple[str, ...]] = []
    for item in segments:
        input_args = (
            ("-loop", "1", "-i", str(item.material_path))
            if item.asset_kind == "image"
            else ("-stream_loop", "-1", "-i", str(item.material_path))
        )
        segment_commands.append(
            (
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                *input_args,
                "-t",
                _format_seconds(item.duration_seconds),
                "-map",
                "0:v:0",
                "-vf",
                filter_graph,
                "-an",
                "-sn",
                *quality_args,
                "-movflags",
                "+faststart",
                str(item.stage_path),
            )
        )

    concat_command = (
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(manifest_path),
        "-map",
        "0:v:0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-n",
        str(output_part_path),
    )
    return TimelineRenderPlan(
        version_id=normalized.version_id,
        mode="visual_only_pilot",
        output_path=output_path,
        output_part_path=output_part_path,
        version_dir=version_dir,
        staging_dir=staging_dir,
        manifest_path=manifest_path,
        plan_path=plan_path,
        render_manifest_path=render_manifest_path,
        ffmpeg_executable=executable,
        settings=settings,
        segments=tuple(segments),
        segment_commands=tuple(segment_commands),
        concat_command=concat_command,
    )


def render_timeline_visual_pilot(
    snapshot: TimelineSpecSnapshot | Mapping[str, Any],
    *,
    output_root: Path | str,
    ffmpeg_executable: Path | str | None = None,
    timeout_seconds: float = 180.0,
    require_candidate_refs: bool = True,
    reuse_from_manifest: Path | str | None = None,
) -> TimelineRenderResult:
    """Execute an isolated visual-only pilot and publish one new MP4.

    The version directory is exclusive: a prior pilot is never overwritten.
    The legacy project files, pipeline state, completion evidence, and
    publishing records are untouched.
    """

    if not math.isfinite(float(timeout_seconds)) or float(timeout_seconds) <= 0:
        raise TimelineRenderError("timeout_invalid", "FFmpeg 超时时间必须是正数")
    plan = build_timeline_render_plan(
        snapshot,
        output_root=output_root,
        ffmpeg_executable=ffmpeg_executable,
        require_candidate_refs=require_candidate_refs,
    )
    reuse_source = (
        _load_reuse_manifest(reuse_from_manifest)
        if reuse_from_manifest is not None
        else None
    )
    _prepare_exclusive_run(plan)
    _write_exclusive_text(plan.manifest_path, _manifest_text(plan.segments))
    _write_exclusive_json(plan.plan_path, plan.to_dict())

    reused_segment_ids: list[str] = []
    rebuilt_segment_ids: list[str] = []
    for command, segment in zip(plan.segment_commands, plan.segments, strict=True):
        reusable = (
            reuse_source.segments.get(segment.segment_id)
            if reuse_source is not None
            else None
        )
        if reusable is not None and reusable.render_fingerprint == segment.render_fingerprint:
            _copy_reused_stage(reusable, segment.stage_path)
            reused_segment_ids.append(segment.segment_id)
        else:
            _run_ffmpeg(command, cwd=plan.staging_dir, timeout_seconds=float(timeout_seconds))
            _assert_nonempty_regular_file(segment.stage_path, "FFmpeg 分段输出")
            rebuilt_segment_ids.append(segment.segment_id)

    _run_ffmpeg(
        plan.concat_command,
        cwd=plan.version_dir,
        timeout_seconds=float(timeout_seconds),
    )
    _assert_nonempty_regular_file(plan.output_part_path, "FFmpeg 汇总输出")
    _publish_exclusive(plan.output_part_path, plan.output_path)
    _assert_nonempty_regular_file(plan.output_path, "试点成片")
    _write_exclusive_json(
        plan.render_manifest_path,
        _render_manifest_payload(
            plan,
            source_version_id=(reuse_source.version_id if reuse_source is not None else None),
            reused_segment_ids=tuple(reused_segment_ids),
            rebuilt_segment_ids=tuple(rebuilt_segment_ids),
        ),
    )
    return TimelineRenderResult(
        version_id=plan.version_id,
        mode=plan.mode,
        output_path=plan.output_path,
        manifest_path=plan.manifest_path,
        plan_path=plan.plan_path,
        render_manifest_path=plan.render_manifest_path,
        reused_segment_ids=tuple(reused_segment_ids),
        rebuilt_segment_ids=tuple(rebuilt_segment_ids),
        source_version_id=(reuse_source.version_id if reuse_source is not None else None),
    )


def _validated_snapshot(
    snapshot: TimelineSpecSnapshot | Mapping[str, Any],
) -> TimelineSpecSnapshot:
    try:
        raw = snapshot.to_dict() if isinstance(snapshot, TimelineSpecSnapshot) else snapshot
        return load_timeline_spec_snapshot(raw)
    except (TimelineSpecError, TypeError, ValueError) as exc:
        detail = str(exc)
        code = "timeline_hash_invalid" if "哈希" in detail else "timeline_invalid"
        raise TimelineRenderError(code, detail) from exc


def _render_settings(raw: Mapping[str, Any]) -> dict[str, Any]:
    defaults = {"width": 1080, "height": 1920, "fps": 30, "sample_rate": 48000, "aspect_ratio": "9:16"}
    settings = dict(defaults)
    settings.update(dict(raw))
    for key, minimum, maximum in (
        ("width", 16, 7680),
        ("height", 16, 7680),
        ("fps", 1, 120),
        ("sample_rate", 8000, 192000),
    ):
        value = settings.get(key)
        if type(value) is not int or not minimum <= value <= maximum:
            raise TimelineRenderError("settings_invalid", f"TimelineSpec {key} 设置无效")
    if settings.get("aspect_ratio") != "9:16":
        raise TimelineRenderError("settings_invalid", "试点只支持 9:16 竖屏设置")
    return settings


def _render_segments(
    snapshot: TimelineSpecSnapshot,
    *,
    staging_dir: Path,
    version_dir: Path,
    settings: Mapping[str, Any],
    require_candidate_refs: bool,
) -> list[TimelineRenderSegment]:
    if not snapshot.segments:
        raise TimelineRenderError("timeline_empty", "TimelineSpec 没有可渲染镜头")
    _validate_tracks(snapshot)
    segments: list[TimelineRenderSegment] = []
    expected_start = 0.0
    for index, raw in enumerate(snapshot.segments, start=1):
        try:
            start = float(raw["start_seconds"])
            end = float(raw["end_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TimelineRenderError("segment_timing_invalid", "镜头时间字段无效") from exc
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise TimelineRenderError("segment_timing_invalid", "镜头结束时间必须晚于开始时间")
        if abs(start - expected_start) > _EPSILON:
            raise TimelineRenderError("timeline_non_contiguous", "TimelineSpec 镜头时间必须连续")
        segment_id = str(raw.get("segment_id") or "").strip()
        clip_id = str(raw.get("clip_id") or "").strip()
        if not segment_id or not clip_id:
            raise TimelineRenderError("segment_identity_invalid", "镜头缺少稳定 ID")
        if require_candidate_refs and not str(raw.get("selected_candidate_id") or "").strip():
            raise TimelineRenderError(
                "candidate_reference_missing",
                f"镜头 {segment_id} 缺少候选引用；锁定状态必须由上游导出闸门证明",
            )
        material = str(raw.get("material_path") or "").strip()
        if not material:
            raise TimelineRenderError("material_missing", f"镜头 {segment_id} 没有素材路径")
        material_path = _require_d_file(material, label=f"镜头 {segment_id} 素材", must_exist=True)
        if material_path.is_relative_to(version_dir.resolve()):
            raise TimelineRenderError("material_output_collision", "素材不能位于当前试点输出目录")
        extension = material_path.suffix.lower()
        if extension in _IMAGE_EXTENSIONS:
            asset_kind = "image"
        elif extension in _VIDEO_EXTENSIONS:
            asset_kind = "video"
        else:
            raise TimelineRenderError("material_type_unsupported", f"不支持的素材格式：{extension or '无扩展名'}")
        duration = round(end - start, 3)
        material_sha256 = _sha256_regular_file(
            material_path,
            label=f"镜头 {segment_id} 素材",
        )
        render_fingerprint = _segment_render_fingerprint(
            raw,
            material_sha256=material_sha256,
            asset_kind=asset_kind,
            duration_seconds=duration,
            settings=settings,
        )
        segments.append(
            TimelineRenderSegment(
                clip_id=clip_id,
                segment_id=segment_id,
                start_seconds=round(start, 3),
                end_seconds=round(end, 3),
                duration_seconds=duration,
                material_path=material_path,
                stage_path=staging_dir / f"{index:04d}-{_safe_name(segment_id)}.mp4",
                asset_kind=asset_kind,
                material_sha256=material_sha256,
                render_fingerprint=render_fingerprint,
            )
        )
        expected_start = end
    return segments


def _validate_tracks(snapshot: TimelineSpecSnapshot) -> None:
    expected_clip_ids = [str(item.get("clip_id") or "") for item in snapshot.segments]
    video_tracks = [item for item in snapshot.tracks if item.get("kind") == "video"]
    if len(video_tracks) != 1:
        raise TimelineRenderError("tracks_invalid", "TimelineSpec 必须有且只有一个 video track")
    if list(video_tracks[0].get("clip_ids") or []) != expected_clip_ids:
        raise TimelineRenderError("tracks_invalid", "video track 顺序与镜头顺序不一致")
    for track in snapshot.tracks:
        if track.get("kind") in {"voiceover", "subtitle"} and track.get("clip_ids"):
            raise TimelineRenderError("pilot_scope_unsupported", "视觉试点不消费音频或字幕 clip")


def _vertical_filter(settings: Mapping[str, Any]) -> str:
    width = settings["width"]
    height = settings["height"]
    fps = settings["fps"]
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={width}:{height},fps={fps},format=yuv420p"
    )


def _quality_args(settings: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-maxrate",
        "12M",
        "-bufsize",
        "24M",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(settings["fps"]),
    )


def _manifest_text(segments: tuple[TimelineRenderSegment, ...]) -> str:
    return "".join(f"file '{_concat_escape(item.stage_path)}'\n" for item in segments)


def _concat_escape(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")


def _format_seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _safe_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in value)
    return cleaned[:80] or "segment"


def _segment_render_fingerprint(
    raw: Mapping[str, Any],
    *,
    material_sha256: str,
    asset_kind: str,
    duration_seconds: float,
    settings: Mapping[str, Any],
) -> str:
    scene_fields = {
        key: raw.get(key)
        for key in (
            "clip_id",
            "segment_id",
            "visual_type",
            "visual_direction",
            "voiceover",
            "selected_candidate_id",
            "transform",
            "subtitle",
            "edit_instruction",
        )
    }
    return _canonical_sha256(
        {
            "renderer_contract": "timeline-visual-v1",
            "scene": scene_fields,
            "material_sha256": material_sha256,
            "asset_kind": asset_kind,
            "duration_seconds": duration_seconds,
            "settings": dict(settings),
        }
    )


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_regular_file(path: Path, *, label: str) -> str:
    try:
        before = path.stat()
        if path.is_symlink() or not path.is_file() or before.st_size <= 0:
            raise TimelineRenderError("file_invalid", f"{label} 不是非空普通文件")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
    except TimelineRenderError:
        raise
    except OSError as exc:
        raise TimelineRenderError("file_unreadable", f"{label} 无法安全读取") from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_identity != after_identity:
        raise TimelineRenderError("file_changed", f"{label} 在计算哈希时发生变化")
    return digest.hexdigest()


def _render_manifest_payload(
    plan: TimelineRenderPlan,
    *,
    source_version_id: str | None,
    reused_segment_ids: tuple[str, ...],
    rebuilt_segment_ids: tuple[str, ...],
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for index, segment in enumerate(plan.segments, start=1):
        _assert_nonempty_regular_file(segment.stage_path, "分段渲染缓存")
        entries.append(
            {
                "index": index,
                "segment_id": segment.segment_id,
                "clip_id": segment.clip_id,
                "render_fingerprint": segment.render_fingerprint,
                "material_sha256": segment.material_sha256,
                "stage_relative_path": segment.stage_path.relative_to(plan.version_dir).as_posix(),
                "stage_sha256": _sha256_regular_file(
                    segment.stage_path,
                    label=f"镜头 {segment.segment_id} 分段缓存",
                ),
            }
        )
    return {
        "schema_version": 1,
        "renderer_contract": "timeline-visual-v1",
        "timeline_version_id": plan.version_id,
        "settings_sha256": _canonical_sha256(plan.settings),
        "source_version_id": source_version_id,
        "reused_segment_ids": list(reused_segment_ids),
        "rebuilt_segment_ids": list(rebuilt_segment_ids),
        "segments": entries,
        "output_sha256": _sha256_regular_file(plan.output_path, label="视觉成片"),
    }


def _load_reuse_manifest(value: Path | str) -> _ReuseManifest:
    manifest_path = _require_d_file(str(value), label="复用渲染清单", must_exist=True)
    if manifest_path.name != "render-manifest.json":
        raise TimelineRenderError("reuse_manifest_invalid", "复用来源不是渲染清单")
    try:
        if manifest_path.stat().st_size > 2 * 1024 * 1024:
            raise TimelineRenderError("reuse_manifest_invalid", "复用渲染清单过大")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except TimelineRenderError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TimelineRenderError("reuse_manifest_invalid", "复用渲染清单无法读取") from exc
    if not isinstance(payload, dict):
        raise TimelineRenderError("reuse_manifest_invalid", "复用渲染清单格式无效")
    version_id = str(payload.get("timeline_version_id") or "").strip()
    rows = payload.get("segments")
    if (
        payload.get("schema_version") != 1
        or payload.get("renderer_contract") != "timeline-visual-v1"
        or not version_id
        or not isinstance(rows, list)
        or not rows
    ):
        raise TimelineRenderError("reuse_manifest_invalid", "复用渲染清单合同不匹配")
    output_hash = str(payload.get("output_sha256") or "").casefold()
    output_path = manifest_path.parent / "rendered.mp4"
    if not _is_sha256(output_hash) or _sha256_regular_file(output_path, label="复用视觉成片") != output_hash:
        raise TimelineRenderError("reuse_manifest_invalid", "复用视觉成片哈希不匹配")
    segments: dict[str, _ReusableSegment] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise TimelineRenderError("reuse_manifest_invalid", "复用分段记录格式无效")
        segment_id = str(row.get("segment_id") or "").strip()
        fingerprint = str(row.get("render_fingerprint") or "").casefold()
        stage_hash = str(row.get("stage_sha256") or "").casefold()
        relative = str(row.get("stage_relative_path") or "").replace("\\", "/")
        if (
            not segment_id
            or segment_id in segments
            or not _is_sha256(fingerprint)
            or not _is_sha256(stage_hash)
        ):
            raise TimelineRenderError("reuse_manifest_invalid", "复用分段身份或哈希无效")
        parts = [part for part in relative.split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts) or ":" in relative:
            raise TimelineRenderError("reuse_manifest_invalid", "复用分段路径无效")
        stage_path = manifest_path.parent.joinpath(*parts)
        _assert_safe_ancestors(stage_path, label="复用分段缓存")
        try:
            stage_path = stage_path.resolve(strict=True)
            stage_path.relative_to(manifest_path.parent.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise TimelineRenderError("reuse_manifest_invalid", "复用分段路径越出版本目录") from exc
        if _sha256_regular_file(stage_path, label=f"复用镜头 {segment_id} 分段") != stage_hash:
            raise TimelineRenderError("reuse_manifest_invalid", "复用分段文件哈希不匹配")
        segments[segment_id] = _ReusableSegment(
            segment_id=segment_id,
            render_fingerprint=fingerprint,
            stage_path=stage_path,
            stage_sha256=stage_hash,
        )
    return _ReuseManifest(version_id=version_id, segments=segments)


def _copy_reused_stage(source: _ReusableSegment, destination: Path) -> None:
    try:
        with source.stage_path.open("rb") as input_handle, destination.open("xb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
            output_handle.flush()
            os.fsync(output_handle.fileno())
    except FileExistsError as exc:
        raise TimelineRenderError("reuse_publish_conflict", "复用分段目标已存在") from exc
    except OSError as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise TimelineRenderError("reuse_copy_failed", "复用分段复制失败") from exc
    if _sha256_regular_file(destination, label=f"复用镜头 {source.segment_id} 分段") != source.stage_sha256:
        raise TimelineRenderError("reuse_copy_failed", "复用分段复制后哈希不匹配")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value.casefold())
    )


def _require_d_directory(path: Path | str, *, label: str, must_exist: bool) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or _drive(candidate) != "D:":
        raise TimelineRenderError("d_drive_required", f"{label} 必须位于 D 盘")
    _assert_safe_ancestors(candidate, label=label)
    resolved = candidate.resolve(strict=False)
    if _drive(resolved) != "D:":
        raise TimelineRenderError("d_drive_required", f"{label} 解析后不在 D 盘")
    if must_exist and not resolved.is_dir():
        raise TimelineRenderError("directory_missing", f"{label} 不存在")
    if resolved.exists() and (resolved.is_symlink() or not resolved.is_dir()):
        raise TimelineRenderError("directory_invalid", f"{label} 不是普通目录")
    return resolved


def _validate_future_directory(path: Path, *, label: str) -> None:
    _assert_safe_ancestors(path, label=label)
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise TimelineRenderError("directory_invalid", f"{label} 不是普通目录")


def _validate_d_output(path: Path, *, label: str) -> None:
    if _drive(path) != "D:":
        raise TimelineRenderError("d_drive_required", f"{label} 必须位于 D 盘")
    _assert_safe_ancestors(path, label=label)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise TimelineRenderError("output_invalid", f"{label} 已存在但不是普通文件")


def _require_d_file(value: str, *, label: str, must_exist: bool) -> Path:
    path = Path(value)
    if not path.is_absolute() or _drive(path) != "D:":
        raise TimelineRenderError("d_drive_required", f"{label} 必须位于 D 盘")
    _assert_safe_ancestors(path, label=label)
    try:
        resolved = path.resolve(strict=must_exist)
    except OSError as exc:
        raise TimelineRenderError("material_missing", f"{label} 无法解析") from exc
    if _drive(resolved) != "D:":
        raise TimelineRenderError("d_drive_required", f"{label} 解析后不在 D 盘")
    if must_exist and (not resolved.is_file() or resolved.is_symlink()):
        raise TimelineRenderError("material_invalid", f"{label} 必须是普通文件")
    return resolved


def _drive(path: Path) -> str:
    raw = str(path).replace("/", "\\")
    return raw[:2].upper() if len(raw) >= 2 and raw[1] == ":" else ""


def _assert_safe_ancestors(path: Path, *, label: str) -> None:
    """Reject symlink/junction ancestors before resolving a D: path."""

    current = path
    while True:
        if current.exists() and _is_reparse_or_link(current):
            raise TimelineRenderError("path_reparse", f"{label} 路径包含符号链接或重解析点")
        parent = current.parent
        if parent == current:
            break
        current = parent


def _is_reparse_or_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & 0x0400)
    except OSError as exc:
        raise TimelineRenderError("path_unreadable", f"无法读取路径属性：{path}") from exc


def _prepare_exclusive_run(plan: TimelineRenderPlan) -> None:
    root = _require_d_directory(plan.version_dir.parent, label="输出根目录", must_exist=False)
    if (
        plan.output_path.exists()
        or plan.output_part_path.exists()
        or plan.plan_path.exists()
        or plan.render_manifest_path.exists()
    ):
        raise TimelineRenderError("pilot_exists", "该 TimelineSpec 已有试点输出，拒绝覆盖")
    if plan.version_dir.exists() and any(plan.version_dir.iterdir()):
        raise TimelineRenderError("pilot_exists", "该 TimelineSpec 试点目录已有文件，拒绝复用")
    for directory in (root, plan.version_dir, plan.staging_dir):
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise TimelineRenderError("directory_invalid", "试点目录不是普通目录")
        else:
            directory.mkdir(parents=True, exist_ok=False)


def _write_exclusive_text(path: Path, text: str) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise TimelineRenderError("pilot_exists", f"拒绝覆盖已有文件：{path}") from exc
    except OSError as exc:
        raise TimelineRenderError("pilot_write_failed", f"无法写入试点文件：{path}") from exc


def _write_exclusive_json(path: Path, payload: Mapping[str, Any]) -> None:
    _write_exclusive_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _run_ffmpeg(command: tuple[str, ...], *, cwd: Path, timeout_seconds: float) -> None:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        raise TimelineRenderError("ffmpeg_unavailable", "FFmpeg 进程无法启动") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimelineRenderError("ffmpeg_timeout", "FFmpeg 试点超时") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or b"").decode("utf-8", errors="replace")
        raise TimelineRenderError("ffmpeg_failed", detail.strip()[-1200:] or "FFmpeg 失败")


def _assert_nonempty_regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise TimelineRenderError("ffmpeg_output_invalid", f"{label} 不是有效普通文件")


def _publish_exclusive(part_path: Path, output_path: Path) -> None:
    if output_path.exists():
        raise TimelineRenderError("output_exists", "拒绝覆盖已有成片")
    try:
        os.rename(part_path, output_path)
    except FileExistsError as exc:
        raise TimelineRenderError("output_exists", "成片发布发生并发冲突") from exc
    except OSError as exc:
        raise TimelineRenderError("output_publish_failed", "无法原子发布试点成片") from exc
