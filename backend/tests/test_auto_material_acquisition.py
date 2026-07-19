from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.materials.asset_paths import AutoMaterialPathError, AutoMaterialPaths
from app.materials.asset_registry import MaterialFingerprintRegistry, MaterialUsageRegistry
from app.materials.official_capture import (
    OfficialPageCaptureService,
    build_official_capture_plan,
)
from app.materials.pexels import (
    PexelsAssetClient,
    PexelsAssetCandidate,
    PexelsDownloadItem,
    PexelsDownloadService,
    PexelsSampleDownloadResult,
)
from app.materials.planner import ShotSegment, build_material_plan
from app.materials.run_material_pipeline_smoke import (
    _run_backup_scan,
    _run_coverr_smoke,
    _run_official_capture_smoke,
    _run_pexels_smoke,
    _run_pixabay_smoke,
)
from app.materials.source_clients import MaterialSourceItem


def test_auto_material_paths_use_d_asset_layout_and_pexels_api_key_file(tmp_path):
    root = tmp_path / "素材"
    paths = AutoMaterialPaths(root=root)
    paths.ensure_directories()
    key_path = paths.pexels_root / "API Key"
    key_path.write_text("secret-pexels-key", encoding="utf-8")

    assert paths.backup_dir == root / "备用"
    assert paths.auto_download_dir == root / "自动下载"
    assert paths.pexels_video_dir == root / "自动下载" / "Pexels" / "视频"
    assert paths.pexels_photo_dir == root / "自动下载" / "Pexels" / "照片"
    assert paths.official_page_dir == root / "自动下载" / "官方页面"
    assert paths.generated_motion_dir == root / "自动生成"
    assert paths.usage_registry_path == root / "素材使用记录.json"
    assert paths.read_pexels_api_key() == "secret-pexels-key"


def test_auto_material_paths_reject_c_drive_even_inside_invoked_workspace():
    paths = AutoMaterialPaths(root=Path("C:/workspace/素材"))

    with pytest.raises(AutoMaterialPathError, match="不能配置到 C 盘"):
        paths.ensure_directories()


def test_pexels_api_key_reader_supports_env_file_but_reports_empty_file(tmp_path):
    paths = AutoMaterialPaths(root=tmp_path / "素材")
    paths.ensure_directories()
    legacy_key_file = paths.pexels_root / "API Key.env"
    legacy_key_file.write_text("", encoding="utf-8")

    try:
        paths.read_pexels_api_key()
    except Exception as exc:
        assert "API Key.env" in str(exc)
        assert "文件为空" in str(exc)
    else:
        raise AssertionError("empty API Key.env should not be accepted")

    legacy_key_file.write_text("PEXELS_API_KEY=abc123", encoding="utf-8")

    assert paths.read_pexels_api_key() == "abc123"


def test_pexels_api_key_reader_supports_api_key_folder_with_text_file(tmp_path):
    paths = AutoMaterialPaths(root=tmp_path / "素材")
    paths.ensure_directories()
    key_dir = paths.pexels_root / "API Key"
    key_dir.mkdir()
    (key_dir / "API Key.txt").write_text("folder-key-123", encoding="utf-8")

    assert paths.read_pexels_api_key() == "folder-key-123"


def test_pexels_client_saves_video_and_photo_without_keyword_folders(tmp_path):
    paths = AutoMaterialPaths(root=tmp_path / "素材")
    paths.ensure_directories()
    (paths.pexels_root / "API Key").write_text("fake-key", encoding="utf-8")
    client = PexelsAssetClient(paths=paths)
    video = PexelsAssetCandidate(
        pexels_id="video-1",
        asset_type="video",
        title="Business office",
        keyword="business meeting",
        download_url="https://example.com/video.mp4",
        width=1920,
        height=1080,
    )
    photo = PexelsAssetCandidate(
        pexels_id="photo-1",
        asset_type="photo",
        title="AI search",
        keyword="ai search",
        download_url="https://example.com/photo.jpg",
        width=1920,
        height=1080,
    )

    video_path = client.target_path(video, fetched_at=datetime(2026, 6, 22, 9, 15))
    photo_path = client.target_path(photo, fetched_at=datetime(2026, 6, 22, 9, 16))

    assert video_path.parent == paths.pexels_video_dir
    assert photo_path.parent == paths.pexels_photo_dir
    assert "business meeting" not in str(video_path.parent)
    assert video_path.suffix == ".mp4"
    assert photo_path.suffix == ".jpg"


