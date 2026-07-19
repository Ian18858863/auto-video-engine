"""Owner-lane production seam for one contained material attempt.

This module is deliberately an application adapter, not an endpoint.  It
accepts only the handle-free command DTO from ``material_execution_application``
and constructs every database/runner object lazily from inside the lane
thread.  A single call executes one operation attempt; fallback selection,
next-task activation, and restart recovery remain explicit callers' jobs.

The important ordering is kept in one place::

    acquire mutex -> fresh detached DB read -> operation plan ->
    prepare_and_claim_with_owned_mutex -> runner/coordinator ->
    publisher (claim-bound CAS) -> completion -> retirement -> release

The request thread never receives a SQLAlchemy session, ORM object, mutex,
lease, directory pin, or publication object.  Unknown edges return an opaque
owner-lane ticket while the raw state stays reachable by the same lane.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from app.materials.asset_paths import AutoMaterialPaths
from app.materials.candidate_media_validation import validate_candidate_media
from app.core.windows_named_mutex import WindowsMutexBusy
from app.pipeline.material_artifact_publisher import (
    CanonicalMaterialArtifactPublisher,
    CanonicalMaterialPublication,
)
from app.pipeline.material_execution_application import (
    MaterialExecutionCommand,
    MaterialExecutionLaneBusy,
    MaterialExecutionLaneKey,
    MaterialExecutionLaneUnknown,
    MaterialExecutionResponse,
    MaterialExecutionRetainedOwnership,
    MaterialRestartRecoveryCommand,
    MaterialRunnerRetirementRecoveryCommand,
    MaterialRecoveryTicket,
)
from app.pipeline.material_execution_claims import (
    MaterialExecutionClaimError,
    MaterialExecutionClaimKey,
    SqlAlchemyMaterialExecutionClaimStore,
)
from app.pipeline.material_execution_coordinator import (
    MaterialCompletionReceipt,
    MaterialExecutionCoordinator,
    MaterialExecutionCoordinatorUnknown,
    MaterialCoordinatorResult,
    MaterialLifecycleAdapter,
    PreparedMaterialClaim,
)
from app.pipeline.material_execution_lifecycle import (
    ClaimLeasePublicationDatabase,
    MaterialExecutionClaimPreparer,
    MaterialPrepareAndClaimUnknown,
    MaterialPreparedClaimRecovery,
    MaterialRestartRecoveryCoordinator,
    MaterialRestartRecoveryUnknown,
    SqlAlchemyMaterialLifecycleAdapter,
)
from app.pipeline.material_operation_protocol import (
    MaterialOperationBlocked,
    MaterialOperationKind,
    MaterialOperationOutcome,
    MaterialOperationRequest,
    MaterialOperationSecrets,
    MaterialRequestPayload,
    MaterialSecretPayload,
    PROTOCOL_VERSION,
    STOCK_CANDIDATES_PROTOCOL_VERSION,
)
from app.pipeline.material_operation_runner import (
    PreparedMaterialAttempt,
)
from app.pipeline.material_runner_retirement import (
    pending_runner_retirement_from_payload,
)
from app.pipeline.project_identity import PreparedProjectChildDirectoryUse


def _protocol_version_for_operation(operation: MaterialOperationKind) -> int:
    return (
        STOCK_CANDIDATES_PROTOCOL_VERSION
        if operation == "stock_candidates"
        else PROTOCOL_VERSION
    )


class ProductionMaterialExecutionError(RuntimeError):
    """Base class for the explicit single-attempt application seam."""


@dataclass(slots=True)
class ProductionMaterialOperationPlan:
    """Handle-free operation inputs prepared from one fresh DB snapshot.

    ``cleanup`` is an owner-thread callback for project-directory guards or
    other plan resources.  It is never serialized into a response and is only
    invoked after a clean terminal coordinator result (or by an explicit
    recovery resolver).
    """

    request_payload: MaterialRequestPayload
    secret_payload: MaterialSecretPayload = field(repr=False)
    canonical_paths: AutoMaterialPaths
    project_directory_uses: Mapping[str, Any] = field(default_factory=dict, repr=False)
    cleanup: Callable[[], None] | None = field(default=None, repr=False, compare=False)
    _released: bool = field(default=False, init=False, repr=False, compare=False)
    _owner_thread_ident: int | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if type(self.canonical_paths) is not AutoMaterialPaths:
            raise TypeError("canonical_paths must be AutoMaterialPaths")
        if not isinstance(self.project_directory_uses, Mapping):
            raise TypeError("project_directory_uses must be a mapping")
        copied_uses: dict[str, Any] = {}
        for key, value in self.project_directory_uses.items():
            if not isinstance(key, str) or not key.strip():
                raise TypeError("project_directory_uses keys must be non-empty strings")
            copied_uses[key] = value
        self.project_directory_uses = MappingProxyType(copied_uses)
        if self.cleanup is not None and not callable(self.cleanup):
            raise TypeError("cleanup must be callable")
        root = self.canonical_paths.root
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("canonical material root must be an absolute D path")
        if root.resolve(strict=False).drive.casefold() != "d:":
            raise ValueError("canonical material root must remain on D:")

    @property
    def released(self) -> bool:
        return self._released

    def bind_owner_thread(self, thread_ident: int | None = None) -> None:
        """Bind plan cleanup to the lane thread that created the plan."""

        current = threading.get_ident() if thread_ident is None else thread_ident
        if self._owner_thread_ident is None:
            self._owner_thread_ident = current
        elif self._owner_thread_ident != current:
            raise ProductionMaterialExecutionError(
                "material operation plan belongs to another owner thread"
            )

    def release(self) -> None:
        """Release plan-owned guards exactly once on the owner lane."""

        if self._released:
            return
        if self._owner_thread_ident is not None and (
            self._owner_thread_ident != threading.get_ident()
        ):
            raise ProductionMaterialExecutionError(
                "material plan cleanup must run on its owner lane"
            )
        callback = self.cleanup
        if callback is not None:
            callback()
        self._released = True


class ProductionMaterialOperationPlanFactory(Protocol):
    """Build a plan from a detached, fresh ``output_summary`` mapping."""

    def __call__(
        self,
        command: MaterialExecutionCommand,
        fresh_payload: Mapping[str, Any],
    ) -> ProductionMaterialOperationPlan:
        ...


@dataclass(frozen=True, slots=True)
class ProductionMaterialPublicationRecoveryContext:
    """Server-owned paths needed to re-attest an existing publication only."""

    canonical_paths: AutoMaterialPaths
    project_directory_uses: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if type(self.canonical_paths) is not AutoMaterialPaths:
            raise TypeError("canonical_paths must be AutoMaterialPaths")
        root = self.canonical_paths.root
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("canonical material root must be an absolute D path")
        if root.resolve(strict=False).drive.casefold() != "d:":
            raise ValueError("canonical material root must remain on D:")
        if not isinstance(self.project_directory_uses, Mapping):
            raise TypeError("project_directory_uses must be a mapping")
        copied: dict[str, Any] = {}
        for key, value in self.project_directory_uses.items():
            if type(key) is not str or not key.strip():
                raise TypeError("project_directory_uses keys must be non-empty strings")
            copied[key] = value
        object.__setattr__(self, "project_directory_uses", MappingProxyType(copied))


class ProductionMaterialPublicationRecoveryContextFactory(Protocol):
    def __call__(
        self,
        command: MaterialRestartRecoveryCommand | MaterialRunnerRetirementRecoveryCommand,
        fresh_payload: Mapping[str, Any],
    ) -> ProductionMaterialPublicationRecoveryContext:
        ...


class ProductionMaterialPublisherFactory(Protocol):
    """Create a publisher from the claim-bound publication database seam."""

    def __call__(
        self,
        *,
        canonical_paths: AutoMaterialPaths,
        database: ClaimLeasePublicationDatabase,
        expected_payload: Mapping[str, Any],
    ) -> Any:
        ...


class ProductionMaterialRecoveryHandler(Protocol):
    """Explicit recovery callback; no automatic retry is guessed here."""

    def __call__(
        self,
        state: "ProductionMaterialRecoveryState",
        ticket: MaterialRecoveryTicket,
    ) -> MaterialExecutionResponse | MaterialExecutionRetainedOwnership:
        ...


@dataclass(slots=True)
class _ProductionRuntime:
    store: Any
    runner: Any
    lifecycle: MaterialLifecycleAdapter
    preparer: MaterialExecutionClaimPreparer


@dataclass(slots=True)
class ProductionMaterialRecoveryState:
    """Lane-local ownership retained after an unknown/blocked edge.

    This object is intentionally never placed in ``MaterialExecutionResponse``
    or a recovery ticket.  It is reachable only from the owner lane and an
    explicitly injected recovery callback.
    """

    handler: "ProductionMaterialExecutionHandler" = field(repr=False)
    runtime: _ProductionRuntime = field(repr=False)
    plan: ProductionMaterialOperationPlan = field(repr=False)
    operation: MaterialOperationKind
    material_task_id: str
    prepared: PreparedMaterialAttempt
    mutex: Any = field(repr=False)
    lease: Any | None = field(default=None, repr=False)
    claim: PreparedMaterialClaim | None = field(default=None, repr=False)
    request: MaterialOperationRequest | None = field(default=None, repr=False)
    secrets: MaterialOperationSecrets | None = field(default=None, repr=False)
    publication: CanonicalMaterialPublication | None = field(default=None, repr=False)
    snapshot: Mapping[str, Any] = field(default_factory=dict, repr=False)
    reason: str = field(default="", repr=False)

    def __repr__(self) -> str:  # pragma: no cover - defensive redaction
        return (
            "ProductionMaterialRecoveryState("
            f"execution_id={self.prepared.execution_id!r}, "
            f"attempt_id={self.prepared.attempt_id!r}, "
            "ownership=<lane-local>)"
        )


@dataclass(slots=True)
class ProductionMaterialRestartRecoveryState:
    """Raw restart ownership retained after an unknowable recovery edge."""

    handler: "ProductionMaterialExecutionHandler" = field(repr=False)
    command: MaterialRestartRecoveryCommand | MaterialRunnerRetirementRecoveryCommand
    mutex: Any = field(repr=False)
    error: BaseException = field(repr=False)

    def __repr__(self) -> str:  # pragma: no cover - defensive redaction
        return (
            "ProductionMaterialRestartRecoveryState("
            f"execution_id={self.command.execution_id!r}, "
            f"attempt_id={self.command.attempt_id!r}, "
            "ownership=<lane-local>)"
        )


class ProductionMaterialExecutionHandler:
    """Execute exactly one operation while retaining all raw ownership locally."""

    def __init__(
        self,
        *,
        key: MaterialExecutionLaneKey,
        claim_store_factory: Callable[[MaterialExecutionClaimKey], Any],
        runner_factory: Callable[[MaterialExecutionLaneKey], Any],
        operation_plan_factory: ProductionMaterialOperationPlanFactory,
        lifecycle_factory: Callable[[MaterialExecutionLaneKey, Any], MaterialLifecycleAdapter],
        publisher_factory: ProductionMaterialPublisherFactory,
        recovery_handler: ProductionMaterialRecoveryHandler | None,
        publication_recovery_context_factory: (
            ProductionMaterialPublicationRecoveryContextFactory | None
        ) = None,
        restart_recovery_coordinator_factory: Callable[..., Any] = (
            MaterialRestartRecoveryCoordinator
        ),
    ) -> None:
        if type(key) is not MaterialExecutionLaneKey:
            raise TypeError("key must be MaterialExecutionLaneKey")
        for value, label in (
            (claim_store_factory, "claim_store_factory"),
            (runner_factory, "runner_factory"),
            (operation_plan_factory, "operation_plan_factory"),
            (lifecycle_factory, "lifecycle_factory"),
            (publisher_factory, "publisher_factory"),
            (restart_recovery_coordinator_factory, "restart_recovery_coordinator_factory"),
        ):
            if not callable(value):
                raise TypeError(f"{label} must be callable")
        if recovery_handler is not None and not callable(recovery_handler):
            raise TypeError("recovery_handler must be callable")
        if publication_recovery_context_factory is not None and not callable(
            publication_recovery_context_factory
        ):
            raise TypeError("publication_recovery_context_factory must be callable")
        self.key = key
        self._claim_store_factory = claim_store_factory
        self._runner_factory = runner_factory
        self._operation_plan_factory = operation_plan_factory
        self._lifecycle_factory = lifecycle_factory
        self._publisher_factory = publisher_factory
        self._recovery_handler = recovery_handler
        self._publication_recovery_context_factory = publication_recovery_context_factory
        self._restart_recovery_coordinator_factory = restart_recovery_coordinator_factory
        self._runtime: _ProductionRuntime | None = None
        self._retained: ProductionMaterialRecoveryState | None = None
        self._restart_retained: ProductionMaterialRestartRecoveryState | None = None
        self._owner_thread_ident: int | None = None
        self._closed = False

    @property
    def owner_thread_ident(self) -> int | None:
        return self._owner_thread_ident

    def execute(self, command: MaterialExecutionCommand):
        """Run one claim/runner/publisher attempt on the lane thread."""

        self._assert_lane_thread()
        if type(command) is not MaterialExecutionCommand:
            raise TypeError("command must be MaterialExecutionCommand")
        if (
            command.pipeline_id != self.key.pipeline_id
            or command.material_step_id != self.key.material_step_id
        ):
            raise ValueError("command belongs to another material step")
        if self._retained is not None or self._restart_retained is not None:
            raise MaterialExecutionLaneBusy(
                "production material handler still retains recovery ownership"
            )
        runtime = self._ensure_runtime()

        mutex = runtime.store.acquire_claim_mutex(timeout_ms=0)
        plan: ProductionMaterialOperationPlan | None = None
        fresh_payload: dict[str, Any] | None = None
        try:
            # The snapshot is detached before any request/secrets object is
            # built.  No ORM identity map crosses this boundary.
            fresh_payload = _clone_json_mapping(runtime.store.fresh_read())
            _assert_task_snapshot(fresh_payload, command.material_task_id)
            plan = self._operation_plan_factory(command, fresh_payload)
            plan.bind_owner_thread()
            self._validate_plan_for_operation(command, plan)
        except BaseException:
            _release_preclaim_resources(plan, mutex)
            raise

        try:
            claim = runtime.preparer.prepare_and_claim_with_owned_mutex(
                mutex=mutex,
                material_task_id=command.material_task_id,
                execution_id=command.execution_id,
                operation=command.operation,
            )
        except MaterialPrepareAndClaimUnknown as exc:
            recovery = exc.recovery
            state = ProductionMaterialRecoveryState(
                handler=self,
                runtime=runtime,
                plan=plan,
                operation=command.operation,
                material_task_id=command.material_task_id,
                prepared=recovery.prepared,
                mutex=recovery.mutex,
                lease=recovery.lease,
                snapshot=fresh_payload or {},
                reason="material claim acknowledgement is unknown",
            )
            self._retained = state
            response = self._response_from_snapshot(
                command,
                recovery.prepared.attempt_id,
                fresh_payload or {},
                status="blocked",
                message="material claim acknowledgement is unknown; explicit recovery is required",
            )
            return MaterialExecutionRetainedOwnership(response=response, state=state)
        except MaterialExecutionClaimError:
            # The preparer retires a known-unclaimed attempt and releases the
            # same mutex.  Plan-owned project guards are still ours to close.
            plan.release()
            raise

        # The claim now owns the same mutex.  Build request/secrets only after
        # the attempt identity is known, then cross the coordinator boundary.
        try:
            request = MaterialOperationRequest(
                protocol_version=_protocol_version_for_operation(command.operation),
                execution_id=claim.binding.execution_id,
                attempt_id=claim.binding.attempt_id,
                operation=command.operation,
                payload=plan.request_payload,
            )
            secrets = MaterialOperationSecrets(
                version=_protocol_version_for_operation(command.operation),
                execution_id=claim.binding.execution_id,
                attempt_id=claim.binding.attempt_id,
                operation=command.operation,
                payload=plan.secret_payload,
            )
        except BaseException:
            # A malformed/mutated plan after claim is still an ownership
            # edge.  Keep the lease and prepared roots on this lane.
            state = ProductionMaterialRecoveryState(
                handler=self,
                runtime=runtime,
                plan=plan,
                operation=command.operation,
                material_task_id=command.material_task_id,
                prepared=claim.prepared,
                mutex=getattr(claim.lease, "mutex", mutex),
                lease=claim.lease,
                claim=claim,
                snapshot=fresh_payload or {},
                reason="material operation inputs could not be bound",
            )
            self._retained = state
            response = self._response_from_snapshot(
                command,
                claim.binding.attempt_id,
                fresh_payload or {},
                status="blocked",
                message=(
                    "material operation inputs could not be bound; explicit recovery is required"
                ),
            )
            return MaterialExecutionRetainedOwnership(response=response, state=state)
        state = ProductionMaterialRecoveryState(
            handler=self,
            runtime=runtime,
            plan=plan,
            operation=command.operation,
            material_task_id=command.material_task_id,
            prepared=claim.prepared,
            mutex=getattr(claim.lease, "mutex", mutex),
            lease=claim.lease,
            claim=claim,
            request=request,
            secrets=secrets,
            snapshot=fresh_payload or {},
        )

        def publication_factory(
            factory_claim: PreparedMaterialClaim,
            factory_request: MaterialOperationRequest,
            outcome: MaterialOperationOutcome,
        ) -> CanonicalMaterialPublication:
            publication = self._build_publication(
                plan,
                factory_claim,
                factory_request,
                outcome,
            )
            state.publication = publication
            return publication

        try:
            coordinator = MaterialExecutionCoordinator(
                runner=runtime.runner,
                lifecycle=runtime.lifecycle,
                publication_factory=publication_factory,
            )
            result = coordinator.run_once(
                claim,
                request,
                secrets,
                startup_timeout_seconds=command.startup_timeout_seconds,
                operation_timeout_seconds=command.operation_timeout_seconds,
            )
        except MaterialOperationBlocked:
            state.reason = "material operation is blocked; explicit recovery is required"
            state.snapshot = self._safe_fresh_read(runtime.store, fresh_payload or {})
            self._retained = state
            response = self._response_from_snapshot(
                command,
                claim.binding.attempt_id,
                state.snapshot,
                status="blocked",
                message="material operation is blocked; explicit recovery is required",
            )
            return MaterialExecutionRetainedOwnership(response=response, state=state)
        except BaseException:
            # A coordinator unknown edge may have committed a reference or
            # retained runner/publisher ownership.  Never release here.
            state.reason = "material execution acknowledgement is unknown"
            state.snapshot = self._safe_fresh_read(runtime.store, fresh_payload or {})
            self._retained = state
            response = self._response_from_snapshot(
                command,
                claim.binding.attempt_id,
                state.snapshot,
                status="blocked",
                message=(
                    "material execution acknowledgement is unknown; explicit recovery is required"
                ),
            )
            return MaterialExecutionRetainedOwnership(response=response, state=state)

        final_snapshot = self._safe_fresh_read(runtime.store, fresh_payload or {})
        response = self._response_from_snapshot(
            command,
            claim.binding.attempt_id,
            final_snapshot,
            status=result.status,
            message=_result_message(result),
            completion=result.completion,
        )
        try:
            plan.release()
        except BaseException:
            # The DB/runner terminal receipt is already durable, but a plan
            # guard could not be acknowledged.  Keep it lane-local for an
            # explicit resolver rather than pretending the lane is clean.
            state.reason = "plan resource release acknowledgement is unknown"
            state.snapshot = final_snapshot
            self._retained = state
            blocked = self._response_from_snapshot(
                command,
                claim.binding.attempt_id,
                final_snapshot,
                status="blocked",
                message="material plan cleanup is unknown; explicit recovery is required",
            )
            return MaterialExecutionRetainedOwnership(response=blocked, state=state)
        return response

    def execute_source_chain(
        self,
        command: MaterialExecutionCommand,
        *,
        source_chain: Sequence[MaterialOperationKind],
    ):
        """Execute an explicit same-execution source chain on the owner lane.

        ``execute`` remains the compatibility single-attempt adapter.  This
        opt-in seam is the only production-side caller that may rotate from a
        failed/terminated source: the coordinator records terminal history,
        obtains the runner's post-retirement receipt, then prepares and binds
        the next source while the original step mutex is still held.
        """

        self._assert_lane_thread()
        if type(command) is not MaterialExecutionCommand:
            raise TypeError("command must be MaterialExecutionCommand")
        if (
            command.pipeline_id != self.key.pipeline_id
            or command.material_step_id != self.key.material_step_id
        ):
            raise ValueError("command belongs to another material step")
        if type(source_chain) not in (tuple, list) or not source_chain:
            raise ValueError("source_chain must be a non-empty tuple or list")
        chain = tuple(source_chain)
        if chain[0] != command.operation:
            raise ValueError("source_chain must begin with command.operation")
        allowed = {
            "official_capture",
            "pexels",
            "pixabay",
            "coverr",
            "info_motion",
            "stock_candidates",
        }
        if any(operation not in allowed for operation in chain):
            raise ValueError("source_chain contains an unsupported operation")
        if "stock_candidates" in chain and chain != ("stock_candidates",):
            raise ValueError(
                "stock_candidates must run alone and cannot be a fallback source"
            )
        if self._retained is not None or self._restart_retained is not None:
            raise MaterialExecutionLaneBusy(
                "production material handler still retains recovery ownership"
            )
        runtime = self._ensure_runtime()

        mutex = runtime.store.acquire_claim_mutex(timeout_ms=0)
        initial_plan: ProductionMaterialOperationPlan | None = None
        fresh_payload: dict[str, Any] | None = None
        try:
            fresh_payload = _clone_json_mapping(runtime.store.fresh_read())
            _assert_task_snapshot(fresh_payload, command.material_task_id)
            initial_plan = self._operation_plan_factory(command, fresh_payload)
            initial_plan.bind_owner_thread()
            self._validate_plan_for_operation(command, initial_plan)
        except BaseException:
            _release_preclaim_resources(initial_plan, mutex)
            raise

        try:
            claim = runtime.preparer.prepare_and_claim_with_owned_mutex(
                mutex=mutex,
                material_task_id=command.material_task_id,
                execution_id=command.execution_id,
                operation=command.operation,
            )
        except MaterialPrepareAndClaimUnknown as exc:
            state = ProductionMaterialRecoveryState(
                handler=self,
                runtime=runtime,
                plan=initial_plan,
                operation=command.operation,
                material_task_id=command.material_task_id,
                prepared=exc.recovery.prepared,
                mutex=exc.recovery.mutex,
                lease=exc.recovery.lease,
                snapshot=fresh_payload or {},
                reason="material claim acknowledgement is unknown",
            )
            self._retained = state
            response = self._response_from_snapshot(
                command,
                exc.recovery.prepared.attempt_id,
                fresh_payload or {},
                status="blocked",
                message="material claim acknowledgement is unknown; explicit recovery is required",
            )
            return MaterialExecutionRetainedOwnership(response=response, state=state)
        except MaterialExecutionClaimError:
            initial_plan.release()
            raise

        plan_holder: dict[str, ProductionMaterialOperationPlan] = {
            "plan": initial_plan,
        }
        claim_holder: dict[str, Any] = {
            "claim": claim,
            "operation": command.operation,
            "request": None,
            "secrets": None,
        }
        next_inputs: dict[int, tuple[MaterialOperationRequest, MaterialOperationSecrets]] = {}
        state = ProductionMaterialRecoveryState(
            handler=self,
            runtime=runtime,
            plan=initial_plan,
            operation=command.operation,
            material_task_id=command.material_task_id,
            prepared=claim.prepared,
            mutex=getattr(claim.lease, "mutex", mutex),
            lease=claim.lease,
            claim=claim,
            snapshot=fresh_payload or {},
        )

        try:
            request = MaterialOperationRequest(
                protocol_version=_protocol_version_for_operation(command.operation),
                execution_id=claim.binding.execution_id,
                attempt_id=claim.binding.attempt_id,
                operation=command.operation,
                payload=initial_plan.request_payload,
            )
            secrets = MaterialOperationSecrets(
                version=_protocol_version_for_operation(command.operation),
                execution_id=claim.binding.execution_id,
                attempt_id=claim.binding.attempt_id,
                operation=command.operation,
                payload=initial_plan.secret_payload,
            )
            claim_holder["request"] = request
            claim_holder["secrets"] = secrets
        except BaseException:
            state.reason = "material operation inputs could not be bound"
            self._retained = state
            response = self._response_from_snapshot(
                command,
                claim.binding.attempt_id,
                fresh_payload or {},
                status="blocked",
                message=(
                    "material operation inputs could not be bound; explicit recovery is required"
                ),
            )
            return MaterialExecutionRetainedOwnership(response=response, state=state)

        def publication_factory(
            factory_claim: PreparedMaterialClaim,
            factory_request: MaterialOperationRequest,
            outcome: MaterialOperationOutcome,
        ) -> CanonicalMaterialPublication:
            publication = self._build_publication(
                plan_holder["plan"],
                factory_claim,
                factory_request,
                outcome,
            )
            state.publication = publication
            return publication

        def prepare_next_attempt(
            operation: MaterialOperationKind,
            execution_id: str,
        ) -> PreparedMaterialAttempt:
            # The previous attempt has already been DB-cleared and runner-
            # retired when this callback is entered.  Release only this
            # attempt's plan guards before creating the next source plan.
            plan_holder["plan"].release()
            next_command = replace(command, operation=operation)
            snapshot = _clone_json_mapping(runtime.store.fresh_read())
            _assert_task_snapshot(snapshot, command.material_task_id)
            next_plan = self._operation_plan_factory(next_command, snapshot)
            next_plan.bind_owner_thread()
            self._validate_plan_for_operation(next_command, next_plan)
            plan_holder["plan"] = next_plan
            prepared = runtime.preparer.prepare_next_attempt_with_owned_mutex(
                mutex=getattr(claim_holder["claim"].lease, "mutex", mutex),
                execution_id=execution_id,
                operation=operation,
            )
            # Publish the pending evidence into the lane-local recovery state
            # before the rotation CAS.  If rotation or binding is ambiguous,
            # the opaque ticket must still identify the prepared next roots;
            # otherwise those roots would be unreachable from reconciliation.
            state.plan = next_plan
            state.prepared = prepared
            state.operation = operation
            next_inputs[id(prepared)] = (
                MaterialOperationRequest(
                    protocol_version=_protocol_version_for_operation(operation),
                    execution_id=execution_id,
                    attempt_id=prepared.attempt_id,
                    operation=operation,
                    payload=next_plan.request_payload,
                ),
                MaterialOperationSecrets(
                    version=_protocol_version_for_operation(operation),
                    execution_id=execution_id,
                    attempt_id=prepared.attempt_id,
                    operation=operation,
                    payload=next_plan.secret_payload,
                ),
            )
            return prepared

        def bind_rotated_attempt(
            previous_claim: PreparedMaterialClaim,
            prepared: PreparedMaterialAttempt,
            operation: MaterialOperationKind,
        ) -> PreparedMaterialClaim:
            next_claim = runtime.preparer.bind_rotated_attempt(
                claim=previous_claim,
                prepared=prepared,
                operation=operation,
            )
            claim_holder.update(
                {
                    "claim": next_claim,
                    "operation": operation,
                    "request": None,
                    "secrets": None,
                }
            )
            state.claim = next_claim
            state.prepared = prepared
            state.operation = operation
            state.lease = next_claim.lease
            state.mutex = getattr(next_claim.lease, "mutex", mutex)
            state.plan = plan_holder["plan"]
            return next_claim

        def request_factory(
            operation: MaterialOperationKind,
            prepared: PreparedMaterialAttempt,
        ) -> tuple[MaterialOperationRequest, MaterialOperationSecrets]:
            del operation
            values = next_inputs.pop(id(prepared), None)
            if values is None:
                raise MaterialExecutionCoordinatorUnknown(
                    "next attempt inputs were not prepared before rotation"
                )
            claim_holder["request"], claim_holder["secrets"] = values
            return values

        coordinator = MaterialExecutionCoordinator(
            runner=runtime.runner,
            lifecycle=runtime.lifecycle,
            publication_factory=publication_factory,
        )
        try:
            result = coordinator.run_with_fallback(
                claim,
                request,
                secrets,
                fallback_operations=chain[1:],
                prepare_next_attempt=prepare_next_attempt,
                bind_rotated_attempt=bind_rotated_attempt,
                request_factory=request_factory,
                startup_timeout_seconds=command.startup_timeout_seconds,
                operation_timeout_seconds=command.operation_timeout_seconds,
            )
        except MaterialOperationBlocked:
            state.reason = "material operation is blocked; explicit recovery is required"
            state.snapshot = self._safe_fresh_read(runtime.store, fresh_payload or {})
            self._retained = state
            response = self._response_from_snapshot(
                replace(command, operation=state.operation),
                state.prepared.attempt_id,
                state.snapshot,
                status="blocked",
                message="material operation is blocked; explicit recovery is required",
            )
            return MaterialExecutionRetainedOwnership(response=response, state=state)
        except BaseException:
            state.reason = "material execution acknowledgement is unknown"
            state.snapshot = self._safe_fresh_read(runtime.store, fresh_payload or {})
            state.plan = plan_holder["plan"]
            self._retained = state
            response = self._response_from_snapshot(
                replace(command, operation=state.operation),
                state.prepared.attempt_id,
                state.snapshot,
                status="blocked",
                message=(
                    "material execution acknowledgement is unknown; explicit recovery is required"
                ),
            )
            return MaterialExecutionRetainedOwnership(response=response, state=state)

        final_snapshot = self._safe_fresh_read(runtime.store, fresh_payload or {})
        final_operation = claim_holder["operation"]
        final_attempt_id = (
            result.completion.attempt_id
            if result.completion
            else state.prepared.attempt_id
        )
        response = self._response_from_snapshot(
            replace(command, operation=final_operation),
            final_attempt_id,
            final_snapshot,
            status=result.status,
            message=_result_message(result),
            completion=result.completion,
        )
        try:
            plan_holder["plan"].release()
        except BaseException:
            state.reason = "material plan cleanup acknowledgement is unknown"
            state.snapshot = final_snapshot
            state.plan = plan_holder["plan"]
            self._retained = state
            blocked = self._response_from_snapshot(
                replace(command, operation=final_operation),
                final_attempt_id,
                final_snapshot,
                status="blocked",
                message="material plan cleanup is unknown; explicit recovery is required",
            )
            return MaterialExecutionRetainedOwnership(response=blocked, state=state)
        return response

    # Explicit alias used by application adapters that call the operation a
    # chain rather than a source chain.  It intentionally remains opt-in.
    def execute_chain(
        self,
        command: MaterialExecutionCommand,
        *,
        source_chain: Sequence[MaterialOperationKind],
    ):
        return self.execute_source_chain(command, source_chain=source_chain)

    def recover_restart_published(
        self,
        command: MaterialRestartRecoveryCommand,
    ) -> MaterialExecutionResponse:
        """Finish one already-published attempt without preparing or running it."""

        self._assert_lane_thread()
        if type(command) is not MaterialRestartRecoveryCommand:
            raise TypeError("command must be MaterialRestartRecoveryCommand")
        if (
            command.pipeline_id != self.key.pipeline_id
            or command.material_step_id != self.key.material_step_id
        ):
            raise ValueError("restart recovery belongs to another owner lane")
        if self._retained is not None or self._restart_retained is not None:
            raise MaterialExecutionLaneBusy(
                "production material handler still retains recovery ownership"
            )

        runtime = self._ensure_runtime()
        fresh = _clone_json_mapping(runtime.store.fresh_read())
        if (
            fresh.get("execution_revision") != command.expected_revision
            or _json_sha256(fresh) != command.expected_payload_sha256
            or fresh.get("execution_state") != "publishing"
            or fresh.get("publication_phase") != "db_committed"
            or (
                fresh.get("active_material_task_id"),
                fresh.get("active_execution_id"),
                fresh.get("active_attempt_id"),
                fresh.get("active_attempt_kind"),
                fresh.get("active_control_id"),
                fresh.get("active_job_instance_id"),
            )
            != (
                command.material_task_id,
                command.execution_id,
                command.attempt_id,
                command.operation,
                command.control_id,
                command.job_instance_id,
            )
        ):
            return _blocked_restart_response(
                command,
                "restart evidence changed after startup discovery; manual audit is required",
            )

        context_factory = self._publication_recovery_context_factory
        if not callable(context_factory):
            return _blocked_restart_response(
                command,
                "restart publication context is unavailable; manual audit is required",
            )
        try:
            context = context_factory(command, fresh)
            if type(context) is not ProductionMaterialPublicationRecoveryContext:
                raise TypeError(
                    "publication recovery context factory returned an invalid context"
                )
            if self._publisher_factory is _default_publisher_factory:
                normal_command = _execution_command_from_restart(command)
                self._validate_default_publisher_project_guard_mapping(
                    normal_command,
                    context.project_directory_uses,
                )
        except (TypeError, ValueError, ProductionMaterialExecutionError):
            return _blocked_restart_response(
                command,
                "restart publication identity is unavailable; manual audit is required",
            )

        acquire = getattr(runtime.store, "acquire_recovery_mutex", None)
        if not callable(acquire):
            return _blocked_restart_response(
                command,
                "restart recovery mutex is unavailable; manual audit is required",
            )
        try:
            mutex = acquire(timeout_ms=max(0, int(command.startup_timeout_seconds * 1000)))
        except WindowsMutexBusy:
            return _blocked_restart_response(
                command,
                "restart recovery is already owned by another live thread",
            )

        try:
            def recover_publication(claim: PreparedMaterialClaim, adopted: Any) -> Any:
                request = getattr(adopted, "request", None)
                outcome = getattr(adopted, "outcome", None)
                if type(request) is not MaterialOperationRequest:
                    raise MaterialExecutionCoordinatorUnknown(
                        "restart runner adoption returned no typed request"
                    )
                if (
                    type(outcome) is not MaterialOperationOutcome
                    or outcome.status != "succeeded"
                    or outcome.result is None
                ):
                    raise MaterialExecutionCoordinatorUnknown(
                        "restart runner adoption returned no successful result"
                    )
                database = ClaimLeasePublicationDatabase(claim.lease)
                publisher = self._publisher_factory(
                    canonical_paths=context.canonical_paths,
                    database=database,
                    expected_payload=claim.lease.expected_payload,
                )
                if getattr(publisher, "database", None) is not database:
                    raise MaterialExecutionCoordinatorUnknown(
                        "restart publisher is not bound to the recovered claim database"
                    )
                expected_payload = getattr(publisher, "expected_payload", None)
                if expected_payload is None or _clone_json_mapping(
                    expected_payload
                ) != _clone_json_mapping(claim.lease.expected_payload):
                    raise MaterialExecutionCoordinatorUnknown(
                        "restart publisher expected payload is not the adopted snapshot"
                    )
                adopt = getattr(publisher, "adopt_committed_publication", None)
                if not callable(adopt):
                    raise TypeError(
                        "publisher does not expose committed publication adoption"
                    )
                return adopt(
                    request,
                    claim.prepared.directory,
                    outcome.result,
                    project_directory_uses=context.project_directory_uses,
                )

            coordinator = self._restart_recovery_coordinator_factory(
                store=runtime.store,
                runner=runtime.runner,
                lifecycle=runtime.lifecycle,
                publication_recovery_factory=recover_publication,
            )
            complete = getattr(coordinator, "complete_published_attempt", None)
            if not callable(complete):
                raise TypeError(
                    "restart recovery coordinator has no published completion surface"
                )
            completion = complete(
                mutex=mutex,
                material_task_id=command.material_task_id,
                execution_id=command.execution_id,
                attempt_id=command.attempt_id,
                control_id=command.control_id,
                job_instance_id=command.job_instance_id,
                operation=command.operation,
            )
            if type(completion) is not MaterialCompletionReceipt:
                raise TypeError("restart recovery returned an invalid completion receipt")
            final = _clone_json_mapping(runtime.store.fresh_read())
            return self._response_from_snapshot(
                _execution_command_from_restart(command),
                command.attempt_id,
                final,
                status="succeeded",
                message="published material recovery completed without rerunning the worker",
                completion=completion,
            )
        except BaseException as exc:
            self._restart_retained = ProductionMaterialRestartRecoveryState(
                handler=self,
                command=command,
                mutex=mutex,
                error=exc,
            )
            raise MaterialExecutionLaneUnknown(
                "restart publication recovery is unknown; ownership remains on this lane"
            ) from exc

    def recover_runner_retirement(
        self,
        command: MaterialRunnerRetirementRecoveryCommand,
    ) -> MaterialExecutionResponse:
        """Retire one final checkpoint without replaying worker or publication."""

        self._assert_lane_thread()
        if type(command) is not MaterialRunnerRetirementRecoveryCommand:
            raise TypeError(
                "command must be MaterialRunnerRetirementRecoveryCommand"
            )
        if (
            command.pipeline_id != self.key.pipeline_id
            or command.material_step_id != self.key.material_step_id
        ):
            raise ValueError("runner retirement belongs to another owner lane")
        if self._retained is not None or self._restart_retained is not None:
            raise MaterialExecutionLaneBusy(
                "production material handler still retains recovery ownership"
            )

        runtime = self._ensure_runtime()
        fresh = _clone_json_mapping(runtime.store.fresh_read())
        try:
            checkpoint = pending_runner_retirement_from_payload(fresh)
        except ValueError:
            return _blocked_restart_response(
                command,
                "runner retirement evidence changed after startup discovery; manual audit is required",
            )
        if (
            checkpoint.material_task_id != command.material_task_id
            or checkpoint.execution_id != command.execution_id
            or checkpoint.attempt_id != command.attempt_id
            or checkpoint.operation != command.operation
            or checkpoint.control_id != command.control_id
            or checkpoint.job_instance_id != command.job_instance_id
            or checkpoint.completion_status != command.completion_status
            or checkpoint.completion_revision != command.completion_revision
            or checkpoint.checkpoint_sha256 != command.checkpoint_sha256
        ):
            return _blocked_restart_response(
                command,
                "runner retirement checkpoint changed after startup discovery; manual audit is required",
            )

        context: ProductionMaterialPublicationRecoveryContext | None = None
        if command.completion_status == "succeeded":
            context_factory = self._publication_recovery_context_factory
            if not callable(context_factory):
                return _blocked_restart_response(
                    command,
                    "completed publication context is unavailable; manual audit is required",
                )
            try:
                context = context_factory(command, fresh)
                if type(context) is not ProductionMaterialPublicationRecoveryContext:
                    raise TypeError(
                        "publication recovery context factory returned an invalid context"
                    )
                if self._publisher_factory is _default_publisher_factory:
                    normal_command = _execution_command_from_runner_retirement(command)
                    self._validate_default_publisher_project_guard_mapping(
                        normal_command,
                        context.project_directory_uses,
                    )
            except (TypeError, ValueError, ProductionMaterialExecutionError):
                return _blocked_restart_response(
                    command,
                    "completed publication identity is unavailable; manual audit is required",
                )

        acquire = getattr(runtime.store, "acquire_recovery_mutex", None)
        if not callable(acquire):
            return _blocked_restart_response(
                command,
                "runner retirement mutex is unavailable; manual audit is required",
            )
        try:
            mutex = acquire(timeout_ms=2000)
        except WindowsMutexBusy:
            return _blocked_restart_response(
                command,
                "runner retirement is already owned by another live thread",
            )

        try:
            def recover_completed_publication(claim: Any, adopted: Any) -> Any:
                if context is None:
                    raise MaterialExecutionCoordinatorUnknown(
                        "terminal retirement cannot recover a publication"
                    )
                request = getattr(adopted, "request", None)
                outcome = getattr(adopted, "outcome", None)
                if type(request) is not MaterialOperationRequest:
                    raise MaterialExecutionCoordinatorUnknown(
                        "retirement adoption returned no typed request"
                    )
                if (
                    type(outcome) is not MaterialOperationOutcome
                    or outcome.status != "succeeded"
                    or outcome.result is None
                ):
                    raise MaterialExecutionCoordinatorUnknown(
                        "retirement adoption returned no successful result"
                    )
                database = ClaimLeasePublicationDatabase(claim.lease)
                publisher = self._publisher_factory(
                    canonical_paths=context.canonical_paths,
                    database=database,
                    expected_payload=claim.lease.expected_payload,
                )
                if getattr(publisher, "database", None) is not database:
                    raise MaterialExecutionCoordinatorUnknown(
                        "completed publisher is not bound to the retirement lease"
                    )
                expected_payload = getattr(publisher, "expected_payload", None)
                if expected_payload is None or _clone_json_mapping(
                    expected_payload
                ) != _clone_json_mapping(claim.lease.expected_payload):
                    raise MaterialExecutionCoordinatorUnknown(
                        "completed publisher expected payload is not the pending snapshot"
                    )
                adopt = getattr(publisher, "adopt_completed_publication", None)
                if not callable(adopt):
                    raise TypeError(
                        "publisher does not expose completed publication adoption"
                    )
                return adopt(
                    request,
                    claim.prepared.directory,
                    outcome.result,
                    project_directory_uses=context.project_directory_uses,
                )

            coordinator = self._restart_recovery_coordinator_factory(
                store=runtime.store,
                runner=runtime.runner,
                lifecycle=runtime.lifecycle,
                publication_recovery_factory=recover_completed_publication,
            )
            complete = getattr(
                coordinator,
                "complete_pending_runner_retirement",
                None,
            )
            if not callable(complete):
                raise TypeError(
                    "restart recovery coordinator has no pending retirement surface"
                )
            completion = complete(
                mutex=mutex,
                material_task_id=command.material_task_id,
                execution_id=command.execution_id,
                attempt_id=command.attempt_id,
                control_id=command.control_id,
                job_instance_id=command.job_instance_id,
                operation=command.operation,
                completion_status=command.completion_status,
                completion_revision=command.completion_revision,
                checkpoint_sha256=command.checkpoint_sha256,
            )
            if type(completion) is not MaterialCompletionReceipt:
                raise TypeError(
                    "runner retirement recovery returned an invalid completion receipt"
                )
            final = _clone_json_mapping(runtime.store.fresh_read())
            return self._response_from_snapshot(
                _execution_command_from_runner_retirement(command),
                command.attempt_id,
                final,
                status=completion.status,
                message="runner retirement recovered without replaying the worker",
                completion=completion,
            )
        except BaseException as exc:
            self._restart_retained = ProductionMaterialRestartRecoveryState(
                handler=self,
                command=command,
                mutex=mutex,
                error=exc,
            )
            raise MaterialExecutionLaneUnknown(
                "runner retirement recovery is unknown; ownership remains on this lane"
            ) from exc

    def reconcile(
        self,
        state: ProductionMaterialRecoveryState,
        ticket: MaterialRecoveryTicket,
    ) -> MaterialExecutionResponse | MaterialExecutionRetainedOwnership:
        self._assert_lane_thread()
        if type(state) is not ProductionMaterialRecoveryState:
            raise TypeError("state must be ProductionMaterialRecoveryState")
        if state.handler is not self or ticket.lane_id != self._lane_id:
            raise ValueError("recovery state belongs to another owner lane")
        if (
            ticket.pipeline_id != self.key.pipeline_id
            or ticket.material_step_id != self.key.material_step_id
            or ticket.material_task_id != state.material_task_id
            or ticket.execution_id != state.prepared.execution_id
            or ticket.attempt_id != state.prepared.attempt_id
            or ticket.operation != state.operation
        ):
            raise ValueError("recovery ticket does not match retained attempt")
        resolver = self._recovery_handler
        if resolver is None:
            raise MaterialExecutionLaneUnknown(
                "production recovery requires an explicit owner-lane resolver"
            )
        result = resolver(state, ticket)
        if type(result) is MaterialExecutionRetainedOwnership:
            next_state = result.state
            if not isinstance(next_state, ProductionMaterialRecoveryState):
                raise TypeError(
                    "retained recovery result must carry ProductionMaterialRecoveryState"
                )
            if next_state.handler is not self:
                raise ValueError("recovery result belongs to another owner lane")
            if (
                next_state.operation != ticket.operation
                or next_state.material_task_id != ticket.material_task_id
                or next_state.prepared.execution_id != ticket.execution_id
                or next_state.prepared.attempt_id != ticket.attempt_id
            ):
                raise ValueError("recovery result identity does not match its ticket")
            self._retained = next_state
            return result
        if type(result) is not MaterialExecutionResponse:
            raise TypeError("recovery handler returned an unsupported result")
        if result.recovery_ticket is not None:
            raise ValueError("recovery handler returned an externally ticketed response")
        if (
            result.pipeline_id != ticket.pipeline_id
            or result.material_step_id != ticket.material_step_id
            or result.material_task_id != ticket.material_task_id
            or result.execution_id != ticket.execution_id
            or result.attempt_id != ticket.attempt_id
            or result.operation != ticket.operation
        ):
            raise ValueError("recovery response identity does not match its ticket")
        state.plan.release()
        self._retained = None
        return result

    def close(self) -> None:
        """Close lane-owned runner resources after all recovery is resolved."""

        self._assert_lane_thread()
        if self._retained is not None or self._restart_retained is not None:
            raise MaterialExecutionLaneBusy(
                "production material handler still retains recovery ownership"
            )
        if self._closed:
            return
        runtime = self._runtime
        if runtime is not None:
            close = getattr(runtime.runner, "close", None)
            if callable(close):
                close()
        self._closed = True

    def _ensure_runtime(self) -> _ProductionRuntime:
        self._assert_lane_thread()
        if self._closed:
            raise MaterialExecutionLaneUnknown("production material handler is closed")
        runtime = self._runtime
        if runtime is None:
            claim_key = MaterialExecutionClaimKey(
                database_path=self.key.database_path,
                pipeline_id=self.key.pipeline_id,
                material_step_id=self.key.material_step_id,
            )
            store = self._claim_store_factory(claim_key)
            runner = self._runner_factory(self.key)
            lifecycle = self._lifecycle_factory(self.key, store)
            preparer = MaterialExecutionClaimPreparer(store=store, runner=runner)
            runtime = _ProductionRuntime(
                store=store,
                runner=runner,
                lifecycle=lifecycle,
                preparer=preparer,
            )
            self._runtime = runtime
        return runtime

    def _build_publication(
        self,
        plan: ProductionMaterialOperationPlan,
        claim: PreparedMaterialClaim,
        request: MaterialOperationRequest,
        outcome: MaterialOperationOutcome,
    ) -> CanonicalMaterialPublication:
        if outcome.result is None:
            raise MaterialExecutionCoordinatorUnknown(
                "publication requires a worker result"
            )
        database = ClaimLeasePublicationDatabase(claim.lease)
        publisher = self._publisher_factory(
            canonical_paths=plan.canonical_paths,
            database=database,
            expected_payload=claim.lease.expected_payload,
        )
        if getattr(publisher, "database", None) is not database:
            raise MaterialExecutionCoordinatorUnknown(
                "publisher is not bound to the active claim lease database"
            )
        expected_payload = getattr(publisher, "expected_payload", None)
        if expected_payload is None or _clone_json_mapping(expected_payload) != _clone_json_mapping(
            claim.lease.expected_payload
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "publisher expected payload is not the immutable claim snapshot"
            )
        prepare = getattr(publisher, "prepare", None)
        if not callable(prepare):
            raise TypeError("publisher must expose prepare")
        publication = prepare(
            request,
            claim.prepared.directory,
            outcome.result,
            project_directory_uses=plan.project_directory_uses,
        )
        if not isinstance(publication, CanonicalMaterialPublication):
            # Test seams may provide a protocol-compatible object; the
            # coordinator will enforce its committed ownership properties.
            if not all(
                callable(getattr(publication, name, None))
                for name in ("commit", "release_after_retirement")
            ):
                raise TypeError("publisher.prepare returned an invalid publication")
        return publication

    def _validate_plan_for_operation(
        self,
        command: MaterialExecutionCommand,
        plan: ProductionMaterialOperationPlan,
    ) -> None:
        if type(plan) is not ProductionMaterialOperationPlan:
            raise TypeError("operation plan factory must return ProductionMaterialOperationPlan")
        probe_attempt_id = str(uuid4())
        # Validate payload/secret types before creating an attempt or DB claim.
        MaterialOperationRequest(
            protocol_version=_protocol_version_for_operation(command.operation),
            execution_id=command.execution_id,
            attempt_id=probe_attempt_id,
            operation=command.operation,
            payload=plan.request_payload,
        )
        MaterialOperationSecrets(
            version=_protocol_version_for_operation(command.operation),
            execution_id=command.execution_id,
            attempt_id=probe_attempt_id,
            operation=command.operation,
            payload=plan.secret_payload,
        )
        if self._publisher_factory is _default_publisher_factory:
            self._validate_default_publisher_project_guards(command, plan)

    @staticmethod
    def _validate_default_publisher_project_guards(
        command: MaterialExecutionCommand,
        plan: ProductionMaterialOperationPlan,
    ) -> None:
        ProductionMaterialExecutionHandler._validate_default_publisher_project_guard_mapping(
            command,
            plan.project_directory_uses,
        )

    @staticmethod
    def _validate_default_publisher_project_guard_mapping(
        command: MaterialExecutionCommand,
        project_directory_uses: Mapping[str, Any],
    ) -> None:
        required_by_operation = {
            "official_capture": ("官方录屏",),
            "pexels": ("Pexels素材", "全部素材"),
            "pixabay": ("全部素材",),
            "coverr": ("全部素材",),
            "info_motion": ("信息动效",),
            "stock_candidates": ("Pexels素材", "全部素材"),
        }
        required = required_by_operation.get(command.operation)
        if required is None:
            raise ProductionMaterialExecutionError(
                "material operation has no project directory guard contract"
            )
        guards: list[PreparedProjectChildDirectoryUse] = []
        for name in required:
            guard = project_directory_uses.get(name)
            if not isinstance(guard, PreparedProjectChildDirectoryUse):
                raise ProductionMaterialExecutionError(
                    f"material operation requires prepared project directory guard: {name}"
                )
            pipeline_id = getattr(guard.project_use.pipeline, "id", None)
            if pipeline_id != command.pipeline_id:
                raise ProductionMaterialExecutionError(
                    "project directory guard belongs to another pipeline"
                )
            guard.verify()
            guards.append(guard)
        project_paths = {guard.project_use.path for guard in guards}
        if len(project_paths) != 1:
            raise ProductionMaterialExecutionError(
                "project directory guards do not belong to one prepared project"
            )

    def _response_from_snapshot(
        self,
        command: MaterialExecutionCommand,
        attempt_id: str,
        snapshot: Mapping[str, Any],
        *,
        status: str,
        message: str,
        completion: Any | None = None,
    ) -> MaterialExecutionResponse:
        tasks: list[Mapping[str, Any]] = []
        raw_tasks = snapshot.get("material_tasks", ())
        if isinstance(raw_tasks, (list, tuple)):
            for item in raw_tasks:
                if isinstance(item, Mapping):
                    tasks.append(_json_mapping(item))
        executed = next(
            (
                item
                for item in tasks
                if item.get("material_task_id") == command.material_task_id
            ),
            None,
        )
        return MaterialExecutionResponse(
            pipeline_id=command.pipeline_id,
            material_step_id=command.material_step_id,
            material_task_id=command.material_task_id,
            execution_id=command.execution_id,
            attempt_id=attempt_id,
            operation=command.operation,
            status=status,  # type: ignore[arg-type]
            message=message[:2000],
            executed_task=executed,
            tasks=tuple(tasks),
            completion=completion,
        )

    def _safe_fresh_read(
        self,
        store: Any,
        fallback: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            return _clone_json_mapping(store.fresh_read())
        except BaseException:
            return _clone_json_mapping(fallback)

    def _assert_lane_thread(self) -> None:
        current = threading.get_ident()
        if self._owner_thread_ident is None:
            self._owner_thread_ident = current
        elif self._owner_thread_ident != current:
            raise MaterialExecutionLaneUnknown(
                "production material ownership must stay on one lane thread"
            )

    @property
    def _lane_id(self) -> str:
        return self.key.lane_id


class ProductionMaterialExecutionHandlerFactory:
    """Lazy, testable factory for :class:`ProductionMaterialExecutionHandler`.

    ``claim_store_factory`` is preferred in tests.  Production callers may
    instead provide ``session_factory``; the concrete SQLAlchemy claim store
    is then constructed only when the owner lane first executes a command.
    """

    def __init__(
        self,
        *,
        runner_factory: Callable[[MaterialExecutionLaneKey], Any],
        operation_plan_factory: ProductionMaterialOperationPlanFactory,
        lifecycle_factory: Callable[[MaterialExecutionLaneKey, Any], MaterialLifecycleAdapter]
        | None = None,
        publisher_factory: ProductionMaterialPublisherFactory | None = None,
        recovery_handler: ProductionMaterialRecoveryHandler | None = None,
        publication_recovery_context_factory: (
            ProductionMaterialPublicationRecoveryContextFactory | None
        ) = None,
        restart_recovery_coordinator_factory: Callable[..., Any] = (
            MaterialRestartRecoveryCoordinator
        ),
        claim_store_factory: Callable[[MaterialExecutionClaimKey], Any] | None = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        if (claim_store_factory is None) == (session_factory is None):
            raise ValueError(
                "provide exactly one of claim_store_factory or session_factory"
            )
        if claim_store_factory is not None and not callable(claim_store_factory):
            raise TypeError("claim_store_factory must be callable")
        if session_factory is not None and not callable(session_factory):
            raise TypeError("session_factory must be callable, not a Session instance")
        if not callable(runner_factory):
            raise TypeError("runner_factory must be callable")
        if not callable(operation_plan_factory):
            raise TypeError("operation_plan_factory must be callable")
        if lifecycle_factory is not None and not callable(lifecycle_factory):
            raise TypeError("lifecycle_factory must be callable")
        if publisher_factory is not None and not callable(publisher_factory):
            raise TypeError("publisher_factory must be callable")
        if recovery_handler is not None and not callable(recovery_handler):
            raise TypeError("recovery_handler must be callable")
        if publication_recovery_context_factory is not None and not callable(
            publication_recovery_context_factory
        ):
            raise TypeError("publication_recovery_context_factory must be callable")
        if not callable(restart_recovery_coordinator_factory):
            raise TypeError("restart_recovery_coordinator_factory must be callable")
        self._runner_factory = runner_factory
        self._operation_plan_factory = operation_plan_factory
        self._lifecycle_factory = lifecycle_factory or (
            lambda _key, _store: SqlAlchemyMaterialLifecycleAdapter()
        )
        self._publisher_factory = publisher_factory or _default_publisher_factory
        self._recovery_handler = recovery_handler
        self._publication_recovery_context_factory = publication_recovery_context_factory
        self._restart_recovery_coordinator_factory = restart_recovery_coordinator_factory
        if claim_store_factory is not None:
            self._claim_store_factory = claim_store_factory
        else:
            assert session_factory is not None

            def _store_factory(key: MaterialExecutionClaimKey) -> Any:
                return SqlAlchemyMaterialExecutionClaimStore(session_factory, key=key)

            self._claim_store_factory = _store_factory

    def __call__(self, key: MaterialExecutionLaneKey) -> ProductionMaterialExecutionHandler:
        return ProductionMaterialExecutionHandler(
            key=key,
            claim_store_factory=self._claim_store_factory,
            runner_factory=self._runner_factory,
            operation_plan_factory=self._operation_plan_factory,
            lifecycle_factory=self._lifecycle_factory,
            publisher_factory=self._publisher_factory,
            recovery_handler=self._recovery_handler,
            publication_recovery_context_factory=(
                self._publication_recovery_context_factory
            ),
            restart_recovery_coordinator_factory=(
                self._restart_recovery_coordinator_factory
            ),
        )

    def build(self, key: MaterialExecutionLaneKey) -> ProductionMaterialExecutionHandler:
        """Named alias for registries that prefer an explicit factory method."""

        return self(key)


def _execution_command_from_restart(
    command: MaterialRestartRecoveryCommand,
) -> MaterialExecutionCommand:
    return MaterialExecutionCommand(
        pipeline_id=command.pipeline_id,
        material_step_id=command.material_step_id,
        material_task_id=command.material_task_id,
        execution_id=command.execution_id,
        operation=command.operation,
        startup_timeout_seconds=command.startup_timeout_seconds,
        operation_timeout_seconds=max(command.startup_timeout_seconds, 1.0),
    )


def _execution_command_from_runner_retirement(
    command: MaterialRunnerRetirementRecoveryCommand,
) -> MaterialExecutionCommand:
    return MaterialExecutionCommand(
        pipeline_id=command.pipeline_id,
        material_step_id=command.material_step_id,
        material_task_id=command.material_task_id,
        execution_id=command.execution_id,
        operation=command.operation,
        startup_timeout_seconds=1.0,
        operation_timeout_seconds=1.0,
    )


def _blocked_restart_response(
    command: MaterialRestartRecoveryCommand | MaterialRunnerRetirementRecoveryCommand,
    message: str,
) -> MaterialExecutionResponse:
    return MaterialExecutionResponse(
        pipeline_id=command.pipeline_id,
        material_step_id=command.material_step_id,
        material_task_id=command.material_task_id,
        execution_id=command.execution_id,
        attempt_id=command.attempt_id,
        operation=command.operation,
        status="blocked",
        message=message,
        executed_task=None,
        tasks=(),
    )


def _json_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    import hashlib

    return hashlib.sha256(encoded).hexdigest()


def _default_publisher_factory(
    *,
    canonical_paths: AutoMaterialPaths,
    database: ClaimLeasePublicationDatabase,
    expected_payload: Mapping[str, Any],
) -> CanonicalMaterialArtifactPublisher:
    """Build the only publisher shape allowed by this application seam."""

    return CanonicalMaterialArtifactPublisher(
        canonical_paths=canonical_paths,
        database=database,
        expected_payload=expected_payload,
        media_validator=validate_candidate_media,
    )


def _release_known_mutex(mutex: Any) -> None:
    release = getattr(mutex, "release", None)
    if not callable(release):
        raise MaterialExecutionLaneUnknown("claim mutex has no release method")
    try:
        release()
    except BaseException as exc:
        raise MaterialExecutionLaneUnknown(
            "claim mutex release acknowledgement is unknown"
        ) from exc


def _release_preclaim_resources(
    plan: ProductionMaterialOperationPlan | None,
    mutex: Any,
) -> None:
    """Release pre-claim resources in reverse order, preserving ambiguity."""

    errors: list[BaseException] = []
    if plan is not None:
        try:
            plan.release()
        except BaseException as exc:
            errors.append(exc)
    try:
        _release_known_mutex(mutex)
    except BaseException as exc:
        errors.append(exc)
    if errors:
        raise MaterialExecutionLaneUnknown(
            "pre-claim resource release acknowledgement is unknown"
        ) from errors[0]


def _clone_json_mapping(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("fresh material payload must be a mapping")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TypeError("fresh material payload must contain only JSON values") from exc
    if not isinstance(decoded, dict):
        raise TypeError("fresh material payload must be a JSON object")
    return decoded


def _json_mapping(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    return _clone_json_mapping(value)


def _assert_task_snapshot(payload: Mapping[str, Any], material_task_id: str) -> None:
    """Reject an explicitly present task list that omits the claimed task."""

    if "material_tasks" not in payload:
        return
    raw_tasks = payload.get("material_tasks")
    if not isinstance(raw_tasks, (list, tuple)):
        raise ValueError("fresh material payload has no valid material task list")
    matches = tuple(
        item
        for item in raw_tasks
        if isinstance(item, Mapping)
        and item.get("material_task_id") == material_task_id
    )
    if not matches:
        raise ValueError("fresh material payload does not contain the requested task")
    if len(matches) != 1:
        raise ValueError(
            "fresh material payload requires exactly one matching material_task_id"
        )


def _result_message(result: MaterialCoordinatorResult) -> str:
    outcome = result.outcome
    if outcome is None:
        return result.status
    if outcome.result is not None and outcome.result.reason:
        return outcome.result.reason
    return result.status


__all__ = [
    "ProductionMaterialExecutionError",
    "ProductionMaterialExecutionHandler",
    "ProductionMaterialExecutionHandlerFactory",
    "ProductionMaterialOperationPlan",
    "ProductionMaterialOperationPlanFactory",
    "ProductionMaterialPublicationRecoveryContext",
    "ProductionMaterialPublicationRecoveryContextFactory",
    "ProductionMaterialPublisherFactory",
    "ProductionMaterialRecoveryHandler",
    "ProductionMaterialRecoveryState",
    "ProductionMaterialRestartRecoveryState",
]
