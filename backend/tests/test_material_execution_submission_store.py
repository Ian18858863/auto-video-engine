from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path
import sqlite3
import os
from uuid import uuid4

import pytest

from app.core.windows_named_mutex import build_mutex_name
from app.pipeline.material_execution_api import serialize_material_execution_response
from app.pipeline.material_execution_api_context import MaterialExecutionSubmissionContext
from app.pipeline.material_execution_application import (
    MaterialExecutionCommand,
    MaterialExecutionLaneKey,
    MaterialExecutionResponse,
    MaterialRecoveryTicket,
)
from app.pipeline import material_execution_submission_store as store_module
from app.pipeline.material_execution_submission_store import (
    MaterialExecutionSubmissionErrorMetadata,
    MaterialExecutionSubmissionIdentityConflict,
    MaterialExecutionSubmissionPathError,
    MaterialExecutionSubmissionSchemaError,
    MaterialExecutionSubmissionStateConflict,
    MaterialExecutionSubmissionStore,
    MaterialExecutionSubmissionTransitionError,
)


def _owner_mutex_name(database_path: Path) -> str:
    physical = os.path.realpath(os.path.abspath(os.fspath(database_path)))
    return build_mutex_name(
        "material-submission-owner", physical.replace("/", "\\").casefold()
    )


def _context(
    tmp_path: Path,
    *,
    execution_id: str | None = None,
    task_id: str = "material-shot-1",
) -> MaterialExecutionSubmissionContext:
    lane_key = MaterialExecutionLaneKey(
        database_path=(tmp_path / "production.db"),
        pipeline_id=11,
        material_step_id=23,
    )
    command = MaterialExecutionCommand(
        pipeline_id=11,
        material_step_id=23,
        material_task_id=task_id,
        execution_id=execution_id or str(uuid4()),
        operation="pexels",
        startup_timeout_seconds=1.0,
        operation_timeout_seconds=30.0,
    )
    return MaterialExecutionSubmissionContext(
        lane_key=lane_key,
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
    message: str = "done",
) -> MaterialExecutionResponse:
    attempt_id = ticket.attempt_id if ticket is not None else str(uuid4())
    return MaterialExecutionResponse(
        pipeline_id=context.command.pipeline_id,
        material_step_id=context.command.material_step_id,
        material_task_id=context.command.material_task_id,
        execution_id=context.command.execution_id,
        attempt_id=attempt_id,
        operation=context.command.operation,
        status=status,  # type: ignore[arg-type]
        message=message,
        executed_task={"material_task_id": context.command.material_task_id},
        tasks=({"material_task_id": context.command.material_task_id},),
        recovery_ticket=ticket,
    )


def _advance_to_running(
    store: MaterialExecutionSubmissionStore,
    submission_id: str,
    *,
    owner_instance_id: str,
) -> None:
    store.compare_and_swap(
        submission_id,
        expected_state="accepted",
        next_state="dispatching",
        owner_instance_id=owner_instance_id,
    )
    store.compare_and_swap(
        submission_id,
        expected_state="dispatching",
        next_state="running",
        owner_instance_id=owner_instance_id,
    )


def test_store_requires_an_absolute_physical_d_file(tmp_path: Path, monkeypatch):
    assert tmp_path.drive.upper() == "D:"
    with pytest.raises(MaterialExecutionSubmissionPathError):
        MaterialExecutionSubmissionStore(Path("C:/submission.sqlite3"))
    with pytest.raises(MaterialExecutionSubmissionPathError):
        MaterialExecutionSubmissionStore(Path(":memory:"))
    with pytest.raises(MaterialExecutionSubmissionPathError):
        MaterialExecutionSubmissionStore(tmp_path)

    simulated_reparse = tmp_path / "reparse.sqlite3"
    simulated_reparse.touch()
    original = store_module._is_reparse_point
    monkeypatch.setattr(
        store_module,
        "_is_reparse_point",
        lambda path: path == simulated_reparse or original(path),
    )
    with pytest.raises(MaterialExecutionSubmissionPathError):
        MaterialExecutionSubmissionStore(simulated_reparse)
    for ambiguous in (
        "D:/CodexData/../unsafe.sqlite3",
        "D:/CodexData/unsafe.sqlite3:secret",
        "D:/CodexData/CON.sqlite3",
        "D:/CodexData/unsafe.sqlite3 ",
    ):
        with pytest.raises(MaterialExecutionSubmissionPathError):
            MaterialExecutionSubmissionStore(ambiguous)


