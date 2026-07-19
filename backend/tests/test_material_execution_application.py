from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading
from uuid import uuid4

import pytest

from app.pipeline.material_execution_application import (
    MaterialExecutionCommand,
    MaterialExecutionLaneBusy,
    MaterialExecutionLaneKey,
    MaterialExecutionOwnerLane,
    MaterialExecutionOwnerLaneRegistry,
    MaterialExecutionResponse,
    MaterialExecutionRetainedOwnership,
    MaterialRecoveryTicket,
    MaterialRunnerRetirementRecoveryCommand,
)


def _command(*, pipeline_id: int = 18, material_step_id: int = 31):
    return MaterialExecutionCommand(
        pipeline_id=pipeline_id,
        material_step_id=material_step_id,
        material_task_id="material-shot-01",
        execution_id=str(uuid4()),
        operation="pexels",
        startup_timeout_seconds=2.0,
        operation_timeout_seconds=180.0,
    )


def _response(command: MaterialExecutionCommand, *, status: str = "blocked"):
    return MaterialExecutionResponse(
        pipeline_id=command.pipeline_id,
        material_step_id=command.material_step_id,
        material_task_id=command.material_task_id,
        execution_id=command.execution_id,
        attempt_id=str(uuid4()),
        operation=command.operation,
        status=status,
        message="retained" if status == "blocked" else "completed",
        executed_task={"material_task_id": command.material_task_id, "status": status},
        tasks=({"material_task_id": command.material_task_id, "status": status},),
    )


def _runner_retirement_command(
    *,
    pipeline_id: int = 18,
    material_step_id: int = 31,
) -> MaterialRunnerRetirementRecoveryCommand:
    return MaterialRunnerRetirementRecoveryCommand(
        pipeline_id=pipeline_id,
        material_step_id=material_step_id,
        material_task_id="material-shot-01",
        execution_id=str(uuid4()),
        attempt_id=str(uuid4()),
        operation="pexels",
        control_id=str(uuid4()),
        job_instance_id=str(uuid4()),
        completion_status="failed",
        completion_revision=8,
        checkpoint_sha256="a" * 64,
    )


class _SameThreadOwner:
    def __init__(self) -> None:
        self.thread = threading.current_thread()
        self.released = False

    def release(self) -> None:
        if threading.current_thread() is not self.thread:
            raise RuntimeError("owner released from a different thread")
        self.released = True


class _RetainingHandler:
    def __init__(self) -> None:
        self.threads: list[threading.Thread] = []
        self.owner: _SameThreadOwner | None = None

    def execute(self, command: MaterialExecutionCommand):
        self.threads.append(threading.current_thread())
        self.owner = _SameThreadOwner()
        return MaterialExecutionRetainedOwnership(
            response=_response(command),
            state=self.owner,
        )

    def reconcile(self, state: object, ticket: MaterialRecoveryTicket):
        self.threads.append(threading.current_thread())
        assert state is self.owner
        assert ticket.material_task_id == "material-shot-01"
        assert isinstance(state, _SameThreadOwner)
        state.release()
        blocked = _response(_command())
        return replace(
            blocked,
            pipeline_id=ticket.pipeline_id,
            material_step_id=ticket.material_step_id,
            material_task_id=ticket.material_task_id,
            execution_id=ticket.execution_id,
            attempt_id=ticket.attempt_id,
            operation=ticket.operation,
            status="terminated",
            message="reconciled",
        )


def test_owner_lane_keeps_raw_ownership_on_one_permanent_thread(tmp_path: Path):
    key = MaterialExecutionLaneKey(
        database_path=tmp_path / "lane.db",
        pipeline_id=18,
        material_step_id=31,
    )
    handler = _RetainingHandler()
    lane = MaterialExecutionOwnerLane(key=key, handler=handler)
    command = _command()
    try:
        first = lane.submit(command).result(timeout=5)

        assert first.status == "blocked"
        assert type(first.recovery_ticket) is MaterialRecoveryTicket
        assert "_SameThreadOwner" not in repr(first)
        assert handler.owner is not None and handler.owner.released is False
        assert handler.threads[0] is not threading.current_thread()

        final = lane.reconcile(first.recovery_ticket).result(timeout=5)

        assert final.status == "terminated"
        assert final.recovery_ticket is None
        assert handler.owner.released is True
        assert handler.threads[0] is handler.threads[1]
        assert handler.threads[0].ident == lane.owner_thread_ident
    finally:
        lane.close(timeout=5)


def test_owner_lane_refuses_shutdown_while_recovery_ownership_is_retained(
    tmp_path: Path,
):
    key = MaterialExecutionLaneKey(
        database_path=tmp_path / "lane.db",
        pipeline_id=18,
        material_step_id=31,
    )
    handler = _RetainingHandler()
    lane = MaterialExecutionOwnerLane(key=key, handler=handler)
    first = lane.submit(_command()).result(timeout=5)

    with pytest.raises(MaterialExecutionLaneBusy, match="retained"):
        lane.close(timeout=5)

    lane.reconcile(first.recovery_ticket).result(timeout=5)
    lane.close(timeout=5)


def test_owner_lane_rejects_new_execute_until_recovery_is_reconciled(tmp_path: Path):
    key = MaterialExecutionLaneKey(
        database_path=tmp_path / "lane.db",
        pipeline_id=18,
        material_step_id=31,
    )
    handler = _RetainingHandler()
    lane = MaterialExecutionOwnerLane(key=key, handler=handler)
    first = lane.submit(_command()).result(timeout=5)
    second = replace(
        _command(),
        material_task_id="material-shot-02",
        execution_id=str(uuid4()),
    )
    try:
        with pytest.raises(MaterialExecutionLaneBusy, match="recovery ownership"):
            lane.submit(second).result(timeout=5)
        lane.reconcile(first.recovery_ticket).result(timeout=5)
    finally:
        lane.close(timeout=5)


