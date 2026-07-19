from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.materials.asset_paths import AutoMaterialPaths
from app.materials.pexels import PexelsDownloadService


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 1 条 Pexels 视频和 1 张图片。")
    parser.add_argument("--keyword", default="technology office", help="Pexels 英文搜索词")
    parser.add_argument("--root", default="D:/素材", help="素材根目录，默认 D:/素材")
    parser.add_argument("--per-page", type=int, default=10, help="单次候选数量，最多 10")
    args = parser.parse_args()

    service = PexelsDownloadService(paths=AutoMaterialPaths(root=Path(args.root)))
    result = service.download_one_video_and_one_photo(
        args.keyword,
        per_page=args.per_page,
    )
    print(json.dumps(result.to_chinese_dict(), ensure_ascii=False, indent=2))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
