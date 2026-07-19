from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DDL,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.orm.attributes import NO_VALUE

from app.core.database import Base
from app.pipeline.project_identity import (
    ProjectIdentityError,
    SQLITE_PROJECT_IDENTITY_TRIGGER_SQL,
    sqlalchemy_pending_project_key,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class HotItem(Base):
    __tablename__ = "hot_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_platform: Mapped[str] = mapped_column(String(100), index=True)
    source_type: Mapped[str] = mapped_column(String(50), index=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    url: Mapped[str] = mapped_column(String(800), unique=True, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    summary: Mapped[str] = mapped_column(String(300), default="")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    category: Mapped[str] = mapped_column(String(50), default="其他", index=True)
    data_origin: Mapped[str] = mapped_column(String(30), default="real", index=True)
    origin_note: Mapped[str] = mapped_column(String(200), default="")
    is_filtered: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    filter_reason: Mapped[str] = mapped_column(String(200), default="")
    hot_score: Mapped[float] = mapped_column(Float, default=0.0)
    topic_score: Mapped[float] = mapped_column(Float, default=0.0)
    save_score: Mapped[float] = mapped_column(Float, default=0.0)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    freshness_score: Mapped[float] = mapped_column(Float, default=0.0)
    duplicate_reason: Mapped[str] = mapped_column(String(300), default="")
    duplicate_group_key: Mapped[str] = mapped_column(String(160), default="", index=True)
    lifecycle_status: Mapped[str] = mapped_column(String(40), default="active", index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archive_reason: Mapped[str] = mapped_column(String(200), default="")
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    has_topic_ideas: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    has_script: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    topic_ideas: Mapped[list[TopicIdea]] = relationship(
        back_populates="hot_item",
        cascade="all, delete-orphan",
    )


class TopicIdea(Base):
    __tablename__ = "topic_ideas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hot_item_id: Mapped[int] = mapped_column(ForeignKey("hot_items.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    core_view: Mapped[str] = mapped_column(String(500), default="")
    recommended_format: Mapped[str] = mapped_column(String(80), default="")
    why_watch: Mapped[str] = mapped_column(String(500), default="")
    why_save: Mapped[str] = mapped_column(String(500), default="")
    comment_trigger: Mapped[str] = mapped_column(String(500), default="")
    risk_note: Mapped[str] = mapped_column(String(500), default="")
    recommended_duration: Mapped[str] = mapped_column(String(80), default="")
    hook: Mapped[str] = mapped_column(String(500), default="")
    video_type: Mapped[str] = mapped_column(String(80), default="single_deep_dive", index=True)
    generation_source: Mapped[str] = mapped_column(String(80), default="fake_provider", index=True)
    recommendation_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    recommendation_bucket: Mapped[str] = mapped_column(String(40), default="backup", index=True)
    source_hotspot_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    source_hotspot_titles: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_example: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    hot_item: Mapped[HotItem] = relationship(back_populates="topic_ideas")
    script_assets: Mapped[list[ScriptAsset]] = relationship(
        back_populates="topic_idea",
        cascade="all, delete-orphan",
    )


class AiGeneration(Base):
    __tablename__ = "ai_generations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation_type: Mapped[str] = mapped_column(String(80), index=True)
    input_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    input_content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(80), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ScriptAsset(Base):
    __tablename__ = "script_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_idea_id: Mapped[int] = mapped_column(ForeignKey("topic_ideas.id"), index=True)
    voiceover: Mapped[str] = mapped_column(Text, default="")
    storyboard: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    subtitles: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    animation_prompts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    title_options: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    cover_copy: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    publish_copy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    edit_checklist: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    topic_idea: Mapped[TopicIdea] = relationship(back_populates="script_assets")


class ContentQualityCheck(Base):
    __tablename__ = "content_quality_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pipeline_id: Mapped[int | None] = mapped_column(
        ForeignKey("production_pipelines.id"),
        nullable=True,
        index=True,
    )
    script_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("script_assets.id"),
        nullable=True,
        index=True,
    )
    source_type: Mapped[str] = mapped_column(String(80), default="手动粘贴", index=True)
    source_title: Mapped[str] = mapped_column(String(300), default="")
    checked_text: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CopywritingSample(Base):
    __tablename__ = "copywriting_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    sample_type: Mapped[str] = mapped_column(String(80), index=True)
    sample_text: Mapped[str] = mapped_column(Text, default="")
    scene: Mapped[str] = mapped_column(String(100), default="", index=True)
    content_direction: Mapped[str] = mapped_column(String(120), default="", index=True)
    structure_type: Mapped[str] = mapped_column(String(120), default="")
    why_reference: Mapped[str] = mapped_column(Text, default="")
    reusable_points: Mapped[str] = mapped_column(Text, default="")
    avoid_points: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_key_reference: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )


class VideoPost(Base):
    __tablename__ = "video_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pipeline_id: Mapped[int | None] = mapped_column(
        ForeignKey("production_pipelines.id"),
        nullable=True,
        index=True,
    )
    platform: Mapped[str] = mapped_column(String(80), index=True)
    video_title: Mapped[str] = mapped_column(String(300))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    video_url: Mapped[str] = mapped_column(String(800), default="")
    topic_type: Mapped[str] = mapped_column(String(80), default="")
    video_format: Mapped[str] = mapped_column(String(80), default="")
    hook_type: Mapped[str] = mapped_column(String(80), default="")
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    favorites: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    followers_gained: Mapped[int] = mapped_column(Integer, default=0)
    completion_rate: Mapped[float] = mapped_column(Float, default=0.0)
    average_watch_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    profile_visits: Mapped[int] = mapped_column(Integer, default=0)
    current_view_velocity: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )

    snapshots: Mapped[list[VideoPostSnapshot]] = relationship(
        back_populates="video_post",
        cascade="all, delete-orphan",
        order_by="VideoPostSnapshot.captured_at",
    )


