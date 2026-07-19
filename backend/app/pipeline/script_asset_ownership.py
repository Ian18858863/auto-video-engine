from __future__ import annotations

import copy

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import PipelineStep, ProductionPipeline, ScriptAsset


def ensure_pipeline_script_asset_owned(
    session: Session,
    pipeline: ProductionPipeline,
    script: ScriptAsset,
) -> ScriptAsset:
    """Return a project-owned mutable script, splitting legacy shared rows.

    AI generation responses are safely reusable, but ``ScriptAsset`` rows are
    mutable project working copies.  Older builds could point several projects
    at the same row.  Clone on the first later write so editing one legacy
    project cannot silently rewrite another one.
    """

    if pipeline.script_asset_id != script.id:
        raise ValueError("项目当前脚本与待修改脚本不一致，请刷新后重试。")

    owner_count = int(
        session.query(func.count(ProductionPipeline.id))
        .filter(ProductionPipeline.script_asset_id == script.id)
        .scalar()
        or 0
    )
    if owner_count <= 1:
        return script

    clone = ScriptAsset(
        topic_idea_id=script.topic_idea_id,
        voiceover=script.voiceover,
        storyboard=copy.deepcopy(script.storyboard or []),
        subtitles=copy.deepcopy(script.subtitles or []),
        animation_prompts=copy.deepcopy(script.animation_prompts or []),
        title_options=copy.deepcopy(script.title_options or []),
        cover_copy=copy.deepcopy(script.cover_copy or []),
        publish_copy=copy.deepcopy(script.publish_copy or {}),
        edit_checklist=copy.deepcopy(script.edit_checklist or []),
    )
    session.add(clone)
    session.flush()

    pipeline.script_asset_id = clone.id
    session.add(pipeline)

    script_step = (
        session.query(PipelineStep)
        .filter(PipelineStep.pipeline_id == pipeline.id)
        .filter(PipelineStep.name == "生成脚本素材")
        .one_or_none()
    )
    if script_step is not None:
        output = dict(script_step.output_summary or {})
        output["script_asset_id"] = clone.id
        script_step.output_summary = output
        session.add(script_step)

    return clone
