from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.core.database import get_db
from app.main import app
from app.models import HotItem, ProductionPipeline
from app.pipeline.completion_policy import evaluate_pipeline_completion
from app.pipeline.quality_contract_service import ensure_default_quality_contract
from app.pipeline.service import build_project_from_hotspot
from app.pipeline.quality_report_service import (
    QualityApprovalCommitUnknownError,
    QualityReportError,
    approve_quality_report,
    list_quality_artifact_candidates,
    write_quality_report,
)
from app.video.technical_probe import TechnicalCheck, VideoTechnicalReport


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
        title="quality report",
        url="https://example.test/quality-report",
        published_at=datetime.now(timezone.utc),
        summary="quality report",
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


def _fake_probe(path: Path) -> VideoTechnicalReport:
    return VideoTechnicalReport(
        path=path,
        is_valid=True,
        checks=(TechnicalCheck("decode", True, True, "ok"),),
        width=1080,
        height=1920,
        fps=30.0,
        duration_seconds=1.0,
        video_codec="h264",
        pixel_format="yuv420p",
        audio_streams=1,
        subtitle_streams=1,
        probe_source="D:/tools/ffprobe.exe",
    )


def _manual_gates() -> dict[str, dict[str, object]]:
    return {
        name: {"status": "passed", "evidence": [f"Ian reviewed {name}"]}
        for name in ("visual_narration", "facts_rights", "quality_contract")
    }


def _quality_contract_review(
    session: Session,
    pipeline_id: int,
    project_root: Path,
) -> dict[str, object]:
    snapshot = ensure_default_quality_contract(
        session,
        pipeline_id,
        project_root=project_root,
    )
    return {
        "contract_sha256": snapshot.sha256,
        "must_preserve_results": {
            item["id"]: {
                "result": "passed",
                "evidence": f"Ian reviewed {item['criterion']}",
            }
            for item in snapshot.contract["must_preserve"]
        },
        "allowed_difference_results": {
            item["id"]: {
                "result": (
                    "used"
                    if index == 0
                    else "not_used"
                ),
                "actual_difference": (
                    "旁白实测时长驱动镜头定时" if index == 0 else ""
                ),
                "evidence": "Ian reviewed the allowed difference",
            }
            for index, item in enumerate(snapshot.contract["allowed_differences"])
        },
        "planned_exceed_results": {
            item["id"]: {
                "result": "exceeded",
                "difference": f"Exceeded the R1 baseline for {item['label']}",
                "evidence": "Ian compared the finished video item by item",
            }
            for item in snapshot.contract["planned_exceed_dimensions"]
        },
    }


def _timeline(version_id: str = "ts-current", input_hash: str = "b" * 64):
    return SimpleNamespace(
        version_id=version_id,
        input_hash=input_hash,
        segments=(1,),
        artifacts={"voiceover": {}, "subtitles": {}},
        settings={"width": 1080, "height": 1920, "fps": 30},
    )


