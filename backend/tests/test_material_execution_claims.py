from __future__ import annotations

import copy
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, build_engine
from app.models.entities import HotItem, PipelineStep, ProductionPipeline
from app.pipeline.material_execution_claims import (
    MaterialExecutionClaimError,
    MaterialExecutionClaimConflict,
    MaterialExecutionClaimUnknown,
    MaterialExecutionClaimKey,
    SqlAlchemyMaterialExecutionClaimStore,
)
from app.pipeline.material_runner_retirement import (
    MaterialRunnerRetirementCheckpoint,
    install_pending_runner_retirement,
    json_payload_sha256,
)


@pytest.fixture()
def claim_store(tmp_path: Path):
    database_path = tmp_path / "claim.db"
    assert database_path.drive.upper() == "D:"
    engine = build_engine(f"sqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as session:
        hot_item = HotItem(
            source_platform="test",
            source_type="test",
            title="claim test",
            url=f"https://example.invalid/{uuid4().hex}",
        )
        session.add(hot_item)
        session.flush()
        pipeline = ProductionPipeline(
            hotspot_id=hot_item.id,
            project_name="claim-test",
            project_dir=str(tmp_path / "project"),
            project_key=f"pending-{uuid4().hex}-claim-test",
        )
        step = PipelineStep(
            name="生成素材任务",
            step_order=3,
            output_summary={
                "material_tasks": [
                    {"material_task_id": "task-001", "status": "等待"}
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
        yield store
    finally:
        engine.dispose()


def _install_failed_pending_retirement(claim_store):
    initial = claim_store.fresh_read()
    with_next_task = copy.deepcopy(initial)
    with_next_task["material_tasks"].append(
        {"material_task_id": "task-002", "status": "等待"}
    )
    assert claim_store.compare_and_swap(initial, with_next_task) is True
    execution_id = str(uuid4())
    attempt_id = str(uuid4())
    control_id = str(uuid4())
    job_instance_id = str(uuid4())
    lease = claim_store.claim_lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        attempt_kind="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    claimed = lease.expected_payload
    observed_at = "2026-07-18T10:00:00+00:00"
    history = {
        "material_task_id": "task-001",
        "execution_id": execution_id,
        "operation": "pexels",
        "attempt_id": attempt_id,
        "control_id": control_id,
        "job_instance_id": job_instance_id,
        "status": "failed",
        "observed_at": observed_at,
        "result_sha256": "b" * 64,
        "publication_journal_sha256": None,
        "reason_code": "no_result",
        "reason": "none",
        "entry_sha256": "c" * 64,
    }
    completed = copy.deepcopy(claimed)
    completed.update(
        {
            "execution_state": "idle",
            "publication_phase": "rolled_back",
            "last_execution_status": "failed",
            "execution_completed_at": observed_at,
            "execution_revision": 2,
            "material_attempt_history": [history],
            "active_material_task_id": None,
            "active_execution_id": None,
            "active_attempt_id": None,
            "active_attempt_kind": None,
            "active_control_id": None,
            "active_job_instance_id": None,
        }
    )
    checkpoint = MaterialRunnerRetirementCheckpoint.create(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
        completion_status="failed",
        completion_revision=2,
        completion_observed_at=observed_at,
        reference_revision=1,
        reference_payload_sha256=json_payload_sha256(claimed),
        attempt_history_sha256="c" * 64,
        result_sha256="b" * 64,
    )
    install_pending_runner_retirement(completed, checkpoint)
    lease.compare_and_swap(completed)
    lease.release()
    return {
        "execution_id": execution_id,
        "attempt_id": attempt_id,
        "control_id": control_id,
        "job_instance_id": job_instance_id,
        "checkpoint": checkpoint,
        "pending": completed,
    }


def _adopt_pending_retirement(claim_store, values):
    mutex = claim_store.acquire_recovery_mutex()
    return claim_store.adopt_pending_retirement_lease_with_owned_recovery_mutex(
        mutex=mutex,
        material_task_id="task-001",
        execution_id=values["execution_id"],
        attempt_id=values["attempt_id"],
        attempt_kind="pexels",
        control_id=values["control_id"],
        job_instance_id=values["job_instance_id"],
    )


def _valid_retirement_ack(pending, checkpoint):
    acknowledged = copy.deepcopy(pending)
    acknowledged["execution_revision"] = checkpoint.completion_revision + 1
    acknowledged["runner_retirement"] = {
        "format": "material-runner-retirement-v1",
        "state": "attested",
        "checkpoint": checkpoint.to_json(),
        "attestation": {
            "completion_payload_sha256": json_payload_sha256(pending),
            "tombstone_sha256": "d" * 64,
            "retired_at": "2026-07-18T10:00:01+00:00",
            "release_basis": "dead_controller_after_tombstone",
            "ack_revision": checkpoint.completion_revision + 1,
            "acknowledged_at": "2026-07-18T10:00:02+00:00",
        },
    }
    return acknowledged


def test_fresh_read_is_a_detached_deep_copy(claim_store):
    first = claim_store.fresh_read()
    first["material_tasks"][0]["status"] = "changed-locally"
    second = claim_store.fresh_read()

    assert second["material_tasks"][0]["status"] == "等待"


def test_compare_and_swap_is_exact_and_fresh(claim_store):
    expected = claim_store.fresh_read()
    replacement = dict(expected)
    replacement["execution_revision"] = 1

    assert claim_store.compare_and_swap(expected, replacement) is True
    assert claim_store.compare_and_swap(expected, {"stale": True}) is False
    assert claim_store.fresh_read()["execution_revision"] == 1


def test_claim_writes_active_identity_and_increments_revision(claim_store):
    receipt = claim_store.claim(
        material_task_id="task-001",
        execution_id=str(uuid4()),
        attempt_id=str(uuid4()),
    )
    current = claim_store.fresh_read()

    assert receipt.material_task_id == "task-001"
    assert current["publication_phase"] == "preparing"
    assert current["execution_state"] == "claimed"
    assert current["active_execution_id"] == receipt.execution_id
    assert current["active_attempt_id"] == receipt.attempt_id
    assert current["execution_revision"] == 1
    assert current["material_tasks"][0]["status"] == "进行中"
    assert current["material_tasks"][0]["status_history"] == ["进行中"]


def test_claim_lease_can_adopt_an_already_owned_step_mutex(claim_store):
    mutex = claim_store.acquire_claim_mutex()
    execution_id = str(uuid4())
    attempt_id = str(uuid4())
    control_id = str(uuid4())
    job_instance_id = str(uuid4())

    lease = claim_store.claim_lease_with_owned_mutex(
        mutex=mutex,
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        attempt_kind="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
    )

    assert lease.mutex is mutex
    assert lease.active_attempt_id == attempt_id
    assert lease.active_control_id == control_id
    assert lease.active_job_instance_id == job_instance_id
    lease.assert_owned()
    lease.release()


def test_recovery_mutex_can_adopt_exact_publishing_owner_without_new_claim(
    claim_store,
):
    execution_id = str(uuid4())
    attempt_id = str(uuid4())
    control_id = str(uuid4())
    job_instance_id = str(uuid4())
    lease = claim_store.claim_lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        attempt_kind="stock_candidates",
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    publishing = lease.expected_payload
    publishing["material_tasks"][0]["status"] = "发布收口中"
    publishing["material_tasks"][0]["status_history"].append("发布收口中")
    publishing.update(
        {
            "publication_phase": "db_committed",
            "execution_state": "publishing",
            "publication_journal": f"D:/attempts/{attempt_id}/publication-journal.json",
            "publication_intent_sha256": "a" * 64,
            "publication_result_sha256": "b" * 64,
            "publication_worker_result_sha256": "c" * 64,
            "publication_reference_base_revision": lease.expected_revision,
            "publication_reference_base_payload_sha256": "d" * 64,
            "execution_revision": lease.expected_revision + 1,
        }
    )
    lease.compare_and_swap(publishing)
    lease.release()

    mutex = claim_store.acquire_recovery_mutex()
    recovered = claim_store.adopt_publishing_lease_with_owned_recovery_mutex(
        mutex=mutex,
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        attempt_kind="stock_candidates",
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    assert recovered.mutex is mutex
    assert recovered.expected_revision == 2
    assert recovered.expected_payload == publishing
    recovered.assert_owned()
    recovered.release()


def test_recovery_mutex_refuses_to_adopt_a_non_publishing_claim(claim_store):
    execution_id = str(uuid4())
    attempt_id = str(uuid4())
    control_id = str(uuid4())
    job_instance_id = str(uuid4())
    lease = claim_store.claim_lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        attempt_kind="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    lease.release()

    mutex = claim_store.acquire_recovery_mutex()
    try:
        with pytest.raises(MaterialExecutionClaimConflict, match="publishing"):
            claim_store.adopt_publishing_lease_with_owned_recovery_mutex(
                mutex=mutex,
                material_task_id="task-001",
                execution_id=execution_id,
                attempt_id=attempt_id,
                attempt_kind="pexels",
                control_id=control_id,
                job_instance_id=job_instance_id,
            )
    finally:
        mutex.release()


def test_recovery_mutex_adopts_exact_pending_runner_retirement_without_new_claim(
    claim_store,
):
    execution_id = str(uuid4())
    attempt_id = str(uuid4())
    control_id = str(uuid4())
    job_instance_id = str(uuid4())
    lease = claim_store.claim_lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        attempt_kind="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    claimed = lease.expected_payload
    observed_at = "2026-07-18T10:00:00+00:00"
    history = {
        "material_task_id": "task-001",
        "execution_id": execution_id,
        "operation": "pexels",
        "attempt_id": attempt_id,
        "control_id": control_id,
        "job_instance_id": job_instance_id,
        "status": "failed",
        "observed_at": observed_at,
        "result_sha256": "b" * 64,
        "publication_journal_sha256": None,
        "reason_code": "no_result",
        "reason": "none",
        "entry_sha256": "c" * 64,
    }
    completed = copy.deepcopy(claimed)
    completed.update(
        {
            "execution_state": "idle",
            "publication_phase": "rolled_back",
            "last_execution_status": "failed",
            "execution_completed_at": observed_at,
            "execution_revision": 2,
            "material_attempt_history": [history],
            "active_material_task_id": None,
            "active_execution_id": None,
            "active_attempt_id": None,
            "active_attempt_kind": None,
            "active_control_id": None,
            "active_job_instance_id": None,
        }
    )
    checkpoint = MaterialRunnerRetirementCheckpoint.create(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
        completion_status="failed",
        completion_revision=2,
        completion_observed_at=observed_at,
        reference_revision=1,
        reference_payload_sha256=json_payload_sha256(claimed),
        attempt_history_sha256="c" * 64,
        result_sha256="b" * 64,
    )
    install_pending_runner_retirement(completed, checkpoint)
    lease.compare_and_swap(completed)
    lease.release()

    mutex = claim_store.acquire_recovery_mutex()
    recovered = claim_store.adopt_pending_retirement_lease_with_owned_recovery_mutex(
        mutex=mutex,
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        attempt_kind="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
    )
    assert recovered.expected_revision == 2
    assert recovered.expected_payload == completed
    assert recovered.reference_revision == 1
    assert recovered.reference_payload_sha256 == json_payload_sha256(claimed)
    recovered.assert_owned()
    recovered.release()


def test_pending_retirement_lease_rejects_a_forged_attestation_ack(claim_store):
    values = _install_failed_pending_retirement(claim_store)
    recovered = _adopt_pending_retirement(claim_store, values)
    forged = _valid_retirement_ack(values["pending"], values["checkpoint"])
    forged["runner_retirement"]["attestation"]["completion_payload_sha256"] = (
        "e" * 64
    )

    with pytest.raises(MaterialExecutionClaimConflict, match="attestation"):
        recovered.compare_and_swap(forged)

    assert claim_store.fresh_read() == values["pending"]
    recovered.release()


def test_claim_accepts_a_fully_bound_runner_retirement_attestation(claim_store):
    values = _install_failed_pending_retirement(claim_store)
    recovered = _adopt_pending_retirement(claim_store, values)
    acknowledged = _valid_retirement_ack(values["pending"], values["checkpoint"])
    recovered.compare_and_swap(acknowledged)
    recovered.release()

    receipt = claim_store.claim(
        material_task_id="task-002",
        execution_id=str(uuid4()),
        attempt_id=str(uuid4()),
    )

    assert receipt.material_task_id == "task-002"
    assert (
        claim_store.fresh_read()["execution_revision"]
        == acknowledged["execution_revision"] + 1
    )


def test_claim_rejects_attested_retirement_after_protected_payload_tampering(
    claim_store,
):
    values = _install_failed_pending_retirement(claim_store)
    recovered = _adopt_pending_retirement(claim_store, values)
    acknowledged = _valid_retirement_ack(values["pending"], values["checkpoint"])
    recovered.compare_and_swap(acknowledged)
    recovered.release()

    tampered = copy.deepcopy(acknowledged)
    tampered["material_attempt_history"][0]["reason"] = "rewritten-after-ack"
    assert claim_store._compare_and_swap_unlocked(acknowledged, tampered) is True

    with pytest.raises(MaterialExecutionClaimConflict, match="attestation"):
        claim_store.claim(
            material_task_id="task-002",
            execution_id=str(uuid4()),
            attempt_id=str(uuid4()),
        )

    assert claim_store.fresh_read() == tampered


def test_compatibility_cas_cannot_invent_an_active_owner(claim_store):
    expected = claim_store.fresh_read()
    replacement = copy.deepcopy(expected)
    replacement.update(
        {
            "active_material_task_id": "task-001",
            "active_execution_id": str(uuid4()),
            "active_attempt_id": str(uuid4()),
            "active_attempt_kind": "pexels",
            "active_control_id": str(uuid4()),
            "active_job_instance_id": str(uuid4()),
            "execution_revision": expected["execution_revision"] + 1,
        }
    )

    with pytest.raises(MaterialExecutionClaimConflict, match="cannot create"):
        claim_store.compare_and_swap(expected, replacement)

    assert claim_store.fresh_read() == expected


def test_claim_atomically_migrates_legacy_shot_only_task_identity(claim_store):
    expected = claim_store.fresh_read()
    legacy = copy.deepcopy(expected)
    legacy_task = legacy["material_tasks"][0]
    legacy_task.pop("material_task_id")
    legacy_task["shot_id"] = "shot-01"
    assert claim_store.compare_and_swap(expected, legacy) is True

    receipt = claim_store.claim(
        material_task_id="material-shot-01",
        execution_id=str(uuid4()),
        attempt_id=str(uuid4()),
    )

    current = claim_store.fresh_read()
    assert receipt.material_task_id == "material-shot-01"
    assert current["material_tasks"][0]["material_task_id"] == "material-shot-01"


def test_claim_rejects_duplicate_derived_task_identity(claim_store):
    expected = claim_store.fresh_read()
    duplicate = copy.deepcopy(expected)
    duplicate["material_tasks"] = [
        {"shot_id": "shot-01", "status": "等待"},
        {"shot_id": "shot-01", "status": "等待"},
    ]
    assert claim_store.compare_and_swap(expected, duplicate) is True

    with pytest.raises(MaterialExecutionClaimError, match="unique"):
        claim_store.claim(
            material_task_id="material-shot-01",
            execution_id=str(uuid4()),
            attempt_id=str(uuid4()),
        )


def test_second_claim_is_rejected_without_overwrite(claim_store):
    first = claim_store.claim(
        material_task_id="task-001",
        execution_id=str(uuid4()),
        attempt_id=str(uuid4()),
    )

    with pytest.raises(MaterialExecutionClaimConflict):
        claim_store.claim(
            material_task_id="task-001",
            execution_id=str(uuid4()),
            attempt_id=str(uuid4()),
        )

    current = claim_store.fresh_read()
    assert current["active_execution_id"] == first.execution_id
    assert current["active_attempt_id"] == first.attempt_id


def test_claim_rejects_idle_step_while_runner_retirement_is_pending(claim_store):
    expected = claim_store.fresh_read()
    pending = copy.deepcopy(expected)
    pending.update(
        {
            "publication_phase": "committed",
            "execution_state": "idle",
            "execution_revision": 3,
            "execution_id": str(uuid4()),
            "last_execution_status": "succeeded",
            "runner_retirement": {
                "format": "material-runner-retirement-v1",
                "state": "pending",
                "checkpoint": {"checkpoint_sha256": "a" * 64},
                "attestation": None,
            },
        }
    )
    assert claim_store._compare_and_swap_unlocked(expected, pending) is True

    with pytest.raises(MaterialExecutionClaimConflict, match="retirement"):
        claim_store.claim(
            material_task_id="task-001",
            execution_id=str(uuid4()),
            attempt_id=str(uuid4()),
        )

    assert claim_store.fresh_read() == pending


@pytest.mark.parametrize("claimable_phase", ["none", "rolled_back", "committed"])
def test_claim_accepts_only_an_explicit_claimable_phase(claim_store, claimable_phase):
    expected = claim_store.fresh_read()
    rolled_back = dict(expected)
    rolled_back.update(
        {
            "publication_phase": claimable_phase,
            "execution_state": "idle",
            "active_execution_id": None,
            "active_attempt_id": None,
        }
    )
    assert claim_store.compare_and_swap(expected, rolled_back) is True

    receipt = claim_store.claim(
        material_task_id="task-001",
        execution_id=str(uuid4()),
        attempt_id=str(uuid4()),
    )
    assert claim_store.fresh_read()["active_attempt_id"] == receipt.attempt_id


def test_claim_requires_uuid_execution_and_attempt_ids(claim_store):
    with pytest.raises(ValueError):
        claim_store.claim(
            material_task_id="task-001",
            execution_id="not-a-uuid",
            attempt_id=str(uuid4()),
        )


def test_terminal_task_is_not_reclaimed(claim_store):
    expected = claim_store.fresh_read()
    terminal = dict(expected)
    terminal["material_tasks"] = [
        {"material_task_id": "task-001", "status": "已完成", "material_path": "D:/x.mp4"}
    ]
    assert claim_store.compare_and_swap(expected, terminal) is True

    with pytest.raises(MaterialExecutionClaimConflict):
        claim_store.claim(
            material_task_id="task-001",
            execution_id=str(uuid4()),
            attempt_id=str(uuid4()),
        )


def test_material_step_lock_uses_physical_key(monkeypatch, claim_store):
    calls: dict[str, object] = {}

    class FakeMutex:
        def release(self):
            calls["released"] = True

    def fake_build(namespace: str, key: str) -> str:
        calls["namespace"] = namespace
        calls["key"] = key
        return "fake-mutex"

    def fake_acquire(name: str, *, timeout_ms: int = 0):
        calls["name"] = name
        calls["timeout_ms"] = timeout_ms
        return FakeMutex()

    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.build_mutex_name", fake_build
    )
    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.WindowsNamedMutex.try_acquire",
        fake_acquire,
    )

    with claim_store.material_step_lock(timeout_ms=17):
        assert calls["namespace"] == "material-step"
        assert str(claim_store.key.pipeline_id) in str(calls["key"])
        assert str(claim_store.key.material_step_id) in str(calls["key"])
        assert calls["name"] == "fake-mutex"
        assert calls["timeout_ms"] == 17
    assert calls["released"] is True


def test_claim_lease_holds_mutex_and_advances_only_after_exact_cas(
    monkeypatch,
    claim_store,
):
    calls: dict[str, object] = {}

    class FakeMutex:
        was_abandoned = False

        def release(self):
            calls["released"] = True

    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.build_mutex_name",
        lambda _namespace, _key: "fake-mutex",
    )
    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.WindowsNamedMutex.try_acquire",
        lambda _name, *, timeout_ms=0: (
            calls.update(timeout_ms=timeout_ms) or FakeMutex()
        ),
    )

    execution_id = str(uuid4())
    attempt_id = str(uuid4())
    lease = claim_store.claim_lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=attempt_id,
        attempt_kind="pexels",
        control_id="control-001",
        job_instance_id="job-001",
        timeout_ms=23,
    )
    assert calls["timeout_ms"] == 23
    assert "released" not in calls
    lease.assert_owned()
    assert lease.active_attempt_id == attempt_id
    assert lease.active_attempt_kind == "pexels"
    assert lease.active_control_id == "control-001"
    assert lease.active_job_instance_id == "job-001"

    replacement = copy.deepcopy(lease.expected_payload)
    replacement["execution_revision"] = 2
    replacement["execution_state"] = "running"
    lease.compare_and_swap(replacement)

    assert lease.expected_revision == 2
    assert lease.expected_payload == replacement
    assert claim_store.fresh_read() == replacement
    lease.release()
    assert calls["released"] is True