def test_every_store_connection_uses_delete_full_and_foreign_keys(tmp_path: Path):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")

    with store._transaction(write=False) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_create_is_durable_idempotent_and_rejects_context_conflicts(tmp_path: Path):
    database_path = tmp_path / "submissions.sqlite3"
    store = MaterialExecutionSubmissionStore(database_path)
    submission_id = str(uuid4())
    context = _context(tmp_path)

    created = store.create(submission_id, context)
    repeated = store.create(submission_id, context)
    reopened = MaterialExecutionSubmissionStore(database_path).get(submission_id)

    assert created == repeated == reopened
    assert created.schema_version == 2
    assert created.submission_id == submission_id
    assert created.context == context
    assert created.state == "accepted"
    assert created.revision == 0
    assert created.response_json is None
    assert created.error is None
    assert created.recovery_ticket is None

    with pytest.raises(MaterialExecutionSubmissionIdentityConflict):
        store.create(submission_id, _context(tmp_path, task_id="material-shot-2"))


def test_store_schema_contains_only_serializable_submission_metadata(tmp_path: Path):
    database_path = tmp_path / "submissions.sqlite3"
    store = MaterialExecutionSubmissionStore(database_path)
    store.create(str(uuid4()), _context(tmp_path))

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(material_execution_submissions)"
            ).fetchall()
        }
    assert {
        "schema_version",
        "submission_id",
        "lane_id",
        "database_path",
        "command_json",
        "source_chain_json",
        "snapshot_sha256",
        "state",
        "response_json",
        "error_json",
        "recovery_ticket_json",
    } <= columns
    assert not ({"future", "session", "mutex", "raw_state"} & columns)


def test_cas_success_persists_only_the_typed_response_serializer(tmp_path: Path):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    submission_id = str(uuid4())
    owner_instance_id = str(uuid4())
    context = _context(tmp_path)
    store.create(submission_id, context)
    _advance_to_running(store, submission_id, owner_instance_id=owner_instance_id)
    response = _response(context)

    completed = store.compare_and_swap(
        submission_id,
        expected_state="running",
        next_state="succeeded",
        response=response,
    )

    assert completed.state == "succeeded"
    assert completed.attempt_id == response.attempt_id
    assert completed.response_json == serialize_material_execution_response(response)
    assert completed.error is None
    assert completed.recovery_ticket is None
    assert completed.revision == 3
    assert completed.updated_at > completed.created_at

    with pytest.raises(TypeError, match="MaterialExecutionResponse"):
        store.compare_and_swap(
            submission_id,
            expected_state="succeeded",
            next_state="succeeded",
            response={"status": "succeeded"},  # type: ignore[arg-type]
        )


def test_cas_rejects_illegal_or_stale_transitions_without_mutation(tmp_path: Path):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    submission_id = str(uuid4())
    context = _context(tmp_path)
    store.create(submission_id, context)

    with pytest.raises(MaterialExecutionSubmissionTransitionError):
        store.compare_and_swap(
            submission_id,
            expected_state="accepted",
            next_state="running",
        )

    owner_instance_id = str(uuid4())
    store.compare_and_swap(
        submission_id,
        expected_state="accepted",
        next_state="dispatching",
        owner_instance_id=owner_instance_id,
    )
    with pytest.raises(MaterialExecutionSubmissionStateConflict):
        store.compare_and_swap(
            submission_id,
            expected_state="accepted",
            next_state="dispatching",
            owner_instance_id=owner_instance_id,
        )
    assert store.get(submission_id).state == "dispatching"  # type: ignore[union-attr]


