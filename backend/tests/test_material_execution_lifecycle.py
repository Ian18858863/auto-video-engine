from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, build_engine
from app.core.windows_storage_policy import approve_physical_d_write_root
from app.materials.asset_paths import AutoMaterialPaths
from app.materials.stock_rights_evidence import build_stock_rights_evidence
from app.models.entities import HotItem, PipelineStep, ProductionPipeline
from app.pipeline.material_artifact_publisher import (
    CanonicalMaterialArtifactPublisher,
    CanonicalMaterialPublication,
)
from app.pipeline.material_execution_claims import (
    MaterialExecutionClaimConflict,
    MaterialExecutionClaimKey,
    MaterialExecutionClaimUnknown,
    SqlAlchemyMaterialExecutionClaimStore,
)
from app.pipeline.material_execution_coordinator import (
    MaterialExecutionCoordinatorUnknown,
    MaterialCompletionReceipt,
    PreparedMaterialClaim,
)
from app.pipeline.material_execution_lifecycle import (
    ClaimLeasePublicationDatabase,
    MaterialExecutionClaimPreparer,
    MaterialPrepareAndClaimUnknown,
    MaterialRestartRecoveryCoordinator,
    MaterialRestartRecoveryUnknown,
    SqlAlchemyMaterialLifecycleAdapter,
    completion_receipt_from_pending_payload,
)
from app.pipeline.material_candidate_contract import CandidateEntry, CandidateSet
from app.pipeline.material_execution_service import (
    MaterialCandidateBatchResult,
    MaterialExecutionResult,
)
from app.pipeline import material_execution_lifecycle as lifecycle_subject
from app.pipeline.material_operation_protocol import (
    MaterialOperationOutcome,
    MaterialOperationRequest,
    MaterialWorkerResult,
    StockSearchPayload,
    StagedArtifact,
    TerminationEvidence,
    encode_material_worker_result,
)
from app.pipeline.material_operation_runner import (
    MaterialAttemptRetirementReceipt,
    MaterialRunnerClaimBinding,
    PreparedMaterialAttempt,
)
from app.pipeline.material_runner_retirement import (
    MaterialRunnerRetirementAcknowledgement,
    MaterialRunnerRetirementAttestation,
    MaterialRunnerRetirementCheckpoint,
    install_pending_runner_retirement,
    pending_runner_retirement_from_payload,
)


