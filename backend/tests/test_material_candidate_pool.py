from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.materials.end_to_end_video import EndToEndVideoService
from app.materials.stock_rights_evidence import build_stock_rights_evidence
from app.models import HotItem, PipelineStep, ProductionPipeline
from app.models import ScriptAsset
from app.pipeline.material_candidate_contract import (
    CandidateContractError,
    CandidateLockedError,
)
from app.pipeline.editing_export_service import build_editing_export_package
from app.pipeline.material_candidate_gate import assert_pipeline_candidate_review_ready
from app.pipeline import material_package_service
from app.pipeline.material_package_service import (
    CandidatePoolConflictError,
    CandidatePoolCommitUnknownError,
    MaterialExportCommitUnknownError,
    _candidate_pool_context,
    _candidate_pool_state,
    _persist_candidate_pool,
    get_material_package,
    lock_material_candidate,
    rebuild_export_file,
    reject_material_candidate,
    review_material_candidate_rights,
    replace_material_candidate_pool,
    save_server_material_candidate_pool,
    save_material_candidate_pool,
    update_material_task_status,
)
from app.pipeline.material_candidate_contract import lock_candidate
from app.pipeline.service import build_project_from_hotspot


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _material_root(tmp_path: Path) -> Path:
    root = tmp_path / "D" / "material-root"
    (root / "项目").mkdir(parents=True, exist_ok=True)
    return root