def test_terminal_response_and_recovery_ticket_must_match_submission_identity(
    tmp_path: Path,
):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    submission_id = str(uuid4())
    owner_instance_id = str(uuid4())
    context = _context(tmp_path)
    store.create(submission_id, context)
    _advance_to_running(store, submission_id, owner_instance_id=owner_instance_id)

    other_context = _context(tmp_path, task_id="material-shot-2")
    with pytest.raises(MaterialExecutionSubmissionIdentityConflict):
        store.compare_and_swap(
            submission_id,
            expected_state="running",
            next_state="succeeded",
            response=_response(other_context),
        )

    wrong_ticket = _ticket(other_context)
    with pytest.raises(MaterialExecutionSubmissionIdentityConflict):
        store.compare_and_swap(
            submission_id,
            expected_state="running",
            next_state="blocked",
            recovery_ticket=wrong_ticket,
        )
    assert store.get(submission_id).state == "running"  # type: ignore[union-attr]


def test_blocked_reconcile_flow_round_trips_a_typed_recovery_ticket(tmp_path: Path):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    submission_id = str(uuid4())
    owner_instance_id = str(uuid4())
    context = _context(tmp_path)
    store.create(submission_id, context)
    _advance_to_running(store, submission_id, owner_instance_id=owner_instance_id)
    ticket = _ticket(context)
    blocked_response = _response(context, status="blocked", ticket=ticket)

    blocked = store.compare_and_swap(
        submission_id,
        expected_state="running",
        next_state="blocked",
        response=blocked_response,
        recovery_ticket=ticket,
    )
    assert blocked.recovery_ticket == ticket
    assert blocked.attempt_id == ticket.attempt_id

    reconciling = store.compare_and_swap(
        submission_id,
        expected_state="blocked",
        next_state="reconciling",
        owner_instance_id=owner_instance_id,
    )
    assert reconciling.state == "reconciling"
    assert reconciling.recovery_ticket == ticket

    still_blocked = store.compare_and_swap(
        submission_id,
        expected_state="reconciling",
        next_state="blocked",
        response=blocked_response,
        recovery_ticket=ticket,
    )
    assert still_blocked.state == "blocked"
    assert still_blocked.recovery_ticket == ticket


def test_failed_and_unknown_errors_are_typed_json_safe_and_bounded(tmp_path: Path):
    with pytest.raises(TypeError, match="JSON"):
        MaterialExecutionSubmissionErrorMetadata(
            code="worker_failure",
            detail="failed",
            data={"not_json": object()},
        )
    with pytest.raises(ValueError, match="bounded|long|limit"):
        MaterialExecutionSubmissionErrorMetadata(
            code="worker_failure",
            detail="x" * 2001,
        )

    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    submission_id = str(uuid4())
    owner_instance_id = str(uuid4())
    store.create(submission_id, _context(tmp_path))
    _advance_to_running(store, submission_id, owner_instance_id=owner_instance_id)
    error = MaterialExecutionSubmissionErrorMetadata(
        code="worker_failure",
        detail="typed failure",
        retryable=False,
        recovery_required=False,
        data={"phase": "worker"},
    )

    failed = store.compare_and_swap(
        submission_id,
        expected_state="running",
        next_state="failed",
        error=error,
    )
    assert failed.error == error
    assert failed.response_json is None


