from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.video.media_technical_probe import (
    MediaTechnicalProbeError,
    probe_audio_subtitles,
)


def _payload(duration: str = "1.25") -> bytes:
    return json.dumps(
        {
            "format": {"duration": duration},
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "pcm_s16le",
                    "sample_rate": "8000",
                    "channels": 1,
                    "duration": duration,
                }
            ],
        }
    ).encode("utf-8")


def test_probe_checks_audio_decode_and_srt_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    audio = tmp_path / "audio.wav"
    subtitles = tmp_path / "subtitles.srt"
    audio.write_bytes(b"wav")
    subtitles.write_text(
        "1\n00:00:00,000 --> 00:00:01,250\n测试字幕\n",
        encoding="utf-8",
    )
    commands: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...], *, timeout_seconds: float) -> bytes:
        commands.append(command)
        return _payload() if "ffprobe" in command[0] else b""

    monkeypatch.setattr("app.video.media_technical_probe._run_command", fake_run)
    report = probe_audio_subtitles(
        audio,
        subtitles,
        ffprobe_executable="D:/tools/ffprobe.exe",
        ffmpeg_executable="D:/tools/ffmpeg.exe",
    )

    assert report.is_valid is True
    assert report.audio_duration_seconds == pytest.approx(1.25)
    assert report.subtitle_cue_count == 1
    assert len(commands) == 2


def test_probe_fails_when_srt_outlives_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    audio = tmp_path / "audio.wav"
    subtitles = tmp_path / "subtitles.srt"
    audio.write_bytes(b"wav")
    subtitles.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n超出音频\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.video.media_technical_probe._run_command",
        lambda command, *, timeout_seconds: _payload() if "ffprobe" in command[0] else b"",
    )

    report = probe_audio_subtitles(
        audio,
        subtitles,
        ffprobe_executable="D:/tools/ffprobe.exe",
        ffmpeg_executable="D:/tools/ffmpeg.exe",
    )
    assert report.is_valid is False
    assert any(check.name == "subtitle_timing" and not check.passed for check in report.checks)


def test_probe_rejects_c_drive(tmp_path: Path):
    subtitles = tmp_path / "subtitles.srt"
    subtitles.write_text("", encoding="utf-8")
    with pytest.raises(MediaTechnicalProbeError, match="D 盘"):
        probe_audio_subtitles("C:/audio.wav", subtitles, ffprobe_executable="D:/ffprobe.exe")
