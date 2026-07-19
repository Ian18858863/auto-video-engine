from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.materials.asset_registry import MaterialUsageRegistry
from app.materials.end_to_end_video import (
    EndToEndVideoService,
    MaterialAsset,
    TestScriptSegment as ScriptSegment,
    _as_file_uri,
    _assets_from_material_tasks,
    _decode_process_text,
    _ffmpeg_concat_line,
    _ffmpeg_path_arg,
    _make_segment_clip,
    build_test_script_segments,
    match_assets_to_segments,
)
from app.materials.pexels import PexelsDownloadItem, PexelsSampleDownloadResult
from app.models import HotItem, PipelineStep, ProductionPipeline, ScriptAsset, TopicIdea


def test_finished_video_list_and_delete_manage_only_run_directory(tmp_path):
    service = EndToEndVideoService(
        output_root=tmp_path / "成片测试",
        material_root=tmp_path / "素材",
        enable_real_downloads=False,
        enable_real_render=False,
    )
    result = service.run_test_export(topic="AI 搜索正在改变普通人获取信息的方式")
    video_path = Path(result["导出文件"]["最终成片 MP4"])
    video_path.write_bytes(b"fake mp4")
    external_material = tmp_path / "素材" / "自动下载" / "Pexels" / "视频" / "原始素材.mp4"
    external_material.parent.mkdir(parents=True, exist_ok=True)
    external_material.write_bytes(b"do not delete")

    videos = service.list_finished_videos()

    assert len(videos) == 1
    assert videos[0]["标题"] == "AI 搜索正在改变普通人获取信息的方式"
    assert videos[0]["状态"] == "已生成"
    assert videos[0]["最终成片路径"] == str(video_path)
    assert videos[0]["预览地址"].startswith("/api/editing-export/finished-videos/")

    delete_result = service.delete_finished_video(videos[0]["成片ID"])

    assert delete_result["已删除"] is True
    assert not Path(videos[0]["成片目录"]).exists()
    assert external_material.exists()


