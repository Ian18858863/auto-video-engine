"""Application seam for binding measured media to the immutable timeline.

The editing exporter remains the compatibility path.  This module owns the
explicit next step: an operator chooses a measured audio/subtitle artifact,
the service appends a new TimelineSpec version with those references, and an
isolated renderer may consume that version.  No completion evidence or
publishing state is changed here.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_configured_path
from app.models import ProductionPipeline
from app.pipeline.audio_subtitle_artifact_service import (
    AudioSubtitleArtifact,
    AudioSubtitleArtifactError,
    load_pipeline_audio_subtitle_artifact,
)
from app.pipeline.project_identity import (
    ProjectIdentityError,
    begin_prepared_project_directory_use,
)
from app.pipeline.timeline_spec import (
    TimelineSpecError,
    TimelineSpecLedger,
    TimelineSpecSnapshot,
)
from app.video.timeline_media_renderer import (
    TimelineMediaRenderError,
    TimelineMediaRenderResult,
    render_timeline_media,
)


class TimelineMediaServiceError(ValueError):
    """Raised when an explicit media binding/render request is rejected."""


@dataclass(frozen=True, slots=True)
class TimelineMediaBinding:
    pipeline_id: int
    artifact_id: str
    timeline_spec: TimelineSpecSnapshot
    artifact: AudioSubtitleArtifact

    def to_dict(self, *, project_dir: Path) -> dict[str, Any]:
        artifact_public = self.artifact.to_public_dict(project_dir=project_dir)
        return {
            "pipeline_id": self.pipeline_id,
            "artifact_id": self.artifact_id,
            "timeline_spec": _public_timeline_spec(
                self.timeline_spec,
                project_dir=project_dir,
            ),
            "artifact": artifact_public,
            "publish_supported": False,
        }


@dataclass(frozen=True, slots=True)
class TimelineMediaRun:
    binding: TimelineMediaBinding
    render: TimelineMediaRenderResult

    def to_dict(self, *, project_dir: Path) -> dict[str, Any]:
        return {
            "binding": self.binding.to_dict(project_dir=project_dir),
            "render": _public_render_result(self.render, project_dir=project_dir),
            "publish_supported": False,
        }


def bind_pipeline_audio_subtitle_artifact(
    session: Session,
    pipeline_id: int,
    artifact_id: str,
    *,
    project_root: Path | None = None,
    timeline_version_id: str | None = None,
    duration_tolerance_seconds: float = 0.15,
) -> TimelineMediaBinding:
    """Append a TimelineSpec version that references one verified artifact."""

    pipeline, project_dir, ledger = _pipeline_context(
        session, pipeline_id, project_root=project_root
    )
    if not math.isfinite(float(duration_tolerance_seconds)) or duration_tolerance_seconds < 0:
        raise TimelineMediaServiceError("duration tolerance must be finite and non-negative")
    try:
        snapshot = ledger.latest()
        if snapshot is None:
            raise TimelineMediaServiceError(
                "timeline spec is missing; rebuild the editing timeline first"
            )
        if timeline_version_id and str(timeline_version_id) != snapshot.version_id:
            raise TimelineMediaServiceError(
                "selected timeline is not the current timeline; rebuild or refresh before rendering"
            )
        artifact = load_pipeline_audio_subtitle_artifact(
            session,
            pipeline_id,
            artifact_id,
            project_root=project_root,
        )
        timeline_duration = _timeline_duration(snapshot)
        if abs(artifact.audio_duration_seconds - timeline_duration) > float(
            duration_tolerance_seconds
        ):
            raise TimelineMediaServiceError(
                "audio duration does not match the selected timeline version"
            )
        payload = {
            "tracks": deepcopy(list(snapshot.tracks)),
            "segments": deepcopy(list(snapshot.segments)),
            "settings": deepcopy(snapshot.settings),
            "artifacts": _artifact_references(artifact, project_dir=project_dir),
        }
        bound = ledger.create(payload, source="audio_subtitle_binding")
        return TimelineMediaBinding(
            pipeline_id=int(getattr(pipeline, "id")),
            artifact_id=artifact.artifact_id,
            timeline_spec=bound,
            artifact=artifact,
        )
    except TimelineMediaServiceError:
        raise
    except (AudioSubtitleArtifactError, TimelineSpecError) as exc:
        raise TimelineMediaServiceError(str(exc)) from exc
    except (ProjectIdentityError, OSError, ValueError) as exc:
        raise TimelineMediaServiceError(f"timeline media binding failed: {exc}") from exc


def render_pipeline_timeline_media(
    session: Session,
    pipeline_id: int,
    artifact_id: str,
    *,
    project_root: Path | None = None,
    timeline_version_id: str | None = None,
    ffmpeg_executable: Path | str | None = None,
    ffprobe_executable: Path | str | None = None,
    timeout_seconds: float = 180.0,
    duration_tolerance_seconds: float = 0.15,
) -> TimelineMediaRun:
    """Bind, render and verify one isolated full-media draft version."""

    binding = _bind_audio_first_timeline(
        session,
        pipeline_id,
        artifact_id,
        project_root=project_root,
        timeline_version_id=timeline_version_id,
        duration_tolerance_seconds=duration_tolerance_seconds,
    )
    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None or not pipeline.project_dir:
        raise TimelineMediaServiceError("pipeline project directory is missing")
    project_dir = Path(pipeline.project_dir).resolve(strict=True)
    output_root = project_dir / "\u6210\u7247" / "versions"
    reuse_from_visual_manifest = _latest_verified_visual_manifest(output_root)
    try:
        rendered = render_timeline_media(
            binding.timeline_spec,
            project_dir=project_dir,
            output_root=output_root,
            ffmpeg_executable=ffmpeg_executable,
            ffprobe_executable=ffprobe_executable,
            timeout_seconds=timeout_seconds,
            duration_tolerance_seconds=duration_tolerance_seconds,
            reuse_from_visual_manifest=reuse_from_visual_manifest,
        )
    except TimelineMediaRenderError as exc:
        raise TimelineMediaServiceError(str(exc)) from exc
    return TimelineMediaRun(binding=binding, render=rendered)


def _bind_audio_first_timeline(
    session: Session,
    pipeline_id: int,
    artifact_id: str,
    *,
    project_root: Path | None,
    timeline_version_id: str | None,
    duration_tolerance_seconds: float,
) -> TimelineMediaBinding:
    """Append the renderable timeline only after WAV duration is measured.

    Editing owns the planned scene proportions.  This render seam owns the
    transition from that plan to real media timing: it verifies the caller is
    still on the latest plan, scales every scene boundary to the decoded WAV,
    and then binds the immutable audio/subtitle references.  The standalone
    binding API deliberately stays strict for callers that claim their
    timeline is already measured.
    """

    pipeline, project_dir, ledger = _pipeline_context(
        session,
        pipeline_id,
        project_root=project_root,
    )
    if (
        not math.isfinite(float(duration_tolerance_seconds))
        or duration_tolerance_seconds < 0
    ):
        raise TimelineMediaServiceError(
            "duration tolerance must be finite and non-negative"
        )
    try:
        snapshot = ledger.latest()
        if snapshot is None:
            raise TimelineMediaServiceError(
                "timeline spec is missing; rebuild the editing timeline first"
            )
        if timeline_version_id and str(timeline_version_id) != snapshot.version_id:
            raise TimelineMediaServiceError(
                "selected timeline is not the current timeline; rebuild or refresh before rendering"
            )
        artifact = load_pipeline_audio_subtitle_artifact(
            session,
            pipeline_id,
            artifact_id,
            project_root=project_root,
        )
        payload = {
            "tracks": deepcopy(list(snapshot.tracks)),
            "segments": _retime_segments_to_duration(
                snapshot.segments,
                artifact.audio_duration_seconds,
            ),
            "settings": deepcopy(snapshot.settings),
            "artifacts": _artifact_references(artifact, project_dir=project_dir),
        }
        bound = ledger.create(payload, source="audio_first_timing")
        return TimelineMediaBinding(
            pipeline_id=int(getattr(pipeline, "id")),
            artifact_id=artifact.artifact_id,
            timeline_spec=bound,
            artifact=artifact,
        )
    except TimelineMediaServiceError:
        raise
    except (AudioSubtitleArtifactError, TimelineSpecError) as exc:
        raise TimelineMediaServiceError(str(exc)) from exc
    except (ProjectIdentityError, OSError, ValueError) as exc:
        raise TimelineMediaServiceError(
            f"timeline media binding failed: {exc}"
        ) from exc


def _artifact_references(
    artifact: AudioSubtitleArtifact,
    *,
    project_dir: Path,
) -> dict[str, Any]:
    public = artifact.to_public_dict(project_dir=project_dir)
    return {
        "artifact_id": artifact.artifact_id,
        "manifest_project_relative_path": public["manifest_project_relative_path"],
        "provider": public["provider"],
        "created_at": public["created_at"],
        "voiceover": deepcopy(public["audio"]),
        "subtitles": deepcopy(public["subtitles"]),
    }


def _retime_segments_to_duration(
    segments: tuple[dict[str, Any], ...],
    target_duration_seconds: float,
) -> list[dict[str, Any]]:
    if not segments:
        raise TimelineMediaServiceError("timeline spec has no segments")
    target = float(target_duration_seconds)
    if not math.isfinite(target) or target <= 0:
        raise TimelineMediaServiceError("measured audio duration is invalid")
    planned = _timeline_duration_from_segments(segments)
    scale = target / planned
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(segments):
        item = deepcopy(raw)
        try:
            original_start = float(item["start_seconds"])
            original_end = float(item["end_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TimelineMediaServiceError("timeline segment timing is invalid") from exc
        start = 0.0 if index == 0 else original_start * scale
        end = target if index == len(segments) - 1 else original_end * scale
        if not all(math.isfinite(value) for value in (start, end)) or end <= start:
            raise TimelineMediaServiceError("retimed timeline segment is invalid")
        item["start_seconds"] = start
        item["end_seconds"] = end
        item["start"] = _format_timeline_seconds(start)
        item["end"] = _format_timeline_seconds(end)
        result.append(item)
    return result


def _timeline_duration_from_segments(segments: tuple[dict[str, Any], ...]) -> float:
    try:
        duration = float(segments[-1]["end_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TimelineMediaServiceError("timeline duration is invalid") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise TimelineMediaServiceError("timeline duration is invalid")
    return duration


def _format_timeline_seconds(value: float) -> str:
    milliseconds = int(round(value * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, fraction = divmod(remainder, 1_000)
    prefix = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return prefix if fraction == 0 else f"{prefix}.{fraction:03d}".rstrip("0")


def _pipeline_context(
    session: Session,
    pipeline_id: int,
    *,
    project_root: Path | None,
) -> tuple[ProductionPipeline, Path, TimelineSpecLedger]:
    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None:
        raise TimelineMediaServiceError(f"pipeline not found: {pipeline_id}")
    if not pipeline.project_dir:
        raise TimelineMediaServiceError("pipeline project directory is missing")
    approved_root = resolve_configured_path(project_root or get_settings().project_root_dir)
    try:
        project_use = begin_prepared_project_directory_use(
            pipeline,
            project_root=approved_root,
        )
        project_use.verify()
    except (ProjectIdentityError, OSError, ValueError) as exc:
        raise TimelineMediaServiceError(f"project identity check failed: {exc}") from exc
    return pipeline, project_use.path, TimelineSpecLedger(
        pipeline,
        project_root=approved_root,
    )


def _timeline_duration(snapshot: TimelineSpecSnapshot) -> float:
    if not snapshot.segments:
        raise TimelineMediaServiceError("timeline spec has no segments")
    try:
        duration = float(snapshot.segments[-1]["end_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TimelineMediaServiceError("timeline duration is invalid") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise TimelineMediaServiceError("timeline duration is invalid")
    return duration


def _public_render_result(
    result: TimelineMediaRenderResult,
    *,
    project_dir: Path,
) -> dict[str, Any]:
    """Strip physical paths from renderer reports before returning an API DTO."""

    video = result.video_report.to_dict()
    media = result.media_report.to_dict()
    for report in (video, media):
        for key in ("path", "audio_path", "subtitle_path"):
            if key in report and report[key]:
                report[key] = _relative_path(project_dir, Path(str(report[key])))
        if "probe_source" in report:
            report["probe_source"] = "ffprobe"
    return {
        "version_id": result.version_id,
        "timeline_version_id": result.timeline_version_id,
        "timeline_input_hash": result.timeline_input_hash,
        "mode": "full_media_isolated",
        "output_project_relative_path": _relative_path(project_dir, result.output_path),
        "report_project_relative_path": _relative_path(project_dir, result.report_path),
        "visual_render_manifest_project_relative_path": _relative_path(
            project_dir,
            result.visual_render_manifest_path,
        ),
        "artifact_sha256": result.artifact_sha256,
        "visual_rebuild": {
            "source_timeline_version_id": result.source_timeline_version_id,
            "reused_segment_ids": list(result.reused_segment_ids),
            "rebuilt_segment_ids": list(result.rebuilt_segment_ids),
        },
        "video_report": video,
        "media_report": media,
    }


def _public_timeline_spec(
    snapshot: TimelineSpecSnapshot,
    *,
    project_dir: Path,
) -> dict[str, Any]:
    """Keep the browser contract free of physical material paths."""

    payload = snapshot.public_dict()
    segments = payload.get("segments")
    if isinstance(segments, list):
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            raw_path = str(segment.get("material_path") or "").strip()
            if not raw_path:
                continue
            path = Path(raw_path)
            if path.is_absolute():
                try:
                    segment["material_path"] = _relative_path(project_dir, path)
                except TimelineMediaServiceError:
                    segment["material_path"] = ""
            else:
                segment["material_path"] = PurePosixPath(
                    raw_path.replace("\\", "/")
                ).as_posix()
    return payload


def _relative_path(project_dir: Path, path: Path) -> str:
    try:
        relative = path.resolve(strict=False).relative_to(project_dir.resolve(strict=False))
    except ValueError as exc:
        raise TimelineMediaServiceError("renderer path escaped the project directory") from exc
    return PurePosixPath(relative.as_posix()).as_posix()


def _latest_verified_visual_manifest(output_root: Path) -> Path | None:
    """Return the newest complete visual manifest, never a partial run."""

    if not output_root.exists():
        return None
    try:
        root = output_root.resolve(strict=True)
    except OSError:
        return None
    if root.is_symlink() or not root.is_dir():
        return None
    candidates: list[tuple[int, str, Path]] = []
    try:
        run_dirs = list(root.iterdir())
    except OSError:
        return None
    for run_dir in run_dirs:
        try:
            if run_dir.is_symlink() or not run_dir.is_dir():
                continue
            run = run_dir.resolve(strict=True)
            run.relative_to(root)
            report_path = run / "technical-report.json"
            artifact_path = run / "rendered.mp4"
            if not _plain_file(report_path, max_bytes=1024 * 1024) or not _plain_file(
                artifact_path,
                max_bytes=None,
            ):
                continue
            report_bytes = report_path.read_bytes()
            if report_bytes.startswith(b"\xef\xbb\xbf"):
                continue
            report = json.loads(report_bytes.decode("utf-8", errors="strict"))
            if not isinstance(report, dict) or report.get("schema_version") != 1:
                continue
            timeline_version_id = str(report.get("timeline_version_id") or "").strip()
            artifact_sha256 = str(report.get("artifact_sha256") or "").casefold()
            visual_sha256 = str(
                report.get("visual_render_manifest_sha256") or ""
            ).casefold()
            if (
                not timeline_version_id
                or not _is_sha256(artifact_sha256)
                or not _is_sha256(visual_sha256)
                or _sha256_file(artifact_path) != artifact_sha256
            ):
                continue
            visual_manifest = (
                run
                / "visual-root"
                / timeline_version_id
                / "render-manifest.json"
            )
            if not _plain_file(visual_manifest, max_bytes=2 * 1024 * 1024):
                continue
            visual_manifest = visual_manifest.resolve(strict=True)
            visual_manifest.relative_to(run)
            if _sha256_file(visual_manifest) != visual_sha256:
                continue
            candidates.append(
                (report_path.stat().st_mtime_ns, run.name, visual_manifest)
            )
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ):
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def _plain_file(path: Path, *, max_bytes: int | None) -> bool:
    try:
        stat = path.stat()
        return (
            path.is_file()
            and not path.is_symlink()
            and stat.st_nlink == 1
            and stat.st_size > 0
            and (max_bytes is None or stat.st_size <= max_bytes)
        )
    except OSError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value.casefold())
    )


__all__ = [
    "TimelineMediaBinding",
    "TimelineMediaRun",
    "TimelineMediaServiceError",
    "bind_pipeline_audio_subtitle_artifact",
    "render_pipeline_timeline_media",
]