def _canonical_sha(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _ids() -> tuple[str, str, str, str]:
    return tuple(str(uuid4()) for _ in range(4))  # type: ignore[return-value]


@pytest.fixture()
def real_claim_store(tmp_path: Path):
    database_path = tmp_path / "lifecycle.db"
    assert database_path.drive.upper() == "D:"
    engine = build_engine(f"sqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        hot_item = HotItem(
            source_platform="test",
            source_type="test",
            title="lifecycle test",
            url=f"https://example.invalid/{uuid4().hex}",
        )
        session.add(hot_item)
        session.flush()
        pipeline = ProductionPipeline(
            hotspot_id=hot_item.id,
            project_name="lifecycle-test",
            project_dir=str(tmp_path / "project"),
            project_key=f"pending-{uuid4().hex}-lifecycle-test",
        )
        step = PipelineStep(
            name="生成素材任务",
            step_order=3,
            output_summary={
                "material_tasks": [
                    {
                        "material_task_id": "task-001",
                        "shot_id": "shot-01",
                        "status": "等待",
                        "status_history": ["等待"],
                    },
                    {
                        "material_task_id": "task-002",
                        "shot_id": "shot-02",
                        "status": "等待",
                        "status_history": ["等待"],
                    },
                ],
                "publication_phase": "idle",
                "execution_state": "idle",
                "execution_revision": 0,
            },
        )
        pipeline.steps.append(step)
        session.add(pipeline)
        session.commit()
        key = MaterialExecutionClaimKey(
            database_path=database_path,
            pipeline_id=pipeline.id,
            material_step_id=step.id,
        )
    store = SqlAlchemyMaterialExecutionClaimStore(factory, key=key)
    try:
        yield store, factory, key
    finally:
        engine.dispose()


def _prepared(
    execution_id: str,
    attempt_id: str,
    control_id: str,
    job_instance_id: str,
) -> PreparedMaterialAttempt:
    return PreparedMaterialAttempt(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        control_directory=object(),  # runner test double owns these opaque pins
        directory=object(),
        pin_scope=(),
        container_hash="a" * 64,
    )


class _Mutex:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.released = False

    def assert_owned(self) -> None:
        if self.released:
            raise RuntimeError("released")

    def release(self) -> None:
        self.events.append("release_mutex")
        self.released = True


@dataclass
class _ProjectGuard:
    path: Path
    project_use: object = object()
    path_identities: tuple[object, ...] = ()

    def verify(self) -> None:
        if not self.path.is_dir():
            raise AssertionError(f"guarded project directory disappeared: {self.path}")


class _RegistryMutex:
    def __init__(self) -> None:
        self.acquired = False

    def acquire(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.acquired = True

    def release(self) -> None:
        self.acquired = False


class _Lease:
    def __init__(
        self,
        *,
        material_task_id: str,
        execution_id: str,
        attempt_id: str,
        control_id: str,
        job_instance_id: str,
        payload: dict,
        events: list[str] | None = None,
    ) -> None:
        self.material_task_id = material_task_id
        self.execution_id = execution_id
        self.active_attempt_kind = str(payload["active_attempt_kind"])
        self.active_attempt_id = attempt_id
        self.active_control_id = control_id
        self.active_job_instance_id = job_instance_id
        self._expected_payload = copy.deepcopy(payload)
        self.expected_revision = int(payload["execution_revision"])
        self.events = events if events is not None else []
        self.released = False

    @property
    def expected_payload(self) -> dict:
        return copy.deepcopy(self._expected_payload)

    def assert_owned(self, session=None) -> None:
        del session
        self.events.append("assert_owned")
        if self.released:
            raise RuntimeError("released")

    def compare_and_swap(self, replacement: dict) -> None:
        self.events.append("completion_cas")
        assert replacement["execution_revision"] == self.expected_revision + 1
        self._expected_payload = copy.deepcopy(replacement)
        self.expected_revision = int(replacement["execution_revision"])

    def clear_attempt_for_fallback(self, *, replacement: dict):
        self.events.append("clear_rotation_cas")
        replacement = copy.deepcopy(replacement)
        assert replacement["execution_revision"] == self.expected_revision + 1
        assert replacement["active_attempt_id"] is None
        self._expected_payload = replacement
        self.expected_revision = int(replacement["execution_revision"])
        self.active_attempt_id = ""
        self.active_attempt_kind = ""
        self.active_control_id = ""
        self.active_job_instance_id = ""
        return SimpleNamespace(
            material_task_id=self.material_task_id,
            execution_id=self.execution_id,
            attempt_id=self.active_attempt_id,
            revision=self.expected_revision,
            payload=copy.deepcopy(replacement),
        )

    def install_rotated_attempt(
        self,
        *,
        attempt_id: str,
        attempt_kind: str,
        control_id: str,
        job_instance_id: str,
        terminal_receipt: MaterialCompletionReceipt,
        retirement_receipt: MaterialAttemptRetirementReceipt,
        replacement: dict,
    ):
        self.events.append("rotation_cas")
        assert terminal_receipt.attempt_id == retirement_receipt.attempt_id
        replacement = copy.deepcopy(replacement)
        replacement.update(
            {
                "active_material_task_id": self.material_task_id,
                "active_execution_id": self.execution_id,
                "active_attempt_id": attempt_id,
                "active_attempt_kind": attempt_kind,
                "active_control_id": control_id,
                "active_job_instance_id": job_instance_id,
                "execution_revision": self.expected_revision + 1,
            }
        )
        self._expected_payload = replacement
        self.expected_revision = int(replacement["execution_revision"])
        self.active_attempt_id = attempt_id
        self.active_attempt_kind = attempt_kind
        self.active_control_id = control_id
        self.active_job_instance_id = job_instance_id
        return SimpleNamespace(
            material_task_id=self.material_task_id,
            execution_id=self.execution_id,
            attempt_id=attempt_id,
            revision=self.expected_revision,
            payload=copy.deepcopy(replacement),
        )

    def release(self) -> None:
        self.events.append("release_lease")
        self.released = True


class _Runner:
    def __init__(self, prepared: PreparedMaterialAttempt, events: list[str]) -> None:
        self.prepared = prepared
        self.events = events

    def prepare_attempt(self, *, execution_id: str, operation: str):
        self.events.append("prepare")
        assert execution_id == self.prepared.execution_id
        return self.prepared

    def retire_unstarted_attempt(self, prepared: PreparedMaterialAttempt) -> None:
        assert prepared is self.prepared
        self.events.append("retire_unstarted")

    def bind_claim(self, prepared: PreparedMaterialAttempt, binding) -> None:
        assert prepared is self.prepared
        assert binding.attempt_id == prepared.attempt_id


class _Store:
    def __init__(self, lease: _Lease, events: list[str], error: Exception | None = None):
        self.lease = lease
        self.events = events
        self.error = error
        self.mutex = _Mutex(events)

    def acquire_claim_mutex(self, *, timeout_ms: int = 0):
        assert timeout_ms >= 0
        self.events.append("acquire_mutex")
        return self.mutex

    def claim_lease_with_owned_mutex(self, *, mutex, **values):
        self.events.append("claim")
        assert mutex is self.mutex
        mutex.assert_owned()
        if self.error is not None:
            raise self.error
        assert values["attempt_id"] == self.lease.active_attempt_id
        return self.lease


def _claim_payload(
    *,
    execution_id: str,
    attempt_id: str,
    control_id: str,
    job_instance_id: str,
    operation: str = "pexels",
) -> dict:
    return {
        "material_tasks": [
            {
                "material_task_id": "task-001",
                "shot_id": "shot-01",
                "status": "进行中",
                "status_history": ["等待", "进行中"],
            },
            {
                "material_task_id": "task-002",
                "shot_id": "shot-02",
                "status": "等待",
                "status_history": ["等待"],
            },
        ],
        "publication_phase": "preparing",
        "execution_state": "claimed",
        "execution_revision": 1,
        "execution_id": execution_id,
        "execution_started_at": "2026-07-16T00:00:00Z",
        "claim_key": {
            "pipeline_id": 1,
            "material_step_id": 2,
            "material_task_id": "task-001",
        },
        "active_material_task_id": "task-001",
        "active_execution_id": execution_id,
        "active_attempt_id": attempt_id,
        "active_attempt_kind": operation,
        "active_control_id": control_id,
        "active_job_instance_id": job_instance_id,
    }


def _claim_with_lease(lease: _Lease) -> PreparedMaterialClaim:
    prepared = _prepared(
        lease.execution_id,
        lease.active_attempt_id,
        lease.active_control_id,
        lease.active_job_instance_id,
    )
    binding = MaterialRunnerClaimBinding(
        material_task_id="task-001",
        execution_id=prepared.execution_id,
        operation=lease.active_attempt_kind,  # type: ignore[arg-type]
        attempt_id=prepared.attempt_id,
        control_id=prepared.control_id,
        job_instance_id=prepared.job_instance_id,
        execution_revision=lease.expected_revision,
        payload_sha256="b" * 64,
    )
    return PreparedMaterialClaim(prepared=prepared, binding=binding, lease=lease)


def _termination() -> TerminationEvidence:
    return TerminationEvidence(
        platform="windows_job",
        root_pid=1234,
        exit_code=0,
        termination_method="natural",
        active_processes=0,
        observed_at="2026-07-16T00:01:00Z",
    )


def _success_outcome(execution_id: str, attempt_id: str) -> MaterialOperationOutcome:
    artifact = StagedArtifact(
        relative_path="payload/selected.mp4",
        media_kind="video",
        source_type="pexels",
        source_url="https://example.com/video",
        evidence_kind="atmosphere",
        is_locally_generated=False,
        sha256="c" * 64,
        size_bytes=10,
    )
    result = MaterialWorkerResult(
        protocol_version=1,
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="pexels",
        status="succeeded",
        selected_artifact="payload/selected.mp4",
        artifacts=(artifact,),
        reason_code="",
        reason="",
    )
    return MaterialOperationOutcome(status="succeeded", result=result, termination=_termination())


def _stock_success_outcome(
    execution_id: str,
    attempt_id: str,
) -> MaterialOperationOutcome:
    specs = (
        ("Pexels", "101", "https://www.pexels.com/video/example-101/"),
        ("Pixabay", "202", "https://pixabay.com/videos/example-202/"),
        ("Pexels", "303", "https://www.pexels.com/video/example-303/"),
    )
    artifacts = tuple(
        StagedArtifact(
            relative_path=f"payload/candidate-{index}.mp4",
            media_kind="video",
            source_type=provider,
            source_url=url,
            evidence_kind="atmosphere",
            is_locally_generated=False,
            sha256=f"{index + 1:x}" * 64,
            size_bytes=10 + index,
            rights_evidence=build_stock_rights_evidence(
                provider=provider,
                provider_asset_id=provider_asset_id,
                media_type="video",
                asset_page_url=url,
                contributor_name=f"{provider} contributor {index}",
                search_query="office",
                observed_at="2026-07-18T09:30:00+00:00",
            ),
        )
        for index, (provider, provider_asset_id, url) in enumerate(specs)
    )
    result = MaterialWorkerResult(
        protocol_version=2,
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="stock_candidates",
        status="succeeded",
        selected_artifact="",
        artifacts=artifacts,
        reason_code="",
        reason="candidates ready",
    )
    return MaterialOperationOutcome(
        status="succeeded",
        result=result,
        termination=_termination(),
    )


def _published_candidate_batch(
    root: Path,
    *,
    id_offset: int = 0,
) -> MaterialCandidateBatchResult:
    specs = (
        ("Pexels", 101 + id_offset, "https://www.pexels.com/video/example-101/"),
        ("Pixabay", 202 + id_offset, "https://pixabay.com/videos/example-202/"),
        ("Pexels", 303 + id_offset, "https://www.pexels.com/video/example-303/"),
    )
    candidates: list[CandidateEntry] = []
    root.mkdir(parents=True, exist_ok=True)
    for index, (provider, provider_asset_id, url) in enumerate(specs):
        preview = root / f"candidate-{id_offset}-{index}.mp4"
        preview.write_bytes(f"candidate-{id_offset}-{index}".encode("ascii"))
        rights = build_stock_rights_evidence(
            provider=provider,
            provider_asset_id=provider_asset_id,
            media_type="video",
            asset_page_url=url,
            contributor_name=f"Creator {index}",
            search_query="office",
            observed_at="2026-07-18T09:30:00+00:00",
        )
        candidates.append(
            CandidateEntry(
                candidate_id=f"{provider.casefold()}-{provider_asset_id}",
                source=provider,
                preview_path=str(preview),
                url=url,
                rights=rights["license_snapshot"]["license_name"],
                evidence_kind="atmosphere",
                reason="waiting for review",
                rights_evidence=rights,
            )
        )
    return MaterialCandidateBatchResult(
        success=True,
        status="需要人工确认",
        candidates=tuple(candidates),
        reason="candidate batch published",
    )


def _install_reference_intent(
    payload: dict,
    *,
    claim: PreparedMaterialClaim,
    outcome: MaterialOperationOutcome,
    published_result: MaterialCandidateBatchResult | MaterialExecutionResult,
) -> dict:
    if type(published_result) is MaterialCandidateBatchResult:
        encoded_result = {
            "kind": "stock_candidate_batch",
            "success": published_result.success,
            "status": published_result.status,
            "reason": published_result.reason,
            "candidates": [item.to_dict() for item in published_result.candidates],
        }
    else:
        encoded_result = {
            "kind": "material_execution_result",
            "success": published_result.success,
            "status": published_result.status,
            "material_path": published_result.material_path,
            "material_type": published_result.material_type,
            "source_type": published_result.source_type,
            "reason": published_result.reason,
            "evidence_kind": published_result.evidence_kind,
            "source_url": published_result.source_url,
            "is_locally_generated": published_result.is_locally_generated,
            "rights_evidence": copy.deepcopy(published_result.rights_evidence),
        }
    assert outcome.result is not None
    intent = {
        "format": "material-publication-completion-intent-v1",
        "claim_receipt": {
            "material_task_id": claim.binding.material_task_id,
            "execution_id": claim.binding.execution_id,
            "attempt_id": claim.binding.attempt_id,
            "operation": claim.binding.operation,
            "control_id": claim.binding.control_id,
            "job_instance_id": claim.binding.job_instance_id,
            "reference_base_revision": claim.binding.execution_revision,
            "reference_base_payload_sha256": claim.binding.payload_sha256,
        },
        "worker_result_sha256": hashlib.sha256(
            encode_material_worker_result(outcome.result)
        ).hexdigest(),
        "published_result": encoded_result,
        "published_result_sha256": _canonical_sha(encoded_result),
    }
    intent["intent_sha256"] = _canonical_sha(intent)
    payload.update(
        {
            "publication_intent_sha256": intent["intent_sha256"],
            "publication_result_sha256": intent["published_result_sha256"],
            "publication_worker_result_sha256": intent["worker_result_sha256"],
            "publication_reference_base_revision": claim.binding.execution_revision,
            "publication_reference_base_payload_sha256": claim.binding.payload_sha256,
        }
    )
    return intent


def _failed_outcome(execution_id: str, attempt_id: str) -> MaterialOperationOutcome:
    result = MaterialWorkerResult(
        protocol_version=1,
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="pexels",
        status="failed",
        selected_artifact="",
        artifacts=(),
        reason_code="no_result",
        reason="no usable result",
    )
    return MaterialOperationOutcome(status="failed", result=result, termination=_termination())


def _terminated_outcome() -> MaterialOperationOutcome:
    return MaterialOperationOutcome(
        status="terminated",
        result=None,
        termination=TerminationEvidence(
            platform="windows_job",
            root_pid=1234,
            exit_code=1,
            termination_method="job_terminate",
            active_processes=0,
            observed_at="2026-07-16T00:01:00Z",
        ),
    )


def _retirement_receipt(
    receipt: MaterialCompletionReceipt,
    claim: PreparedMaterialClaim,
) -> MaterialAttemptRetirementReceipt:
    return MaterialAttemptRetirementReceipt(
        status=receipt.status,
        execution_id=receipt.execution_id,
        operation=receipt.operation,
        attempt_id=receipt.attempt_id,
        control_id=claim.binding.control_id,
        job_instance_id=claim.binding.job_instance_id,
        execution_revision=receipt.execution_revision,
        final_payload_sha256=receipt.final_payload_sha256,
        attempt_history_sha256=receipt.attempt_history_sha256 or "",
        result_sha256=receipt.result_sha256,
        observed_at=receipt.observed_at,
        retired_at="2026-07-16T00:02:00Z",
        tombstone_sha256="f" * 64,
    )


def test_prepare_and_claim_keeps_one_mutex_from_prepare_through_claim():
    events: list[str] = []
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    prepared = _prepared(execution_id, attempt_id, control_id, job_instance_id)
    payload = _claim_payload(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=payload,
        events=events,
    )
    store = _Store(lease, events)
    preparer = MaterialExecutionClaimPreparer(store=store, runner=_Runner(prepared, events))

    claim = preparer.prepare_and_claim(
        material_task_id="task-001",
        execution_id=execution_id,
        operation="pexels",
    )

    assert events == ["acquire_mutex", "prepare", "claim"]
    assert claim.lease is lease
    assert claim.binding.attempt_id == attempt_id
    assert claim.binding.payload_sha256 != ""
    assert store.mutex.released is False


def test_prepare_and_claim_known_conflict_retires_before_releasing_mutex():
    events: list[str] = []
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    prepared = _prepared(execution_id, attempt_id, control_id, job_instance_id)
    payload = _claim_payload(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=payload,
    )
    store = _Store(lease, events, MaterialExecutionClaimConflict("busy"))
    preparer = MaterialExecutionClaimPreparer(store=store, runner=_Runner(prepared, events))

    with pytest.raises(MaterialExecutionClaimConflict, match="busy"):
        preparer.prepare_and_claim(
            material_task_id="task-001",
            execution_id=execution_id,
            operation="pexels",
        )

    assert events == [
        "acquire_mutex",
        "prepare",
        "claim",
        "retire_unstarted",
        "release_mutex",
    ]


def test_prepare_and_claim_unknown_retains_mutex_and_prepared_evidence():
    events: list[str] = []
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    prepared = _prepared(execution_id, attempt_id, control_id, job_instance_id)
    payload = _claim_payload(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=payload,
    )
    store = _Store(lease, events, MaterialExecutionClaimUnknown("unknown"))
    preparer = MaterialExecutionClaimPreparer(store=store, runner=_Runner(prepared, events))

    with pytest.raises(MaterialPrepareAndClaimUnknown) as caught:
        preparer.prepare_and_claim(
            material_task_id="task-001",
            execution_id=execution_id,
            operation="pexels",
        )

    assert caught.value.recovery.prepared is prepared
    assert caught.value.recovery.mutex is store.mutex
    assert caught.value.recovery.ownership_retained is True
    assert events == ["acquire_mutex", "prepare", "claim"]


def test_prepare_and_claim_can_adopt_a_mutex_already_owned_by_the_owner_lane():
    events: list[str] = []
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    prepared = _prepared(execution_id, attempt_id, control_id, job_instance_id)
    payload = _claim_payload(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=payload,
    )
    store = _Store(lease, events)
    preparer = MaterialExecutionClaimPreparer(store=store, runner=_Runner(prepared, events))

    claim = preparer.prepare_and_claim_with_owned_mutex(
        mutex=store.mutex,
        material_task_id="task-001",
        execution_id=execution_id,
        operation="pexels",
    )

    assert claim.binding.attempt_id == attempt_id
    assert events == ["prepare", "claim"]
    assert store.mutex.released is False
    store.mutex.release()


def test_prepare_and_claim_rejects_unowned_mutex_before_creating_attempt():
    events: list[str] = []
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    prepared = _prepared(execution_id, attempt_id, control_id, job_instance_id)
    payload = _claim_payload(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=payload,
    )
    store = _Store(lease, events)

    class _UnownedMutex:
        def assert_owned(self) -> None:
            events.append("assert_owned")
            raise RuntimeError("different owner thread")

        def release(self) -> None:
            events.append("release_mutex")

    store.mutex = _UnownedMutex()  # type: ignore[assignment]
    preparer = MaterialExecutionClaimPreparer(
        store=store,
        runner=_Runner(prepared, events),
    )

    with pytest.raises(
        MaterialExecutionCoordinatorUnknown,
        match="not owned by the current thread",
    ):
        preparer.prepare_and_claim_with_owned_mutex(
            mutex=store.mutex,
            material_task_id="task-001",
            execution_id=execution_id,
            operation="pexels",
        )

    assert events == ["assert_owned"]


def test_success_completion_cas_updates_task_history_next_and_clears_owner():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    payload = _claim_payload(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=payload,
    )
    claim = _claim_with_lease(lease)
    outcome = _success_outcome(execution_id, attempt_id)
    published_result = MaterialExecutionResult(
        success=True,
        status="已完成",
        material_path="D:/project/素材/selected.mp4",
        material_type="视频",
        source_type="pexels",
        reason="素材发布完成",
        evidence_kind="atmosphere",
        source_url="https://example.com/video",
        is_locally_generated=False,
    )
    committed = lease.expected_payload
    committed.update(
        {
            "publication_phase": "db_committed",
            "execution_state": "publishing",
            "execution_revision": 2,
            "publication_journal": "D:/project/publication-journal.json",
        }
    )
    completion_intent = _install_reference_intent(
        committed,
        claim=claim,
        outcome=outcome,
        published_result=published_result,
    )
    lease._expected_payload = committed
    lease.expected_revision = 2
    publication = SimpleNamespace(
        phase="committed",
        ownership_retained=True,
        committed_journal_sha256="d" * 64,
        request=SimpleNamespace(
            execution_id=execution_id,
            attempt_id=attempt_id,
            operation="pexels",
        ),
        result=published_result,
        completion_intent=completion_intent,
    )

    receipt = SqlAlchemyMaterialLifecycleAdapter().complete_success(
        claim,
        outcome=outcome,
        publication=publication,
    )

    final = lease.expected_payload
    task = final["material_tasks"][0]
    assert task["status"] == "已完成"
    assert task["status_history"][-1] == "已完成"
    assert task["material_path"] == "D:/project/素材/selected.mp4"
    assert final["next_material_task_id"] == "task-002"
    assert final["material_tasks"][1]["status"] == "待执行"
    assert final["material_tasks"][1]["status_history"][-1] == "待执行"
    assert final["execution_state"] == "idle"
    assert final["active_material_task_id"] is None
    assert final["active_attempt_id"] is None
    assert final["execution_revision"] == 3
    assert final["material_attempt_history"][-1]["status"] == "succeeded"
    assert receipt.status == "succeeded"
    assert receipt.publication_journal_sha256 == "d" * 64
    assert receipt.execution_revision == 3
    checkpoint = pending_runner_retirement_from_payload(final)
    assert checkpoint.material_task_id == "task-001"
    assert checkpoint.execution_id == execution_id
    assert checkpoint.attempt_id == attempt_id
    assert checkpoint.completion_status == "succeeded"
    assert checkpoint.completion_revision == 3
    assert checkpoint.reference_revision == 2
    assert checkpoint.publication_journal_sha256 == "d" * 64
    assert receipt.runner_retirement_checkpoint_sha256 == checkpoint.checkpoint_sha256
    assert completion_receipt_from_pending_payload(final) == receipt

    attestation = MaterialRunnerRetirementAttestation(
        status="succeeded",
        execution_id=execution_id,
        operation="pexels",
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        completion_revision=receipt.execution_revision,
        completion_payload_sha256=receipt.final_payload_sha256,
        checkpoint_sha256=checkpoint.checkpoint_sha256,
        attempt_history_sha256=receipt.attempt_history_sha256 or "",
        tombstone_sha256="e" * 64,
        retired_at="2026-07-16T00:02:00Z",
        release_basis="in_process_close",
    )
    acknowledgement = SqlAlchemyMaterialLifecycleAdapter().acknowledge_runner_retirement(
        claim,
        completion=receipt,
        attestation=attestation,
    )
    acknowledged = lease.expected_payload
    assert acknowledgement.ack_revision == 4
    assert acknowledged["execution_revision"] == 4
    assert acknowledged["runner_retirement"]["state"] == "attested"
    assert (
        acknowledged["runner_retirement"]["attestation"]["tombstone_sha256"]
        == "e" * 64
    )


def test_stock_candidate_success_writes_pool_and_finishes_in_one_cas(
    tmp_path: Path,
):
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    payload = _claim_payload(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        operation="stock_candidates",
    )
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=payload,
    )
    claim = _claim_with_lease(lease)
    outcome = _stock_success_outcome(execution_id, attempt_id)
    committed = lease.expected_payload
    committed.update(
        {
            "publication_phase": "db_committed",
            "execution_state": "publishing",
            "execution_revision": 2,
            "publication_journal": "D:/project/publication-journal.json",
        }
    )
    batch = _published_candidate_batch(tmp_path / "published")
    completion_intent = _install_reference_intent(
        committed,
        claim=claim,
        outcome=outcome,
        published_result=batch,
    )
    lease._expected_payload = committed
    lease.expected_revision = 2
    publication = SimpleNamespace(
        phase="committed",
        ownership_retained=True,
        committed_journal_sha256="d" * 64,
        request=SimpleNamespace(
            execution_id=execution_id,
            attempt_id=attempt_id,
            operation="stock_candidates",
        ),
        result=batch,
        completion_intent=completion_intent,
    )

    receipt = SqlAlchemyMaterialLifecycleAdapter().complete_success(
        claim,
        outcome=outcome,
        publication=publication,
    )

    final = lease.expected_payload
    task = final["material_tasks"][0]
    state = CandidateSet.from_dict(final["candidate_pool"]["shot-01"])
    assert task["status"] == "需要人工确认"
    assert task["status_history"][-1] == "需要人工确认"
    assert "material_path" not in task
    assert "rights_evidence" not in task
    assert state.revision == 0
    assert state.candidates == batch.candidates
    assert final["candidate_generation_history"][-1]["shot_id"] == "shot-01"
    assert final["candidate_generation_history"][-1]["candidate_count"] == 3
    assert final["next_material_task_id"] == "task-002"
    assert lease.events.count("completion_cas") == 1
    assert receipt.status == "succeeded"


def test_stock_candidate_success_rejects_locked_pool_before_completion_cas(
    tmp_path: Path,
):
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    existing_batch = _published_candidate_batch(tmp_path / "existing")
    locked_rows = list(existing_batch.candidates)
    locked_rows[0] = replace(
        locked_rows[0],
        status="locked",
        reason="Ian selected this candidate",
    )
    locked_state = CandidateSet.from_candidates("shot-01", locked_rows)
    payload = _claim_payload(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        operation="stock_candidates",
    )
    payload["candidate_pool"] = {"shot-01": locked_state.to_dict()}
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=payload,
    )
    claim = _claim_with_lease(lease)
    outcome = _stock_success_outcome(execution_id, attempt_id)
    committed = lease.expected_payload
    committed.update(
        {
            "publication_phase": "db_committed",
            "execution_state": "publishing",
            "execution_revision": 2,
            "publication_journal": "D:/project/publication-journal.json",
        }
    )
    replacement_batch = _published_candidate_batch(
        tmp_path / "replacement",
        id_offset=1000,
    )
    completion_intent = _install_reference_intent(
        committed,
        claim=claim,
        outcome=outcome,
        published_result=replacement_batch,
    )
    lease._expected_payload = committed
    lease.expected_revision = 2
    before = lease.expected_payload
    publication = SimpleNamespace(
        phase="committed",
        ownership_retained=True,
        committed_journal_sha256="d" * 64,
        request=SimpleNamespace(
            execution_id=execution_id,
            attempt_id=attempt_id,
            operation="stock_candidates",
        ),
        result=replacement_batch,
        completion_intent=completion_intent,
    )

    with pytest.raises(MaterialExecutionCoordinatorUnknown, match="locked"):
        SqlAlchemyMaterialLifecycleAdapter().complete_success(
            claim,
            outcome=outcome,
            publication=publication,
        )

    assert lease.expected_payload == before
    assert "completion_cas" not in lease.events


