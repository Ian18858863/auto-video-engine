from __future__ import annotations

from pydantic import BaseModel, Field


class ManualQualityCheckRequest(BaseModel):
    text: str = Field(default="", max_length=30000)
    source_title: str = Field(default="手动粘贴内容", max_length=300)
    source_type: str = Field(default="手动粘贴", max_length=80)
    pipeline_id: int | None = None
    script_asset_id: int | None = None