def _hotspot(session: Session) -> HotItem:
    item = HotItem(
        source_platform="test",
        source_type="manual",
        title="候选素材合同测试",
        url="https://example.test/hotspot",
        published_at=datetime.now(timezone.utc),
        summary="测试候选池保存和锁定",
        tags=["test"],
        category="test",
        hot_score=90,
        topic_score=90,
        save_score=90,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def _jpeg_bytes(color: tuple[int, int, int] = (32, 96, 160)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (12, 8), color).save(output, format="JPEG")
    return output.getvalue()


def _candidate_rows(tmp_path: Path, *, count: int = 3) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(1, count + 1):
        preview = tmp_path / f"preview-{index}.jpg"
        preview.write_bytes(_jpeg_bytes((index * 40, 96, 160)))
        rows.append(
            {
                "candidate_id": f"cand-{index}",
                "source": "pexels" if index < 3 else "pixabay",
                "preview_path": str(preview),
                "url": f"https://example.test/assets/{index}",
                "rights": "commercial-use terms captured",
                "evidence_kind": "atmosphere",
                "status": "candidate",
                "reason": "matches the shot intent",
            }
        )
    return rows


def _trusted_stock_candidate_rows(tmp_path: Path) -> list[dict[str, Any]]:
    rows = _candidate_rows(tmp_path)
    provider_rows = (
        (
            "Pexels",
            "photo",
            "101",
            "https://www.pexels.com/photo/example-101/",
            "Ada",
        ),
        (
            "Pexels",
            "photo",
            "102",
            "https://www.pexels.com/photo/example-102/",
            "Ben",
        ),
        (
            "Pixabay",
            "photo",
            "201",
            "https://pixabay.com/photos/example-201/",
            "Lin",
        ),
    )
    for row, (provider, media_type, asset_id, page_url, author) in zip(
        rows,
        provider_rows,
        strict=True,
    ):
        evidence = build_stock_rights_evidence(
            provider=provider,
            provider_asset_id=asset_id,
            media_type=media_type,
            asset_page_url=page_url,
            contributor_name=author,
            search_query="office",
            observed_at="2026-07-18T09:30:00+00:00",
        )
        row.update(
            {
                "source": provider,
                "url": page_url,
                "rights": evidence["license_snapshot"]["license_name"],
                "rights_evidence": evidence,
            }
        )
    return rows


def _pipeline_with_material_tasks(session: Session, tmp_path: Path) -> int:
    hotspot = _hotspot(session)
    built = build_project_from_hotspot(
        session,
        hotspot.id,
        material_root=_material_root(tmp_path),
    )
    return built.pipeline_id


def _save_and_lock_all_candidate_pools(
    session: Session,
    pipeline_id: int,
    tmp_path: Path,
) -> None:
    package = get_material_package(session, pipeline_id)
    for task in package["material_tasks"]:
        shot_id = task["shot_id"]
        save_material_candidate_pool(
            session,
            pipeline_id,
            shot_id,
            _candidate_rows(tmp_path),
        )
        lock_material_candidate(
            session,
            pipeline_id,
            shot_id,
            "cand-1",
            reason="reviewed for downstream export",
        )


def test_candidate_pool_is_saved_in_step_payload_and_read_back_from_package(tmp_path):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        package = get_material_package(session, pipeline_id)
        shot_id = package["material_tasks"][0]["shot_id"]
        rows = _candidate_rows(tmp_path)
        rows[0]["media_validation"] = {
            "status": "verified",
            "sha256": "client-must-not-be-trusted",
        }
        rows[0]["rights_evidence"] = build_stock_rights_evidence(
            provider="Pexels",
            provider_asset_id="forged-client-claim",
            media_type="photo",
            asset_page_url="https://www.pexels.com/photo/forged-client-claim/",
            contributor_name="Untrusted Client",
            search_query="forged",
            observed_at="2026-07-18T09:30:00+00:00",
        )

        saved = save_material_candidate_pool(session, pipeline_id, shot_id, rows)
        session.expire_all()
        reread = get_material_package(session, pipeline_id)

        assert saved["shot_id"] == shot_id
        assert saved["revision"] == 0
        assert len(saved["candidates"]) == 3
        assert saved["candidates"][0]["media_validation"]["status"] == "verified"
        assert (
            saved["candidates"][0]["media_validation"]["sha256"]
            != "client-must-not-be-trusted"
        )
        assert saved["candidates"][0]["media_validation"]["decode_verified"] is True
        assert "rights_evidence" not in saved["candidates"][0]
        task = next(item for item in reread["material_tasks"] if item["shot_id"] == shot_id)
        assert task["candidate_pool"]["shot_id"] == shot_id
        assert task["candidate_pool"]["candidates"][0]["candidate_id"] == "cand-1"


def test_dependency_hash_mismatch_blocks_export_without_stale_marker(tmp_path):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        pipeline = session.get(ProductionPipeline, pipeline_id)
        script = session.get(ScriptAsset, pipeline.script_asset_id)
        script.voiceover = f"{script.voiceover}\n绕过正常入口的上游修改。"
        session.add(script)
        session.commit()
        session.refresh(pipeline)

        shot_ids = [
            item["shot_id"]
            for item in get_material_package(session, pipeline_id)["material_tasks"]
        ]
        with pytest.raises(CandidateContractError, match="脚本输入已经变化"):
            assert_pipeline_candidate_review_ready(pipeline, shot_ids)


@pytest.mark.parametrize(
    ("rowcount", "error_type"),
    [
        (0, CandidatePoolConflictError),
        (2, CandidatePoolCommitUnknownError),
    ],
)
def test_public_material_status_update_uses_whole_step_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rowcount: int,
    error_type: type[Exception],
):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        package = get_material_package(session, pipeline_id)
        shot_id = package["material_tasks"][0]["shot_id"]
        pipeline = session.get(ProductionPipeline, pipeline_id)
        _ = list(pipeline.steps)
        _ = pipeline.script_asset

        monkeypatch.setattr(
            session,
            "execute",
            lambda *_args, **_kwargs: SimpleNamespace(rowcount=rowcount),
        )
        with pytest.raises(error_type):
            update_material_task_status(
                session,
                pipeline_id,
                shot_id,
                "需要人工确认",
            )


def test_export_commit_unknown_is_not_misclassified_as_candidate_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        real_commit = session.commit

        def commit_then_lose_acknowledgement() -> None:
            real_commit()
            raise OSError("simulated export commit acknowledgement loss")

        monkeypatch.setattr(session, "commit", commit_then_lose_acknowledgement)

        with pytest.raises(MaterialExportCommitUnknownError, match="export"):
            rebuild_export_file(
                session,
                pipeline_id,
                "script_md",
                project_root=_material_root(tmp_path) / "项目",
            )


