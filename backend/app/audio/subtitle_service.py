from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SubtitleSegment:
    index: int
    start_seconds: float
    end_seconds: float
    text: str


def build_subtitle_segments(
    voiceover: str,
    *,
    total_duration_seconds: float = 60.0,
) -> list[SubtitleSegment]:
    sentences = _split_sentences(voiceover)
    if not sentences:
        return []
    duration = max(total_duration_seconds, len(sentences) * 2.0)
    weights = [max(len(sentence), 8) for sentence in sentences]
    total_weight = sum(weights)
    cursor = 0.0
    segments: list[SubtitleSegment] = []
    for index, (sentence, weight) in enumerate(zip(sentences, weights), start=1):
        segment_duration = max(1.8, duration * weight / total_weight)
        end = min(duration, cursor + segment_duration)
        segments.append(
            SubtitleSegment(
                index=index,
                start_seconds=cursor,
                end_seconds=end,
                text=sentence,
            )
        )
        cursor = end
    if segments:
        segments[-1].end_seconds = duration
    return segments


def build_subtitle_segments_for_duration(
    voiceover: str,
    *,
    total_duration_seconds: float,
) -> list[SubtitleSegment]:
    """Fit subtitle cues to a measured audio duration without extending it.

    The legacy helper intentionally padded short/fake durations for the old
    demo path.  A real audio artifact must be the authority: cues may be
    shorter, but they must never outlive the decoded WAV.
    """

    if not math.isfinite(float(total_duration_seconds)) or total_duration_seconds <= 0:
        raise ValueError("subtitle duration must be a positive finite number")
    sentences = _split_sentences(voiceover)
    if not sentences:
        return []
    duration = float(total_duration_seconds)
    weights = [max(len(sentence), 8) for sentence in sentences]
    total_weight = float(sum(weights))
    # Keep a readable minimum when the measured clip is long enough.  For a
    # very short clip, proportional allocation is the only honest option.
    minimum = min(1.8, duration / len(sentences) / 2.0)
    raw = [duration * weight / total_weight for weight in weights]
    if minimum > 0 and minimum * len(raw) <= duration:
        raw = [max(value, minimum) for value in raw]
        scale = duration / sum(raw)
        raw = [value * scale for value in raw]
    segments: list[SubtitleSegment] = []
    cursor = 0.0
    for index, (sentence, segment_duration) in enumerate(zip(sentences, raw), start=1):
        end = duration if index == len(sentences) else min(duration, cursor + segment_duration)
        if end <= cursor:
            raise ValueError("subtitle cues cannot fit measured audio duration")
        segments.append(
            SubtitleSegment(
                index=index,
                start_seconds=cursor,
                end_seconds=end,
                text=sentence,
            )
        )
        cursor = end
    validate_subtitle_segments(segments, duration)
    return segments


def validate_subtitle_segments(
    segments: list[SubtitleSegment],
    duration_seconds: float,
) -> None:
    """Fail closed on overlaps, non-monotonic cues, and overrun."""

    if not math.isfinite(float(duration_seconds)) or duration_seconds <= 0:
        raise ValueError("subtitle duration must be positive")
    previous_end = 0.0
    for segment in segments:
        if not math.isfinite(segment.start_seconds) or not math.isfinite(segment.end_seconds):
            raise ValueError("subtitle cue time is not finite")
        if segment.start_seconds < previous_end - 0.001:
            raise ValueError("subtitle cues overlap or are out of order")
        if segment.end_seconds <= segment.start_seconds:
            raise ValueError("subtitle cue must have positive duration")
        if segment.end_seconds > duration_seconds + 0.001:
            raise ValueError("subtitle cue exceeds measured audio duration")
        previous_end = segment.end_seconds


def build_srt_from_segments(segments: list[SubtitleSegment]) -> str:
    rows: list[str] = []
    for segment in segments:
        rows.extend(
            [
                str(segment.index),
                f"{_format_srt_time(segment.start_seconds)} --> {_format_srt_time(segment.end_seconds)}",
                segment.text,
                "",
            ]
        )
    return "\n".join(rows).strip() + "\n" if rows else ""


def write_srt(path: Path, segments: list[SubtitleSegment]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_srt_from_segments(segments), encoding="utf-8")
    return path


def segments_from_storyboard(storyboard: list[dict[str, Any]]) -> list[SubtitleSegment]:
    segments: list[SubtitleSegment] = []
    for index, shot in enumerate(storyboard, start=1):
        start = _parse_time(str(shot.get("start", "")))
        end = _parse_time(str(shot.get("end", "")))
        text = str(shot.get("voiceover") or shot.get("subtitle") or "").strip()
        if end <= start:
            end = start + 4
        if text:
            segments.append(SubtitleSegment(index=index, start_seconds=start, end_seconds=end, text=text))
    return segments


def _split_sentences(text: str) -> list[str]:
    normalized = text.replace("\r", "\n")
    chunks: list[str] = []
    current = ""
    for char in normalized:
        current += char
        if char in "。！？!?；;\n":
            chunk = current.strip()
            if chunk:
                chunks.append(chunk)
            current = ""
    tail = current.strip()
    if tail:
        chunks.append(tail)
    compact: list[str] = []
    for chunk in chunks:
        if len(chunk) <= 32:
            compact.append(chunk)
        else:
            compact.extend(_split_long_chunk(chunk))
    return [item for item in compact if item.strip()]


def _split_long_chunk(chunk: str) -> list[str]:
    parts: list[str] = []
    current = ""
    for token in chunk.replace("，", "，|").replace(",", ",|").split("|"):
        if len(current) + len(token) > 28 and current:
            parts.append(current.strip())
            current = token
        else:
            current += token
    if current.strip():
        parts.append(current.strip())
    return parts


def _format_srt_time(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours = milliseconds // 3_600_000
    milliseconds %= 3_600_000
    minutes = milliseconds // 60_000
    milliseconds %= 60_000
    secs = milliseconds // 1000
    millis = milliseconds % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _parse_time(value: str) -> float:
    if not value:
        return 0.0
    parts = [part for part in value.replace(".", ":").split(":") if part]
    try:
        nums = [float(part) for part in parts]
    except ValueError:
        return 0.0
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[-3] * 3600 + nums[-2] * 60 + nums[-1]
