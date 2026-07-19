"""Route-free asynchronous coordinator for durable material submissions.

The owner lane deliberately returns a :class:`concurrent.futures.Future`.
This boundary turns that future into durable, typed submission evidence without
waiting on it from the request thread.  The future is kept only by its callback
closure; the durable store remains the source of truth for status and recovery.

No FastAPI route, SQLAlchemy session, owner-lane raw state, or future is
registered here.  A route adapter can call :meth:`submit`, return the JSON DTO,
and later call :meth:`status` or :meth:`reconcile`.
"""

from __future__ import annotations

from concurrent.futures import CancelledError, Future
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import re
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from app.pipeline.material_execution_api_context import (
    MaterialExecutionSubmissionContext,
)
from app.pipeline.material_execution_application import (
    MaterialExecutionLaneUnknown,
    MaterialExecutionOwnerLaneRegistry,
    MaterialExecutionResponse,
    MaterialRecoveryTicket,
)
from app.pipeline.material_execution_submission_store import (
    MaterialExecutionSubmissionErrorMetadata,
    MaterialExecutionSubmissionIdentityConflict,
    MaterialExecutionSubmissionNotFound,
    MaterialExecutionSubmissionRecord,
    MaterialExecutionSubmissionStateConflict,
    MaterialExecutionSubmissionStore,
    MaterialExecutionSubmissionTransitionError,
    SubmissionState,
)
from app.pipeline.material_execution_submission_owner_lease import (
    MaterialExecutionSubmissionOwnerLease,
)


MAX_ERROR_DETAIL_CHARS = 2000
"""Maximum detail retained in a status error DTO."""

_STATUS_STATES = frozenset(
    {
        "accepted",
        "dispatching",
        "running",
        "succeeded",
        "failed",
        "blocked",
        "reconciling",
        "unknown",
    }
)
_STATUS_OPERATIONS = frozenset(
    {
        "official_capture",
        "pexels",
        "pixabay",
        "coverr",
        "info_motion",
        "stock_candidates",
    }
)
_STATUS_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_STATUS_LANE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_PATH_RE = re.compile(r"(?i)(?:[A-Z]:[\\/]|\\\\)[^\s<>\"']+")


class MaterialExecutionSubmissionCoordinatorError(RuntimeError):
    """Base class for route-free coordinator failures."""


class MaterialExecutionSubmissionRetryRejected(
    MaterialExecutionSubmissionCoordinatorError
):
    """An ``unknown`` submission cannot be retried automatically."""


class MaterialExecutionSubmissionReconcileRejected(
    MaterialExecutionSubmissionCoordinatorError
):
    """A submission is not a live blocked owner-lane recovery."""


def _validate_uuid(value: Any, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a canonical lowercase UUID")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a canonical lowercase UUID") from exc
    return value


