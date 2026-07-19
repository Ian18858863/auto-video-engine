from __future__ import annotations

from pathlib import Path

from app.materials.assistant import DownloadResult
from app.materials.scoring import CandidateMaterial


class BrowserJianyingAdapter:
    def search(self, keywords: list[str]) -> list[CandidateMaterial]:
        raise RuntimeError("browser adapter 仅保留骨架，本轮默认使用 desktop adapter。")

    def download(
        self,
        candidate: CandidateMaterial,
        download_dir: Path,
    ) -> DownloadResult:
        return DownloadResult(
            success=False,
            error_message="browser adapter 仅保留骨架，未执行下载。",
        )
