from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ManualHotspotImportRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    source_platform: str = Field(default="手动导入", max_length=100)
    url: str = Field(default="", max_length=800)
    summary: str = Field(default="", max_length=300)
    category: str = Field(default="其他", max_length=50)
    tags: list[str] = Field(default_factory=list, max_length=5)
    published_at: datetime | None = None
    notes: str = Field(default="", max_length=500)


class HotspotFetchRequest(BaseModel):
    source_names: list[str] = Field(default_factory=list)


class AutoHotspotRunRequest(BaseModel):
    source_names: list[str] = Field(default_factory=list)
    force: bool = False
    generate_topics: bool = True
    max_hotspots_for_topics: int = Field(default=70, ge=0, le=100)


class BrowserHotspotRunRequest(BaseModel):
    source_names: list[str] = Field(default_factory=list)
    force: bool = False
