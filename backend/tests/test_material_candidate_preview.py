from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app
from app.models import HotItem, ProductionPipeline
from app.pipeline.material_candidate_preview_service import replace_candidate_with_upload
from app.pipeline.material_package_service import (
    CandidatePoolCommitUnknownError,
    get_material_package,
    lock_material_candidate,
    save_material_candidate_pool,
)
from app.pipeline.service import build_project_from_hotspot


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _build_pipeline(session: Session, root: Path) -> int:
    # The production identity contract deliberately refuses to create a new
    # project root implicitly.  Tests must provision the approved D: root just
    # as the launcher does.
    (root / "项目").mkdir(parents=True, exist_ok=True)
    hotspot = HotItem(
        source_platform="test",
        source_type="manual",
        title="candidate preview test",
        url="https://example.test/topic",
        published_at=datetime.now(timezone.utc),
        summary="preview",
        tags=["test"],
        category="test",
        hot_score=90,
        topic_score=90,
        save_score=90,
    )
    session.add(hotspot)
    session.commit()
    session.refresh(hotspot)
    built = build_project_from_hotspot(
        session,
        hotspot.id,
        material_root=root,
    )
    return built.pipeline_id


def _jpeg_bytes(color: tuple[int, int, int] = (32, 96, 160)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (12, 8), color).save(output, format="JPEG")
    return output.getvalue()


def _rows(project_dir: Path, *, prefix: str = "cand") -> list[dict[str, str]]:
    preview_dir = project_dir / "素材信息" / "候选预览"
    preview_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    for index, source in enumerate(("pexels", "pixabay", "official"), start=1):
        path = preview_dir / f"{prefix}-{index}.jpg"
        path.write_bytes(_jpeg_bytes((index * 40, 96, 160)))
        rows.append(
            {
                "candidate_id": f"{prefix}-{index}",
                "source": source,
                "preview_path": str(path),
                "url": f"https://example.test/assets/{index}",
                "rights": "commercial-use terms captured",
                "evidence_kind": "atmosphere",
                "status": "candidate",
                "reason": "matches the shot intent",
            }
        )
    return rows


def test_preview_route_returns_media_without_exposing_physical_path(tmp_path: Path):
    with _session() as session:
        pipeline_id = _build_pipeline(session, tmp_path / "D" / "material-root")
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        task = get_material_package(session, pipeline_id)["material_tasks"][0]
        rows = _rows(Path(pipeline.project_dir))
        save_material_candidate_pool(session, pipeline_id, task["shot_id"], rows)

        package = get_material_package(session, pipeline_id)
        candidate = package["material_tasks"][0]["candidate_pool"]["candidates"][0]
        assert candidate["preview_url"].startswith("/api/pipeline/")
        assert candidate["preview_media_type"] == "image/jpeg"
        assert "preview_path" not in candidate
        assert str(pipeline.project_dir) not in candidate["preview_url"]

        def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        try:
            response = TestClient(app).get(candidate["preview_url"])
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/jpeg")
        assert "inline" in response.headers["content-disposition"]
        assert response.content == _jpeg_bytes((40, 96, 160))
        assert candidate["media_validation"]["status"] == "verified"
        assert candidate["media_validation"]["decode_verified"] is True


def test_replace_route_uploads_to_project_and_survives_refresh(tmp_path: Path):
    with _session() as session:
        pipeline_id = _build_pipeline(session, tmp_path / "D" / "material-root")
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        task = get_material_package(session, pipeline_id)["material_tasks"][0]
        rows = _rows(Path(pipeline.project_dir))
        save_material_candidate_pool(session, pipeline_id, task["shot_id"], rows)

        def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        try:
            client = TestClient(app)
            replacement_bytes = _jpeg_bytes((180, 20, 60))
            response = client.post(
                f"/api/pipeline/{pipeline_id}/material-tasks/{task['shot_id']}/candidate-pool/cand-1/replace",
                files={"preview": ("replacement.jpg", replacement_bytes, "image/jpeg")},
                data={
                    "replacement_candidate_id": "cand-new",
                    "source": "uploaded",
                    "url": "https://example.test/replacement",
                    "rights": "Ian-owned review asset",
                    "evidence_kind": "self_owned",
                    "reason": "original framing was wrong",
                },
            )
            assert response.status_code == 200, response.text
            response_pool = response.json()
            replacement_response = next(
                item
                for item in response_pool["candidates"]
                if item["candidate_id"] == "cand-new"
            )
            assert replacement_response["preview_url"].endswith("/cand-new/preview")
            assert replacement_response["preview_media_type"] == "image/jpeg"
            package = get_material_package(session, pipeline_id)
            pool = package["material_tasks"][0]["candidate_pool"]
            replacement = next(
                item for item in pool["candidates"] if item["candidate_id"] == "cand-new"
            )
            assert pool["revision"] == 1
            assert replacement["preview_url"].endswith("/cand-new/preview")
            preview_response = client.get(replacement["preview_url"])
            assert preview_response.status_code == 200
            assert preview_response.content == replacement_bytes
            assert replacement["media_validation"]["status"] == "verified"
        finally:
            app.dependency_overrides.clear()