def test_stale_candidate_mutation_fails_closed_without_overwriting_new_lock(tmp_path):
    """Two review tabs must not let an old snapshot undo a newer lock."""

    database_path = tmp_path / "D" / "candidate-cas.sqlite3"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_a = factory()
    session_b = factory()
    try:
        pipeline_id = _pipeline_with_material_tasks(session_a, tmp_path)
        shot_id = get_material_package(session_a, pipeline_id)["material_tasks"][0][
            "shot_id"
        ]
        rows = _candidate_rows(tmp_path)
        save_material_candidate_pool(session_a, pipeline_id, shot_id, rows)

        # Tab B captures the old whole-step JSON before tab A makes a choice.
        _pipeline_b, step_b, stale_payload = _candidate_pool_context(
            session_b, pipeline_id, shot_id
        )
        stale_state = _candidate_pool_state(stale_payload, shot_id)
        assert stale_state is not None
        session_b.rollback()

        locked = lock_material_candidate(
            session_a,
            pipeline_id,
            shot_id,
            "cand-1",
            reason="tab A selected it",
        )
        assert locked["locked_candidate_id"] == "cand-1"

        stale_next = lock_candidate(stale_state, "cand-2", reason="stale tab")
        with pytest.raises(CandidatePoolConflictError):
            _persist_candidate_pool(
                session_b,
                step_b,
                stale_payload,
                stale_next,
                pipeline_id=pipeline_id,
            )

        session_b.expire_all()
        current = get_material_package(session_b, pipeline_id)
        current_pool = current["material_tasks"][0]["candidate_pool"]
        assert current_pool["locked_candidate_id"] == "cand-1"
        assert current_pool["revision"] == 1
    finally:
        session_a.close()
        session_b.close()
        engine.dispose()


def test_ambiguous_candidate_pool_rowcount_requires_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        shot_id = get_material_package(session, pipeline_id)["material_tasks"][0][
            "shot_id"
        ]
        save_material_candidate_pool(
            session,
            pipeline_id,
            shot_id,
            _candidate_rows(tmp_path),
        )
        _pipeline, step, payload = _candidate_pool_context(
            session,
            pipeline_id,
            shot_id,
        )
        state = _candidate_pool_state(payload, shot_id)
        assert state is not None
        next_state = lock_candidate(state, "cand-1", reason="ambiguous rowcount")
        real_execute = session.execute

        class AmbiguousResult:
            rowcount = None

        def execute_with_ambiguous_rowcount(*args: object, **kwargs: object):
            real_execute(*args, **kwargs)
            return AmbiguousResult()

        monkeypatch.setattr(session, "execute", execute_with_ambiguous_rowcount)

        with pytest.raises(CandidatePoolCommitUnknownError, match="reconcile"):
            _persist_candidate_pool(
                session,
                step,
                payload,
                next_state,
                pipeline_id=pipeline_id,
            )


def test_replace_lock_and_reject_persist_without_overwriting_lock(tmp_path):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        shot_id = get_material_package(session, pipeline_id)["material_tasks"][0]["shot_id"]
        rows = _candidate_rows(tmp_path)
        save_material_candidate_pool(session, pipeline_id, shot_id, rows)

        locked = lock_material_candidate(
            session,
            pipeline_id,
            shot_id,
            "cand-2",
            reason="Ian selected this preview",
        )
        rejected = reject_material_candidate(
            session,
            pipeline_id,
            shot_id,
            "cand-1",
            reason="wrong subject",
        )

        assert locked["locked_candidate_id"] == "cand-2"
        assert rejected["candidates"][0]["status"] == "rejected"

        changed = _candidate_rows(tmp_path)
        changed[1]["url"] = "https://example.test/changed"
        with pytest.raises(CandidateLockedError):
            replace_material_candidate_pool(session, pipeline_id, shot_id, changed)

        reread = get_material_package(session, pipeline_id)
        pool = next(item for item in reread["material_tasks"] if item["shot_id"] == shot_id)[
            "candidate_pool"
        ]
        assert pool["locked_candidate_id"] == "cand-2"
        assert pool["candidates"][1]["url"] == rows[1]["url"]