def _write_render_lineage(
    output: Path,
    artifact: Path,
    *,
    version_id: str = "ts-current",
    input_hash: str = "b" * 64,
) -> None:
    visual = output / "visual-root" / version_id / "render-manifest.json"
    visual.parent.mkdir(parents=True, exist_ok=True)
    visual.write_text(
        json.dumps({"timeline_version_id": version_id}) + "\n",
        encoding="utf-8",
    )
    (output / "technical-report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "timeline_version_id": version_id,
                "timeline_input_hash": input_hash,
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "visual_render_manifest_sha256": hashlib.sha256(
                    visual.read_bytes()
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )


def test_quality_candidates_mark_historical_versions_and_only_allow_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _root(tmp_path)
    with _session() as session:
        pipeline_id = _pipeline(session, root)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        versions = Path(pipeline.project_dir) / "成片" / "versions"
        current_dir = versions / "current-run"
        historical_dir = versions / "historical-run"
        for output in (current_dir, historical_dir):
            output.mkdir(parents=True)
            (output / "rendered.mp4").write_bytes(_valid_mp4())
        _write_render_lineage(
            current_dir,
            current_dir / "rendered.mp4",
            version_id="ts-current",
            input_hash="b" * 64,
        )
        _write_render_lineage(
            historical_dir,
            historical_dir / "rendered.mp4",
            version_id="ts-old",
            input_hash="a" * 64,
        )
        monkeypatch.setattr(
            "app.pipeline.quality_report_service._latest_timeline",
            lambda *_args, **_kwargs: _timeline(),
        )

        candidates = list_quality_artifact_candidates(
            session,
            pipeline_id,
            project_root=root / "项目",
        )
        by_path = {item.artifact_relative_path: item for item in candidates}

        current = by_path["成片/versions/current-run/rendered.mp4"]
        historical = by_path["成片/versions/historical-run/rendered.mp4"]
        assert current.timeline_version_id == "ts-current"
        assert current.timeline_status == "current"
        assert current.can_quality_check is True
        assert historical.timeline_version_id == "ts-old"
        assert historical.timeline_status == "historical"
        assert historical.can_quality_check is False


def test_writer_is_immutable_and_defaults_manual_gates_to_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _root(tmp_path)
    with _session() as session:
        pipeline_id = _pipeline(session, root)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        output = Path(pipeline.project_dir) / "成片" / "versions" / "v1"
        output.mkdir(parents=True)
        artifact = output / "rendered.mp4"
        artifact.write_bytes(_valid_mp4())
        _write_render_lineage(output, artifact)
        monkeypatch.setattr(
            "app.pipeline.quality_report_service._latest_timeline",
            lambda *_args, **_kwargs: _timeline(),
        )
        monkeypatch.setattr(
            "app.pipeline.quality_report_service._probe_artifact",
            lambda path, *_args, **_kwargs: _fake_probe(path),
        )
        result = write_quality_report(
            session,
            pipeline_id,
            "成片/versions/v1/rendered.mp4",
            project_root=root / "项目",
        )
        assert result.report["report_format"] == "auto-video-engine-quality-report-v1"
        assert result.report["release_blockers"] == [
            "visual_narration",
            "facts_rights",
            "quality_contract",
        ]
        assert str(pipeline.project_dir) not in result.to_dict().__repr__()
        assert result.report_sha256 == hashlib.sha256(
            (Path(pipeline.project_dir) / result.report_relative_path).read_bytes()
        ).hexdigest()


def test_manual_quality_checkbox_cannot_replace_structured_contract_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _root(tmp_path)
    with _session() as session:
        pipeline_id = _pipeline(session, root)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        output = Path(pipeline.project_dir) / "成片" / "versions" / "v1"
        output.mkdir(parents=True)
        artifact = output / "rendered.mp4"
        artifact.write_bytes(_valid_mp4())
        _write_render_lineage(output, artifact)
        monkeypatch.setattr(
            "app.pipeline.quality_report_service._latest_timeline",
            lambda *_args, **_kwargs: _timeline(),
        )
        monkeypatch.setattr(
            "app.pipeline.quality_report_service._probe_artifact",
            lambda path, *_args, **_kwargs: _fake_probe(path),
        )

        checkbox_only = write_quality_report(
            session,
            pipeline_id,
            "成片/versions/v1/rendered.mp4",
            manual_gates=_manual_gates(),
            project_root=root / "项目",
        )
        assert checkbox_only.report["gates"]["quality_contract"] == {
            "status": "failed",
            "evidence": ["quality_contract_review_required"],
        }
        assert "quality_contract" in checkbox_only.report["release_blockers"]

        structured = write_quality_report(
            session,
            pipeline_id,
            "成片/versions/v1/rendered.mp4",
            manual_gates=_manual_gates(),
            quality_contract_review=_quality_contract_review(
                session,
                pipeline_id,
                root / "项目",
            ),
            project_root=root / "项目",
        )
        gate = structured.report["gates"]["quality_contract"]
        assert gate["status"] == "passed"
        assert any(
            item.startswith("allowed_difference_review:")
            and '"result":"used"' in item
            for item in gate["evidence"]
        )
        assert any(
            item.startswith("planned_exceed_review:")
            and '"result":"exceeded"' in item
            for item in gate["evidence"]
        )


def test_writer_rejects_absolute_or_c_drive_paths(tmp_path: Path):
    root = _root(tmp_path)
    with _session() as session:
        pipeline_id = _pipeline(session, root)
        with pytest.raises(QualityReportError, match="project-relative"):
            write_quality_report(
                session,
                pipeline_id,
                "C:/secret/rendered.mp4",
                project_root=root / "项目",
            )


def test_writer_rejects_artifact_from_previous_timeline_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _root(tmp_path)
    with _session() as session:
        pipeline_id = _pipeline(session, root)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        output = Path(pipeline.project_dir) / "成片" / "versions" / "v1"
        visual = output / "visual-root" / "ts-old" / "render-manifest.json"
        visual.parent.mkdir(parents=True)
        visual.write_text('{"timeline_version_id":"ts-old"}\n', encoding="utf-8")
        artifact = output / "rendered.mp4"
        artifact.write_bytes(_valid_mp4())
        (output / "technical-report.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "timeline_version_id": "ts-old",
                    "timeline_input_hash": "a" * 64,
                    "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "visual_render_manifest_sha256": hashlib.sha256(
                        visual.read_bytes()
                    ).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "app.pipeline.quality_report_service._latest_timeline",
            lambda *_args, **_kwargs: SimpleNamespace(
                version_id="ts-current",
                input_hash="b" * 64,
                segments=(1,),
                artifacts={"voiceover": {}, "subtitles": {}},
                settings={"width": 1080, "height": 1920, "fps": 30},
            ),
        )
        monkeypatch.setattr(
            "app.pipeline.quality_report_service._probe_artifact",
            lambda path, *_args, **_kwargs: _fake_probe(path),
        )

        with pytest.raises(QualityReportError, match="artifact_timeline_mismatch"):
            write_quality_report(
                session,
                pipeline_id,
                "成片/versions/v1/rendered.mp4",
                project_root=root / "项目",
            )


