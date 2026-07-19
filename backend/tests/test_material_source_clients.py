from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.materials.asset_paths import AutoMaterialPaths
from app.materials.source_clients import CoverrDownloadService, PixabayDownloadService


class _FakeHttp:
    def __init__(self):
        self.json_payloads: dict[str, dict[str, Any]] = {}
        self.bytes_payloads: dict[str, bytes] = {}
        self.download_to_calls: list[tuple[str, str, str]] = []

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if "videos" in url and "pixabay" in url:
            return self.json_payloads["pixabay_videos"]
        if "pixabay.com/api/" in url:
            return self.json_payloads["pixabay_photos"]
        if "coverr" in url:
            return self.json_payloads["coverr"]
        return {}

    def get_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        raise AssertionError("streaming download_to must be preferred")

    def download_to(
        self,
        url: str,
        destination: Path,
        *,
        source_name: str,
        asset_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.download_to_calls.append((url, source_name, asset_type))
        destination.write_bytes(self.bytes_payloads[url])


def test_pixabay_key_can_be_read_from_env_file(tmp_path):
    paths = AutoMaterialPaths(root=tmp_path / "素材")
    key_file = paths.pixabay_root / "Pixabay API.env"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text("PIXABAY_API_KEY=test-key", encoding="utf-8")

    assert paths.read_pixabay_api_key() == "test-key"


def test_pixabay_key_does_not_read_desktop_or_cwd_fallback(tmp_path, monkeypatch):
    from app.materials.asset_paths import AutoMaterialPathError

    paths = AutoMaterialPaths(root=tmp_path / "素材")
    workflow_dir = tmp_path / "Desktop" / "工作流"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "Pixabay API.env.txt").write_text(
        "PIXABAY_API_KEY=must-not-read",
        encoding="utf-8",
    )
    (tmp_path / "Pixabay API.env").write_text(
        "PIXABAY_API_KEY=must-not-read-cwd",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(AutoMaterialPathError) as error:
        paths.read_pixabay_api_key()
    assert "Desktop" not in str(error.value)
    assert "must-not-read" not in str(error.value)
    assert str(paths.pixabay_api_key_path) in str(error.value)


def test_pixabay_downloads_video_first_and_writes_fingerprint(tmp_path):
    paths = AutoMaterialPaths(root=tmp_path / "素材")
    key_file = paths.pixabay_root / "API Key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text("fake-key", encoding="utf-8")
    http = _FakeHttp()
    http.json_payloads["pixabay_videos"] = {
        "hits": [
                {
                    "id": 123,
                    "pageURL": "https://pixabay.com/videos/test",
                    "user_id": 77,
                    "user": "Pixabay Creator",
                    "duration": 8,
                "videos": {
                    "large": {
                        "url": "https://cdn.example.com/video.mp4",
                        "width": 1080,
                        "height": 1920,
                    }
                },
            }
        ]
    }
    http.json_payloads["pixabay_photos"] = {"hits": []}
    http.bytes_payloads["https://cdn.example.com/video.mp4"] = b"video-bytes"

    result = PixabayDownloadService(paths=paths, http_client=http).download_best("technology")

    assert result.status == "已下载"
    assert result.asset_type == "video"
    assert result.source_name == "Pixabay"
    assert Path(result.save_path).is_file()
    assert paths.fingerprint_registry_path.is_file()
    assert http.download_to_calls == [
        ("https://cdn.example.com/video.mp4", "Pixabay", "video")
    ]


def test_coverr_returns_clear_failure_when_no_mp4(tmp_path):
    paths = AutoMaterialPaths(root=tmp_path / "素材")
    http = _FakeHttp()
    http.json_payloads["coverr"] = {"videos": [{"id": "a1", "title": "No file"}]}

    result = CoverrDownloadService(paths=paths, http_client=http).download_best("office")

    assert result.status == "失败"
    assert "Coverr" in result.reason


def test_coverr_downloads_mp4_when_api_returns_nested_url(tmp_path):
    paths = AutoMaterialPaths(root=tmp_path / "素材")
    http = _FakeHttp()
    http.json_payloads["coverr"] = {
        "videos": [
            {
                "id": "coverr-1",
                "title": "Office",
                "files": {"mp4": "https://coverr.example.com/office.mp4"},
            }
        ]
    }
    http.bytes_payloads["https://coverr.example.com/office.mp4"] = b"coverr-video"

    result = CoverrDownloadService(paths=paths, http_client=http).download_best("office")

    assert result.status == "已下载"
    assert result.asset_type == "video"
    assert Path(result.save_path).is_file()