def test_initial_lock_appends_selection_history_without_fabricating_actor(tmp_path):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        package = get_material_package(session, pipeline_id)
        shot_id = package["material_tasks"][0]["shot_id"]
        save_material_candidate_pool(
            session,
            pipeline_id,
            shot_id,
            _candidate_rows(tmp_path),
        )

        locked = lock_material_candidate(
            session,
            pipeline_id,
            shot_id,
            "cand-1",
            reason="reviewed against the current script",
        )

        session.expire_all()
        pipeline = session.get(ProductionPipeline, pipeline_id)
        material_step = next(
            step for step in pipeline.steps if step.name == "生成素材任务"
        )
        events = material_step.output_summary["candidate_selection_history"][shot_id]
        event = events[-1]

        assert event["event"] == "candidate_locked"
        assert event["previous_candidate_id"] is None
        assert event["selected_candidate_id"] == "cand-1"
        assert event["revision_from"] == locked["revision"] - 1
        assert event["revision_to"] == locked["revision"]
        assert event["reason"] == "reviewed against the current script"
        assert "actor" not in event


def test_explicit_reselection_persists_audit_history_and_rejects_stale_revision(
    tmp_path,
):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        shot_id = get_material_package(session, pipeline_id)["material_tasks"][0]["shot_id"]
        save_material_candidate_pool(
            session,
            pipeline_id,
            shot_id,
            _candidate_rows(tmp_path),
        )
        locked = lock_material_candidate(
            session,
            pipeline_id,
            shot_id,
            "cand-1",
            reason="first approved framing",
        )

        changed = material_package_service.reselect_material_candidate(
            session,
            pipeline_id,
            shot_id,
            "cand-2",
            expected_revision=locked["revision"],
            reason="second candidate supports the narration better",
            actor="Ian",
        )

        assert changed["locked_candidate_id"] == "cand-2"
        assert changed["revision"] == locked["revision"] + 1
        assert changed["selection_event"]["previous_candidate_id"] == "cand-1"
        assert changed["selection_event"]["selected_candidate_id"] == "cand-2"
        session.expire_all()
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        material_step = next(step for step in pipeline.steps if step.name == "生成素材任务")
        history = material_step.output_summary["candidate_selection_history"][shot_id]
        assert history[-1]["reason"] == "second candidate supports the narration better"

        with pytest.raises(CandidatePoolConflictError, match="revision"):
            material_package_service.reselect_material_candidate(
                session,
                pipeline_id,
                shot_id,
                "cand-3",
                expected_revision=locked["revision"],
                reason="stale browser action",
                actor="Ian",
            )


def test_candidate_pool_routes_save_lock_and_reject(tmp_path):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        shot_id = get_material_package(session, pipeline_id)["material_tasks"][0]["shot_id"]

        def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        try:
            client = TestClient(app)
            save_response = client.put(
                f"/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidate-pool",
                json={"candidates": _candidate_rows(tmp_path)},
            )
            assert save_response.status_code == 200
            blocked_export = client.post(
                f"/api/pipeline/{pipeline_id}/editing-export/rebuild"
            )
            assert blocked_export.status_code == 409
            assert "锁定" in blocked_export.json()["detail"]
            lock_response = client.post(
                f"/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidate-pool/cand-1/lock",
                json={"reason": "selected in review"},
            )
            assert lock_response.status_code == 200
            reject_response = client.post(
                f"/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidate-pool/cand-2/reject",
                json={"reason": "not suitable"},
            )
            assert reject_response.status_code == 200
            assert reject_response.json()["locked_candidate_id"] == "cand-1"
        finally:
            app.dependency_overrides.clear()


