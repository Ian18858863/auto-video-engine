"""Process-wide owner lease for the durable material submission boundary.

The lease is deliberately narrower than a heartbeat/TTL scheme.  A dedicated
holder thread owns a stable, D-sidecar-scoped Windows named mutex for the
whole application lifetime.  A successor may allocate a new SQLite fencing
epoch only after it has acquired that mutex.  UUIDs, PIDs and elapsed time are
audit fields; none is treated as proof that another process is dead.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import queue
import threading
from typing import Any
from uuid import uuid4

from app.core.windows_named_mutex import (
    WindowsMutexBusy,
    WindowsNamedMutex,
    build_mutex_name,
)
from app.pipeline.material_execution_submission_store import (
    MaterialExecutionOwnerLeaseActivation,
    MaterialExecutionOwnerLeaseCapability,
    MaterialExecutionOwnerToken,
    MaterialExecutionSubmissionStore,
)


class MaterialExecutionOwnerLeaseError(RuntimeError):
    """Base error for the process-wide submission owner lease."""


class MaterialExecutionOwnerLeaseBusy(MaterialExecutionOwnerLeaseError):
    """Another live holder currently owns the sidecar's named mutex."""

    status_code = 409


class MaterialExecutionOwnerLeaseUnavailable(MaterialExecutionOwnerLeaseError):
    """The operating system cannot provide the required lease primitive."""


MAX_OWNER_LEASE_TIMEOUT_SECONDS = 24 * 60 * 60
# A killed Windows process can take a short scheduler turn to release/mark an
# abandoned kernel mutex after ``Popen.wait`` returns.  A bounded grace keeps a
# live second owner a quick 409 while making clean process-death handoff less
# timing-sensitive; it is never used as death evidence.
MUTEX_HANDOFF_GRACE_MS = 250


def _validate_timeout(value: float | int, *, label: str) -> float:
    if (
        type(value) not in {int, float}
        or value <= 0
        or not math.isfinite(float(value))
        or float(value) > MAX_OWNER_LEASE_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"{label} must be positive, finite and no greater than "
            f"{MAX_OWNER_LEASE_TIMEOUT_SECONDS} seconds"
        )
    return float(value)


@dataclass(frozen=True, slots=True)
class _HolderReady:
    mutex: WindowsNamedMutex
    holder_thread_id: int
    was_abandoned: bool
    activation: MaterialExecutionOwnerLeaseActivation


def _owner_mutex_name(store: MaterialExecutionSubmissionStore) -> str:
    # Resolve and normalize once so independent processes hash the same stable
    # physical path.  The sidecar itself already rejects C:, reparse points and
    # lexical traversal before this function is reached.
    physical = os.path.realpath(os.path.abspath(os.fspath(store.path)))
    physical = physical.replace("/", "\\").casefold()
    return build_mutex_name("material-submission-owner", physical)


