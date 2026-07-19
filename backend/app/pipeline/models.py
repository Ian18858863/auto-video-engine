from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


PIPELINE_STEP_NAMES = [
    "生成候选选题",
    "选择候选选题",
    "生成脚本素材",
    "生成素材任务",
    "生成 Codex 图文任务",
    "生成数字人字段",
    "创建中文项目目录",
    "生成剪辑清单",
    "导出 script.md",
    "导出 shotlist.csv",
    "导出 edit_manifest.json",
]

PIPELINE_STATUSES = {"未开始", "进行中", "已完成", "失败", "需要人工确认"}


@dataclass(slots=True)
class PipelineStepResult:
    name: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str = ""
    output_id: int | None = None
    output_path: str = ""
    output_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def error(self) -> str:
        return self.error_message


@dataclass(slots=True)
class ProductionPipelineResult:
    pipeline_id: int
    hotspot_id: int
    topic_idea_id: int | None
    script_asset_id: int | None
    project_name: str
    project_key: str
    project_dir: str
    legacy_project_dir: str | None
    status: str
    completion_reason_code: str
    next_surface: str
    current_step: str
    provider: str
    model: str
    steps: list[PipelineStepResult] = field(default_factory=list)
    material_tasks: list[dict[str, Any]] = field(default_factory=list)
    graphic_tasks: list[dict[str, Any]] = field(default_factory=list)
    exports: dict[str, str] = field(default_factory=dict)
    generated_files: dict[str, str] = field(default_factory=dict)
    failure_reasons: list[dict[str, str]] = field(default_factory=list)
    needs_confirmation: bool = False
    next_suggestion: str = ""


@dataclass(slots=True)
class PipelineListItem:
    pipeline_id: int
    hotspot_id: int
    topic_idea_id: int | None
    script_asset_id: int | None
    project_name: str
    project_key: str
    project_dir: str
    legacy_project_dir: str | None
    status: str
    completion_reason_code: str
    next_surface: str
    current_step: str
    completed_steps: int
    total_steps: int
    has_failed: bool
    failure_reason: str
    needs_confirmation: bool
    created_at: datetime
    updated_at: datetime
    next_suggestion: str


@dataclass(slots=True)
class PipelineListResult:
    items: list[PipelineListItem]
    total: int
    page: int
    page_size: int
    total_pages: int
