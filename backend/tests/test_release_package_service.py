from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import json
import struct

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
import pytest

from app.core.database import Base
from app.core.database import get_db
from app.main import app
from app.materials.stock_rights_evidence import (
    build_stock_rights_evidence,
    review_stock_rights_evidence,
)
from app.models import HotItem, ProductionPipeline
from app.pipeline.release_package_service import (
    ReleasePackageBlockedError,
    ReleasePackageCommitUnknownError,
    _build_release_metadata,
    create_release_package,
)
from app.pipeline.service import build_project_from_hotspot


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "D" / "factory"
    (root / "项目").mkdir(parents=True, exist_ok=True)
    return root


def _pipeline(session: Session, root: Path) -> int:
    hotspot = HotItem(
        source_platform="test",
        source_type="manual",
        title="release package",
        url="https://example.test/release-package",
        published_at=datetime.now(timezone.utc),
        summary="release package",
        tags=["test"],
        category="test",
        hot_score=90,
        topic_score=90,
        save_score=90,
    )
    session.add(hotspot)
    session.commit()
    session.refresh(hotspot)
    return build_project_from_hotspot(session, hotspot.id, material_root=root).pipeline_id


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _valid_mp4() -> bytes:
    return _box(b"ftyp", b"isom\x00\x00\x02\x00isom") + _box(
        b"moov", _box(b"mvhd")
    ) + _box(b"mdat", b"verified-frame-data")


def test_release_package_requires_canonical_approval(tmp_path: Path, monkeypatch):
    root = _root(tmp_path)
    with _session() as session:
        pipeline_id = _pipeline(session, root)
        with pytest.raises(ReleasePackageBlockedError, match="release blocked"):
            create_release_package(session, pipeline_id, project_root=root / "项目")


def test_release_package_is_project_relative_and_idempotent(tmp_path: Path, monkeypatch):
    root = _root(tmp_path)
    with _session() as session:
        pipeline_id = _pipeline(session, root)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        source = Path(pipeline.project_dir) / "成片" / "versions" / "v1" / "rendered.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(_valid_mp4())
        report = Path(pipeline.project_dir) / "成片" / "quality-reports" / "qr-1.json"
        report.parent.mkdir(parents=True)
        report.write_bytes(b"quality-report")
        artifact_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
        pipeline.completion_evidence = {
            "evidence_version": 1,
            "pipeline_state_sha256": "0" * 64,
            "artifact_relative_path": "成片/versions/v1/rendered.mp4",
            "artifact_sha256": artifact_sha,
            "quality_report_relative_path": "成片/quality-reports/qr-1.json",
            "quality_report_sha256": report_sha,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "approved_by": "Ian",
        }
        session.commit()

        class _Decision:
            is_complete = True
            reason_code = "approved_artifact_verified"
            message = "ok"

        monkeypatch.setattr(
            "app.pipeline.release_package_service.evaluate_pipeline_completion",
            lambda *_args, **_kwargs: _Decision(),
        )
        first = create_release_package(session, pipeline_id, project_root=root / "项目")
        second = create_release_package(session, pipeline_id, project_root=root / "项目")

        assert first.to_dict()["artifact_relative_path"].startswith("成片/releases/")
        assert first.artifact_sha256 == artifact_sha
        assert first.release_manifest_sha256 == second.release_manifest_sha256
        assert first.released_at == second.released_at
        assert str(pipeline.project_dir) not in repr(first.to_dict())


def test_release_package_route_keeps_blocked_boundary(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        with Session(engine) as db:
            yield db

    monkeypatch.setattr(
        "app.main.create_release_package",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ReleasePackageBlockedError("approval_missing", "need Ian")
        ),
    )
    app.dependency_overrides[get_db] = override_get_db
    try:
        response = TestClient(app).post("/api/pipeline/1/release-package")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 409
    assert "release blocked" in response.json()["detail"]


