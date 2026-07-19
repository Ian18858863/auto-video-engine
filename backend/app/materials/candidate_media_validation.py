"""Fail-closed content validation for material candidates.

The legacy download and candidate paths historically trusted a filename suffix
or multipart ``Content-Type``.  This module is the shared admission boundary
for downloaded, uploaded and already-persisted candidate media.  It does not
decide semantic quality or rights; it establishes only that the bytes are a
bounded, decodable image/video of the declared kind.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping
import warnings

from PIL import Image, UnidentifiedImageError


_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
_VIDEO_MIMES = frozenset({"video/mp4", "video/quicktime", "video/webm"})
_MIME_ALIASES = {"image/jpg": "image/jpeg", "video/x-m4v": "video/mp4"}
_MAX_PROBE_OUTPUT_BYTES = 2 * 1024 * 1024


class CandidateMediaValidationError(ValueError):
    """A candidate cannot safely enter or remain in the review chain."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CandidateMediaLimits:
    max_bytes: int = 512 * 1024 * 1024
    max_image_pixels: int = 50_000_000
    max_image_dimension: int = 16_384
    max_video_pixels: int = 33_177_600
    max_video_dimension: int = 8_192
    max_video_duration_seconds: float = 600.0
    command_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        integer_fields = (
            "max_bytes",
            "max_image_pixels",
            "max_image_dimension",
            "max_video_pixels",
            "max_video_dimension",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_video_duration_seconds", "command_timeout_seconds"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class CandidateMediaReport:
    media_kind: str
    mime_type: str
    sha256: str
    size_bytes: int
    width: int
    height: int
    duration_seconds: float | None
    codec: str
    container: str
    decode_verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "verified",
            "media_kind": self.media_kind,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "duration_seconds": self.duration_seconds,
            "codec": self.codec,
            "container": self.container,
            "decode_verified": self.decode_verified,
        }


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    identity: tuple[int, int, int, int]
    sha256: str
    size_bytes: int
    prefix: bytes


def validate_candidate_media(
    path: Path | str,
    *,
    declared_kind: str | None = None,
    declared_mime: str | None = None,
    limits: CandidateMediaLimits | None = None,
    ffprobe_executable: Path | str | None = None,
    ffmpeg_executable: Path | str | None = None,
) -> CandidateMediaReport:
    """Validate one ordinary D:/non-system-drive file without modifying it."""

    active_limits = limits or CandidateMediaLimits()
    media_path = _require_safe_file(path)
    before = _snapshot_file(media_path, max_bytes=active_limits.max_bytes)
    detected_kind, detected_mime = _detect_signature(before.prefix)
    expected_kind = _normalise_kind(declared_kind)
    expected_mime = _normalise_mime(declared_mime)
    if expected_kind and expected_kind != detected_kind:
        raise CandidateMediaValidationError(
            "declared_kind_mismatch",
            f"声明为 {expected_kind}，实际内容是 {detected_kind}。",
        )
    if expected_mime and not _mime_matches(expected_mime, detected_mime):
        raise CandidateMediaValidationError(
            "declared_mime_mismatch",
            f"声明 MIME {expected_mime} 与实际内容 {detected_mime} 不一致。",
        )

    if detected_kind == "image":
        details = _validate_image(media_path, detected_mime, active_limits)
    else:
        details = _validate_video(
            media_path,
            detected_mime,
            active_limits,
            ffprobe_executable=ffprobe_executable,
            ffmpeg_executable=ffmpeg_executable,
        )

    after = _snapshot_file(media_path, max_bytes=active_limits.max_bytes)
    if before.identity != after.identity or before.sha256 != after.sha256:
        raise CandidateMediaValidationError(
            "media_changed_during_validation",
            "素材在验证过程中发生变化，请重新选择或下载。",
        )
    return CandidateMediaReport(
        media_kind=detected_kind,
        mime_type=detected_mime,
        sha256=after.sha256,
        size_bytes=after.size_bytes,
        width=details["width"],
        height=details["height"],
        duration_seconds=details.get("duration_seconds"),
        codec=str(details.get("codec") or ""),
        container=str(details.get("container") or ""),
        decode_verified=True,
    )


def _require_safe_file(path: Path | str) -> Path:
    raw = Path(path)
    if not raw.is_absolute():
        raise CandidateMediaValidationError("unsafe_path", "候选素材路径必须是绝对路径。")
    # Check the caller-visible path before resolving it; otherwise a symlink
    # component disappears from the resolved path and cannot be detected.
    _reject_reparse_components(raw)
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise CandidateMediaValidationError("media_missing", "候选素材不存在或无法读取。") from exc
    if os.name == "nt" and resolved.drive.casefold() == "c:":
        raise CandidateMediaValidationError("unsafe_path", "候选素材不能位于系统盘。")
    _reject_reparse_components(resolved)
    try:
        metadata = resolved.stat()
    except OSError as exc:
        raise CandidateMediaValidationError("media_missing", "候选素材不存在或无法读取。") from exc
    if not resolved.is_file():
        raise CandidateMediaValidationError("media_missing", "候选素材不是普通文件。")
    if int(getattr(metadata, "st_nlink", 1)) != 1:
        raise CandidateMediaValidationError("unsafe_path", "候选素材不能是硬链接。")
    return resolved