def test_list_inflight_and_restart_unknown_never_retry_or_touch_another_owner(
    tmp_path: Path,
):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    owner = str(uuid4())
    other_owner = str(uuid4())
    running_id = str(uuid4())
    other_id = str(uuid4())
    accepted_id = str(uuid4())
    store.create(running_id, _context(tmp_path))
    store.create(other_id, _context(tmp_path))
    store.create(accepted_id, _context(tmp_path))
    _advance_to_running(store, running_id, owner_instance_id=owner)
    _advance_to_running(store, other_id, owner_instance_id=other_owner)

    marked = store._mark_inflight_restart_unknown_legacy(owner)

    assert tuple(record.submission_id for record in marked) == (running_id,)
    restarted = store.get(running_id)
    assert restarted is not None
    assert restarted.state == "unknown"
    assert restarted.error is not None
    assert restarted.error.code == "restart_unknown"
    assert restarted.error.retryable is False
    assert store.get(other_id).state == "running"  # type: ignore[union-attr]
    assert store.get(accepted_id).state == "accepted"  # type: ignore[union-attr]
    assert {record.submission_id for record in store.list_inflight()} == {
        accepted_id,
        other_id,
    }


def test_restart_marks_reconciling_unknown_but_preserves_recovery_identity(
    tmp_path: Path,
):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    context = _context(tmp_path)
    submission_id = str(uuid4())
    owner = str(uuid4())
    store.create(submission_id, context)
    _advance_to_running(store, submission_id, owner_instance_id=owner)
    ticket = _ticket(context)
    store.compare_and_swap(
        submission_id,
        expected_state="running",
        next_state="blocked",
        recovery_ticket=ticket,
    )
    store.compare_and_swap(
        submission_id,
        expected_state="blocked",
        next_state="reconciling",
        owner_instance_id=owner,
    )

    marked = store._mark_inflight_restart_unknown_legacy(owner)

    assert tuple(item.submission_id for item in marked) == (submission_id,)
    assert marked[0].state == "unknown"
    assert marked[0].attempt_id == ticket.attempt_id
    assert marked[0].recovery_ticket == ticket
    assert marked[0].error is not None
    assert marked[0].error.code == "restart_unknown"


def test_reconciling_can_persist_typed_terminal_failure_without_ticket(
    tmp_path: Path,
):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    context = _context(tmp_path)
    submission_id = str(uuid4())
    owner = str(uuid4())
    store.create(submission_id, context)
    _advance_to_running(store, submission_id, owner_instance_id=owner)
    ticket = _ticket(context)
    store.compare_and_swap(
        submission_id,
        expected_state="running",
        next_state="blocked",
        recovery_ticket=ticket,
    )
    store.compare_and_swap(
        submission_id,
        expected_state="blocked",
        next_state="reconciling",
        owner_instance_id=owner,
    )
    terminal = _response(context, status="terminated")
    terminal = terminal.__class__(
        pipeline_id=terminal.pipeline_id,
        material_step_id=terminal.material_step_id,
        material_task_id=terminal.material_task_id,
        execution_id=terminal.execution_id,
        attempt_id=ticket.attempt_id,
        operation=terminal.operation,
        status=terminal.status,
        message=terminal.message,
        executed_task=terminal.executed_task,
        tasks=terminal.tasks,
    )
    failed = store.compare_and_swap(
        submission_id,
        expected_state="reconciling",
        next_state="failed",
        response=terminal,
    )
    assert failed.state == "failed"
    assert failed.response_json is not None
    assert failed.error is None
    assert failed.recovery_ticket is None


def test_create_and_cas_are_thread_safe_and_transactional(tmp_path: Path):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    context = _context(tmp_path)
    submission_id = str(uuid4())

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(
            pool.map(lambda _index: store.create(submission_id, context), range(32))
        )
    assert {record.submission_id for record in records} == {submission_id}
    assert {record.revision for record in records} == {0}

    owner = str(uuid4())
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(
                store.compare_and_swap,
                submission_id,
                expected_state="accepted",
                next_state="dispatching",
                owner_instance_id=owner,
            )
            for _index in range(8)
        ]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result().state)
        except MaterialExecutionSubmissionStateConflict:
            outcomes.append("conflict")
    assert outcomes.count("dispatching") == 1
    assert outcomes.count("conflict") == 7