def test_claim_lease_rejects_unproofed_attempt_rotation(
    monkeypatch,
    claim_store,
):
    """A second source cannot be installed before terminal retirement proof."""

    class FakeMutex:
        was_abandoned = False

        def release(self):
            pass

    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.build_mutex_name",
        lambda _namespace, _key: "fake-mutex",
    )
    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.WindowsNamedMutex.try_acquire",
        lambda _name, *, timeout_ms=0: FakeMutex(),
    )

    execution_id = str(uuid4())
    first_attempt_id = str(uuid4())
    lease = claim_store.claim_lease(
        material_task_id="task-001",
        execution_id=execution_id,
        attempt_id=first_attempt_id,
        attempt_kind="pexels",
        control_id="control-001",
        job_instance_id="job-001",
    )

    second_attempt_id = str(uuid4())
    with pytest.raises(TypeError, match="terminal_receipt"):
        lease.rotate_attempt(
            attempt_id=second_attempt_id,
            attempt_kind="pixabay",
            control_id="control-002",
            job_instance_id="job-002",
            terminal_receipt=None,  # type: ignore[arg-type]
            retirement_receipt=None,  # type: ignore[arg-type]
        )

    assert lease.execution_id == execution_id
    assert lease.active_attempt_id == first_attempt_id
    assert lease.active_attempt_kind == "pexels"
    assert lease.expected_revision == 1
    assert claim_store.fresh_read() == lease.expected_payload
    lease.release()