def test_replace_upload_preserves_file_when_commit_acknowledgement_is_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A commit that may have succeeded must never leave a dangling DB path."""

    with _session() as session:
        pipeline_id = _build_pipeline(session, tmp_path / "D" / "material-root")
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        task = get_material_package(session, pipeline_id)["material_tasks"][0]
        save_material_candidate_pool(
            session,
            pipeline_id,
            task["shot_id"],
            _rows(Path(pipeline.project_dir)),
        )

        real_commit = session.commit

        def commit_then_lose_acknowledgement() -> None:
            real_commit()
            raise OSError("simulated commit acknowledgement loss")

        monkeypatch.setattr(session, "commit", commit_then_lose_acknowledgement)

        uploaded_bytes = _jpeg_bytes((10, 120, 30))
        with pytest.raises(CandidatePoolCommitUnknownError, match="reconcile"):
            replace_candidate_with_upload(
                session,
                pipeline_id,
                task["shot_id"],
                "cand-1",
                upload=BytesIO(uploaded_bytes),
                filename="replacement.jpg",
                content_type="image/jpeg",
                replacement_candidate_id="cand-ack",
                source="uploaded",
                url="https://example.test/commit-unknown",
                rights="Ian-owned review asset",
                evidence_kind="self_owned",
                reason="test ambiguous commit acknowledgement",
            )

        monkeypatch.setattr(session, "commit", real_commit)
        session.expire_all()
        package = get_material_package(session, pipeline_id)
        pool = package["material_tasks"][0]["candidate_pool"]
        assert any(item["candidate_id"] == "cand-ack" for item in pool["candidates"])

        uploaded = list(
            (Path(pipeline.project_dir) / "素材信息" / "候选预览").glob(
                "cand-ack-*.jpg"
            )
        )
        assert len(uploaded) == 1
        assert uploaded[0].read_bytes() == uploaded_bytes


def test_replace_upload_does_not_require_a_post_commit_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    with _session() as session:
        pipeline_id = _build_pipeline(session, tmp_path / "D" / "material-root")
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        task = get_material_package(session, pipeline_id)["material_tasks"][0]
        save_material_candidate_pool(
            session,
            pipeline_id,
            task["shot_id"],
            _rows(Path(pipeline.project_dir)),
        )
        real_refresh = session.refresh

        def fail_refresh(*_args: object, **_kwargs: object) -> None:
            raise OSError("post-commit refresh should not be required")

        monkeypatch.setattr(session, "refresh", fail_refresh)
        uploaded_bytes = _jpeg_bytes((210, 120, 30))
        result = replace_candidate_with_upload(
            session,
            pipeline_id,
            task["shot_id"],
            "cand-1",
            upload=BytesIO(uploaded_bytes),
            filename="replacement.jpg",
            content_type="image/jpeg",
            replacement_candidate_id="cand-no-refresh",
            source="uploaded",
            url="https://example.test/no-refresh",
            rights="Ian-owned review asset",
            evidence_kind="self_owned",
            reason="the committed response must not depend on a second DB read",
        )
        monkeypatch.setattr(session, "refresh", real_refresh)

        assert any(
            item["candidate_id"] == "cand-no-refresh"
            for item in result["candidates"]
        )
        uploaded = list(
            (Path(pipeline.project_dir) / "素材信息" / "候选预览").glob(
                "cand-no-refresh-*.jpg"
            )
        )
        assert len(uploaded) == 1
        assert uploaded[0].read_bytes() == uploaded_bytes


def test_replace_route_rejects_html_disguised_as_jpeg(tmp_path: Path):
    with _session() as session:
        pipeline_id = _build_pipeline(session, tmp_path / "D" / "material-root")
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        task = get_material_package(session, pipeline_id)["material_tasks"][0]
        save_material_candidate_pool(
            session,
            pipeline_id,
            task["shot_id"],
            _rows(Path(pipeline.project_dir)),
        )

        def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        try:
            response = TestClient(app).post(
                f"/api/pipeline/{pipeline_id}/material-tasks/{task['shot_id']}/candidate-pool/cand-1/replace",
                files={
                    "preview": (
                        "fake.jpg",
                        b"<html><body>not an image</body></html>",
                        "image/jpeg",
                    )
                },
                data={
                    "replacement_candidate_id": "cand-html",
                    "source": "uploaded",
                    "url": "https://example.test/fake",
                    "rights": "Ian-owned review asset",
                    "evidence_kind": "self_owned",
                    "reason": "must fail before database mutation",
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 400
        assert "媒体" in response.json()["detail"] or "文件内容" in response.json()["detail"]
        assert not list(
            (Path(pipeline.project_dir) / "素材信息" / "候选预览").glob(
                "cand-html-*.jpg"
            )
        )
        pool = get_material_package(session, pipeline_id)["material_tasks"][0][
            "candidate_pool"
        ]
        assert pool["revision"] == 0
        assert all(
            item["candidate_id"] != "cand-html" for item in pool["candidates"]
        )


def test_replace_locked_candidate_route_returns_conflict(tmp_path: Path):
    with _session() as session:
        pipeline_id = _build_pipeline(session, tmp_path / "D" / "material-root")
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        task = get_material_package(session, pipeline_id)["material_tasks"][0]
        save_material_candidate_pool(
            session,
            pipeline_id,
            task["shot_id"],
            _rows(Path(pipeline.project_dir)),
        )
        lock_material_candidate(
            session,
            pipeline_id,
            task["shot_id"],
            "cand-1",
            reason="Ian approved this candidate",
        )

        def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        try:
            response = TestClient(app).post(
                f"/api/pipeline/{pipeline_id}/material-tasks/{task['shot_id']}/candidate-pool/cand-1/replace",
                files={"preview": ("replacement.jpg", b"must-not-write", "image/jpeg")},
                data={
                    "replacement_candidate_id": "locked-attempt",
                    "source": "uploaded",
                    "url": "https://example.test/locked",
                    "rights": "Ian-owned review asset",
                    "evidence_kind": "self_owned",
                    "reason": "must be rejected before filesystem mutation",
                },
            )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 409
        assert "locked" in response.json()["detail"]
        assert not list(
            (Path(pipeline.project_dir) / "素材信息" / "候选预览").glob(
                "locked-attempt-*.jpg"
            )
        )


def test_preview_route_rejects_candidate_file_outside_approved_roots(tmp_path: Path):
    with _session() as session:
        pipeline_id = _build_pipeline(session, tmp_path / "D" / "material-root")
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        task = get_material_package(session, pipeline_id)["material_tasks"][0]
        outside = tmp_path / "D" / "outside.jpg"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(_jpeg_bytes((50, 50, 50)))
        rows = _rows(Path(pipeline.project_dir))
        rows[0]["preview_path"] = str(outside)
        save_material_candidate_pool(session, pipeline_id, task["shot_id"], rows)

        def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        try:
            response = TestClient(app).get(
                f"/api/pipeline/{pipeline_id}/material-tasks/{task['shot_id']}/candidate-pool/cand-1/preview"
            )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 400
        assert "approved" in response.json()["detail"]


def test_preview_route_returns_not_found_for_unknown_candidate(tmp_path: Path):
    with _session() as session:
        pipeline_id = _build_pipeline(session, tmp_path / "D" / "material-root")
        pipeline = session.get(ProductionPipeline, pipeline_id)
        assert pipeline is not None
        task = get_material_package(session, pipeline_id)["material_tasks"][0]
        save_material_candidate_pool(
            session,
            pipeline_id,
            task["shot_id"],
            _rows(Path(pipeline.project_dir)),
        )

        def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        try:
            response = TestClient(app).get(
                f"/api/pipeline/{pipeline_id}/material-tasks/{task['shot_id']}/candidate-pool/missing/preview"
            )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 404
