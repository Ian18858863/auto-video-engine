from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class VideoMetrics(BaseModel):
    views: int = Field(default=0, ge=0)
    likes: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)
    favorites: int = Field(default=0, ge=0)
    shares: int = Field(default=0, ge=0)
    followers_gained: int = Field(default=0, ge=0)
    completion_rate: float = Field(default=0.0, ge=0, le=100)
    average_watch_seconds: float = Field(default=0.0, ge=0)


class VideoPostCreateRequest(VideoMetrics):
    pipeline_id: int | None = None
    platform: str = Field(min_length=1, max_length=80)
    video_title: str = Field(min_length=1, max_length=300)
    published_at: datetime
    video_url: str = Field(default="", max_length=800)
    topic_type: str = Field(default="", max_length=80)
    video_format: str = Field(default="", max_length=80)
    hook_type: str = Field(default="", max_length=80)
    duration_seconds: int = Field(default=0, ge=0)
    notes: str = ""


class VideoPostUpdateRequest(BaseModel):
    pipeline_id: int | None = None
    platform: str | None = Field(default=None, min_length=1, max_length=80)
    video_title: str | None = Field(default=None, min_length=1, max_length=300)
    published_at: datetime | None = None
    video_url: str | None = Field(default=None, max_length=800)
    topic_type: str | None = Field(default=None, max_length=80)
    video_format: str | None = Field(default=None, max_length=80)
    hook_type: str | None = Field(default=None, max_length=80)
    duration_seconds: int | None = Field(default=None, ge=0)
    notes: str | None = None


class VideoSnapshotCreateRequest(VideoMetrics):
    notes: str = ""
