from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.materials.asset_paths import AutoMaterialPaths
from app.materials.end_to_end_video import EndToEndVideoService
from app.materials.official_capture import OfficialPageCaptureService, build_official_capture_plan
from app.materials.pexels import PexelsDownloadService
from app.materials.source_clients import CoverrDownloadService, PixabayDownloadService


TOPIC = "AI 搜索正在改变普通人获取信息的方式"
KEYWORD = "technology office"


def _build_smoke_report_row(
    source: str,
    status: str,
    reason: str,
    path: str = "",
    evidence_kind: str = "",
    source_url: str = "",
    is_locally_generated: bool = False,
    *,
    include_provenance: bool = True,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "素材源": source,
        "状态": status,
        "原因": reason,
        "路径": path,
    }
    if include_provenance:
        row.update(
            {
                "evidence_kind": evidence_kind,
                "source_url": source_url,
                "is_locally_generated": is_locally_generated,
            }
        )
    return row


def main() -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = Path("D:/短视频工厂/归档") / f"素材链路测试_{timestamp}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    paths = AutoMaterialPaths(root=Path("D:/素材"))
    report: dict[str, Any] = {
        "测试主题": TOPIC,
        "测试时间": datetime.now().isoformat(timespec="seconds"),
        "归档目录": str(archive_dir),
        "素材源结果": [],
        "成片结果": {},
    }

    def record(
        source: str,
        status: str,
        reason: str,
        path: str = "",
        evidence_kind: str = "",
        source_url: str = "",
        is_locally_generated: bool = False,
        *,
        include_provenance: bool = True,
    ) -> None:
        row = _build_smoke_report_row(
            source,
            status,
            reason,
            path,
            evidence_kind,
            source_url,
            is_locally_generated,
            include_provenance=include_provenance,
        )
        report["素材源结果"].append(row)
        print(f"[{status}] {source}：{reason}" + (f" | {path}" if path else ""))

    try:
        paths.ensure_directories()
        record(
            "目录检查",
            "成功",
            "D:/素材 目录和素材子目录可用。",
            str(paths.root),
            include_provenance=False,
        )
    except Exception as exc:
        record("目录检查", "失败", str(exc), include_provenance=False)

    _run_official_capture_smoke(paths, archive_dir, record)
    _run_pexels_smoke(paths, record)
    _run_pixabay_smoke(paths, record)
    _run_coverr_smoke(paths, record)
    _run_backup_scan(paths, record)

    try:
        service = EndToEndVideoService(
            output_root=archive_dir / "成片",
            material_root=paths.root,
            enable_real_downloads=True,
            enable_real_render=True,
        )
        result = service.run_test_export(topic=TOPIC)
        report["成片结果"] = result
        status = "成功" if result.get("成功") else "失败"
        final_path = str((result.get("导出文件") or {}).get("最终成片 MP4") or "")
        record(
            "自动成片",
            status,
            "已运行 8 画面测试脚本，包含口播音频和烧录字幕。",
            final_path,
            include_provenance=False,
        )
    except Exception as exc:
        record("自动成片", "失败", str(exc), include_provenance=False)

    # Simulate multi-version generation without calling any real AI.
    versions: list[dict[str, str]] = []
    for index in range(1, 3):
        try:
            service = EndToEndVideoService(
                output_root=archive_dir / "多版本模拟",
                material_root=paths.root,
                enable_real_downloads=False,
                enable_real_render=False,
            )
            result = service.run_test_export(topic=f"{TOPIC} 版本{index}")
            versions.append(
                {
                    "版本": f"版本{index}",
                    "状态": "成功",
                    "报告路径": str((result.get("导出文件") or {}).get("报告 JSON") or ""),
                }
            )
        except Exception as exc:
            versions.append({"版本": f"版本{index}", "状态": "失败", "原因": str(exc)})
    report["多版本模拟"] = versions

    report_path = archive_dir / "素材链路报告.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = archive_dir / "素材链路报告.md"
    md_path.write_text(_markdown_report(report), encoding="utf-8")
    print(f"报告 JSON：{report_path}")
    print(f"报告 MD：{md_path}")