def test_rights_review_route_uses_persisted_evidence_and_server_audit_identity(
    tmp_path: Path,
):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        shot_id = get_material_package(session, pipeline_id)["material_tasks"][0][
            "shot_id"
        ]
        save_server_material_candidate_pool(
            session,
            pipeline_id,
            shot_id,
            _trusted_stock_candidate_rows(tmp_path),
        )

        def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        try:
            client = TestClient(app)
            response = client.post(
                f"/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidate-pool/cand-1/rights-review",
                json={
                    "expected_revision": 0,
                    "status": "cleared_for_intended_use",
                    "intended_use": "short-video product demo",
                    "identifiable_people": "no",
                    "brands_or_logos": "no",
                    "property_or_architecture": "no",
                    "music_or_third_party_audio": "no",
                    "third_party_clearance": "not_required",
                    "restriction_notes": ["Only for this approved edit."],
                },
            )
            assert response.status_code == 200, response.text
            pool = response.json()
            public_candidate = pool["candidates"][0]
            reviewed = public_candidate["rights_evidence"]["rights_review"]
            assert pool["revision"] == 1
            assert "preview_path" not in public_candidate
            assert "download_url" not in repr(public_candidate["rights_evidence"])
            assert "raw_api" not in repr(public_candidate["rights_evidence"])
            assert reviewed["status"] == "cleared_for_intended_use"
            assert reviewed["reviewed_by"] == "Ian"
            assert datetime.fromisoformat(reviewed["reviewed_at"]).tzinfo is not None
            assert pool["rights_review_event"]["status_from"] == "unreviewed"
            assert pool["rights_review_event"]["status_to"] == "cleared_for_intended_use"
            session.expire_all()
            pipeline = session.get(ProductionPipeline, pipeline_id)
            assert pipeline is not None
            material_step = next(
                step for step in pipeline.steps if step.name == "生成素材任务"
            )
            history = material_step.output_summary[
                "candidate_rights_review_history"
            ][shot_id]
            assert history[-1]["candidate_id"] == "cand-1"

            stale = client.post(
                f"/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidate-pool/cand-1/rights-review",
                json={
                    "expected_revision": 0,
                    "status": "needs_review",
                    "intended_use": "short-video product demo",
                    "identifiable_people": "unknown",
                    "brands_or_logos": "unknown",
                    "property_or_architecture": "unknown",
                    "music_or_third_party_audio": "unknown",
                    "third_party_clearance": "unknown",
                    "restriction_notes": [],
                },
            )
            assert stale.status_code == 409
        finally:
            app.dependency_overrides.clear()


