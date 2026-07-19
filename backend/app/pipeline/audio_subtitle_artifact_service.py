"""Append-only, measured voiceover and subtitle artifacts.

This is the first real-media bridge for the refactor.  It is deliberately
route-free and does not touch the legacy ``口播音频.wav``/``字幕.srt`` files:
one invocation gets its own version directory under the prepared D: project,
decodes the generated WAV, fits SRT cues to that measured duration, and writes
one immutable manifest containing project-relative paths and hashes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import wave
from typing import Any, Callable, Mapping
from uuid import uuid4

from sqlalchemy.orm import Session

from app.audio.subtitle_service import (
    build_srt_from_segments,
    build_subtitle_segments_for_duration,
)
from app.audio.tts_service import TTSResult, synthesize_tts
from app.core.config import get_settings, resolve_configured_path
from app.models import ProductionPipeline, ScriptAsset
from app.pipeline.project_identity import (
    ProjectIdentityError,
    begin_prepared_project_child_directory_use,
    prepared_project_output_path,
    write_prepared_project_files,
)


class AudioSubtitleArtifactError(RuntimeError):
    """Raised when a real audio/subtitle artifact cannot be established."""


@dataclass(frozen=True, slots=True)
class AudioSubtitleArtifact:
    artifact_id: str
    audio_path: Path
    subtitle_path: Path
    manifest_path: Path
    audio_duration_seconds: float
    audio_sha256: str
    subtitle_sha256: str
    provider: str
    created_at: str

    def to_public_dict(self, *, project_dir: Path) -> dict[str, Any]:
        """Return a path-safe public contract (never an absolute path)."""

        root = project_dir.resolve(strict=True)
        return {
            "artifact_id": self.artifact_id,
            "audio": {
                "project_relative_path": _relative_project_path(root, self.audio_path),
                "sha256": self.audio_sha256,
                "duration_seconds": self.audio_duration_seconds,
                "mime": "audio/wav",
            },
            "subtitles": {
                "project_relative_path": _relative_project_path(root, self.subtitle_path),
                "sha256": self.subtitle_sha256,
                "mime": "application/x-subrip",
            },
            "manifest_project_relative_path": _relative_project_path(root, self.manifest_path),
            "provider": self.provider,
            "created_at": self.created_at,
        }


TTSCallable = Callable[..., TTSResult]
_ARTIFACT_ID = re.compile(r"^as-[0-9]+-[a-f0-9]{16}$")
_AUDIO_DIR = Path("素材信息") / "音频字幕版本"


def build_pipeline_audio_subtitle_artifact(
    session: Session,
    pipeline_id: int,
    *,
    provider: str | None = None,
    project_root: Path | None = None,
    synthesizer: TTSCallable | None = None,
    artifact_id: str | None = None,
) -> AudioSubtitleArtifact:
    """Generate one measured WAV/SRT pair in a new immutable project version.

    The default TTS provider is the existing local adapter.  Tests and future
    providers can inject a callable with the same ``synthesize_tts`` shape;
    no fake bytes are accepted because the WAV header is decoded before the
    artifact is published.
    """

    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None:
        raise AudioSubtitleArtifactError(f"流程不存在：{pipeline_id}")
    if pipeline.script_asset_id is None:
        raise AudioSubtitleArtifactError("流程还没有脚本，不能生成音频素材")
    if not pipeline.project_dir:
        raise AudioSubtitleArtifactError("项目目录还没有创建，不能生成音频素材")
    script = session.get(ScriptAsset, pipeline.script_asset_id)
    if script is None:
        raise AudioSubtitleArtifactError(f"脚本素材不存在：{pipeline.script_asset_id}")
    voiceover = str(script.voiceover or "").strip()
    if not voiceover:
        raise AudioSubtitleArtifactError("口播稿为空，不能生成音频素材")

    approved_root = resolve_configured_path(project_root or get_settings().project_root_dir)
    project_dir = Path(pipeline.project_dir).resolve(strict=True)
    artifact = _new_artifact_id(pipeline_id, artifact_id)
    relative_dir = _AUDIO_DIR / artifact
    try:
        directory_use = begin_prepared_project_child_directory_use(
            pipeline,
            project_root=approved_root,
            relative_path=relative_dir,
            create=True,
        )
        audio_path = prepared_project_output_path(directory_use, "voiceover.wav")
        subtitle_path = prepared_project_output_path(directory_use, "subtitles.srt")
        manifest_path = prepared_project_output_path(directory_use, "manifest.json")
        temp_audio_name = ".tts-input.wav"
        temp_audio_path = prepared_project_output_path(directory_use, temp_audio_name)
        if any(path.exists() for path in (audio_path, subtitle_path, manifest_path, temp_audio_path)):
            raise AudioSubtitleArtifactError("音频素材版本目录已存在文件，拒绝覆盖")

        tts = synthesizer or synthesize_tts
        result = tts(voiceover, temp_audio_path, provider=provider)
        if not isinstance(result, TTSResult) or not result.success:
            raise AudioSubtitleArtifactError(
                f"TTS 未生成可用音频：{getattr(result, 'reason', '未知错误')}"
            )
        measured = _measure_wav(temp_audio_path)
        segments = build_subtitle_segments_for_duration(
            voiceover,
            total_duration_seconds=measured.duration_seconds,
        )
        srt_bytes = build_srt_from_segments(segments).encode("utf-8")
        audio_bytes = temp_audio_path.read_bytes()
        audio_sha = _sha256_bytes(audio_bytes)
        subtitle_sha = _sha256_bytes(srt_bytes)
        created_at = datetime.now(timezone.utc).isoformat()
        manifest = {
            "schema_version": 1,
            "artifact_id": artifact,
            "created_at": created_at,
            "provider": str(getattr(result, "provider", "") or provider or "unknown"),
            "audio": {
                "project_relative_path": _relative_project_path(project_dir, audio_path),
                "sha256": audio_sha,
                "size_bytes": len(audio_bytes),
                "duration_seconds": measured.duration_seconds,
                "sample_rate": measured.sample_rate,
                "channels": measured.channels,
                "mime": "audio/wav",
            },
            "subtitles": {
                "project_relative_path": _relative_project_path(project_dir, subtitle_path),
                "sha256": subtitle_sha,
                "cue_count": len(segments),
                "last_end_seconds": segments[-1].end_seconds if segments else 0.0,
                "mime": "application/x-subrip",
            },
        }
        write_prepared_project_files(
            directory_use,
            {
                "voiceover.wav": audio_bytes,
                "subtitles.srt": srt_bytes,
                "manifest.json": _canonical_json(manifest),
            },
        )
        if temp_audio_path.exists():
            temp_audio_path.unlink()
        directory_use.verify()
        return AudioSubtitleArtifact(
            artifact_id=artifact,
            audio_path=audio_path,
            subtitle_path=subtitle_path,
            manifest_path=manifest_path,
            audio_duration_seconds=measured.duration_seconds,
            audio_sha256=audio_sha,
            subtitle_sha256=subtitle_sha,
            provider=str(getattr(result, "provider", "") or provider or "unknown"),
            created_at=created_at,
        )
    except AudioSubtitleArtifactError:
        _remove_failed_artifact(project_dir / relative_dir)
        raise
    except (OSError, ValueError, ProjectIdentityError, wave.Error) as exc:
        _remove_failed_artifact(project_dir / relative_dir)
        raise AudioSubtitleArtifactError(f"音频素材版本写入失败：{exc}") from exc


def load_pipeline_audio_subtitle_artifact(
    session: Session,
    pipeline_id: int,
    artifact_id: str,
    *,
    project_root: Path | None = None,
) -> AudioSubtitleArtifact:
    """Read and verify one previously published artifact version.

    Binding an artifact to a TimelineSpec must not trust a caller-provided
    path or a stale manifest.  This loader resolves the version through the
    prepared project identity, checks the immutable file names, hashes and
    measured WAV duration, and returns the same value object used by the
    writer.  It performs no writes.
    """

    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None:
        raise AudioSubtitleArtifactError(f"pipeline not found: {pipeline_id}")
    artifact_key = str(artifact_id or "")
    if not _ARTIFACT_ID.fullmatch(artifact_key):
        raise AudioSubtitleArtifactError("audio subtitle artifact id is invalid")
    if not pipeline.project_dir:
        raise AudioSubtitleArtifactError("pipeline project directory is missing")

    approved_root = resolve_configured_path(project_root or get_settings().project_root_dir)
    relative_dir = _AUDIO_DIR / artifact_key
    try:
        project_dir = Path(pipeline.project_dir).resolve(strict=True)
        directory_use = begin_prepared_project_child_directory_use(
            pipeline,
            project_root=approved_root,
            relative_path=relative_dir,
            create=False,
        )
        manifest_path = prepared_project_output_path(
            directory_use, "manifest.json", must_exist=True
        )
        audio_path = prepared_project_output_path(
            directory_use, "voiceover.wav", must_exist=True
        )
        subtitle_path = prepared_project_output_path(
            directory_use, "subtitles.srt", must_exist=True
        )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AudioSubtitleArtifactError("audio subtitle manifest is unreadable") from exc
        if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
            raise AudioSubtitleArtifactError("audio subtitle manifest schema is invalid")
        if str(manifest.get("artifact_id") or "") != artifact_key:
            raise AudioSubtitleArtifactError("audio subtitle manifest id mismatch")

        audio_entry = manifest.get("audio")
        subtitle_entry = manifest.get("subtitles")
        if not isinstance(audio_entry, Mapping) or not isinstance(subtitle_entry, Mapping):
            raise AudioSubtitleArtifactError("audio subtitle manifest entries are missing")
        expected_audio_relative = _relative_project_path(project_dir, audio_path)
        expected_subtitle_relative = _relative_project_path(project_dir, subtitle_path)
        if str(audio_entry.get("project_relative_path") or "").replace("\\", "/") != expected_audio_relative:
            raise AudioSubtitleArtifactError("audio artifact path does not match its version")
        if str(subtitle_entry.get("project_relative_path") or "").replace("\\", "/") != expected_subtitle_relative:
            raise AudioSubtitleArtifactError("subtitle artifact path does not match its version")

        audio_bytes = audio_path.read_bytes()
        subtitle_bytes = subtitle_path.read_bytes()
        audio_sha = _sha256_bytes(audio_bytes)
        subtitle_sha = _sha256_bytes(subtitle_bytes)
        if str(audio_entry.get("sha256") or "").casefold() != audio_sha:
            raise AudioSubtitleArtifactError("audio artifact hash mismatch")
        if str(subtitle_entry.get("sha256") or "").casefold() != subtitle_sha:
            raise AudioSubtitleArtifactError("subtitle artifact hash mismatch")
        measured = _measure_wav(audio_path)
        try:
            declared_duration = float(audio_entry.get("duration_seconds"))
        except (TypeError, ValueError) as exc:
            raise AudioSubtitleArtifactError("audio artifact duration is invalid") from exc
        if not math.isfinite(declared_duration) or abs(declared_duration - measured.duration_seconds) > 0.001:
            raise AudioSubtitleArtifactError("audio artifact duration mismatch")
        subtitle_text = subtitle_bytes.decode("utf-8")
        cue_count, last_end = _srt_summary(subtitle_text)
        try:
            declared_cues = int(subtitle_entry.get("cue_count"))
        except (TypeError, ValueError) as exc:
            raise AudioSubtitleArtifactError("subtitle cue count is invalid") from exc
        if declared_cues != cue_count or last_end > measured.duration_seconds + 0.001:
            raise AudioSubtitleArtifactError("subtitle timing does not match measured audio")
        directory_use.verify()
        return AudioSubtitleArtifact(
            artifact_id=artifact_key,
            audio_path=audio_path,
            subtitle_path=subtitle_path,
            manifest_path=manifest_path,
            audio_duration_seconds=measured.duration_seconds,
            audio_sha256=audio_sha,
            subtitle_sha256=subtitle_sha,
            provider=str(manifest.get("provider") or "unknown"),
            created_at=str(manifest.get("created_at") or ""),
        )
    except AudioSubtitleArtifactError:
        raise
    except (OSError, ValueError, ProjectIdentityError, wave.Error) as exc:
        raise AudioSubtitleArtifactError(
            f"audio subtitle artifact verification failed: {exc}"
        ) from exc


@dataclass(frozen=True, slots=True)
class _MeasuredWav:
    duration_seconds: float
    sample_rate: int
    channels: int


def _measure_wav(path: Path) -> _MeasuredWav:
    if not path.is_file() or path.stat().st_size <= 44:
        raise AudioSubtitleArtifactError("TTS 输出不是非空 WAV 文件")
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
    except (OSError, EOFError, wave.Error) as exc:
        raise AudioSubtitleArtifactError(f"WAV 解码失败：{exc}") from exc
    if frames <= 0 or rate <= 0 or channels <= 0 or sample_width <= 0:
        raise AudioSubtitleArtifactError("WAV 参数无效")
    duration = frames / float(rate)
    if not math.isfinite(duration) or duration <= 0:
        raise AudioSubtitleArtifactError("WAV 时长无效")
    return _MeasuredWav(duration_seconds=duration, sample_rate=rate, channels=channels)


def _new_artifact_id(pipeline_id: int, requested: str | None) -> str:
    if requested is not None:
        value = requested.strip()
        if not _ARTIFACT_ID.fullmatch(value):
            raise AudioSubtitleArtifactError("音频素材版本 ID 无效")
        return value
    return f"as-{pipeline_id}-{uuid4().hex[:16]}"


def _relative_project_path(project_dir: Path, path: Path) -> str:
    try:
        relative = path.resolve(strict=False).relative_to(project_dir.resolve(strict=False))
    except ValueError as exc:
        raise AudioSubtitleArtifactError("音频素材路径越出项目目录") from exc
    return PurePosixPath(relative.as_posix()).as_posix()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _srt_summary(text: str) -> tuple[int, float]:
    """Return cue count and final end time for the generated SRT shape."""

    blocks = re.split(r"\r?\n\s*\r?\n", text.strip()) if text.strip() else []
    last_end = 0.0
    for index, block in enumerate(blocks, start=1):
        lines = [line.strip("\ufeff") for line in block.splitlines()]
        if len(lines) < 3:
            raise AudioSubtitleArtifactError(f"subtitle cue {index} is incomplete")
        timing = lines[1].split(" --> ", 1)
        if len(timing) != 2:
            raise AudioSubtitleArtifactError(f"subtitle cue {index} timing is invalid")
        start = _parse_srt_timestamp(timing[0].strip())
        end = _parse_srt_timestamp(timing[1].strip())
        if end <= start or start < last_end:
            raise AudioSubtitleArtifactError("subtitle cues are not monotonic")
        if not "".join(lines[2:]).strip():
            raise AudioSubtitleArtifactError(f"subtitle cue {index} text is empty")
        last_end = end
    if not blocks:
        raise AudioSubtitleArtifactError("subtitle artifact contains no cues")
    return len(blocks), last_end


def _parse_srt_timestamp(value: str) -> float:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value)
    if match is None:
        raise AudioSubtitleArtifactError("subtitle timestamp is invalid")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    if minutes >= 60 or seconds >= 60:
        raise AudioSubtitleArtifactError("subtitle timestamp is invalid")
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def _canonical_json(value: dict[str, Any]) -> bytes:
    import json

    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _remove_failed_artifact(path: Path) -> None:
    """Remove only a just-created, unreferenced version directory."""

    try:
        if not path.exists() or path.is_symlink() or not path.is_dir():
            return
        for child in path.iterdir():
            if child.is_file() and not child.is_symlink():
                child.unlink()
        path.rmdir()
    except OSError:
        # Preserve the original failure; a future recovery/audit can inspect
        # the orphan directory rather than masking the root cause.
        pass


__all__ = [
    "AudioSubtitleArtifact",
    "AudioSubtitleArtifactError",
    "build_pipeline_audio_subtitle_artifact",
    "load_pipeline_audio_subtitle_artifact",
]