def test_stock_candidate_unlocked_regeneration_advances_pool_revision(
    tmp_path: Path,
):
    existing = _published_candidate_batch(tmp_path / "existing")
    replacement_batch = _published_candidate_batch(
        tmp_path / "replacement",
        id_offset=1000,
    )
    state = CandidateSet.from_candidates("shot-01", existing.candidates)
    payload = {"candidate_pool": {"shot-01": state.to_dict()}}
    task = {
        "material_task_id": "task-001",
        "shot_id": "shot-01",
        "status": "进行中",
        "status_history": ["进行中"],
    }
    claim = SimpleNamespace(
        binding=SimpleNamespace(
            material_task_id="task-001",
            execution_id=str(uuid4()),
            attempt_id=str(uuid4()),
        )
    )

    lifecycle_subject._apply_stock_candidate_batch(
        payload,
        task,
        replacement_batch,
        claim=claim,
        observed_at="2026-07-18T10:00:00Z",
    )

    refreshed = CandidateSet.from_dict(payload["candidate_pool"]["shot-01"])
    assert refreshed.revision == 1
    assert refreshed.candidates == replacement_batch.candidates


def test_success_completion_rejects_missing_publication_reference_cas():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    payload = _claim_payload(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=payload,
    )
    claim = _claim_with_lease(lease)
    publication = SimpleNamespace(
        phase="committed",
        ownership_retained=True,
        committed_journal_sha256="d" * 64,
        request=SimpleNamespace(
            execution_id=execution_id,
            attempt_id=attempt_id,
            operation="pexels",
        ),
        result=SimpleNamespace(success=True),
    )

    with pytest.raises(
        MaterialExecutionCoordinatorUnknown,
        match="exact phase",
    ):
        SqlAlchemyMaterialLifecycleAdapter().complete_success(
            claim,
            outcome=_success_outcome(execution_id, attempt_id),
            publication=publication,
        )

    assert "completion_cas" not in lease.events