def test_approval_requires_Ian_and_writes_completion_evidence_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _root(tmp_path)
    with _session() as session:
        pipeline_id = _pipeline(session, root)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        output = Path(pipeline.project_dir) / "成片" / "versions" / "v1"
        output.mkdir(parents=True)
        artifact = output / "rendered.mp4"
        artifact.write_bytes(_valid_mp4())
        _write_render_lineage(output, artifact)
        monkeypatch.setattr(
            "app.pipeline.quality_report_service._latest_timeline",
            lambda *_args, **_kwargs: _timeline(),
        )
        monkeypatch.setattr(
            "app.pipeline.quality_report_service._probe_artifact",
            lambda path, *_args, **_kwargs: _fake_probe(path),
        )
        report = write_quality_report(
            session,
            pipeline_id,
            "成片/versions/v1/rendered.mp4",
            manual_gates=_manual_gates(),
            quality_contract_review=_quality_contract_review(
                session,
                pipeline_id,
                root / "项目",
            ),
            project_root=root / "项目",
        )
        with pytest.raises(QualityReportError, match="Ian"):
            approve_quality_report(
                session,
                pipeline_id,
                artifact_relative_path=report.artifact_relative_path,
                quality_report_relative_path=report.report_relative_path,
                approved_by="Someone else",
                project_root=root / "项目",
            )

        class _Decision:
            is_complete = True
            status = "complete"
            reason_code = "approved_artifact_verified"
            message = "ok"
            next_action = ""

        monkeypatch.setattr(
            "app.pipeline.quality_report_service.evaluate_pipeline_completion",
            lambda *_args, **_kwargs: _Decision(),
        )
        approved = approve_quality_report(
            session,
            pipeline_id,
            artifact_relative_path=report.artifact_relative_path,
            quality_report_relative_path=report.report_relative_path,
            approved_by="Ian",
            project_root=root / "项目",
        )
        session.expire_all()
        pipeline = session.get(type(pipeline), pipeline_id)
        assert pipeline is not None
        assert pipeline.completion_evidence["evidence_version"] == 2
        assert pipeline.completion_evidence["approved_by"] == "Ian"
        assert approved.evidence["artifact_relative_path"] == report.artifact_relative_path