def test_unknown_row_schema_version_fails_closed(tmp_path: Path):
    database_path = tmp_path / "submissions.sqlite3"
    store = MaterialExecutionSubmissionStore(database_path)
    submission_id = str(uuid4())
    store.create(submission_id, _context(tmp_path))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE material_execution_submissions SET schema_version = 99 "
            "WHERE submission_id = ?",
            (submission_id,),
        )
        connection.commit()

    with pytest.raises(MaterialExecutionSubmissionSchemaError):
        store.get(submission_id)


def test_public_record_and_error_metadata_are_frozen_dataclasses():
    record_field_names = {
        field.name for field in fields(store_module.MaterialExecutionSubmissionRecord)
    }
    assert {"context", "state", "response_json", "error", "recovery_ticket"} <= (
        record_field_names
    )
    assert store_module.MaterialExecutionSubmissionRecord.__dataclass_params__.frozen
    assert MaterialExecutionSubmissionErrorMetadata.__dataclass_params__.frozen


def test_terminal_transition_requires_matching_evidence_and_rejects_ticketed_success(
    tmp_path: Path,
):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    submission_id = str(uuid4())
    owner_instance_id = str(uuid4())
    context = _context(tmp_path)
    store.create(submission_id, context)
    _advance_to_running(store, submission_id, owner_instance_id=owner_instance_id)

    with pytest.raises(store_module.MaterialExecutionSubmissionTransitionError):
        store.compare_and_swap(
            submission_id,
            expected_state="running",
            next_state="succeeded",
        )

    ticket = _ticket(context)
    ticketed_success = _response(context, status="succeeded", ticket=ticket)
    with pytest.raises(store_module.MaterialExecutionSubmissionTransitionError):
        store.compare_and_swap(
            submission_id,
            expected_state="running",
            next_state="succeeded",
            response=ticketed_success,
            recovery_ticket=ticket,
        )
    assert store.get(submission_id).state == "running"  # type: ignore[union-attr]


def test_row_identity_tampering_fails_closed(tmp_path: Path):
    database_path = tmp_path / "submissions.sqlite3"
    store = MaterialExecutionSubmissionStore(database_path)
    submission_id = str(uuid4())
    store.create(submission_id, _context(tmp_path))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE material_execution_submissions SET material_task_id = ? "
            "WHERE submission_id = ?",
            ("material-shot-tampered", submission_id),
        )
        connection.commit()
    with pytest.raises(MaterialExecutionSubmissionSchemaError):
        store.get(submission_id)


def test_second_store_instance_cas_cannot_overwrite_first_transition(tmp_path: Path):
    database_path = tmp_path / "submissions.sqlite3"
    first = MaterialExecutionSubmissionStore(database_path)
    second = MaterialExecutionSubmissionStore(database_path)
    submission_id = str(uuid4())
    context = _context(tmp_path)
    first.create(submission_id, context)
    owner = str(uuid4())
    first.compare_and_swap(
        submission_id,
        expected_state="accepted",
        next_state="dispatching",
        owner_instance_id=owner,
    )
    with pytest.raises(store_module.MaterialExecutionSubmissionStateConflict):
        second.compare_and_swap(
            submission_id,
            expected_state="accepted",
            next_state="dispatching",
            owner_instance_id=owner,
        )
    assert second.get(submission_id).state == "dispatching"  # type: ignore[union-attr]


