from __future__ import annotations

from concurrent.futures import Future
from dataclasses import replace
import json
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

import pytest

from app.pipeline.material_execution_api_context import MaterialExecutionSubmissionContext
from app.pipeline.material_execution_application import (
    MaterialExecutionCommand,
    MaterialExecutionLaneKey,
    MaterialExecutionLaneUnknown,
    MaterialExecutionOwnerLaneRegistry,
    MaterialExecutionResponse,
    MaterialRecoveryTicket,
)
from app.pipeline.material_execution_submission_coordinator import (
    MaterialExecutionSubmissionCoordinator,
)
from app.pipeline.material_execution_submission_owner_lease import (
    MaterialExecutionOwnerLeaseBusy,
    MaterialExecutionOwnerLeaseError,
    MaterialExecutionSubmissionOwnerLease,
)
from app.pipeline.material_execution_submission_store import (
    MaterialExecutionSubmissionErrorMetadata,
    MaterialExecutionSubmissionStore,
)


class _NonBlockingFuture(Future[MaterialExecutionResponse]):
    """Fail a test if submit tries to wait for unfinished owner-lane work."""

    def result(self, timeout: float | None = None) -> MaterialExecutionResponse:
        if not self.done():
            raise AssertionError("submission coordinator waited for an unfinished Future")
        return super().result(timeout=timeout)


class _Lane:
    def __init__(self) -> None:
        self.execution_futures: list[Future[MaterialExecutionResponse]] = []
        self.reconcile_futures: list[Future[MaterialExecutionResponse]] = []
        self.execution_calls: list[tuple[MaterialExecutionCommand, tuple[str, ...]]] = []
        self.reconcile_calls: list[MaterialRecoveryTicket] = []

    def submit_source_chain(
        self,
        command: MaterialExecutionCommand,
        *,
        source_chain: tuple[str, ...],
    ) -> Future[MaterialExecutionResponse]:
        self.execution_calls.append((command, tuple(source_chain)))
        return self.execution_futures.pop(0)

    def reconcile(
        self,
        ticket: MaterialRecoveryTicket,
    ) -> Future[MaterialExecutionResponse]:
        self.reconcile_calls.append(ticket)
        return self.reconcile_futures.pop(0)


class _Registry:
    def __init__(self, lane: _Lane) -> None:
        self.lane = lane
        self.keys: list[MaterialExecutionLaneKey] = []

    def get_or_create(self, key: MaterialExecutionLaneKey) -> _Lane:
        self.keys.append(key)
        return self.lane

    def close_all(self, *, timeout: float = 5.0) -> None:
        return None


def _context(tmp_path: Path) -> MaterialExecutionSubmissionContext:
    key = MaterialExecutionLaneKey(
        database_path=tmp_path / "production.db",
        pipeline_id=11,
        material_step_id=23,
    )
    command = MaterialExecutionCommand(
        pipeline_id=11,
        material_step_id=23,
        material_task_id="material-shot-1",
        execution_id=str(uuid4()),
        operation="pexels",
        startup_timeout_seconds=1.0,
        operation_timeout_seconds=30.0,
    )
    return MaterialExecutionSubmissionContext(
        lane_key=key,
        command=command,
        source_chain=("pexels", "pixabay"),
        snapshot_sha256="a" * 64,
    )


def _ticket(context: MaterialExecutionSubmissionContext) -> MaterialRecoveryTicket:
    return MaterialRecoveryTicket(
        ticket_id=str(uuid4()),
        lane_id=context.lane_key.lane_id,
        pipeline_id=context.command.pipeline_id,
        material_step_id=context.command.material_step_id,
        material_task_id=context.command.material_task_id,
        execution_id=context.command.execution_id,
        attempt_id=str(uuid4()),
        operation=context.command.operation,
    )


def _response(
    context: MaterialExecutionSubmissionContext,
    *,
    status: str = "succeeded",
    ticket: MaterialRecoveryTicket | None = None,
    attempt_id: str | None = None,
) -> MaterialExecutionResponse:
    return MaterialExecutionResponse(
        pipeline_id=context.command.pipeline_id,
        material_step_id=context.command.material_step_id,
        material_task_id=context.command.material_task_id,
        execution_id=context.command.execution_id,
        attempt_id=ticket.attempt_id if ticket is not None else attempt_id or str(uuid4()),
        operation=context.command.operation,
        status=status,  # type: ignore[arg-type]
        message="done",
        executed_task={"material_task_id": context.command.material_task_id},
        tasks=({"material_task_id": context.command.material_task_id},),
        recovery_ticket=ticket,
    )


