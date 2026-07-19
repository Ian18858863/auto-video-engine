from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from app.pipeline.material_execution_submission_owner_lease import (
    MaterialExecutionOwnerLeaseBusy,
    MAX_OWNER_LEASE_TIMEOUT_SECONDS,
    MaterialExecutionSubmissionOwnerLease,
)
from app.pipeline.material_execution_api_context import MaterialExecutionSubmissionContext
from app.pipeline.material_execution_application import (
    MaterialExecutionCommand,
    MaterialExecutionLaneKey,
)
from app.pipeline.material_execution_submission_store import (
    MaterialExecutionSubmissionErrorMetadata,
    MaterialExecutionSubmissionIdentityConflict,
    MaterialExecutionSubmissionStateConflict,
    MaterialExecutionSubmissionStore,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    env["TEMP"] = r"D:\CodexData\Temp\auto-video-engine"
    env["TMP"] = env["TEMP"]
    env["TMPDIR"] = env["TEMP"]
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = r"D:\CodexData\Temp\auto-video-engine\pycache"
    return env


def test_live_owner_lease_is_exclusive_and_persists_epoch(tmp_path: Path):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    first = MaterialExecutionSubmissionOwnerLease.acquire(
        store,
        owner_instance_id=str(uuid4()),
    )
    try:
        assert first.token.owner_epoch == 1
        assert first.activation.acquisition_kind == "initial"
        with pytest.raises(MaterialExecutionOwnerLeaseBusy):
            MaterialExecutionSubmissionOwnerLease.acquire(
                store,
                owner_instance_id=str(uuid4()),
            )
    finally:
        first.close()


def test_clean_handoff_increments_epoch_without_claiming_death(tmp_path: Path):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    first = MaterialExecutionSubmissionOwnerLease.acquire(store)
    first.close()

    second = MaterialExecutionSubmissionOwnerLease.acquire(store)
    try:
        assert second.token.owner_epoch == 2
        assert second.activation.acquisition_kind == "clean_handoff"
        assert second.activation.was_abandoned is False
    finally:
        second.close()


@pytest.mark.parametrize(
    "timeout",
    [math.nan, math.inf, -math.inf, 1e100, MAX_OWNER_LEASE_TIMEOUT_SECONDS + 1],
)
def test_owner_lease_rejects_non_finite_timeouts(tmp_path: Path, timeout: float):
    store = MaterialExecutionSubmissionStore(tmp_path / "timeouts.sqlite3")
    with pytest.raises(ValueError):
        MaterialExecutionSubmissionOwnerLease.acquire(
            store,
            startup_timeout=timeout,
        )

    lease = MaterialExecutionSubmissionOwnerLease.acquire(store)
    try:
        with pytest.raises(ValueError):
            lease.close(timeout=timeout)
        assert lease.closed is False
    finally:
        lease.close()


def test_inflight_clean_close_keeps_mutex_held_until_work_is_terminal(tmp_path: Path):
    database_path = tmp_path / "submissions.sqlite3"
    store = MaterialExecutionSubmissionStore(database_path)
    lease = MaterialExecutionSubmissionOwnerLease.acquire(store)
    key = MaterialExecutionLaneKey(database_path, 11, 23)
    command = MaterialExecutionCommand(
        pipeline_id=11,
        material_step_id=23,
        material_task_id="material-shot-1",
        execution_id=str(uuid4()),
        operation="pexels",
        startup_timeout_seconds=1.0,
        operation_timeout_seconds=30.0,
    )
    context = MaterialExecutionSubmissionContext(
        lane_key=key,
        command=command,
        source_chain=("pexels", "pixabay"),
        snapshot_sha256="a" * 64,
    )
    submission_id = str(uuid4())
    store.create(submission_id, context)
    store.compare_and_swap(
        submission_id,
        expected_state="accepted",
        next_state="dispatching",
        owner_instance_id=lease.owner_instance_id,
        owner_epoch=lease.owner_epoch,
        owner_capability=lease.capability,
    )

    with pytest.raises(MaterialExecutionSubmissionStateConflict):
        lease.close()
    assert lease.closed is False
    with pytest.raises(MaterialExecutionOwnerLeaseBusy):
        MaterialExecutionSubmissionOwnerLease.acquire(store)

    store.compare_and_swap(
        submission_id,
        expected_state="dispatching",
        next_state="failed",
        owner_instance_id=lease.owner_instance_id,
        owner_epoch=lease.owner_epoch,
        owner_capability=lease.capability,
        error=MaterialExecutionSubmissionErrorMetadata(
            code="operator_cancelled",
            detail="test terminalisation",
        ),
    )
    lease.close()
    successor = MaterialExecutionSubmissionOwnerLease.acquire(store)
    try:
        assert successor.owner_epoch == lease.owner_epoch + 1
    finally:
        successor.close()


def test_killed_owner_is_exclusively_replaced_and_old_epoch_cannot_write(
    tmp_path: Path,
):
    database_path = tmp_path / "submissions.sqlite3"
    store = MaterialExecutionSubmissionStore(database_path)
    child_code = r'''
import json
import sys
from pathlib import Path
from uuid import uuid4
from app.pipeline.material_execution_api_context import MaterialExecutionSubmissionContext
from app.pipeline.material_execution_application import MaterialExecutionCommand, MaterialExecutionLaneKey, MaterialRecoveryTicket
from app.pipeline.material_execution_submission_owner_lease import MaterialExecutionSubmissionOwnerLease
from app.pipeline.material_execution_submission_store import MaterialExecutionSubmissionStore

path = sys.argv[1]
store = MaterialExecutionSubmissionStore(path)
lease = MaterialExecutionSubmissionOwnerLease.acquire(store)
key = MaterialExecutionLaneKey(database_path=Path(path), pipeline_id=11, material_step_id=23)
command = MaterialExecutionCommand(
    pipeline_id=11, material_step_id=23, material_task_id="material-shot-1",
    execution_id=str(uuid4()), operation="pexels",
    startup_timeout_seconds=1.0, operation_timeout_seconds=30.0,
)
context = MaterialExecutionSubmissionContext(
    lane_key=key, command=command, source_chain=("pexels", "pixabay"),
    snapshot_sha256="a" * 64,
)
submission_id = str(uuid4())
store.create(submission_id, context)
store.compare_and_swap(
    submission_id, expected_state="accepted", next_state="dispatching",
    owner_instance_id=lease.owner_instance_id, owner_epoch=lease.owner_epoch,
    owner_capability=lease.capability,
)
ticket = MaterialRecoveryTicket(
    ticket_id=str(uuid4()), lane_id=key.lane_id,
    pipeline_id=command.pipeline_id, material_step_id=command.material_step_id,
    material_task_id=command.material_task_id, execution_id=command.execution_id,
    attempt_id=str(uuid4()), operation=command.operation,
)
store.compare_and_swap(
    submission_id, expected_state="dispatching", next_state="blocked",
    owner_instance_id=lease.owner_instance_id, owner_epoch=lease.owner_epoch,
    owner_capability=lease.capability,
    recovery_ticket=ticket,
)
print(json.dumps({
    "submission_id": submission_id,
    "owner_instance_id": lease.owner_instance_id,
    "owner_epoch": lease.owner_epoch,
}, separators=(",", ":")), flush=True)
sys.stdin.readline()
'''
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(database_path)],
        cwd=BACKEND_ROOT,
        env=_child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        close_fds=True,
    )
    assert child.stdout is not None
    line = child.stdout.readline()
    assert line, child.stderr.read() if child.stderr else "child owner exited silently"
    child_metadata = json.loads(line)
    submission_id = child_metadata["submission_id"]
    assert store.get(submission_id).state == "blocked"  # type: ignore[union-attr]

    with pytest.raises(MaterialExecutionOwnerLeaseBusy):
        MaterialExecutionSubmissionOwnerLease.acquire(store)

    child.kill()
    child.wait(timeout=10)

    successor = MaterialExecutionSubmissionOwnerLease.acquire(store)
    try:
        assert successor.token.owner_epoch == int(child_metadata["owner_epoch"]) + 1
        assert successor.activation.acquisition_kind in {
            "exclusive_reacquire",
            "abandoned_mutex",
        }
        assert [row.submission_id for row in successor.fenced_submissions] == [
            submission_id
        ]
        fenced = store.get(submission_id)
        assert fenced is not None
        assert fenced.state == "unknown"
        assert fenced.recovery_ticket is not None
        assert fenced.error is not None
        assert fenced.error.code == "owner_lease_superseded"

        with pytest.raises(MaterialExecutionSubmissionIdentityConflict):
            store.compare_and_swap(
                submission_id,
                expected_state="unknown",
                next_state="failed",
                owner_instance_id=child_metadata["owner_instance_id"],
                error=MaterialExecutionSubmissionErrorMetadata(
                    code="stale_uuid_only_callback",
                    detail="a UUID without the fencing epoch is not sufficient",
                ),
            )

        with pytest.raises(MaterialExecutionSubmissionIdentityConflict):
            store.compare_and_swap(
                submission_id,
                expected_state="unknown",
                next_state="failed",
                owner_instance_id=child_metadata["owner_instance_id"],
                owner_epoch=int(child_metadata["owner_epoch"]),
                error=MaterialExecutionSubmissionErrorMetadata(
                    code="stale_callback",
                    detail="old owner callback must be fenced",
                ),
            )
    finally:
        successor.close()