def test_preexisting_unrecognised_sidecars_fail_closed(tmp_path: Path):
    empty_path = tmp_path / "empty.sqlite3"
    empty_path.touch()
    with pytest.raises(MaterialExecutionSubmissionSchemaError):
        MaterialExecutionSubmissionStore(empty_path)

    unrelated_path = tmp_path / "unrelated.sqlite3"
    with sqlite3.connect(unrelated_path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.commit()
    with pytest.raises(MaterialExecutionSubmissionSchemaError):
        MaterialExecutionSubmissionStore(unrelated_path)


def test_callback_can_win_the_accepted_or_dispatching_crash_window(tmp_path: Path):
    context = _context(tmp_path)
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")

    accepted_id = str(uuid4())
    store.create(accepted_id, context)
    accepted_done = store.compare_and_swap(
        accepted_id,
        expected_state="accepted",
        next_state="succeeded",
        response=_response(context),
    )
    assert accepted_done.state == "succeeded"
    with pytest.raises(MaterialExecutionSubmissionStateConflict):
        store.compare_and_swap(
            accepted_id,
            expected_state="accepted",
            next_state="failed",
            error=MaterialExecutionSubmissionErrorMetadata(
                code="late_callback", detail="must not overwrite first evidence"
            ),
        )

    dispatching_id = str(uuid4())
    store.create(dispatching_id, context)
    owner = str(uuid4())
    store.compare_and_swap(
        dispatching_id,
        expected_state="accepted",
        next_state="dispatching",
        owner_instance_id=owner,
    )
    failed = store.compare_and_swap(
        dispatching_id,
        expected_state="dispatching",
        next_state="failed",
        error=MaterialExecutionSubmissionErrorMetadata(
            code="enqueue_failure", detail="typed dispatch failure"
        ),
    )
    assert failed.state == "failed"
    assert failed.response_json is None
    assert failed.error is not None


def test_unknown_requires_explicit_close_or_adoption_and_is_not_inflight(
    tmp_path: Path,
):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    context = _context(tmp_path)
    submission_id = str(uuid4())
    owner = str(uuid4())
    store.create(submission_id, context)
    _advance_to_running(store, submission_id, owner_instance_id=owner)
    store._mark_inflight_restart_unknown_legacy(owner)

    with pytest.raises(MaterialExecutionSubmissionTransitionError):
        store.compare_and_swap(
            submission_id,
            expected_state="unknown",
            next_state="running",
            owner_instance_id=owner,
        )
    with pytest.raises(MaterialExecutionSubmissionTransitionError):
        store.compare_and_swap(
            submission_id,
            expected_state="unknown",
            next_state="failed",
        )

    close_error = MaterialExecutionSubmissionErrorMetadata(
        code="manual_close", detail="operator confirmed the attempt was not run"
    )
    closed = store.compare_and_swap(
        submission_id,
        expected_state="unknown",
        next_state="failed",
        error=close_error,
    )
    assert closed.state == "failed"
    assert closed.error == close_error
    assert store.list_unresolved() == ()


def test_attempt_id_is_derived_from_typed_evidence_and_cannot_be_forged(
    tmp_path: Path,
):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    context = _context(tmp_path)
    submission_id = str(uuid4())
    owner = str(uuid4())
    store.create(submission_id, context)
    with pytest.raises(MaterialExecutionSubmissionTransitionError):
        store.compare_and_swap(
            submission_id,
            expected_state="accepted",
            next_state="dispatching",
            owner_instance_id=owner,
            attempt_id=str(uuid4()),
        )
    _advance_to_running(store, submission_id, owner_instance_id=owner)

    ticket = _ticket(context)
    with pytest.raises(MaterialExecutionSubmissionIdentityConflict):
        store.compare_and_swap(
            submission_id,
            expected_state="running",
            next_state="blocked",
            recovery_ticket=ticket,
            attempt_id=str(uuid4()),
        )

    blocked = store.compare_and_swap(
        submission_id,
        expected_state="running",
        next_state="blocked",
        recovery_ticket=ticket,
    )
    assert blocked.attempt_id == ticket.attempt_id

    with pytest.raises(MaterialExecutionSubmissionIdentityConflict):
        store.compare_and_swap(
            submission_id,
            expected_state="blocked",
            next_state="reconciling",
            owner_instance_id=owner,
            recovery_ticket=MaterialRecoveryTicket(
                ticket_id=str(uuid4()),
                lane_id=context.lane_key.lane_id,
                pipeline_id=context.command.pipeline_id,
                material_step_id=context.command.material_step_id,
                material_task_id=context.command.material_task_id,
                execution_id=context.command.execution_id,
                attempt_id=str(uuid4()),
                operation=context.command.operation,
            ),
        )


def test_terminal_transition_rejects_two_competing_evidence_kinds(tmp_path: Path):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    context = _context(tmp_path)
    submission_id = str(uuid4())
    owner = str(uuid4())
    store.create(submission_id, context)
    _advance_to_running(store, submission_id, owner_instance_id=owner)
    with pytest.raises(MaterialExecutionSubmissionTransitionError):
        store.compare_and_swap(
            submission_id,
            expected_state="running",
            next_state="failed",
            response=_response(context, status="failed"),
            error=MaterialExecutionSubmissionErrorMetadata(
                code="duplicate_evidence", detail="response and error are not additive"
            ),
        )
    assert store.get(submission_id).state == "running"  # type: ignore[union-attr]


def test_replacing_sidecar_after_initialisation_is_rejected(tmp_path: Path):
    database_path = tmp_path / "submissions.sqlite3"
    store = MaterialExecutionSubmissionStore(database_path)
    store.create(str(uuid4()), _context(tmp_path))
    replacement = tmp_path / "replacement.sqlite3"
    replacement.touch()
    os.replace(replacement, database_path)
    with pytest.raises(MaterialExecutionSubmissionPathError):
        store.list_inflight()


def test_restart_fence_marks_blocked_rows_unknown_and_preserves_ticket(tmp_path: Path):
    store = MaterialExecutionSubmissionStore(tmp_path / "submissions.sqlite3")
    context = _context(tmp_path)
    submission_id = str(uuid4())
    owner = str(uuid4())
    store.create(submission_id, context)
    _advance_to_running(store, submission_id, owner_instance_id=owner)
    ticket = _ticket(context)
    store.compare_and_swap(
        submission_id,
        expected_state="running",
        next_state="blocked",
        recovery_ticket=ticket,
    )

    marked = store._mark_inflight_restart_unknown_legacy(owner)

    assert [record.submission_id for record in marked] == [submission_id]
    assert marked[0].state == "unknown"
    assert marked[0].recovery_ticket == ticket
    assert marked[0].error is not None
    assert marked[0].error.code == "restart_unknown"


def test_submission_schema_contains_owner_epoch_and_lease_table(tmp_path: Path):
    database_path = tmp_path / "submissions.sqlite3"
    MaterialExecutionSubmissionStore(database_path)

    with sqlite3.connect(database_path) as connection:
        submission_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(material_execution_submissions)"
            ).fetchall()
        }
        lease_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert "owner_epoch" in submission_columns
    assert "material_execution_submission_owner_leases" in lease_tables