def _reject_reparse_components(path: Path) -> None:
    components = [path]
    components.extend(path.parents)
    for component in components:
        try:
            if component.is_symlink() or bool(getattr(os.path, "isjunction", lambda _p: False)(component)):
                raise CandidateMediaValidationError("unsafe_path", "候选素材路径不能经过链接或联接点。")
        except OSError as exc:
            raise CandidateMediaValidationError("unsafe_path", "无法安全检查候选素材路径。") from exc


def _snapshot_file(path: Path, *, max_bytes: int) -> _FileSnapshot:
    digest = hashlib.sha256()
    prefix = bytearray()
    total = 0
    try:
        with path.open("rb") as handle:
            first = os.fstat(handle.fileno())
            if first.st_size <= 0:
                raise CandidateMediaValidationError("empty_media", "候选素材为空文件。")
            if first.st_size > max_bytes:
                raise CandidateMediaValidationError(
                    "size_limit_exceeded",
                    f"候选素材超过 {max_bytes} 字节上限。",
                )
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise CandidateMediaValidationError(
                        "size_limit_exceeded",
                        f"候选素材超过 {max_bytes} 字节上限。",
                    )
                if len(prefix) < 64:
                    prefix.extend(chunk[: 64 - len(prefix)])
                digest.update(chunk)
            second = os.fstat(handle.fileno())
    except CandidateMediaValidationError:
        raise
    except OSError as exc:
        raise CandidateMediaValidationError("media_unreadable", "候选素材无法安全读取。") from exc
    first_identity = (first.st_dev, first.st_ino, first.st_size, first.st_mtime_ns)
    second_identity = (second.st_dev, second.st_ino, second.st_size, second.st_mtime_ns)
    if first_identity != second_identity or total != first.st_size:
        raise CandidateMediaValidationError(
            "media_changed_during_validation",
            "素材在读取过程中发生变化，请重新选择或下载。",
        )
    return _FileSnapshot(first_identity, digest.hexdigest(), total, bytes(prefix))


def _detect_signature(prefix: bytes) -> tuple[str, str]:
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "image", "image/webp"
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        return "video", "video/mp4"
    if prefix.startswith(b"\x1aE\xdf\xa3"):
        return "video", "video/webm"
    raise CandidateMediaValidationError(
        "unsupported_signature",
        "文件内容不是受支持的 JPEG、PNG、WebP、MP4/MOV 或 WebM 媒体。",
    )


def _normalise_kind(value: str | None) -> str:
    if value is None or not str(value).strip():
        return ""
    text = str(value).strip().casefold()
    aliases = {"photo": "image", "picture": "image", "movie": "video"}
    text = aliases.get(text, text)
    if text not in {"image", "video"}:
        raise CandidateMediaValidationError("invalid_declaration", "声明的媒体类型不受支持。")
    return text


def _normalise_mime(value: str | None) -> str:
    if value is None or not str(value).strip():
        return ""
    text = str(value).split(";", 1)[0].strip().casefold()
    text = _MIME_ALIASES.get(text, text)
    if text not in _IMAGE_MIMES | _VIDEO_MIMES:
        raise CandidateMediaValidationError("invalid_declaration", "声明的 MIME 类型不受支持。")
    return text


def _mime_matches(declared: str, detected: str) -> bool:
    if declared == detected:
        return True
    return detected == "video/mp4" and declared == "video/quicktime"


def _validate_image(
    path: Path,
    detected_mime: str,
    limits: CandidateMediaLimits,
) -> dict[str, Any]:
    expected_format = {
        "image/jpeg": "JPEG",
        "image/png": "PNG",
        "image/webp": "WEBP",
    }[detected_mime]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                width, height = image.size
                actual_format = str(image.format or "").upper()
                if actual_format != expected_format:
                    raise CandidateMediaValidationError(
                        "image_format_mismatch",
                        f"图片 magic 与解码格式不一致：{detected_mime}/{actual_format or 'unknown'}。",
                    )
                _validate_image_dimensions(width, height, limits)
                image.verify()
            with Image.open(path) as image:
                image.load()
    except CandidateMediaValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise CandidateMediaValidationError(
            "image_dimensions_exceeded",
            "图片像素规模超过安全上限。",
        ) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise CandidateMediaValidationError("image_decode_failed", "图片无法完整解码。") from exc
    return {
        "width": int(width),
        "height": int(height),
        "duration_seconds": None,
        "codec": expected_format.casefold(),
        "container": expected_format.casefold(),
    }