def _coordinator(
    tmp_path: Path,
    lane: _Lane,
    *,
    owner_instance_id: str | None = None,
) -> MaterialExecutionSubmissionCoordinator:
    return MaterialExecutionSubmissionCoordinator(
        store=MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3"),
        registry=_Registry(lane),  # type: ignore[arg-type]
        owner_instance_id=owner_instance_id,
    )


def test_submit_returns_running_without_waiting_and_status_is_json_safe(tmp_path: Path):
    lane = _Lane()
    lane.execution_futures.append(_NonBlockingFuture())
    coordinator = _coordinator(tmp_path, lane)
    context = _context(tmp_path)

    submitted = coordinator.submit(context)

    assert submitted.state == "running"
    assert len(lane.execution_calls) == 1
    assert lane.execution_calls[0] == (context.command, context.source_chain)
    payload = submitted.to_json()
    assert json.loads(json.dumps(payload, allow_nan=False)) == payload
    assert not ({"future", "session", "raw_state"} & payload.keys())
    assert coordinator.status(submitted.submission_id) == submitted


def test_status_dto_rejects_untrusted_scalar_identity_fields(tmp_path: Path):
    lane = _Lane()
    lane.execution_futures.append(_NonBlockingFuture())
    coordinator = _coordinator(tmp_path, lane)
    submitted = coordinator.submit(_context(tmp_path))

    with pytest.raises(ValueError):
        replace(submitted, lane_id="C:\\client-selected.db")
    with pytest.raises(ValueError):
        replace(submitted, state="not-a-submission-state")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        replace(submitted, pipeline_id=0)


def test_explicit_empty_or_non_string_submission_ids_are_rejected(tmp_path: Path):
    lane = _Lane()
    lane.execution_futures.append(Future())
    coordinator = _coordinator(tmp_path, lane)
    context = _context(tmp_path)
    with pytest.raises(ValueError):
        coordinator.submit(context, submission_id="")
    with pytest.raises(ValueError):
        coordinator.submit(context, submission_id=0)  # type: ignore[arg-type]


def test_completed_future_callback_can_win_before_running_is_persisted(tmp_path: Path):
    lane = _Lane()
    context = _context(tmp_path)
    completed: Future[MaterialExecutionResponse] = Future()
    completed.set_result(_response(context))
    lane.execution_futures.append(completed)
    coordinator = _coordinator(tmp_path, lane)

    submitted = coordinator.submit(context)

    assert submitted.state == "succeeded"
    assert submitted.response is not None
    assert coordinator.status(submitted.submission_id).state == "succeeded"  # type: ignore[union-attr]


def test_callback_after_running_persists_one_terminal_response(tmp_path: Path):
    lane = _Lane()
    future: Future[MaterialExecutionResponse] = Future()
    lane.execution_futures.append(future)
    coordinator = _coordinator(tmp_path, lane)
    context = _context(tmp_path)
    submitted = coordinator.submit(context)

    future.set_result(_response(context))
    completed = coordinator.status(submitted.submission_id)
    assert completed is not None
    assert completed.state == "succeeded"
    terminal_revision = completed.revision

    repeated = coordinator.submit(context, submission_id=submitted.submission_id)
    assert repeated.state == "succeeded"
    assert repeated.revision == terminal_revision
    assert len(lane.execution_calls) == 1


def test_future_exception_becomes_bounded_typed_error(tmp_path: Path):
    lane = _Lane()
    future: Future[MaterialExecutionResponse] = Future()
    lane.execution_futures.append(future)
    coordinator = _coordinator(tmp_path, lane)
    context = _context(tmp_path)
    submitted = coordinator.submit(context)

    future.set_exception(RuntimeError("x" * 5000))

    failed = coordinator.status(submitted.submission_id)
    assert failed is not None
    assert failed.state == "failed"
    assert failed.response is None
    assert failed.error is not None
    assert len(failed.error["detail"]) <= 2000
    json.dumps(failed.to_json(), allow_nan=False)


