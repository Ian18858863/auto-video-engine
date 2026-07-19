from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.video.technical_probe import (
    VideoTechnicalExpectation,
    VideoTechnicalProbeError,
    probe_video,
)


def _probe_payload(*, width: int = 1080, height: int = 1920, fps: str = "30/1", duration: str = "1.0", audio: bool = False) -> bytes:
    streams = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": width,
            "height": height,
            "pix_fmt": "yuv420p",
            "avg_frame_rate": fps,
            "duration": duration,
        }
    ]
    if audio:
        streams.append(
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "duration": duration,
            }
        )
    return json.dumps(
        {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": duration}, "streams": streams}
    ).encode("utf-8")


def test_probe_is_read_only_and_reports_required_video_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    media = tmp_path / "rendered.mp4"
    media.write_bytes(b"media")
    commands: list[tuple[str, ...]] = []

    def fake_probe(command: tuple[str, ...], *, timeout_seconds: float) -> bytes:
        commands.append(command)
        return _probe_payload()

    def fake_decode(command: tuple[str, ...], *, timeout_seconds: float) -> None:
        commands.append(command)

    monkeypatch.setattr("app.video.technical_probe._run_probe_command", fake_probe)
    monkeypatch.setattr("app.video.technical_probe._run_decode_command", fake_decode)
    report = probe_video(
        media,
        ffprobe_executable="D:/tools/ffprobe.exe",
        ffmpeg_executable="D:/tools/ffmpeg.exe",
    )

    assert report.is_valid is True
    assert report.width == 1080
    assert report.height == 1920
    assert report.fps == 30
    assert report.duration_seconds == 1.0
    assert report.audio_streams == 0
    assert all(check.passed for check in report.checks if check.required)
    assert not (tmp_path / "probe.json").exists()
    assert len(commands) == 2


def test_probe_fails_closed_for_wrong_video_shape_and_required_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    media = tmp_path / "rendered.mp4"
    media.write_bytes(b"media")
    monkeypatch.setattr(
        "app.video.technical_probe._run_probe_command",
        lambda command, *, timeout_seconds: _probe_payload(width=720, height=1280, fps="25/1"),
    )
    monkeypatch.setattr(
        "app.video.technical_probe._run_decode_command",
        lambda command, *, timeout_seconds: None,
    )
    report = probe_video(
        media,
        ffprobe_executable="D:/tools/ffprobe.exe",
        ffmpeg_executable="D:/tools/ffmpeg.exe",
        expected=VideoTechnicalExpectation(require_audio=True),
    )

    assert report.is_valid is False
    failed = {check.name for check in report.checks if check.required and not check.passed}
    assert {"dimensions", "fps", "audio"} <= failed


def test_probe_rejects_c_drive_and_unavailable_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(VideoTechnicalProbeError, match="D 盘"):
        probe_video(Path("C:/not-allowed/rendered.mp4"))

    media = tmp_path / "rendered.mp4"
    media.write_bytes(b"media")
    monkeypatch.setattr("app.video.technical_probe.shutil.which", lambda name: None)
    with pytest.raises(VideoTechnicalProbeError, match="ffprobe"):
        probe_video(media, ffmpeg_executable="D:/tools/ffmpeg.exe")