@pytest.mark.parametrize(
    "table_name",
    [
        "material_execution_submissions",
        "material_execution_submission_owner_leases",
    ],
)
def test_existing_unsupported_schema_versions_fail_closed(tmp_path: Path, table_name: str):
    database_path = tmp_path / f"unsupported-{table_name}.sqlite3"
    store = MaterialExecutionSubmissionStore(database_path)
    if table_name == "material_execution_submissions":
        store.create(str(uuid4()), _context(tmp_path))
    with sqlite3.connect(database_path) as connection:
        if table_name == "material_execution_submission_owner_leases":
            # The lease table is empty in this case, so exercise the database
            # level marker rather than relying on a vacuous row-version query.
            connection.execute("PRAGMA user_version = 1")
        else:
            connection.execute(f"UPDATE {table_name} SET schema_version = 1")
        connection.commit()

    with pytest.raises(MaterialExecutionSubmissionSchemaError):
        MaterialExecutionSubmissionStore(database_path)


def test_abandoned_mutex_evidence_takes_precedence_over_clean_handoff(
    tmp_path: Path,
):
    store = MaterialExecutionSubmissionStore(tmp_path / "abandoned.sqlite3")
    first = store._activate_owner_lease(
        owner_instance_id=str(uuid4()),
        mutex_name=_owner_mutex_name(tmp_path / "abandoned.sqlite3"),
        was_abandoned=False,
        process_id=os.getpid(),
        holder_thread_id=1,
    )
    store.end_owner_lease(first.token, capability=first.capability)

    second = store._activate_owner_lease(
        owner_instance_id=str(uuid4()),
        mutex_name=_owner_mutex_name(tmp_path / "abandoned.sqlite3"),
        was_abandoned=True,
        process_id=os.getpid(),
        holder_thread_id=1,
    )
    assert second.acquisition_kind == "abandoned_mutex"


