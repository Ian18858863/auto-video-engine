from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.materials.planner import MaterialTask
from app.materials.scoring import CandidateMaterial, score_candidate


@dataclass(slots=True)
class LocalMaterial:
    name: str
    path: str
    source: str
    keywords: list[str]
    category: str = "其他"
    usage_count: int = 0
    match_score: float = 0.0
    recommended_reuse: bool = True
    rejected: bool = False
    reject_reason: str = ""
    last_used_project: str = ""
    last_used_at: str = ""


class LocalMaterialLibrary:
    def __init__(self, library_dir: Path):
        self.library_dir = library_dir
        self.metadata_path = library_dir / "metadata.json"

    def load(self) -> list[LocalMaterial]:
        if not self.metadata_path.exists():
            return []
        raw_items = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        return [LocalMaterial(**item) for item in raw_items]

    def save(self, materials: list[LocalMaterial]) -> None:
        self.library_dir.mkdir(parents=True, exist_ok=True)
        payload = [asdict(material) for material in materials]
        self.metadata_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def find_best_match(self, task: MaterialTask) -> tuple[LocalMaterial, float] | None:
        best: tuple[LocalMaterial, float] | None = None
        for material in self.load():
            if material.rejected or not material.recommended_reuse:
                continue
            candidate = CandidateMaterial(
                material_id=material.name,
                title=material.name,
                description=material.category,
                keywords=material.keywords,
                source=material.source,
                license_type="free",
                quality_flags=["clean", "sharp", "stable"],
                local_path=material.path,
            )
            score = score_candidate(task, candidate, existing_keywords=[])
            if best is None or score.total_score > best[1]:
                best = (material, score.total_score)
        return best

    def record_use(self, material: LocalMaterial, project_name: str, used_at: str) -> None:
        materials = self.load()
        for item in materials:
            if item.path == material.path:
                item.usage_count += 1
                item.last_used_project = project_name
                item.last_used_at = used_at
                break
        self.save(materials)