def test_recovery_ticket_cannot_be_sent_to_another_material_step(tmp_path: Path):
    first_handler = _RetainingHandler()
    second_handler = _RetainingHandler()
    first_lane = MaterialExecutionOwnerLane(
        key=MaterialExecutionLaneKey(
            database_path=tmp_path / "lane.db",
            pipeline_id=18,
            material_step_id=31,
        ),
        handler=first_handler,
    )
    second_lane = MaterialExecutionOwnerLane(
        key=MaterialExecutionLaneKey(
            database_path=tmp_path / "lane.db",
            pipeline_id=18,
            material_step_id=32,
        ),
        handler=second_handler,
    )
    response = first_lane.submit(_command()).result(timeout=5)
    try:
        with pytest.raises(ValueError, match="another material step"):
            second_lane.reconcile(response.recovery_ticket)
    finally:
        first_lane.reconcile(response.recovery_ticket).result(timeout=5)
        first_lane.close(timeout=5)
        second_lane.close(timeout=5)


def test_owner_lane_serializes_commands_for_the_same_material_step(tmp_path: Path):
    events: list[tuple[str, int]] = []

    class _Handler:
        def execute(self, command: MaterialExecutionCommand):
            events.append((command.material_task_id, threading.get_ident()))
            return _response(command, status="failed")

        def reconcile(self, state: object, ticket: MaterialRecoveryTicket):
            raise AssertionError((state, ticket))

    lane = MaterialExecutionOwnerLane(
        key=MaterialExecutionLaneKey(
            database_path=tmp_path / "lane.db",
            pipeline_id=18,
            material_step_id=31,
        ),
        handler=_Handler(),
    )
    first = _command()
    second = replace(first, material_task_id="material-shot-02", execution_id=str(uuid4()))
    try:
        first_future = lane.submit(first)
        second_future = lane.submit(second)
        assert first_future.result(timeout=5).status == "failed"
        assert second_future.result(timeout=5).status == "failed"
        assert [item[0] for item in events] == ["material-shot-01", "material-shot-02"]
        assert len({item[1] for item in events}) == 1
    finally:
        lane.close(timeout=5)


def test_owner_lane_runs_handler_close_on_the_owner_thread(tmp_path: Path):
    key = MaterialExecutionLaneKey(
        database_path=tmp_path / "lane.db",
        pipeline_id=18,
        material_step_id=31,
    )
    close_threads: list[int] = []

    class _Handler:
        def execute(self, command: MaterialExecutionCommand):
            return _response(command, status="failed")

        def reconcile(self, state: object, ticket: MaterialRecoveryTicket):
            raise AssertionError((state, ticket))

        def close(self):
            close_threads.append(threading.get_ident())

    lane = MaterialExecutionOwnerLane(key=key, handler=_Handler())
    try:
        assert lane.submit(_command()).result(timeout=5).status == "failed"
    finally:
        lane.close(timeout=5)

    assert close_threads == [lane.owner_thread_ident]
    assert close_threads[0] != threading.get_ident()


def test_owner_lane_dispatches_runner_retirement_without_assuming_recovery_logic(
    tmp_path: Path,
):
    command = _runner_retirement_command()
    observed: list[tuple[MaterialRunnerRetirementRecoveryCommand, int]] = []

    class _Handler:
        def execute(self, received):
            raise AssertionError(received)

        def reconcile(self, state, ticket):
            raise AssertionError((state, ticket))

        def recover_runner_retirement(self, received):
            observed.append((received, threading.get_ident()))
            return MaterialExecutionResponse(
                pipeline_id=received.pipeline_id,
                material_step_id=received.material_step_id,
                material_task_id=received.material_task_id,
                execution_id=received.execution_id,
                attempt_id=received.attempt_id,
                operation=received.operation,
                status=received.completion_status,
                message="runner retired",
                executed_task=None,
                tasks=(),
            )

    lane = MaterialExecutionOwnerLane(
        key=MaterialExecutionLaneKey(
            database_path=tmp_path / "lane.db",
            pipeline_id=command.pipeline_id,
            material_step_id=command.material_step_id,
        ),
        handler=_Handler(),
    )
    try:
        response = lane.recover_runner_retirement(command).result(timeout=5)
    finally:
        lane.close(timeout=5)

    assert response.status == "failed"
    assert observed == [(command, lane.owner_thread_ident)]


def test_lane_registry_reuses_only_the_same_physical_material_step(tmp_path: Path):
    handlers: list[_RetainingHandler] = []

    def factory(_key: MaterialExecutionLaneKey):
        handler = _RetainingHandler()
        handlers.append(handler)
        return handler

    registry = MaterialExecutionOwnerLaneRegistry(handler_factory=factory)
    first_key = MaterialExecutionLaneKey(
        database_path=tmp_path / "lane.db",
        pipeline_id=18,
        material_step_id=31,
    )
    second_key = MaterialExecutionLaneKey(
        database_path=tmp_path / "lane.db",
        pipeline_id=18,
        material_step_id=32,
    )
    first = registry.get_or_create(first_key)
    assert registry.get_or_create(first_key) is first
    second = registry.get_or_create(second_key)
    assert second is not first
    assert len(handlers) == 2
    registry.close_all(timeout=5)
