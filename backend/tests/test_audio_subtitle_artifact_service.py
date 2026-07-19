from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import wave

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.audio.tts_service import TTSResult
from app.core.database import Base
from app.models import HotItem, ProductionPipeline
from app.pipeline.audio_subtitle_artifact_service import (
    AudioSubtitleArtifactError,
    build_pipeline_audio_subtitle_artifact,
)
from app.pipeline.service import build_project_from_hotspot


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "D" / "material-root"
    (root / "项目").mkdir(parents=True, exist_ok=True)
    return root


def _build_pipeline(session: Session, root: Path) -> int:
    hotspot = HotItem(
        source_platform="test",
        source_type="manual",
        title="audio artifact test",
        url="https://example.test/audio",
        published_at=datetime.now(timezone.utc),
        summary="audio",
        tags=["test"],
        category="test",
        hot_score=90,
        topic_score=90,
        save_score=90,
    )
    session.add(hotspot)
    session.commit()
    session.refresh(hotspot)
    return build_project_from_hotspot(
        session,
        hotspot.id,
        material_root=root,
    ).pipeline_id


def _wav_bytes(duration_seconds: float = 1.0) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * int(8000 * duration_seconds))
    return buffer.getvalue()


def test_artifact_uses_measured_wav_duration_and_append_only_versions(tmp_path: Path):
    root = _root(tmp_path)
    wav = _wav_bytes(1.25)

    def fake_tts(text: str, output_path: Path, *, provider: str | None = None):
        output_path.write_bytes(wav)
        return TTSResult(
            success=True,
            status="已生成",
            provider=provider or "test-tts",
            audio_path=str(output_path),
            reason="ok",
        )

    with _session() as session:
        pipeline_id = _build_pipeline(session, root)
        first = build_pipeline_audio_subtitle_artifact(
            session,
            pipeline_id,
            project_root=root / "项目",
            synthesizer=fake_tts,
        )
        second = build_pipeline_audio_subtitle_artifact(
            session,
            pipeline_id,
            project_root=root / "项目",
            synthesizer=fake_tts,
        )
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        public = first.to_public_dict(project_dir=Path(pipeline.project_dir))

        assert first.artifact_id != second.artifact_id
        assert first.audio_duration_seconds == pytest.approx(1.25)
        assert first.audio_path.is_file()
        assert first.subtitle_path.is_file()
        assert first.manifest_path.is_file()
        assert not (first.audio_path.parent / ".tts-input.wav").exists()
        assert str(pipeline.project_dir) not in str(public)
        assert public["audio"]["duration_seconds"] == pytest.approx(1.25)
        srt = first.subtitle_path.read_text(encoding="utf-8")
        assert "00:00:01," in srt
        assert (second.audio_path.parent / "voiceover.wav").is_file()


def test_artifact_rejects_non_decodable_tts_bytes_without_publishing(tmp_path: Path):
    root = _root(tmp_path)

    def fake_tts(text: str, output_path: Path, *, provider: str | None = None):
        output_path.write_bytes(b"not-a-wav")
        return TTSResult(
            success=True,
            status="已生成",
            provider="test-tts",
            audio_path=str(output_path),
            reason="spoof",
        )

    with _session() as session:
        pipeline_id = _build_pipeline(session, root)
        with pytest.raises(AudioSubtitleArtifactError, match="WAV|音频"):
            build_pipeline_audio_subtitle_artifact(
                session,
                pipeline_id,
                project_root=root / "项目",
                synthesizer=fake_tts,
                artifact_id="as-1-0000000000000000",
            )
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        artifact_root = Path(pipeline.project_dir) / "素材信息" / "音频字幕版本"
        assert not (artifact_root / "as-1-0000000000000000").exists()
