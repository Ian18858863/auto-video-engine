from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy.orm import Session, selectinload

from app.audio.subtitle_service import SubtitleSegment, write_srt
from app.audio.tts_service import synthesize_tts
from app.core.config import get_settings, resolve_configured_path
from app.materials.asset_paths import AutoMaterialPaths
from app.materials.asset_registry import MaterialUsageRegistry
from app.materials.official_capture import (
    OfficialPageCaptureService,
    build_official_capture_plan,
)
from app.materials.pexels import PexelsDownloadService
from app.materials.provenance import (
    classify_legacy_material_provenance,
    classify_capture_provenance,
    validate_success_provenance,
)
from app.materials.source_clients import CoverrDownloadService, PixabayDownloadService
from app.models import HotItem, ProductionPipeline, ScriptAsset, TopicIdea
from app.pipeline.material_candidate_gate import assert_pipeline_candidate_review_ready


VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VERTICAL_EXPORT_FILTER = (
    "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
    "crop=1080:1920,fps=30,format=yuv420p"
)
HIGH_QUALITY_VIDEO_ARGS = [
    "-c:v",
    "libx264",
    "-preset",
    "medium",
    "-crf",
    "18",
    "-maxrate",
    "12M",
    "-bufsize",
    "24M",
    "-pix_fmt",
    "yuv420p",
    "-r",
    "30",
]


@dataclass(slots=True)
class TestScriptSegment:
    shot_id: str
    start_seconds: int
    end_seconds: int
    voiceover: str
    subtitle: str
    visual_description: str
    keywords: list[str]
    category: str
    preferred_source: str

    @property
    def duration_seconds(self) -> int:
        return self.end_seconds - self.start_seconds


@dataclass(slots=True)
class MaterialAsset:
    asset_id: str
    asset_type: str
    file_path: Path
    keywords: list[str] = field(default_factory=list)
    category: str = ""
    source: str = ""
    title: str = ""
    usage_count: int = 0
    evidence_kind: str = "atmosphere"
    source_url: str = ""
    is_locally_generated: bool = False

    def __post_init__(self) -> None:
        validate_success_provenance(
            evidence_kind=self.evidence_kind,
            source_url=self.source_url,
            is_locally_generated=self.is_locally_generated,
        )


@dataclass(slots=True)
class MaterialMatch:
    segment: TestScriptSegment
    asset: MaterialAsset | None
    status: str
    match_reason: str

    def to_chinese_dict(self) -> dict[str, Any]:
        return {
            "镜头": self.segment.shot_id,
            "时间段": f"{_time(self.segment.start_seconds)}-{_time(self.segment.end_seconds)}",
            "口播": self.segment.voiceover,
            "字幕": self.segment.subtitle,
            "画面说明": self.segment.visual_description,
            "素材关键词": self.segment.keywords,
            "素材状态": self.status,
            "素材类型": self.asset.asset_type if self.asset else "缺素材",
            "素材路径": str(self.asset.file_path) if self.asset else "",
            "evidence_kind": self.asset.evidence_kind if self.asset else "",
            "source_url": self.asset.source_url if self.asset else "",
            "is_locally_generated": (
                self.asset.is_locally_generated if self.asset else False
            ),
            "匹配原因": self.match_reason,
        }


def build_test_script_segments(topic: str) -> list[TestScriptSegment]:
    return [
        TestScriptSegment(
            "shot-01",
            0,
            7,
            f"先说结论：{topic}，不是一个小功能变化，而是信息入口开始换位置。",
            "信息入口开始换位置",
            "来源页面慢速滚动，标题和核心信息高亮。",
            ["AI 搜索", "信息入口", "搜索"],
            "AI",
            "官方页面",
        ),
        TestScriptSegment(
            "shot-02",
            7,
            14,
            "过去我们习惯先打开搜索框，再自己筛网页；现在越来越多人直接问 AI。",
            "从搜网页，变成问 AI",
            "Pexels 科技办公或手机搜索场景。",
            ["technology office", "search app", "phone search"],
            "科技",
            "Pexels视频",
        ),
        TestScriptSegment(
            "shot-03",
            14,
            22,
            "这背后真正变的是分发逻辑：谁先回答用户，谁就更容易拿到注意力。",
            "谁先回答，谁拿注意力",
            "信息动效：数据流进入一个 AI 回答框。",
            ["平台流量", "注意力", "AI answer"],
            "商业",
            "信息动效",
        ),
        TestScriptSegment(
            "shot-04",
            22,
            29,
            "对平台来说，这会影响广告、内容生态，还有创作者获取流量的方式。",
            "广告和内容生态都会被影响",
            "Pexels 商业会议、数据分析屏幕或办公室素材。",
            ["business meeting", "data analysis", "office"],
            "商业",
            "Pexels视频",
        ),
        TestScriptSegment(
            "shot-05",
            29,
            37,
            "对普通人来说，重点不是马上换工具，而是学会判断：答案从哪里来，靠不靠谱。",
            "普通人要看答案来源",
            "来源页面局部放大，强调来源和链接。",
            ["来源", "可信度", "官方页面"],
            "生活",
            "官方页面",
        ),
        TestScriptSegment(
            "shot-06",
            37,
            45,
            "如果一个 AI 只给结论、不给出处，你就别急着全信；这不是效率，是风险。",
            "只给结论不给出处，是风险",
            "信息动效：风险提示卡片、扫描线、红色高亮。",
            ["风险", "出处", "判断标准"],
            "生活",
            "信息动效",
        ),
        TestScriptSegment(
            "shot-07",
            45,
            53,
            "更值得关注的是，未来内容可能不是拼谁写得多，而是拼谁能被 AI 正确引用。",
            "未来要拼能不能被 AI 引用",
            "Pexels 创作者、键盘、屏幕内容生产画面。",
            ["creator", "keyboard", "content"],
            "AI",
            "Pexels视频",
        ),
        TestScriptSegment(
            "shot-08",
            53,
            60,
            "所以这条变化可以先记住：搜索入口变了，普通人也要学会检查答案来源。",
            "搜索入口变了，要检查来源",
            "信息动效总结卡片，三点判断清单收束。",
            ["判断清单", "搜索入口", "总结"],
            "AI",
            "信息动效",
        ),
    ]