def _validate_image_dimensions(width: int, height: int, limits: CandidateMediaLimits) -> None:
    if (
        width <= 0
        or height <= 0
        or width > limits.max_image_dimension
        or height > limits.max_image_dimension
        or width * height > limits.max_image_pixels
    ):
        raise CandidateMediaValidationError(
            "image_dimensions_exceeded",
            "图片宽高或总像素超过安全上限。",
        )


def _validate_video(
    path: Path,
    detected_mime: str,
    limits: CandidateMediaLimits,
    *,
    ffprobe_executable: Path | str | None,
    ffmpeg_executable: Path | str | None,
) -> dict[str, Any]:
    probe = str(ffprobe_executable or shutil.which("ffprobe") or "").strip()
    ffmpeg = str(ffmpeg_executable or _default_ffmpeg() or "").strip()
    if not probe or not ffmpeg:
        raise CandidateMediaValidationError(
            "video_tools_unavailable",
            "缺少 ffprobe 或 FFmpeg，无法验证候选视频。",
        )
    probe_command = (
        probe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    )
    try:
        raw = _run_command(probe_command, timeout_seconds=limits.command_timeout_seconds)
        if len(raw) > _MAX_PROBE_OUTPUT_BYTES:
            raise ValueError("ffprobe output is too large")
        payload = json.loads(raw.decode("utf-8", errors="strict"))
        if not isinstance(payload, Mapping):
            raise ValueError("ffprobe root is not an object")
        streams = payload.get("streams")
        if not isinstance(streams, list):
            raise ValueError("ffprobe streams missing")
        video = next(
            (
                item
                for item in streams
                if isinstance(item, Mapping) and item.get("codec_type") == "video"
            ),
            None,
        )
        if video is None:
            raise ValueError("video stream missing")
        format_data = payload.get("format")
        if not isinstance(format_data, Mapping):
            format_data = {}
        width = _positive_int(video.get("width"))
        height = _positive_int(video.get("height"))
        duration = _first_positive_float(video.get("duration"), format_data.get("duration"))
        codec = str(video.get("codec_name") or "").strip().casefold()
        container = str(format_data.get("format_name") or "").strip().casefold()
    except CandidateMediaValidationError:
        raise
    except Exception as exc:
        raise CandidateMediaValidationError(
            "video_probe_failed",
            f"候选视频技术信息读取失败：{str(exc)[-300:]}",
        ) from exc

    if (
        width > limits.max_video_dimension
        or height > limits.max_video_dimension
        or width * height > limits.max_video_pixels
    ):
        raise CandidateMediaValidationError(
            "video_dimensions_exceeded",
            "候选视频宽高或总像素超过安全上限。",
        )
    if duration > limits.max_video_duration_seconds:
        raise CandidateMediaValidationError(
            "video_duration_exceeded",
            f"候选视频超过 {limits.max_video_duration_seconds:g} 秒上限。",
        )
    if detected_mime == "video/webm" and not any(
        name in container for name in ("webm", "matroska")
    ):
        raise CandidateMediaValidationError("video_container_mismatch", "WebM magic 与容器信息不一致。")
    if detected_mime == "video/mp4" and not any(
        name in container for name in ("mov", "mp4", "3gp", "mj2")
    ):
        raise CandidateMediaValidationError("video_container_mismatch", "MP4/MOV magic 与容器信息不一致。")

    decode_command = (
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-threads",
        "1",
        "-f",
        "null",
        "-",
    )
    try:
        _run_command(decode_command, timeout_seconds=limits.command_timeout_seconds)
    except Exception as exc:
        raise CandidateMediaValidationError(
            "video_decode_failed",
            f"候选视频无法完整解码：{str(exc)[-300:]}",
        ) from exc
    return {
        "width": width,
        "height": height,
        "duration_seconds": duration,
        "codec": codec,
        "container": container,
    }


def _positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("video dimensions are invalid") from exc
    if result <= 0:
        raise ValueError("video dimensions are invalid")
    return result


def _first_positive_float(*values: Any) -> float:
    for value in values:
        try:
            result = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(result) and result > 0:
            return result
    raise ValueError("video duration is invalid")


def _default_ffmpeg() -> str:
    discovered = shutil.which("ffmpeg")
    if discovered:
        return discovered
    try:
        import imageio_ffmpeg

        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return ""


def _run_command(command: tuple[str, ...], *, timeout_seconds: float) -> bytes:
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(str(exc)) from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail[-800:] or f"command returned {completed.returncode}")
    return bytes(completed.stdout)


__all__ = [
    "CandidateMediaLimits",
    "CandidateMediaReport",
    "CandidateMediaValidationError",
    "validate_candidate_media",
]
