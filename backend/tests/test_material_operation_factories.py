from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.materials.asset_paths import AutoMaterialPaths
from app.pipeline.material_execution_application import (
    MaterialExecutionCommand,
    MaterialRunnerRetirementRecoveryCommand,
)
from app.pipeline.material_operation_factories import (
    MaterialOperationFactoryError,
    ServerMaterialOperationPlanFactory,
)
from app.pipeline.material_operation_protocol import (
    CoverrSecrets,
    InfoMotionPayload,
    InfoMotionSecrets,
    OfficialCapturePayload,
    OfficialCaptureSecrets,
    PexelsSecrets,
    PixabaySecrets,
    StockCandidateSecrets,
    StockSearchPayload,
)


def _command(operation: str) -> MaterialExecutionCommand:
    return MaterialExecutionCommand(
        pipeline_id=7,
        material_step_id=11,
        material_task_id="material-shot-1",
        execution_id=str(uuid4()),
        operation=operation,  # type: ignore[arg-type]
        startup_timeout_seconds=1.0,
        operation_timeout_seconds=20.0,
    )


def _snapshot(**task_overrides: object) -> dict[str, object]:
    task: dict[str, object] = {
        "material_task_id": "material-shot-1",
        "shot_id": "shot-1",
        "asset_keywords": ["AI", "办公"],
        "recommended_visual": "dashboard office",
        "voiceover": "a useful explanation",
        "official_source_url": "https://example.com/article",
        "official_source_name": "Example News",
        "official_page_title": "Example article",
        "project_name": "factory-demo",
        "output_path": "C:/must-not-cross-boundary.mp4",
    }
    task.update(task_overrides)
    return {"material_tasks": [task]}


@pytest.fixture()
def factory_runtime(tmp_path: Path):
    root = tmp_path / "approved-material-root"
    calls: list[str] = []

    def load_secret(operation: str, paths: AutoMaterialPaths):
        calls.append(operation)
        if operation == "pexels":
            return PexelsSecrets("pexels-test-key")
        if operation == "pixabay":
            return PixabaySecrets("pixabay-test-key")
        if operation == "stock_candidates":
            return StockCandidateSecrets(
                pexels_api_key="pexels-test-key",
                pixabay_api_key="pixabay-test-key",
            )
        if operation == "coverr":
            return CoverrSecrets()
        if operation == "official_capture":
            return OfficialCaptureSecrets()
        if operation == "info_motion":
            return InfoMotionSecrets()
        raise AssertionError(operation)

    return ServerMaterialOperationPlanFactory(
        material_root=root,
        secret_loader=load_secret,
        per_page={
            "pexels": 6,
            "pixabay": 8,
            "coverr": 4,
            "stock_candidates": 5,
        },
    ), calls, root


@pytest.mark.parametrize(
    ("operation", "payload_type", "secret_type"),
    [
        ("official_capture", OfficialCapturePayload, OfficialCaptureSecrets),
        ("pexels", StockSearchPayload, PexelsSecrets),
        ("pixabay", StockSearchPayload, PixabaySecrets),
        ("stock_candidates", StockSearchPayload, StockCandidateSecrets),
        ("info_motion", InfoMotionPayload, InfoMotionSecrets),
    ],
)
def test_factory_builds_exact_typed_plan_from_server_snapshot(
    factory_runtime,
    operation,
    payload_type,
    secret_type,
):
    factory, calls, root = factory_runtime

    plan = factory(_command(operation), _snapshot())

    assert type(plan.request_payload) is payload_type
    assert type(plan.secret_payload) is secret_type
    assert plan.canonical_paths is not None
    assert plan.canonical_paths.root == root.resolve()
    assert plan.project_directory_uses == {}
    assert "output_path" not in repr(plan)
    assert calls == [operation]
    assert not root.exists(), "factory must not create staging directories"


def test_completed_retirement_builds_publication_context_without_provider_secrets(
    factory_runtime,
) -> None:
    factory, calls, root = factory_runtime
    execution_id, attempt_id, control_id, job_instance_id = (
        str(uuid4()) for _ in range(4)
    )
    command = MaterialRunnerRetirementRecoveryCommand(
        pipeline_id=7,
        material_step_id=11,
        material_task_id="material-shot-1",
        execution_id=execution_id,
        attempt_id=attempt_id,
        operation="pexels",
        control_id=control_id,
        job_instance_id=job_instance_id,
        completion_status="succeeded",
        completion_revision=8,
        checkpoint_sha256="a" * 64,
    )

    context = factory.build_publication_recovery_context(command, _snapshot())

    assert context.canonical_paths.root == root.resolve()
    assert context.project_directory_uses == {}
    assert calls == []