def test_claim_lease_stale_cas_never_adopts_concurrent_payload(
    monkeypatch,
    claim_store,
):
    class FakeMutex:
        was_abandoned = False

        def release(self):
            pass

    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.build_mutex_name",
        lambda _namespace, _key: "fake-mutex",
    )
    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.WindowsNamedMutex.try_acquire",
        lambda _name, *, timeout_ms=0: FakeMutex(),
    )
    lease = claim_store.claim_lease(
        material_task_id="task-001",
        execution_id=str(uuid4()),
        attempt_id=str(uuid4()),
    )
    owned = copy.deepcopy(lease.expected_payload)
    concurrent = copy.deepcopy(owned)
    concurrent["concurrent_edit"] = "must-not-be-adopted"
    concurrent["execution_revision"] = owned["execution_revision"] + 1
    assert claim_store.compare_and_swap(owned, concurrent) is True
    replacement = copy.deepcopy(owned)
    replacement["execution_revision"] = 2

    with pytest.raises(MaterialExecutionClaimConflict):
        lease.compare_and_swap(replacement)

    assert lease.expected_payload == owned
    assert claim_store.fresh_read() == concurrent
    with pytest.raises(MaterialExecutionClaimConflict):
        lease.assert_owned()
    lease.release()


def test_claim_lease_expected_snapshot_cannot_be_mutated_into_foreign_payload(
    monkeypatch,
    claim_store,
):
    class FakeMutex:
        was_abandoned = False

        def release(self):
            pass

    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.build_mutex_name",
        lambda _namespace, _key: "fake-mutex",
    )
    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.WindowsNamedMutex.try_acquire",
        lambda _name, *, timeout_ms=0: FakeMutex(),
    )
    lease = claim_store.claim_lease(
        material_task_id="task-001",
        execution_id=str(uuid4()),
        attempt_id=str(uuid4()),
    )
    detached = lease.expected_payload
    detached["foreign_edit"] = True
    assert "foreign_edit" not in lease.expected_payload
    foreign_owner = lease.expected_payload
    foreign_owner["active_attempt_id"] = str(uuid4())
    foreign_owner["execution_revision"] = lease.expected_revision + 1
    with pytest.raises(MaterialExecutionClaimConflict, match="immutable claim owner"):
        lease.compare_and_swap(foreign_owner)
    assert claim_store.fresh_read() == lease.expected_payload
    replacement = lease.expected_payload
    replacement["execution_revision"] = lease.expected_revision + 1
    replacement["execution_state"] = "running"
    lease.compare_and_swap(replacement)
    assert "foreign_edit" not in claim_store.fresh_read()
    lease.release()