def test_success_completion_rejects_a_skipped_reference_revision():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    payload = _claim_payload(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=payload,
    )
    claim = _claim_with_lease(lease)
    jumped = lease.expected_payload
    jumped.update(
        {
            "publication_phase": "db_committed",
            "execution_state": "publishing",
            "execution_revision": 3,
            "publication_journal": "D:/project/publication-journal.json",
        }
    )
    lease._expected_payload = jumped
    lease.expected_revision = 3
    publication = SimpleNamespace(
        phase="committed",
        ownership_retained=True,
        committed_journal_sha256="d" * 64,
        request=SimpleNamespace(
            execution_id=execution_id,
            attempt_id=attempt_id,
            operation="pexels",
        ),
        result=SimpleNamespace(success=True),
    )

    with pytest.raises(MaterialExecutionCoordinatorUnknown, match="exact phase"):
        SqlAlchemyMaterialLifecycleAdapter().complete_success(
            claim,
            outcome=_success_outcome(execution_id, attempt_id),
            publication=publication,
        )

    assert "completion_cas" not in lease.events


def test_terminal_completion_cas_writes_failure_history_and_next_task():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    payload = _claim_payload(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=payload,
    )
    claim = _claim_with_lease(lease)

    receipt = SqlAlchemyMaterialLifecycleAdapter().complete_terminal(
        claim,
        outcome=_failed_outcome(execution_id, attempt_id),
    )

    final = lease.expected_payload
    task = final["material_tasks"][0]
    assert task["status"] == "失败"
    assert task["reject_reason"] == "no usable result"
    assert final["publication_phase"] == "rolled_back"
    assert final["execution_state"] == "idle"
    assert final["active_execution_id"] is None
    assert final["next_material_task_id"] == "task-002"
    assert final["material_tasks"][1]["status"] == "待执行"
    assert final["material_tasks"][1]["status_history"][-1] == "待执行"
    assert receipt.status == "failed"
    assert receipt.attempt_history_sha256 is not None
    assert final["material_attempt_history"][-1]["entry_sha256"] == receipt.attempt_history_sha256
    checkpoint = pending_runner_retirement_from_payload(final)
    assert checkpoint.completion_status == "failed"
    assert checkpoint.reference_revision == 1
    assert checkpoint.publication_journal_sha256 is None
    assert checkpoint.publication_intent_sha256 is None
    assert receipt.runner_retirement_checkpoint_sha256 == checkpoint.checkpoint_sha256


def test_terminated_completion_installs_a_pending_runner_retirement_checkpoint():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    payload = _claim_payload(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=payload,
    )
    receipt = SqlAlchemyMaterialLifecycleAdapter().complete_terminal(
        _claim_with_lease(lease),
        outcome=_terminated_outcome(),
    )

    checkpoint = pending_runner_retirement_from_payload(lease.expected_payload)
    assert checkpoint.completion_status == "terminated"
    assert checkpoint.result_sha256 is None
    assert checkpoint.attempt_history_sha256 == receipt.attempt_history_sha256
    assert receipt.runner_retirement_checkpoint_sha256 == checkpoint.checkpoint_sha256


def test_terminal_rotation_keeps_execution_and_records_terminal_history():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    payload = _claim_payload(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=payload,
    )
    claim = _claim_with_lease(lease)
    adapter = SqlAlchemyMaterialLifecycleAdapter()
    terminal_receipt = adapter.record_terminal_attempt_for_fallback(
        claim,
        outcome=_failed_outcome(execution_id, attempt_id),
    )
    next_attempt_id, next_control_id, next_job_instance_id, _ = _ids()
    cleared = lease.expected_payload
    assert cleared["execution_id"] == execution_id
    assert cleared["execution_state"] == "running"
    assert cleared["publication_phase"] == "rolled_back"
    assert cleared["attempt_transition"] == "retiring"
    assert cleared["active_attempt_id"] is None

    receipt = adapter.rotate_terminal_attempt(
        claim,
        terminal_receipt=terminal_receipt,
        retirement_receipt=_retirement_receipt(terminal_receipt, claim),
        attempt_id=next_attempt_id,
        attempt_kind="pixabay",
        control_id=next_control_id,
        job_instance_id=next_job_instance_id,
    )

    final = lease.expected_payload
    task = final["material_tasks"][0]
    assert receipt.status == "failed"
    assert receipt.execution_id == execution_id
    assert receipt.terminal_attempt_id == attempt_id
    assert receipt.next_attempt_id == next_attempt_id
    assert final["execution_id"] == execution_id
    assert final["execution_state"] == "claimed"
    assert final["publication_phase"] == "preparing"
    assert final["active_execution_id"] == execution_id
    assert final["active_attempt_id"] == next_attempt_id
    assert final["active_attempt_kind"] == "pixabay"
    assert task["status"] == "进行中"
    assert task["status_history"][-2:] == ["失败", "进行中"]
    assert final["material_attempt_history"][-1]["attempt_id"] == attempt_id
    assert final["material_attempt_history"][-1]["status"] == "failed"
    assert final["execution_revision"] == 3
    assert lease.events.count("clear_rotation_cas") == 1
    assert lease.events.count("rotation_cas") == 1
    assert "completion_cas" not in lease.events


