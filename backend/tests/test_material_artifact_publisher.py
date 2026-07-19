from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.core.windows_storage_policy import approve_physical_d_write_root
from app.materials.asset_paths import AutoMaterialPaths
from app.materials.stock_rights_evidence import build_stock_rights_evidence
from app.pipeline.material_operation_protocol import (
    InfoMotionPayload,
    MaterialOperationRequest,
    MaterialWorkerResult,
    StagedArtifact,
    encode_material_worker_result,
)
from app.pipeline.material_runner_retirement import (
    MaterialRunnerRetirementCheckpoint,
    install_pending_runner_retirement,
    json_payload_sha256,
)


EXECUTION_ID = "123e4567-e89b-12d3-a456-426614174000"


def _canonical_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass
class _Guard:
    path: Path
    project_use: object = object()
    path_identities: tuple[object, ...] = ()

    def verify(self) -> None:
        if not self.path.is_dir():
            raise AssertionError(f"guarded directory disappeared: {self.path}")


@dataclass
class _Store:
    payload: dict[str, Any]

    def fresh_read(self) -> dict[str, Any]:
        return copy.deepcopy(self.payload)

    def compare_and_swap(
        self,
        expected: dict[str, Any],
        replacement: dict[str, Any],
    ) -> bool:
        if self.payload != expected:
            return False
        self.payload = copy.deepcopy(replacement)
        return True


@dataclass
class _FailCompletionStore(_Store):
    calls: int = 0
    journal_path: Path | None = None

    def compare_and_swap(
        self,
        expected: dict[str, Any],
        replacement: dict[str, Any],
    ) -> bool:
        self.calls += 1
        if self.calls == 2:
            if self.journal_path is not None:
                journal = json.loads(self.journal_path.read_text(encoding="utf-8"))
                assert journal.get("attestation") == "committed"
            raise RuntimeError("completion acknowledgement lost")
        return super().compare_and_swap(expected, replacement)


@dataclass
class _FailReadStore(_Store):
    def fresh_read(self) -> dict[str, Any]:
        raise RuntimeError("fresh read unavailable")


def _attempt(tmp_path: Path):
    attempt_path = tmp_path / "attempt"
    (attempt_path / "payload").mkdir(parents=True)
    return attempt_path, approve_physical_d_write_root(attempt_path)


def _request(operation: str = "pexels") -> MaterialOperationRequest:
    return MaterialOperationRequest(
        protocol_version=2 if operation == "stock_candidates" else 1,
        execution_id=EXECUTION_ID,
        attempt_id=str(uuid4()),
        operation=operation,  # type: ignore[arg-type]
        payload=InfoMotionPayload(project_name="project", title="title")
        if operation == "info_motion"
        else __import__(
            "app.pipeline.material_operation_protocol",
            fromlist=["StockSearchPayload"],
        ).StockSearchPayload(query="office", per_page=1),
    )


def _stock_result(
    request: MaterialOperationRequest,
    payload_root: Path,
) -> MaterialWorkerResult:
    specs = (
        ("Pexels", "101", "https://www.pexels.com/video/example-101/"),
        ("Pixabay", "202", "https://pixabay.com/videos/example-202/"),
        ("Pexels", "303", "https://www.pexels.com/video/example-303/"),
    )
    artifacts: list[StagedArtifact] = []
    for index, (provider, provider_asset_id, asset_page_url) in enumerate(specs):
        filename = f"candidate-{index}.mp4"
        source = payload_root / filename
        source.write_bytes(f"real-provider-bytes-{index}".encode("ascii"))
        data = source.read_bytes()
        rights = build_stock_rights_evidence(
            provider=provider,
            provider_asset_id=provider_asset_id,
            media_type="video",
            asset_page_url=asset_page_url,
            contributor_name=f"Creator {index}",
            search_query="office",
            observed_at="2026-07-18T09:30:00+00:00",
        )
        artifacts.append(
            StagedArtifact(
                relative_path=f"payload/{filename}",
                media_kind="video",
                source_type=provider,
                source_url=asset_page_url,
                evidence_kind="atmosphere",
                is_locally_generated=False,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                rights_evidence=rights,
            )
        )
    return MaterialWorkerResult(
        protocol_version=2,
        execution_id=request.execution_id,
        attempt_id=request.attempt_id,
        operation="stock_candidates",
        status="succeeded",
        selected_artifact="",
        artifacts=tuple(artifacts),
        reason_code="",
        reason="Pexels and Pixabay candidates ready",
    )


def _project_guards(root: Path) -> dict[str, _Guard]:
    names = ("Pexels素材", "官方录屏", "信息动效", "全部素材", "素材信息")
    guards: dict[str, _Guard] = {}
    for name in names:
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        guards[name] = _Guard(path)
    return guards


