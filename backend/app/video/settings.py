from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class VideoRenderSettings:
    aspect_ratio: str = "9:16"
    width: int = 1080
    height: int = 1920
    fps: int = 30
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    video_bitrate: str = "10M"
    audio_sample_rate: int = 48000
    subtitle_font_size: int = 42
    subtitle_margin_bottom: int = 180
    bgm_volume: float = 0.18
    voice_volume: float = 1.0

    def normalized(self) -> "VideoRenderSettings":
        width = max(360, int(self.width))
        height = max(360, int(self.height))
        fps = max(15, min(60, int(self.fps)))
        return VideoRenderSettings(
            aspect_ratio=self.aspect_ratio or _aspect_ratio(width, height),
            width=width,
            height=height,
            fps=fps,
            video_codec=self.video_codec or "libx264",
            audio_codec=self.audio_codec or "aac",
            video_bitrate=self.video_bitrate or "10M",
            audio_sample_rate=int(self.audio_sample_rate or 48000),
            subtitle_font_size=max(20, int(self.subtitle_font_size)),
            subtitle_margin_bottom=max(20, int(self.subtitle_margin_bottom)),
            bgm_volume=max(0.0, min(1.0, float(self.bgm_volume))),
            voice_volume=max(0.0, min(2.0, float(self.voice_volume))),
        )

    def to_dict(self) -> dict:
        return {
            "aspect_ratio": self.aspect_ratio,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "video_bitrate": self.video_bitrate,
            "audio_sample_rate": self.audio_sample_rate,
            "subtitle_font_size": self.subtitle_font_size,
            "subtitle_margin_bottom": self.subtitle_margin_bottom,
            "bgm_volume": self.bgm_volume,
            "voice_volume": self.voice_volume,
        }


def default_vertical_settings() -> VideoRenderSettings:
    return VideoRenderSettings()


def horizontal_settings() -> VideoRenderSettings:
    return VideoRenderSettings(aspect_ratio="16:9", width=1920, height=1080)


def settings_from_dict(payload: dict | None) -> VideoRenderSettings:
    if not isinstance(payload, dict):
        return default_vertical_settings()
    return VideoRenderSettings(
        aspect_ratio=str(payload.get("aspect_ratio") or "9:16"),
        width=int(payload.get("width") or 1080),
        height=int(payload.get("height") or 1920),
        fps=int(payload.get("fps") or 30),
        video_codec=str(payload.get("video_codec") or "libx264"),
        audio_codec=str(payload.get("audio_codec") or "aac"),
        video_bitrate=str(payload.get("video_bitrate") or "10M"),
        audio_sample_rate=int(payload.get("audio_sample_rate") or 48000),
        subtitle_font_size=int(payload.get("subtitle_font_size") or 42),
        subtitle_margin_bottom=int(payload.get("subtitle_margin_bottom") or 180),
        bgm_volume=float(payload.get("bgm_volume") if payload.get("bgm_volume") is not None else 0.18),
        voice_volume=float(payload.get("voice_volume") if payload.get("voice_volume") is not None else 1.0),
    ).normalized()


def _aspect_ratio(width: int, height: int) -> str:
    return "9:16" if height >= width else "16:9"
