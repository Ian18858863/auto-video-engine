from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.audio.subtitle_service import (
    build_srt_from_segments,
    build_subtitle_segments,
    write_srt,
)
from app.audio.tts_service import synthesize_tts
from app.core.config import get_settings, resolve_configured_path
from app.models import PipelineStep, ProductionPipeline, ScriptAsset
from app.pipeline.project_identity import (
    begin_prepared_project_child_directory_use,
    prepared_project_output_path,
)


class PipelineAudioNotFoundError(ValueError):
    pass


def generate_pipeline_voiceover_audio(
    session: Session,
    pipeline_id: int,
    *,
    provider: str | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None:
        raise PipelineAudioNotFoundError(f"流程不存在：{pipeline_id}")
    if pipeline.script_asset_id is None:
        raise PipelineAudioNotFoundError("流程还没有脚本，不能生成配音。")
    if not pipeline.project_dir:
        raise PipelineAudioNotFoundError("项目目录还没有创建，不能保存配音文件。")

    script = session.get(ScriptAsset, pipeline.script_asset_id)
    if script is None:
        raise PipelineAudioNotFoundError(f"脚本素材不存在：{pipeline.script_asset_id}")
    voiceover = (script.voiceover or "").strip()
    if not voiceover:
        raise PipelineAudioNotFoundError("口播稿为空，不能生成配音。")

    approved_project_root = resolve_configured_path(
        project_root or get_settings().project_root_dir
    )
    info_use = begin_prepared_project_child_directory_use(
        pipeline,
        project_root=approved_project_root,
        relative_path="素材信息",
        create=False,
    )
    audio_path = prepared_project_output_path(info_use, "口播音频.wav")
    subtitle_path = prepared_project_output_path(info_use, "字幕.srt")

    segments = build_subtitle_segments(voiceover, total_duration_seconds=60)
    srt_content = build_srt_from_segments(segments)
    write_srt(subtitle_path, segments)

    tts_result = synthesize_tts(voiceover, audio_path, provider=provider)
    prepared_project_output_path(info_use, "字幕.srt", must_exist=True)
    prepared_project_output_path(
        info_use,
        "口播音频.wav",
        must_exist=tts_result.success,
    )
    info_use.verify()
    _merge_audio_outputs_into_pipeline(session, pipeline, audio_path, subtitle_path, tts_result.success)

    return {
        "pipeline_id": pipeline.id,
        "script_asset_id": script.id,
        "success": tts_result.success,
        "provider": tts_result.provider,
        "audio_path": str(audio_path) if tts_result.success else "",
        "subtitle_path": str(subtitle_path),
        "subtitle_preview": srt_content[:800],
        "message": tts_result.reason
        if tts_result.success
        else f"字幕已生成，但配音失败：{tts_result.reason}",
        "real_tts_api_called": False,
        "huoshan_tts_ready": True,
    }


def _merge_audio_outputs_into_pipeline(
    session: Session,
    pipeline: ProductionPipeline,
    audio_path: Path,
    subtitle_path: Path,
    audio_success: bool,
) -> None:
    step = _find_step(pipeline, "生成剪辑清单")
    if step is None:
        return
    payload = dict(step.output_summary or {})
    generated_files = dict(payload.get("generated_files") or {})
    if audio_success:
        generated_files["voiceover_audio"] = str(audio_path)
    generated_files["subtitle_srt"] = str(subtitle_path)
    payload["generated_files"] = generated_files
    payload["voiceover_audio_status"] = "已生成" if audio_success else "生成失败"
    step.output_summary = payload
    session.add(step)
    session.commit()


def _find_step(pipeline: ProductionPipeline, name: str) -> PipelineStep | None:
    for step in pipeline.steps:
        if step.name == name:
            return step
    return None