def _result(
    request: MaterialOperationRequest,
    source: Path,
    *,
    media_kind: str = "video",
    rights_evidence: dict[str, Any] | None = None,
) -> MaterialWorkerResult:
    data = source.read_bytes()
    artifact = StagedArtifact(
        relative_path="payload/clip.mp4",
        media_kind=media_kind,  # type: ignore[arg-type]
        source_type="Pexels",
        source_url=(
            rights_evidence["asset_page_url"]
            if rights_evidence is not None
            else "https://example.com/clip"
        ),
        evidence_kind="atmosphere",
        is_locally_generated=False,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        rights_evidence=rights_evidence,
    )
    return MaterialWorkerResult(
        protocol_version=1,
        execution_id=request.execution_id,
        attempt_id=request.attempt_id,
        operation=request.operation,
        status="succeeded",
        selected_artifact=artifact.relative_path,
        artifacts=(artifact,),
        reason_code="",
        reason="ok",
    )


def test_publisher_rejects_traversal_and_hash_mismatch_before_write(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source")
    bad = _result(request, source)
    object.__setattr__(bad.artifacts[0], "relative_path", "payload/../escape.mp4")
    try:
        with pytest.raises(ValueError):
            CanonicalMaterialArtifactPublisher(canonical_paths=canonical).prepare(
                request,
                attempt,
                bad,
                project_directory_uses=project_guards,
            )
        assert not [path for path in (canonical.root / "自动下载").rglob("*") if path.is_file()]
    finally:
        attempt.close()


def test_parent_media_validator_rejects_disguised_artifact_before_write(
    tmp_path: Path,
):
    from app.materials.candidate_media_validation import validate_candidate_media
    from app.pipeline.material_artifact_publisher import (
        CanonicalMaterialArtifactPublisher,
        PublicationRejected,
    )

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"<html>not a video</html>")
    try:
        with pytest.raises(PublicationRejected, match="unsupported_signature"):
            CanonicalMaterialArtifactPublisher(
                canonical_paths=canonical,
                media_validator=validate_candidate_media,
            ).prepare(
                request,
                attempt,
                _result(request, source),
                project_directory_uses=project_guards,
            )
        assert not list(canonical.root.rglob("*.mp4"))
        assert not list((tmp_path / "project").rglob("*.mp4"))
    finally:
        attempt.close()