class VideoPostSnapshot(Base):
    __tablename__ = "video_post_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_post_id: Mapped[int] = mapped_column(
        ForeignKey("video_posts.id"),
        index=True,
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )
    elapsed_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    favorites: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    followers_gained: Mapped[int] = mapped_column(Integer, default=0)
    completion_rate: Mapped[float] = mapped_column(Float, default=0.0)
    average_watch_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str] = mapped_column(Text, default="")

    video_post: Mapped[VideoPost] = relationship(back_populates="snapshots")


class SourceRun(Base):
    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_name: Mapped[str] = mapped_column(String(120), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="失败", index=True)
    items_fetched: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    is_rate_limited: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class AppConfig(Base):
    __tablename__ = "app_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProductionPipeline(Base):
    __tablename__ = "production_pipelines"
    __table_args__ = (
        UniqueConstraint("project_key", name="uq_production_pipelines_project_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hotspot_id: Mapped[int] = mapped_column(ForeignKey("hot_items.id"), index=True)
    topic_idea_id: Mapped[int | None] = mapped_column(
        ForeignKey("topic_ideas.id"),
        nullable=True,
        index=True,
    )
    script_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("script_assets.id"),
        nullable=True,
        index=True,
    )
    project_name: Mapped[str] = mapped_column(String(200), default="")
    project_dir: Mapped[str] = mapped_column(String(800), default="")
    project_key: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        index=True,
        default=sqlalchemy_pending_project_key,
    )
    legacy_project_dir: Mapped[str | None] = mapped_column(String(800), nullable=True)
    completion_evidence: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    status: Mapped[str] = mapped_column(String(40), default="未开始", index=True)
    provider: Mapped[str] = mapped_column(String(80), default="fake")
    model: Mapped[str] = mapped_column(String(120), default="fake-low-cost")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )

    steps: Mapped[list[PipelineStep]] = relationship(
        back_populates="pipeline",
        cascade="all, delete-orphan",
        order_by="PipelineStep.step_order",
    )
    script_asset: Mapped[ScriptAsset | None] = relationship(
        foreign_keys=[script_asset_id],
        viewonly=True,
    )


@event.listens_for(ProductionPipeline.project_key, "set", retval=True, active_history=True)
def _prevent_project_key_mutation(
    _target: ProductionPipeline,
    value: str,
    old_value: object,
    _initiator: object,
) -> str:
    if old_value in {NO_VALUE, None, ""} or old_value == value:
        return value
    if isinstance(old_value, str) and old_value.startswith("pending-"):
        return value
    raise ProjectIdentityError("project_key is immutable after first assignment")


for _project_identity_trigger_sql in SQLITE_PROJECT_IDENTITY_TRIGGER_SQL.values():
    event.listen(
        ProductionPipeline.__table__,
        "after_create",
        DDL(_project_identity_trigger_sql).execute_if(dialect="sqlite"),
    )


class PipelineStep(Base):
    __tablename__ = "pipeline_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(
        ForeignKey("production_pipelines.id"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(80))
    step_order: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), default="未开始", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    output_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    pipeline: Mapped[ProductionPipeline] = relationship(back_populates="steps")