def test_terminated_rotation_keeps_execution_active_for_next_attempt():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=_claim_payload(
            execution_id=execution_id,
            attempt_id=attempt_id,
            control_id=control_id,
            job_instance_id=job_instance_id,
        ),
    )
    claim = _claim_with_lease(lease)
    next_attempt_id, next_control_id, next_job_instance_id, _ = _ids()

    adapter = SqlAlchemyMaterialLifecycleAdapter()
    terminal_receipt = adapter.record_terminal_attempt_for_fallback(
        claim,
        outcome=_terminated_outcome(),
    )
    assert terminal_receipt.status == "terminated"
    assert lease.expected_payload["execution_id"] == execution_id
    assert lease.expected_payload["active_attempt_id"] is None

    rotation = adapter.rotate_terminal_attempt(
        claim,
        terminal_receipt=terminal_receipt,
        retirement_receipt=_retirement_receipt(terminal_receipt, claim),
        attempt_id=next_attempt_id,
        attempt_kind="pixabay",
        control_id=next_control_id,
        job_instance_id=next_job_instance_id,
    )

    assert rotation.status == "terminated"
    assert lease.expected_payload["execution_id"] == execution_id
    assert lease.expected_payload["active_attempt_id"] == next_attempt_id
    assert lease.expected_payload["material_attempt_history"][-1]["status"] == "terminated"


def test_exhausted_fallback_finalizes_only_after_runner_retirement_receipt():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=_claim_payload(
            execution_id=execution_id,
            attempt_id=attempt_id,
            control_id=control_id,
            job_instance_id=job_instance_id,
        ),
    )
    claim = _claim_with_lease(lease)
    adapter = SqlAlchemyMaterialLifecycleAdapter()
    terminal_receipt = adapter.record_terminal_attempt_for_fallback(
        claim,
        outcome=_failed_outcome(execution_id, attempt_id),
    )
    retirement_receipt = _retirement_receipt(terminal_receipt, claim)

    completion = adapter.finalize_terminal_attempt_after_fallback(
        claim,
        terminal_receipt=terminal_receipt,
        retirement_receipt=retirement_receipt,
    )

    final = lease.expected_payload
    assert completion.status == "failed"
    assert completion.execution_revision == 3
    assert final["execution_state"] == "idle"
    assert final["publication_phase"] == "rolled_back"
    assert final["attempt_transition"] is None
    assert final["active_attempt_id"] is None
    assert final["material_tasks"][0]["status"] == "失败"
    assert final["material_tasks"][1]["status"] == "待执行"
    assert final["next_material_task_id"] == "task-002"


def test_terminal_rotation_rejects_successful_outcome_without_cas():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=_claim_payload(
            execution_id=execution_id,
            attempt_id=attempt_id,
            control_id=control_id,
            job_instance_id=job_instance_id,
        ),
    )
    claim = _claim_with_lease(lease)
    next_attempt_id, next_control_id, next_job_instance_id, _ = _ids()

    with pytest.raises(MaterialExecutionCoordinatorUnknown, match="failed or terminated"):
        SqlAlchemyMaterialLifecycleAdapter().complete_terminal_for_fallback(
            claim,
            outcome=_success_outcome(execution_id, attempt_id),
        )
    assert "rotation_cas" not in lease.events


def test_terminal_rotation_rejects_blocked_outcome_without_cas():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=_claim_payload(
            execution_id=execution_id,
            attempt_id=attempt_id,
            control_id=control_id,
            job_instance_id=job_instance_id,
        ),
    )
    claim = _claim_with_lease(lease)
    next_attempt_id, next_control_id, next_job_instance_id, _ = _ids()

    with pytest.raises(TypeError, match="MaterialOperationOutcome"):
        SqlAlchemyMaterialLifecycleAdapter().complete_terminal_for_fallback(
            claim,
            outcome=SimpleNamespace(status="blocked"),
        )
    assert "rotation_cas" not in lease.events


def test_terminal_rotation_rejects_post_reference_state_without_cas():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=_claim_payload(
            execution_id=execution_id,
            attempt_id=attempt_id,
            control_id=control_id,
            job_instance_id=job_instance_id,
        ),
    )
    post_reference = lease.expected_payload
    post_reference.update(
        {
            "publication_phase": "db_committed",
            "execution_state": "publishing",
            "execution_revision": 2,
        }
    )
    lease._expected_payload = post_reference
    lease.expected_revision = 2
    claim = _claim_with_lease(lease)
    next_attempt_id, next_control_id, next_job_instance_id, _ = _ids()

    with pytest.raises(MaterialExecutionCoordinatorUnknown, match="exact phase"):
        SqlAlchemyMaterialLifecycleAdapter().complete_terminal_for_fallback(
            claim,
            outcome=_failed_outcome(execution_id, attempt_id),
        )
    assert "rotation_cas" not in lease.events


@pytest.mark.parametrize("reuse", ["attempt", "control", "job"])
def test_terminal_rotation_rejects_reused_attempt_identity(reuse):
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=_claim_payload(
            execution_id=execution_id,
            attempt_id=attempt_id,
            control_id=control_id,
            job_instance_id=job_instance_id,
        ),
    )
    claim = _claim_with_lease(lease)
    adapter = SqlAlchemyMaterialLifecycleAdapter()
    terminal_receipt = adapter.record_terminal_attempt_for_fallback(
        claim,
        outcome=_failed_outcome(execution_id, attempt_id),
    )
    next_attempt_id, next_control_id, next_job_instance_id, _ = _ids()
    if reuse == "attempt":
        next_attempt_id = attempt_id
    elif reuse == "control":
        next_control_id = control_id
    else:
        next_job_instance_id = job_instance_id

    with pytest.raises(MaterialExecutionCoordinatorUnknown, match="authorize"):
        adapter.rotate_terminal_attempt(
            claim,
            terminal_receipt=terminal_receipt,
            retirement_receipt=_retirement_receipt(terminal_receipt, claim),
            attempt_id=next_attempt_id,
            attempt_kind="pixabay",
            control_id=next_control_id,
            job_instance_id=next_job_instance_id,
        )
    assert "rotation_cas" not in lease.events


def test_real_terminal_rotation_fresh_reads_one_execution_owner(real_claim_store):
    store, _factory, _key = real_claim_store
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    lease = store.claim_lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        attempt_kind="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    claim = _claim_with_lease(lease)
    next_attempt_id, next_control_id, next_job_instance_id, _ = _ids()
    try:
        adapter = SqlAlchemyMaterialLifecycleAdapter()
        terminal_receipt = adapter.record_terminal_attempt_for_fallback(
            claim,
            outcome=_failed_outcome(execution_id, attempt_id),
        )
        receipt = adapter.rotate_terminal_attempt(
            claim,
            terminal_receipt=terminal_receipt,
            retirement_receipt=_retirement_receipt(terminal_receipt, claim),
            attempt_id=next_attempt_id,
            attempt_kind="pixabay",
            control_id=next_control_id,
            job_instance_id=next_job_instance_id,
        )
        fresh = store.fresh_read()
        assert receipt.execution_revision == 3
        assert fresh["execution_id"] == execution_id
        assert fresh["active_execution_id"] == execution_id
        assert fresh["active_attempt_id"] == next_attempt_id
        assert fresh["active_control_id"] == next_control_id
        assert fresh["active_job_instance_id"] == next_job_instance_id
        assert fresh["execution_state"] == "claimed"
        assert fresh["material_tasks"][0]["status"] == "进行中"
        assert fresh["material_attempt_history"][-1]["attempt_id"] == attempt_id
    finally:
        lease.release()


def test_terminal_and_blocked_transitions_reject_post_reference_state():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    payload = _claim_payload(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=payload,
    )
    claim = _claim_with_lease(lease)
    post_reference = lease.expected_payload
    post_reference.update(
        {
            "publication_phase": "db_committed",
            "execution_state": "publishing",
            "execution_revision": 2,
        }
    )
    lease._expected_payload = post_reference
    lease.expected_revision = 2

    with pytest.raises(MaterialExecutionCoordinatorUnknown, match="exact phase"):
        SqlAlchemyMaterialLifecycleAdapter().complete_terminal(
            claim,
            outcome=_failed_outcome(execution_id, attempt_id),
        )
    with pytest.raises(MaterialExecutionCoordinatorUnknown, match="exact phase"):
        SqlAlchemyMaterialLifecycleAdapter().record_blocked(
            claim,
            reason="must not roll publishing state backward",
        )

    assert "completion_cas" not in lease.events


def test_blocked_cas_keeps_owner_and_marks_system_blocked_state():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    payload = _claim_payload(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    lease = _Lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        payload=payload,
    )
    claim = _claim_with_lease(lease)

    SqlAlchemyMaterialLifecycleAdapter().record_blocked(
        claim,
        reason="durable evidence needs reconciliation",
    )

    final = lease.expected_payload
    assert final["execution_state"] == "blocked"
    assert final["publication_phase"] == "audit_required"
    assert final["active_attempt_id"] == attempt_id
    assert final["material_tasks"][0]["status"] == "被阻止"
    assert final["material_tasks"][0]["status_history"][-1] == "被阻止"


def test_claim_lease_publication_database_advances_the_real_lease_token(
    real_claim_store,
):
    store, _factory, _key = real_claim_store
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    lease = store.claim_lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        attempt_kind="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    database = ClaimLeasePublicationDatabase(lease)
    expected = database.fresh_read()
    replacement = copy.deepcopy(expected)
    replacement.update(
        {
            "publication_phase": "db_committed",
            "execution_state": "publishing",
            "publication_journal": "D:/project/publication-journal.json",
            "execution_revision": expected["execution_revision"] + 1,
        }
    )
    try:
        assert database.compare_and_swap(expected, replacement) is True
        assert lease.expected_revision == 2
        assert lease.expected_payload == replacement
        assert store.fresh_read() == replacement
    finally:
        lease.release()


