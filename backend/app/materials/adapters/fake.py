from __future__ import annotations

from pathlib import Path

from app.materials.assistant import DownloadResult
from app.materials.scoring import CandidateMaterial


class FakeJianyingAdapter:
    def __init__(self):
        self.download_calls = 0

    def search(self, keywords: list[str]) -> list[CandidateMaterial]:
        keyword = keywords[0] if keywords else "素材"
        return [
            CandidateMaterial(
                material_id="fake-good",
                title=f"{keyword} 科技办公素材",
                description="本地测试候选，不打开剪映。",
                keywords=[keyword, "科技", "办公", "商业"],
                source="剪映素材",
                license_type="member",
                quality_flags=["clean", "sharp", "stable"],
                orientation="portrait",
            )
        ]

    def download(
        self,
        candidate: CandidateMaterial,
        download_dir: Path,
    ) -> DownloadResult:
        self.download_calls += 1
        download_dir.mkdir(parents=True, exist_ok=True)
        path = download_dir / f"{candidate.material_id}.mp4"
        path.write_bytes(b"fake-video")
        return DownloadResult(success=True, downloaded_path=path)
