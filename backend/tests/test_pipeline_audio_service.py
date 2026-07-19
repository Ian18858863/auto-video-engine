from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.audio.tts_service import TTSResult
from app.core.database import Base
from app.models import HotItem, ProductionPipeline
from app.pipeline.project_identity import ProjectIdentityError
from app.pipeline.audio_service import (
    PipelineAudioNotFoundError,
    generate_pipeline_voiceover_audio,
)
from app.pipeline.service import build_project_from_hotspot


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def prepared_material_root(tmp_path) -> Path:
    root = tmp_path / "短视频工厂"
    (root / "项目").mkdir(parents=True, exist_ok=True)
    return root


def add_hotspot(session: Session) -> HotItem:
    item = HotItem(
        source_platform="Hacker News",
        source_type="community",
        title="AI 搜索改变信息获取方式",
        url="https://example.com/audio-service",
        published_at=datetime.now(timezone.utc),
        summary="AI 搜索正在改变普通人获取信息的方式。",
        tags=["AI", "搜索"],
        category="AI",
        hot_score=88,
        topic_score=92,
        save_score=90,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def test_generate_pipeline_voiceover_audio_writes_audio_and_srt(tmp_path, monkeypatch):
    def fake_synthesize(text, output_path, provider=None):
        output_path.write_bytes(b"fake wav")
        return TTSResult(
            success=True,
            status="已生成",
            provider=provider or "windows_sapi",
            audio_path=str(output_path),
            reason="本地 TTS 配音已生成。",
        )

    monkeypatch.setattr("app.pipeline.audio_service.synthesize_tts", fake_synthesize)

    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )

        result = generate_pipeline_voiceover_audio(
            session,
            built.pipeline_id,
            project_root=prepared_material_root(tmp_path) / "项目",
        )

        assert result["success"] is True
        assert result["provider"] == "windows_sapi"
        assert result["real_tts_api_called"] is False
        assert result["huoshan_tts_ready"] is True
        assert result["audio_path"].endswith("口播音频.wav")
        assert result["subtitle_path"].endswith("字幕.srt")
        assert "00:00:" in result["subtitle_preview"]
        assert (tmp_path / "短视频工厂").exists()


def test_generate_pipeline_voiceover_audio_reports_local_tts_failure(tmp_path):
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=prepared_material_root(tmp_path),
        )

        result = generate_pipeline_voiceover_audio(
            session,
            built.pipeline_id,
            provider="unknown",
            project_root=prepared_material_root(tmp_path) / "项目",
        )

        assert result["success"] is False
        assert result["audio_path"] == ""
        assert result["subtitle_path"].endswith("字幕.srt")
        assert "字幕已生成，但配音失败" in result["message"]


def test_generate_pipeline_voiceover_audio_missing_pipeline_returns_clear_error():
    with make_session() as session:
        try:
            generate_pipeline_voiceover_audio(session, 999)
        except PipelineAudioNotFoundError as exc:
            assert "流程不存在" in str(exc)
        else:
            raise AssertionError("缺少流程时应返回明确错误")


def test_audio_write_rejects_spoofed_stored_project_directory(tmp_path):
    material_root = tmp_path / "短视频工厂"
    project_root = material_root / "项目"
    project_root.mkdir(parents=True)
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=material_root,
        )
        hostile = tmp_path / "hostile-title-directory"
        hostile.mkdir()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        pipeline.project_dir = str(hostile)
        session.commit()

        with pytest.raises(ProjectIdentityError, match="project_dir|canonical"):
            generate_pipeline_voiceover_audio(
                session,
                built.pipeline_id,
                project_root=project_root,
            )
        assert not (hostile / "素材信息").exists()


def test_audio_write_rejects_reparse_material_info_before_file_or_state_change(
    tmp_path,
    monkeypatch,
):
    material_root = prepared_material_root(tmp_path)
    project_root = material_root / "项目"
    with make_session() as session:
        hotspot = add_hotspot(session)
        built = build_project_from_hotspot(
            session,
            hotspot.id,
            material_root=material_root,
        )
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        info_dir = Path(pipeline.project_dir) / "素材信息"
        before_step = copy.deepcopy(
            next(step for step in pipeline.steps if step.name == "生成剪辑清单").output_summary
        )
        original_probe = __import__(
            "app.pipeline.project_identity",
            fromlist=["_is_reparse"],
        )._is_reparse

        def simulated_reparse(path: Path) -> bool:
            return Path(path) == info_dir or original_probe(path)

        monkeypatch.setattr(
            "app.pipeline.project_identity._is_reparse",
            simulated_reparse,
        )

        with pytest.raises(ProjectIdentityError, match="reparse|junction|child"):
            generate_pipeline_voiceover_audio(
                session,
                built.pipeline_id,
                project_root=project_root,
            )

        session.expire_all()
        pipeline = session.get(ProductionPipeline, built.pipeline_id)
        after_step = next(
            step for step in pipeline.steps if step.name == "生成剪辑清单"
        ).output_summary
        assert after_step == before_step
        assert not (info_dir / "口播音频.wav").exists()
        assert not (info_dir / "字幕.srt").exists()
