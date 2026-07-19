"""Explicit, route-free assembly of the new material execution runtime.

This is an application-lifecycle adapter, not a FastAPI route.  It composes
the durable submission sidecar, process-wide owner lease, owner-lane registry,
fresh-read command factory, concrete operation-plan factory and lazy production
handler factory.  The legacy ``run-next`` endpoint is intentionally untouched;
the caller must make the later opt-in route decision explicitly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.engine import Engine

from app.core.config import get_settings, resolve_configured_path
from app.core.database import get_sqlite_database_path
from app.materials.asset_paths import AutoMaterialPaths
from app.pipeline.material_execution_application import (
    MaterialExecutionLaneKey,
    MaterialExecutionOwnerLaneRegistry,
    MaterialExecutionResponse,
    MaterialRunnerRetirementRecoveryCommand,
)
from app.pipeline.material_execution_api_context import (
    MaterialExecutionCommandFactory,
    SqlAlchemyFreshMaterialExecutionSnapshotLoader,
)
from app.pipeline.material_execution_lifecycle import SqlAlchemyMaterialLifecycleAdapter
from app.pipeline.material_execution_submission_coordinator import (
    MaterialExecutionSubmissionCoordinator,
)
from app.pipeline.material_execution_submission_owner_lease import (
    MaterialExecutionSubmissionOwnerLease,
)
from app.pipeline.material_execution_submission_store import (
    MaterialExecutionSubmissionStore,
)
from app.pipeline.material_operation_factories import (
    SecretLoader,
    ServerMaterialOperationPlanFactory,
    SqlAlchemyProjectDirectoryUseFactory,
)
from app.pipeline.material_operation_runner import ProcessMaterialOperationRunner
from app.pipeline.material_restart_recovery import (
    MaterialRestartRecoveryCandidate,
    MaterialRestartRecoveryScanReport,
    SqlAlchemyMaterialRestartRecoveryScanner,
)
from app.pipeline.production_material_execution import (
    ProductionMaterialExecutionHandlerFactory,
    ProductionMaterialOperationPlanFactory,
    ProductionMaterialRecoveryHandler,
)


class MaterialExecutionRuntimeError(RuntimeError):
    """A runtime assembly or teardown failure."""


def _normalise_d_file(value: Path | str, *, label: str) -> Path:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value.strip():
        path = Path(value)
    else:
        raise MaterialExecutionRuntimeError(f"{label} must be a path")
    if not path.is_absolute():
        raise MaterialExecutionRuntimeError(f"{label} must be an absolute D path")
    resolved = path.resolve(strict=False)
    if resolved.drive.casefold() != "d:" or str(resolved).startswith("\\\\"):
        raise MaterialExecutionRuntimeError(f"{label} must remain on D:")
    return resolved


def derive_submission_sidecar_path(database_path: Path | str) -> Path:
    """Derive a deterministic sibling sidecar without touching the primary DB."""

    primary = _normalise_d_file(database_path, label="database_path")
    stem = primary.stem or "database"
    candidate = primary.with_name(f"{stem}.material-submissions.sqlite3")
    if candidate == primary:
        raise MaterialExecutionRuntimeError("submission sidecar must differ from the primary database")
    return candidate


@dataclass(slots=True)
class MaterialExecutionRuntime:
    """Owned references that must live exactly as long as the application."""

    database_path: Path
    submission_store: MaterialExecutionSubmissionStore
    owner_lease: MaterialExecutionSubmissionOwnerLease
    registry: MaterialExecutionOwnerLaneRegistry
    coordinator: MaterialExecutionSubmissionCoordinator
    command_factory: MaterialExecutionCommandFactory
    plan_factory: ServerMaterialOperationPlanFactory | ProductionMaterialOperationPlanFactory
    restart_scanner: SqlAlchemyMaterialRestartRecoveryScanner | Any
    _startup_recovery_report: MaterialRestartRecoveryScanReport | None = None
    _closed: bool = False

    @property
    def store(self) -> MaterialExecutionSubmissionStore:
        """Short alias used by lifecycle adapters."""

        return self.submission_store

    @property
    def closed(self) -> bool:
        return self._closed

    def restart_sweep(self):
        """Return only the proof-backed activation fence evidence."""

        return self.coordinator.restart_sweep()

    @property
    def startup_recovery_report(self) -> MaterialRestartRecoveryScanReport | None:
        return self._startup_recovery_report

    def discover_restart_recoveries(self) -> MaterialRestartRecoveryScanReport:
        """Perform the bounded read-only startup scan exactly when requested."""

        if self._closed:
            raise MaterialExecutionRuntimeError("material execution runtime is closed")
        self.owner_lease.assert_active()
        scan = getattr(self.restart_scanner, "scan", None)
        if not callable(scan):
            raise TypeError("restart_scanner must expose scan")
        report = scan()
        if type(report) is not MaterialRestartRecoveryScanReport:
            raise TypeError("restart scanner returned an invalid report")
        self._startup_recovery_report = report
        return report

    def reconcile_restart_recovery(
        self,
        candidate: MaterialRestartRecoveryCandidate,
        *,
        timeout: float = 30.0,
    ) -> MaterialExecutionResponse:
        """Run one freshly revalidated candidate on its permanent owner lane."""

        if type(candidate) is not MaterialRestartRecoveryCandidate:
            raise TypeError("candidate must be MaterialRestartRecoveryCandidate")
        if type(timeout) not in {int, float} or timeout <= 0 or timeout > 3600:
            raise ValueError("timeout must be positive and no greater than 3600 seconds")
        report = self._startup_recovery_report
        if report is None or candidate not in report.candidates:
            raise MaterialExecutionRuntimeError(
                "restart candidate was not discovered by this runtime startup"
            )
        fresh_report = self.discover_restart_recoveries()
        fresh_matches = tuple(
            item
            for item in fresh_report.candidates
            if item.candidate_id == candidate.candidate_id
        )
        if len(fresh_matches) != 1 or fresh_matches[0] != candidate:
            raise MaterialExecutionRuntimeError(
                "restart candidate changed after discovery; no recovery was attempted"
            )
        lane = self.registry.get_or_create(candidate.lane_key)
        recovery_surface = (
            "recover_runner_retirement"
            if type(candidate.command) is MaterialRunnerRetirementRecoveryCommand
            else "recover_restart_published"
        )
        recover = getattr(lane, recovery_surface, None)
        if not callable(recover):
            raise TypeError(
                f"owner lane does not expose {recovery_surface} recovery"
            )
        future = recover(candidate.command)
        result = future.result(timeout=float(timeout))
        if type(result) is not MaterialExecutionResponse:
            raise TypeError("owner lane returned an invalid restart response")
        self.discover_restart_recoveries()
        return result

    def close(self, *, timeout: float = 5.0) -> None:
        """Close lanes first, then release the process-wide owner lease.

        If a lane retains unknown/recovery ownership, ``coordinator.close``
        raises and the lease remains held.  That fail-closed behavior is
        intentional: a shutdown caller must not silently abandon live work.
        """

        if self._closed:
            return
        try:
            self.coordinator.close(timeout=timeout)
        except BaseException as exc:
            raise MaterialExecutionRuntimeError(
                "material execution runtime could not close cleanly"
            ) from exc
        self._closed = True

    shutdown = close

    def __enter__(self) -> "MaterialExecutionRuntime":
        if self._closed:
            raise MaterialExecutionRuntimeError("material execution runtime is closed")
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


@asynccontextmanager
async def material_execution_runtime_lifespan(
    runtime_factory: Callable[[], MaterialExecutionRuntime],
) -> AsyncIterator[MaterialExecutionRuntime]:
    """Own one runtime for an application lifespan without registering a route.

    This adapter is intentionally separate from ``main.py``.  It gives the
    application integration a small, testable seam: startup must finish
    assembling a concrete runtime before the application is considered ready;
    shutdown always delegates to the runtime's fail-closed lane-then-lease
    close path.  The factory itself owns partial-startup unwind.
    """

    if not callable(runtime_factory):
        raise TypeError("runtime_factory must be callable")
    runtime = runtime_factory()
    if type(runtime) is not MaterialExecutionRuntime:
        raise TypeError("runtime_factory must return MaterialExecutionRuntime")
    try:
        runtime.discover_restart_recoveries()
        yield runtime
    finally:
        runtime.close()


def build_material_execution_runtime(
    *,
    engine: Engine,
    session_factory: Callable[[], Any],
    material_root: Path | str,
    project_root: Path | str | None = None,
    sidecar_path: Path | str | None = None,
    operation_plan_factory: ProductionMaterialOperationPlanFactory | None = None,
    secret_loader: SecretLoader | None = None,
    per_page: dict[str, int] | None = None,
    allow_coverr: bool = False,
    runner_factory: Callable[[MaterialExecutionLaneKey], Any] | None = None,
    lifecycle_factory: Callable[[MaterialExecutionLaneKey, Any], Any] | None = None,
    recovery_handler: ProductionMaterialRecoveryHandler | None = None,
    publication_recovery_context_factory: Callable[..., Any] | None = None,
    owner_instance_id: str | None = None,
    store_factory: Callable[[Path], MaterialExecutionSubmissionStore] = MaterialExecutionSubmissionStore,
    owner_lease_factory: Callable[..., MaterialExecutionSubmissionOwnerLease] = MaterialExecutionSubmissionOwnerLease.acquire,
    registry_factory: Callable[..., MaterialExecutionOwnerLaneRegistry] = MaterialExecutionOwnerLaneRegistry,
    coordinator_factory: Callable[..., MaterialExecutionSubmissionCoordinator] = MaterialExecutionSubmissionCoordinator,
    restart_scanner_factory: Callable[..., Any] = SqlAlchemyMaterialRestartRecoveryScanner,
) -> MaterialExecutionRuntime:
    """Build the runtime in a strict acquisition order with unwind on failure."""

    if not isinstance(engine, Engine):
        raise TypeError("engine must be a SQLAlchemy Engine")
    if not callable(session_factory):
        raise TypeError("session_factory must be callable")
    if not callable(store_factory):
        raise TypeError("store_factory must be callable")
    if not callable(owner_lease_factory):
        raise TypeError("owner_lease_factory must be callable")
    if not callable(registry_factory):
        raise TypeError("registry_factory must be callable")
    if not callable(coordinator_factory):
        raise TypeError("coordinator_factory must be callable")
    if not callable(restart_scanner_factory):
        raise TypeError("restart_scanner_factory must be callable")

    database_path = _normalise_d_file(
        get_sqlite_database_path(engine),
        label="engine database_path",
    )
    if sidecar_path is None:
        resolved_sidecar = derive_submission_sidecar_path(database_path)
    else:
        resolved_sidecar = _normalise_d_file(sidecar_path, label="sidecar_path")
        if resolved_sidecar == database_path:
            raise MaterialExecutionRuntimeError(
                "submission sidecar must differ from the primary database"
            )
    approved_material_root = _normalise_d_file(
        material_root,
        label="material_root",
    )
    # Constructing the concrete factory validates the material root before a
    # mutex is acquired.  It also guarantees no directory is created here.
    concrete_plan_factory = operation_plan_factory
    if concrete_plan_factory is None:
        approved_project_root = resolve_configured_path(
            project_root
            if project_root is not None
            else get_settings().project_root_dir
        )
        project_directory_use_factory = SqlAlchemyProjectDirectoryUseFactory(
            session_factory=session_factory,
            expected_database_path=database_path,
            project_root=approved_project_root,
        )
        concrete_plan_factory = ServerMaterialOperationPlanFactory(
            material_root=approved_material_root,
            secret_loader=secret_loader,
            per_page=per_page,
            allow_coverr=allow_coverr,
            project_directory_use_factory=project_directory_use_factory,
        )
    elif not callable(concrete_plan_factory):
        raise TypeError("operation_plan_factory must be callable")

    concrete_publication_recovery_context_factory = (
        publication_recovery_context_factory
    )
    if concrete_publication_recovery_context_factory is None:
        concrete_publication_recovery_context_factory = getattr(
            concrete_plan_factory,
            "build_publication_recovery_context",
            None,
        )
    if (
        concrete_publication_recovery_context_factory is not None
        and not callable(concrete_publication_recovery_context_factory)
    ):
        raise TypeError("publication_recovery_context_factory must be callable")

    if runner_factory is None:
        # The path is captured, but the runner itself is created only by a
        # lane's owner thread on its first execution.
        approved_root = approved_material_root

        def _runner_factory(_key: MaterialExecutionLaneKey) -> Any:
            return ProcessMaterialOperationRunner(approved_root)

        concrete_runner_factory = _runner_factory
    else:
        if not callable(runner_factory):
            raise TypeError("runner_factory must be callable")
        concrete_runner_factory = runner_factory

    concrete_lifecycle_factory = lifecycle_factory
    if concrete_lifecycle_factory is None:
        concrete_lifecycle_factory = lambda _key, _store: SqlAlchemyMaterialLifecycleAdapter()
    elif not callable(concrete_lifecycle_factory):
        raise TypeError("lifecycle_factory must be callable")

    store: MaterialExecutionSubmissionStore | None = None
    lease: MaterialExecutionSubmissionOwnerLease | None = None
    coordinator: MaterialExecutionSubmissionCoordinator | None = None
    try:
        store = store_factory(resolved_sidecar)
        if not isinstance(store, MaterialExecutionSubmissionStore):
            raise TypeError("store_factory must return MaterialExecutionSubmissionStore")
        lease_kwargs: dict[str, Any] = {}
        if owner_instance_id is not None:
            lease_kwargs["owner_instance_id"] = owner_instance_id
        lease = owner_lease_factory(store, **lease_kwargs)
        if type(lease) is not MaterialExecutionSubmissionOwnerLease:
            raise TypeError("owner_lease_factory must return MaterialExecutionSubmissionOwnerLease")

        handler_factory = ProductionMaterialExecutionHandlerFactory(
            session_factory=session_factory,
            runner_factory=concrete_runner_factory,
            operation_plan_factory=concrete_plan_factory,
            lifecycle_factory=concrete_lifecycle_factory,
            recovery_handler=recovery_handler,
            publication_recovery_context_factory=(
                concrete_publication_recovery_context_factory
            ),
        )
        registry = registry_factory(handler_factory=handler_factory)
        loader = SqlAlchemyFreshMaterialExecutionSnapshotLoader(
            session_factory=session_factory,
        )
        command_factory = MaterialExecutionCommandFactory(
            engine=engine,
            snapshot_loader=loader,
        )
        restart_scanner = restart_scanner_factory(
            session_factory=session_factory,
            database_path=database_path,
        )
        if not callable(getattr(restart_scanner, "scan", None)):
            raise TypeError("restart_scanner_factory must return a scanner")
        coordinator = coordinator_factory(
            store=store,
            registry=registry,
            owner_lease=lease,
        )
        return MaterialExecutionRuntime(
            database_path=database_path,
            submission_store=store,
            owner_lease=lease,
            registry=registry,
            coordinator=coordinator,
            command_factory=command_factory,
            plan_factory=concrete_plan_factory,
            restart_scanner=restart_scanner,
        )
    except BaseException:
        # If coordinator construction bound the lease, its close path owns the
        # registry/lease ordering.  Otherwise release the lease directly.  No
        # destructor/GC fallback is relied upon.
        if coordinator is not None:
            try:
                coordinator.close()
            except BaseException:
                pass
        elif lease is not None:
            try:
                lease.close()
            except BaseException:
                pass
        raise


__all__ = [
    "MaterialExecutionRuntime",
    "MaterialExecutionRuntimeError",
    "build_material_execution_runtime",
    "derive_submission_sidecar_path",
    "material_execution_runtime_lifespan",
]