def test_factory_prefers_exact_material_task_id_over_colliding_shot_alias(
    factory_runtime,
) -> None:
    factory, calls, _root = factory_runtime
    alias_task = _snapshot(asset_keywords=["城市"])["material_tasks"][0]
    alias_task["material_task_id"] = "other-material-task"
    alias_task["shot_id"] = "material-shot-1"
    exact_task = _snapshot(asset_keywords=["财经"])["material_tasks"][0]
    exact_task["shot_id"] = "shot-exact"

    plan = factory(
        _command("stock_candidates"),
        {"material_tasks": [alias_task, exact_task]},
    )

    assert "financial news office" in plan.request_payload.query
    assert "city office" not in plan.request_payload.query
    assert calls == ["stock_candidates"]


def test_factory_rejects_duplicate_exact_material_task_ids(factory_runtime) -> None:
    factory, calls, _root = factory_runtime
    first = _snapshot()["material_tasks"][0]
    second = dict(first)
    second["shot_id"] = "shot-duplicate"

    with pytest.raises(MaterialOperationFactoryError, match="exactly one"):
        factory(
            _command("stock_candidates"),
            {"material_tasks": [first, second]},
        )

    assert calls == []


def test_info_motion_uses_server_project_name_when_task_snapshot_omits_it(
    factory_runtime,
):
    factory, _calls, _root = factory_runtime
    snapshot = _snapshot()
    task = snapshot["material_tasks"][0]  # type: ignore[index]
    task.pop("project_name", None)  # type: ignore[union-attr]
    snapshot["project_name"] = "server-owned-project"

    plan = factory(_command("info_motion"), snapshot)

    assert isinstance(plan.request_payload, InfoMotionPayload)
    assert plan.request_payload.project_name == "server-owned-project"


def test_info_motion_has_deterministic_legacy_fallback_without_project_name(factory_runtime):
    factory, _calls, _root = factory_runtime
    snapshot = _snapshot()
    task = snapshot["material_tasks"][0]  # type: ignore[index]
    task.pop("project_name", None)  # type: ignore[union-attr]

    plan = factory(_command("info_motion"), snapshot)

    assert isinstance(plan.request_payload, InfoMotionPayload)
    assert plan.request_payload.project_name == "pipeline-7"


def test_stock_query_is_deterministic_bounded_and_ignores_client_paths(factory_runtime):
    factory, _calls, _root = factory_runtime

    plan = factory(
        _command("pexels"),
        _snapshot(
            asset_keywords=["城市", "C:/client-path.mp4"],
            recommended_visual="city office",
            voiceover="",
        ),
    )

    payload = plan.request_payload
    assert isinstance(payload, StockSearchPayload)
    assert payload.per_page == 6
    assert "C:/client-path.mp4" not in payload.query
    assert payload.query == "city office modern office"


def test_stock_candidate_refresh_rejects_locked_target_before_loading_secrets(
    factory_runtime,
):
    factory, calls, _root = factory_runtime
    snapshot = _snapshot()
    snapshot["candidate_pool"] = {
        "shot-1": {
            "shot_id": "shot-1",
            "revision": 2,
            "candidates": [
                {
                    "candidate_id": "pexels-101",
                    "status": "locked",
                }
            ],
        }
    }

    with pytest.raises(MaterialOperationFactoryError, match="locked"):
        factory(_command("stock_candidates"), snapshot)

    assert calls == []


@pytest.mark.parametrize(
    "candidate_pool",
    [
        None,
        {
            "shot_id": "shot-1",
            "revision": 1,
            "candidates": [
                {"candidate_id": "pexels-101", "status": "candidate"}
            ],
        },
        {
            "shot-2": {
                "shot_id": "shot-2",
                "revision": 1,
                "candidates": [
                    {"candidate_id": "pixabay-202", "status": "locked"}
                ],
            }
        },
    ],
)
def test_stock_candidate_refresh_allows_absent_or_unlocked_target_pool(
    factory_runtime,
    candidate_pool,
):
    factory, calls, _root = factory_runtime
    snapshot = _snapshot()
    if candidate_pool is not None:
        snapshot["candidate_pool"] = candidate_pool

    plan = factory(_command("stock_candidates"), snapshot)

    assert type(plan.secret_payload) is StockCandidateSecrets
    assert plan.request_payload.per_page == 5
    assert calls == ["stock_candidates"]