def test_real_lease_publisher_and_success_completion_share_one_cas_token(
    real_claim_store,
    tmp_path: Path,
):
    store, _factory, _key = real_claim_store
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    lease = store.claim_lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        attempt_kind="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    attempt_path = tmp_path / "attempt"
    (attempt_path / "payload").mkdir(parents=True)
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"lease-bound-publication")
    attempt = approve_physical_d_write_root(attempt_path)
    prepared = PreparedMaterialAttempt(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        control_directory=attempt,
        directory=attempt,
        pin_scope=(attempt,),
        container_hash="a" * 64,
    )
    binding = MaterialRunnerClaimBinding(
        material_task_id="task-001",
        execution_id=execution_id,
        operation="pexels",
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        execution_revision=lease.expected_revision,
        payload_sha256="b" * 64,
    )
    claim = PreparedMaterialClaim(prepared=prepared, binding=binding, lease=lease)
    request = MaterialOperationRequest(
        protocol_version=1,
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="pexels",
        payload=StockSearchPayload(query="office", per_page=1),
    )
    artifact = StagedArtifact(
        relative_path="payload/clip.mp4",
        media_kind="video",
        source_type="pexels",
        source_url="https://example.com/clip",
        evidence_kind="atmosphere",
        is_locally_generated=False,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        size_bytes=source.stat().st_size,
    )
    worker_result = MaterialWorkerResult(
        protocol_version=1,
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="pexels",
        status="succeeded",
        selected_artifact="payload/clip.mp4",
        artifacts=(artifact,),
        reason_code="",
        reason="",
    )
    outcome = MaterialOperationOutcome(
        status="succeeded",
        result=worker_result,
        termination=_termination(),
    )
    canonical = AutoMaterialPaths(root=tmp_path / "canonical")
    canonical.ensure_directories()
    project_guards: dict[str, _ProjectGuard] = {}
    for name in ("Pexels素材", "官方录屏", "信息动效", "全部素材", "素材信息"):
        path = tmp_path / "project" / name
        path.mkdir(parents=True, exist_ok=True)
        project_guards[name] = _ProjectGuard(path)
    publisher = CanonicalMaterialArtifactPublisher(
        canonical_paths=canonical,
        database=ClaimLeasePublicationDatabase(lease),
        expected_payload=lease.expected_payload,
        registry_mutex=_RegistryMutex(),
    )
    publication = None
    try:
        publication = publisher.prepare(
            request,
            attempt,
            worker_result,
            project_directory_uses=project_guards,
        )
        publication.commit(retain_ownership=True)
        assert lease.expected_revision == 2
        assert lease.expected_payload["publication_phase"] == "db_committed"

        receipt = SqlAlchemyMaterialLifecycleAdapter().complete_success(
            claim,
            outcome=outcome,
            publication=publication,
        )
        assert receipt.execution_revision == 3
        assert store.fresh_read()["material_tasks"][0]["status"] == "已完成"
        assert store.fresh_read()["active_attempt_id"] is None
    finally:
        if publication is not None and publication.ownership_retained:
            publication.release_after_retirement()
        lease.release()


def test_real_restart_recovery_reuses_published_files_and_completes_once(
    real_claim_store,
    tmp_path: Path,
):
    store, _factory, _key = real_claim_store
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    original_lease = store.claim_lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        attempt_kind="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    attempt_path = tmp_path / "restart-attempt"
    (attempt_path / "payload").mkdir(parents=True)
    source = attempt_path / "payload" / "clip.mp4"
    source.write_bytes(b"restart-lease-bound-publication")
    attempt = approve_physical_d_write_root(attempt_path)
    request = MaterialOperationRequest(
        protocol_version=1,
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="pexels",
        payload=StockSearchPayload(query="office", per_page=1),
    )
    artifact = StagedArtifact(
        relative_path="payload/clip.mp4",
        media_kind="video",
        source_type="pexels",
        source_url="https://example.com/clip",
        evidence_kind="atmosphere",
        is_locally_generated=False,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        size_bytes=source.stat().st_size,
    )
    worker_result = MaterialWorkerResult(
        protocol_version=1,
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="pexels",
        status="succeeded",
        selected_artifact="payload/clip.mp4",
        artifacts=(artifact,),
        reason_code="",
        reason="",
    )
    outcome = MaterialOperationOutcome(
        status="succeeded",
        result=worker_result,
        termination=_termination(),
    )
    canonical = AutoMaterialPaths(root=tmp_path / "restart-canonical")
    canonical.ensure_directories()
    project_guards: dict[str, _ProjectGuard] = {}
    for name in ("Pexels素材", "官方录屏", "信息动效", "全部素材", "素材信息"):
        path = tmp_path / "restart-project" / name
        path.mkdir(parents=True, exist_ok=True)
        project_guards[name] = _ProjectGuard(path)

    first = CanonicalMaterialArtifactPublisher(
        canonical_paths=canonical,
        database=ClaimLeasePublicationDatabase(original_lease),
        expected_payload=original_lease.expected_payload,
        registry_mutex=_RegistryMutex(),
    )
    first_publication = first.prepare(
        request,
        attempt,
        worker_result,
        project_directory_uses=project_guards,
    )
    first_publication.commit(retain_ownership=True)
    assert original_lease.expected_revision == 2
    assert store.fresh_read()["publication_phase"] == "db_committed"
    first_publication.release_after_retirement()
    original_lease.release()

    counts_before = {
        "canonical": len(list(canonical.pexels_video_dir.glob("*.mp4"))),
        "project": len(list(project_guards["全部素材"].path.glob("*.mp4"))),
    }
    recovered_attempt = approve_physical_d_write_root(attempt_path)
    prepared = PreparedMaterialAttempt(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        control_directory=recovered_attempt,
        directory=recovered_attempt,
        pin_scope=(recovered_attempt,),
        container_hash="a" * 64,
    )

    class _Runner:
        def __init__(self):
            self.retirements = 0

        def adopt_durable_attempt(self, **kwargs):
            assert kwargs["binding"].execution_revision == 2
            return SimpleNamespace(prepared=prepared, outcome=outcome)

        def retire_adopted_terminal_attempt(self, *_args, **_kwargs):
            raise AssertionError("successful recovery must not use terminal retirement")

        def retire_adopted_published_attempt(
            self,
            adopted,
            *,
            proof,
            publication_ownership,
        ):
            assert adopted.prepared is prepared
            assert proof.execution_revision == 3
            publication_ownership.assert_retained_ownership()
            self.retirements += 1
            return MaterialRunnerRetirementAttestation(
                status="succeeded",
                execution_id=execution_id,
                operation="pexels",
                attempt_id=attempt_id,
                control_id=control_id,
                job_instance_id=job_instance_id,
                completion_revision=proof.execution_revision,
                completion_payload_sha256=proof.final_payload_sha256,
                checkpoint_sha256=proof.checkpoint_sha256,
                attempt_history_sha256=proof.attempt_history_sha256,
                tombstone_sha256="9" * 64,
                retired_at="2026-07-18T00:02:00+00:00",
                release_basis="dead_controller_after_tombstone",
            )

    runner = _Runner()
    recovered_publications: list[CanonicalMaterialPublication] = []

    def recover_publication(claim, adopted):
        assert adopted.outcome is outcome
        publisher = CanonicalMaterialArtifactPublisher(
            canonical_paths=canonical,
            database=ClaimLeasePublicationDatabase(claim.lease),
            expected_payload=claim.lease.expected_payload,
            registry_mutex=_RegistryMutex(),
        )
        publication = publisher.adopt_committed_publication(
            request,
            claim.prepared.directory,
            worker_result,
            project_directory_uses=project_guards,
        )
        recovered_publications.append(publication)
        return publication

    recovery_mutex = store.acquire_recovery_mutex(timeout_ms=0)
    completion = MaterialRestartRecoveryCoordinator(
        store=store,
        runner=runner,
        lifecycle=SqlAlchemyMaterialLifecycleAdapter(),
        publication_recovery_factory=recover_publication,
    ).complete_published_attempt(
        mutex=recovery_mutex,
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        operation="pexels",
    )

    final = store.fresh_read()
    assert completion.status == "succeeded"
    assert completion.execution_revision == 3
    assert runner.retirements == 1
    assert final["material_tasks"][0]["status"] == "已完成"
    assert len(final["material_attempt_history"]) == 1
    assert final["active_attempt_id"] is None
    assert final["execution_revision"] == 4
    assert final["runner_retirement"]["state"] == "attested"
    assert counts_before == {
        "canonical": len(list(canonical.pexels_video_dir.glob("*.mp4"))),
        "project": len(list(project_guards["全部素材"].path.glob("*.mp4"))),
    }
    assert recovered_publications[0].ownership_retained is False

    retry_mutex = store.acquire_recovery_mutex(timeout_ms=0)
    try:
        with pytest.raises(MaterialExecutionClaimConflict):
            store.adopt_publishing_lease_with_owned_recovery_mutex(
                mutex=retry_mutex,
                material_task_id="task-001",
                execution_id=execution_id,
                attempt_id=attempt_id,
                attempt_kind="pexels",
                control_id=control_id,
                job_instance_id=job_instance_id,
            )
    finally:
        retry_mutex.release()


def test_terminal_completion_uses_real_fresh_session_and_rejects_stale_blob(
    real_claim_store,
):
    store, factory, key = real_claim_store
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    lease = store.claim_lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        attempt_kind="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    claim = _claim_with_lease(lease)
    stale_session = factory()
    try:
        stale_step = stale_session.get(PipelineStep, key.material_step_id)
        assert stale_step is not None
        stale_payload = copy.deepcopy(stale_step.output_summary)
        assert stale_payload["execution_state"] == "claimed"

        receipt = SqlAlchemyMaterialLifecycleAdapter().complete_terminal(
            claim,
            outcome=_failed_outcome(execution_id, attempt_id),
        )
        fresh = store.fresh_read()
        assert receipt.execution_revision == 2
        assert fresh["execution_state"] == "idle"
        assert fresh["material_tasks"][0]["status"] == "失败"
        # The request-scoped identity map remains stale, proving it was not the
        # source of the completion ownership decision.
        assert stale_step.output_summary["execution_state"] == "claimed"
    finally:
        lease.release()
        stale_session.close()

    stale_replacement = copy.deepcopy(stale_payload)
    stale_replacement["execution_revision"] = stale_payload["execution_revision"] + 1
    stale_replacement["execution_state"] = "running"
    assert store.compare_and_swap(stale_payload, stale_replacement) is False