def test_durable_owner_token_alone_cannot_replay_an_epoch_mutation(tmp_path: Path):
    database_path = tmp_path / "capability.sqlite3"
    store = MaterialExecutionSubmissionStore(database_path)
    activation = store._activate_owner_lease(
        owner_instance_id=str(uuid4()),
        mutex_name=_owner_mutex_name(database_path),
        was_abandoned=False,
        process_id=os.getpid(),
        holder_thread_id=1,
    )
    submission_id = str(uuid4())
    context = _context(tmp_path)
    store.create(submission_id, context)
    replay_store = MaterialExecutionSubmissionStore(database_path)
    with pytest.raises(MaterialExecutionSubmissionIdentityConflict, match="capability"):
        replay_store.compare_and_swap(
            submission_id,
            expected_state="accepted",
            next_state="dispatching",
            owner_instance_id=activation.token.owner_instance_id,
            owner_epoch=activation.token.owner_epoch,
        )
    store.compare_and_swap(
        submission_id,
        expected_state="accepted",
        next_state="dispatching",
        owner_instance_id=activation.token.owner_instance_id,
        owner_epoch=activation.token.owner_epoch,
        owner_capability=activation.capability,
    )
    store.compare_and_swap(
        submission_id,
        expected_state="dispatching",
        next_state="failed",
        owner_instance_id=activation.token.owner_instance_id,
        owner_epoch=activation.token.owner_epoch,
        owner_capability=activation.capability,
        error=MaterialExecutionSubmissionErrorMetadata(
            code="test_terminal", detail="capability test"
        ),
    )
    store.end_owner_lease(activation.token, capability=activation.capability)


def test_owner_lease_row_tampering_fails_closed(tmp_path: Path):
    database_path = tmp_path / "tampered-lease.sqlite3"
    store = MaterialExecutionSubmissionStore(database_path)
    activation = store._activate_owner_lease(
        owner_instance_id=str(uuid4()),
        mutex_name=_owner_mutex_name(database_path),
        was_abandoned=False,
        process_id=os.getpid(),
        holder_thread_id=1,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE material_execution_submission_owner_leases "
            "SET acquired_at = 'not-a-timestamp' WHERE owner_epoch = ?",
            (activation.token.owner_epoch,),
        )
        connection.commit()
    with pytest.raises(MaterialExecutionSubmissionSchemaError):
        store.assert_owner_lease(activation.token, capability=activation.capability)


def test_same_named_but_weaker_active_index_fails_closed(tmp_path: Path):
    database_path = tmp_path / "tampered-index.sqlite3"
    MaterialExecutionSubmissionStore(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "DROP INDEX idx_material_execution_submission_owner_active"
        )
        connection.execute(
            "CREATE UNIQUE INDEX idx_material_execution_submission_owner_active "
            "ON material_execution_submission_owner_leases(active) WHERE active = 0"
        )
        connection.commit()
    with pytest.raises(MaterialExecutionSubmissionSchemaError):
        MaterialExecutionSubmissionStore(database_path)
