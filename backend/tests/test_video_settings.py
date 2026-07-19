from __future__ import annotations

from app.video.settings import default_vertical_settings, horizontal_settings, settings_from_dict


def test_default_video_settings_match_vertical_short_video():
    settings = default_vertical_settings()

    assert settings.aspect_ratio == "9:16"
    assert settings.width == 1080
    assert settings.height == 1920
    assert settings.fps == 30
    assert settings.video_codec == "libx264"
    assert settings.audio_codec == "aac"


def test_horizontal_settings_can_be_selected_later():
    settings = horizontal_settings()

    assert settings.aspect_ratio == "16:9"
    assert settings.width == 1920
    assert settings.height == 1080


def test_settings_from_dict_normalizes_invalid_values():
    settings = settings_from_dict(
        {
            "width": 10,
            "height": 20,
            "fps": 100,
            "bgm_volume": 9,
            "voice_volume": 9,
        }
    )

    assert settings.width == 360
    assert settings.height == 360
    assert settings.fps == 60
    assert settings.bgm_volume == 1.0
    assert settings.voice_volume == 2.0