def test_restart_terminal_recovery_rejects_legacy_completion_without_checkpoint():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    observed_at = "2026-07-16T08:00:00.000000Z"
    history_base = {
        "material_task_id": "task-001",
        "execution_id": execution_id,
        "operation": "pexels",
        "attempt_id": attempt_id,
        "control_id": control_id,
        "job_instance_id": job_instance_id,
        "status": "terminated",
        "observed_at": observed_at,
        "result_sha256": None,
        "publication_journal_sha256": None,
        "reason_code": "worker_terminated",
        "reason": "terminated",
    }
    history_entry = dict(history_base)
    history_entry["entry_sha256"] = _canonical_sha(history_base)
    payload = {
        "material_tasks": [
            {
                "material_task_id": "task-001",
                "status": "澶辫触",
                "status_history": ["澶辫触"],
                "material_path": "",
            }
        ],
        "execution_id": execution_id,
        "execution_revision": 3,
        "execution_state": "idle",
        "publication_phase": "rolled_back",
        "last_execution_status": "terminated",
        "execution_completed_at": observed_at,
        "material_attempt_history": [history_entry],
        "active_material_task_id": None,
        "active_execution_id": None,
        "active_attempt_id": None,
        "active_attempt_kind": None,
        "active_control_id": None,
        "active_job_instance_id": None,
    }
    completion = lifecycle_subject.MaterialCompletionReceipt(
        status="terminated",
        execution_id=execution_id,
        operation="pexels",
        attempt_id=attempt_id,
        execution_revision=3,
        final_payload_sha256=_canonical_sha(payload),
        attempt_history_sha256=history_entry["entry_sha256"],
        result_sha256=None,
        observed_at=observed_at,
    )

    class _Store:
        def fresh_read(self):
            return copy.deepcopy(payload)

    class _Mutex:
        def __init__(self):
            self.released = False
            self.assertions = 0

        def assert_owned(self):
            assert not self.released
            self.assertions += 1

        def release(self):
            self.released = True

    class _Runner:
        def __init__(self):
            self.adopted = None
            self.retired = None

        def adopt_durable_attempt(self, **kwargs):
            self.adopted = kwargs
            return object()

        def retire_adopted_terminal_attempt(self, adopted, *, proof):
            self.retired = (adopted, proof)

    mutex = _Mutex()
    runner = _Runner()
    with pytest.raises(MaterialRestartRecoveryUnknown, match="checkpoint"):
        MaterialRestartRecoveryCoordinator(
            store=_Store(),
            runner=runner,
        ).retire_terminal_attempt(
            mutex=mutex,
            material_task_id="task-001",
            execution_id=execution_id,
            attempt_id=attempt_id,
            control_id=control_id,
            job_instance_id=job_instance_id,
            operation="pexels",
            completion=completion,
        )

    assert runner.adopted is None
    assert runner.retired is None
    assert mutex.released is False


def test_restart_recovery_completes_published_owner_without_rerunning_worker():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    observed_at = "2026-07-16T08:00:00.000000Z"
    events: list[str] = []
    publishing_payload = {
        "material_tasks": [
            {
                "material_task_id": "task-001",
                "status": "发布收口中",
                "status_history": ["进行中", "发布收口中"],
            }
        ],
        "execution_id": execution_id,
        "execution_started_at": observed_at,
        "claim_key": "claim-001",
        "execution_revision": 8,
        "execution_state": "publishing",
        "publication_phase": "db_committed",
        "publication_journal": "D:\\recovery\\publication-journal.json",
        "publication_intent_sha256": "1" * 64,
        "publication_result_sha256": "2" * 64,
        "publication_worker_result_sha256": "3" * 64,
        "publication_reference_base_revision": 7,
        "publication_reference_base_payload_sha256": "4" * 64,
        "active_material_task_id": "task-001",
        "active_execution_id": execution_id,
        "active_attempt_id": attempt_id,
        "active_attempt_kind": "pexels",
        "active_control_id": control_id,
        "active_job_instance_id": job_instance_id,
    }
    outcome = _success_outcome(execution_id, attempt_id)
    assert outcome.result is not None
    result_sha256 = hashlib.sha256(
        encode_material_worker_result(outcome.result)
    ).hexdigest()
    publishing_payload["publication_worker_result_sha256"] = result_sha256
    journal_sha256 = "5" * 64

    class _Mutex:
        def __init__(self):
            self.released = False

        def assert_owned(self):
            assert not self.released
            events.append("assert_mutex")

        def release(self):
            assert not self.released
            self.released = True
            events.append("release_lease")

    class _Lease:
        def __init__(self, store, mutex):
            self.store = store
            self.mutex = mutex
            self.execution_id = execution_id
            self.expected_revision = 8
            self.expected_payload = copy.deepcopy(publishing_payload)
            self.active_attempt_kind = "pexels"
            self.active_attempt_id = attempt_id
            self.active_control_id = control_id
            self.active_job_instance_id = job_instance_id

        def assert_owned(self, _session=None):
            self.mutex.assert_owned()
            assert self.store.payload == self.expected_payload

        def release(self):
            self.mutex.release()

    class _Store:
        def __init__(self, mutex):
            self.payload = copy.deepcopy(publishing_payload)
            self.lease = _Lease(self, mutex)

        def assert_recovery_mutex(self, mutex):
            mutex.assert_owned()

        def adopt_publishing_lease_with_owned_recovery_mutex(self, **kwargs):
            assert kwargs["mutex"] is mutex
            events.append("adopt_lease")
            return self.lease

        def fresh_read(self):
            events.append("fresh_read")
            return copy.deepcopy(self.payload)

    prepared = PreparedMaterialAttempt(
        execution_id=execution_id,
        attempt_id=attempt_id,
        job_instance_id=job_instance_id,
        control_id=control_id,
        control_directory=SimpleNamespace(),
        directory=SimpleNamespace(),
        pin_scope=(),
        container_hash="6" * 64,
    )
    adopted = SimpleNamespace(prepared=prepared, outcome=outcome)

    class _Runner:
        def adopt_durable_attempt(self, **kwargs):
            events.append("adopt_runner")
            assert kwargs["binding"].execution_revision == 8
            return adopted

        def retire_adopted_terminal_attempt(self, *_args, **_kwargs):
            raise AssertionError("published recovery must not use terminal retirement")

        def retire_adopted_published_attempt(
            self,
            value,
            *,
            proof,
            publication_ownership,
        ):
            events.append("retire_runner")
            assert value is adopted
            assert publication_ownership is publication
            assert proof.execution_revision == 9
            assert proof.publication_journal_sha256 == journal_sha256
            return MaterialRunnerRetirementAttestation(
                status="succeeded",
                execution_id=execution_id,
                operation="pexels",
                attempt_id=attempt_id,
                control_id=control_id,
                job_instance_id=job_instance_id,
                completion_revision=9,
                completion_payload_sha256=proof.final_payload_sha256,
                checkpoint_sha256=proof.checkpoint_sha256,
                attempt_history_sha256=proof.attempt_history_sha256,
                tombstone_sha256="9" * 64,
                retired_at="2026-07-16T08:01:00.000000Z",
                release_basis="dead_controller_after_tombstone",
            )

    class _Publication:
        phase = "committed"
        ownership_retained = True
        committed_journal_sha256 = journal_sha256
        result = SimpleNamespace(success=True)

        def assert_retained_ownership(self):
            events.append("assert_publication")

        def release_after_retirement(self):
            events.append("release_publication")

    publication = _Publication()

    def recover_publication(claim, value):
        events.append("adopt_publication")
        assert claim.lease is store.lease
        assert value is adopted
        return publication

    class _Lifecycle:
        def complete_recovered_success(self, claim, *, outcome, publication):
            events.append("completion_cas")
            assert claim.lease is store.lease
            assert outcome is adopted.outcome
            history_base = {
                "material_task_id": "task-001",
                "execution_id": execution_id,
                "operation": "pexels",
                "attempt_id": attempt_id,
                "control_id": control_id,
                "job_instance_id": job_instance_id,
                "status": "succeeded",
                "observed_at": observed_at,
                "result_sha256": result_sha256,
                "publication_journal_sha256": journal_sha256,
                "reason_code": "",
                "reason": "",
            }
            history_entry = dict(history_base)
            history_entry["entry_sha256"] = _canonical_sha(history_base)
            final = copy.deepcopy(store.payload)
            final.update(
                {
                    "execution_revision": 9,
                    "execution_state": "idle",
                    "publication_phase": "committed",
                    "last_execution_status": "succeeded",
                    "execution_completed_at": observed_at,
                    "material_attempt_history": [history_entry],
                    "active_material_task_id": None,
                    "active_execution_id": None,
                    "active_attempt_id": None,
                    "active_attempt_kind": None,
                    "active_control_id": None,
                    "active_job_instance_id": None,
                }
            )
            final["material_tasks"][0]["status"] = "已完成"
            store.payload = copy.deepcopy(final)
            store.lease.expected_payload = copy.deepcopy(final)
            store.lease.expected_revision = 9
            return MaterialCompletionReceipt(
                status="succeeded",
                execution_id=execution_id,
                operation="pexels",
                attempt_id=attempt_id,
                execution_revision=9,
                final_payload_sha256=_canonical_sha(final),
                publication_journal_sha256=journal_sha256,
                attempt_history_sha256=history_entry["entry_sha256"],
                result_sha256=result_sha256,
                publication_intent_sha256="1" * 64,
                publication_result_sha256="2" * 64,
                publication_worker_result_sha256=result_sha256,
                publication_reference_base_revision=7,
                publication_reference_base_payload_sha256="4" * 64,
                runner_retirement_checkpoint_sha256="8" * 64,
                observed_at=observed_at,
            )

        def acknowledge_runner_retirement(
            self, claim, *, completion, attestation
        ):
            events.append("ack_db")
            assert claim.lease is store.lease
            assert completion.execution_revision == 9
            assert attestation.checkpoint_sha256 == "8" * 64
            store.payload["execution_revision"] = 10
            store.payload["runner_retirement"] = {"state": "attested"}
            store.lease.expected_payload = copy.deepcopy(store.payload)
            store.lease.expected_revision = 10
            return MaterialRunnerRetirementAcknowledgement(
                completion_revision=9,
                ack_revision=10,
                completion_payload_sha256=completion.final_payload_sha256,
                checkpoint_sha256="8" * 64,
                tombstone_sha256=attestation.tombstone_sha256,
                acknowledged_at="2026-07-16T08:02:00.000000Z",
            )

    mutex = _Mutex()
    store = _Store(mutex)
    completion = MaterialRestartRecoveryCoordinator(
        store=store,
        runner=_Runner(),
        lifecycle=_Lifecycle(),
        publication_recovery_factory=recover_publication,
    ).complete_published_attempt(
        mutex=mutex,
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        operation="pexels",
    )

    assert completion.status == "succeeded"
    assert mutex.released is True
    assert events.index("adopt_runner") < events.index("adopt_publication")
    assert events.index("adopt_publication") < events.index("completion_cas")
    assert events.index("completion_cas") < events.index("retire_runner")
    assert events.index("retire_runner") < events.index("ack_db")
    assert events.index("ack_db") < events.index("release_publication")
    assert events.index("release_publication") < events.index("release_lease")