def match_assets_to_segments(
    segments: list[TestScriptSegment],
    assets: list[MaterialAsset],
    *,
    backup_assets: list[MaterialAsset] | None = None,
    usage_registry: MaterialUsageRegistry | None = None,
    now: datetime | None = None,
) -> list[MaterialMatch]:
    backup_assets = backup_assets or []
    matches: list[MaterialMatch] = []
    previous_path = ""
    backup_used = False
    for segment in segments:
        candidates = _rank_candidates(
            segment,
            assets,
            previous_path,
            usage_registry=usage_registry,
            now=now,
        )
        if candidates:
            score, reason, asset = candidates[0]
            matches.append(MaterialMatch(segment, asset, "已匹配", reason))
            previous_path = str(asset.file_path)
            continue
        backup_candidates = _rank_candidates(
            segment,
            backup_assets,
            previous_path,
            usage_registry=usage_registry,
            now=now,
        )
        if backup_candidates and not backup_used:
            _, _, asset = backup_candidates[0]
            matches.append(MaterialMatch(segment, asset, "已匹配", "使用备用素材：常规素材不足，本轮最多使用 1 个备用。"))
            previous_path = str(asset.file_path)
            backup_used = True
            continue
        matches.append(MaterialMatch(segment, None, "缺素材", "没有找到可用素材"))
        previous_path = ""
    return matches


