from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class HotspotCandidate:
    source_platform: str
    source_type: str
    title: str
    url: str
    published_at: datetime | None = None
    summary: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    category: str = "其他"
    data_origin: str = "real"
    origin_note: str = ""


@dataclass(slots=True)
class FilterResult:
    is_filtered: bool
    reason: str = ""


@dataclass(slots=True)
class ScoreResult:
    hot_score: float
    topic_score: float
    save_score: float
    risk_score: float
    freshness_score: float


@dataclass(slots=True)
class PipelineRunResult:
    fetched_count: int = 0
    saved_count: int = 0
    duplicate_count: int = 0
    filtered_count: int = 0
    failed_sources: list[str] = field(default_factory=list)
    rate_limited_sources: list[str] = field(default_factory=list)