def test_restart_recovery_coordinator_keeps_mutex_on_stale_completion_proof():
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    payload = {
        "execution_id": execution_id,
        "execution_revision": 3,
        "execution_state": "idle",
        "publication_phase": "rolled_back",
        "active_material_task_id": None,
        "active_execution_id": None,
        "active_attempt_id": None,
        "active_attempt_kind": None,
        "active_control_id": None,
        "active_job_instance_id": None,
        "material_tasks": [],
        "material_attempt_history": [],
    }

    class _Store:
        def fresh_read(self):
            return copy.deepcopy(payload)

    class _Mutex:
        def __init__(self):
            self.released = False

        def assert_owned(self):
            assert not self.released

        def release(self):
            self.released = True

    class _Runner:
        def adopt_durable_attempt(self, **_kwargs):
            raise AssertionError("stale DB proof must fail before runner adoption")

        def retire_adopted_terminal_attempt(self, *_args, **_kwargs):
            raise AssertionError("runner retirement must not be reached")

    completion = lifecycle_subject.MaterialCompletionReceipt(
        status="terminated",
        execution_id=execution_id,
        operation="pexels",
        attempt_id=attempt_id,
        execution_revision=3,
        final_payload_sha256="f" * 64,
        attempt_history_sha256="e" * 64,
        result_sha256=None,
        observed_at="2026-07-16T08:00:00.000000Z",
    )
    mutex = _Mutex()
    with pytest.raises(MaterialRestartRecoveryUnknown):
        MaterialRestartRecoveryCoordinator(
            store=_Store(),
            runner=_Runner(),
        ).retire_terminal_attempt(
            mutex=mutex,
            material_task_id="task-001",
            execution_id=execution_id,
            attempt_id=attempt_id,
            control_id=control_id,
            job_instance_id=job_instance_id,
            operation="pexels",
            completion=completion,
        )
    assert mutex.released is False


def test_pending_terminal_retirement_is_adopted_attested_and_acked_in_order(
    tmp_path: Path,
) -> None:
    execution_id, attempt_id, control_id, job_instance_id = _ids()
    observed_at = "2026-07-18T08:30:00+00:00"
    history_sha256 = "5" * 64
    result_sha256 = "6" * 64
    checkpoint = MaterialRunnerRetirementCheckpoint.create(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
        completion_status="failed",
        completion_revision=8,
        completion_observed_at=observed_at,
        reference_revision=7,
        reference_payload_sha256="4" * 64,
        attempt_history_sha256=history_sha256,
        result_sha256=result_sha256,
    )
    payload = {
        "material_tasks": [{"material_task_id": "task-001", "status": "failed"}],
        "execution_id": execution_id,
        "execution_revision": 8,
        "execution_state": "idle",
        "publication_phase": "rolled_back",
        "last_execution_status": "failed",
        "execution_completed_at": observed_at,
        "active_material_task_id": None,
        "active_execution_id": None,
        "active_attempt_id": None,
        "active_attempt_kind": None,
        "active_control_id": None,
        "active_job_instance_id": None,
        "material_attempt_history": [
            {
                "material_task_id": "task-001",
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "operation": "pexels",
                "control_id": control_id,
                "job_instance_id": job_instance_id,
                "status": "failed",
                "observed_at": observed_at,
                "entry_sha256": history_sha256,
                "result_sha256": result_sha256,
                "publication_journal_sha256": None,
            }
        ],
    }
    install_pending_runner_retirement(payload, checkpoint)
    events: list[str] = []
    mutex = _Mutex(events)

    class _Lease:
        def __init__(self):
            self.checkpoint = checkpoint
            self.expected_payload = copy.deepcopy(payload)
            self.expected_revision = 8

        def assert_owned(self):
            events.append("assert_lease")
            mutex.assert_owned()

        def release(self):
            events.append("release_lease")
            mutex.release()

    lease = _Lease()

    class _Store:
        def assert_recovery_mutex(self, value):
            assert value is mutex
            value.assert_owned()

        def fresh_read(self):
            return copy.deepcopy(lease.expected_payload)

        def adopt_pending_retirement_lease_with_owned_recovery_mutex(self, **values):
            events.append("adopt_lease")
            assert values == {
                "mutex": mutex,
                "material_task_id": "task-001",
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "attempt_kind": "pexels",
                "control_id": control_id,
                "job_instance_id": job_instance_id,
            }
            return lease

    attempt_path = tmp_path / "pending-retirement-attempt"
    attempt_path.mkdir()
    attempt_root = approve_physical_d_write_root(attempt_path)
    prepared = PreparedMaterialAttempt(
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        control_directory=attempt_root,
        directory=attempt_root,
        pin_scope=(attempt_root,),
        container_hash="a" * 64,
    )
    outcome = _failed_outcome(execution_id, attempt_id)
    expected_attestation = MaterialRunnerRetirementAttestation(
        status="failed",
        execution_id=execution_id,
        operation="pexels",
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        completion_revision=8,
        completion_payload_sha256=_canonical_sha(payload),
        checkpoint_sha256=checkpoint.checkpoint_sha256,
        attempt_history_sha256=history_sha256,
        tombstone_sha256="7" * 64,
        retired_at="2026-07-18T08:31:00+00:00",
        release_basis="dead_controller_after_tombstone",
    )

    class _Runner:
        def adopt_durable_attempt(self, **values):
            events.append("adopt_runner")
            assert values["retirement_proof"].checkpoint_sha256 == checkpoint.checkpoint_sha256
            assert values["binding"] is None
            return SimpleNamespace(prepared=prepared, outcome=outcome)

        def retire_adopted_terminal_attempt(self, *_args, **_kwargs):
            raise AssertionError("typed completed retirement surface is required")

        def attest_completed_adopted_terminal_attempt(self, adopted, *, proof):
            events.append("attest_runner")
            assert adopted.prepared is prepared
            assert proof.checkpoint_sha256 == checkpoint.checkpoint_sha256
            return expected_attestation

    acknowledgement = MaterialRunnerRetirementAcknowledgement(
        completion_revision=8,
        ack_revision=9,
        completion_payload_sha256=_canonical_sha(payload),
        checkpoint_sha256=checkpoint.checkpoint_sha256,
        tombstone_sha256=expected_attestation.tombstone_sha256,
        acknowledged_at="2026-07-18T08:32:00+00:00",
    )

    class _Lifecycle:
        def acknowledge_pending_runner_retirement(
            self, value, *, completion, attestation
        ):
            events.append("ack_db")
            assert value is lease
            assert completion.execution_revision == 8
            assert attestation is expected_attestation
            lease.expected_payload["execution_revision"] = 9
            lease.expected_payload["runner_retirement"]["state"] = "attested"
            return acknowledgement

    completion = MaterialRestartRecoveryCoordinator(
        store=_Store(),
        runner=_Runner(),
        lifecycle=_Lifecycle(),
        publication_recovery_factory=lambda *_args: (_ for _ in ()).throw(
            AssertionError("terminal retirement must not recover publication")
        ),
    ).complete_pending_runner_retirement(
        mutex=mutex,
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        operation="pexels",
        completion_status="failed",
        completion_revision=8,
        checkpoint_sha256=checkpoint.checkpoint_sha256,
    )

    assert completion.status == "failed"
    assert events.index("adopt_lease") < events.index("adopt_runner")
    assert events.index("adopt_runner") < events.index("attest_runner")
    assert events.index("attest_runner") < events.index("ack_db")
    assert events.index("ack_db") < events.index("release_lease")
    assert mutex.released is True
