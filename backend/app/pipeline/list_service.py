from __future__ import annotations

from math import ceil

from sqlalchemy.orm import Session, selectinload

from app.models import ProductionPipeline
from app.pipeline.completion_policy import evaluate_pipeline_completion
from app.pipeline.models import PipelineListItem, PipelineListResult


def list_pipelines(
    session: Session,
    *,
    status: str | None = None,
    failed_only: bool = False,
    needs_confirmation_only: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> PipelineListResult:
    items = derive_pipeline_items(session)
    if status:
        items = [item for item in items if item.status == status]
    if failed_only:
        items = [item for item in items if item.has_failed]
    if needs_confirmation_only:
        items = [item for item in items if item.needs_confirmation]

    total = len(items)
    start = (page - 1) * page_size
    return PipelineListResult(
        items=items[start : start + page_size],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=ceil(total / page_size) if total else 0,
    )


def serialize_pipeline_list(result: PipelineListResult) -> dict:
    return {
        "items": [
            {
                "pipeline_id": item.pipeline_id,
                "hotspot_id": item.hotspot_id,
                "topic_idea_id": item.topic_idea_id,
                "script_asset_id": item.script_asset_id,
                "project_name": item.project_name,
                "project_key": item.project_key,
                "project_dir": item.project_dir,
                "legacy_project_dir": item.legacy_project_dir,
                "status": item.status,
                "completion_reason_code": item.completion_reason_code,
                "next_surface": item.next_surface,
                "current_step": item.current_step,
                "completed_steps": item.completed_steps,
                "total_steps": item.total_steps,
                "has_failed": item.has_failed,
                "failure_reason": item.failure_reason,
                "needs_confirmation": item.needs_confirmation,
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat(),
                "next_suggestion": item.next_suggestion,
            }
            for item in result.items
        ],
        "pagination": {
            "page": result.page,
            "page_size": result.page_size,
            "total": result.total,
            "total_pages": result.total_pages,
        },
    }


def derive_pipeline_items(session: Session) -> list[PipelineListItem]:
    pipelines = (
        session.query(ProductionPipeline)
        .options(
            selectinload(ProductionPipeline.steps),
            selectinload(ProductionPipeline.script_asset),
        )
        .order_by(
            ProductionPipeline.created_at.desc(),
            ProductionPipeline.id.desc(),
        )
        .all()
    )
    return [_build_list_item(pipeline) for pipeline in pipelines]


def _build_list_item(pipeline: ProductionPipeline) -> PipelineListItem:
    decision = evaluate_pipeline_completion(pipeline)
    steps = sorted(
        pipeline.steps,
        key=lambda step: (
            (0, step.step_order) if type(step.step_order) is int else (1, 0)
        ),
    )
    completed_steps = sum(step.status == "已完成" for step in steps)
    failed_step = next((step for step in steps if step.status == "失败"), None)
    return PipelineListItem(
        pipeline_id=pipeline.id,
        hotspot_id=pipeline.hotspot_id,
        topic_idea_id=pipeline.topic_idea_id,
        script_asset_id=pipeline.script_asset_id,
        project_name=pipeline.project_name,
        project_key=pipeline.project_key,
        project_dir=pipeline.project_dir,
        legacy_project_dir=pipeline.legacy_project_dir,
        status=decision.status,
        completion_reason_code=decision.reason_code,
        next_surface=decision.next_surface,
        current_step=decision.current_step,
        completed_steps=completed_steps,
        total_steps=len(steps),
        has_failed=failed_step is not None,
        failure_reason=(
            failed_step.error
            if failed_step is not None and failed_step.error
            else decision.message if decision.needs_confirmation else ""
        ),
        needs_confirmation=decision.needs_confirmation,
        created_at=pipeline.created_at,
        updated_at=pipeline.updated_at,
        next_suggestion=decision.next_action,
    )