def test_end_to_end_service_defaults_to_d_drive_export_root(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.delenv("MATERIAL_ROOT_DIR", raising=False)
    get_settings.cache_clear()
    service = EndToEndVideoService()

    assert service.output_root == Path("D:/短视频工厂/归档/成片测试")
    get_settings.cache_clear()


def test_build_test_script_segments_returns_8_chinese_segments():
    segments = build_test_script_segments("AI 搜索正在改变普通人获取信息的方式")

    assert len(segments) == 8
    assert segments[0].start_seconds == 0
    assert segments[-1].end_seconds == 60
    assert all(segment.voiceover for segment in segments)
    assert any("普通人" in segment.voiceover for segment in segments)
    assert all(segment.keywords for segment in segments)


def test_match_assets_uses_keyword_category_source_and_backup_without_adjacent_repeat(tmp_path):
    direct = _asset(
        tmp_path,
        "source-page.mp4",
        "来源页面录屏",
        ["AI 搜索"],
        "AI",
        "来源页面",
        evidence_kind="direct_evidence",
        source_url="https://example.com/news",
    )
    pexels = _asset(tmp_path, "pexels.mp4", "Pexels视频", ["office"], "科技", "Pexels")
    info = _asset(
        tmp_path,
        "motion.mp4",
        "信息动效",
        ["平台流量"],
        "商业",
        "信息动效",
        evidence_kind="explanation",
        is_locally_generated=True,
    )
    backup = _asset(tmp_path, "backup.mp4", "备用素材", ["备用"], "生活", "备用")
    segments = build_test_script_segments("AI 搜索正在改变普通人获取信息的方式")

    matches = match_assets_to_segments(
        segments,
        [direct, pexels, info],
        backup_assets=[backup],
    )

    assert len(matches) == 8
    assert any(
        match.asset and match.asset.evidence_kind == "direct_evidence"
        for match in matches
    )
    assert any(match.asset and match.asset.asset_type == "Pexels视频" for match in matches)
    assert any(match.asset and match.asset.asset_type == "信息动效" for match in matches)
    assert all(match.match_reason for match in matches)
    for previous, current in zip(matches, matches[1:]):
        if previous.asset and current.asset:
            assert previous.asset.file_path != current.asset.file_path


def test_match_assets_marks_missing_when_no_asset_exists():
    segments = build_test_script_segments("AI 搜索正在改变普通人获取信息的方式")

    matches = match_assets_to_segments(segments, [], backup_assets=[])

    assert all(match.status == "缺素材" for match in matches)
    assert all(match.match_reason == "没有找到可用素材" for match in matches)


def test_match_assets_uses_semantic_source_fallback(tmp_path):
    direct = _asset(
        tmp_path,
        "source.mp4",
        "来源页面录屏",
        ["source"],
        "AI",
        "来源页面",
        evidence_kind="direct_evidence",
        source_url="https://example.com/source",
    )
    motion = _asset(
        tmp_path,
        "motion.mp4",
        "信息动效",
        ["motion"],
        "商业",
        "信息动效",
        evidence_kind="explanation",
        is_locally_generated=True,
    )
    segments = [
        ScriptSegment(
            "shot-01",
            0,
            5,
            "这里需要看原文证据。",
            "看原文证据",
            "官方新闻截图配科技办公背景",
            ["不匹配关键词"],
            "未知",
            "先找免费素材",
        ),
        ScriptSegment(
            "shot-02",
            5,
            10,
            "这里需要一个判断清单。",
            "判断清单",
            "三点判断清单信息卡片",
            ["不匹配关键词"],
            "未知",
            "先找免费素材",
        ),
    ]

    matches = match_assets_to_segments(segments, [direct, motion])

    assert matches[0].asset is not None
    assert matches[0].asset.evidence_kind == "direct_evidence"
    assert "语义匹配" in matches[0].match_reason
    assert matches[1].asset is not None
    assert matches[1].asset.asset_type == "信息动效"


def test_match_assets_skips_assets_used_in_last_two_days(tmp_path):
    now = datetime(2026, 6, 23, 0, 40, tzinfo=timezone.utc)
    recent = _asset(tmp_path, "recent.mp4", "Pexels视频", ["technology office"], "科技", "Pexels")
    alternative = _asset(tmp_path, "alternative.mp4", "Pexels视频", ["office"], "科技", "Pexels")
    registry = MaterialUsageRegistry(tmp_path / "素材使用记录.json")
    registry.record_use(
        str(recent.file_path),
        source_type="Pexels视频",
        used_at=now - timedelta(hours=2),
    )
    segment = build_test_script_segments("AI 搜索正在改变普通人获取信息的方式")[1]

    matches = match_assets_to_segments(
        [segment],
        [recent, alternative],
        usage_registry=registry,
        now=now,
    )

    assert matches[0].asset is not None
    assert matches[0].asset.file_path == alternative.file_path


def test_end_to_end_service_writes_report_with_chinese_fields(tmp_path):
    service = EndToEndVideoService(
        output_root=tmp_path / "成片测试",
        material_root=tmp_path / "素材",
        enable_real_downloads=False,
        enable_real_render=False,
    )

    result = service.run_test_export(topic="AI 搜索正在改变普通人获取信息的方式")

    assert result["成功"] is True
    assert result["选题"] == "AI 搜索正在改变普通人获取信息的方式"
    assert result["画面数量"] == 8
    assert Path(result["导出文件"]["报告 JSON"]).is_file()
    assert Path(result["导出文件"]["字幕 SRT"]).is_file()
    assert "最终成片 MP4" in result["导出文件"]
    assert result["剪辑参数"]["比例"] == "9:16"
    assert result["素材匹配"][0]["匹配原因"]
    assert {
        "evidence_kind",
        "source_url",
        "is_locally_generated",
    } <= set(result["素材匹配"][0])
    assert result["素材源结果"][0]["evidence_kind"] == "test_placeholder"
    assert result["素材源结果"][0]["source_url"] == ""
    assert result["素材源结果"][0]["is_locally_generated"] is True
    assert "官方证据" not in json.dumps(result, ensure_ascii=False)


def test_fake_assets_are_test_placeholders_without_fake_official_wording(tmp_path):
    service = EndToEndVideoService(
        output_root=tmp_path / "成片测试",
        material_root=tmp_path / "素材",
        enable_real_downloads=False,
        enable_real_render=False,
    )

    assets = service._fake_assets(tmp_path / "run")

    assert all(asset.evidence_kind == "test_placeholder" for asset in assets)
    assert all(asset.is_locally_generated is True for asset in assets)
    assert all(asset.source_url == "" for asset in assets)
    labels = [
        value
        for asset in assets
        for value in (asset.asset_type, asset.title, asset.source)
    ]
    assert all("官方证据" not in value for value in labels)


def test_partial_pexels_download_retains_successful_asset_provenance(
    tmp_path,
    monkeypatch,
):
    service = EndToEndVideoService(
        output_root=tmp_path / "成片测试",
        material_root=tmp_path / "素材",
        enable_real_downloads=True,
        enable_real_render=False,
    )
    service.paths.ensure_directories()
    video_path = service.paths.pexels_video_dir / "partial-video.mp4"
    video_path.write_bytes(b"pexels video")
    source_url = "https://videos.pexels.com/video-files/partial-video.mp4"

    class PartialPexelsService:
        def __init__(self, **_kwargs):
            pass

        def download_one_video_and_one_photo(self, keyword, *, per_page):
            assert keyword
            assert per_page == 5
            return PexelsSampleDownloadResult(
                keyword=keyword,
                success=False,
                video=PexelsDownloadItem(
                    asset_type="video",
                    status="已下载",
                    save_path=str(video_path),
                    source_url=source_url,
                ),
                photo=PexelsDownloadItem(
                    asset_type="photo",
                    status="失败",
                    reason="没有合格图片。",
                ),
                generated_at=datetime.now(),
            )

    monkeypatch.setattr(
        "app.materials.end_to_end_video.PexelsDownloadService",
        PartialPexelsService,
    )
    source_results: list[dict] = []

    assets = service._download_or_scan_pexels_assets([], source_results)

    matched = next(asset for asset in assets if asset.file_path == video_path)
    assert matched.evidence_kind == "atmosphere"
    assert matched.source_url == source_url
    successful_rows = [row for row in source_results if row["evidence_kind"] == "atmosphere"]
    assert successful_rows
    assert all(row["source_url"] == source_url for row in successful_rows)


def test_local_explanation_capture_is_never_direct_evidence(
    tmp_path,
    monkeypatch,
):
    class LocalCaptureService:
        def __init__(self, **_kwargs):
            pass

        def capture(self, plan):
            clip = plan.output_dir / "解释卡.mp4"
            clip.write_bytes(b"local explanation")
            return SimpleNamespace(
                success=True,
                scroll_recording_path=str(clip),
                first_screen_screenshot_path="",
                full_page_screenshot_path="",
                reason="本地解释卡已生成。",
                files=[str(clip)],
                used_placeholder=True,
            )

    monkeypatch.setattr(
        "app.materials.end_to_end_video.OfficialPageCaptureService",
        LocalCaptureService,
    )
    service = EndToEndVideoService(
        output_root=tmp_path / "成片测试",
        material_root=tmp_path / "素材",
        enable_real_downloads=True,
        enable_real_render=False,
    )
    source_results: list[dict] = []
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    asset = service._create_official_capture_asset(
        "AI 搜索",
        run_dir,
        [],
        source_results,
    )

    assert asset is not None
    assert asset.evidence_kind == "explanation"
    assert asset.source_url == ""
    assert asset.is_locally_generated is True
    assert "官方证据" not in json.dumps(source_results, ensure_ascii=False)


def test_task_reconstruction_preserves_provenance_fields(tmp_path):
    material = tmp_path / "material.mp4"
    material.write_bytes(b"material")

    assets = _assets_from_material_tasks(
        [
            {
                "material_path": str(material),
                "material_type": "信息动效",
                "source_type": "自动生成",
                "evidence_kind": "explanation",
                "source_url": "",
                "is_locally_generated": True,
            }
        ]
    )

    assert len(assets) == 1
    assert assets[0].evidence_kind == "explanation"
    assert assets[0].source_url == ""
    assert assets[0].is_locally_generated is True


def test_legacy_task_reconstruction_uses_conservative_provenance(tmp_path):
    motion = tmp_path / "motion.mp4"
    old_official = tmp_path / "old-official.mp4"
    motion.write_bytes(b"motion")
    old_official.write_bytes(b"unknown legacy capture")

    assets = _assets_from_material_tasks(
        [
            {
                "material_path": str(motion),
                "material_type": "信息动效",
                "source_type": "自动生成",
            },
            {
                "material_path": str(old_official),
                "material_type": "官方证据画面",
                "source_type": "官方页面",
            },
        ]
    )

    assert assets[0].evidence_kind == "explanation"
    assert assets[0].source_url == ""
    assert assets[0].is_locally_generated is True
    assert assets[1].evidence_kind != "direct_evidence"
    assert assets[1].source_url == ""
    assert assets[1].evidence_kind == "explanation"
    assert assets[1].is_locally_generated is False


def test_spoofed_official_label_cannot_outrank_direct_evidence(tmp_path):
    spoof = _asset(
        tmp_path,
        "spoof.mp4",
        "官方证据画面",
        ["none"],
        "未知",
        "伪标签",
        evidence_kind="explanation",
        is_locally_generated=True,
    )
    direct = _asset(
        tmp_path,
        "direct.mp4",
        "来源页面录屏",
        ["none"],
        "未知",
        "来源页面",
        evidence_kind="direct_evidence",
        source_url="https://example.com/news",
    )
    segment = ScriptSegment(
        "shot-01",
        0,
        5,
        "请查看原文证据。",
        "查看原文",
        "需要来源页面录屏",
        ["no-keyword-hit"],
        "其他",
        "先找证据",
    )

    match = match_assets_to_segments([segment], [spoof, direct])[0]

    assert match.asset is direct
    assert match.asset.evidence_kind == "direct_evidence"


def test_pipeline_export_uses_project_script_and_material_tasks(tmp_path):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    asset_path = tmp_path / "项目素材.mp4"
    asset_path.write_bytes(b"fake mp4")
    with Session(engine) as session:
        hotspot = HotItem(
            source_platform="测试源",
            source_type="新闻",
            title="真实项目热点",
            url="https://example.com/hotspot",
            summary="真实项目摘要",
            tags=["AI"],
            category="AI",
        )
        session.add(hotspot)
        session.flush()
        topic = TopicIdea(
            hot_item_id=hotspot.id,
            title="真实项目选题",
            core_view="不要复用固定测试稿",
            recommended_format="图文动画",
        )
        session.add(topic)
        session.flush()
        script = ScriptAsset(
            topic_idea_id=topic.id,
            voiceover="第一段：这是项目自己的口播。\n第二段：继续讲项目逻辑。",
            storyboard=[
                {
                    "shot_id": "shot-01",
                    "start": 0,
                    "end": 6,
                    "voiceover": "第一段：这是项目自己的口播。",
                    "recommended_visual": "项目自己的画面说明。",
                }
            ],
            subtitles=[{"text": "项目自己的字幕"}],
        )
        session.add(script)
        session.flush()
        pipeline = ProductionPipeline(
            hotspot_id=hotspot.id,
            topic_idea_id=topic.id,
            script_asset_id=script.id,
            project_name="真实项目选题",
            project_dir=str(tmp_path / "项目"),
            status="已完成",
        )
        session.add(pipeline)
        session.flush()
        session.add(
            PipelineStep(
                pipeline_id=pipeline.id,
                name="生成素材任务",
                step_order=4,
                status="已完成",
                output_summary={
                    "material_tasks": [
                        {
                            "shot_id": "shot-01",
                            "start": "00:00",
                            "end": "00:06",
                            "voiceover": "第一段：这是项目自己的口播。",
                            "recommended_visual": "项目自己的画面说明。",
                            "material_type": "Pexels视频",
                            "source_type": "Pexels视频",
                            "asset_keywords": ["AI"],
                            "category": "AI",
                            "material_path": str(asset_path),
                        }
                    ]
                },
            )
        )
        session.commit()
        pipeline_id = pipeline.id
        script_id = script.id

        service = EndToEndVideoService(
            output_root=tmp_path / "成片测试",
            material_root=tmp_path / "素材",
            enable_real_downloads=False,
            enable_real_render=False,
        )
        result = service.run_pipeline_export(session, pipeline_id)

    assert result["成功"] is True
    assert result["项目ID"] == pipeline_id
    assert result["脚本ID"] == script_id
    assert result["选题"] == "真实项目选题"
    assert result["简略脚本"] == ["第一段：这是项目自己的口播。"]
    assert "信息入口开始换位置" not in " ".join(result["简略脚本"])
    assert result["素材匹配"][0]["素材路径"] == str(asset_path)
    assert Path(result["导出文件"]["成片脚本"]).is_file()
    assert Path(result["导出文件"]["字幕 SRT"]).is_file()
    assert "最终成片 MP4" in result["导出文件"]


def test_file_uri_helper_resolves_relative_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    relative_file = Path("官方证据页.html")
    relative_file.write_text("<html></html>", encoding="utf-8")

    uri = _as_file_uri(relative_file)

    assert uri.startswith("file:///")
    assert quote("官方证据页.html") in uri


def test_decode_process_text_handles_non_utf8_bytes():
    decoded = _decode_process_text(b"\xff\xfeffmpeg \xe9\x94\x99\xe8\xaf\xaf")

    assert "ffmpeg" in decoded


def test_ffmpeg_concat_line_uses_absolute_path(tmp_path):
    clip_path = tmp_path / "clip_01.mp4"
    clip_path.write_bytes(b"fake")

    line = _ffmpeg_concat_line(clip_path)

    assert line.startswith("file '")
    assert str(tmp_path).replace("\\", "/") in line
    assert "clip_01.mp4" in line


def test_ffmpeg_path_arg_uses_absolute_path(tmp_path):
    video_path = tmp_path / "video_no_audio.mp4"

    value = _ffmpeg_path_arg(video_path)

    assert str(tmp_path) in value
    assert value.endswith("video_no_audio.mp4")


def test_make_segment_clip_uses_high_quality_vertical_transcode(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run_ffmpeg(args, *, cwd=None):
        calls.append(args)

    monkeypatch.setattr("app.materials.end_to_end_video._run_ffmpeg", fake_run_ffmpeg)
    asset_path = tmp_path / "source.mp4"
    asset_path.write_bytes(b"fake video")

    _make_segment_clip(asset_path, tmp_path / "clip.mp4", duration_seconds=6)

    args = calls[0]
    assert "-crf" in args
    assert args[args.index("-crf") + 1] == "18"
    assert args[args.index("-preset") + 1] == "medium"
    assert "flags=lanczos" in args[args.index("-vf") + 1]


def _asset(
    tmp_path: Path,
    filename: str,
    asset_type: str,
    keywords: list[str],
    category: str,
    source: str,
    *,
    evidence_kind: str = "atmosphere",
    source_url: str = "",
    is_locally_generated: bool = False,
) -> MaterialAsset:
    path = tmp_path / filename
    path.write_bytes(b"fake asset")
    return MaterialAsset(
        asset_id=filename,
        asset_type=asset_type,
        file_path=path,
        keywords=keywords,
        category=category,
        source=source,
        evidence_kind=evidence_kind,
        source_url=source_url,
        is_locally_generated=is_locally_generated,
    )