def test_future_exception_redacts_physical_paths_from_status(tmp_path: Path):
    lane = _Lane()
    future: Future[MaterialExecutionResponse] = Future()
    lane.execution_futures.append(future)
    coordinator = _coordinator(tmp_path, lane)
    submitted = coordinator.submit(_context(tmp_path))

    future.set_exception(RuntimeError(r"worker failed at D:\secret\private.sqlite3"))

    failed = coordinator.status(submitted.submission_id)
    assert failed is not None and failed.error is not None
    assert "D:\\secret" not in failed.error["detail"]
    assert "[path]" in failed.error["detail"]


def test_malformed_future_result_becomes_unknown_not_a_retryable_failure(
    tmp_path: Path,
):
    lane = _Lane()
    future: Future[MaterialExecutionResponse] = Future()
    lane.execution_futures.append(future)
    coordinator = _coordinator(tmp_path, lane)
    context = _context(tmp_path)
    submitted = coordinator.submit(context)

    future.set_result(object())  # type: ignore[arg-type]

    unknown = coordinator.status(submitted.submission_id)
    assert unknown is not None
    assert unknown.state == "unknown"
    assert unknown.error is not None
    assert unknown.error["code"] == "submission_invalid_response"
    assert coordinator.submit(
        context,
        submission_id=submitted.submission_id,
    ).state == "unknown"


def test_unexpected_callback_persistence_exception_fails_closed_to_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    lane = _Lane()
    future: Future[MaterialExecutionResponse] = Future()
    lane.execution_futures.append(future)
    coordinator = _coordinator(tmp_path, lane)
    context = _context(tmp_path)
    submitted = coordinator.submit(context)

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("persistence callback exploded")

    monkeypatch.setattr(coordinator, "_persist_initial_response", explode)
    future.set_result(_response(context))

    unknown = coordinator.status(submitted.submission_id)
    assert unknown is not None
    assert unknown.state == "unknown"
    assert unknown.error is not None
    assert unknown.error["code"] == "submission_callback_unknown"


def test_owner_uncertain_future_is_unknown_and_never_auto_failed_or_retried(
    tmp_path: Path,
):
    lane = _Lane()
    future: Future[MaterialExecutionResponse] = Future()
    lane.execution_futures.append(future)
    coordinator = _coordinator(tmp_path, lane)
    context = _context(tmp_path)
    submitted = coordinator.submit(context)

    future.set_exception(MaterialExecutionLaneUnknown("ownership is uncertain"))

    unknown = coordinator.status(submitted.submission_id)
    assert unknown is not None
    assert unknown.state == "unknown"
    assert unknown.error is not None
    assert unknown.error["recovery_required"] is True
    assert coordinator.submit(
        context,
        submission_id=submitted.submission_id,
    ).state == "unknown"
    assert len(lane.execution_calls) == 1


def test_unknown_has_an_explicit_audited_close_path(tmp_path: Path):
    lane = _Lane()
    future: Future[MaterialExecutionResponse] = Future()
    lane.execution_futures.append(future)
    coordinator = _coordinator(tmp_path, lane)
    submitted = coordinator.submit(_context(tmp_path))
    future.set_exception(MaterialExecutionLaneUnknown("ownership is uncertain"))

    with pytest.raises(ValueError):
        coordinator.close_unknown(
            submitted.submission_id,
            error=MaterialExecutionSubmissionErrorMetadata(
                code="unsafe_manual_close",
                detail="retryable manual close",
                retryable=True,
                recovery_required=True,
            ),
        )

    closed = coordinator.close_unknown(submitted.submission_id)

    assert closed.state == "failed"
    assert closed.error is not None
    assert closed.error["code"] == "manual_unknown_close"


def test_live_blocked_submission_reconciles_asynchronously(tmp_path: Path):
    lane = _Lane()
    execution: Future[MaterialExecutionResponse] = Future()
    reconciliation: Future[MaterialExecutionResponse] = Future()
    lane.execution_futures.append(execution)
    lane.reconcile_futures.append(reconciliation)
    coordinator = _coordinator(tmp_path, lane)
    context = _context(tmp_path)
    submitted = coordinator.submit(context)
    ticket = _ticket(context)
    execution.set_result(_response(context, status="blocked", ticket=ticket))
    assert coordinator.status(submitted.submission_id).state == "blocked"  # type: ignore[union-attr]

    reconciling = coordinator.reconcile(submitted.submission_id)

    assert reconciling.state == "reconciling"
    assert lane.reconcile_calls == [ticket]
    reconciliation.set_result(_response(context, attempt_id=ticket.attempt_id))
    assert coordinator.status(submitted.submission_id).state == "succeeded"  # type: ignore[union-attr]