def _run_official_capture_smoke(
    paths: AutoMaterialPaths,
    archive_dir: Path,
    record,
) -> None:
    html_path = archive_dir / "本地解释卡测试.html"
    html_path.write_text(
        """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8" />
<style>
body{margin:0;background:#eef4f5;font-family:'Microsoft YaHei',sans-serif}
aside,.ad,.ads,.banner,.sidebar{display:none}
main{width:860px;margin:80px auto;background:white;padding:56px;border-radius:24px;line-height:1.8}
h1{font-size:52px}p{font-size:30px;border-bottom:1px solid #d7e3e6;padding:26px 0}
</style></head><body><aside>广告，不应进入画面</aside><main>
<h1>AI 搜索正在改变普通人获取信息的方式</h1>
<p>这是本地解释卡测试，用于验证慢速滚动录屏。</p>
<p>画面应尽量保留正文，不抓边栏广告。</p>
<p>如果失败，需要记录原因并切换到其他素材方式。</p>
<p>这是继续滚动的正文段落。</p><p>这是继续滚动的正文段落。</p>
</main></body></html>
""",
        encoding="utf-8",
    )
    try:
        plan = build_official_capture_plan(
            url=html_path.resolve().as_uri(),
            source_name="本地解释卡Smoke",
            page_title=TOPIC,
            paths=paths,
        )
        plan.output_dir = archive_dir / "解释卡"
        plan.filename_prefix = "本地解释卡Smoke"
        plan.scroll_recording_seconds = 5
        result = OfficialPageCaptureService(paths=paths, fallback_to_placeholder=True).capture(plan)
        status = "成功" if result.success else "失败"
        record(
            "本地解释卡",
            status,
            result.reason,
            result.scroll_recording_path,
            "explanation" if result.success else "",
            "",
            bool(result.success),
        )
    except Exception as exc:
        record("本地解释卡", "失败", str(exc))


def _run_pexels_smoke(paths: AutoMaterialPaths, record) -> None:
    try:
        result = PexelsDownloadService(paths=paths).download_one_video_and_one_photo(KEYWORD, per_page=5)
        data = result.to_chinese_dict()
        video = data["视频"]
        photo = data["图片"]
        record(
            "Pexels视频",
            video["状态"],
            video["失败原因"] or "已处理视频素材。",
            video["保存路径"],
            "atmosphere" if video["状态"] == "已下载" else "",
            video["来源链接"],
            False,
        )
        record(
            "Pexels图片",
            photo["状态"],
            photo["失败原因"] or "已处理图片素材。",
            photo["保存路径"],
            "atmosphere" if photo["状态"] == "已下载" else "",
            photo["来源链接"],
            False,
        )
    except Exception as exc:
        record("Pexels", "失败", str(exc))


def _run_pixabay_smoke(paths: AutoMaterialPaths, record) -> None:
    try:
        result = PixabayDownloadService(paths=paths).download_best(KEYWORD, per_page=5)
        record(
            "Pixabay",
            result.status,
            result.reason or "已处理 Pixabay 素材。",
            result.save_path,
            "atmosphere" if result.status == "已下载" else "",
            result.source_url,
            False,
        )
    except Exception as exc:
        record("Pixabay", "失败", str(exc))


def _run_coverr_smoke(paths: AutoMaterialPaths, record) -> None:
    try:
        result = CoverrDownloadService(paths=paths).download_best(KEYWORD, per_page=5)
        record(
            "Coverr",
            result.status,
            result.reason or "已处理 Coverr 素材。",
            result.save_path,
            "atmosphere" if result.status == "已下载" else "",
            result.source_url,
            False,
        )
    except Exception as exc:
        record("Coverr", "失败", str(exc))


def _run_backup_scan(paths: AutoMaterialPaths, record) -> None:
    try:
        files = [
            path
            for path in paths.backup_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp"}
        ] if paths.backup_dir.exists() else []
        if files:
            record(
                "备选素材",
                "成功",
                f"发现 {len(files)} 个可用备选素材，本轮最多使用 1 个。",
                str(paths.backup_dir),
                "atmosphere",
                "",
                False,
            )
        else:
            record("备选素材", "跳过", "备选素材目录没有可用文件。", str(paths.backup_dir))
    except Exception as exc:
        record("备选素材", "失败", str(exc))


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# 素材链路测试报告",
        "",
        f"- 测试主题：{report.get('测试主题')}",
        f"- 测试时间：{report.get('测试时间')}",
        f"- 归档目录：{report.get('归档目录')}",
        "",
        "## 素材源结果",
    ]
    for row in report.get("素材源结果", []):
        lines.append(
            f"- {row.get('素材源')}：{row.get('状态')}。原因：{row.get('原因')}。路径：{row.get('路径') or '无'}"
        )
    lines.extend(["", "## 多版本模拟"])
    for row in report.get("多版本模拟", []):
        lines.append(f"- {row.get('版本')}：{row.get('状态')} {row.get('报告路径') or row.get('原因') or ''}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