def test_release_package_final_verification_failure_requires_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _root(tmp_path)
    with _session() as session:
        pipeline_id = _pipeline(session, root)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        source = Path(pipeline.project_dir) / "成片" / "versions" / "v1" / "rendered.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(_valid_mp4())
        report = Path(pipeline.project_dir) / "成片" / "quality-reports" / "qr-1.json"
        report.parent.mkdir(parents=True)
        report.write_bytes(b"quality-report")
        artifact_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
        pipeline.completion_evidence = {
            "evidence_version": 1,
            "pipeline_state_sha256": "0" * 64,
            "artifact_relative_path": "成片/versions/v1/rendered.mp4",
            "artifact_sha256": artifact_sha,
            "quality_report_relative_path": "成片/quality-reports/qr-1.json",
            "quality_report_sha256": report_sha,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "approved_by": "Ian",
        }
        session.commit()

        class _Decision:
            is_complete = True
            reason_code = "approved_artifact_verified"
            message = "ok"

        monkeypatch.setattr(
            "app.pipeline.release_package_service.evaluate_pipeline_completion",
            lambda *_args, **_kwargs: _Decision(),
        )
        original_report_snapshot = (
            __import__(
                "app.pipeline.release_package_service",
                fromlist=["_guarded_report_snapshot"],
            )._guarded_report_snapshot
        )
        calls = 0

        def fail_after_side_effect(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise OSError("simulated final package read failure")
            return original_report_snapshot(*args, **kwargs)

        monkeypatch.setattr(
            "app.pipeline.release_package_service._guarded_report_snapshot",
            fail_after_side_effect,
        )
        with pytest.raises(ReleasePackageCommitUnknownError, match="reconcile"):
            create_release_package(session, pipeline_id, project_root=root / "项目")


def test_release_manifest_contains_script_and_durable_candidate_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _root(tmp_path)
    with _session() as session:
        pipeline_id = _pipeline(session, root)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        script = pipeline.script_asset
        assert script is not None
        script.title_options = [{"title": "可发布标题", "type": "主标题"}]
        script.cover_copy = [{"text": "封面短句"}]
        script.publish_copy = {"抖音": "发布说明"}
        script.storyboard = [{"source_refs": ["https://example.test/fact"]}]
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        material_step.output_summary = {
            "material_tasks": [{"shot_id": "shot-1"}],
            "candidate_pool": {
                "shot-1": {
                    "shot_id": "shot-1",
                    "revision": 2,
                    "locked_candidate_id": "candidate-2",
                    "candidates": [
                        {
                            "candidate_id": "candidate-1",
                            "source": "source-a",
                            "url": "https://example.test/a",
                            "rights": "licensed",
                            "evidence_kind": "licensed",
                            "status": "candidate",
                        },
                        {
                            "candidate_id": "candidate-2",
                            "source": "source-b",
                            "url": "https://example.test/b",
                            "rights": "commercial license with attribution",
                            "evidence_kind": "licensed",
                            "status": "locked",
                        },
                        {
                            "candidate_id": "candidate-3",
                            "source": "source-c",
                            "url": "https://example.test/c",
                            "rights": "licensed",
                            "evidence_kind": "licensed",
                            "status": "candidate",
                        },
                    ],
                }
            },
        }
        source = Path(pipeline.project_dir) / "成片" / "versions" / "v1" / "rendered.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(_valid_mp4())
        report = Path(pipeline.project_dir) / "成片" / "quality-reports" / "qr-1.json"
        report.parent.mkdir(parents=True)
        report.write_bytes(b"quality-report")
        artifact_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
        pipeline.completion_evidence = {
            "evidence_version": 1,
            "pipeline_state_sha256": "0" * 64,
            "artifact_relative_path": "成片/versions/v1/rendered.mp4",
            "artifact_sha256": artifact_sha,
            "quality_report_relative_path": "成片/quality-reports/qr-1.json",
            "quality_report_sha256": report_sha,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "approved_by": "Ian",
        }
        session.commit()

        class _Decision:
            is_complete = True
            reason_code = "approved_artifact_verified"
            message = "ok"

        monkeypatch.setattr(
            "app.pipeline.release_package_service.evaluate_pipeline_completion",
            lambda *_args, **_kwargs: _Decision(),
        )
        result = create_release_package(session, pipeline_id, project_root=root / "项目")
        manifest_path = Path(pipeline.project_dir) / result.release_manifest_relative_path
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        assert manifest["publication_metadata"]["title_options"][0]["title"] == "可发布标题"
        assert manifest["cover"]["status"] == "copy_only"
        assert manifest["fact_sources"] == ["https://example.test/fact"]
        assert manifest["materials"] == [
            {
                "shot_id": "shot-1",
                "candidate_id": "candidate-2",
                "source": "source-b",
                "url": "https://example.test/b",
                "rights": "commercial license with attribution",
                "evidence_kind": "licensed",
                "status": "locked",
                "reason": "",
            }
        ]
        assert manifest["provenance"]["approved_by"] == "Ian"


def test_release_manifest_blocks_locked_unknown_rights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _root(tmp_path)
    with _session() as session:
        pipeline_id = _pipeline(session, root)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        material_step.output_summary = {
            "material_tasks": [{"shot_id": "shot-1"}],
            "candidate_pool": {
                "shot-1": {
                    "shot_id": "shot-1",
                    "locked_candidate_id": "candidate-1",
                    "candidates": [
                        {
                            "candidate_id": "candidate-1",
                            "source": "source-a",
                            "url": "https://example.test/a",
                            "rights": "unknown rights",
                            "evidence_kind": "atmosphere",
                            "status": "locked",
                        },
                        {"candidate_id": "candidate-2", "source": "b", "rights": "licensed", "evidence_kind": "licensed"},
                        {"candidate_id": "candidate-3", "source": "c", "rights": "licensed", "evidence_kind": "licensed"},
                    ],
                }
            },
        }
        source = Path(pipeline.project_dir) / "成片" / "versions" / "v1" / "rendered.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(_valid_mp4())
        report = Path(pipeline.project_dir) / "成片" / "quality-reports" / "qr-1.json"
        report.parent.mkdir(parents=True)
        report.write_bytes(b"quality-report")
        pipeline.completion_evidence = {
            "evidence_version": 1,
            "pipeline_state_sha256": "0" * 64,
            "artifact_relative_path": "成片/versions/v1/rendered.mp4",
            "artifact_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "quality_report_relative_path": "成片/quality-reports/qr-1.json",
            "quality_report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "approved_by": "Ian",
        }
        session.commit()

        class _Decision:
            is_complete = True
            reason_code = "approved_artifact_verified"
            message = "ok"

        monkeypatch.setattr(
            "app.pipeline.release_package_service.evaluate_pipeline_completion",
            lambda *_args, **_kwargs: _Decision(),
        )
        with pytest.raises(ReleasePackageBlockedError, match="rights_unknown"):
            create_release_package(session, pipeline_id, project_root=root / "项目")


def test_release_metadata_requires_and_preserves_structured_per_use_clearance(
    tmp_path: Path,
):
    root = _root(tmp_path)
    with _session() as session:
        pipeline_id = _pipeline(session, root)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        evidence = build_stock_rights_evidence(
            provider="Pexels",
            provider_asset_id="101",
            media_type="photo",
            asset_page_url="https://www.pexels.com/photo/example-101/",
            contributor_name="Ada",
            search_query="office",
            observed_at="2026-07-18T09:30:00+00:00",
        )
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        candidate = {
            "candidate_id": "candidate-1",
            "source": "Pexels",
            "url": evidence["asset_page_url"],
            "rights": "Pexels License",
            "evidence_kind": "atmosphere",
            "status": "locked",
            "rights_evidence": evidence,
        }
        material_step.output_summary = {
            "material_tasks": [{"shot_id": "shot-1"}],
            "candidate_pool": {
                "shot-1": {
                    "shot_id": "shot-1",
                    "locked_candidate_id": "candidate-1",
                    "candidates": [candidate],
                }
            },
        }

        with pytest.raises(ReleasePackageBlockedError, match="rights_not_cleared"):
            _build_release_metadata(pipeline, {})

        candidate["rights_evidence"] = review_stock_rights_evidence(
            evidence,
            status="cleared_for_intended_use",
            intended_use="short-video product demo",
            identifiable_people="no",
            brands_or_logos="no",
            property_or_architecture="no",
            music_or_third_party_audio="no",
            third_party_clearance="not_required",
            restriction_notes=[],
            reviewed_by="Ian",
            reviewed_at="2026-07-18T10:00:00+00:00",
        )
        metadata = _build_release_metadata(pipeline, {})

        assert metadata["materials"][0]["rights_evidence"] == candidate[
            "rights_evidence"
        ]