class MaterialExecutionSubmissionOwnerLease:
    """Hold a named mutex on a dedicated thread and expose only a fencing token."""

    def __init__(
        self,
        *,
        store: MaterialExecutionSubmissionStore,
        activation: MaterialExecutionOwnerLeaseActivation,
        mutex_name: str,
        holder_thread: threading.Thread,
        release_event: threading.Event,
    ) -> None:
        self.store = store
        self.activation = activation
        if type(activation.capability) is not MaterialExecutionOwnerLeaseCapability:
            raise MaterialExecutionOwnerLeaseError(
                "owner activation did not return a live lease capability"
            )
        self._capability = activation.capability
        self.mutex_name = mutex_name
        self._holder_thread = holder_thread
        self._release_event = release_event
        self._lock = threading.RLock()
        self._closed = False
        # One lease owns one coordinator/registry pair.  Without this binding,
        # two in-process coordinators could share a valid epoch while each
        # creates a separate owner-lane registry and executes the same lane
        # concurrently.  The binding is deliberately process-local; the named
        # mutex remains the cross-process exclusion primitive.
        self._bound_coordinator: object | None = None
        self._bound_registry: object | None = None

    @classmethod
    def acquire(
        cls,
        store: MaterialExecutionSubmissionStore,
        *,
        owner_instance_id: str | None = None,
        startup_timeout: float = 10.0,
    ) -> "MaterialExecutionSubmissionOwnerLease":
        if not isinstance(store, MaterialExecutionSubmissionStore):
            raise TypeError("store must be MaterialExecutionSubmissionStore")
        startup_timeout_value = _validate_timeout(
            startup_timeout, label="startup_timeout"
        )
        owner_id = str(uuid4()) if owner_instance_id is None else owner_instance_id
        mutex_name = _owner_mutex_name(store)
        ready: queue.Queue[object] = queue.Queue(maxsize=1)
        release_event = threading.Event()

        def hold_mutex() -> None:
            mutex: WindowsNamedMutex | None = None
            handed_off = False
            try:
                mutex = WindowsNamedMutex.try_acquire(
                    mutex_name,
                    timeout_ms=MUTEX_HANDOFF_GRACE_MS,
                )
                # The activation transaction must be performed by the same
                # thread that owns the Windows mutex.  This keeps the normal
                # path from ever treating a UUID/PID as a substitute for the
                # OS ownership proof.
                mutex.assert_owned()
                activation = store._activate_owner_lease(
                    owner_instance_id=owner_id,
                    mutex_name=mutex_name,
                    was_abandoned=mutex.was_abandoned,
                    process_id=os.getpid(),
                    holder_thread_id=threading.get_native_id(),
                )
                ready.put(
                    _HolderReady(
                        mutex=mutex,
                        holder_thread_id=threading.get_native_id(),
                        was_abandoned=mutex.was_abandoned,
                        activation=activation,
                    )
                )
                handed_off = True
                release_event.wait()
                mutex.release()
            except BaseException as exc:
                try:
                    ready.put(exc)
                except BaseException:
                    pass
                if mutex is not None and not handed_off:
                    try:
                        mutex.release()
                    except BaseException:
                        pass

        holder = threading.Thread(
            target=hold_mutex,
            name="auto-video-engine-submission-owner",
            daemon=True,
        )
        holder.start()
        try:
            result = ready.get(timeout=startup_timeout_value)
        except queue.Empty as exc:
            release_event.set()
            holder.join(timeout=1.0)
            raise MaterialExecutionOwnerLeaseUnavailable(
                "owner mutex holder did not start in time"
            ) from exc
        if isinstance(result, WindowsMutexBusy):
            raise MaterialExecutionOwnerLeaseBusy(
                f"submission owner is already active for {mutex_name}"
            ) from result
        if isinstance(result, BaseException):
            if type(result).__name__ == "WindowsMutexUnsupported":
                raise MaterialExecutionOwnerLeaseUnavailable(
                    "Windows named mutex is unavailable"
                ) from result
            raise MaterialExecutionOwnerLeaseUnavailable(
                "cannot acquire submission owner mutex"
            ) from result
        if not isinstance(result, _HolderReady):  # pragma: no cover - defensive
            release_event.set()
            holder.join(timeout=1.0)
            raise MaterialExecutionOwnerLeaseUnavailable(
                "owner mutex holder returned an invalid result"
            )

        return cls(
            store=store,
            activation=result.activation,
            mutex_name=mutex_name,
            holder_thread=holder,
            release_event=release_event,
        )

    @property
    def token(self) -> MaterialExecutionOwnerToken:
        return self.activation.token

    @property
    def owner_instance_id(self) -> str:
        return self.token.owner_instance_id

    @property
    def owner_epoch(self) -> int:
        return self.token.owner_epoch

    @property
    def capability(self) -> MaterialExecutionOwnerLeaseCapability:
        """Opaque process-local mutation capability; never serialize it."""

        return self._capability

    @property
    def fenced_submissions(self):
        return self.activation.fenced_submissions

    @property
    def was_abandoned(self) -> bool:
        return self.activation.was_abandoned

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def assert_active(self) -> None:
        """Check the holder thread and current DB epoch from any caller thread."""

        with self._lock:
            if self._closed or not self._holder_thread.is_alive():
                raise MaterialExecutionOwnerLeaseError("owner lease is closed")
        self.store.assert_owner_lease(self.token, capability=self._capability)

    def bind_coordinator(self, coordinator: object, registry: object) -> None:
        """Bind this lease to exactly one coordinator/registry pair."""

        if coordinator is None or registry is None:
            raise TypeError("coordinator and registry are required")
        with self._lock:
            if self._closed:
                raise MaterialExecutionOwnerLeaseError("owner lease is closed")
            if self._bound_coordinator is not None and self._bound_coordinator is not coordinator:
                raise MaterialExecutionOwnerLeaseError(
                    "owner lease is already bound to another coordinator"
                )
            if self._bound_registry is not None and self._bound_registry is not registry:
                raise MaterialExecutionOwnerLeaseError(
                    "owner lease is already bound to another lane registry"
                )
            self._bound_coordinator = coordinator
            self._bound_registry = registry

    def unbind_coordinator(self, coordinator: object) -> None:
        """Release the process-local binding after a successful shutdown."""

        with self._lock:
            if self._bound_coordinator is coordinator:
                self._bound_coordinator = None
                self._bound_registry = None

    def close(self, *, timeout: float = 5.0) -> None:
        """Cleanly end the DB lease, then release the mutex on its owner thread."""

        timeout_value = _validate_timeout(timeout, label="timeout")
        with self._lock:
            if self._closed:
                return
            # If this raises because work is still in flight, deliberately keep
            # the holder thread and mutex alive: a later process must not take
            # over a lease whose owner has not shut down cleanly.
            self.store.end_owner_lease(self.token, capability=self._capability)
            self._release_event.set()
            self._holder_thread.join(timeout=timeout_value)
            if self._holder_thread.is_alive():
                raise MaterialExecutionOwnerLeaseError(
                    "owner mutex holder did not stop after clean lease close"
                )
            self.store.release_owner_capability(
                self.token,
                capability=self._capability,
            )
            self._closed = True
            self._bound_coordinator = None
            self._bound_registry = None

    shutdown = close

    def __enter__(self) -> "MaterialExecutionSubmissionOwnerLease":
        self.assert_active()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


__all__ = [
    "MaterialExecutionOwnerLeaseBusy",
    "MaterialExecutionOwnerLeaseError",
    "MaterialExecutionOwnerLeaseUnavailable",
    "MaterialExecutionSubmissionOwnerLease",
]
