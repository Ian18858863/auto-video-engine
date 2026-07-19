from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path

from PIL import Image
import pytest

import app.materials.candidate_media_validation as media_validation
from app.materials.candidate_media_validation import (
    CandidateMediaLimits,
    CandidateMediaValidationError,
    validate_candidate_media,
)


def _image_bytes(format_name: str, *, size: tuple[int, int] = (8, 6)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (24, 96, 160)).save(output, format=format_name)
    return output.getvalue()


def _mp4_signature() -> bytes:
    return b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"


def test_real_jpeg_is_decoded_and_reported_with_content_hash(tmp_path: Path):
    path = tmp_path / "candidate.jpg"
    path.write_bytes(_image_bytes("JPEG", size=(12, 9)))

    report = validate_candidate_media(
        path,
        declared_kind="image",
        declared_mime="image/jpeg",
    )

    assert report.media_kind == "image"
    assert report.mime_type == "image/jpeg"
    assert report.width == 12
    assert report.height == 9
    assert report.duration_seconds is None
    assert report.decode_verified is True
    assert len(report.sha256) == 64
    assert report.size_bytes == path.stat().st_size


def test_html_disguised_as_jpeg_is_rejected(tmp_path: Path):
    path = tmp_path / "fake.jpg"
    path.write_bytes(b"<!doctype html><title>not media</title>")

    with pytest.raises(CandidateMediaValidationError) as captured:
        validate_candidate_media(
            path,
            declared_kind="image",
            declared_mime="image/jpeg",
        )

    assert captured.value.code == "unsupported_signature"


def test_declared_mime_must_match_actual_magic(tmp_path: Path):
    path = tmp_path / "mismatch.jpg"
    path.write_bytes(_image_bytes("PNG"))

    with pytest.raises(CandidateMediaValidationError) as captured:
        validate_candidate_media(
            path,
            declared_kind="image",
            declared_mime="image/jpeg",
        )

    assert captured.value.code == "declared_mime_mismatch"


def test_size_and_pixel_limits_fail_closed(tmp_path: Path):
    path = tmp_path / "large.png"
    path.write_bytes(_image_bytes("PNG", size=(20, 20)))

    with pytest.raises(CandidateMediaValidationError) as size_error:
        validate_candidate_media(
            path,
            declared_kind="image",
            limits=CandidateMediaLimits(max_bytes=8),
        )
    assert size_error.value.code == "size_limit_exceeded"

    with pytest.raises(CandidateMediaValidationError) as pixel_error:
        validate_candidate_media(
            path,
            declared_kind="image",
            limits=CandidateMediaLimits(max_image_pixels=100),
        )
    assert pixel_error.value.code == "image_dimensions_exceeded"


def test_video_uses_ffprobe_and_ffmpeg_decode_before_accepting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "candidate.mp4"
    path.write_bytes(_mp4_signature())
    commands: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...], *, timeout_seconds: float) -> bytes:
        commands.append(command)
        if "ffprobe" in Path(command[0]).name.casefold():
            return json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "pix_fmt": "yuv420p",
                            "width": 1280,
                            "height": 720,
                            "duration": "5.0",
                        }
                    ],
                    "format": {
                        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                        "duration": "5.0",
                    },
                }
            ).encode()
        return b""

    monkeypatch.setattr(media_validation, "_run_command", fake_run)

    report = validate_candidate_media(
        path,
        declared_kind="video",
        declared_mime="video/mp4",
        ffprobe_executable="ffprobe-test",
        ffmpeg_executable="ffmpeg-test",
    )

    assert report.media_kind == "video"
    assert report.width == 1280
    assert report.height == 720
    assert report.duration_seconds == 5.0
    assert report.codec == "h264"
    assert report.decode_verified is True
    assert len(commands) == 2
    assert "-show_streams" in commands[0]
    assert "-f" in commands[1] and "null" in commands[1]


def test_video_probe_dimensions_and_timeout_are_recoverable_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "candidate.mp4"
    path.write_bytes(_mp4_signature())

    def oversized_probe(command: tuple[str, ...], *, timeout_seconds: float) -> bytes:
        if "ffprobe" in Path(command[0]).name.casefold():
            return json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 9000,
                            "height": 9000,
                            "duration": "1.0",
                        }
                    ],
                    "format": {"format_name": "mov,mp4", "duration": "1.0"},
                }
            ).encode()
        raise AssertionError("decode should not run for oversized video")

    monkeypatch.setattr(media_validation, "_run_command", oversized_probe)
    with pytest.raises(CandidateMediaValidationError) as dimensions_error:
        validate_candidate_media(
            path,
            declared_kind="video",
            ffprobe_executable="ffprobe-test",
            ffmpeg_executable="ffmpeg-test",
        )
    assert dimensions_error.value.code == "video_dimensions_exceeded"

    def timed_out(_command: tuple[str, ...], *, timeout_seconds: float) -> bytes:
        raise TimeoutError("probe timeout")

    monkeypatch.setattr(media_validation, "_run_command", timed_out)
    with pytest.raises(CandidateMediaValidationError) as timeout_error:
        validate_candidate_media(
            path,
            declared_kind="video",
            ffprobe_executable="ffprobe-test",
            ffmpeg_executable="ffmpeg-test",
        )
    assert timeout_error.value.code == "video_probe_failed"
