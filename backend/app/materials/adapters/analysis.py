from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from app.materials.scoring import CandidateMaterial


class CandidateScreenshotAnalyzer(Protocol):
    def analyze(
        self,
        screenshot_path: Path,
        keyword: str,
        candidate_index: int,
    ) -> CandidateMaterial:
        raise NotImplementedError


class ConservativeScreenshotAnalyzer:
    """No AI call: unknown license means the candidate cannot auto-download."""

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path

    def analyze(
        self,
        screenshot_path: Path,
        keyword: str,
        candidate_index: int,
    ) -> CandidateMaterial:
        cache = self._load_cache()
        cache_key = _file_hash(screenshot_path)
        cached = cache.get(cache_key)
        if cached is not None:
            return CandidateMaterial(**cached)

        candidate = CandidateMaterial(
            material_id=f"{keyword}-{candidate_index}-{cache_key[:8]}",
            title=f"{keyword} 候选素材 {candidate_index}",
            description="候选截图已保存，尚未识别免费/会员标记，需要人工确认或视觉识别。",
            keywords=[keyword],
            source="剪映素材",
            license_type="unknown",
            quality_flags=[],
            local_path=str(screenshot_path),
        )
        cache[cache_key] = asdict(candidate)
        self._save_cache(cache)
        return candidate

    def _load_cache(self) -> dict:
        if not self.cache_path.exists():
            return {}
        return json.loads(self.cache_path.read_text(encoding="utf-8"))

    def _save_cache(self, cache: dict) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