def test_publisher_rejects_undeclared_payload_file_before_write(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source-bytes")
    (attempt_path / "payload" / "extra.bin").write_bytes(b"undeclared")
    try:
        with pytest.raises(Exception, match="undeclared"):
            CanonicalMaterialArtifactPublisher(canonical_paths=canonical).prepare(
                request,
                attempt,
                _result(request, source),
                project_directory_uses=project_guards,
            )
        assert not list(canonical.root.rglob("*.mp4"))
    finally:
        attempt.close()


def test_publisher_commit_copies_to_canonical_and_project_without_clobber(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source-bytes")
    sentinel = canonical.pexels_video_dir / "clip.mp4"
    sentinel.write_bytes(b"keep-me")
    store = _Store(
        {
            "publication_phase": "none",
            "execution_state": "running",
            "execution_revision": 0,
        }
    )
    publisher = CanonicalMaterialArtifactPublisher(
        canonical_paths=canonical,
        database=store,
        expected_payload=store.payload,
    )
    rights_evidence = build_stock_rights_evidence(
        provider="Pexels",
        provider_asset_id="101",
        media_type="video",
        asset_page_url="https://www.pexels.com/video/example-101/",
        contributor_name="Ada",
        search_query="office",
        observed_at="2026-07-18T09:30:00+00:00",
    )
    try:
        publication = publisher.prepare(
            request,
            attempt,
            _result(request, source, rights_evidence=rights_evidence),
            project_directory_uses=project_guards,
        )
        result = publication.commit()
        assert result.success is True
        assert result.rights_evidence == rights_evidence
        assert result.material_path
        assert Path(result.material_path).is_file()
        assert sentinel.read_bytes() == b"keep-me"
        assert store.payload["publication_phase"] == "committed"
        assert store.payload["execution_revision"] == 2
        assert (attempt_path / "publication-journal.json").is_file()
        assert list(canonical.pexels_video_dir.glob("*.mp4"))
        assert list(project_guards["Pexels素材"].path.glob("*.mp4"))
        assert list(project_guards["全部素材"].path.glob("*.mp4"))
    finally:
        attempt.close()


def test_stock_candidate_batch_publishes_every_candidate_without_selecting_one(
    tmp_path: Path,
):
    from app.materials.candidate_media_validation import CandidateMediaReport
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher
    from app.pipeline.material_execution_service import MaterialCandidateBatchResult

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request("stock_candidates")
    worker_result = _stock_result(request, attempt_path / "payload")

    def validate_media(path: Path, *, declared_kind: str, declared_mime: str, limits):
        del limits
        data = path.read_bytes()
        return CandidateMediaReport(
            media_kind=declared_kind,
            mime_type=declared_mime,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            width=1920,
            height=1080,
            duration_seconds=2.0,
            codec="h264",
            container="mp4",
            decode_verified=True,
        )

    publisher = CanonicalMaterialArtifactPublisher(
        canonical_paths=canonical,
        media_validator=validate_media,
    )
    try:
        publication = publisher.prepare(
            request,
            attempt,
            worker_result,
            project_directory_uses=project_guards,
        )
        assert publication.selected_index is None

        result = publication.commit()

        assert type(result) is MaterialCandidateBatchResult
        assert result.status == "需要人工确认"
        assert len(result.candidates) == 3
        assert {candidate.source for candidate in result.candidates} == {
            "Pexels",
            "Pixabay",
        }
        assert all(Path(candidate.preview_path).is_file() for candidate in result.candidates)
        assert all(candidate.media_validation is not None for candidate in result.candidates)
        assert all(candidate.rights_evidence is not None for candidate in result.candidates)
        journal = json.loads(
            (attempt_path / "publication-journal.json").read_text(encoding="utf-8")
        )
        intent = journal["completion_intent"]
        assert intent["format"] == "material-publication-completion-intent-v1"
        assert intent["claim_receipt"] is None
        assert intent["worker_result_sha256"] == hashlib.sha256(
            encode_material_worker_result(worker_result)
        ).hexdigest()
        assert intent["published_result"]["kind"] == "stock_candidate_batch"
        assert len(intent["published_result"]["candidates"]) == 3
        assert intent["published_result_sha256"] == _canonical_sha(
            intent["published_result"]
        )
        intent_without_receipt = dict(intent)
        intent_sha256 = intent_without_receipt.pop("intent_sha256")
        assert intent_sha256 == _canonical_sha(intent_without_receipt)
        assert len(list(canonical.pexels_video_dir.glob("*.mp4"))) == 2
        assert len(list(canonical.pixabay_video_dir.glob("*.mp4"))) == 1
        assert len(list(project_guards["Pexels素材"].path.glob("*.mp4"))) == 2
        assert len(list(project_guards["全部素材"].path.glob("*.mp4"))) == 3
    finally:
        attempt.close()


def test_stock_candidate_routing_rejects_source_and_rights_provider_mismatch(
    tmp_path: Path,
):
    from app.pipeline.material_artifact_publisher import (
        CanonicalMaterialArtifactPublisher,
        PublicationError,
    )

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request("stock_candidates")
    worker_result = _stock_result(request, attempt_path / "payload")
    first = worker_result.artifacts[0]
    mismatched = StagedArtifact(
        relative_path=first.relative_path,
        media_kind=first.media_kind,
        source_type="Pixabay",
        source_url=first.source_url,
        evidence_kind=first.evidence_kind,
        is_locally_generated=first.is_locally_generated,
        sha256=first.sha256,
        size_bytes=first.size_bytes,
        rights_evidence=first.rights_evidence,
    )
    # Simulate corrupted post-protocol in-memory state so the parent publisher's
    # independent defence remains covered even though protocol v2 now rejects
    # this mismatch earlier during normal execution.
    object.__setattr__(
        worker_result,
        "artifacts",
        (mismatched, *worker_result.artifacts[1:]),
    )
    try:
        with pytest.raises(PublicationError, match="rights provider"):
            CanonicalMaterialArtifactPublisher(canonical_paths=canonical).prepare(
                request,
                attempt,
                worker_result,
                project_directory_uses=project_guards,
            )
        assert not list(project_guards["全部素材"].path.glob("*.mp4"))
    finally:
        attempt.close()


@pytest.mark.parametrize("durable_phase", ["tentative", "db_committed", "committed"])
def test_restart_adopts_committed_stock_batch_without_copying_again(
    tmp_path: Path,
    durable_phase: str,
):
    from app.materials.candidate_media_validation import CandidateMediaReport
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request("stock_candidates")
    worker_result = _stock_result(request, attempt_path / "payload")
    control_id = str(uuid4())
    job_instance_id = str(uuid4())
    store = _Store(
        {
            "material_tasks": [
                {
                    "material_task_id": "task-001",
                    "shot_id": "shot-001",
                    "status": "进行中",
                    "status_history": ["进行中"],
                }
            ],
            "publication_phase": "preparing",
            "execution_state": "claimed",
            "execution_revision": 7,
            "execution_id": request.execution_id,
            "active_material_task_id": "task-001",
            "active_execution_id": request.execution_id,
            "active_attempt_id": request.attempt_id,
            "active_attempt_kind": request.operation,
            "active_control_id": control_id,
            "active_job_instance_id": job_instance_id,
        }
    )

    def validate_media(path: Path, *, declared_kind: str, declared_mime: str, limits):
        del limits
        data = path.read_bytes()
        return CandidateMediaReport(
            media_kind=declared_kind,
            mime_type=declared_mime,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            width=1920,
            height=1080,
            duration_seconds=2.0,
            codec="h264",
            container="mp4",
            decode_verified=True,
        )

    first = CanonicalMaterialArtifactPublisher(
        canonical_paths=canonical,
        database=store,
        expected_payload=store.payload,
        media_validator=validate_media,
    )
    publication = first.prepare(
        request,
        attempt,
        worker_result,
        project_directory_uses=project_guards,
    )
    expected_result = publication.commit(retain_ownership=True)
    publication.release_after_retirement()
    if durable_phase != "committed":
        journal_path = attempt_path / "publication-journal.json"
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["phase"] = durable_phase
        journal.pop("attestation", None)
        journal_path.write_text(
            json.dumps(journal, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    counts_before = {
        "pexels": len(list(canonical.pexels_video_dir.glob("*.mp4"))),
        "pixabay": len(list(canonical.pixabay_video_dir.glob("*.mp4"))),
        "project": len(list(project_guards["全部素材"].path.glob("*.mp4"))),
    }

    recovered_attempt = approve_physical_d_write_root(attempt_path)
    recovered = None
    try:
        second = CanonicalMaterialArtifactPublisher(
            canonical_paths=canonical,
            database=_Store(store.payload),
            expected_payload=store.payload,
            media_validator=validate_media,
        )
        recovered = second.adopt_committed_publication(
            request,
            recovered_attempt,
            worker_result,
            project_directory_uses=project_guards,
        )
        assert recovered.result == expected_result
        assert recovered.ownership_retained is True
        recovered.assert_retained_ownership()
        sealed = json.loads(
            (attempt_path / "publication-journal.json").read_text(encoding="utf-8")
        )
        assert sealed["phase"] == "committed"
        assert sealed["attestation"] == "committed"
        assert counts_before == {
            "pexels": len(list(canonical.pexels_video_dir.glob("*.mp4"))),
            "pixabay": len(list(canonical.pixabay_video_dir.glob("*.mp4"))),
            "project": len(list(project_guards["全部素材"].path.glob("*.mp4"))),
        }
    finally:
        if recovered is not None and recovered.ownership_retained:
            recovered.release_after_retirement()
        recovered_attempt.close()
        attempt.close()


def test_restart_adopts_completed_pending_publication_without_copying_again(
    tmp_path: Path,
):
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request("pexels")
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"completed-pending-source")
    worker_result = _result(request, source)
    control_id = str(uuid4())
    job_instance_id = str(uuid4())
    store = _Store(
        {
            "material_tasks": [
                {
                    "material_task_id": "task-001",
                    "status": "running",
                    "status_history": ["running"],
                }
            ],
            "publication_phase": "preparing",
            "execution_state": "claimed",
            "execution_revision": 7,
            "execution_id": request.execution_id,
            "active_material_task_id": "task-001",
            "active_execution_id": request.execution_id,
            "active_attempt_id": request.attempt_id,
            "active_attempt_kind": request.operation,
            "active_control_id": control_id,
            "active_job_instance_id": job_instance_id,
        }
    )
    first = CanonicalMaterialArtifactPublisher(
        canonical_paths=canonical,
        database=store,
        expected_payload=store.payload,
    )
    publication = first.prepare(
        request,
        attempt,
        worker_result,
        project_directory_uses=project_guards,
    )
    expected_result = publication.commit(retain_ownership=True)
    journal_sha256 = publication.committed_journal_sha256
    publication.release_after_retirement()
    publishing = copy.deepcopy(store.payload)
    observed_at = "2026-07-18T11:00:00+00:00"
    result_sha256 = hashlib.sha256(
        encode_material_worker_result(worker_result)
    ).hexdigest()
    history_sha256 = "9" * 64
    completed = copy.deepcopy(publishing)
    completed.update(
        {
            "execution_state": "idle",
            "publication_phase": "committed",
            "last_execution_status": "succeeded",
            "execution_completed_at": observed_at,
            "execution_revision": 9,
            "active_material_task_id": None,
            "active_execution_id": None,
            "active_attempt_id": None,
            "active_attempt_kind": None,
            "active_control_id": None,
            "active_job_instance_id": None,
            "material_attempt_history": [
                {
                    "material_task_id": "task-001",
                    "execution_id": request.execution_id,
                    "operation": request.operation,
                    "attempt_id": request.attempt_id,
                    "control_id": control_id,
                    "job_instance_id": job_instance_id,
                    "status": "succeeded",
                    "observed_at": observed_at,
                    "result_sha256": result_sha256,
                    "publication_journal_sha256": journal_sha256,
                    "reason_code": "",
                    "reason": "",
                    "entry_sha256": history_sha256,
                }
            ],
        }
    )
    checkpoint = MaterialRunnerRetirementCheckpoint.create(
        material_task_id="task-001",
        execution_id=request.execution_id,
        attempt_id=request.attempt_id,
        operation=request.operation,
        control_id=control_id,
        job_instance_id=job_instance_id,
        completion_status="succeeded",
        completion_revision=9,
        completion_observed_at=observed_at,
        reference_revision=8,
        reference_payload_sha256=json_payload_sha256(publishing),
        attempt_history_sha256=history_sha256,
        result_sha256=result_sha256,
        publication_journal_sha256=journal_sha256,
        publication_intent_sha256=publishing["publication_intent_sha256"],
        publication_result_sha256=publishing["publication_result_sha256"],
        publication_worker_result_sha256=publishing[
            "publication_worker_result_sha256"
        ],
        publication_reference_base_revision=publishing[
            "publication_reference_base_revision"
        ],
        publication_reference_base_payload_sha256=publishing[
            "publication_reference_base_payload_sha256"
        ],
    )
    install_pending_runner_retirement(completed, checkpoint)
    store.payload = completed
    counts_before = len(list((tmp_path / "project").rglob("*.mp4")))

    recovered_attempt = approve_physical_d_write_root(attempt_path)
    recovered = None
    try:
        second = CanonicalMaterialArtifactPublisher(
            canonical_paths=canonical,
            database=_Store(completed),
            expected_payload=completed,
        )
        recovered = second.adopt_completed_publication(
            request,
            recovered_attempt,
            worker_result,
            project_directory_uses=project_guards,
        )
        assert recovered.result == expected_result
        assert recovered.ownership_retained is True
        recovered.assert_retained_ownership()
        assert len(list((tmp_path / "project").rglob("*.mp4"))) == counts_before
    finally:
        if recovered is not None and recovered.ownership_retained:
            recovered.release_after_retirement()
        recovered_attempt.close()
        attempt.close()


def test_publisher_can_retain_ownership_until_runner_retirement(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher

    class _TrackingMutex:
        def __init__(self) -> None:
            self.acquired = False
            self.release_calls = 0

        def acquire(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self.acquired = True

        def release(self) -> None:
            self.release_calls += 1
            self.acquired = False

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"retained-source")
    store = _Store(
        {
            "publication_phase": "none",
            "execution_state": "running",
            "execution_revision": 7,
            "active_material_task_id": "task-001",
            "material_tasks": [
                {
                    "material_task_id": "task-001",
                    "status": "进行中",
                    "status_history": ["等待", "进行中"],
                },
                {
                    "material_task_id": "task-002",
                    "status": "等待",
                    "status_history": ["等待"],
                },
            ],
        }
    )
    mutex = _TrackingMutex()
    publisher = CanonicalMaterialArtifactPublisher(
        canonical_paths=canonical,
        database=store,
        expected_payload=store.payload,
        registry_mutex=mutex,
    )
    publication = None
    try:
        publication = publisher.prepare(
            request,
            attempt,
            _result(request, source),
            project_directory_uses=project_guards,
        )
        publication.commit(retain_ownership=True)
        assert publication.ownership_retained is True
        assert publication._released is False
        assert mutex.release_calls == 0
        assert store.payload["publication_phase"] == "db_committed"
        assert store.payload["material_tasks"][0]["status"] == "发布收口中"
        assert store.payload["material_tasks"][0]["status_history"][-1] == "发布收口中"
        assert store.payload["material_tasks"][1]["status"] == "等待"
        assert store.payload["execution_revision"] == 8
        intent = json.loads(
            (attempt_path / "publication-journal.json").read_text(encoding="utf-8")
        )["completion_intent"]
        assert store.payload["publication_intent_sha256"] == intent["intent_sha256"]
        assert (
            store.payload["publication_result_sha256"]
            == intent["published_result_sha256"]
        )
        assert (
            store.payload["publication_worker_result_sha256"]
            == intent["worker_result_sha256"]
        )
        assert (
            store.payload["publication_reference_base_revision"]
            == intent["claim_receipt"]["reference_base_revision"]
            == 7
        )
        journal_path = attempt_path / "publication-journal.json"
        assert publication.committed_journal_sha256 == hashlib.sha256(
            journal_path.read_bytes()
        ).hexdigest()

        publication.release_after_retirement()
        assert publication.ownership_retained is False
        assert publication._released is True
        assert mutex.release_calls == 1
    finally:
        if publication is not None and not publication._released:
            publication.release_after_retirement()
        attempt.close()


def test_publisher_tentative_rollback_removes_only_its_files(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source-bytes")
    store = _Store({"publication_phase": "none", "execution_state": "running"})
    publisher = CanonicalMaterialArtifactPublisher(
        canonical_paths=canonical,
        database=store,
        expected_payload=store.payload,
    )
    try:
        publication = publisher.prepare(
            request,
            attempt,
            _result(request, source),
            project_directory_uses=project_guards,
        )
        publication.publish_tentative()
        assert list(canonical.pexels_video_dir.glob("*.mp4"))
        publication.rollback()
        assert not list(canonical.pexels_video_dir.glob("*.mp4"))
        assert not list(project_guards["Pexels素材"].path.glob("*.mp4"))
        assert not list(project_guards["全部素材"].path.glob("*.mp4"))
        assert store.payload == {"publication_phase": "none", "execution_state": "running"}
    finally:
        attempt.close()


def test_publisher_second_project_target_failure_rolls_back_first_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.pipeline.material_artifact_publisher import (
        CanonicalMaterialArtifactPublisher,
    )

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source-bytes")
    publisher = CanonicalMaterialArtifactPublisher(canonical_paths=canonical)
    real_copy = publisher._copy_with_collision

    def fail_aggregate(*args: object, **kwargs: object):
        if kwargs.get("target_label") == "all_materials":
            raise RuntimeError("aggregate target unavailable")
        return real_copy(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(publisher, "_copy_with_collision", fail_aggregate)
    try:
        publication = publisher.prepare(
            request,
            attempt,
            _result(request, source),
            project_directory_uses=project_guards,
        )
        with pytest.raises(RuntimeError, match="aggregate target"):
            publication.publish_tentative()
        publication.rollback()
        assert not list(canonical.root.rglob("*.mp4"))
        assert not list(project_guards["Pexels素材"].path.glob("*.mp4"))
        assert not list(project_guards["全部素材"].path.glob("*.mp4"))
    finally:
        attempt.close()


def test_publisher_refuses_rollback_after_db_committed(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import (
        CanonicalMaterialArtifactPublisher,
        PublicationAuditRequired,
    )

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source-bytes")
    store = _Store({"publication_phase": "none", "execution_state": "running"})
    publisher = CanonicalMaterialArtifactPublisher(
        canonical_paths=canonical,
        database=store,
        expected_payload=store.payload,
    )
    try:
        publication = publisher.prepare(
            request,
            attempt,
            _result(request, source),
            project_directory_uses=project_guards,
        )
        publication.commit()
        with pytest.raises(PublicationAuditRequired):
            publication.rollback()
        assert list(canonical.pexels_video_dir.glob("*.mp4"))
        assert store.payload["publication_phase"] == "committed"
    finally:
        attempt.close()


def test_publisher_rollback_requires_durable_pre_reference_phase(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import (
        CanonicalMaterialArtifactPublisher,
        PublicationAuditRequired,
    )

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source-bytes")
    publisher = CanonicalMaterialArtifactPublisher(canonical_paths=canonical)
    try:
        publication = publisher.prepare(
            request,
            attempt,
            _result(request, source),
            project_directory_uses=project_guards,
        )
        publication.publish_tentative()
        journal_path = attempt_path / "publication-journal.json"
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["phase"] = "db_committed"
        journal_path.write_text(
            json.dumps(journal, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        with pytest.raises(PublicationAuditRequired, match="rollback boundary"):
            publication.rollback()
        assert list(canonical.pexels_video_dir.glob("*.mp4"))
        assert list(project_guards["Pexels素材"].path.glob("*.mp4"))
        assert list(project_guards["全部素材"].path.glob("*.mp4"))
    finally:
        attempt.close()


def test_move_intent_is_durable_before_native_move(tmp_path: Path, monkeypatch):
    import app.pipeline.material_artifact_publisher as publisher_module
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source-bytes")
    observed: list[dict[str, Any]] = []
    real_move = publisher_module._move_no_replace

    def inspect_before_move(part: Path, destination: Path):
        journal = json.loads((attempt_path / "publication-journal.json").read_text(encoding="utf-8"))
        observed.append(journal["moves"][-1])
        return real_move(part, destination)

    monkeypatch.setattr(publisher_module, "_move_no_replace", inspect_before_move)
    publisher = CanonicalMaterialArtifactPublisher(canonical_paths=canonical)
    try:
        publication = publisher.prepare(
            request,
            attempt,
            _result(request, source),
            project_directory_uses=project_guards,
        )
        publication.publish_tentative()
        assert observed
        assert observed[0]["state"] == "move_intent"
        assert Path(observed[0]["part_path"]).name.startswith(".")
    finally:
        attempt.close()


def test_completion_cas_failure_retains_all_publication_evidence(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import (
        CanonicalMaterialArtifactPublisher,
        PublicationAuditRequired,
    )

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source-bytes")
    store = _FailCompletionStore({"publication_phase": "none", "execution_state": "running"})
    store.journal_path = attempt_path / "publication-journal.json"
    publisher = CanonicalMaterialArtifactPublisher(
        canonical_paths=canonical,
        database=store,
        expected_payload=store.payload,
    )
    try:
        publication = publisher.prepare(
            request,
            attempt,
            _result(request, source),
            project_directory_uses=project_guards,
        )
        with pytest.raises(PublicationAuditRequired):
            publication.commit()
        assert list(canonical.pexels_video_dir.glob("*.mp4"))
        assert list(project_guards["Pexels素材"].path.glob("*.mp4"))
        journal = json.loads((attempt_path / "publication-journal.json").read_text(encoding="utf-8"))
        assert journal["phase"] == "audit_required"
        assert store.payload["publication_phase"] == "db_committed"
    finally:
        attempt.close()


def test_parent_rejects_hardlink_and_suffix_mismatch(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher

    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")

    hardlink_attempt_path, hardlink_attempt = _attempt(tmp_path / "hardlink")
    hardlink_request = _request()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    hardlink = hardlink_attempt_path / "payload" / "clip.mp4"
    os.link(outside, hardlink)
    try:
        with pytest.raises(ValueError, match="hard link"):
            CanonicalMaterialArtifactPublisher(canonical_paths=canonical).prepare(
                hardlink_request,
                hardlink_attempt,
                _result(hardlink_request, hardlink),
                project_directory_uses=project_guards,
            )
    finally:
        hardlink_attempt.close()

    suffix_attempt_path, suffix_attempt = _attempt(tmp_path / "suffix")
    suffix_request = _request()
    source = suffix_attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"video")
    try:
        with pytest.raises(ValueError, match="suffix"):
            CanonicalMaterialArtifactPublisher(canonical_paths=canonical).prepare(
                suffix_request,
                suffix_attempt,
                _result(suffix_request, source, media_kind="image"),
                project_directory_uses=project_guards,
            )
    finally:
        suffix_attempt.close()


def test_parent_rejects_symlink_without_publishing(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    target = attempt_path / "payload" / "target.mp4"
    target.write_bytes(b"target")
    link = attempt_path / "payload" / "clip.mp4"
    try:
        link.symlink_to(target)
    except OSError:
        attempt.close()
        pytest.skip("this Windows account cannot create a symlink")
    try:
        with pytest.raises(ValueError, match="reparse"):
            CanonicalMaterialArtifactPublisher(canonical_paths=canonical).prepare(
                request,
                attempt,
                _result(request, link),
                project_directory_uses=project_guards,
            )
    finally:
        attempt.close()


def test_stable_duplicate_is_reused_but_tampered_record_is_not(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher

    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")

    first_attempt_path, first_attempt = _attempt(tmp_path / "first")
    first_request = _request()
    first_source = first_attempt_path / "payload" / "clip.mp4"
    first_source.write_bytes(b"same-bytes")
    try:
        first_pub = CanonicalMaterialArtifactPublisher(canonical_paths=canonical).prepare(
            first_request,
            first_attempt,
            _result(first_request, first_source),
            project_directory_uses=project_guards,
        )
        first_pub.commit()
    finally:
        first_attempt.close()
    canonical_files = list(canonical.pexels_video_dir.glob("*.mp4"))
    assert len(canonical_files) == 1

    second_attempt_path, second_attempt = _attempt(tmp_path / "second")
    second_request = _request()
    second_source = second_attempt_path / "payload" / "clip.mp4"
    second_source.write_bytes(b"same-bytes")
    try:
        second_pub = CanonicalMaterialArtifactPublisher(canonical_paths=canonical).prepare(
            second_request,
            second_attempt,
            _result(second_request, second_source),
            project_directory_uses=project_guards,
        )
        second_pub.commit()
        assert list(canonical.pexels_video_dir.glob("*.mp4")) == canonical_files
    finally:
        second_attempt.close()

    canonical_files[0].write_bytes(b"tampered!")
    third_attempt_path, third_attempt = _attempt(tmp_path / "third")
    third_request = _request()
    third_source = third_attempt_path / "payload" / "clip.mp4"
    third_source.write_bytes(b"same-bytes")
    try:
        third_pub = CanonicalMaterialArtifactPublisher(canonical_paths=canonical).prepare(
            third_request,
            third_attempt,
            _result(third_request, third_source),
            project_directory_uses=project_guards,
        )
        third_pub.commit()
        assert len(list(canonical.pexels_video_dir.glob("*.mp4"))) == 2
        assert canonical_files[0].read_bytes() == b"tampered!"
    finally:
        third_attempt.close()


def test_publisher_requires_database_and_expected_payload_as_a_pair(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher

    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    with pytest.raises(ValueError, match="expected_payload"):
        CanonicalMaterialArtifactPublisher(
            canonical_paths=canonical,
            database=_Store({"publication_phase": "none"}),
        )
    with pytest.raises(ValueError, match="expected_payload"):
        CanonicalMaterialArtifactPublisher(
            canonical_paths=canonical,
            expected_payload={"publication_phase": "none"},
        )


def test_publisher_rejects_result_operation_mismatch_before_any_write(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request("pexels")
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source-bytes")
    result = _result(request, source)
    object.__setattr__(result, "operation", "pixabay")
    try:
        with pytest.raises(Exception, match="operation"):
            CanonicalMaterialArtifactPublisher(canonical_paths=canonical).prepare(
                request,
                attempt,
                result,
                project_directory_uses=project_guards,
            )
        assert not list(canonical.root.rglob("*.mp4"))
    finally:
        attempt.close()


def test_publisher_rejects_reusing_a_bare_project_path(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    pexels_key = next(key for key in project_guards if key.startswith("Pexels"))
    project_guards[pexels_key] = project_guards[pexels_key].path
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source-bytes")
    try:
        with pytest.raises(Exception, match="prepared child identity guard"):
            CanonicalMaterialArtifactPublisher(canonical_paths=canonical).prepare(
                request,
                attempt,
                _result(request, source),
                project_directory_uses=project_guards,
            )
    finally:
        attempt.close()


def test_publisher_refuses_to_overwrite_an_existing_publication_journal(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import (
        CanonicalMaterialArtifactPublisher,
        PublicationAuditRequired,
    )

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source-bytes")
    (attempt_path / "publication-journal.json").write_text(
        '{"phase":"tentative"}', encoding="utf-8"
    )
    try:
        with pytest.raises(PublicationAuditRequired, match="journal"):
            CanonicalMaterialArtifactPublisher(canonical_paths=canonical).prepare(
                request,
                attempt,
                _result(request, source),
                project_directory_uses=project_guards,
            )
        assert (attempt_path / "publication-journal.json").read_text(
            encoding="utf-8"
        ) == '{"phase":"tentative"}'
    finally:
        attempt.close()


def test_database_fresh_read_failure_enters_audit_and_keeps_files(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import (
        CanonicalMaterialArtifactPublisher,
        PublicationAuditRequired,
    )

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source-bytes")
    store = _FailReadStore({"publication_phase": "none", "execution_state": "running"})
    publisher = CanonicalMaterialArtifactPublisher(
        canonical_paths=canonical,
        database=store,
        expected_payload=store.payload,
    )
    try:
        publication = publisher.prepare(
            request,
            attempt,
            _result(request, source),
            project_directory_uses=project_guards,
        )
        with pytest.raises(PublicationAuditRequired, match="fresh read"):
            publication.commit()
        assert publication.phase == "audit_required"
        assert list(canonical.pexels_video_dir.glob("*.mp4"))
        with pytest.raises(PublicationAuditRequired):
            publication.rollback()
    finally:
        attempt.close()


def test_move_applied_but_journal_ack_unknown_never_auto_deletes(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import (
        CanonicalMaterialArtifactPublisher,
        PublicationAuditRequired,
    )

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source-bytes")
    publisher = CanonicalMaterialArtifactPublisher(canonical_paths=canonical)
    try:
        publication = publisher.prepare(
            request,
            attempt,
            _result(request, source),
            project_directory_uses=project_guards,
        )

        def fail_after_move(_move: object) -> None:
            raise RuntimeError("journal flush acknowledgement lost")

        publication._record_move_update = fail_after_move  # type: ignore[method-assign]
        with pytest.raises(PublicationAuditRequired, match="journal"):
            publication.publish_tentative()
        assert list(canonical.pexels_video_dir.glob("*.mp4"))
        with pytest.raises(PublicationAuditRequired):
            publication.rollback()
    finally:
        attempt.close()


def test_registry_hardlink_is_rejected_before_publication(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import (
        CanonicalMaterialArtifactPublisher,
        PublicationAuditRequired,
    )

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    external_registry = tmp_path / "external-registry.json"
    external_registry.write_text("{}", encoding="utf-8")
    registry_path = canonical.fingerprint_registry_path
    os.link(external_registry, registry_path)
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source-bytes")
    try:
        with pytest.raises(PublicationAuditRequired, match="hard link"):
            CanonicalMaterialArtifactPublisher(canonical_paths=canonical).prepare(
                request,
                attempt,
                _result(request, source),
                project_directory_uses=project_guards,
            )
        assert external_registry.read_text(encoding="utf-8") == "{}"
    finally:
        attempt.close()
        if registry_path.exists():
            registry_path.unlink()


@pytest.mark.parametrize("name", ["CON.mp4", "NUL", "COM1.txt"])
def test_publication_filename_rejects_windows_reserved_or_ambiguous_names(name: str):
    from app.pipeline.material_artifact_publisher import PublicationError, _safe_filename

    with pytest.raises(PublicationError):
        _safe_filename(name)


def test_publication_context_exception_rolls_back_tentative_files(tmp_path: Path):
    from app.pipeline.material_artifact_publisher import CanonicalMaterialArtifactPublisher

    attempt_path, attempt = _attempt(tmp_path)
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards = _project_guards(tmp_path / "project")
    request = _request()
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"source-bytes")
    publisher = CanonicalMaterialArtifactPublisher(canonical_paths=canonical)
    try:
        with pytest.raises(RuntimeError, match="stop"):
            with publisher.prepare(
                request,
                attempt,
                _result(request, source),
                project_directory_uses=project_guards,
            ) as publication:
                publication.publish_tentative()
                raise RuntimeError("stop")
        assert not list(canonical.root.rglob("*.mp4"))
        assert not list(project_guards[next(iter(project_guards))].path.glob("*.mp4"))
    finally:
        attempt.close()


def test_exact_cleanup_does_not_fall_back_to_path_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.pipeline.material_artifact_publisher import _identity, _remove_exact_part

    path = tmp_path / "owned.part"
    payload = b"handle-bound-delete"
    path.write_bytes(payload)
    identity = _identity(path)
    assert identity is not None

    def forbidden_unlink(_self: Path, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("path.unlink must not be used for exact cleanup")

    monkeypatch.setattr(Path, "unlink", forbidden_unlink)
    _remove_exact_part(
        path,
        expected_identity=identity,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_size=len(payload),
    )
    assert not path.exists()


def test_copy_failure_cleans_unjournaled_exclusive_part(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import app.pipeline.material_artifact_publisher as publisher_module

    source = tmp_path / "source.mp4"
    part = tmp_path / ".source.mp4.part"
    source.write_bytes(b"copy-failure-cleanup")

    def fail_fsync(_fd: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(publisher_module.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="fsync failed"):
        publisher_module._copy_to_exclusive_part(
            source,
            part,
            max_bytes=1024,
            expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            expected_size=source.stat().st_size,
        )
    assert not part.exists()