def test_approval_execute_acknowledgement_unknown_is_not_a_retryable_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _root(tmp_path)
    with _session() as session:
        pipeline_id = _pipeline(session, root)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        output = Path(pipeline.project_dir) / "成片" / "versions" / "v1"
        output.mkdir(parents=True)
        artifact = output / "rendered.mp4"
        artifact.write_bytes(_valid_mp4())
        _write_render_lineage(output, artifact)
        monkeypatch.setattr(
            "app.pipeline.quality_report_service._latest_timeline",
            lambda *_args, **_kwargs: _timeline(),
        )
        monkeypatch.setattr(
            "app.pipeline.quality_report_service._probe_artifact",
            lambda path, *_args, **_kwargs: _fake_probe(path),
        )
        report = write_quality_report(
            session,
            pipeline_id,
            "成片/versions/v1/rendered.mp4",
            manual_gates=_manual_gates(),
            quality_contract_review=_quality_contract_review(
                session,
                pipeline_id,
                root / "项目",
            ),
            project_root=root / "项目",
        )

        class _Decision:
            is_complete = True
            status = "complete"
            reason_code = "approved_artifact_verified"
            message = "ok"
            next_action = ""

        monkeypatch.setattr(
            "app.pipeline.quality_report_service.evaluate_pipeline_completion",
            lambda *_args, **_kwargs: _Decision(),
        )

        def fail_execute(*_args, **_kwargs):
            raise OSError("simulated lost database acknowledgement")

        monkeypatch.setattr(session, "execute", fail_execute)
        with pytest.raises(QualityApprovalCommitUnknownError, match="reconcile"):
            approve_quality_report(
                session,
                pipeline_id,
                artifact_relative_path=report.artifact_relative_path,
                quality_report_relative_path=report.report_relative_path,
                approved_by="Ian",
                project_root=root / "项目",
            )


def test_real_v2_approval_is_revoked_if_frozen_contract_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = _root(tmp_path)
    with _session() as session:
        pipeline_id = _pipeline(session, root)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        output = Path(pipeline.project_dir) / "成片" / "versions" / "v1"
        output.mkdir(parents=True)
        artifact = output / "rendered.mp4"
        artifact.write_bytes(_valid_mp4())
        _write_render_lineage(output, artifact)
        monkeypatch.setattr(
            "app.pipeline.quality_report_service._latest_timeline",
            lambda *_args, **_kwargs: _timeline(),
        )
        monkeypatch.setattr(
            "app.pipeline.quality_report_service._probe_artifact",
            lambda path, *_args, **_kwargs: _fake_probe(path),
        )
        report = write_quality_report(
            session,
            pipeline_id,
            "成片/versions/v1/rendered.mp4",
            manual_gates=_manual_gates(),
            quality_contract_review=_quality_contract_review(
                session,
                pipeline_id,
                root / "项目",
            ),
            project_root=root / "项目",
        )

        approved = approve_quality_report(
            session,
            pipeline_id,
            artifact_relative_path=report.artifact_relative_path,
            quality_report_relative_path=report.report_relative_path,
            approved_by="Ian",
            project_root=root / "项目",
        )
        assert approved.decision["is_complete"] is True
        session.expire_all()
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        contract_path = Path(pipeline.project_dir) / "质量合同" / "quality-contract-v1.json"
        contract_path.write_text('{"tampered":true}\n', encoding="utf-8")

        decision = evaluate_pipeline_completion(pipeline)
        assert decision.is_complete is False
        assert decision.reason_code == "quality_contract_hash_mismatch"