def _json_copy(value: Any, *, label: str) -> Any:
    """Return a detached JSON-safe value, failing closed on non-JSON data."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        return json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TypeError(f"{label} must be JSON-safe") from exc


@dataclass(frozen=True, slots=True)
class MaterialExecutionSubmissionStatus:
    """Pure JSON-safe projection of a durable submission record.

    The durable record contains a :class:`Path` and typed command/context
    objects.  This projection intentionally exposes only scalar identity and
    detached JSON evidence; no ``Future``, session, mutex, lane handler, or
    retained raw recovery state can cross this boundary.
    """

    submission_id: str
    state: SubmissionState
    revision: int
    created_at: str
    updated_at: str
    lane_id: str
    pipeline_id: int
    material_step_id: int
    material_task_id: str
    execution_id: str
    operation: str
    attempt_id: str | None = None
    response: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    recovery_ticket: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _validate_uuid(self.submission_id, "submission_id")
        if type(self.state) is not str or self.state not in _STATUS_STATES:
            raise ValueError("state is not a recognized submission state")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if type(self.created_at) is not str or type(self.updated_at) is not str:
            raise TypeError("status timestamps must be strings")
        try:
            created_at = datetime.fromisoformat(self.created_at)
            updated_at = datetime.fromisoformat(self.updated_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("status timestamps must be ISO-8601 values") from exc
        try:
            timestamps_reversed = updated_at < created_at
        except TypeError as exc:
            raise ValueError("status timestamps must use compatible timezones") from exc
        if timestamps_reversed:
            raise ValueError("updated_at cannot precede created_at")
        if type(self.lane_id) is not str or _STATUS_LANE_ID_RE.fullmatch(self.lane_id) is None:
            raise ValueError("lane_id must be a lowercase SHA-256 hex string")
        for value, label in (
            (self.pipeline_id, "pipeline_id"),
            (self.material_step_id, "material_step_id"),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        if (
            type(self.material_task_id) is not str
            or _STATUS_TASK_ID_RE.fullmatch(self.material_task_id) is None
        ):
            raise ValueError("material_task_id is invalid")
        _validate_uuid(self.execution_id, "execution_id")
        if type(self.operation) is not str or self.operation not in _STATUS_OPERATIONS:
            raise ValueError("operation is not allowed")
        if self.attempt_id is not None:
            _validate_uuid(self.attempt_id, "attempt_id")
        if self.response is not None:
            value = _json_copy(dict(self.response), label="response")
            if not isinstance(value, dict):
                raise TypeError("response must be a JSON object")
            object.__setattr__(self, "response", value)
        if self.error is not None:
            value = _json_copy(dict(self.error), label="error")
            if not isinstance(value, dict):
                raise TypeError("error must be a JSON object")
            object.__setattr__(self, "error", value)
        if self.recovery_ticket is not None:
            value = _json_copy(dict(self.recovery_ticket), label="recovery_ticket")
            if not isinstance(value, dict):
                raise TypeError("recovery_ticket must be a JSON object")
            object.__setattr__(self, "recovery_ticket", value)

    @property
    def response_json(self) -> Mapping[str, Any] | None:
        """Compatibility alias for callers migrating from the store record."""

        return self.response

    @property
    def error_metadata(self) -> Mapping[str, Any] | None:
        return self.error

    @property
    def recovery_ticket_json(self) -> Mapping[str, Any] | None:
        return self.recovery_ticket

    def to_json(self) -> dict[str, Any]:
        """Return a detached object suitable for ``json.dumps``/HTTP JSON."""

        value = {
            "submission_id": self.submission_id,
            "state": self.state,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "lane_id": self.lane_id,
            "pipeline_id": self.pipeline_id,
            "material_step_id": self.material_step_id,
            "material_task_id": self.material_task_id,
            "execution_id": self.execution_id,
            "operation": self.operation,
            "attempt_id": self.attempt_id,
            "response": None if self.response is None else dict(self.response),
            "error": None if self.error is None else dict(self.error),
            "recovery_ticket": (
                None
                if self.recovery_ticket is None
                else dict(self.recovery_ticket)
            ),
        }
        copied = _json_copy(value, label="submission status")
        if not isinstance(copied, dict):  # pragma: no cover - defensive
            raise TypeError("submission status must be a JSON object")
        return copied

    # A few route adapters use ``to_dict`` for DTOs; keep it an explicit alias
    # while retaining the stronger ``to_json`` name in the public contract.
    def to_dict(self) -> dict[str, Any]:
        return self.to_json()

    @classmethod
    def from_record(
        cls,
        record: MaterialExecutionSubmissionRecord,
    ) -> "MaterialExecutionSubmissionStatus":
        response = (
            None
            if record.response_json is None
            else _json_copy(dict(record.response_json), label="response")
        )
        error = None if record.error is None else record.error.to_json()
        ticket = None if record.recovery_ticket is None else asdict(record.recovery_ticket)
        command = record.context.command
        return cls(
            submission_id=record.submission_id,
            state=record.state,
            revision=record.revision,
            created_at=record.created_at,
            updated_at=record.updated_at,
            lane_id=record.context.lane_key.lane_id,
            pipeline_id=command.pipeline_id,
            material_step_id=command.material_step_id,
            material_task_id=command.material_task_id,
            execution_id=command.execution_id,
            operation=command.operation,
            attempt_id=record.attempt_id,
            response=response,
            error=error,
            recovery_ticket=ticket,
        )


def _exception_detail(exc: BaseException, *, prefix: str = "") -> str:
    """Make bounded, deterministic-enough detail from an arbitrary exception."""

    try:
        detail = str(exc).strip()
    except BaseException:  # pragma: no cover - hostile exception ``__str__``
        detail = "exception text was unavailable"
    if not detail:
        detail = "operation raised an exception"
    value = f"{prefix}{detail}" if prefix else detail
    # Status projections are safe to persist and expose to a future route, so
    # never echo a physical sidecar/database path from an arbitrary provider or
    # callback exception.  Keep the exception type and a useful redacted reason.
    value = _WINDOWS_PATH_RE.sub("[path]", value)
    return value[:MAX_ERROR_DETAIL_CHARS]


def _error_metadata(
    code: str,
    exc: BaseException,
    *,
    retryable: bool = False,
    recovery_required: bool = False,
    prefix: str = "",
) -> MaterialExecutionSubmissionErrorMetadata:
    exception_type = type(exc).__name__
    # Type names are generally safe identifiers but a user-defined exception
    # may provide an unusual name.  Keep data boring and bounded.
    exception_type = "".join(
        character for character in exception_type if character.isalnum() or character == "_"
    )[:128] or "Exception"
    return MaterialExecutionSubmissionErrorMetadata(
        code=code,
        detail=_exception_detail(exc, prefix=prefix),
        retryable=retryable,
        recovery_required=recovery_required,
        data={"exception_type": exception_type},
    )


class MaterialExecutionSubmissionCoordinator:
    """Coordinate durable submission state with one process-local owner lane."""

    def __init__(
        self,
        *,
        store: MaterialExecutionSubmissionStore,
        registry: MaterialExecutionOwnerLaneRegistry | Any,
        owner_instance_id: str | None = None,
        owner_lease: MaterialExecutionSubmissionOwnerLease | None = None,
    ) -> None:
        if not isinstance(store, MaterialExecutionSubmissionStore):
            raise TypeError("store must be MaterialExecutionSubmissionStore")
        if not callable(getattr(registry, "get_or_create", None)):
            raise TypeError("registry must expose get_or_create")
        self.store = store
        self.registry = registry
        acquired_here = False
        if owner_lease is None:
            owner_lease = MaterialExecutionSubmissionOwnerLease.acquire(
                store,
                owner_instance_id=(
                    str(uuid4()) if owner_instance_id is None else owner_instance_id
                ),
            )
            acquired_here = True
        elif type(owner_lease) is not MaterialExecutionSubmissionOwnerLease:
            raise TypeError("owner_lease must be MaterialExecutionSubmissionOwnerLease")
        if owner_lease.store is not store:
            raise ValueError(
                "owner_lease must belong to this exact submission store instance"
            )
        if owner_instance_id is not None and owner_lease.owner_instance_id != owner_instance_id:
            raise ValueError("owner_instance_id does not match owner_lease")
        try:
            owner_lease.bind_coordinator(self, registry)
        except BaseException:
            if acquired_here:
                try:
                    owner_lease.close()
                except BaseException:
                    pass
            raise
        self.owner_lease = owner_lease
        self.owner_instance_id = owner_lease.owner_instance_id
        self.owner_epoch = owner_lease.owner_epoch
        self._owner_capability = owner_lease.capability
        self._fenced_at_start = tuple(owner_lease.fenced_submissions)
        self._closed = False

    def submit(
        self,
        context: MaterialExecutionSubmissionContext,
        submission_id: str | None = None,
    ) -> MaterialExecutionSubmissionStatus:
        """Durably accept and enqueue a source chain without waiting.

        A duplicate ID is identity-checked by the store.  Only the caller that
        wins ``accepted -> dispatching`` gets to enqueue, so retries cannot
        submit the same source chain twice.
        """

        self.owner_lease.assert_active()
        if type(context) is not MaterialExecutionSubmissionContext:
            raise TypeError("context must be MaterialExecutionSubmissionContext")
        sid = str(uuid4()) if submission_id is None else submission_id
        _validate_uuid(sid, "submission_id")
        record = self.store.create(sid, context)
        if record.state != "accepted":
            # ``unknown`` is intentionally display-only.  A duplicate submit
            # must never turn it into a fresh owner-lane attempt.
            return self._status(record)

        try:
            record = self.store.compare_and_swap(
                sid,
                expected_state="accepted",
                next_state="dispatching",
                owner_instance_id=self.owner_instance_id,
                owner_epoch=self.owner_epoch,
                owner_capability=self._owner_capability,
            )
        except MaterialExecutionSubmissionStateConflict:
            return self._status_required(sid)

        try:
            lane = self.registry.get_or_create(context.lane_key)
            submit_source_chain = getattr(lane, "submit_source_chain", None)
            if not callable(submit_source_chain):
                raise TypeError("owner lane does not expose submit_source_chain")
            future = submit_source_chain(
                context.command,
                source_chain=context.source_chain,
            )
            self._validate_future(future)
        except BaseException as exc:
            # The lane call may have had an external side effect before an
            # exception or malformed Future crossed this boundary.  The
            # acknowledgement is therefore unknown, never a retryable
            # deterministic failure.
            error = _error_metadata(
                "submission_dispatch_unknown",
                exc,
                recovery_required=True,
            )
            self._persist_unknown(sid, error)
            return self._status_required(sid)

        # Register before the running CAS.  An already-completed Future invokes
        # this callback inline, intentionally covering the accepted/dispatching
        # crash window.  The callback itself never waits on an unfinished
        # Future; ``result()`` is reached only after ``done()`` is true.
        try:
            future.add_done_callback(
                lambda done: self._on_execution_done(sid, done, reconcile=False)
            )
        except BaseException as exc:
            # Callback registration failure means completion evidence is not
            # trustworthy; mark the dispatch unknown rather than enqueueing a
            # second attempt on a later request.
            self._persist_unknown(
                sid,
                _error_metadata(
                    "submission_callback_unknown",
                    exc,
                    recovery_required=True,
                ),
            )
            return self._status_required(sid)

        try:
            self.store.compare_and_swap(
                sid,
                expected_state="dispatching",
                next_state="running",
                owner_instance_id=self.owner_instance_id,
                owner_epoch=self.owner_epoch,
                owner_capability=self._owner_capability,
            )
        except MaterialExecutionSubmissionStateConflict:
            # The callback may have won and committed terminal evidence.  A
            # conflict is normal in that race; return whatever is durable now.
            return self._status_required(sid)
        return self._status_required(sid)

    def status(
        self,
        submission_id: str,
    ) -> MaterialExecutionSubmissionStatus | None:
        """Read a JSON-safe status projection; never expose the raw record."""

        record = self.store.get(submission_id)
        return None if record is None else self._status(record)

    def reconcile(self, submission_id: str) -> MaterialExecutionSubmissionStatus:
        """Asynchronously reconcile a blocked recovery retained by this lane."""

        self.owner_lease.assert_active()
        record = self.store.get(submission_id)
        if record is None:
            raise MaterialExecutionSubmissionNotFound(submission_id)
        if record.state == "unknown":
            raise MaterialExecutionSubmissionRetryRejected(
                "unknown submission requires explicit operator adoption or close"
            )
        if record.state != "blocked":
            if record.state in {"reconciling", "succeeded", "failed"}:
                return self._status(record)
            raise MaterialExecutionSubmissionReconcileRejected(
                f"submission is not live blocked evidence: {record.state}"
            )
        if record.owner_instance_id != self.owner_instance_id:
            raise MaterialExecutionSubmissionReconcileRejected(
                "blocked recovery belongs to another owner instance"
            )
        ticket = record.recovery_ticket
        if ticket is None:  # Defensive; store schema already rejects this.
            raise MaterialExecutionSubmissionReconcileRejected(
                "blocked submission has no recovery ticket"
            )

        try:
            current = self.store.compare_and_swap(
                submission_id,
                expected_state="blocked",
                next_state="reconciling",
                owner_instance_id=self.owner_instance_id,
                owner_epoch=self.owner_epoch,
                owner_capability=self._owner_capability,
            )
        except MaterialExecutionSubmissionStateConflict:
            return self._status_required(submission_id)

        try:
            lane = self.registry.get_or_create(record.context.lane_key)
            reconcile = getattr(lane, "reconcile", None)
            if not callable(reconcile):
                raise TypeError("owner lane does not expose reconcile")
            future = reconcile(ticket)
            self._validate_future(future)
        except BaseException as exc:
            self._persist_reconcile_blocked(
                submission_id,
                _error_metadata(
                    "submission_reconcile_failed",
                    exc,
                    recovery_required=True,
                ),
                ticket,
            )
            return self._status_required(submission_id)

        try:
            future.add_done_callback(
                lambda done: self._on_execution_done(submission_id, done, reconcile=True)
            )
        except BaseException as exc:
            self._persist_unknown(
                submission_id,
                _error_metadata(
                    "submission_reconcile_callback_unknown",
                    exc,
                    recovery_required=True,
                ),
            )
        return self._status_required(submission_id)

    def close_unknown(
        self,
        submission_id: str,
        *,
        error: MaterialExecutionSubmissionErrorMetadata | None = None,
    ) -> MaterialExecutionSubmissionStatus:
        """Explicitly close an ``unknown`` record as a typed failure.

        This is an operator/audit action, not a retry.  It intentionally does
        not assert the old owner instance: after a process restart the old
        owner may be gone, while the CAS revision still prevents a stale
        callback from overwriting the close.
        """

        self.owner_lease.assert_active()
        record = self.store.get(submission_id)
        if record is None:
            raise MaterialExecutionSubmissionNotFound(submission_id)
        if record.state != "unknown":
            if record.state in {"failed", "succeeded"}:
                return self._status(record)
            raise MaterialExecutionSubmissionRetryRejected(
                f"only unknown submissions can be explicitly closed: {record.state}"
            )
        if error is None:
            error = MaterialExecutionSubmissionErrorMetadata(
                code="manual_unknown_close",
                detail="operator explicitly closed an unresolved material submission",
                retryable=False,
                recovery_required=True,
            )
        if type(error) is not MaterialExecutionSubmissionErrorMetadata:
            raise TypeError("error must be MaterialExecutionSubmissionErrorMetadata")
        if error.retryable or not error.recovery_required:
            raise ValueError(
                "manual unknown close error must be non-retryable and recovery-required"
            )
        try:
            closed = self.store.compare_and_swap(
                submission_id,
                expected_state="unknown",
                next_state="failed",
                owner_instance_id=self.owner_instance_id,
                owner_epoch=self.owner_epoch,
                owner_capability=self._owner_capability,
                error=error,
            )
        except MaterialExecutionSubmissionStateConflict:
            return self._status_required(submission_id)
        return self._status(closed)

    # Explicit naming for route adapters that expose an operator action.
    resolve_unknown = close_unknown

    def restart_sweep(self) -> tuple[MaterialExecutionSubmissionStatus, ...]:
        """Return rows fenced during mutex-backed owner activation.

        Fencing already happened atomically before this coordinator accepted a
        request.  There is intentionally no UUID-only or timeout-based sweep
        method: a caller cannot manufacture process-death evidence.
        """

        return tuple(self._status(record) for record in self._fenced_at_start)

    # Descriptive aliases for lifecycle/maintenance callers.
    def sweep_restart_unknown(self) -> tuple[MaterialExecutionSubmissionStatus, ...]:
        return self.restart_sweep()

    def mark_inflight_restart_unknown(self) -> tuple[MaterialExecutionSubmissionStatus, ...]:
        return self.restart_sweep()

    def restart_sweep_for_owner(self) -> tuple[MaterialExecutionSubmissionStatus, ...]:
        """Compatibility spelling for the proof-backed activation sweep."""

        return self.restart_sweep()

    def close(self, *, timeout: float = 5.0) -> None:
        """Close process-local owner lanes during application shutdown.

        The registry remains the owner of lane lifetime; this explicit seam
        lets a future application lifespan hook close it without exposing a
        registry or native lane handle through the status API.
        """

        close_all = getattr(self.registry, "close_all", None)
        if not callable(close_all):
            raise TypeError("registry does not expose close_all")
        if self._closed:
            return
        close_all(timeout=timeout)
        self.owner_lease.close(timeout=timeout)
        self.owner_lease.unbind_coordinator(self)
        self._closed = True

    shutdown = close

    @staticmethod
    def _validate_future(value: Any) -> None:
        if not isinstance(value, Future):
            raise TypeError("owner lane must return concurrent.futures.Future")
        if not callable(getattr(value, "add_done_callback", None)):
            raise TypeError("owner lane returned an unsupported Future")
        if not callable(getattr(value, "done", None)) or not callable(
            getattr(value, "result", None)
        ):
            raise TypeError("owner lane Future has an incomplete interface")

    @staticmethod
    def _status(record: MaterialExecutionSubmissionRecord) -> MaterialExecutionSubmissionStatus:
        return MaterialExecutionSubmissionStatus.from_record(record)

    def _status_required(self, submission_id: str) -> MaterialExecutionSubmissionStatus:
        record = self.store.get(submission_id)
        if record is None:
            raise MaterialExecutionSubmissionNotFound(submission_id)
        return self._status(record)

    def _on_execution_done(
        self,
        submission_id: str,
        future: Future[MaterialExecutionResponse],
        *,
        reconcile: bool,
    ) -> None:
        """Persist exactly one completion outcome from a finished Future."""

        try:
            # ``done()`` is checked before ``result`` so this callback can
            # never block a request thread, even with a custom Future.
            if not future.done():  # pragma: no cover - concurrent.futures contract
                raise RuntimeError("Future callback ran before Future completion")
            try:
                response = future.result()
            except BaseException as exc:
                error = _error_metadata(
                    (
                        "submission_reconcile_failed"
                        if reconcile
                        else (
                            "submission_future_unknown"
                            if isinstance(exc, (MaterialExecutionLaneUnknown, CancelledError))
                            else "submission_future_failed"
                        )
                    ),
                    exc,
                    recovery_required=(
                        reconcile
                        or isinstance(exc, (MaterialExecutionLaneUnknown, CancelledError))
                    ),
                )
                if reconcile:
                    record = self.store.get(submission_id)
                    if record is not None and record.recovery_ticket is not None:
                        self._persist_reconcile_blocked(
                            submission_id,
                            error,
                            record.recovery_ticket,
                        )
                    return
                if isinstance(exc, (MaterialExecutionLaneUnknown, CancelledError)):
                    self._persist_unknown(submission_id, error)
                else:
                    self._persist_initial_error(submission_id, error)
                return

            if type(response) is not MaterialExecutionResponse:
                error = _error_metadata(
                    "submission_invalid_response",
                    TypeError("owner lane returned an unsupported response"),
                    recovery_required=reconcile,
                )
                if reconcile:
                    self._persist_unknown(submission_id, error)
                else:
                    self._persist_unknown(submission_id, error)
                return

            try:
                if reconcile:
                    self._persist_reconcile_response(submission_id, response)
                else:
                    self._persist_initial_response(submission_id, response)
            except (
                MaterialExecutionSubmissionIdentityConflict,
                MaterialExecutionSubmissionTransitionError,
                TypeError,
                ValueError,
            ) as exc:
                # A malformed typed response is still completion evidence of
                # an unsuccessful boundary.  Convert it once to bounded error
                # metadata; never mutate a terminal row with a second payload.
                error = _error_metadata(
                    "submission_invalid_response",
                    exc,
                    recovery_required=reconcile,
                )
                if reconcile:
                    self._persist_unknown(submission_id, error)
                else:
                    self._persist_unknown(submission_id, error)
        except BaseException:
            # Callback exceptions must not escape into Future's callback
            # machinery.  Durable store failures remain visible through status
            # reads/restart sweep rather than causing a second enqueue.
            try:
                self._persist_unknown(
                    submission_id,
                    _error_metadata(
                        "submission_callback_unknown",
                        RuntimeError("completion callback failed before durable evidence"),
                        recovery_required=True,
                    ),
                )
            except BaseException:
                return

    def _persist_initial_response(
        self,
        submission_id: str,
        response: MaterialExecutionResponse,
    ) -> MaterialExecutionSubmissionRecord:
        if response.status == "succeeded":
            next_state: SubmissionState = "succeeded"
        elif response.status in {"failed", "terminated"}:
            next_state = "failed"
        elif response.status == "blocked":
            next_state = "blocked"
        else:  # pragma: no cover - response DTO validates status
            raise ValueError("response status is not terminal")
        return self._cas_completion(
            submission_id,
            allowed=("accepted", "dispatching", "running"),
            next_state=next_state,
            response=response,
            recovery_ticket=response.recovery_ticket,
        )

    def _persist_initial_error(
        self,
        submission_id: str,
        error: MaterialExecutionSubmissionErrorMetadata,
    ) -> MaterialExecutionSubmissionRecord:
        return self._cas_completion(
            submission_id,
            allowed=("accepted", "dispatching", "running"),
            next_state="failed",
            error=error,
        )

    def _persist_reconcile_response(
        self,
        submission_id: str,
        response: MaterialExecutionResponse,
    ) -> MaterialExecutionSubmissionRecord:
        if response.status == "succeeded":
            next_state: SubmissionState = "succeeded"
        elif response.status in {"failed", "terminated"}:
            next_state = "failed"
        elif response.status == "blocked":
            next_state = "blocked"
        else:
            raise MaterialExecutionSubmissionTransitionError(
                "reconcile response is not terminal"
            )
        return self._cas_completion(
            submission_id,
            allowed=("reconciling",),
            next_state=next_state,
            response=response,
            recovery_ticket=response.recovery_ticket,
        )

    def _persist_reconcile_blocked(
        self,
        submission_id: str,
        error: MaterialExecutionSubmissionErrorMetadata,
        ticket: MaterialRecoveryTicket,
    ) -> MaterialExecutionSubmissionRecord:
        return self._cas_completion(
            submission_id,
            allowed=("reconciling",),
            next_state="blocked",
            error=error,
            recovery_ticket=ticket,
        )

    def _persist_unknown(
        self,
        submission_id: str,
        error: MaterialExecutionSubmissionErrorMetadata,
    ) -> MaterialExecutionSubmissionRecord:
        return self._cas_completion(
            submission_id,
            allowed=("dispatching", "running", "reconciling"),
            next_state="unknown",
            error=error,
        )

    def _cas_completion(
        self,
        submission_id: str,
        *,
        allowed: Sequence[SubmissionState],
        next_state: SubmissionState,
        response: MaterialExecutionResponse | None = None,
        error: MaterialExecutionSubmissionErrorMetadata | None = None,
        recovery_ticket: MaterialRecoveryTicket | None = None,
    ) -> MaterialExecutionSubmissionRecord:
        """CAS a completion, retrying only the short state-race window."""

        allowed_states = set(allowed)
        # At most accepted/dispatching/running (or reconciling) can be crossed
        # before one terminal writer wins.  Re-reading after each conflict
        # keeps a callback reliable when the running CAS races it.
        for _attempt in range(8):
            current = self.store.get(submission_id)
            if current is None:
                raise MaterialExecutionSubmissionNotFound(submission_id)
            if current.state not in allowed_states:
                return current
            try:
                return self.store.compare_and_swap(
                    submission_id,
                    expected_state=current.state,
                    next_state=next_state,
                    owner_instance_id=self.owner_instance_id,
                    owner_epoch=self.owner_epoch,
                    owner_capability=self._owner_capability,
                    response=response,
                    error=error,
                    recovery_ticket=recovery_ticket,
                )
            except MaterialExecutionSubmissionStateConflict:
                continue
        return self._record_required(submission_id)

    def _record_required(self, submission_id: str) -> MaterialExecutionSubmissionRecord:
        record = self.store.get(submission_id)
        if record is None:
            raise MaterialExecutionSubmissionNotFound(submission_id)
        return record


__all__ = [
    "MAX_ERROR_DETAIL_CHARS",
    "MaterialExecutionSubmissionCoordinator",
    "MaterialExecutionSubmissionCoordinatorError",
    "MaterialExecutionSubmissionReconcileRejected",
    "MaterialExecutionSubmissionRetryRejected",
    "MaterialExecutionSubmissionStatus",
]
