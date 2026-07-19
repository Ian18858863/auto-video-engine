from __future__ import annotations

from pydantic import BaseModel, Field


class CopywritingSamplePayload(BaseModel):
    title: str = Field(default="", max_length=300)
    sample_type: str = Field(default="", max_length=80)
    sample_text: str = ""
    scene: str = Field(default="", max_length=100)
    content_direction: str = Field(default="", max_length=120)
    structure_type: str = Field(default="", max_length=120)
    why_reference: str = ""
    reusable_points: str = ""
    avoid_points: str = ""
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    is_key_reference: bool = False
    notes: str = ""
