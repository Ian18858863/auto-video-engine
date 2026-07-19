from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from uuid import uuid4

import pytest

from app.pipeline.material_execution_api import (
    MaterialExecutionRequest,
    build_material_execution_command,
    build_material_execution_context,
    map_material_execution_error,
    serialize_material_execution_response,
)
from app.pipeline.material_execution_application import (
    MaterialExecutionLaneBusy,
    MaterialExecutionLaneKey,
    MaterialExecutionLaneUnknown,
    MaterialExecutionOwnerLane,
    MaterialExecutionCommand,
    MaterialExecutionResponse,
)
from app.pipeline.material_execution_claims import MaterialExecutionClaimConflict


def _request(**overrides):
    body = {
        "pipeline_snapshot": {"id": 3, "database_path": "D:/state/pipeline.db"},
        "step_snapshot": {
            "id": 4,
            "pipeline_id": 3,
            "output_summary": {"material_tasks": [{"shot_id": "shot-1"}]},
        },
        "material_task_id": "shot-1",
        "execution_id": str(uuid4()),
        "operation": "pexels",
        "source_chain": ["pexels", "pixabay"],
    }
    body.update(overrides)
    return MaterialExecutionRequest.model_validate(body)


def test_detached_snapshots_build_lane_key_and_command():
    lane_key, command = build_material_execution_context(
        _request(), database_path="D:/state/pipeline.db"
    )

    assert lane_key.pipeline_id == 3
    assert lane_key.material_step_id == 4
    assert lane_key.database_path == Path("D:/state/pipeline.db").resolve()
    assert command.pipeline_id == 3
    assert command.material_step_id == 4
    assert command.material_task_id == "material-shot-1"
    assert command.operation == "pexels"

    with pytest.raises(ValueError, match="server-authoritative"):
        build_material_execution_context(_request())


def test_source_chain_is_explicit_and_task_must_be_in_snapshot():
    schema = MaterialExecutionRequest.model_json_schema()
    assert "source_chain" in schema["required"]
    assert "default" not in schema["properties"]["source_chain"]
    assert schema["properties"]["source_chain"]["maxItems"] == 5
    assert schema["properties"]["execution_id"]["format"] == "uuid"
    assert "pattern" in schema["properties"]["execution_id"]
    pipeline_schema = schema["$defs"]["DetachedPipelineSnapshot"]
    step_schema = schema["$defs"]["DetachedPipelineStepSnapshot"]
    assert len(pipeline_schema["anyOf"]) == 2
    assert "pipeline_id" in step_schema["required"]
    assert len(step_schema["anyOf"]) == 3

    with pytest.raises(ValueError, match="source_chain must begin"):
        _request(source_chain=["pixabay"])
    with pytest.raises(ValueError, match="at most 5"):
        _request(source_chain=["pexels"] * 6)
    with pytest.raises(ValueError, match="must not repeat"):
        _request(source_chain=["pexels", "pexels"])
    request = _request()
    dumped = request.model_dump()
    assert "pipeline_snapshot" in dumped
    assert "step_snapshot" in dumped
    assert "pipeline" not in dumped
    assert "step" not in dumped
    with pytest.raises(ValueError, match="does not contain"):
        build_material_execution_command(
            request.model_copy(
                update={
                    "material_task_id": "missing-task",
                }
            )
        )

    with pytest.raises(ValueError, match="material_tasks"):
        build_material_execution_command(
            _request(step_snapshot={"id": 4, "pipeline_id": 3})
        )


def test_stock_candidate_generation_is_one_atomic_source_not_a_fallback_chain():
    request = _request(
        operation="stock_candidates",
        source_chain=["stock_candidates"],
    )
    assert request.source_chain == ("stock_candidates",)

    for mixed_chain in (
        ["stock_candidates", "pexels"],
        ["pexels", "stock_candidates"],
    ):
        with pytest.raises(ValueError, match="must run alone"):
            _request(
                operation=mixed_chain[0],
                source_chain=mixed_chain,
            )


def test_c_drive_lane_path_is_rejected_before_registry_access():
    with pytest.raises(ValueError, match="D:"):
        build_material_execution_context(_request(), database_path="C:/state.db")


def test_parent_identity_and_database_path_cannot_be_ambiguous():
    with pytest.raises(ValueError, match="pipeline_id"):
        _request(step_snapshot={"id": 4})

    request = _request()
    with pytest.raises(ValueError, match="database_path arguments disagree"):
        build_material_execution_context(request, database_path="D:/configured.db")


def test_response_serializer_and_registry_errors_are_json_safe():
    response = MaterialExecutionResponse(
        pipeline_id=3,
        material_step_id=4,
        material_task_id="shot-1",
        execution_id=str(uuid4()),
        attempt_id=str(uuid4()),
        operation="pexels",
        status="succeeded",
        message="ok",
        executed_task={"shot_id": "shot-1"},
        tasks=({"shot_id": "shot-1"},),
    )
    payload = serialize_material_execution_response(response)
    assert payload["status"] == "succeeded"
    assert payload["tasks"] == [{"shot_id": "shot-1"}]

    with pytest.raises(TypeError, match="MaterialExecutionResponse"):
        serialize_material_execution_response(
            {"status": "blocked", "state": {"native_handle": "secret"}}
        )

    status, error = map_material_execution_error(MaterialExecutionLaneBusy("busy"))
    assert status == 409
    assert error == {
        "code": "material_lane_busy",
        "detail": "busy",
        "retryable": False,
        "recovery_required": True,
    }

    status, error = map_material_execution_error(MaterialExecutionClaimConflict("claim"))
    assert status == 409
    assert error == {
        "code": "material_claim_conflict",
        "detail": "claim",
        "retryable": False,
        "recovery_required": False,
    }

    status, error = map_material_execution_error(FutureTimeoutError("owner wait"))
    assert status == 504
    assert error == {
        "code": "material_execution_timeout",
        "detail": "owner wait",
        "retryable": False,
        "recovery_required": True,
    }


def test_owner_lane_wraps_claim_conflict_as_recovery_required_unknown():
    class _Handler:
        def execute(self, command):
            raise MaterialExecutionClaimConflict("claim is not safely acknowledged")

        def reconcile(self, state, ticket):
            raise AssertionError((state, ticket))

    key = MaterialExecutionLaneKey(
        database_path=Path("D:/api-lane.db"),
        pipeline_id=3,
        material_step_id=4,
    )
    lane = MaterialExecutionOwnerLane(key=key, handler=_Handler())
    command = MaterialExecutionCommand(
        pipeline_id=3,
        material_step_id=4,
        material_task_id="material-shot-1",
        execution_id=str(uuid4()),
        operation="pexels",
        startup_timeout_seconds=1.0,
        operation_timeout_seconds=1.0,
    )
    try:
        with pytest.raises(MaterialExecutionLaneUnknown) as captured:
            lane.submit(command).result(timeout=5)
    finally:
        lane.close(timeout=5)

    status, error = map_material_execution_error(captured.value)
    assert status == 503
    assert error["recovery_required"] is True
    assert error["retryable"] is False
