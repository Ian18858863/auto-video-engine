from __future__ import annotations

import argparse
import sys
from pathlib import Path
from tkinter import BooleanVar, Button, Checkbutton, IntVar, Label, Radiobutton, Tk

from PIL import Image, ImageTk

from app.materials.adapters.desktop import DesktopAutomationError
from app.materials.adapters.factory import (
    adapter_config_from_settings,
    build_jianying_adapter,
)
from app.materials.controlled_download import (
    CandidateReview,
    ControlledDownloadRunner,
)
from app.materials.paths import MaterialPathError, ProjectMaterialPaths
from app.materials.scoring import CandidateMaterial


class ManualCandidateReviewer:
    def review(self, candidate: CandidateMaterial) -> CandidateReview:
        root = Tk()
        root.title("剪映候选素材人工确认")
        root.attributes("-topmost", True)

        image_path = Path(candidate.local_path)
        if image_path.exists():
            original = Image.open(image_path)
            scale = min(900 / original.width, 560 / original.height, 1.0)
            display = original.resize(
                (int(original.width * scale), int(original.height * scale)),
                Image.Resampling.LANCZOS,
            )
            photo = ImageTk.PhotoImage(display)
            image_label = Label(root, image=photo)
            image_label.image = photo
            image_label.pack(padx=12, pady=12)

        Label(
            root,
            text="请只根据截图确认。看不清授权、画质或风险时，选择“无法判断”。",
            font=("Microsoft YaHei UI", 11),
        ).pack(padx=12, pady=6)
        license_value = IntVar(value=0)
        Radiobutton(root, text="无法判断，不下载", variable=license_value, value=0).pack()
        Radiobutton(root, text="免费素材", variable=license_value, value=1).pack()
        Radiobutton(root, text="会员可用素材", variable=license_value, value=2).pack()
        quality_ok = BooleanVar(value=False)
        risk_found = BooleanVar(value=False)
        orientation_value = IntVar(value=0)
        Radiobutton(
            root,
            text="比例无法判断，不下载",
            variable=orientation_value,
            value=0,
        ).pack()
        Radiobutton(
            root,
            text="竖屏素材，优先",
            variable=orientation_value,
            value=1,
        ).pack()
        Radiobutton(
            root,
            text="横屏素材，需要检查能否裁成 9:16",
            variable=orientation_value,
            value=2,
        ).pack()
        Checkbutton(root, text="画质清楚、主题匹配", variable=quality_ok).pack()
        Checkbutton(root, text="发现低俗、风险或异常", variable=risk_found).pack()
        subject_centered = BooleanVar(value=False)
        key_info_safe = BooleanVar(value=False)
        background_suitable = BooleanVar(value=False)
        overlay_safe = BooleanVar(value=False)
        Checkbutton(root, text="主体居中", variable=subject_centered).pack()
        Checkbutton(root, text="裁切后不丢失关键信息", variable=key_info_safe).pack()
        Checkbutton(root, text="适合作为背景或氛围素材", variable=background_suitable).pack()
        Checkbutton(root, text="不影响字幕和右下角数字人", variable=overlay_safe).pack()

        confirmed = {"value": False}

        def finish() -> None:
            confirmed["value"] = True
            root.destroy()

        Button(root, text="确认本候选", command=finish).pack(pady=12)
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        root.mainloop()

        if not confirmed["value"]:
            return CandidateReview("unknown", False, False)
        license_type = {1: "free", 2: "member"}.get(license_value.get(), "unknown")
        orientation = {1: "portrait", 2: "landscape"}.get(
            orientation_value.get(),
            "unknown",
        )
        if license_type == "unknown" or orientation == "unknown":
            return CandidateReview(
                license_type="unknown",
                quality_ok=False,
                risk_found=risk_found.get(),
                orientation="unknown",
            )
        return CandidateReview(
            license_type=license_type,
            quality_ok=quality_ok.get(),
            risk_found=risk_found.get(),
            orientation=orientation,
            subject_centered=subject_centered.get(),
            key_info_safe=key_info_safe.get(),
            background_suitable=background_suitable.get(),
            overlay_safe=overlay_safe.get(),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="剪映专业版受控真实下载测试")
    parser.add_argument("--keyword", default="财经新闻", help="只搜索一个关键词")
    parser.add_argument(
        "--max-downloads",
        type=int,
        default=1,
        choices=[1],
        help="安全限制：固定为 1",
    )
    parser.add_argument(
        "--confirm-download",
        action="store_true",
        help="明确允许下载一个通过人工确认和评分的素材；不加此参数只预演",
    )
    parser.add_argument("--project-name", default="剪映受控下载测试")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = adapter_config_from_settings(require_profile=True)
        config.max_candidates_per_search = 3
        config.max_downloads_per_task = 1
        adapter = build_jianying_adapter(config, project_name=args.project_name)
        runner = ControlledDownloadRunner(
            adapter=adapter,
            paths=ProjectMaterialPaths(config.material_root_dir, args.project_name),
            reviewer=ManualCandidateReviewer(),
            min_material_score=config.min_material_score,
        )
        result = runner.run(
            keyword=args.keyword,
            max_candidates=3,
            max_downloads=args.max_downloads,
            confirm_download=args.confirm_download,
        )
    except (DesktopAutomationError, MaterialPathError, ValueError) as exc:
        print(f"受控测试已停止：{exc}", file=sys.stderr)
        return 1

    print(f"测试状态：{result.status}")
    print(f"记录文件：{result.report_path}")
    if result.downloaded_path:
        print(f"下载文件：{result.downloaded_path}")
    if result.failure_reason:
        print(f"说明：{result.failure_reason}")
    return 0 if result.status in {"预演完成", "已下载", "需要人工确认"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