def test_claim_lease_rejects_non_monotonic_revision_without_writing(
    monkeypatch,
    claim_store,
):
    class FakeMutex:
        was_abandoned = False

        def release(self):
            pass

    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.build_mutex_name",
        lambda _namespace, _key: "fake-mutex",
    )
    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.WindowsNamedMutex.try_acquire",
        lambda _name, *, timeout_ms=0: FakeMutex(),
    )
    lease = claim_store.claim_lease(
        material_task_id="task-001",
        execution_id=str(uuid4()),
        attempt_id=str(uuid4()),
    )
    before = copy.deepcopy(lease.expected_payload)
    replacement = copy.deepcopy(before)
    replacement["execution_state"] = "running"

    with pytest.raises(MaterialExecutionClaimConflict):
        lease.compare_and_swap(replacement)

    assert claim_store.fresh_read() == before
    lease.release()


def test_claim_lease_rejects_an_abandoned_mutex_until_reconciled(
    monkeypatch,
    claim_store,
):
    calls: dict[str, object] = {}

    class AbandonedMutex:
        was_abandoned = True

        def release(self):
            calls["released"] = True

    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.build_mutex_name",
        lambda _namespace, _key: "fake-mutex",
    )
    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.WindowsNamedMutex.try_acquire",
        lambda _name, *, timeout_ms=0: AbandonedMutex(),
    )

    with pytest.raises(MaterialExecutionClaimConflict, match="abandoned"):
        claim_store.claim_lease(
            material_task_id="task-001",
            execution_id=str(uuid4()),
            attempt_id=str(uuid4()),
        )
    assert calls["released"] is True
    assert claim_store.fresh_read()["execution_revision"] == 0


