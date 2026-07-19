"""Permanent owner-thread lane for production material execution.

Windows named mutexes in the material pipeline are owned by the thread that
acquired them.  A normal FastAPI worker thread is not a durable owner: a later
recovery request may run on another worker.  This module provides the small
application boundary that keeps execute and reconcile work on one permanent
thread per physical material step.  It deliberately does not switch the
legacy public endpoint.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
from queue import Queue
import re
import threading
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence
from uuid import UUID, uuid4

from app.pipeline.material_execution_coordinator import MaterialCompletionReceipt
from app.pipeline.material_operation_protocol import MaterialOperationKind


_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_OPERATIONS = frozenset(
    {
        "official_capture",
        "pexels",
        "pixabay",
        "coverr",
        "info_motion",
        "stock_candidates",
    }
)
_STATUSES = frozenset({"succeeded", "failed", "terminated", "blocked"})


class MaterialExecutionLaneError(RuntimeError):
    """Base class for owner-lane failures."""


class MaterialExecutionLaneBusy(MaterialExecutionLaneError):
    """The lane still owns work or recovery evidence and cannot close."""


class MaterialExecutionLaneClosed(MaterialExecutionLaneError):
    """The lane no longer accepts commands."""


class MaterialExecutionLaneUnknown(MaterialExecutionLaneError):
    """A handler edge failed while ownership may still be retained."""


@dataclass(frozen=True, slots=True)
class MaterialExecutionLaneKey:
    database_path: Path
    pipeline_id: int
    material_step_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.database_path, Path):
            raise TypeError("database_path must be a Path")
        if not self.database_path.is_absolute():
            raise ValueError("database_path must be absolute")
        resolved = self.database_path.resolve(strict=False)
        if resolved.drive.upper() != "D:":
            raise ValueError("material owner lane database must remain on D:")
        for value, label in (
            (self.pipeline_id, "pipeline_id"),
            (self.material_step_id, "material_step_id"),
        ):
            _validate_positive_int(value, label)
        object.__setattr__(self, "database_path", resolved)

    @property
    def lane_id(self) -> str:
        identity = (
            f"{str(self.database_path).casefold()}|pipeline:{self.pipeline_id}"
            f"|step:{self.material_step_id}"
        ).encode("utf-8")
        return hashlib.sha256(identity).hexdigest()


@dataclass(frozen=True, slots=True)
class MaterialExecutionCommand:
    """Handle-free input accepted from a request thread."""

    pipeline_id: int
    material_step_id: int
    material_task_id: str
    execution_id: str
    operation: MaterialOperationKind
    startup_timeout_seconds: float
    operation_timeout_seconds: float

    def __post_init__(self) -> None:
        _validate_positive_int(self.pipeline_id, "pipeline_id")
        _validate_positive_int(self.material_step_id, "material_step_id")
        _validate_task_id(self.material_task_id)
        _validate_uuid(self.execution_id, "execution_id")
        _validate_operation(self.operation)
        _validate_timeout(self.startup_timeout_seconds, "startup_timeout_seconds")
        _validate_timeout(self.operation_timeout_seconds, "operation_timeout_seconds")


@dataclass(frozen=True, slots=True)
class MaterialRestartRecoveryCommand:
    """Handle-free proof selector for one abandoned published attempt.

    This command cannot launch a provider or create a new attempt.  It carries
    only the exact owner identities and whole-payload fence discovered from a
    fresh server-side database scan.
    """

    pipeline_id: int
    material_step_id: int
    material_task_id: str
    execution_id: str
    attempt_id: str
    operation: MaterialOperationKind
    control_id: str
    job_instance_id: str
    expected_revision: int
    expected_payload_sha256: str
    startup_timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        _validate_positive_int(self.pipeline_id, "pipeline_id")
        _validate_positive_int(self.material_step_id, "material_step_id")
        _validate_task_id(self.material_task_id)
        for value, label in (
            (self.execution_id, "execution_id"),
            (self.attempt_id, "attempt_id"),
            (self.control_id, "control_id"),
            (self.job_instance_id, "job_instance_id"),
        ):
            _validate_uuid(value, label)
        if len(
            {
                self.execution_id,
                self.attempt_id,
                self.control_id,
                self.job_instance_id,
            }
        ) != 4:
            raise ValueError("restart recovery owner identities must be independent")
        _validate_operation(self.operation)
        _validate_positive_int(self.expected_revision, "expected_revision")
        _validate_sha256(self.expected_payload_sha256, "expected_payload_sha256")
        _validate_timeout(self.startup_timeout_seconds, "startup_timeout_seconds")


@dataclass(frozen=True, slots=True)
class MaterialRunnerRetirementRecoveryCommand:
    """Handle-free selector for one exact post-completion retirement checkpoint."""

    pipeline_id: int
    material_step_id: int
    material_task_id: str
    execution_id: str
    attempt_id: str
    operation: MaterialOperationKind
    control_id: str
    job_instance_id: str
    completion_status: Literal["succeeded", "failed", "terminated"]
    completion_revision: int
    checkpoint_sha256: str

    def __post_init__(self) -> None:
        _validate_positive_int(self.pipeline_id, "pipeline_id")
        _validate_positive_int(self.material_step_id, "material_step_id")
        _validate_task_id(self.material_task_id)
        for value, label in (
            (self.execution_id, "execution_id"),
            (self.attempt_id, "attempt_id"),
            (self.control_id, "control_id"),
            (self.job_instance_id, "job_instance_id"),
        ):
            _validate_uuid(value, label)
        if len(
            {
                self.execution_id,
                self.attempt_id,
                self.control_id,
                self.job_instance_id,
            }
        ) != 4:
            raise ValueError("runner retirement owner identities must be independent")
        _validate_operation(self.operation)
        if self.completion_status not in {"succeeded", "failed", "terminated"}:
            raise ValueError("runner retirement completion status is not allowed")
        _validate_positive_int(self.completion_revision, "completion_revision")
        _validate_sha256(self.checkpoint_sha256, "checkpoint_sha256")


@dataclass(frozen=True, slots=True)
class MaterialRecoveryTicket:
    """Opaque, handle-free token that routes recovery back to its owner lane."""

    ticket_id: str
    lane_id: str
    pipeline_id: int
    material_step_id: int
    material_task_id: str
    execution_id: str
    attempt_id: str
    operation: MaterialOperationKind

    def __post_init__(self) -> None:
        _validate_uuid(self.ticket_id, "ticket_id")
        _validate_sha256(self.lane_id, "lane_id")
        _validate_positive_int(self.pipeline_id, "pipeline_id")
        _validate_positive_int(self.material_step_id, "material_step_id")
        _validate_task_id(self.material_task_id)
        _validate_uuid(self.execution_id, "execution_id")
        _validate_uuid(self.attempt_id, "attempt_id")
        _validate_operation(self.operation)


@dataclass(frozen=True, slots=True)
class MaterialExecutionResponse:
    """JSON-safe result returned to a request thread.

    Native mutexes, leases, prepared attempts, publisher pins and recovery
    state are intentionally not fields of this DTO.
    """

    pipeline_id: int
    material_step_id: int
    material_task_id: str
    execution_id: str
    attempt_id: str
    operation: MaterialOperationKind
    status: Literal["succeeded", "failed", "terminated", "blocked"]
    message: str
    executed_task: Mapping[str, Any] | None
    tasks: tuple[Mapping[str, Any], ...]
    completion: MaterialCompletionReceipt | None = None
    recovery_ticket: MaterialRecoveryTicket | None = None

    def __post_init__(self) -> None:
        _validate_positive_int(self.pipeline_id, "pipeline_id")
        _validate_positive_int(self.material_step_id, "material_step_id")
        _validate_task_id(self.material_task_id)
        _validate_uuid(self.execution_id, "execution_id")
        _validate_uuid(self.attempt_id, "attempt_id")
        _validate_operation(self.operation)
        if self.status not in _STATUSES:
            raise ValueError("material execution response status is not allowed")
        if type(self.message) is not str or len(self.message) > 2000:
            raise ValueError("material execution response message is invalid")
        executed_task = (
            None if self.executed_task is None else _json_mapping(self.executed_task)
        )
        if type(self.tasks) is not tuple:
            raise TypeError("tasks must be a tuple")
        tasks = tuple(_json_mapping(item) for item in self.tasks)
        if self.completion is not None and type(self.completion) is not MaterialCompletionReceipt:
            raise TypeError("completion must be MaterialCompletionReceipt")
        if self.recovery_ticket is not None:
            if type(self.recovery_ticket) is not MaterialRecoveryTicket:
                raise TypeError("recovery_ticket must be MaterialRecoveryTicket")
            ticket = self.recovery_ticket
            if (
                ticket.pipeline_id != self.pipeline_id
                or ticket.material_step_id != self.material_step_id
                or ticket.material_task_id != self.material_task_id
                or ticket.execution_id != self.execution_id
                or ticket.attempt_id != self.attempt_id
                or ticket.operation != self.operation
            ):
                raise ValueError("recovery ticket does not match the response")
        object.__setattr__(self, "executed_task", executed_task)
        object.__setattr__(self, "tasks", tasks)


@dataclass(frozen=True, slots=True)
class MaterialExecutionRetainedOwnership:
    """Internal handler result whose raw state must remain on the owner lane."""

    response: MaterialExecutionResponse
    state: object = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.response) is not MaterialExecutionResponse:
            raise TypeError("response must be MaterialExecutionResponse")
        if self.response.recovery_ticket is not None:
            raise ValueError("handler cannot choose its own recovery ticket")
        if self.state is None:
            raise ValueError("retained ownership state cannot be None")


class MaterialExecutionLaneHandler(Protocol):
    def execute(
        self,
        command: MaterialExecutionCommand,
    ) -> MaterialExecutionResponse | MaterialExecutionRetainedOwnership:
        ...

    def reconcile(
        self,
        state: object,
        ticket: MaterialRecoveryTicket,
    ) -> MaterialExecutionResponse | MaterialExecutionRetainedOwnership:
        ...


class MaterialExecutionSourceChainHandler(Protocol):
    """Optional owner-lane surface for explicit same-execution fallback."""

    def execute_source_chain(
        self,
        command: MaterialExecutionCommand,
        *,
        source_chain: Sequence[MaterialOperationKind],
    ) -> MaterialExecutionResponse | MaterialExecutionRetainedOwnership:
        ...


class MaterialRestartRecoveryHandler(Protocol):
    """Optional owner-lane surface for restart-only publication recovery."""

    def recover_restart_published(
        self,
        command: MaterialRestartRecoveryCommand,
    ) -> MaterialExecutionResponse | MaterialExecutionRetainedOwnership:
        ...


class MaterialRunnerRetirementRecoveryHandler(Protocol):
    """Optional owner-lane surface for post-completion runner retirement."""

    def recover_runner_retirement(
        self,
        command: MaterialRunnerRetirementRecoveryCommand,
    ) -> MaterialExecutionResponse | MaterialExecutionRetainedOwnership:
        ...


@dataclass(slots=True)
class _ExecuteWork:
    command: MaterialExecutionCommand
    future: Future[MaterialExecutionResponse]


@dataclass(slots=True)
class _ExecuteSourceChainWork:
    command: MaterialExecutionCommand
    source_chain: tuple[MaterialOperationKind, ...]
    future: Future[MaterialExecutionResponse]


@dataclass(slots=True)
class _ReconcileWork:
    ticket: MaterialRecoveryTicket
    future: Future[MaterialExecutionResponse]


@dataclass(slots=True)
class _RestartRecoveryWork:
    command: MaterialRestartRecoveryCommand
    future: Future[MaterialExecutionResponse]


@dataclass(slots=True)
class _RunnerRetirementRecoveryWork:
    command: MaterialRunnerRetirementRecoveryCommand
    future: Future[MaterialExecutionResponse]


@dataclass(slots=True)
class _StopWork:
    future: Future[None]


@dataclass(slots=True)
class _RecoveryEntry:
    ticket: MaterialRecoveryTicket
    state: object


class MaterialExecutionOwnerLane:
    """Serialize one physical material step on a permanent owner thread."""

    def __init__(
        self,
        *,
        key: MaterialExecutionLaneKey,
        handler: MaterialExecutionLaneHandler,
    ) -> None:
        if type(key) is not MaterialExecutionLaneKey:
            raise TypeError("key must be MaterialExecutionLaneKey")
        if not callable(getattr(handler, "execute", None)) or not callable(
            getattr(handler, "reconcile", None)
        ):
            raise TypeError("handler must expose execute and reconcile")
        self.key = key
        self.handler = handler
        self._queue: Queue[
            _ExecuteWork
            | _ExecuteSourceChainWork
            | _ReconcileWork
            | _RestartRecoveryWork
            | _RunnerRetirementRecoveryWork
            | _StopWork
        ] = Queue()
        self._state_lock = threading.Lock()
        self._recoveries: dict[str, _RecoveryEntry] = {}
        self._pending_count = 0
        self._accepting = True
        self._closed = False
        self._owner_thread_ident: int | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"material-owner-{key.lane_id[:12]}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise MaterialExecutionLaneUnknown("material owner lane did not start")

    @property
    def owner_thread_ident(self) -> int:
        ident = self._owner_thread_ident
        if ident is None:
            raise MaterialExecutionLaneUnknown("material owner lane has no thread identity")
        return ident

    @property
    def retained_recovery_count(self) -> int:
        with self._state_lock:
            return len(self._recoveries)

    def submit(
        self,
        command: MaterialExecutionCommand,
    ) -> Future[MaterialExecutionResponse]:
        if type(command) is not MaterialExecutionCommand:
            raise TypeError("command must be MaterialExecutionCommand")
        if (
            command.pipeline_id != self.key.pipeline_id
            or command.material_step_id != self.key.material_step_id
        ):
            raise ValueError("command belongs to another material step")
        future: Future[MaterialExecutionResponse] = Future()
        self._enqueue(_ExecuteWork(command=command, future=future))
        return future

    def submit_source_chain(
        self,
        command: MaterialExecutionCommand,
        *,
        source_chain: Sequence[MaterialOperationKind],
    ) -> Future[MaterialExecutionResponse]:
        """Queue an explicit fallback chain on this lane's permanent thread."""

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
        for operation in chain:
            _validate_operation(operation)
        execute_source_chain = getattr(self.handler, "execute_source_chain", None)
        if not callable(execute_source_chain):
            raise TypeError("handler does not expose execute_source_chain")
        future: Future[MaterialExecutionResponse] = Future()
        self._enqueue(
            _ExecuteSourceChainWork(
                command=command,
                source_chain=chain,
                future=future,
            )
        )
        return future

    def reconcile(
        self,
        ticket: MaterialRecoveryTicket | None,
    ) -> Future[MaterialExecutionResponse]:
        if type(ticket) is not MaterialRecoveryTicket:
            raise TypeError("ticket must be MaterialRecoveryTicket")
        if (
            ticket.lane_id != self.key.lane_id
            or ticket.pipeline_id != self.key.pipeline_id
            or ticket.material_step_id != self.key.material_step_id
        ):
            raise ValueError("recovery ticket belongs to another material step")
        future: Future[MaterialExecutionResponse] = Future()
        self._enqueue(_ReconcileWork(ticket=ticket, future=future))
        return future

    def recover_restart_published(
        self,
        command: MaterialRestartRecoveryCommand,
    ) -> Future[MaterialExecutionResponse]:
        """Queue one proof-bound restart recovery on this lane's owner thread."""

        if type(command) is not MaterialRestartRecoveryCommand:
            raise TypeError("command must be MaterialRestartRecoveryCommand")
        if (
            command.pipeline_id != self.key.pipeline_id
            or command.material_step_id != self.key.material_step_id
        ):
            raise ValueError("restart recovery belongs to another material step")
        recover = getattr(self.handler, "recover_restart_published", None)
        if not callable(recover):
            raise TypeError("handler does not expose recover_restart_published")
        future: Future[MaterialExecutionResponse] = Future()
        self._enqueue(_RestartRecoveryWork(command=command, future=future))
        return future

    def recover_runner_retirement(
        self,
        command: MaterialRunnerRetirementRecoveryCommand,
    ) -> Future[MaterialExecutionResponse]:
        """Queue one checkpoint-bound retirement on this lane's owner thread."""

        if type(command) is not MaterialRunnerRetirementRecoveryCommand:
            raise TypeError("command must be MaterialRunnerRetirementRecoveryCommand")
        if (
            command.pipeline_id != self.key.pipeline_id
            or command.material_step_id != self.key.material_step_id
        ):
            raise ValueError("runner retirement belongs to another material step")
        recover = getattr(self.handler, "recover_runner_retirement", None)
        if not callable(recover):
            raise TypeError("handler does not expose recover_runner_retirement")
        future: Future[MaterialExecutionResponse] = Future()
        self._enqueue(_RunnerRetirementRecoveryWork(command=command, future=future))
        return future

    def close(self, *, timeout: float = 5.0) -> None:
        _validate_timeout(timeout, "timeout")
        with self._state_lock:
            if self._closed:
                return
            if self._recoveries:
                raise MaterialExecutionLaneBusy(
                    "material owner lane still has retained recovery ownership"
                )
            if self._pending_count:
                raise MaterialExecutionLaneBusy(
                    "material owner lane still has pending commands"
                )
            self._accepting = False
        future: Future[None] = Future()
        self._queue.put(_StopWork(future=future))
        try:
            future.result(timeout=timeout)
        except BaseException:
            # A handler may discover a late ownership problem while its
            # owner-thread close hook runs.  Keep the lane alive so an
            # explicit recovery/close attempt can be made on that same lane;
            # never mark it closed merely because the stop acknowledgement
            # was uncertain.
            with self._state_lock:
                if not self._closed and self._thread.is_alive():
                    self._accepting = True
            raise
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            raise MaterialExecutionLaneUnknown("material owner lane did not stop")
        with self._state_lock:
            self._closed = True

    def _enqueue(
        self,
        work: _ExecuteWork
        | _ExecuteSourceChainWork
        | _ReconcileWork
        | _RestartRecoveryWork
        | _RunnerRetirementRecoveryWork,
    ) -> None:
        with self._state_lock:
            if not self._accepting or self._closed:
                raise MaterialExecutionLaneClosed("material owner lane is closed")
            self._pending_count += 1
        self._queue.put(work)

    def _run(self) -> None:
        self._owner_thread_ident = threading.get_ident()
        self._ready.set()
        while True:
            work = self._queue.get()
            if isinstance(work, _StopWork):
                try:
                    close = getattr(self.handler, "close", None)
                    if callable(close):
                        close()
                except BaseException as exc:
                    work.future.set_exception(
                        MaterialExecutionLaneUnknown(
                            "material owner handler close acknowledgement is unknown"
                        )
                    )
                    continue
                work.future.set_result(None)
                return
            try:
                if isinstance(work, _ExecuteWork):
                    self._execute(work)
                elif isinstance(work, _ExecuteSourceChainWork):
                    self._execute_source_chain(work)
                elif isinstance(work, _RestartRecoveryWork):
                    self._recover_restart_published(work)
                elif isinstance(work, _RunnerRetirementRecoveryWork):
                    self._recover_runner_retirement(work)
                else:
                    self._reconcile(work)
            finally:
                with self._state_lock:
                    self._pending_count -= 1

    def _execute(self, work: _ExecuteWork) -> None:
        with self._state_lock:
            if self._recoveries:
                # A retained claim/mutex/pin is still owned by this lane.  A
                # second execute command cannot safely start or rotate an
                # attempt until the opaque recovery ticket has been resolved.
                work.future.set_exception(
                    MaterialExecutionLaneBusy(
                        "material owner lane retains recovery ownership; reconcile it first"
                    )
                )
                return
        try:
            result = self.handler.execute(work.command)
            if type(result) is MaterialExecutionRetainedOwnership:
                response = self._retain_new(result)
            elif type(result) is MaterialExecutionResponse:
                if result.recovery_ticket is not None:
                    raise TypeError("handler returned an externally ticketed response")
                response = result
            else:
                raise TypeError("handler returned an unsupported execution result")
        except BaseException:
            work.future.set_exception(
                MaterialExecutionLaneUnknown(
                    "material execution failed inside its owner lane"
                )
            )
            return
        work.future.set_result(response)

    def _execute_source_chain(self, work: _ExecuteSourceChainWork) -> None:
        with self._state_lock:
            if self._recoveries:
                work.future.set_exception(
                    MaterialExecutionLaneBusy(
                        "material owner lane retains recovery ownership; reconcile it first"
                    )
                )
                return
        execute_source_chain = getattr(self.handler, "execute_source_chain", None)
        if not callable(execute_source_chain):
            work.future.set_exception(
                MaterialExecutionLaneUnknown(
                    "material owner handler has no source-chain surface"
                )
            )
            return
        try:
            result = execute_source_chain(
                work.command,
                source_chain=work.source_chain,
            )
            if type(result) is MaterialExecutionRetainedOwnership:
                response = self._retain_new(result)
            elif type(result) is MaterialExecutionResponse:
                if result.recovery_ticket is not None:
                    raise TypeError("handler returned an externally ticketed response")
                response = result
            else:
                raise TypeError("handler returned an unsupported execution result")
        except BaseException:
            work.future.set_exception(
                MaterialExecutionLaneUnknown(
                    "material source chain failed inside its owner lane"
                )
            )
            return
        work.future.set_result(response)

    def _reconcile(self, work: _ReconcileWork) -> None:
        with self._state_lock:
            entry = self._recoveries.get(work.ticket.ticket_id)
        if entry is None or entry.ticket != work.ticket:
            work.future.set_exception(
                MaterialExecutionLaneError("recovery ticket is unknown or already consumed")
            )
            return
        try:
            result = self.handler.reconcile(entry.state, entry.ticket)
            if type(result) is MaterialExecutionRetainedOwnership:
                entry.state = result.state
                response = replace(result.response, recovery_ticket=entry.ticket)
            elif type(result) is MaterialExecutionResponse:
                if result.recovery_ticket is not None:
                    raise TypeError("handler returned an externally ticketed response")
                with self._state_lock:
                    removed = self._recoveries.pop(entry.ticket.ticket_id, None)
                if removed is not entry:
                    raise MaterialExecutionLaneUnknown(
                        "recovery ownership registry changed unexpectedly"
                    )
                response = result
            else:
                raise TypeError("handler returned an unsupported recovery result")
        except BaseException:
            # The raw state remains registered on this same live thread.  The
            # request thread receives no lease/mutex/pin object and can retry
            # only by presenting the same opaque ticket.
            work.future.set_exception(
                MaterialExecutionLaneUnknown(
                    "material recovery failed inside its owner lane; ownership is retained"
                )
            )
            return
        work.future.set_result(response)

    def _recover_restart_published(self, work: _RestartRecoveryWork) -> None:
        with self._state_lock:
            if self._recoveries:
                work.future.set_exception(
                    MaterialExecutionLaneBusy(
                        "material owner lane retains recovery ownership; reconcile it first"
                    )
                )
                return
        recover = getattr(self.handler, "recover_restart_published", None)
        if not callable(recover):
            work.future.set_exception(
                MaterialExecutionLaneUnknown(
                    "material owner handler has no restart recovery surface"
                )
            )
            return
        try:
            result = recover(work.command)
            if type(result) is MaterialExecutionRetainedOwnership:
                response = self._retain_new(result)
            elif type(result) is MaterialExecutionResponse:
                if result.recovery_ticket is not None:
                    raise TypeError("handler returned an externally ticketed response")
                response = result
            else:
                raise TypeError("handler returned an unsupported restart recovery result")
        except BaseException:
            work.future.set_exception(
                MaterialExecutionLaneUnknown(
                    "material restart recovery failed inside its owner lane"
                )
            )
            return
        work.future.set_result(response)

    def _recover_runner_retirement(
        self,
        work: _RunnerRetirementRecoveryWork,
    ) -> None:
        with self._state_lock:
            if self._recoveries:
                work.future.set_exception(
                    MaterialExecutionLaneBusy(
                        "material owner lane retains recovery ownership; reconcile it first"
                    )
                )
                return
        recover = getattr(self.handler, "recover_runner_retirement", None)
        if not callable(recover):
            work.future.set_exception(
                MaterialExecutionLaneUnknown(
                    "material owner handler has no runner retirement surface"
                )
            )
            return
        try:
            result = recover(work.command)
            if type(result) is MaterialExecutionRetainedOwnership:
                response = self._retain_new(result)
            elif type(result) is MaterialExecutionResponse:
                if result.recovery_ticket is not None:
                    raise TypeError("handler returned an externally ticketed response")
                response = result
            else:
                raise TypeError("handler returned an unsupported runner retirement result")
        except BaseException:
            work.future.set_exception(
                MaterialExecutionLaneUnknown(
                    "material runner retirement failed inside its owner lane"
                )
            )
            return
        work.future.set_result(response)

    def _retain_new(
        self,
        retained: MaterialExecutionRetainedOwnership,
    ) -> MaterialExecutionResponse:
        response = retained.response
        ticket = MaterialRecoveryTicket(
            ticket_id=str(uuid4()),
            lane_id=self.key.lane_id,
            pipeline_id=response.pipeline_id,
            material_step_id=response.material_step_id,
            material_task_id=response.material_task_id,
            execution_id=response.execution_id,
            attempt_id=response.attempt_id,
            operation=response.operation,
        )
        entry = _RecoveryEntry(ticket=ticket, state=retained.state)
        with self._state_lock:
            if ticket.ticket_id in self._recoveries:
                raise MaterialExecutionLaneUnknown("recovery ticket collision")
            self._recoveries[ticket.ticket_id] = entry
        return replace(response, recovery_ticket=ticket)


