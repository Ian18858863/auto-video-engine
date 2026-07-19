from __future__ import annotations

from concurrent.futures import Future
import json
from pathlib import Path
from uuid import uuid4

from app.pipeline.material_execution_api_context import MaterialExecutionSubmissionContext
from app.pipeline.material_execution_application import (
    MaterialExecutionCommand,
    MaterialExecutionLaneKey,
    MaterialExecutionResponse,
)
from app.pipeline.material_execution_submission_api import (
    close_unknown_material_execution,
    get_material_execution_status,
    reconcile_material_execution,
    submit_material_execution,
)
from app.pipeline.material_execution_submission_coordinator import (
    MaterialExecutionSubmissionCoordinator,
)
from app.pipeline.material_execution_submission_owner_lease import (
    MaterialExecutionOwnerLeaseBusy,
)
from app.pipeline.material_execution_application import MaterialExecutionLaneUnknown
from app.pipeline.material_execution_submission_store import (
    MaterialExecutionSubmissionStore,
)


class _Lane:
    def __init__(self, future: Future[MaterialExecutionResponse]) -> None:
        self.future = future

    def submit_source_chain(self, command, *, source_chain):
        return self.future

    def reconcile(self, ticket):
        return self.future


class _Registry:
    def __init__(self, lane: _Lane) -> None:
        self.lane = lane

    def get_or_create(self, key):
        return self.lane


class _BusyCoordinator:
    def submit(self, *_args, **_kwargs):
        raise MaterialExecutionOwnerLeaseBusy("another process owns the sidecar")


def _context(tmp_path: Path) -> MaterialExecutionSubmissionContext:
    key = MaterialExecutionLaneKey(tmp_path / "production.db", 11, 23)
    command = MaterialExecutionCommand(
        11,
        23,
        "material-shot-1",
        str(uuid4()),
        "pexels",
        1.0,
        30.0,
    )
    return MaterialExecutionSubmissionContext(
        lane_key=key,
        command=command,
        source_chain=("pexels", "pixabay"),
        snapshot_sha256="a" * 64,
    )


def _coordinator(tmp_path: Path, future: Future[MaterialExecutionResponse]):
    return MaterialExecutionSubmissionCoordinator(
        store=MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3"),
        registry=_Registry(_Lane(future)),  # type: ignore[arg-type]
    )


def test_submission_api_is_route_free_and_returns_json_safe_202(tmp_path: Path):
    future: Future[MaterialExecutionResponse] = Future()
    coordinator = _coordinator(tmp_path, future)
    result = submit_material_execution(coordinator, _context(tmp_path))

    assert result.status_code == 202
    assert result.body["state"] == "running"
    assert json.loads(json.dumps(result.to_json(), allow_nan=False)) == result.to_json()
    assert not ({"future", "session", "raw_state"} & result.body.keys())


def test_submission_api_maps_live_owner_contention_to_recoverable_409(tmp_path: Path):
    result = submit_material_execution(_BusyCoordinator(), _context(tmp_path))

    assert result.status_code == 409
    assert result.body["error"]["code"] == "material_submission_owner_busy"
    assert result.body["error"]["recovery_required"] is True
    public_detail = result.body["error"]["detail"]
    assert "Global\\" not in public_detail
    assert "S-1-" not in public_detail
    assert "another process owns" not in public_detail


def test_status_api_reads_durable_terminal_result_and_maps_missing(tmp_path: Path):
    context = _context(tmp_path)
    future: Future[MaterialExecutionResponse] = Future()
    coordinator = _coordinator(tmp_path, future)
    submitted = submit_material_execution(coordinator, context)
    submission_id = submitted.body["submission_id"]
    response = MaterialExecutionResponse(
        pipeline_id=context.command.pipeline_id,
        material_step_id=context.command.material_step_id,
        material_task_id=context.command.material_task_id,
        execution_id=context.command.execution_id,
        attempt_id=str(uuid4()),
        operation=context.command.operation,
        status="succeeded",
        message="done",
        executed_task={"material_task_id": context.command.material_task_id},
        tasks=({"material_task_id": context.command.material_task_id},),
    )
    future.set_result(response)

    done = get_material_execution_status(coordinator, submission_id)
    missing = get_material_execution_status(coordinator, str(uuid4()))
    assert done.status_code == 200
    assert done.body["state"] == "succeeded"
    assert missing.status_code == 404
    assert missing.body["error"]["code"] == "material_submission_not_found"


def test_unknown_reconcile_is_a_conflict_not_a_retry(tmp_path: Path):
    future: Future[MaterialExecutionResponse] = Future()
    coordinator = _coordinator(tmp_path, future)
    submitted = submit_material_execution(coordinator, _context(tmp_path))
    future.set_exception(MaterialExecutionLaneUnknown("owner stopped"))

    result = reconcile_material_execution(
        coordinator,
        submitted.body["submission_id"],
    )
    assert result.status_code == 409
    assert result.body["error"]["recovery_required"] is True


def test_unknown_close_is_an_audited_terminal_failure(tmp_path: Path):
    future: Future[MaterialExecutionResponse] = Future()
    coordinator = _coordinator(tmp_path, future)
    submitted = submit_material_execution(coordinator, _context(tmp_path))
    future.set_exception(MaterialExecutionLaneUnknown("owner stopped"))

    result = close_unknown_material_execution(
        coordinator,
        submitted.body["submission_id"],
    )

    assert result.status_code == 200
    assert result.body["state"] == "failed"
    assert result.body["error"]["code"] == "manual_unknown_close"