def test_recovery_mutex_retains_abandoned_owner_for_same_thread_reconciliation(
    monkeypatch,
    claim_store,
):
    calls: dict[str, object] = {}

    class AbandonedMutex:
        was_abandoned = True

        def assert_owned(self):
            calls["asserted"] = True

        def release(self):
            calls["released"] = True

    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.build_mutex_name",
        lambda _namespace, _key: "fake-mutex",
    )
    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.WindowsNamedMutex.try_acquire",
        lambda _name, *, timeout_ms=0: AbandonedMutex(),
    )

    mutex = claim_store.acquire_recovery_mutex(timeout_ms=11)
    assert calls.get("released") is None
    mutex.assert_owned()
    assert calls["asserted"] is True
    mutex.release()
    assert calls["released"] is True


def test_claim_rejects_non_idle_state_and_stale_active_task_identity(claim_store):
    expected = claim_store.fresh_read()
    blocked = dict(expected)
    blocked["execution_state"] = "blocked"
    assert claim_store.compare_and_swap(expected, blocked) is True
    with pytest.raises(MaterialExecutionClaimConflict):
        claim_store.claim(
            material_task_id="task-001",
            execution_id=str(uuid4()),
            attempt_id=str(uuid4()),
        )
    expected = claim_store.fresh_read()
    idle_with_stale_owner = dict(expected)
    idle_with_stale_owner.update(
        {
            "execution_state": "idle",
            "active_material_task_id": "task-else",
            "execution_revision": expected["execution_revision"] + 1,
        }
    )
    # Direct transaction injection is intentional here: the public
    # compatibility CAS now rejects creation of a stale active owner.
    assert claim_store._compare_and_swap_unlocked(expected, idle_with_stale_owner) is True
    with pytest.raises(MaterialExecutionClaimConflict):
        claim_store.claim(
            material_task_id="task-001",
            execution_id=str(uuid4()),
            attempt_id=str(uuid4()),
        )