def test_live_reconcile_terminated_response_is_typed_failed(tmp_path: Path):
    lane = _Lane()
    execution: Future[MaterialExecutionResponse] = Future()
    reconciliation: Future[MaterialExecutionResponse] = Future()
    lane.execution_futures.append(execution)
    lane.reconcile_futures.append(reconciliation)
    coordinator = _coordinator(tmp_path, lane)
    context = _context(tmp_path)
    submitted = coordinator.submit(context)
    ticket = _ticket(context)
    execution.set_result(_response(context, status="blocked", ticket=ticket))
    coordinator.reconcile(submitted.submission_id)
    reconciliation.set_result(
        _response(context, status="terminated", attempt_id=ticket.attempt_id)
    )
    failed = coordinator.status(submitted.submission_id)
    assert failed is not None
    assert failed.state == "failed"
    assert failed.response is not None
    assert failed.error is None


def test_restart_sweep_does_not_mark_a_live_owner_unknown(tmp_path: Path):
    owner_instance_id = str(uuid4())
    lane = _Lane()
    lane.execution_futures.append(Future())
    coordinator = _coordinator(
        tmp_path,
        lane,
        owner_instance_id=owner_instance_id,
    )
    context = _context(tmp_path)
    submitted = coordinator.submit(context)

    swept = coordinator.restart_sweep()

    assert swept == ()
    assert coordinator.status(submitted.submission_id).state == "running"  # type: ignore[union-attr]
    assert coordinator.submit(
        context,
        submission_id=submitted.submission_id,
    ).state == "running"
    assert len(lane.execution_calls) == 1


def test_new_coordinator_cannot_take_over_a_live_owner(
    tmp_path: Path,
):
    lane = _Lane()
    lane.execution_futures.append(Future())
    first = _coordinator(tmp_path, lane)
    context = _context(tmp_path)
    submitted = first.submit(context)

    # A fresh coordinator cannot infer death from a UUID or timestamp.  The
    # live owner mutex must reject takeover before the database changes.
    with pytest.raises(MaterialExecutionOwnerLeaseBusy):
        _coordinator(tmp_path, _Lane())
    assert len(lane.execution_calls) == 1
    assert first.status(submitted.submission_id).state == "running"  # type: ignore[union-attr]


def test_one_owner_lease_cannot_drive_two_coordinators_or_registries(
    tmp_path: Path,
):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    lease = MaterialExecutionSubmissionOwnerLease.acquire(store)
    first = MaterialExecutionSubmissionCoordinator(
        store=store,
        registry=_Registry(_Lane()),  # type: ignore[arg-type]
        owner_lease=lease,
    )
    try:
        with pytest.raises(MaterialExecutionOwnerLeaseError, match="already bound"):
            MaterialExecutionSubmissionCoordinator(
                store=store,
                registry=_Registry(_Lane()),  # type: ignore[arg-type]
                owner_lease=lease,
            )
    finally:
        first.close()


def test_coordinator_runs_with_real_owner_lane_and_explicit_shutdown(tmp_path: Path):
    context = _context(tmp_path)

    class _Handler:
        def execute(self, command):
            return _response(context)

        def execute_source_chain(self, command, *, source_chain):
            assert tuple(source_chain) == context.source_chain
            return _response(context)

        def reconcile(self, state, ticket):
            raise AssertionError((state, ticket))

    registry = MaterialExecutionOwnerLaneRegistry(
        handler_factory=lambda _key: _Handler()
    )
    coordinator = MaterialExecutionSubmissionCoordinator(
        store=MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3"),
        registry=registry,
    )
    try:
        submitted = coordinator.submit(context)
        # ``submit`` is intentionally non-blocking.  The real owner lane may
        # still be running on its dedicated thread when the acknowledgement
        # returns, so wait only by polling the durable status projection before
        # asking the registry to shut down.
        deadline = time.monotonic() + 5.0
        current = submitted
        while current.state not in {"succeeded", "failed", "unknown"}:
            if time.monotonic() >= deadline:
                pytest.fail(f"owner lane did not reach a terminal state: {current.state}")
            time.sleep(0.01)
            current = coordinator.status(submitted.submission_id)
            assert current is not None
        assert current.state == "succeeded"
        assert current.response is not None
    finally:
        coordinator.close(timeout=5)