def test_factory_rejects_missing_task_and_client_supplied_identity(factory_runtime):
    factory, _calls, _root = factory_runtime

    with pytest.raises(MaterialOperationFactoryError, match="requested material task"):
        factory(_command("pexels"), {"material_tasks": []})
    with pytest.raises(MaterialOperationFactoryError, match="does not accept"):
        factory(
            _command("pexels"),
            {
                **_snapshot(),
                "database_path": "C:/client.db",
                "output_directory": "C:/client-out",
            },
        )


@pytest.mark.parametrize(
    "field",
    [
        "database_path",
        "output_directory",
        "output_dir",
        "project_directory",
        "project_dir",
        "canonical_root",
        "owner_token",
    ],
)
def test_factory_rejects_task_embedded_path_or_owner_controls(
    factory_runtime,
    field: str,
):
    factory, calls, _root = factory_runtime

    with pytest.raises(MaterialOperationFactoryError, match="task does not accept"):
        factory(_command("pexels"), _snapshot(**{field: "C:/client-controlled"}))

    assert calls == []


def test_factory_attaches_a_defensive_copy_of_server_project_guards(
    factory_runtime,
):
    _factory, _calls, root = factory_runtime
    guard = object()
    returned = {"Pexels素材": guard, "全部素材": guard}
    provider_calls: list[tuple[int, str]] = []

    def project_directory_use_factory(command, task):
        provider_calls.append((command.pipeline_id, str(task["shot_id"])))
        return returned

    factory = ServerMaterialOperationPlanFactory(
        material_root=root,
        secret_loader=_factory.secret_loader,
        project_directory_use_factory=project_directory_use_factory,
    )

    plan = factory(_command("pexels"), _snapshot())
    returned.clear()

    assert dict(plan.project_directory_uses) == {
        "Pexels素材": guard,
        "全部素材": guard,
    }
    with pytest.raises(TypeError):
        plan.project_directory_uses["Pexels素材"] = object()  # type: ignore[index]
    assert provider_calls == [(7, "shot-1")]

    # with_cleanup must preserve the same server-only provider rather than
    # silently returning to an empty project-directory mapping.
    returned.update({"Pexels素材": guard, "全部素材": guard})
    copied = factory.with_cleanup(lambda: None)(_command("pexels"), _snapshot())
    assert dict(copied.project_directory_uses) == {
        "Pexels素材": guard,
        "全部素材": guard,
    }


def test_factory_rejects_unsupported_coverr_until_explicit_opt_in(factory_runtime):
    factory, calls, _root = factory_runtime

    with pytest.raises(MaterialOperationFactoryError, match="Coverr"):
        factory(_command("coverr"), _snapshot())
    assert calls == []

    opted_in = ServerMaterialOperationPlanFactory(
        material_root=factory_runtime[2],
        allow_coverr=True,
        secret_loader=factory_runtime[0].secret_loader,
        per_page={"coverr": 4},
    )
    plan = opted_in(_command("coverr"), _snapshot())
    assert type(plan.request_payload) is StockSearchPayload
    assert type(plan.secret_payload) is CoverrSecrets


@pytest.mark.parametrize("bad_root", [Path("C:/outside"), Path("relative-root")])
def test_factory_rejects_non_d_material_roots(bad_root: Path):
    with pytest.raises((MaterialOperationFactoryError, ValueError)):
        ServerMaterialOperationPlanFactory(material_root=bad_root)


def test_plan_cleanup_is_owner_thread_bound(factory_runtime):
    factory, _calls, _root = factory_runtime
    cleaned: list[str] = []
    factory = factory.with_cleanup(lambda: cleaned.append("released"))

    plan = factory(_command("info_motion"), _snapshot())
    plan.bind_owner_thread()
    plan.release()
    plan.release()
    assert cleaned == ["released"]