def test_usage_registry_blocks_recent_reuse_and_penalizes_used_assets(tmp_path):
    registry = MaterialUsageRegistry(tmp_path / "素材使用记录.json")
    now = datetime(2026, 6, 22, 9, 15, tzinfo=timezone.utc)
    asset_path = "D:/素材/自动下载/Pexels/视频/素材.mp4"

    registry.record_use(asset_path, source_type="Pexels视频", used_at=now)

    assert registry.is_recently_used(asset_path, now=now + timedelta(hours=12))
    assert not registry.is_recently_used(asset_path, now=now + timedelta(days=3))
    assert registry.score_multiplier(asset_path, now=now + timedelta(hours=12)) == 0.0
    assert registry.score_multiplier(asset_path, now=now + timedelta(days=3)) < 1.0


def test_official_capture_plan_requires_scroll_recording_and_ad_avoidance(tmp_path):
    paths = AutoMaterialPaths(root=tmp_path / "素材")
    plan = build_official_capture_plan(
        url="https://example.com/news/ai-search",
        source_name="Example News",
        page_title="AI 搜索正在改变普通人获取信息的方式",
        paths=paths,
    )

    assert plan.need_scroll_recording is True
    assert plan.need_first_screen_screenshot is False
    assert plan.need_full_page_screenshot is False
    assert plan.output_dir == paths.official_page_dir / "Example News"
    assert any("ad" in selector.lower() for selector in plan.hide_selectors)
    assert any(selector in {"article", "main", "[role=main]"} for selector in plan.content_selectors)


def test_official_capture_service_writes_scroll_recording_without_required_screenshots(tmp_path):
    paths = AutoMaterialPaths(root=tmp_path / "素材")
    paths.ensure_directories()
    html_path = tmp_path / "官方页面.html"
    html_path.write_text(
        """
        <html><body>
          <aside>广告不要抓</aside>
          <main><h1>AI 搜索正在改变普通人获取信息的方式</h1>
          <p>这是官方证据正文。</p><p>继续滚动展示内容。</p></main>
        </body></html>
        """,
        encoding="utf-8",
    )
    plan = build_official_capture_plan(
        url=html_path.resolve().as_uri(),
        source_name="测试官网",
        page_title="AI 搜索正在改变普通人获取信息的方式",
        paths=paths,
    )

    result = OfficialPageCaptureService(
        paths=paths,
        use_playwright=False,
        fallback_to_placeholder=True,
    ).capture(plan)

    assert result.success is True
    assert result.scroll_recording_path
    assert Path(result.scroll_recording_path).is_file()
    assert Path(result.scroll_recording_path).parent == paths.official_page_dir / "测试官网"
    assert result.first_screen_screenshot_path == ""
    assert result.full_page_screenshot_path == ""
    assert "慢速滚动录屏" in result.reason
    assert getattr(result, "used_placeholder", None) is True
    assert "官方证据" not in result.reason


def test_successful_https_capture_is_distinguishable_from_placeholder(
    tmp_path,
    monkeypatch,
):
    paths = AutoMaterialPaths(root=tmp_path / "素材")
    paths.ensure_directories()
    plan = build_official_capture_plan(
        url="https://example.com/news/ai-search",
        source_name="Example News",
        page_title="AI 搜索",
        paths=paths,
    )

    def fake_capture(_self, _plan, *, frame_dir, first_screen_path, full_page_path):
        assert first_screen_path is None
        assert full_page_path is None
        frame_dir.mkdir(parents=True, exist_ok=True)

    def fake_frames_to_video(_pattern, output_path, *, duration_seconds, fps):
        assert duration_seconds == plan.scroll_recording_seconds
        assert fps == plan.scroll_recording_fps
        output_path.write_bytes(b"real-page-capture")

    monkeypatch.setattr(
        "app.materials.official_capture.OfficialPageCaptureService._capture_with_playwright",
        fake_capture,
    )
    monkeypatch.setattr(
        "app.materials.official_capture._frames_to_video",
        fake_frames_to_video,
    )

    result = OfficialPageCaptureService(paths=paths).capture(plan)

    assert result.success is True
    assert Path(result.scroll_recording_path).read_bytes() == b"real-page-capture"
    assert getattr(result, "used_placeholder", None) is False