class EndToEndVideoService:
    def __init__(
        self,
        *,
        output_root: Path | None = None,
        material_root: Path = Path("D:/素材"),
        enable_real_downloads: bool = True,
        enable_real_render: bool = True,
    ):
        settings = get_settings()
        self.output_root = resolve_configured_path(
            output_root
            or (resolve_configured_path(settings.material_root_dir) / "归档" / "成片测试")
        )
        self.material_root = resolve_configured_path(material_root)
        self.paths = AutoMaterialPaths(root=material_root)
        self.enable_real_downloads = enable_real_downloads
        self.enable_real_render = enable_real_render

    def run_test_export(self, *, topic: str) -> dict[str, Any]:
        now = datetime.now()
        run_dir = self.output_root / f"{_safe_filename(topic)}_{now:%Y%m%d_%H%M%S}"
        run_dir.mkdir(parents=True, exist_ok=True)
        segments = build_test_script_segments(topic)
        warnings: list[str] = []
        source_results: list[dict[str, Any]] = []

        if self.enable_real_downloads:
            assets = self._collect_real_assets(topic, run_dir, warnings, source_results)
        else:
            assets = self._fake_assets(run_dir)
            source_results.append(
                {
                    "素材源": "测试占位素材",
                    "状态": "已生成",
                    "原因": "已跳过真实下载，并生成测试占位素材验证成片链路。",
                    "路径": "",
                    "evidence_kind": "test_placeholder",
                    "source_url": "",
                    "is_locally_generated": True,
                }
            )
        backup_assets = self._scan_backup_assets()
        usage_registry = MaterialUsageRegistry(self.paths.usage_registry_path)
        matches = match_assets_to_segments(
            segments,
            assets,
            backup_assets=backup_assets,
            usage_registry=usage_registry,
        )
        srt_path = run_dir / "字幕.srt"
        write_srt(srt_path, _subtitle_segments_from_test_segments(segments))
        script_path = run_dir / "测试脚本.md"
        script_path.write_text(_script_markdown(topic, segments, matches), encoding="utf-8")

        final_video_path = run_dir / "一分钟测试成片.mp4"
        voiceover_path = run_dir / "口播音频.wav"
        if self.enable_real_render:
            try:
                self._render_final_video(run_dir, matches, srt_path, voiceover_path, final_video_path, warnings)
            except Exception as exc:
                warnings.append(f"成片导出失败：{exc}")
        report = {
            "成功": final_video_path.is_file() if self.enable_real_render else True,
            "选题": topic,
            "简略脚本": [segment.voiceover for segment in segments],
            "画面数量": len(segments),
            "素材匹配": [match.to_chinese_dict() for match in matches],
            "素材源结果": source_results,
            "导出文件": {
                "报告 JSON": str(run_dir / "成片报告.json"),
                "测试脚本": str(script_path),
                "字幕 SRT": str(srt_path),
                "口播音频": str(voiceover_path),
                "最终成片 MP4": str(final_video_path),
            },
            "剪辑参数": {
                "格式": ".mp4",
                "视频编码": "H.264",
                "音频编码": "AAC",
                "比例": "9:16",
                "分辨率": "1080x1920",
                "帧率": "30fps",
                "码率": "约 10 Mbps",
                "字幕": "已烧录" if final_video_path.is_file() else "待烧录",
            },
            "提醒": warnings,
        }
        report_path = run_dir / "成片报告.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def run_pipeline_export(self, session: Session, pipeline_id: int) -> dict[str, Any]:
        pipeline = (
            session.query(ProductionPipeline)
            .options(selectinload(ProductionPipeline.steps))
            .filter(ProductionPipeline.id == pipeline_id)
            .one_or_none()
        )
        if pipeline is None:
            raise FileNotFoundError(f"项目流程不存在：{pipeline_id}")
        if pipeline.script_asset_id is None:
            raise ValueError("这个项目还没有脚本，不能生成成片。")
        script = session.get(ScriptAsset, pipeline.script_asset_id)
        if script is None:
            raise ValueError("脚本素材不存在，不能生成成片。")
        topic = (
            session.get(TopicIdea, pipeline.topic_idea_id)
            if pipeline.topic_idea_id is not None
            else None
        )
        hotspot = session.get(HotItem, pipeline.hotspot_id)
        title = pipeline.project_name or (topic.title if topic else "") or (hotspot.title if hotspot else "") or "未命名项目"
        now = datetime.now()
        run_dir = self.output_root / f"{_safe_filename(title)}_{now:%Y%m%d_%H%M%S}"
        warnings: list[str] = []
        source_results: list[dict[str, Any]] = []
        material_tasks = _pipeline_material_tasks(pipeline)
        assert_pipeline_candidate_review_ready(
            pipeline,
            [str(item.get("shot_id") or "") for item in material_tasks],
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        segments = _segments_from_pipeline_script(
            script,
            title=title,
            material_tasks=material_tasks,
        )
        assets = _assets_from_material_tasks(material_tasks)
        if self.enable_real_downloads:
            assets.extend(self._collect_real_assets(title, run_dir, warnings, source_results))
        else:
            source_results.append(
                {
                    "素材源": "真实下载",
                    "状态": "已跳过",
                    "原因": "当前配置禁用真实下载，仅使用项目已有素材。",
                    "路径": "",
                    "evidence_kind": "",
                    "source_url": "",
                    "is_locally_generated": False,
                }
            )
        backup_assets = self._scan_backup_assets()
        usage_registry = MaterialUsageRegistry(self.paths.usage_registry_path)
        matches = match_assets_to_segments(
            segments,
            assets,
            backup_assets=backup_assets,
            usage_registry=usage_registry,
        )
        srt_path = run_dir / "字幕.srt"
        write_srt(srt_path, _subtitle_segments_from_test_segments(segments))
        script_path = run_dir / "成片脚本.md"
        script_path.write_text(_script_markdown(title, segments, matches), encoding="utf-8")
        final_video_path = run_dir / "最终成片.mp4"
        voiceover_path = run_dir / "口播音频.wav"
        if self.enable_real_render:
            try:
                self._render_final_video(run_dir, matches, srt_path, voiceover_path, final_video_path, warnings)
            except Exception as exc:
                warnings.append(f"成片导出失败：{exc}")
        report = {
            "成功": final_video_path.is_file() if self.enable_real_render else True,
            "项目ID": pipeline.id,
            "脚本ID": script.id,
            "选题": title,
            "简略脚本": [segment.voiceover for segment in segments],
            "画面数量": len(segments),
            "素材匹配": [match.to_chinese_dict() for match in matches],
            "素材源结果": source_results,
            "导出文件": {
                "报告 JSON": str(run_dir / "成片报告.json"),
                "成片脚本": str(script_path),
                "字幕 SRT": str(srt_path),
                "口播音频": str(voiceover_path),
                "最终成片 MP4": str(final_video_path),
            },
            "剪辑参数": {
                "格式": ".mp4",
                "视频编码": "H.264",
                "音频编码": "AAC",
                "比例": "9:16",
                "分辨率": "1080x1920",
                "帧率": "30fps",
                "码率": "约 10 Mbps",
                "字幕": "已烧录" if final_video_path.is_file() else "待烧录",
            },
            "提醒": warnings,
        }
        report_path = run_dir / "成片报告.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def latest_report(self) -> dict[str, Any]:
        reports = sorted(
            self.output_root.glob("*/成片报告.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not reports:
            return {
                "成功": False,
                "提示": "还没有生成过测试成片。",
                "导出文件": {},
                "素材匹配": [],
            }
        return json.loads(reports[0].read_text(encoding="utf-8"))

    def list_finished_videos(self, *, pipeline_id: int | None = None) -> list[dict[str, Any]]:
        reports = sorted(
            self.output_root.glob("*/成片报告.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        items: list[dict[str, Any]] = []
        for report_path in reports:
            if not report_path.is_file():
                continue
            item = self._finished_video_item(report_path)
            if pipeline_id is not None and item.get("pipeline_id") != pipeline_id:
                continue
            if pipeline_id is not None:
                item["预览地址"] = (
                    f"/api/editing-export/finished-videos/{report_path.parent.name}/video"
                    f"?pipeline_id={pipeline_id}"
                )
            items.append(item)
        return items

    def validate_finished_video_pipeline(
        self,
        video_id: str,
        pipeline_id: int | None,
    ) -> None:
        """Reject cross-project legacy video actions when a project is known."""

        if pipeline_id is None:
            return
        item = self._finished_video_item(
            self._resolve_run_dir(video_id) / "成片报告.json"
        )
        if item.get("pipeline_id") != pipeline_id:
            raise FileNotFoundError("成片不属于当前项目")

    def delete_finished_video(self, video_id: str) -> dict[str, Any]:
        run_dir = self._resolve_run_dir(video_id)
        shutil.rmtree(run_dir)
        return {
            "已删除": True,
            "成片ID": video_id,
            "删除目录": str(run_dir),
            "说明": "已删除这条成片生成出来的一套文件，未删除全局原始素材库。",
        }

    def export_finished_video(self, video_id: str) -> dict[str, Any]:
        item = self._finished_video_item(self._resolve_run_dir(video_id) / "成片报告.json")
        source = Path(item["最终成片路径"])
        if not source.is_file():
            raise FileNotFoundError("最终成片 MP4 不存在，无法导出。")
        export_dir = resolve_configured_path(get_settings().export_dir).parent / "成片导出"
        export_dir.mkdir(parents=True, exist_ok=True)
        destination = export_dir / f"{_safe_filename(item['标题'])}_{datetime.now():%Y%m%d_%H%M%S}.mp4"
        shutil.copy2(source, destination)
        return {
            "已导出": True,
            "成片ID": video_id,
            "导出路径": str(destination),
        }

    def open_finished_video_folder(self, video_id: str) -> dict[str, Any]:
        item = self._finished_video_item(self._resolve_run_dir(video_id) / "成片报告.json")
        video_path = Path(item["最终成片路径"])
        target = video_path if video_path.exists() else Path(item["成片目录"])
        try:
            subprocess.Popen(["explorer.exe", f"/select,{target}"])  # noqa: S603,S607
        except Exception as exc:
            raise RuntimeError(f"打开文件夹失败：{exc}") from exc
        return {
            "已打开": True,
            "成片ID": video_id,
            "路径": str(target),
        }

    def video_file_path(self, video_id: str) -> Path:
        item = self._finished_video_item(self._resolve_run_dir(video_id) / "成片报告.json")
        path = Path(item["最终成片路径"])
        if not path.is_file():
            raise FileNotFoundError("最终成片 MP4 不存在。")
        return path

    def _finished_video_item(self, report_path: Path) -> dict[str, Any]:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        run_dir = report_path.parent
        video_path = Path((report.get("导出文件") or {}).get("最终成片 MP4", ""))
        success = bool(report.get("成功")) and video_path.is_file()
        created_at = datetime.fromtimestamp(report_path.stat().st_mtime).isoformat(timespec="seconds")
        return {
            "成片ID": run_dir.name,
            "pipeline_id": report.get("项目ID"),
            "标题": str(report.get("选题") or run_dir.name),
            "生成时间": created_at,
            "画面数量": int(report.get("画面数量") or 0),
            "状态": "已生成" if success else "生成失败",
            "成片目录": str(run_dir),
            "最终成片路径": str(video_path) if video_path else "",
            "预览地址": f"/api/editing-export/finished-videos/{run_dir.name}/video",
            "提醒": report.get("提醒") or [],
        }

    def _resolve_run_dir(self, video_id: str) -> Path:
        cleaned = str(video_id or "").strip()
        if not cleaned or cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
            raise FileNotFoundError("成片不存在。")
        root = self.output_root.resolve()
        run_dir = (root / cleaned).resolve()
        if not run_dir.is_dir() or not run_dir.is_relative_to(root):
            raise FileNotFoundError("成片不存在。")
        report_path = run_dir / "成片报告.json"
        if not report_path.is_file():
            raise FileNotFoundError("成片报告不存在。")
        return run_dir

    def _collect_real_assets(
        self,
        topic: str,
        run_dir: Path,
        warnings: list[str],
        source_results: list[dict[str, Any]],
    ) -> list[MaterialAsset]:
        self.paths.ensure_directories()
        assets: list[MaterialAsset] = []
        official = self._create_official_capture_asset(topic, run_dir, warnings, source_results)
        if official:
            assets.append(official)
        assets.extend(self._download_or_scan_pexels_assets(warnings, source_results))
        assets.extend(self._download_or_scan_pixabay_assets(warnings, source_results))
        assets.extend(self._download_or_scan_coverr_assets(warnings, source_results))
        motion_assets = self._create_info_motion_assets(topic, run_dir, warnings)
        source_results.append(
            {
                "素材源": "信息动效",
                "状态": "已生成" if motion_assets else "失败",
                "原因": f"生成 {len(motion_assets)} 个本地信息动效素材。",
                "路径": "; ".join(str(asset.file_path) for asset in motion_assets),
                "evidence_kind": "explanation" if motion_assets else "",
                "source_url": "",
                "is_locally_generated": bool(motion_assets),
            }
        )
        assets.extend(motion_assets)
        return assets

    def _fake_assets(self, run_dir: Path) -> list[MaterialAsset]:
        assets_dir = run_dir / "fake-assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        fake_assets: list[MaterialAsset] = []
        for filename, asset_type, keywords, category, source in [
            ("page-placeholder.mp4", "测试占位画面", ["AI 搜索"], "AI", "测试占位"),
            ("stock-placeholder.mp4", "测试占位视频", ["technology office"], "科技", "测试占位"),
            ("motion-placeholder.mp4", "测试占位动效", ["平台流量"], "商业", "测试占位"),
        ]:
            path = assets_dir / filename
            path.write_bytes(b"fake asset")
            fake_assets.append(
                MaterialAsset(
                    filename,
                    asset_type,
                    path,
                    keywords,
                    category,
                    source,
                    title="测试占位",
                    evidence_kind="test_placeholder",
                    is_locally_generated=True,
                )
            )
        return fake_assets

    def _create_official_capture_asset(
        self,
        topic: str,
        run_dir: Path,
        warnings: list[str],
        source_results: list[dict[str, Any]],
    ) -> MaterialAsset | None:
        output_dir = self.paths.official_page_dir / "本地解释卡"
        output_dir.mkdir(parents=True, exist_ok=True)
        html_path = run_dir / "解释卡.html"
        html_path.write_text(_explanation_html(topic), encoding="utf-8")
        plan = build_official_capture_plan(
            url=_as_file_uri(html_path),
            source_name="本地解释卡",
            page_title=topic,
            paths=self.paths,
        )
        plan.output_dir = output_dir
        plan.filename_prefix = f"{_safe_filename(topic)}_{datetime.now():%Y%m%d_%H%M%S}"
        plan.scroll_recording_seconds = 8
        capture = OfficialPageCaptureService(
            paths=self.paths,
            fallback_to_placeholder=True,
        ).capture(plan)
        if not capture.success:
            warnings.append(f"本地解释卡生成失败：{capture.reason}")
            source_results.append(
                {
                    "素材源": "本地解释卡",
                    "状态": "失败",
                    "原因": capture.reason,
                    "路径": "",
                    "evidence_kind": "",
                    "source_url": "",
                    "is_locally_generated": False,
                }
            )
            return None
        clip_path = Path(capture.scroll_recording_path)
        provenance = classify_capture_provenance(
            plan.url,
            used_placeholder=capture.used_placeholder,
        )
        source_results.append(
            {
                "素材源": "本地解释卡",
                "状态": "已生成",
                "原因": capture.reason or "已生成本地解释卡慢速滚动画面。",
                "路径": str(clip_path),
                "evidence_kind": provenance.evidence_kind,
                "source_url": provenance.source_url,
                "is_locally_generated": provenance.is_locally_generated,
            }
        )
        return MaterialAsset(
            "local-explanation",
            "解释卡",
            clip_path,
            ["AI 搜索", "解释", "来源线索"],
            "AI",
            "本地生成",
            title="解释卡慢速滚动",
            evidence_kind=provenance.evidence_kind,
            source_url=provenance.source_url,
            is_locally_generated=provenance.is_locally_generated,
        )

    def _download_or_scan_pexels_assets(
        self,
        warnings: list[str],
        source_results: list[dict[str, Any]],
    ) -> list[MaterialAsset]:
        service = PexelsDownloadService(paths=self.paths)
        source_urls: dict[str, str] = {}
        for keyword in ["technology office", "business meeting", "data analysis", "creator keyboard"]:
            result = service.download_one_video_and_one_photo(keyword, per_page=5)
            downloaded_items = [
                item
                for item in (result.video, result.photo)
                if item.status == "已下载" and item.save_path
            ]
            for downloaded in downloaded_items:
                if downloaded.source_url:
                    source_urls[str(Path(downloaded.save_path).resolve())] = (
                        downloaded.source_url
                    )
            available_source_url = next(
                (
                    item.source_url
                    for item in downloaded_items
                    if item.source_url
                ),
                "",
            ) or next(
                (
                    item.source_url
                    for item in (result.video, result.photo)
                    if item.source_url
                ),
                "",
            )
            failure_reasons = result.to_chinese_dict().get("失败原因", [])
            if failure_reasons:
                warnings.extend(failure_reasons)
            if not downloaded_items:
                source_results.append(
                    {
                        "素材源": "Pexels",
                        "状态": "失败",
                        "原因": "；".join(failure_reasons) or "未下载到素材。",
                        "路径": "",
                        "evidence_kind": "",
                        "source_url": available_source_url,
                        "is_locally_generated": False,
                    }
                )
            else:
                source_results.append(
                    {
                        "素材源": "Pexels",
                        "状态": "已下载" if result.success else "部分下载",
                        "原因": f"关键词 {keyword} 已尝试下载视频和照片。",
                        "路径": "; ".join(
                            item.save_path for item in downloaded_items
                        ),
                        "evidence_kind": "atmosphere",
                        "source_url": available_source_url,
                        "is_locally_generated": False,
                    }
                )
        assets: list[MaterialAsset] = []
        for directory, asset_type, category in [
            (self.paths.pexels_video_dir, "Pexels视频", "科技"),
            (self.paths.pexels_photo_dir, "Pexels图片", "科技"),
        ]:
            for item in sorted(directory.glob("*"), key=lambda path: path.stat().st_mtime, reverse=True)[:8]:
                if item.suffix.lower() not in VIDEO_EXTENSIONS | IMAGE_EXTENSIONS:
                    continue
                assets.append(
                    MaterialAsset(
                        asset_id=item.name,
                        asset_type=asset_type,
                        file_path=item,
                        keywords=_keywords_from_filename(item.name),
                        category=category,
                        source="Pexels",
                        title=item.stem,
                        evidence_kind="atmosphere",
                        source_url=source_urls.get(str(item.resolve()), ""),
                    )
                )
        if not assets:
            warnings.append("Pexels 没有可用素材。")
        return assets

    def _download_or_scan_pixabay_assets(
        self,
        warnings: list[str],
        source_results: list[dict[str, Any]],
    ) -> list[MaterialAsset]:
        service = PixabayDownloadService(paths=self.paths)
        assets: list[MaterialAsset] = []
        source_urls: dict[str, str] = {}
        for keyword in ["technology office", "business data"]:
            result = service.download_best(keyword, per_page=10)
            source_results.append(
                {
                    "素材源": "Pixabay",
                    "状态": result.status,
                    "原因": result.reason or f"关键词 {keyword} 已处理。",
                    "路径": result.save_path,
                    "evidence_kind": "atmosphere" if result.status == "已下载" else "",
                    "source_url": result.source_url,
                    "is_locally_generated": False,
                }
            )
            if result.status != "已下载":
                warnings.append(f"Pixabay {keyword}：{result.reason}")
            elif result.save_path and result.source_url:
                source_urls[str(Path(result.save_path).resolve())] = result.source_url
        for directory, asset_type, category in [
            (self.paths.pixabay_video_dir, "Pixabay视频", "科技"),
            (self.paths.pixabay_photo_dir, "Pixabay图片", "科技"),
        ]:
            for item in sorted(directory.glob("*"), key=lambda path: path.stat().st_mtime, reverse=True)[:8]:
                if item.suffix.lower() not in VIDEO_EXTENSIONS | IMAGE_EXTENSIONS:
                    continue
                assets.append(
                    MaterialAsset(
                        asset_id=item.name,
                        asset_type=asset_type,
                        file_path=item,
                        keywords=_keywords_from_filename(item.name),
                        category=category,
                        source="Pixabay",
                        title=item.stem,
                        evidence_kind="atmosphere",
                        source_url=source_urls.get(str(item.resolve()), ""),
                    )
                )
        return assets

    def _download_or_scan_coverr_assets(
        self,
        warnings: list[str],
        source_results: list[dict[str, Any]],
    ) -> list[MaterialAsset]:
        service = CoverrDownloadService(paths=self.paths)
        assets: list[MaterialAsset] = []
        source_urls: dict[str, str] = {}
        for keyword in ["technology office", "business meeting"]:
            result = service.download_best(keyword, per_page=10)
            source_results.append(
                {
                    "素材源": "Coverr",
                    "状态": result.status,
                    "原因": result.reason or f"关键词 {keyword} 已处理。",
                    "路径": result.save_path,
                    "evidence_kind": "atmosphere" if result.status == "已下载" else "",
                    "source_url": result.source_url,
                    "is_locally_generated": False,
                }
            )
            if result.status != "已下载":
                warnings.append(f"Coverr {keyword}：{result.reason}")
            elif result.save_path and result.source_url:
                source_urls[str(Path(result.save_path).resolve())] = result.source_url
        for item in sorted(self.paths.coverr_video_dir.glob("*"), key=lambda path: path.stat().st_mtime, reverse=True)[:8]:
            if item.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            assets.append(
                MaterialAsset(
                    asset_id=item.name,
                    asset_type="Coverr视频",
                    file_path=item,
                    keywords=_keywords_from_filename(item.name),
                    category="科技",
                    source="Coverr",
                    title=item.stem,
                    evidence_kind="atmosphere",
                    source_url=source_urls.get(str(item.resolve()), ""),
                )
            )
        return assets

    def _create_info_motion_assets(
        self,
        topic: str,
        run_dir: Path,
        warnings: list[str],
    ) -> list[MaterialAsset]:
        output_dir = self.paths.generated_motion_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        assets: list[MaterialAsset] = []
        for index, title in enumerate(["平台流量变化", "风险判断标准", "三点总结清单"], start=1):
            frame_dir = run_dir / f"motion_frames_{index}"
            frame_dir.mkdir(parents=True, exist_ok=True)
            _write_motion_frames(frame_dir, topic, title)
            clip_path = output_dir / f"{_safe_filename(topic)}_{datetime.now():%Y%m%d_%H%M%S}_{title}.mp4"
            try:
                _frames_to_video(frame_dir / "motion_%03d.png", clip_path, duration_seconds=8, fps=30)
                assets.append(
                    MaterialAsset(
                        asset_id=f"motion-{index}",
                        asset_type="信息动效",
                        file_path=clip_path,
                        keywords=[title, "平台流量", "判断标准", "总结"],
                        category="商业",
                        source="信息动效",
                        title=title,
                        evidence_kind="explanation",
                        is_locally_generated=True,
                    )
                )
            except Exception as exc:
                warnings.append(f"信息动效生成失败：{title}：{exc}")
        return assets

    def _scan_backup_assets(self) -> list[MaterialAsset]:
        backup_dir = self.paths.backup_dir
        if not backup_dir.exists():
            return []
        assets: list[MaterialAsset] = []
        for item in backup_dir.rglob("*"):
            if not item.is_file() or item.suffix.lower() not in VIDEO_EXTENSIONS | IMAGE_EXTENSIONS:
                continue
            assets.append(
                MaterialAsset(
                    item.name,
                    "备用素材",
                    item,
                    keywords=_keywords_from_filename(item.name),
                    category="备用",
                    source="备用",
                    title=item.stem,
                )
            )
        return assets[:10]

    def _render_final_video(
        self,
        run_dir: Path,
        matches: list[MaterialMatch],
        srt_path: Path,
        voiceover_path: Path,
        final_video_path: Path,
        warnings: list[str],
    ) -> None:
        tts_result = synthesize_tts(
            "。".join(match.segment.voiceover for match in matches),
            voiceover_path,
        )
        if not tts_result.success:
            warnings.append(f"口播音频生成失败，使用静音占位音频：{tts_result.reason}")
            _create_silent_audio(voiceover_path, duration_seconds=60)
        else:
            warnings.append(f"口播音频：{tts_result.reason}")
        clip_paths = []
        for index, match in enumerate(matches, start=1):
            segment_clip = run_dir / f"clip_{index:02d}.mp4"
            asset_path = match.asset.file_path if match.asset else None
            if asset_path is None or not asset_path.is_file():
                missing_dir = run_dir / f"missing_frames_{index}"
                missing_dir.mkdir(exist_ok=True)
                _write_motion_frames(missing_dir, "缺素材", match.segment.visual_description, prefix="missing")
                temp_asset = run_dir / f"缺素材_{index:02d}.mp4"
                _frames_to_video(missing_dir / "missing_%03d.png", temp_asset, duration_seconds=match.segment.duration_seconds)
                asset_path = temp_asset
            _make_segment_clip(asset_path, segment_clip, duration_seconds=match.segment.duration_seconds)
            clip_paths.append(segment_clip)
        concat_path = run_dir / "concat.txt"
        concat_path.write_text(
            "\n".join(_ffmpeg_concat_line(path) for path in clip_paths),
            encoding="utf-8",
        )
        no_audio_path = run_dir / "video_no_audio.mp4"
        _run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", str(concat_path), "-c", "copy", str(no_audio_path)])
        _run_ffmpeg(
            [
                "-y",
                "-i",
                _ffmpeg_path_arg(no_audio_path),
                "-i",
                _ffmpeg_path_arg(voiceover_path),
                "-vf",
                "subtitles=字幕.srt:force_style='FontName=Microsoft YaHei,FontSize=12,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Alignment=2,MarginV=120'",
                "-af",
                "apad",
                "-shortest",
                *HIGH_QUALITY_VIDEO_ARGS,
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                _ffmpeg_path_arg(final_video_path),
            ],
            cwd=run_dir,
        )
        usage_registry = MaterialUsageRegistry(self.paths.usage_registry_path)
        used_asset_paths: set[str] = set()
        for match in matches:
            if match.asset and match.asset.file_path.is_file():
                asset_key = str(match.asset.file_path)
                if asset_key in used_asset_paths:
                    continue
                usage_registry.record_use(asset_key, source_type=match.asset.asset_type)
                used_asset_paths.add(asset_key)
        usage_registry.record_use(
            str(final_video_path),
            source_type="成片导出",
        )


def _rank_candidates(
    segment: TestScriptSegment,
    assets: list[MaterialAsset],
    previous_path: str,
    *,
    usage_registry: MaterialUsageRegistry | None = None,
    now: datetime | None = None,
) -> list[tuple[int, str, MaterialAsset]]:
    ranked: list[tuple[int, str, MaterialAsset]] = []
    for asset in assets:
        if str(asset.file_path) == previous_path and len(assets) > 1:
            continue
        if usage_registry and usage_registry.is_recently_used(str(asset.file_path), now=now):
            continue
        score = 0
        reasons: list[str] = []
        segment_text = _normalize(
            " ".join(
                [
                    segment.preferred_source,
                    segment.visual_description,
                    segment.voiceover,
                    segment.subtitle,
                    segment.category,
                    " ".join(segment.keywords),
                ]
            )
        )
        asset_type = _normalize(asset.asset_type)
        asset_source = _normalize(asset.source)
        keyword_hits = set(_normalize(token) for token in segment.keywords) & set(
            _normalize(token) for token in asset.keywords
        )
        if keyword_hits:
            score += 100
            reasons.append(f"关键词匹配：{'、'.join(sorted(keyword_hits))}")
        if asset.category and _normalize(asset.category) == _normalize(segment.category):
            score += 40
            reasons.append(f"分类匹配：{asset.category}")
        preferred_source = _normalize(segment.preferred_source)
        if preferred_source and preferred_source == asset_type:
            score += 25
            reasons.append(f"来源匹配：{asset.asset_type}")
        elif preferred_source and (preferred_source in asset_type or asset_type in preferred_source):
            score += 20
            reasons.append(f"来源近似匹配：{asset.asset_type}")
        if asset.source and preferred_source == asset_source:
            score += 15
            reasons.append(f"来源匹配：{asset.source}")
        if asset.evidence_kind == "direct_evidence" and any(
            token in segment_text
            for token in ["官方", "截图", "录屏", "原文", "新闻", "证据", "页面"]
        ):
            score += 35
            reasons.append("语义匹配：需要来源页面录屏")
        if asset_type in {"pexels视频", "pexels图片", "pixabay视频", "pixabay图片", "coverr视频"} and any(
            token in segment_text
            for token in ["素材", "视频", "背景", "办公", "会议", "数据", "消费", "科技", "商业", "场景"]
        ):
            score += 28
            reasons.append(f"语义匹配：适合作为{asset.asset_type}")
        if asset_type == "信息动效" and any(
            token in segment_text
            for token in ["动效", "图文", "卡片", "清单", "总结", "判断", "逻辑", "流程", "解释"]
        ):
            score += 32
            reasons.append("语义匹配：适合信息动效")
        if score == 0 and asset_type == "信息动效":
            score += 5
            reasons.append("兜底匹配：常规素材不足，使用信息动效补位")
        score -= asset.usage_count * 10
        if usage_registry:
            score = int(score * usage_registry.score_multiplier(str(asset.file_path), now=now))
        if score > 0:
            ranked.append((score, "；".join(reasons), asset))
    return sorted(ranked, key=lambda item: item[0], reverse=True)


def _frames_to_video(pattern: Path, output_path: Path, *, duration_seconds: int = 8, fps: int = 30) -> None:
    frame_count = len(list(pattern.parent.glob(f"{pattern.stem.split('%')[0]}*.png")))
    input_fps = max(1, math.ceil(frame_count / max(1, duration_seconds)))
    _run_ffmpeg(
        [
            "-y",
            "-framerate",
            str(input_fps),
            "-i",
            str(pattern),
            "-t",
            str(duration_seconds),
            "-vf",
            VERTICAL_EXPORT_FILTER,
            "-an",
            *HIGH_QUALITY_VIDEO_ARGS,
            str(output_path),
        ]
    )


def _make_segment_clip(asset_path: Path, output_path: Path, *, duration_seconds: int) -> None:
    suffix = asset_path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        args = ["-y", "-loop", "1", "-t", str(duration_seconds), "-i", str(asset_path)]
    else:
        args = ["-y", "-stream_loop", "-1", "-t", str(duration_seconds), "-i", str(asset_path)]
    args.extend(
        [
            "-vf",
            VERTICAL_EXPORT_FILTER,
            "-an",
            *HIGH_QUALITY_VIDEO_ARGS,
            str(output_path),
        ]
    )
    _run_ffmpeg(args)


def _generate_voiceover(text: str, output_path: Path, *, duration_seconds: int, warnings: list[str]) -> None:
    text_path = output_path.with_suffix(".txt")
    text_path.write_text(text, encoding="utf-8")
    ps1_path = output_path.with_suffix(".ps1")
    ps1_path.write_text(
        "\n".join(
            [
                "Add-Type -AssemblyName System.Speech",
                "$text = Get-Content -Raw -Encoding UTF8 $args[0]",
                "$out = $args[1]",
                "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer",
                "$synth.Rate = 0",
                "$synth.Volume = 100",
                "$synth.SetOutputToWaveFile($out)",
                "$synth.Speak($text)",
                "$synth.Dispose()",
            ]
        ),
        encoding="utf-8",
    )
    try:
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ps1_path),
                str(text_path),
                str(output_path),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except Exception as exc:
        warnings.append(f"Windows 口播合成失败，使用静音占位音频：{exc}")
        _run_ffmpeg(
            [
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-t",
                str(duration_seconds),
                "-c:a",
                "pcm_s16le",
                str(output_path),
            ]
        )


def _create_silent_audio(output_path: Path, *, duration_seconds: int) -> None:
    _run_ffmpeg(
        [
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            str(duration_seconds),
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    )


def _subtitle_segments_from_test_segments(
    segments: list[TestScriptSegment],
) -> list[SubtitleSegment]:
    return [
        SubtitleSegment(
            index=index,
            start_seconds=segment.start_seconds,
            end_seconds=segment.end_seconds,
            text=segment.subtitle,
        )
        for index, segment in enumerate(segments, start=1)
    ]


def _pipeline_material_tasks(pipeline: ProductionPipeline) -> list[dict[str, Any]]:
    for step in pipeline.steps:
        if step.name != "生成素材任务" or not isinstance(step.output_summary, dict):
            continue
        return [
            dict(item)
            for item in step.output_summary.get("material_tasks", [])
            if isinstance(item, dict)
        ]
    return []


def _segments_from_pipeline_script(
    script: ScriptAsset,
    *,
    title: str,
    material_tasks: list[dict[str, Any]],
) -> list[TestScriptSegment]:
    tasks_by_shot = {
        str(item.get("shot_id") or ""): item
        for item in material_tasks
        if isinstance(item, dict)
    }
    segments: list[TestScriptSegment] = []
    for index, shot in enumerate(script.storyboard or [], start=1):
        if not isinstance(shot, dict):
            continue
        shot_id = str(shot.get("shot_id") or shot.get("id") or f"shot-{index:02d}")
        task = tasks_by_shot.get(shot_id, {})
        start = _parse_seconds(shot.get("start") or task.get("start"), default=(index - 1) * 8)
        end = _parse_seconds(shot.get("end") or task.get("end"), default=start + 8)
        if end <= start:
            end = start + 8
        voiceover = str(shot.get("voiceover") or task.get("voiceover") or "").strip()
        if not voiceover:
            voiceover = _voiceover_line(script.voiceover, index) or title
        subtitle = _subtitle_for_segment(script.subtitles or [], index, voiceover)
        keywords = _material_keywords(task, shot, title)
        segments.append(
            TestScriptSegment(
                shot_id=shot_id,
                start_seconds=start,
                end_seconds=end,
                voiceover=voiceover,
                subtitle=subtitle,
                visual_description=str(
                    shot.get("recommended_visual")
                    or task.get("recommended_visual")
                    or "根据口播生成信息画面"
                ),
                keywords=keywords,
                category=str(task.get("category") or task.get("material_type") or "科技"),
                preferred_source=str(task.get("source_type") or task.get("material_type") or ""),
            )
        )
    if segments:
        return _normalize_segment_timing(segments)
    return _segments_from_voiceover(script.voiceover, title=title)


def _assets_from_material_tasks(tasks: list[dict[str, Any]]) -> list[MaterialAsset]:
    assets: list[MaterialAsset] = []
    for task in tasks:
        path = Path(str(task.get("material_path") or "").strip())
        if not path.is_file():
            continue
        material_type = str(task.get("material_type") or task.get("source_type") or "项目素材")
        source_type = str(task.get("source_type") or material_type)
        if task.get("evidence_kind"):
            provenance = {
                "evidence_kind": str(task["evidence_kind"]),
                "source_url": str(task.get("source_url") or ""),
                "is_locally_generated": bool(
                    task.get("is_locally_generated", False)
                ),
            }
        else:
            legacy = classify_legacy_material_provenance(
                material_type,
                source_type,
            )
            provenance = {
                "evidence_kind": legacy.evidence_kind,
                "source_url": legacy.source_url,
                "is_locally_generated": legacy.is_locally_generated,
            }
        assets.append(
            MaterialAsset(
                asset_id=path.name,
                asset_type=material_type,
                file_path=path,
                keywords=[
                    str(item)
                    for item in (
                        task.get("asset_keywords")
                        or task.get("jianying_keywords")
                        or _keywords_from_filename(path.name)
                    )
                ],
                category=str(task.get("category") or material_type),
                source=source_type,
                title=path.stem,
                **provenance,
            )
        )
    return assets


def _parse_seconds(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip()
    if not text:
        return default
    if "-" in text:
        text = text.split("-", 1)[0].strip()
    parts = text.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(float(parts[1]))
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
        return int(float(text))
    except ValueError:
        return default


def _voiceover_line(voiceover: str, index: int) -> str:
    lines = [line.strip() for line in voiceover.splitlines() if line.strip()]
    if 0 <= index - 1 < len(lines):
        return lines[index - 1]
    return ""


def _subtitle_for_segment(subtitles: list[dict[str, Any]], index: int, fallback: str) -> str:
    if 0 <= index - 1 < len(subtitles):
        item = subtitles[index - 1]
        if isinstance(item, dict):
            return str(item.get("text") or item.get("subtitle") or fallback).strip()
    return fallback


def _material_keywords(task: dict[str, Any], shot: dict[str, Any], title: str) -> list[str]:
    values = task.get("asset_keywords") or task.get("jianying_keywords") or []
    keywords = [str(item).strip() for item in values if str(item).strip()]
    if keywords:
        return keywords[:8]
    text = " ".join(
        str(part)
        for part in [
            title,
            shot.get("recommended_visual", ""),
            shot.get("voiceover", ""),
        ]
    )
    mapping = [
        ("AI", "artificial intelligence"),
        ("科技", "technology office"),
        ("商业", "business meeting"),
        ("数据", "data analysis"),
        ("消费", "consumer lifestyle"),
        ("平台", "platform"),
        ("工具", "software productivity"),
    ]
    return [query for token, query in mapping if token in text][:5] or ["technology office"]


def _normalize_segment_timing(segments: list[TestScriptSegment]) -> list[TestScriptSegment]:
    normalized: list[TestScriptSegment] = []
    cursor = 0
    for segment in segments:
        duration = max(4, segment.end_seconds - segment.start_seconds)
        normalized.append(
            TestScriptSegment(
                shot_id=segment.shot_id,
                start_seconds=cursor,
                end_seconds=cursor + duration,
                voiceover=segment.voiceover,
                subtitle=segment.subtitle,
                visual_description=segment.visual_description,
                keywords=segment.keywords,
                category=segment.category,
                preferred_source=segment.preferred_source,
            )
        )
        cursor += duration
    return normalized


def _segments_from_voiceover(voiceover: str, *, title: str) -> list[TestScriptSegment]:
    raw_parts = [part.strip() for part in voiceover.replace("\n", "。").split("。") if part.strip()]
    parts = raw_parts[:8] or [title]
    segments: list[TestScriptSegment] = []
    cursor = 0
    for index, part in enumerate(parts, start=1):
        duration = max(5, min(10, len(part) // 8 + 5))
        segments.append(
            TestScriptSegment(
                shot_id=f"shot-{index:02d}",
                start_seconds=cursor,
                end_seconds=cursor + duration,
                voiceover=part,
                subtitle=part,
                visual_description="根据口播生成信息动效或匹配素材",
                keywords=_material_keywords({}, {"voiceover": part}, title),
                category="科技",
                preferred_source="信息动效",
            )
        )
        cursor += duration
    return segments


def _run_ffmpeg(args: list[str], *, cwd: Path | None = None) -> None:
    command = [imageio_ffmpeg.get_ffmpeg_exe(), *args]
    result = subprocess.run(command, cwd=cwd, capture_output=True)
    if result.returncode != 0:
        output = _decode_process_text(result.stderr or result.stdout)
        raise RuntimeError((output or "ffmpeg 失败").strip()[-1200:])


def _as_file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def _ffmpeg_concat_line(path: Path) -> str:
    return f"file '{path.resolve().as_posix()}'"


def _ffmpeg_path_arg(path: Path) -> str:
    return str(path.resolve())


def _decode_process_text(data: bytes | None) -> str:
    if not data:
        return ""
    try:
        return data.decode("utf-8", errors="replace")
    except LookupError:
        return data.decode(errors="replace")


def _write_motion_frames(frame_dir: Path, topic: str, title: str, *, prefix: str = "motion") -> None:
    font_large = _font(54)
    font_mid = _font(34)
    for index in range(12):
        image = Image.new("RGB", (1080, 1920), (12, 22, 34))
        draw = ImageDraw.Draw(image)
        offset = index * 16
        for line in range(0, 1920, 120):
            color = (25, 95 + (line + offset) % 80, 135)
            draw.line((0, line + offset % 120, 1080, line + offset % 120), fill=color, width=2)
        card_y = 420 + int(math.sin(index / 3) * 22)
        draw.rounded_rectangle((90, card_y, 990, card_y + 520), radius=36, fill=(21, 42, 59), outline=(77, 205, 190), width=4)
        draw.text((140, card_y + 70), title[:18], font=font_large, fill=(255, 255, 255))
        draw.text((140, card_y + 165), _wrap(topic, 16), font=font_mid, fill=(210, 240, 238), spacing=10)
        draw.text((140, card_y + 380), "大白话判断框架 / 信息动效", font=font_mid, fill=(128, 230, 210))
        draw.rounded_rectangle((170, 1180, 910, 1260), radius=26, fill=(255, 255, 255), outline=(255, 255, 255))
        draw.text((220, 1196), "不是静态 PPT，保留动态扫描线和信息框", font=_font(28), fill=(20, 36, 46))
        image.save(frame_dir / f"{prefix}_{index:03d}.png")


def _write_fallback_frames(frame_dir: Path, topic: str, title: str) -> None:
    for index in range(6):
        _write_single_page_frame(frame_dir / f"explanation_{index:03d}.png", topic, title, scroll=index)


def _write_single_page_frame(path: Path, topic: str, title: str, *, scroll: int) -> None:
    image = Image.new("RGB", (1080, 1920), (245, 248, 250))
    draw = ImageDraw.Draw(image)
    y = 180 - scroll * 120
    draw.text((90, max(60, y)), title, font=_font(58), fill=(20, 36, 46))
    draw.text((90, max(150, y + 120)), _wrap(topic, 16), font=_font(40), fill=(20, 36, 46), spacing=12)
    for index in range(6):
        block_y = y + 320 + index * 230
        draw.rounded_rectangle((80, block_y, 1000, block_y + 150), radius=20, fill=(255, 255, 255), outline=(215, 225, 230))
        draw.text((120, block_y + 36), f"解释段落 {index + 1}：来源线索、时间、标题和核心变化。", font=_font(28), fill=(50, 70, 84))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _explanation_html(topic: str) -> str:
    paragraphs = "\n".join(
        f"<p>解释段落 {index}：围绕 {topic}，展示来源线索、时间、标题、链接和核心变化，边栏广告会被隐藏。</p>"
        for index in range(1, 12)
    )
    return f"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>{topic}</title>
<style>
body {{ margin:0; font-family:'Microsoft YaHei', sans-serif; background:#eef4f5; color:#10242e; }}
main {{ width:860px; margin:80px auto; background:white; padding:56px; border-radius:24px; line-height:1.8; box-shadow:0 18px 60px rgba(0,0,0,.12); }}
h1 {{ font-size:52px; line-height:1.2; }}
p {{ font-size:30px; border-bottom:1px solid #d7e3e6; padding:26px 0; }}
aside,.ad,.ads,.banner,.sidebar {{ display:none; }}
</style>
</head>
<body><aside>广告内容，不应被采集</aside><main><h1>{topic}</h1>{paragraphs}</main></body>
</html>
"""


def _build_srt(segments: list[TestScriptSegment]) -> str:
    rows: list[str] = []
    for index, segment in enumerate(segments, start=1):
        rows.extend(
            [
                str(index),
                f"{_srt_time(segment.start_seconds)} --> {_srt_time(segment.end_seconds)}",
                segment.subtitle,
                "",
            ]
        )
    return "\n".join(rows)


def _script_markdown(topic: str, segments: list[TestScriptSegment], matches: list[MaterialMatch]) -> str:
    lines = [f"# {topic}", "", "## 口播与画面", ""]
    for match in matches:
        lines.append(f"### {match.segment.shot_id} {match.segment.start_seconds}-{match.segment.end_seconds}s")
        lines.append(f"- 口播：{match.segment.voiceover}")
        lines.append(f"- 字幕：{match.segment.subtitle}")
        lines.append(f"- 画面：{match.segment.visual_description}")
        lines.append(f"- 素材：{match.asset.file_path if match.asset else '缺素材'}")
        lines.append(f"- 匹配原因：{match.match_reason}")
        lines.append("")
    return "\n".join(lines)


def _keywords_from_filename(name: str) -> list[str]:
    tokens = [token for token in _safe_filename(Path(name).stem).replace("_", " ").split() if token]
    return tokens[:8]


def _normalize(value: str) -> str:
    return value.lower().replace(" ", "").replace("_", "")


def _safe_filename(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_ " else "_" for char in value)
    return "_".join(cleaned.split())[:80] or "未命名"


def _wrap(text: str, width: int) -> str:
    return "\n".join(text[index : index + width] for index in range(0, len(text), width))


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ]:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _time(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _srt_time(seconds: int) -> str:
    return f"00:{seconds // 60:02d}:{seconds % 60:02d},000"