class MaterialExecutionOwnerLaneRegistry:
    """Process-local registry keyed by physical DB and material step."""

    def __init__(
        self,
        *,
        handler_factory: Callable[[MaterialExecutionLaneKey], MaterialExecutionLaneHandler],
    ) -> None:
        if not callable(handler_factory):
            raise TypeError("handler_factory must be callable")
        self.handler_factory = handler_factory
        self._lock = threading.Lock()
        self._lanes: dict[str, MaterialExecutionOwnerLane] = {}

    def get_or_create(self, key: MaterialExecutionLaneKey) -> MaterialExecutionOwnerLane:
        if type(key) is not MaterialExecutionLaneKey:
            raise TypeError("key must be MaterialExecutionLaneKey")
        with self._lock:
            lane = self._lanes.get(key.lane_id)
            if lane is not None:
                return lane
            lane = MaterialExecutionOwnerLane(
                key=key,
                handler=self.handler_factory(key),
            )
            self._lanes[key.lane_id] = lane
            return lane

    def close_all(self, *, timeout: float = 5.0) -> None:
        _validate_timeout(timeout, "timeout")
        with self._lock:
            lanes = tuple(self._lanes.items())
        errors: list[BaseException] = []
        for lane_id, lane in lanes:
            try:
                lane.close(timeout=timeout)
            except BaseException as exc:
                errors.append(exc)
            else:
                with self._lock:
                    if self._lanes.get(lane_id) is lane:
                        self._lanes.pop(lane_id, None)
        if errors:
            raise MaterialExecutionLaneBusy(
                "one or more material owner lanes still retain ownership"
            ) from errors[0]