@pytest.mark.parametrize("status", ["进行中", "下载中", "被阻止", "需要人工确认"])
def test_claim_rejects_active_or_blocked_task_status_even_when_top_level_is_idle(
    claim_store,
    status,
):
    expected = claim_store.fresh_read()
    changed = copy.deepcopy(expected)
    changed["material_tasks"][0]["status"] = status
    assert claim_store.compare_and_swap(expected, changed) is True

    with pytest.raises(MaterialExecutionClaimConflict, match="status"):
        claim_store.claim(
            material_task_id="task-001",
            execution_id=str(uuid4()),
            attempt_id=str(uuid4()),
        )


def test_claim_lease_release_unknown_is_typed_and_consumes_lease(
    monkeypatch,
    claim_store,
):
    class FailingMutex:
        was_abandoned = False

        def release(self):
            raise OSError("injected native release uncertainty")

    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.build_mutex_name",
        lambda _namespace, _key: "fake-mutex",
    )
    monkeypatch.setattr(
        "app.pipeline.material_execution_claims.WindowsNamedMutex.try_acquire",
        lambda _name, *, timeout_ms=0: FailingMutex(),
    )
    lease = claim_store.claim_lease(
        material_task_id="task-001",
        execution_id=str(uuid4()),
        attempt_id=str(uuid4()),
    )

    with pytest.raises(MaterialExecutionClaimUnknown, match="release"):
        lease.release()
    with pytest.raises(Exception, match="released"):
        lease.assert_owned()


def test_c_drive_database_key_is_rejected():
    with pytest.raises(ValueError):
        MaterialExecutionClaimKey(
            database_path=Path("C:/forbidden/claim.db"),
            pipeline_id=1,
            material_step_id=2,
        )
