from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import wave

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.audio.tts_service import TTSResult
from app.core.config import get_settings
from app.core.database import Base, get_db
from app.main import app
from app.models import HotItem, ProductionPipeline
from app.pipeline.audio_subtitle_artifact_service import (
    build_pipeline_audio_subtitle_artifact,
)
from app.pipeline.service import build_project_from_hotspot
from app.pipeline.timeline_media_service import (
    TimelineMediaServiceError,
    bind_pipeline_audio_subtitle_artifact,
    render_pipeline_timeline_media,
)
from app.pipeline.timeline_spec import TimelineSpecLedger, build_timeline_spec_payload


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _factory_root(tmp_path: Path) -> Path:
    root = tmp_path / "D" / "factory"
    (root / "项目").mkdir(parents=True, exist_ok=True)
    return root


def _build_pipeline(session: Session, root: Path) -> int:
    hotspot = HotItem(
        source_platform="test",
        source_type="manual",
        title="timeline media service",
        url="https://example.test/timeline-media",
        published_at=datetime.now(timezone.utc),
        summary="timeline media",
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


def _fake_tts(text: str, output_path: Path, *, provider: str | None = None):
    output_path.write_bytes(_wav_bytes(1.0))
    return TTSResult(
        success=True,
        status="generated",
        provider=provider or "test-tts",
        audio_path=str(output_path),
        reason="ok",
    )


def _create_base_timeline(
    session: Session,
    pipeline_id: int,
    root: Path,
    *,
    end: str = "00:01",
) -> None:
    pipeline = session.get(ProductionPipeline, pipeline_id)
    assert pipeline is not None
    payload = build_timeline_spec_payload(
        [
            {
                "segment_id": "shot-01",
                "start": "00:00",
                "end": end,
                "voiceover": "test voiceover",
            }
        ]
    )
    TimelineSpecLedger(pipeline, project_root=root / "项目").create(
        payload,
        source="test_base_timeline",
    )


def test_binding_appends_verified_artifact_to_timeline(tmp_path: Path):
    root = _factory_root(tmp_path)
    with _session() as session:
        pipeline_id = _build_pipeline(session, root)
        artifact = build_pipeline_audio_subtitle_artifact(
            session,
            pipeline_id,
            project_root=root / "项目",
            synthesizer=_fake_tts,
        )
        _create_base_timeline(session, pipeline_id, root)

        binding = bind_pipeline_audio_subtitle_artifact(
            session,
            pipeline_id,
            artifact.artifact_id,
            project_root=root / "项目",
        )
        ledger = TimelineSpecLedger(
            session.get(ProductionPipeline, pipeline_id),
            project_root=root / "项目",
        )
        versions = ledger.list()
        assert len(versions) >= 2
        assert versions[-1].artifacts["artifact_id"] == artifact.artifact_id
        assert any(not item.artifacts for item in versions[:-1])
        assert binding.timeline_spec.source == "audio_subtitle_binding"
        assert binding.timeline_spec.artifacts["artifact_id"] == artifact.artifact_id
        assert binding.timeline_spec.artifacts["voiceover"]["duration_seconds"] == pytest.approx(1.0)
        assert str(root) not in json.dumps(binding.to_dict(project_dir=Path(session.get(ProductionPipeline, pipeline_id).project_dir)))


def test_binding_rejects_historical_timeline_instead_of_resurrecting_it(tmp_path: Path):
    root = _factory_root(tmp_path)
    with _session() as session:
        pipeline_id = _build_pipeline(session, root)
        artifact = build_pipeline_audio_subtitle_artifact(
            session,
            pipeline_id,
            project_root=root / "项目",
            synthesizer=_fake_tts,
        )
        _create_base_timeline(session, pipeline_id, root)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        ledger = TimelineSpecLedger(pipeline, project_root=root / "项目")
        historical = ledger.latest()
        assert historical is not None
        current = ledger.create(
            build_timeline_spec_payload(
                [
                    {
                        "segment_id": "shot-01",
                        "start": "00:00",
                        "end": "00:01",
                        "voiceover": "replacement shot is now current",
                    }
                ]
            ),
            source="test_reselection",
        )
        assert current.version_id != historical.version_id

        with pytest.raises(TimelineMediaServiceError, match="current timeline"):
            bind_pipeline_audio_subtitle_artifact(
                session,
                pipeline_id,
                artifact.artifact_id,
                project_root=root / "项目",
                timeline_version_id=historical.version_id,
            )

        assert ledger.latest().version_id == current.version_id


def test_binding_rejects_duration_mismatch_and_manifest_tampering(tmp_path: Path):
    root = _factory_root(tmp_path)
    with _session() as session:
        pipeline_id = _build_pipeline(session, root)
        artifact = build_pipeline_audio_subtitle_artifact(
            session,
            pipeline_id,
            project_root=root / "项目",
            synthesizer=_fake_tts,
        )
        _create_base_timeline(session, pipeline_id, root, end="00:00.5")
        with pytest.raises(TimelineMediaServiceError, match="duration"):
            bind_pipeline_audio_subtitle_artifact(
                session,
                pipeline_id,
                artifact.artifact_id,
                project_root=root / "项目",
                duration_tolerance_seconds=0.0,
            )

        manifest = artifact.manifest_path
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        raw["audio"]["sha256"] = "f" * 64
        manifest.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(TimelineMediaServiceError, match="hash"):
            bind_pipeline_audio_subtitle_artifact(
                session,
                pipeline_id,
                artifact.artifact_id,
                project_root=root / "项目",
            )


def test_render_retimes_planned_timeline_to_measured_voiceover_before_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The production render seam must follow the approved audio-first order.

    The standalone binding seam remains fail-closed for callers that claim an
    already-timed timeline.  Rendering, however, is the product operation that
    turns a planned edit into a measured media timeline, so it must append a
    new version whose final edge matches the decoded WAV before FFmpeg runs.
    """

    root = _factory_root(tmp_path)
    captured: dict[str, object] = {}
    with _session() as session:
        pipeline_id = _build_pipeline(session, root)
        artifact = build_pipeline_audio_subtitle_artifact(
            session,
            pipeline_id,
            project_root=root / "项目",
            synthesizer=_fake_tts,
        )
        _create_base_timeline(session, pipeline_id, root, end="00:02")

        def fake_render(snapshot, **_kwargs):
            captured["timeline"] = snapshot
            return SimpleNamespace(version_id="media-audio-first")

        monkeypatch.setattr(
            "app.pipeline.timeline_media_service.render_timeline_media",
            fake_render,
        )

        run = render_pipeline_timeline_media(
            session,
            pipeline_id,
            artifact.artifact_id,
            project_root=root / "项目",
        )

        assert run.binding.timeline_spec.source == "audio_first_timing"
        assert run.binding.timeline_spec.segments[-1]["end_seconds"] == pytest.approx(1.0)
        assert run.binding.timeline_spec.artifacts["artifact_id"] == artifact.artifact_id
        assert captured["timeline"] is run.binding.timeline_spec


def test_binding_route_returns_relative_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _factory_root(tmp_path)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    with factory() as setup:
        pipeline_id = _build_pipeline(setup, root)
        artifact = build_pipeline_audio_subtitle_artifact(
            setup,
            pipeline_id,
            project_root=root / "项目",
            synthesizer=_fake_tts,
        )
        _create_base_timeline(setup, pipeline_id, root)

    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setenv("PROJECT_ROOT_DIR", str(root / "项目"))
    get_settings.cache_clear()
    app.dependency_overrides[get_db] = override_get_db
    try:
        response = TestClient(app).post(
            f"/api/pipeline/{pipeline_id}/timeline-media/bind",
            json={"artifact_id": artifact.artifact_id},
        )
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["publish_supported"] is False
    assert data["timeline_spec"]["artifacts"]["artifact_id"] == artifact.artifact_id
    assert str(root) not in response.text


def test_pipeline_render_reuses_latest_verified_visual_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _factory_root(tmp_path)
    captured: dict[str, object] = {}
    with _session() as session:
        pipeline_id = _build_pipeline(session, root)
        artifact = build_pipeline_audio_subtitle_artifact(
            session,
            pipeline_id,
            project_root=root / "项目",
            synthesizer=_fake_tts,
        )
        _create_base_timeline(session, pipeline_id, root)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None and pipeline.project_dir
        project_dir = Path(pipeline.project_dir)

        prior = project_dir / "成片" / "versions" / "media-old"
        visual_manifest = prior / "visual-root" / "ts-old" / "render-manifest.json"
        visual_manifest.parent.mkdir(parents=True)
        visual_manifest.write_bytes(b"verified-visual-manifest")
        prior_artifact = prior / "rendered.mp4"
        prior_artifact.write_bytes(b"verified-old-artifact")
        import hashlib

        (prior / "technical-report.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "timeline_version_id": "ts-old",
                    "timeline_input_hash": "a" * 64,
                    "artifact_sha256": hashlib.sha256(prior_artifact.read_bytes()).hexdigest(),
                    "visual_render_manifest_sha256": hashlib.sha256(
                        visual_manifest.read_bytes()
                    ).hexdigest(),
                }
            ),
            encoding="utf-8",
        )

        def fake_render(snapshot, **kwargs):
            captured["reuse"] = kwargs.get("reuse_from_visual_manifest")
            return SimpleNamespace(version_id="media-new")

        monkeypatch.setattr(
            "app.pipeline.timeline_media_service.render_timeline_media",
            fake_render,
        )
        run = render_pipeline_timeline_media(
            session,
            pipeline_id,
            artifact.artifact_id,
            project_root=root / "项目",
        )

    assert run.render.version_id == "media-new"
    assert captured["reuse"] == visual_manifest.resolve(strict=True)