def _validate_positive_int(value: object, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _validate_task_id(value: object) -> None:
    if type(value) is not str or _TASK_ID_RE.fullmatch(value) is None:
        raise ValueError("material_task_id is not a safe immutable identifier")


def _validate_uuid(value: object, label: str) -> None:
    if type(value) is not str:
        raise ValueError(f"{label} must be a canonical UUID")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UUID") from exc


def _validate_sha256(value: object, label: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _validate_operation(value: object) -> None:
    if value not in _OPERATIONS:
        raise ValueError("material operation is not allowed")


def _validate_timeout(value: object, label: str) -> None:
    if type(value) not in {int, float} or value <= 0:
        raise ValueError(f"{label} must be positive")


def _json_mapping(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("material response task must be a mapping")
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
        raise TypeError("material response task must contain only JSON values") from exc
    if not isinstance(decoded, dict):
        raise TypeError("material response task must be a JSON object")
    return decoded


__all__ = [
    "MaterialExecutionCommand",
    "MaterialExecutionLaneBusy",
    "MaterialExecutionLaneClosed",
    "MaterialExecutionLaneError",
    "MaterialExecutionLaneHandler",
    "MaterialExecutionLaneKey",
    "MaterialExecutionLaneUnknown",
    "MaterialExecutionSourceChainHandler",
    "MaterialExecutionOwnerLane",
    "MaterialExecutionOwnerLaneRegistry",
    "MaterialExecutionResponse",
    "MaterialExecutionRetainedOwnership",
    "MaterialRestartRecoveryCommand",
    "MaterialRestartRecoveryHandler",
    "MaterialRecoveryTicket",
    "MaterialRunnerRetirementRecoveryCommand",
    "MaterialRunnerRetirementRecoveryHandler",
]