def test_official_capture_uses_explicit_profile_and_download_directories(
    tmp_path,
    monkeypatch,
):
    paths = AutoMaterialPaths(root=tmp_path / "payload")
    paths.ensure_directories()
    profile_dir = paths.root / "browser-profile"
    downloads_dir = paths.root / "browser-downloads"
    plan = build_official_capture_plan(
        url="https://example.com/article",
        source_name="Example",
        page_title="Article",
        paths=paths,
    )
    observed: dict[str, object] = {}

    class FakePage:
        def set_default_navigation_timeout(self, value):
            observed["navigation_timeout"] = value

        def set_default_timeout(self, value):
            observed["operation_timeout"] = value

        def goto(self, *args, **kwargs):
            observed["goto"] = (args, kwargs)

        def wait_for_load_state(self, *args, **kwargs):
            return None

        def add_style_tag(self, **kwargs):
            return None

        def evaluate(self, script, *args):
            if "scrollHeight" in script:
                return 0
            return None

        def wait_for_timeout(self, _value):
            return None

        def screenshot(self, *, path, **_kwargs):
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"frame")

    class FakeContext:
        pages = [FakePage()]

        def close(self):
            observed["closed"] = True

    class FakeChromium:
        def launch(self, **_kwargs):
            raise AssertionError("ephemeral launch must not be used")

        def launch_persistent_context(self, **kwargs):
            observed["launch"] = kwargs
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakeManager:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: FakeManager(),
    )
    service = OfficialPageCaptureService(
        paths=paths,
        profile_dir=profile_dir,
        downloads_dir=downloads_dir,
    )
    frame_dir = plan.output_dir / "frames"

    service._capture_with_playwright(
        plan,
        frame_dir=frame_dir,
        first_screen_path=None,
        full_page_path=None,
    )

    launch = observed["launch"]
    assert launch["user_data_dir"] == str(profile_dir)
    assert launch["downloads_path"] == str(downloads_dir)
    assert profile_dir.resolve().is_relative_to(paths.root.resolve())
    assert downloads_dir.resolve().is_relative_to(paths.root.resolve())
    assert len(list(frame_dir.glob("official_*.png"))) >= 24
    assert observed["closed"] is True


def test_official_capture_generates_enough_frames_for_smooth_scroll(tmp_path):
    paths = AutoMaterialPaths(root=tmp_path / "素材")
    paths.ensure_directories()
    html_path = tmp_path / "官方页面.html"
    html_path.write_text("<html><body><main><h1>标题</h1><p>正文</p></main></body></html>", encoding="utf-8")
    plan = build_official_capture_plan(
        url=html_path.resolve().as_uri(),
        source_name="测试官网",
        page_title="AI 搜索正在改变普通人获取信息的方式",
        paths=paths,
    )

    result = OfficialPageCaptureService(
        paths=paths,
        use_playwright=False,
        fallback_to_placeholder=True,
    ).capture(plan)

    frame_dir = paths.official_page_dir / "测试官网" / f"{plan.filename_prefix}_frames"
    assert result.success is True
    assert getattr(result, "used_placeholder", None) is True
    assert plan.scroll_recording_fps >= 12
    assert len(list(frame_dir.glob("official_*.png"))) >= (
        plan.scroll_recording_seconds * plan.scroll_recording_fps
    )