def test_quality_report_route_returns_only_project_relative_data(
    monkeypatch: pytest.MonkeyPatch,
):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        with Session(engine) as db:
            yield db

    class _FakeReport:
        def to_dict(self):
            return {
                "quality_report_relative_path": "成片/quality-reports/qr-test.json",
                "artifact_relative_path": "成片/versions/v1/rendered.mp4",
                "publish_supported": False,
            }

    monkeypatch.setattr(
        "app.main.write_quality_report",
        lambda *_args, **_kwargs: _FakeReport(),
    )
    app.dependency_overrides[get_db] = override_get_db
    try:
        response = TestClient(app).post(
            "/api/pipeline/1/quality-report",
            json={"artifact_relative_path": "成片/versions/v1/rendered.mp4"},
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert "C:/" not in response.text
    assert response.json()["publish_supported"] is False


def test_quality_report_request_rejects_unknown_body_fields():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = TestClient(app).post(
            "/api/pipeline/1/quality-report",
            json={
                "artifact_relative_path": "成片/versions/v1/rendered.mp4",
                "unexpected": "oversized caller data",
            },
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 422


def test_quality_contract_route_returns_only_project_relative_snapshot(
    monkeypatch: pytest.MonkeyPatch,
):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        with Session(engine) as db:
            yield db

    class _Snapshot:
        def to_dict(self):
            return {
                "pipeline_id": 1,
                "quality_contract_relative_path": "质量合同/quality-contract-v1.json",
                "quality_contract_sha256": "a" * 64,
                "contract": {
                    "target": {
                        "platform": "抖音",
                        "aspect_ratio": "9:16",
                        "duration_seconds": 60,
                    }
                },
            }

    monkeypatch.setattr(
        "app.main.ensure_default_quality_contract",
        lambda *_args, **_kwargs: _Snapshot(),
    )
    app.dependency_overrides[get_db] = override_get_db
    try:
        response = TestClient(app).get("/api/pipeline/1/quality-contract")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["quality_contract_sha256"] == "a" * 64
    assert "C:/" not in response.text


def test_quality_report_route_forwards_structured_contract_review_and_rejects_checkbox(
    monkeypatch: pytest.MonkeyPatch,
):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    captured: dict[str, object] = {}

    def override_get_db():
        with Session(engine) as db:
            yield db

    class _FakeReport:
        def to_dict(self):
            return {"pipeline_id": 1, "publish_supported": False}

    def fake_writer(*_args, **kwargs):
        captured.update(kwargs)
        return _FakeReport()

    monkeypatch.setattr("app.main.write_quality_report", fake_writer)
    review = {
        "contract_sha256": "b" * 64,
        "must_preserve_results": {
            "facts_traceable": {"result": "passed", "evidence": "已核对"}
        },
        "allowed_difference_results": {
            "audio_first_timing": {
                "result": "not_used",
                "actual_difference": "",
                "evidence": "未使用",
            }
        },
        "planned_exceed_results": {
            "information_clarity": {
                "result": "exceeded",
                "difference": "信息层级更清楚",
                "evidence": "已逐项比较",
            }
        },
    }
    app.dependency_overrides[get_db] = override_get_db
    try:
        response = TestClient(app).post(
            "/api/pipeline/1/quality-report",
            json={
                "artifact_relative_path": "成片/versions/v1/rendered.mp4",
                "gates": {
                    "visual_narration": {
                        "status": "passed",
                        "evidence": ["已播放核对"],
                    }
                },
                "quality_contract_review": review,
            },
        )
        checkbox = TestClient(app).post(
            "/api/pipeline/1/quality-report",
            json={
                "artifact_relative_path": "成片/versions/v1/rendered.mp4",
                "gates": {
                    "quality_contract": {
                        "status": "passed",
                        "evidence": ["只勾选了旧复选框"],
                    }
                },
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert captured["quality_contract_review"] == review
    assert checkbox.status_code == 422
