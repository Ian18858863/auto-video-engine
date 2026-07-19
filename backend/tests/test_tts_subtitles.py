from __future__ import annotations

from app.audio.subtitle_service import (
    build_srt_from_segments,
    build_subtitle_segments,
    segments_from_storyboard,
)
from app.audio.tts_service import synthesize_tts


def test_build_subtitle_segments_splits_chinese_voiceover():
    segments = build_subtitle_segments(
        "这条新闻普通人不用全看，关键是看懂这个变化。它影响的是平台流量和广告模式。",
        total_duration_seconds=12,
    )

    assert len(segments) >= 2
    assert segments[0].start_seconds == 0
    assert segments[-1].end_seconds == 12
    assert "普通人" in segments[0].text


def test_build_srt_from_segments_outputs_standard_timecode():
    segments = build_subtitle_segments("第一句。第二句。", total_duration_seconds=6)

    srt = build_srt_from_segments(segments)

    assert "00:00:00,000 -->" in srt
    assert "第一句" in srt
    assert "第二句" in srt


def test_segments_from_storyboard_uses_shot_times():
    segments = segments_from_storyboard(
        [
            {"start": "00:00", "end": "00:03", "voiceover": "开头钩子"},
            {"start": "00:03", "end": "00:08", "voiceover": "解释背景"},
        ]
    )

    assert len(segments) == 2
    assert segments[0].end_seconds == 3
    assert segments[1].start_seconds == 3


def test_tts_unknown_provider_returns_clear_failure(tmp_path):
    result = synthesize_tts("测试文本", tmp_path / "voice.wav", provider="unknown")

    assert result.success is False
    assert "不支持" in result.reason