def test_local_smoke_capture_is_labeled_as_explanation_not_official_evidence(
    tmp_path,
    monkeypatch,
):
    class LocalCapture:
        def __init__(self, **_kwargs):
            pass

        def capture(self, plan):
            output = plan.output_dir / "解释卡.mp4"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"explanation")
            return SimpleNamespace(
                success=True,
                reason="本地解释卡已生成。",
                scroll_recording_path=str(output),
                used_placeholder=True,
            )

    monkeypatch.setattr(
        "app.materials.run_material_pipeline_smoke.OfficialPageCaptureService",
        LocalCapture,
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    records: list[tuple] = []

    _run_official_capture_smoke(
        AutoMaterialPaths(root=tmp_path / "素材"),
        archive,
        lambda *values: records.append(values),
    )

    html_files = list(archive.glob("*.html"))
    assert len(html_files) == 1
    assert "官方证据" not in html_files[0].read_text(encoding="utf-8")
    assert records[0][0] == "本地解释卡"
    assert records[0][4:] == ("explanation", "", True)


def test_smoke_stock_and_backup_rows_preserve_atmosphere_provenance(
    tmp_path,
    monkeypatch,
):
    pexels_url = "https://videos.pexels.com/example.mp4"
    pixabay_url = "https://pixabay.com/videos/example-1/"
    coverr_url = "https://coverr.co/videos/example"

    class PexelsService:
        def __init__(self, **_kwargs):
            pass

        def download_one_video_and_one_photo(self, _keyword, *, per_page):
            assert per_page == 5
            return PexelsSampleDownloadResult(
                keyword="technology office",
                success=False,
                video=PexelsDownloadItem(
                    asset_type="video",
                    status="已下载",
                    save_path=str(tmp_path / "pexels.mp4"),
                    source_url=pexels_url,
                ),
                photo=PexelsDownloadItem(asset_type="photo", status="失败"),
                generated_at=datetime.now(),
            )

    class PixabayService:
        def __init__(self, **_kwargs):
            pass

        def download_best(self, _keyword, *, per_page):
            assert per_page == 5
            return MaterialSourceItem(
                source_name="Pixabay",
                asset_type="video",
                status="已下载",
                save_path=str(tmp_path / "pixabay.mp4"),
                source_url=pixabay_url,
            )

    class CoverrService:
        def __init__(self, **_kwargs):
            pass

        def download_best(self, _keyword, *, per_page):
            assert per_page == 5
            return MaterialSourceItem(
                source_name="Coverr",
                asset_type="video",
                status="已下载",
                save_path=str(tmp_path / "coverr.mp4"),
                source_url=coverr_url,
            )

    monkeypatch.setattr(
        "app.materials.run_material_pipeline_smoke.PexelsDownloadService",
        PexelsService,
    )
    monkeypatch.setattr(
        "app.materials.run_material_pipeline_smoke.PixabayDownloadService",
        PixabayService,
    )
    monkeypatch.setattr(
        "app.materials.run_material_pipeline_smoke.CoverrDownloadService",
        CoverrService,
    )
    paths = AutoMaterialPaths(root=tmp_path / "素材")
    paths.ensure_directories()
    (paths.backup_dir / "backup.mp4").write_bytes(b"backup")
    records: list[tuple] = []
    record = lambda *values: records.append(values)

    _run_pexels_smoke(paths, record)
    _run_pixabay_smoke(paths, record)
    _run_coverr_smoke(paths, record)
    _run_backup_scan(paths, record)

    successful = {row[0]: row for row in records if row[1] in {"已下载", "成功"}}
    assert successful["Pexels视频"][4:] == ("atmosphere", pexels_url, False)
    assert successful["Pixabay"][4:] == ("atmosphere", pixabay_url, False)
    assert successful["Coverr"][4:] == ("atmosphere", coverr_url, False)
    assert successful["备选素材"][4:] == ("atmosphere", "", False)


def test_material_plan_uses_new_auto_asset_sources_not_jianying_by_default():
    plan = build_material_plan(
        [
            ShotSegment(
                start="00:00",
                end="00:06",
                voiceover="为什么 AI 搜索正在改变普通人获取信息？",
                recommended_visual="官方新闻页面慢速滚动 + 数据流信息动效",
            )
        ],
        project_name="AI搜索趋势",
    )
    task = plan.material_tasks[0]

    assert task.needs_official_capture is True
    assert task.needs_pexels is True
    assert task.needs_info_motion is True
    assert task.needs_jianying is False
    assert "官方录屏" in task.source_priority
    assert "Pexels视频" in task.source_priority
    assert "信息动效" in task.source_priority
    assert "剪映素材" not in task.source_priority
    assert task.asset_keywords


class _FakePexelsHttpClient:
    def __init__(self, *, empty_video: bool = False, low_quality_video: bool = False):
        self.empty_video = empty_video
        self.low_quality_video = low_quality_video
        self.download_to_calls: list[tuple[str, str, str]] = []

    def get_json(self, url, *, headers, params):
        if "videos" in url:
            if self.empty_video:
                return {"videos": []}
            if self.low_quality_video:
                width = 1280
                height = 720
            else:
                width = 1920
                height = 1080
            return {
                "videos": [
                    {
                        "id": 1001,
                        "width": width,
                        "height": height,
                        "duration": 8,
                        "url": "https://www.pexels.com/video/test-video/",
                        "user": {
                            "id": 91,
                            "name": "Pexels Creator",
                            "url": "https://www.pexels.com/@pexels-creator/",
                        },
                        "video_files": [
                            {
                                "id": 1,
                                "quality": "hd",
                                "width": width,
                                "height": height,
                                "file_type": "video/mp4",
                                "link": "https://videos.pexels.com/test-video.mp4",
                            }
                        ],
                    }
                ]
            }
        return {
            "photos": [
                {
                    "id": 2001,
                    "width": 1920,
                    "height": 1280,
                    "url": "https://www.pexels.com/photo/test-photo/",
                    "alt": "Technology office",
                    "photographer": "Pexels Photographer",
                    "photographer_id": 92,
                    "photographer_url": "https://www.pexels.com/@pexels-photographer/",
                    "src": {"large2x": "https://images.pexels.com/test-photo.jpg"},
                }
            ]
        }

    def get_bytes(self, url, *, headers):
        raise AssertionError("streaming download_to must be preferred")

    def download_to(
        self,
        url,
        destination,
        *,
        source_name,
        asset_type,
        headers=None,
    ):
        self.download_to_calls.append((url, source_name, asset_type))
        content = b"fake video bytes" if url.endswith(".mp4") else b"fake photo bytes"
        destination.write_bytes(content)


def test_pexels_service_downloads_one_video_and_one_photo_with_chinese_result(tmp_path):
    paths = AutoMaterialPaths(root=tmp_path / "素材")
    paths.ensure_directories()
    paths.pexels_api_key_path.write_text("fake-key", encoding="utf-8")
    http_client = _FakePexelsHttpClient()
    service = PexelsDownloadService(paths=paths, http_client=http_client)

    result = service.download_one_video_and_one_photo("technology office")
    payload = result.to_chinese_dict()

    assert payload["成功"] is True
    assert payload["关键词"] == "technology office"
    assert payload["视频"]["状态"] == "已下载"
    assert payload["图片"]["状态"] == "已下载"
    assert Path(payload["视频"]["保存路径"]).parent == paths.pexels_video_dir
    assert Path(payload["图片"]["保存路径"]).parent == paths.pexels_photo_dir
    assert Path(payload["视频"]["保存路径"]).is_file()
    assert Path(payload["图片"]["保存路径"]).is_file()
    assert payload["视频"]["文件指纹"]
    assert payload["图片"]["文件指纹"]
    assert paths.fingerprint_registry_path.is_file()
    assert http_client.download_to_calls == [
        ("https://videos.pexels.com/test-video.mp4", "Pexels", "video"),
        ("https://images.pexels.com/test-photo.jpg", "Pexels", "photo"),
    ]


def test_pexels_service_records_failure_reason_when_video_has_no_result(tmp_path):
    paths = AutoMaterialPaths(root=tmp_path / "素材")
    paths.ensure_directories()
    paths.pexels_api_key_path.write_text("fake-key", encoding="utf-8")
    service = PexelsDownloadService(
        paths=paths,
        http_client=_FakePexelsHttpClient(empty_video=True),
    )

    result = service.download_one_video_and_one_photo("no result keyword")

    assert result.success is False
    assert result.video.status == "失败"
    assert "Pexels 没有找到视频结果" in result.video.reason
    assert result.to_chinese_dict()["失败原因"]


def test_pexels_service_rejects_low_resolution_video_that_would_blur_in_vertical_export(tmp_path):
    paths = AutoMaterialPaths(root=tmp_path / "素材")
    paths.ensure_directories()
    paths.pexels_api_key_path.write_text("fake-key", encoding="utf-8")
    service = PexelsDownloadService(
        paths=paths,
        http_client=_FakePexelsHttpClient(low_quality_video=True),
    )

    result = service.download_one_video_and_one_photo("technology office")

    assert result.video.status == "失败"
    assert "低于 1080p" in result.video.reason
    assert not list(paths.pexels_video_dir.glob("*.mp4"))


def test_fingerprint_registry_detects_duplicate_content_not_only_filename(tmp_path):
    registry = MaterialFingerprintRegistry(tmp_path / "素材指纹.json")
    first = tmp_path / "one.mp4"
    second = tmp_path / "two-different-name.mp4"
    first.write_bytes(b"same bytes")
    second.write_bytes(b"same bytes")

    first_fingerprint = registry.record_file(first, source_type="Pexels视频")
    duplicate = registry.find_duplicate(second)

    assert duplicate is not None
    assert duplicate.fingerprint == first_fingerprint.fingerprint
    assert duplicate.file_path == str(first)