def test_candidate_pool_reselect_route_requires_current_revision(tmp_path):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        shot_id = get_material_package(session, pipeline_id)["material_tasks"][0]["shot_id"]
        save_material_candidate_pool(
            session,
            pipeline_id,
            shot_id,
            _candidate_rows(tmp_path),
        )
        locked = lock_material_candidate(
            session,
            pipeline_id,
            shot_id,
            "cand-1",
            reason="initial approved candidate",
        )

        def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        try:
            client = TestClient(app)
            response = client.post(
                f"/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidate-pool/cand-2/reselect",
                json={
                    "reason": "better evidence for this shot",
                    "expected_revision": locked["revision"],
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["locked_candidate_id"] == "cand-2"
            stale = client.post(
                f"/api/pipeline/{pipeline_id}/material-tasks/{shot_id}/candidate-pool/cand-3/reselect",
                json={
                    "reason": "stale tab",
                    "expected_revision": locked["revision"],
                },
            )
            assert stale.status_code == 409
        finally:
            app.dependency_overrides.clear()


def test_unreviewed_candidate_pool_cannot_bypass_through_editing_export(tmp_path):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        shot_id = get_material_package(session, pipeline_id)["material_tasks"][0][
            "shot_id"
        ]
        save_material_candidate_pool(
            session,
            pipeline_id,
            shot_id,
            _candidate_rows(tmp_path),
        )

        with pytest.raises(ValueError, match="候选|锁定"):
            build_editing_export_package(
                session,
                pipeline_id,
                project_root=_material_root(tmp_path) / "项目",
            )


def test_unreviewed_candidate_pool_cannot_create_finished_export_files(tmp_path):
    output_root = tmp_path / "finished-output"
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        shot_id = get_material_package(session, pipeline_id)["material_tasks"][0][
            "shot_id"
        ]
        save_material_candidate_pool(
            session,
            pipeline_id,
            shot_id,
            _candidate_rows(tmp_path),
        )
        service = EndToEndVideoService(
            output_root=output_root,
            material_root=tmp_path / "materials",
            enable_real_downloads=False,
            enable_real_render=False,
        )

        with pytest.raises(ValueError, match="候选|锁定"):
            service.run_pipeline_export(session, pipeline_id)

    assert not output_root.exists()


def test_all_locked_candidate_pools_allow_editing_and_finished_exports(tmp_path):
    output_root = tmp_path / "finished-output"
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        _save_and_lock_all_candidate_pools(session, pipeline_id, tmp_path)

        editing_package = build_editing_export_package(
            session,
            pipeline_id,
            project_root=_material_root(tmp_path) / "项目",
        )
        finished = EndToEndVideoService(
            output_root=output_root,
            material_root=tmp_path / "materials",
            enable_real_downloads=False,
            enable_real_render=False,
        ).run_pipeline_export(session, pipeline_id)

    assert Path(editing_package.generated_files["timeline_json"]).is_file()
    assert Path(finished["导出文件"]["报告 JSON"]).is_file()


def test_locked_stock_candidate_requires_per_use_clearance_before_export(
    tmp_path: Path,
):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        shot_id = get_material_package(session, pipeline_id)["material_tasks"][0][
            "shot_id"
        ]
        save_server_material_candidate_pool(
            session,
            pipeline_id,
            shot_id,
            _trusted_stock_candidate_rows(tmp_path),
        )
        locked = lock_material_candidate(
            session,
            pipeline_id,
            shot_id,
            "cand-1",
            reason="best framing",
        )
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None

        with pytest.raises(CandidateContractError, match="清权|rights"):
            assert_pipeline_candidate_review_ready(pipeline, [shot_id])

        review_material_candidate_rights(
            session,
            pipeline_id,
            shot_id,
            "cand-1",
            expected_revision=locked["revision"],
            status="cleared_for_intended_use",
            intended_use="short-video product demo",
            identifiable_people="no",
            brands_or_logos="no",
            property_or_architecture="no",
            music_or_third_party_audio="no",
            third_party_clearance="not_required",
            restriction_notes=[],
        )
        session.expire_all()
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        states = assert_pipeline_candidate_review_ready(pipeline, [shot_id])

        assert states[shot_id].locked_candidate_id == "cand-1"


def test_locked_candidate_preview_is_revalidated_before_export(tmp_path):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        _save_and_lock_all_candidate_pools(session, pipeline_id, tmp_path)
        (tmp_path / "preview-1.jpg").unlink()

        with pytest.raises(CandidateContractError, match="preview"):
            build_editing_export_package(
                session,
                pipeline_id,
                project_root=_material_root(tmp_path) / "项目",
            )


def test_candidate_pool_rejects_html_disguised_as_jpeg(tmp_path):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        shot_id = get_material_package(session, pipeline_id)["material_tasks"][0][
            "shot_id"
        ]
        rows = _candidate_rows(tmp_path)
        Path(rows[0]["preview_path"]).write_bytes(b"<html>not a jpeg</html>")

        with pytest.raises(CandidateContractError, match="媒体|文件内容"):
            save_material_candidate_pool(session, pipeline_id, shot_id, rows)

        package = get_material_package(session, pipeline_id)
        assert "candidate_pool" not in package["material_tasks"][0]


def test_locked_candidate_content_tamper_is_rejected_before_export(tmp_path):
    with _session() as session:
        pipeline_id = _pipeline_with_material_tasks(session, tmp_path)
        _save_and_lock_all_candidate_pools(session, pipeline_id, tmp_path)
        (tmp_path / "preview-1.jpg").write_bytes(b"<html>tampered</html>")

        with pytest.raises(CandidateContractError, match="媒体|文件内容"):
            build_editing_export_package(
                session,
                pipeline_id,
                project_root=_material_root(tmp_path) / "项目",
            )
