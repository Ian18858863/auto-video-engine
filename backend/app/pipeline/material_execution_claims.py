"""Fresh SQLite claim/CAS boundary for material-step execution.

This module is intentionally independent from the legacy material executor.
It provides the small database and mutex seam that the publisher/runner
integration will need later; importing it does not switch any production route
to the new path.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Callable, Iterator, Mapping
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.core.database import get_sqlite_database_path
from app.core.windows_named_mutex import (
    WindowsMutexBusy,
    WindowsNamedMutex,
    build_mutex_name,
)
from app.models.entities import PipelineStep
from app.materials.planner import material_task_id_for_shot
from app.pipeline.material_execution_coordinator import MaterialCompletionReceipt
from app.pipeline.material_operation_runner import MaterialAttemptRetirementReceipt
from app.pipeline.material_runner_retirement import (
    MaterialRunnerRetirementCheckpoint,
    MaterialRunnerRetirementError,
    attested_runner_retirement_from_payload,
    pending_runner_retirement_from_payload,
)


_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_CLAIMABLE_PHASES = frozenset({"idle", "none", "rolled_back", "committed"})
_CLAIMABLE_EXECUTION_STATES = frozenset({"idle", "none"})
_NON_TERMINAL_STATES = frozenset({"claimed", "publishing", "running"})
_CLAIMABLE_TASK_STATUSES = frozenset(
    {
        "等待",
        "待执行",
        "待处理",
        "待搜索",
        "未开始",
        "pending",
        "idle",
        "queued",
        "ready",
    }
)
_ATTEMPT_KINDS = frozenset(
    {
        "official_capture",
        "pexels",
        "pixabay",
        "coverr",
        "info_motion",
        "stock_candidates",
        "empty",
    }
)


class MaterialExecutionClaimError(RuntimeError):
    """Base class for a rejected or unprovable claim operation."""


class MaterialExecutionClaimConflict(MaterialExecutionClaimError):
    """Another execution owns the material step or the state is not retryable."""

    status_code = 409


class MaterialExecutionClaimUnknown(MaterialExecutionClaimError):
    """A database acknowledgement was not safely knowable."""


@dataclass(frozen=True, slots=True)
class MaterialPublishingRecoveryIdentity:
    """Exact owner and receipt fence for one recoverable publishing blob."""

    material_task_id: str
    execution_id: str
    attempt_id: str
    attempt_kind: str
    control_id: str
    job_instance_id: str
    execution_revision: int
    publication_reference_base_revision: int
    publication_journal: str


def publishing_recovery_identity_from_payload(
    payload: Mapping[str, Any],
) -> MaterialPublishingRecoveryIdentity:
    """Parse only the exact ``r+1`` blob accepted by recovery adoption.

    Startup discovery and the final lease adoption share this validator so a
    scan cannot advertise evidence that the owner-lane recovery path would
    reject later merely because their field lists drifted apart.
    """

    current = _clone_json_mapping(payload)
    owner = (
        current.get("active_material_task_id"),
        current.get("active_execution_id"),
        current.get("active_attempt_id"),
        current.get("active_attempt_kind"),
        current.get("active_control_id"),
        current.get("active_job_instance_id"),
    )
    material_task_id, execution_id, attempt_id, attempt_kind, control_id, job_instance_id = owner
    revision = current.get("execution_revision")
    base_revision = current.get("publication_reference_base_revision")
    journal_path = current.get("publication_journal")
    receipt_fields = (
        current.get("publication_intent_sha256"),
        current.get("publication_result_sha256"),
        current.get("publication_worker_result_sha256"),
        current.get("publication_reference_base_payload_sha256"),
    )
    tasks = current.get("material_tasks")
    task_matches = (
        []
        if not isinstance(tasks, list)
        else [
            item
            for item in tasks
            if isinstance(item, Mapping)
            and item.get("material_task_id") == material_task_id
        ]
    )
    if (
        type(material_task_id) is not str
        or type(execution_id) is not str
        or type(attempt_id) is not str
        or type(attempt_kind) is not str
        or type(control_id) is not str
        or type(job_instance_id) is not str
        or current.get("execution_id") != execution_id
        or current.get("publication_phase") != "db_committed"
        or current.get("execution_state") != "publishing"
        or type(revision) is not int
        or isinstance(revision, bool)
        or type(base_revision) is not int
        or isinstance(base_revision, bool)
        or base_revision <= 0
        or revision != base_revision + 1
        or type(journal_path) is not str
        or not journal_path.replace("\\", "/").casefold().startswith("d:/")
        or not journal_path.replace("\\", "/").casefold().endswith(
            "/publication-journal.json"
        )
        or any(
            type(value) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in receipt_fields
        )
        or len(task_matches) != 1
        or task_matches[0].get("status") != "发布收口中"
    ):
        raise MaterialExecutionClaimConflict(
            "recovery requires the exact receipt-bound publishing owner"
        )
    try:
        _validate_task_id(material_task_id)
        _validate_uuid(execution_id, "execution_id")
        _validate_uuid(attempt_id, "attempt_id")
        _validate_attempt_kind(attempt_kind)
        _validate_uuid(control_id, "control_id")
        _validate_uuid(job_instance_id, "job_instance_id")
        _assert_independent_identity_tokens(
            execution_id,
            attempt_id,
            control_id,
            job_instance_id,
        )
    except (TypeError, ValueError, MaterialExecutionClaimError) as exc:
        raise MaterialExecutionClaimConflict(
            "recovery requires the exact receipt-bound publishing owner"
        ) from exc
    return MaterialPublishingRecoveryIdentity(
        material_task_id=material_task_id,
        execution_id=execution_id,
        attempt_id=attempt_id,
        attempt_kind=attempt_kind,
        control_id=control_id,
        job_instance_id=job_instance_id,
        execution_revision=revision,
        publication_reference_base_revision=base_revision,
        publication_journal=journal_path,
    )


@dataclass(frozen=True, slots=True)
class MaterialExecutionClaimKey:
    """Physical database + material-step identity used for serialization."""

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
            raise ValueError("material execution database must remain on D:")
        for value, label in (
            (self.pipeline_id, "pipeline_id"),
            (self.material_step_id, "material_step_id"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        object.__setattr__(self, "database_path", resolved)

    @property
    def mutex_key(self) -> str:
        return (
            f"{str(self.database_path).casefold()}"
            f"|pipeline:{self.pipeline_id}|step:{self.material_step_id}"
        )


@dataclass(frozen=True, slots=True, init=False)
class MaterialExecutionClaim:
    """Receipt for the exact payload installed by a successful claim CAS."""

    material_task_id: str
    execution_id: str
    attempt_id: str
    revision: int
    _payload: dict[str, Any] = field(repr=False)

    def __init__(
        self,
        *,
        material_task_id: str,
        execution_id: str,
        attempt_id: str,
        revision: int,
        payload: Mapping[str, Any],
    ) -> None:
        object.__setattr__(self, "material_task_id", material_task_id)
        object.__setattr__(self, "execution_id", execution_id)
        object.__setattr__(self, "attempt_id", attempt_id)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "_payload", _clone_json_mapping(payload))
        self.__post_init__()

    @property
    def payload(self) -> dict[str, Any]:
        """Return a detached snapshot; the receipt bytes stay immutable."""

        return _clone_json_mapping(self._payload)

    def __post_init__(self) -> None:
        _validate_task_id(self.material_task_id)
        _validate_uuid(self.execution_id, "execution_id")
        _validate_uuid(self.attempt_id, "attempt_id")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool):
            raise ValueError("revision must be an integer")
        if self.revision <= 0:
            raise ValueError("revision must be positive")
        if not isinstance(self._payload, Mapping):
            raise TypeError("payload must be a mapping")


@dataclass(slots=True, init=False)
class MaterialExecutionClaimLease:
    """Mutable owner token for one claim's full cross-request lifecycle.

    The immutable ``expected_payload`` advances only after an acknowledged
    whole-blob CAS followed by a fresh read that proves the exact replacement.
    The named mutex remains owned until ``release``/context exit.
    """

    key: MaterialExecutionClaimKey
    material_task_id: str
    execution_id: str
    mutex: WindowsNamedMutex
    _expected_revision: int
    _expected_payload: dict[str, Any] = field(repr=False)
    _store: Any = field(repr=False)
    _owner_active_attempt_id: str = field(init=False, repr=False)
    _owner_active_attempt_kind: str = field(init=False, repr=False)
    _owner_active_control_id: str = field(init=False, repr=False)
    _owner_active_job_instance_id: str = field(init=False, repr=False)
    _released: bool = field(default=False, init=False, repr=False)

    def __init__(
        self,
        *,
        key: MaterialExecutionClaimKey,
        material_task_id: str,
        execution_id: str,
        mutex: WindowsNamedMutex,
        expected_revision: int,
        expected_payload: Mapping[str, Any],
        _store: Any,
    ) -> None:
        self.key = key
        self.material_task_id = material_task_id
        self.execution_id = execution_id
        self.mutex = mutex
        self._expected_revision = expected_revision
        self._expected_payload = _clone_json_mapping(expected_payload)
        self._store = _store
        owner_values = (
            self._expected_payload.get("active_attempt_id"),
            self._expected_payload.get("active_attempt_kind"),
            self._expected_payload.get("active_control_id"),
            self._expected_payload.get("active_job_instance_id"),
        )
        if any(type(value) is not str for value in owner_values):
            raise TypeError("claim owner fields must be strings")
        (
            self._owner_active_attempt_id,
            self._owner_active_attempt_kind,
            self._owner_active_control_id,
            self._owner_active_job_instance_id,
        ) = owner_values
        self._released = False
        self.__post_init__()

    def __post_init__(self) -> None:
        if type(self.key) is not MaterialExecutionClaimKey:
            raise TypeError("key must be MaterialExecutionClaimKey")
        _validate_task_id(self.material_task_id)
        _validate_uuid(self.execution_id, "execution_id")
        if not isinstance(self._expected_revision, int) or isinstance(
            self._expected_revision, bool
        ) or self._expected_revision <= 0:
            raise ValueError("expected_revision must be a positive integer")
        self._expected_payload = _clone_json_mapping(self._expected_payload)

    @property
    def expected_revision(self) -> int:
        return self._expected_revision

    @property
    def expected_payload(self) -> dict[str, Any]:
        """Return a detached snapshot; the ownership token stays private."""

        return _clone_json_mapping(self._expected_payload)

    @property
    def active_attempt_id(self) -> str:
        return str(self._expected_payload.get("active_attempt_id") or "")

    @property
    def active_attempt_kind(self) -> str:
        return str(self._expected_payload.get("active_attempt_kind") or "")

    @property
    def active_control_id(self) -> str:
        return str(self._expected_payload.get("active_control_id") or "")

    @property
    def active_job_instance_id(self) -> str:
        return str(self._expected_payload.get("active_job_instance_id") or "")

    def assert_owned(self, session: Session | None = None) -> None:
        del session  # A caller's identity map is never a fresh ownership proof.
        self._ensure_owned()
        try:
            current = self._store.fresh_read()
        except Exception as exc:
            raise MaterialExecutionClaimUnknown(
                "claim ownership fresh read could not be completed"
            ) from exc
        if current != self._expected_payload:
            raise MaterialExecutionClaimConflict(
                "material execution claim is no longer owned by this lease"
            )

    def compare_and_swap(self, replacement: Mapping[str, Any]) -> None:
        self._ensure_owned()
        replacement_payload = _clone_json_mapping(replacement)
        if (
            replacement_payload.get("execution_id") != self.execution_id
            or replacement_payload.get("execution_started_at")
            != self._expected_payload.get("execution_started_at")
            or replacement_payload.get("claim_key")
            != self._expected_payload.get("claim_key")
            or not _active_owner_transition_is_valid(
                replacement_payload,
                owner_material_task_id=self.material_task_id,
                owner_execution_id=self.execution_id,
                owner_attempt_id=self._owner_active_attempt_id,
                owner_attempt_kind=self._owner_active_attempt_kind,
                owner_control_id=self._owner_active_control_id,
                owner_job_instance_id=self._owner_active_job_instance_id,
            )
        ):
            raise MaterialExecutionClaimConflict(
                "replacement would change the immutable claim owner"
            )
        self._commit_replacement(replacement_payload)

    def clear_attempt_for_fallback(
        self,
        *,
        replacement: Mapping[str, Any],
    ) -> MaterialExecutionClaim:
        """Retire the current owner without making a new owner claimable.

        The replacement must carry the terminal history and set
        ``execution_state=running``, ``publication_phase=rolled_back`` and
        ``attempt_transition=retiring`` while clearing every active owner
        field.  Keeping the execution identity and a non-idle state prevents
        the compatibility ``claim`` path from starting a second execution
        before the old attempt has been retired by its runner.
        """

        self._ensure_owned()
        terminal_attempt_id = self._owner_active_attempt_id
        replacement_payload = _clone_json_mapping(replacement)
        if (
            not self._active_owner_matches_internal(self._expected_payload)
            or replacement_payload.get("execution_id") != self.execution_id
            or replacement_payload.get("execution_started_at")
            != self._expected_payload.get("execution_started_at")
            or replacement_payload.get("claim_key")
            != self._expected_payload.get("claim_key")
            or replacement_payload.get("active_material_task_id")
            not in (None, "")
            or replacement_payload.get("active_execution_id") not in (None, "")
            or replacement_payload.get("active_attempt_id") not in (None, "")
            or replacement_payload.get("active_attempt_kind") not in (None, "")
            or replacement_payload.get("active_control_id") not in (None, "")
            or replacement_payload.get("active_job_instance_id") not in (None, "")
            or replacement_payload.get("execution_state") != "running"
            or replacement_payload.get("publication_phase") != "rolled_back"
            or replacement_payload.get("attempt_transition") != "retiring"
        ):
            raise MaterialExecutionClaimConflict(
                "fallback clear requires the exact active owner and retiring state"
            )
        replacement_revision = replacement_payload.get("execution_revision")
        if replacement_revision != self._expected_revision + 1:
            raise MaterialExecutionClaimConflict(
                "fallback clear must advance execution_revision exactly once"
            )
        self._commit_replacement(replacement_payload)
        self._owner_active_attempt_id = ""
        self._owner_active_attempt_kind = ""
        self._owner_active_control_id = ""
        self._owner_active_job_instance_id = ""
        return MaterialExecutionClaim(
            material_task_id=self.material_task_id,
            execution_id=self.execution_id,
            attempt_id=terminal_attempt_id,
            revision=self._expected_revision,
            payload=self._expected_payload,
        )

    def install_rotated_attempt(
        self,
        *,
        attempt_id: str,
        attempt_kind: str,
        control_id: str,
        job_instance_id: str,
        terminal_receipt: MaterialCompletionReceipt,
        retirement_receipt: MaterialAttemptRetirementReceipt,
        replacement: Mapping[str, Any] | None = None,
    ) -> MaterialExecutionClaim:
        """Install a fresh owner only after typed terminal/retirement proof."""

        self._ensure_owned()
        _validate_uuid(attempt_id, "attempt_id")
        _validate_attempt_kind(attempt_kind)
        _validate_optional_token(control_id, "control_id")
        _validate_optional_token(job_instance_id, "job_instance_id")
        _assert_independent_identity_tokens(
            self.execution_id,
            attempt_id,
            control_id,
            job_instance_id,
        )
        if type(terminal_receipt) is not MaterialCompletionReceipt:
            raise TypeError("terminal_receipt must be MaterialCompletionReceipt")
        if type(retirement_receipt) is not MaterialAttemptRetirementReceipt:
            raise TypeError(
                "retirement_receipt must be MaterialAttemptRetirementReceipt"
            )
        if (
            terminal_receipt.execution_id != self.execution_id
            or terminal_receipt.execution_revision != self._expected_revision
            or terminal_receipt.attempt_id
            in (None, "", attempt_id, control_id, job_instance_id)
            or terminal_receipt.operation not in _ATTEMPT_KINDS
            or terminal_receipt.status not in {"failed", "terminated"}
            or terminal_receipt.final_payload_sha256
            != _json_sha256(self._expected_payload)
            or terminal_receipt.attempt_history_sha256 is None
            or retirement_receipt.status != terminal_receipt.status
            or retirement_receipt.execution_id != terminal_receipt.execution_id
            or retirement_receipt.operation != terminal_receipt.operation
            or retirement_receipt.attempt_id != terminal_receipt.attempt_id
            or retirement_receipt.execution_revision
                != terminal_receipt.execution_revision
            or retirement_receipt.final_payload_sha256
                != terminal_receipt.final_payload_sha256
            or retirement_receipt.attempt_history_sha256
                != terminal_receipt.attempt_history_sha256
            or retirement_receipt.result_sha256 != terminal_receipt.result_sha256
            or retirement_receipt.observed_at != terminal_receipt.observed_at
            or retirement_receipt.control_id
            != _history_control_id_for_receipt(
                self._expected_payload,
                terminal_receipt,
            )
            or retirement_receipt.job_instance_id
            != _history_job_instance_id_for_receipt(
                self._expected_payload,
                terminal_receipt,
            )
            or retirement_receipt.retired is not True
            or self._expected_payload.get("execution_state") != "running"
            or self._expected_payload.get("publication_phase") != "rolled_back"
            or self._expected_payload.get("attempt_transition") != "retiring"
            or any(
                self._expected_payload.get(field) not in (None, "")
                for field in (
                    "active_material_task_id",
                    "active_execution_id",
                    "active_attempt_id",
                    "active_attempt_kind",
                    "active_control_id",
                    "active_job_instance_id",
                )
            )
        ):
            raise MaterialExecutionClaimConflict(
                "terminal receipt does not match the cleared retiring execution"
            )
        previous_attempt_tokens = {terminal_receipt.attempt_id}
        if attempt_id in previous_attempt_tokens:
            raise MaterialExecutionClaimConflict(
                "attempt installation requires a fresh attempt identity"
            )
        replacement_payload = _clone_json_mapping(
            self._expected_payload if replacement is None else replacement
        )
        if (
            replacement_payload.get("execution_id") != self.execution_id
            or replacement_payload.get("execution_started_at")
            != self._expected_payload.get("execution_started_at")
            or replacement_payload.get("claim_key")
            != self._expected_payload.get("claim_key")
        ):
            raise MaterialExecutionClaimConflict(
                "attempt installation would change the immutable execution owner"
            )
        replacement_payload.update(
            {
                "execution_id": self.execution_id,
                "execution_state": "claimed",
                "publication_phase": "preparing",
                "attempt_transition": None,
                "active_material_task_id": self.material_task_id,
                "active_execution_id": self.execution_id,
                "active_attempt_id": attempt_id,
                "active_attempt_kind": attempt_kind,
                "active_control_id": control_id,
                "active_job_instance_id": job_instance_id,
                "execution_revision": self._expected_revision + 1,
            }
        )
        self._commit_replacement(replacement_payload)
        self._owner_active_attempt_id = attempt_id
        self._owner_active_attempt_kind = attempt_kind
        self._owner_active_control_id = control_id
        self._owner_active_job_instance_id = job_instance_id
        return MaterialExecutionClaim(
            material_task_id=self.material_task_id,
            execution_id=self.execution_id,
            attempt_id=attempt_id,
            revision=self._expected_revision,
            payload=self._expected_payload,
        )

    def rotate_attempt(
        self,
        *,
        attempt_id: str,
        attempt_kind: str,
        control_id: str,
        job_instance_id: str,
        terminal_receipt: MaterialCompletionReceipt,
        retirement_receipt: MaterialAttemptRetirementReceipt,
        replacement: Mapping[str, Any] | None = None,
    ) -> MaterialExecutionClaim:
        """Proof-gated alias for :meth:`install_rotated_attempt`."""

        return self.install_rotated_attempt(
            attempt_id=attempt_id,
            attempt_kind=attempt_kind,
            control_id=control_id,
            job_instance_id=job_instance_id,
            terminal_receipt=terminal_receipt,
            retirement_receipt=retirement_receipt,
            replacement=replacement,
        )

    def _active_owner_matches_internal(self, payload: Mapping[str, Any]) -> bool:
        return (
            payload.get("active_material_task_id") == self.material_task_id
            and payload.get("active_execution_id") == self.execution_id
            and payload.get("active_attempt_id") == self._owner_active_attempt_id
            and payload.get("active_attempt_kind") == self._owner_active_attempt_kind
            and payload.get("active_control_id") == self._owner_active_control_id
            and payload.get("active_job_instance_id")
            == self._owner_active_job_instance_id
        )

    def _commit_replacement(self, replacement_payload: Mapping[str, Any]) -> None:
        """Commit and freshly prove one already-validated whole-blob change."""

        replacement_payload = _clone_json_mapping(replacement_payload)
        replacement_revision = replacement_payload.get("execution_revision")
        if replacement_revision != self._expected_revision + 1:
            raise MaterialExecutionClaimConflict(
                "replacement must advance execution_revision exactly once"
            )
        if not self._store._compare_and_swap_unlocked(
            self._expected_payload,
            replacement_payload,
        ):
            raise MaterialExecutionClaimConflict(
                "material execution claim CAS lost ownership"
            )
        # The write acknowledgement alone is not enough: a fresh session must
        # prove the exact replacement before advancing the immutable token.
        try:
            current = self._store.fresh_read()
        except Exception as exc:
            raise MaterialExecutionClaimUnknown(
                "claim CAS committed but fresh verification could not be read"
            ) from exc
        if current != replacement_payload:
            raise MaterialExecutionClaimUnknown(
                "claim CAS committed but fresh verification disagreed"
            )
        revision = current.get("execution_revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
            raise MaterialExecutionClaimUnknown(
                "claim CAS replacement has no valid execution revision"
            )
        self._expected_payload = _clone_json_mapping(current)
        self._expected_revision = revision

    def release(self) -> None:
        if self._released:
            return
        try:
            self.mutex.release()
        except BaseException as exc:
            self._released = True
            raise MaterialExecutionClaimUnknown(
                "claim mutex release acknowledgement is unknown"
            ) from exc
        self._released = True

    def __enter__(self) -> "MaterialExecutionClaimLease":
        self._ensure_owned()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()

    def _ensure_owned(self) -> None:
        if self._released:
            raise MaterialExecutionClaimError("material execution claim lease is released")
        assert_owned = getattr(self.mutex, "assert_owned", None)
        if callable(assert_owned):
            try:
                assert_owned()
            except Exception as exc:
                raise MaterialExecutionClaimConflict(
                    "material execution claim mutex is no longer owned"
                ) from exc


class MaterialPendingRunnerRetirementLease:
    """Same-thread lease over one exact completed-but-unattested payload."""

    def __init__(
        self,
        *,
        key: MaterialExecutionClaimKey,
        mutex: WindowsNamedMutex,
        expected_payload: Mapping[str, Any],
        checkpoint: MaterialRunnerRetirementCheckpoint,
        _store: Any,
    ) -> None:
        if type(key) is not MaterialExecutionClaimKey:
            raise TypeError("key must be MaterialExecutionClaimKey")
        if type(checkpoint) is not MaterialRunnerRetirementCheckpoint:
            raise TypeError("checkpoint must be MaterialRunnerRetirementCheckpoint")
        self.key = key
        self.mutex = mutex
        self._expected_payload = _clone_json_mapping(expected_payload)
        self._expected_revision = checkpoint.completion_revision
        self.checkpoint = checkpoint
        self._store = _store
        self._released = False

    @property
    def material_task_id(self) -> str:
        return self.checkpoint.material_task_id

    @property
    def execution_id(self) -> str:
        return self.checkpoint.execution_id

    @property
    def active_attempt_id(self) -> str:
        return self.checkpoint.attempt_id

    @property
    def active_attempt_kind(self) -> str:
        return self.checkpoint.operation

    @property
    def active_control_id(self) -> str:
        return self.checkpoint.control_id

    @property
    def active_job_instance_id(self) -> str:
        return self.checkpoint.job_instance_id

    @property
    def reference_revision(self) -> int:
        return self.checkpoint.reference_revision

    @property
    def reference_payload_sha256(self) -> str:
        return self.checkpoint.reference_payload_sha256

    @property
    def expected_revision(self) -> int:
        return self._expected_revision

    @property
    def expected_payload(self) -> dict[str, Any]:
        return _clone_json_mapping(self._expected_payload)

    def assert_owned(self, session: Session | None = None) -> None:
        del session
        self._ensure_owned()
        try:
            current = self._store.fresh_read()
        except Exception as exc:
            raise MaterialExecutionClaimUnknown(
                "pending retirement ownership fresh read could not be completed"
            ) from exc
        if current != self._expected_payload:
            raise MaterialExecutionClaimConflict(
                "pending runner retirement is no longer owned by this lease"
            )

    def compare_and_swap(self, replacement: Mapping[str, Any]) -> None:
        self._ensure_owned()
        replacement_payload = _clone_json_mapping(replacement)
        current_checkpoint = pending_runner_retirement_from_payload(
            self._expected_payload
        )
        if current_checkpoint != self.checkpoint:
            raise MaterialExecutionClaimConflict(
                "pending runner retirement checkpoint changed"
            )
        expected_business = _clone_json_mapping(self._expected_payload)
        replacement_business = _clone_json_mapping(replacement_payload)
        expected_business.pop("runner_retirement", None)
        replacement_business.pop("runner_retirement", None)
        expected_business.pop("execution_revision", None)
        replacement_business.pop("execution_revision", None)
        retirement = replacement_payload.get("runner_retirement")
        if (
            expected_business != replacement_business
            or replacement_payload.get("execution_revision")
            != self._expected_revision + 1
            or type(retirement) is not dict
            or retirement.get("format") != "material-runner-retirement-v1"
            or retirement.get("state") != "attested"
            or retirement.get("checkpoint") != self.checkpoint.to_json()
            or type(retirement.get("attestation")) is not dict
        ):
            raise MaterialExecutionClaimConflict(
                "pending retirement lease only permits the exact attestation ack"
            )
        try:
            attested = attested_runner_retirement_from_payload(replacement_payload)
        except MaterialRunnerRetirementError as exc:
            raise MaterialExecutionClaimConflict(
                "pending retirement attestation is not fully bound"
            ) from exc
        if attested.checkpoint != self.checkpoint:
            raise MaterialExecutionClaimConflict(
                "pending retirement attestation changed its checkpoint"
            )
        if not self._store._compare_and_swap_unlocked(
            self._expected_payload,
            replacement_payload,
        ):
            raise MaterialExecutionClaimConflict(
                "pending runner retirement CAS lost ownership"
            )
        try:
            current = self._store.fresh_read()
        except Exception as exc:
            raise MaterialExecutionClaimUnknown(
                "runner retirement ack committed but fresh verification failed"
            ) from exc
        if current != replacement_payload:
            raise MaterialExecutionClaimUnknown(
                "runner retirement ack fresh verification disagreed"
            )
        self._expected_payload = _clone_json_mapping(current)
        self._expected_revision += 1

    def release(self) -> None:
        if self._released:
            return
        try:
            self.mutex.release()
        except BaseException as exc:
            self._released = True
            raise MaterialExecutionClaimUnknown(
                "pending retirement mutex release acknowledgement is unknown"
            ) from exc
        self._released = True

    def _ensure_owned(self) -> None:
        if self._released:
            raise MaterialExecutionClaimError(
                "pending runner retirement lease is released"
            )
        assert_owned = getattr(self.mutex, "assert_owned", None)
        if callable(assert_owned):
            try:
                assert_owned()
            except Exception as exc:
                raise MaterialExecutionClaimConflict(
                    "pending retirement mutex is no longer owned"
                ) from exc


class SqlAlchemyMaterialExecutionClaimStore:
    """Fresh-read/whole-blob CAS store for one material PipelineStep.

    The store uses a new SQLAlchemy Session for every operation.  It verifies
    that the bound engine resolves to the same physical D: database supplied in
    ``MaterialExecutionClaimKey``; this prevents an accidental relative/C:
    database from being used by a production claim path.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        key: MaterialExecutionClaimKey,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        if type(key) is not MaterialExecutionClaimKey:
            raise TypeError("key must be MaterialExecutionClaimKey")
        self.session_factory = session_factory
        self.key = key
        self._assert_bound_database()

    def fresh_read(self) -> dict[str, Any]:
        """Read and detach the complete output JSON from a fresh Session."""

        with self.session_factory() as session:
            return self._fresh_read_in_session(session)

    def compare_and_swap(
        self,
        expected: Mapping[str, Any],
        replacement: Mapping[str, Any],
        *,
        timeout_ms: int = 0,
    ) -> bool:
        """Perform one serialized compatibility CAS under the step mutex.

        Lifecycle callers must use :class:`MaterialExecutionClaimLease`; its
        internal CAS bypasses this one-shot wrapper while retaining the mutex.
        """

        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or timeout_ms < 0
        ):
            raise ValueError("timeout_ms must be a non-negative integer")
        mutex = self._acquire_step_mutex(timeout_ms=timeout_ms)
        try:
            current = self.fresh_read()
            if current != _clone_json_mapping(expected):
                return False
            _validate_compatibility_cas(expected, replacement)
            acknowledged = self._compare_and_swap_unlocked(expected, replacement)
            if not acknowledged:
                return False
            expected_replacement = _clone_json_mapping(replacement)
            try:
                current = self.fresh_read()
            except Exception as exc:
                raise MaterialExecutionClaimUnknown(
                    "whole-blob CAS committed but fresh verification could not be read"
                ) from exc
            if current != expected_replacement:
                raise MaterialExecutionClaimUnknown(
                    "whole-blob CAS committed but fresh verification disagreed"
                )
            return True
        finally:
            _release_mutex_or_unknown(mutex)

    def _compare_and_swap_unlocked(
        self,
        expected: Mapping[str, Any],
        replacement: Mapping[str, Any],
    ) -> bool:
        """Atomically replace the whole JSON blob if it is still exact.

        A false result means the expected blob was stale.  Any exception during
        execute/commit is propagated as an unknown acknowledgement so callers
        can preserve filesystem evidence instead of guessing.
        """

        expected_payload = _clone_json_mapping(expected)
        replacement_payload = _clone_json_mapping(replacement)
        with self.session_factory() as session:
            try:
                acknowledged = self._compare_and_swap_in_session(
                    session,
                    expected_payload,
                    replacement_payload,
                )
                if not acknowledged:
                    session.rollback()
                    return False
                session.commit()
                return True
            except MaterialExecutionClaimUnknown:
                _best_effort_rollback(session)
                raise
            except Exception as exc:
                _best_effort_rollback(session)
                raise MaterialExecutionClaimUnknown(
                    "whole-blob CAS acknowledgement is unknown"
                ) from exc

    def claim(
        self,
        *,
        material_task_id: str,
        execution_id: str,
        attempt_id: str,
        attempt_kind: str = "empty",
        control_id: str = "",
        job_instance_id: str = "",
        timeout_ms: int = 0,
    ) -> MaterialExecutionClaim:
        """Acquire a one-shot claim under the step mutex.

        New production callers must use :meth:`claim_lease` so the mutex is
        held through worker, publication, and retirement.  This compatibility
        helper still serializes the claim CAS and releases the mutex before
        returning its immutable receipt; it is not a lifecycle lease.
        """

        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or timeout_ms < 0
        ):
            raise ValueError("timeout_ms must be a non-negative integer")
        mutex = self._acquire_step_mutex(timeout_ms=timeout_ms)
        try:
            return self._claim_unlocked(
                material_task_id=material_task_id,
                execution_id=execution_id,
                attempt_id=attempt_id,
                attempt_kind=attempt_kind,
                control_id=control_id,
                job_instance_id=job_instance_id,
            )
        finally:
            _release_mutex_or_unknown(mutex)

    def _claim_unlocked(
        self,
        *,
        material_task_id: str,
        execution_id: str,
        attempt_id: str,
        attempt_kind: str = "empty",
        control_id: str = "",
        job_instance_id: str = "",
    ) -> MaterialExecutionClaim:
        """Claim one immutable task with a fresh-read/CAS transition.

        This method only installs the control-plane claim.  It deliberately
        does not launch a worker, write a project file, or mark the task's user
        visible status as completed.
        """

        _validate_task_id(material_task_id)
        _validate_uuid(execution_id, "execution_id")
        _validate_uuid(attempt_id, "attempt_id")
        _validate_attempt_kind(attempt_kind)
        _validate_optional_token(control_id, "control_id")
        _validate_optional_token(job_instance_id, "job_instance_id")
        _assert_independent_identity_tokens(
            execution_id,
            attempt_id,
            control_id,
            job_instance_id,
        )
        with self.session_factory() as session:
            try:
                # SQLite has no row-level SELECT FOR UPDATE.  BEGIN IMMEDIATE
                # acquires the single writer reservation before the fresh
                # read, so two processes cannot both validate an idle blob and
                # later overwrite one another.
                session.execute(text("BEGIN IMMEDIATE"))
                persisted_expected = self._fresh_read_in_session(session)
                expected = _with_stable_material_task_ids(persisted_expected)
                _assert_task_present(expected, material_task_id)
                phase_value = expected.get("publication_phase", "idle")
                state_value = expected.get("execution_state", "idle")
                if phase_value is None:
                    phase_value = "idle"
                if state_value is None:
                    state_value = "idle"
                if type(phase_value) is not str or type(state_value) is not str:
                    raise MaterialExecutionClaimConflict(
                        "material step control fields have invalid types"
                    )
                phase = phase_value
                state = state_value
                active_ids = (
                    expected.get("active_material_task_id"),
                    expected.get("active_execution_id"),
                    expected.get("active_attempt_id"),
                    expected.get("active_control_id"),
                    expected.get("active_job_instance_id"),
                )
                if any(value not in (None, "") for value in active_ids):
                    raise MaterialExecutionClaimConflict(
                        "material step already has an active execution identity"
                    )
                retirement = expected.get("runner_retirement")
                if retirement is not None:
                    try:
                        attested_runner_retirement_from_payload(expected)
                    except MaterialRunnerRetirementError as exc:
                        raise MaterialExecutionClaimConflict(
                            "material step runner retirement attestation is invalid"
                        ) from exc
                elif expected.get("execution_id") not in (None, "") or (
                    expected.get("last_execution_status")
                    in {"succeeded", "failed", "terminated"}
                ):
                    raise MaterialExecutionClaimConflict(
                        "completed material step has no runner retirement attestation"
                    )
                if phase not in _CLAIMABLE_PHASES or state not in _CLAIMABLE_EXECUTION_STATES:
                    raise MaterialExecutionClaimConflict(
                        f"material step is not claimable: phase={phase!r}, state={state!r}"
                    )

                replacement = copy.deepcopy(expected)
                _mark_material_task_claimed(replacement, material_task_id)
                revision = _next_revision(expected)
                replacement.update(
                    {
                        "execution_id": execution_id,
                        "execution_started_at": _utc_now(),
                        "execution_completed_at": None,
                        "publication_phase": "preparing",
                        "publication_journal": None,
                        "execution_state": "claimed",
                        "next_material_task_id": None,
                        "active_material_task_id": material_task_id,
                        "active_execution_id": execution_id,
                        "active_attempt_id": attempt_id,
                        "active_attempt_kind": attempt_kind,
                        "active_control_id": control_id,
                        "active_job_instance_id": job_instance_id,
                        "execution_revision": revision,
                        "claim_key": {
                            "pipeline_id": self.key.pipeline_id,
                            "material_step_id": self.key.material_step_id,
                            "material_task_id": material_task_id,
                        },
                    }
                )
                if not self._compare_and_swap_in_session(
                    session,
                    persisted_expected,
                    replacement,
                ):
                    raise MaterialExecutionClaimConflict(
                        "material step changed before claim CAS could acquire it"
                    )
                session.commit()
            except MaterialExecutionClaimError:
                _best_effort_rollback(session)
                raise
            except Exception as exc:
                _best_effort_rollback(session)
                raise MaterialExecutionClaimUnknown(
                    "claim transaction acknowledgement is unknown"
                ) from exc
        receipt = MaterialExecutionClaim(
            material_task_id=material_task_id,
            execution_id=execution_id,
            attempt_id=attempt_id,
            revision=revision,
            payload=_clone_json_mapping(replacement),
        )
        try:
            current = self.fresh_read()
        except Exception as exc:
            raise MaterialExecutionClaimUnknown(
                "claim committed but fresh verification could not be read"
            ) from exc
        if current != receipt.payload:
            raise MaterialExecutionClaimUnknown(
                "claim committed but fresh verification disagreed"
            )
        return receipt

    def acquire_claim_mutex(self, *, timeout_ms: int = 0) -> WindowsNamedMutex:
        """Acquire the step mutex for a same-thread prepare/claim handoff.

        The returned mutex is deliberately still owned.  A caller must either
        transfer it to :meth:`claim_lease_with_owned_mutex` or release it on a
        known pre-claim failure.  Unknown claim acknowledgements must retain it
        for explicit reconciliation.
        """

        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or timeout_ms < 0
        ):
            raise ValueError("timeout_ms must be a non-negative integer")
        return self._acquire_step_mutex(timeout_ms=timeout_ms)

    def claim_lease_with_owned_mutex(
        self,
        *,
        mutex: WindowsNamedMutex,
        material_task_id: str,
        execution_id: str,
        attempt_id: str,
        attempt_kind: str = "empty",
        control_id: str = "",
        job_instance_id: str = "",
    ) -> MaterialExecutionClaimLease:
        """Claim while adopting an already-owned step mutex into the lease.

        This is the production preparation seam: filesystem attempt roots can
        be created after mutex acquisition and before the database claim, with
        no unlock/relock race.  Ownership stays with the caller on every error;
        only a successful return transfers it to the resulting lease.
        """

        if not isinstance(mutex, WindowsNamedMutex):
            raise TypeError("mutex must be WindowsNamedMutex")
        return self._claim_lease_from_owned_mutex(
            mutex=mutex,
            material_task_id=material_task_id,
            execution_id=execution_id,
            attempt_id=attempt_id,
            attempt_kind=attempt_kind,
            control_id=control_id,
            job_instance_id=job_instance_id,
            require_exact_mutex=True,
        )

    def _claim_lease_from_owned_mutex(
        self,
        *,
        mutex: Any,
        material_task_id: str,
        execution_id: str,
        attempt_id: str,
        attempt_kind: str,
        control_id: str,
        job_instance_id: str,
        require_exact_mutex: bool,
    ) -> MaterialExecutionClaimLease:
        expected_name = build_mutex_name("material-step", self.key.mutex_key)
        actual_name = getattr(mutex, "name", None)
        if require_exact_mutex and actual_name != expected_name:
            raise MaterialExecutionClaimConflict(
                "owned mutex does not serialize this material step"
            )
        assert_owned = getattr(mutex, "assert_owned", None)
        if callable(assert_owned):
            try:
                assert_owned()
            except Exception as exc:
                raise MaterialExecutionClaimConflict(
                    "material step mutex is not owned by the current thread"
                ) from exc
        elif require_exact_mutex:
            raise MaterialExecutionClaimConflict(
                "material step mutex cannot prove current-thread ownership"
            )
        if bool(getattr(mutex, "was_abandoned", False)):
            raise MaterialExecutionClaimConflict(
                "abandoned material-step mutex requires explicit reconciliation"
            )

        receipt = self._claim_unlocked(
            material_task_id=material_task_id,
            execution_id=execution_id,
            attempt_id=attempt_id,
            attempt_kind=attempt_kind,
            control_id=control_id,
            job_instance_id=job_instance_id,
        )
        try:
            current = self.fresh_read()
        except Exception as exc:
            raise MaterialExecutionClaimUnknown(
                "claim lease committed but fresh verification could not be read"
            ) from exc
        if current != receipt.payload:
            raise MaterialExecutionClaimUnknown(
                "claim lease fresh verification disagreed"
            )
        return MaterialExecutionClaimLease(
            key=self.key,
            material_task_id=material_task_id,
            execution_id=execution_id,
            mutex=mutex,
            expected_revision=receipt.revision,
            expected_payload=current,
            _store=self,
        )

    def claim_lease(
        self,
        *,
        material_task_id: str,
        execution_id: str,
        attempt_id: str,
        attempt_kind: str = "empty",
        control_id: str = "",
        job_instance_id: str = "",
        timeout_ms: int = 0,
    ) -> MaterialExecutionClaimLease:
        """Compatibility lease helper for callers that do not pre-create attempts.

        New production code must use ``acquire_claim_mutex`` followed by
        ``claim_lease_with_owned_mutex`` so an unknown acknowledgement can keep
        both the prepared evidence and mutex handle reachable for recovery.
        """

        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or timeout_ms < 0
        ):
            raise ValueError("timeout_ms must be a non-negative integer")
        mutex = self.acquire_claim_mutex(timeout_ms=timeout_ms)
        try:
            return self._claim_lease_from_owned_mutex(
                mutex=mutex,
                material_task_id=material_task_id,
                execution_id=execution_id,
                attempt_id=attempt_id,
                attempt_kind=attempt_kind,
                control_id=control_id,
                job_instance_id=job_instance_id,
                require_exact_mutex=False,
            )
        except BaseException:
            try:
                mutex.release()
            except BaseException as release_error:
                raise MaterialExecutionClaimUnknown(
                    "claim lease failed and mutex release acknowledgement is unknown"
                ) from release_error
            raise

    @contextmanager
    def material_step_lock(self, *, timeout_ms: int = 0) -> Iterator[WindowsNamedMutex]:
        """Hold the named material-step mutex across the full lifecycle."""

        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms < 0:
            raise ValueError("timeout_ms must be a non-negative integer")
        mutex = self._acquire_step_mutex(timeout_ms=timeout_ms)
        try:
            yield mutex
        finally:
            _release_mutex_or_unknown(mutex)

    def acquire_recovery_mutex(self, *, timeout_ms: int = 0) -> WindowsNamedMutex:
        """Acquire the step mutex while retaining an abandoned owner handle.

        ``WAIT_ABANDONED`` is not a completion proof.  This recovery-only seam
        intentionally returns the still-owned mutex so a same-thread caller can
        fresh-read the claim, reconcile durable attempt evidence, and release
        it only after the recovery decision.  Ordinary claim paths must keep
        using :meth:`acquire_claim_mutex`, which fails closed on abandonment.
        """

        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or timeout_ms < 0
        ):
            raise ValueError("timeout_ms must be a non-negative integer")
        return self._acquire_step_mutex(
            timeout_ms=timeout_ms,
            allow_abandoned=True,
        )

    def assert_recovery_mutex(self, mutex: WindowsNamedMutex) -> None:
        """Prove a recovery handle serializes this exact material step."""

        if not isinstance(mutex, WindowsNamedMutex):
            raise TypeError("mutex must be WindowsNamedMutex")
        expected_name = build_mutex_name("material-step", self.key.mutex_key)
        if mutex.name != expected_name:
            raise MaterialExecutionClaimConflict(
                "recovery mutex does not serialize this material step"
            )
        try:
            mutex.assert_owned()
        except Exception as exc:
            raise MaterialExecutionClaimConflict(
                "recovery mutex is not owned by the current thread"
            ) from exc

    def adopt_publishing_lease_with_owned_recovery_mutex(
        self,
        *,
        mutex: WindowsNamedMutex,
        material_task_id: str,
        execution_id: str,
        attempt_id: str,
        attempt_kind: str,
        control_id: str,
        job_instance_id: str,
    ) -> MaterialExecutionClaimLease:
        """Adopt one exact abandoned ``publishing`` owner without a new claim.

        This recovery-only seam performs no write.  It turns the current whole
        database blob into a lease token only after the caller proves ownership
        of this step's recovery mutex and every durable owner/receipt field is
        present.  The completion recovery CAS therefore consumes the exact
        ``r+1`` publishing blob instead of pretending to still own the original
        ``r`` claim snapshot.
        """

        _validate_task_id(material_task_id)
        _validate_uuid(execution_id, "execution_id")
        _validate_uuid(attempt_id, "attempt_id")
        _validate_attempt_kind(attempt_kind)
        _validate_uuid(control_id, "control_id")
        _validate_uuid(job_instance_id, "job_instance_id")
        _assert_independent_identity_tokens(
            execution_id,
            attempt_id,
            control_id,
            job_instance_id,
        )
        self.assert_recovery_mutex(mutex)
        try:
            current = self.fresh_read()
        except Exception as exc:
            raise MaterialExecutionClaimUnknown(
                "publishing recovery fresh read could not be completed"
            ) from exc

        identity = publishing_recovery_identity_from_payload(current)
        expected_owner = (
            material_task_id,
            execution_id,
            attempt_id,
            attempt_kind,
            control_id,
            job_instance_id,
        )
        actual_owner = (
            identity.material_task_id,
            identity.execution_id,
            identity.attempt_id,
            identity.attempt_kind,
            identity.control_id,
            identity.job_instance_id,
        )
        if actual_owner != expected_owner:
            raise MaterialExecutionClaimConflict(
                "recovery requires the exact receipt-bound publishing owner"
            )
        revision = identity.execution_revision

        # A second read closes the gap between validation and lease creation.
        # The named mutex prevents a cooperating writer, while exact equality
        # also fails closed on an out-of-band database mutation.
        try:
            verified = self.fresh_read()
        except Exception as exc:
            raise MaterialExecutionClaimUnknown(
                "publishing recovery verification could not be completed"
            ) from exc
        if verified != current:
            raise MaterialExecutionClaimConflict(
                "publishing payload changed during recovery adoption"
            )
        return MaterialExecutionClaimLease(
            key=self.key,
            material_task_id=material_task_id,
            execution_id=execution_id,
            mutex=mutex,
            expected_revision=revision,
            expected_payload=verified,
            _store=self,
        )

    def adopt_pending_retirement_lease_with_owned_recovery_mutex(
        self,
        *,
        mutex: WindowsNamedMutex,
        material_task_id: str,
        execution_id: str,
        attempt_id: str,
        attempt_kind: str,
        control_id: str,
        job_instance_id: str,
    ) -> MaterialPendingRunnerRetirementLease:
        """Adopt one exact final ``runner_retirement=pending`` payload."""

        _validate_task_id(material_task_id)
        _validate_uuid(execution_id, "execution_id")
        _validate_uuid(attempt_id, "attempt_id")
        _validate_attempt_kind(attempt_kind)
        _validate_uuid(control_id, "control_id")
        _validate_uuid(job_instance_id, "job_instance_id")
        _assert_independent_identity_tokens(
            execution_id,
            attempt_id,
            control_id,
            job_instance_id,
        )
        self.assert_recovery_mutex(mutex)
        try:
            current = self.fresh_read()
            checkpoint = pending_runner_retirement_from_payload(current)
        except Exception as exc:
            raise MaterialExecutionClaimConflict(
                "pending runner retirement evidence is not exactly recoverable"
            ) from exc
        expected_identity = (
            material_task_id,
            execution_id,
            attempt_id,
            attempt_kind,
            control_id,
            job_instance_id,
        )
        actual_identity = (
            checkpoint.material_task_id,
            checkpoint.execution_id,
            checkpoint.attempt_id,
            checkpoint.operation,
            checkpoint.control_id,
            checkpoint.job_instance_id,
        )
        if actual_identity != expected_identity:
            raise MaterialExecutionClaimConflict(
                "recovery requires the exact pending retirement owner"
            )
        try:
            verified = self.fresh_read()
        except Exception as exc:
            raise MaterialExecutionClaimUnknown(
                "pending retirement verification could not be completed"
            ) from exc
        if verified != current:
            raise MaterialExecutionClaimConflict(
                "pending retirement payload changed during adoption"
            )
        return MaterialPendingRunnerRetirementLease(
            key=self.key,
            mutex=mutex,
            expected_payload=verified,
            checkpoint=checkpoint,
            _store=self,
        )

    @contextmanager
    def recovery_material_step_lock(
        self,
        *,
        timeout_ms: int = 0,
    ) -> Iterator[WindowsNamedMutex]:
        """Hold a recovery mutex on one thread until durable reconciliation ends."""

        mutex = self.acquire_recovery_mutex(timeout_ms=timeout_ms)
        try:
            yield mutex
        finally:
            _release_mutex_or_unknown(mutex)

    def _acquire_step_mutex(
        self,
        *,
        timeout_ms: int,
        allow_abandoned: bool = False,
    ) -> WindowsNamedMutex:
        try:
            name = build_mutex_name("material-step", self.key.mutex_key)
            mutex = WindowsNamedMutex.try_acquire(name, timeout_ms=timeout_ms)
        except WindowsMutexBusy as exc:
            raise MaterialExecutionClaimConflict(
                "material step mutex is already owned"
            ) from exc
        except BaseException as exc:
            raise MaterialExecutionClaimUnknown(
                "material step mutex acquisition acknowledgement is unknown"
            ) from exc
        if bool(getattr(mutex, "was_abandoned", False)) and not allow_abandoned:
            # WAIT_ABANDONED proves only that the previous owner died; it does
            # not prove whether its DB/file publication reached a safe state.
            # Keep the mutex closed and force an explicit recovery decision.
            try:
                mutex.release()
            except BaseException as exc:
                raise MaterialExecutionClaimUnknown(
                    "abandoned material-step mutex could not be released safely"
                ) from exc
            raise MaterialExecutionClaimConflict(
                "material step mutex was abandoned; explicit reconciliation is required"
            )
        return mutex

    def _assert_bound_database(self) -> None:
        try:
            with self.session_factory() as session:
                bound = session.get_bind()
                actual = get_sqlite_database_path(bound)
        except Exception as exc:
            raise ValueError(
                "material execution claim store requires a file-backed SQLite engine"
            ) from exc
        if actual != self.key.database_path:
            raise ValueError(
                "claim key database path does not match the bound SQLite database"
            )

    def _fresh_read_in_session(self, session: Session) -> dict[str, Any]:
        value = session.execute(
            select(PipelineStep.output_summary).where(
                PipelineStep.id == self.key.material_step_id,
                PipelineStep.pipeline_id == self.key.pipeline_id,
            )
        ).scalar_one_or_none()
        if value is None:
            raise MaterialExecutionClaimError(
                "material execution step does not belong to the requested pipeline"
            )
        return _clone_json_mapping(value)

    def _compare_and_swap_in_session(
        self,
        session: Session,
        expected: Mapping[str, Any],
        replacement: Mapping[str, Any],
    ) -> bool:
        result = session.execute(
            update(PipelineStep)
            .where(
                PipelineStep.id == self.key.material_step_id,
                PipelineStep.pipeline_id == self.key.pipeline_id,
                PipelineStep.output_summary == _clone_json_mapping(expected),
            )
            .values(output_summary=_clone_json_mapping(replacement))
        )
        rowcount = result.rowcount
        if rowcount == 1:
            return True
        if rowcount == 0:
            return False
        raise MaterialExecutionClaimUnknown(
            "whole-blob CAS returned an ambiguous row count"
        )


def _clone_json_mapping(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("output_summary must be a JSON object")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TypeError("output_summary must contain only JSON values") from exc
    if not isinstance(decoded, dict):
        raise TypeError("output_summary must be a JSON object")
    return copy.deepcopy(decoded)


def _json_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_uuid(value: str, label: str) -> None:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError(f"{label} must be a canonical UUID")


def _validate_task_id(value: str) -> None:
    if not isinstance(value, str) or _TASK_ID_RE.fullmatch(value) is None:
        raise ValueError("material_task_id is not a safe immutable identifier")


def _validate_attempt_kind(value: str) -> None:
    if value not in _ATTEMPT_KINDS:
        raise ValueError("attempt_kind is not an allowed material operation")


def _validate_optional_token(value: str, label: str) -> None:
    if not isinstance(value, str) or (
        value and _TASK_ID_RE.fullmatch(value) is None
    ):
        raise ValueError(f"{label} is not a safe identifier")


def _history_entry_for_receipt(
    payload: Mapping[str, Any], receipt: MaterialCompletionReceipt
) -> Mapping[str, Any]:
    history = payload.get("material_attempt_history")
    if not isinstance(history, list):
        raise MaterialExecutionClaimConflict(
            "terminal history is missing for attempt installation"
        )
    matches = [
        item
        for item in history
        if isinstance(item, Mapping)
        and item.get("entry_sha256") == receipt.attempt_history_sha256
    ]
    if len(matches) != 1:
        raise MaterialExecutionClaimConflict(
            "terminal history receipt is not unique for attempt installation"
        )
    entry = matches[0]
    if (
        entry.get("execution_id") != receipt.execution_id
        or entry.get("attempt_id") != receipt.attempt_id
        or entry.get("operation") != receipt.operation
        or entry.get("status") != receipt.status
        or entry.get("result_sha256") != receipt.result_sha256
        or entry.get("observed_at") != receipt.observed_at
    ):
        raise MaterialExecutionClaimConflict(
            "terminal history receipt does not bind the completion proof"
        )
    return entry


def _history_control_id_for_receipt(
    payload: Mapping[str, Any], receipt: MaterialCompletionReceipt
) -> str:
    entry = _history_entry_for_receipt(payload, receipt)
    value = entry.get("control_id")
    if not isinstance(value, str) or not value:
        raise MaterialExecutionClaimConflict(
            "terminal history has no control identity"
        )
    return value


def _history_job_instance_id_for_receipt(
    payload: Mapping[str, Any], receipt: MaterialCompletionReceipt
) -> str:
    entry = _history_entry_for_receipt(payload, receipt)
    value = entry.get("job_instance_id")
    if not isinstance(value, str) or not value:
        raise MaterialExecutionClaimConflict(
            "terminal history has no Job identity"
        )
    return value


def _with_stable_material_task_ids(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically migrate legacy shot-only tasks as part of the claim CAS."""

    normalized = _clone_json_mapping(payload)
    tasks = normalized.get("material_tasks")
    if not isinstance(tasks, list):
        raise MaterialExecutionClaimError("material task list is missing")
    seen: set[str] = set()
    for item in tasks:
        if not isinstance(item, dict):
            raise MaterialExecutionClaimError("material task list contains an invalid item")
        material_task_id = item.get("material_task_id")
        if material_task_id in (None, ""):
            try:
                material_task_id = material_task_id_for_shot(
                    str(item.get("shot_id") or "")
                )
            except ValueError as exc:
                raise MaterialExecutionClaimError(
                    "legacy material task has no stable shot identity"
                ) from exc
            item["material_task_id"] = material_task_id
        try:
            _validate_task_id(material_task_id)
        except ValueError as exc:
            raise MaterialExecutionClaimError(
                "material task has an invalid immutable identifier"
            ) from exc
        normalized_key = material_task_id.casefold()
        if normalized_key in seen:
            raise MaterialExecutionClaimError(
                "material_task_id must be unique within the material step"
            )
        seen.add(normalized_key)
    return normalized


def _assert_independent_identity_tokens(*values: str) -> None:
    nonempty = [value for value in values if value]
    if len(set(nonempty)) != len(nonempty):
        raise ValueError("execution, attempt, control, and Job identities must be independent")


def _assert_task_present(
    payload: Mapping[str, Any], material_task_id: str
) -> Mapping[str, Any]:
    tasks = payload.get("material_tasks")
    if not isinstance(tasks, list):
        raise MaterialExecutionClaimError("material task list is missing")
    matches = [
        item
        for item in tasks
        if isinstance(item, Mapping)
        and item.get("material_task_id") == material_task_id
    ]
    if len(matches) != 1:
        raise MaterialExecutionClaimError(
            "material_task_id must identify exactly one persisted task"
        )
    task = matches[0]
    if task.get("material_path") or task.get("status") in {"已完成", "已跳过"}:
        raise MaterialExecutionClaimConflict(
            "material task already has a terminal result"
        )
    if task.get("status") not in _CLAIMABLE_TASK_STATUSES:
        raise MaterialExecutionClaimConflict(
            "material task status is not claimable; reconciliation is required"
        )
    return task


def _mark_material_task_claimed(
    payload: dict[str, Any], material_task_id: str
) -> None:
    tasks = payload.get("material_tasks")
    if not isinstance(tasks, list):
        raise MaterialExecutionClaimError("material task list is missing")
    matches = [
        index
        for index, item in enumerate(tasks)
        if isinstance(item, dict) and item.get("material_task_id") == material_task_id
    ]
    if len(matches) != 1:
        raise MaterialExecutionClaimError(
            "material_task_id must identify exactly one persisted task"
        )
    index = matches[0]
    task = copy.deepcopy(tasks[index])
    task["status"] = "进行中"
    history = list(task.get("status_history") or [])
    if not history or history[-1] != "进行中":
        history.append("进行中")
    task["status_history"] = history[-20:]
    tasks[index] = task


def _next_revision(payload: Mapping[str, Any]) -> int:
    current = payload.get("execution_revision", 0)
    if not isinstance(current, int) or isinstance(current, bool) or current < 0:
        raise MaterialExecutionClaimError("execution_revision is invalid")
    return current + 1


def _validate_compatibility_cas(
    expected: Mapping[str, Any],
    replacement: Mapping[str, Any],
) -> None:
    expected_payload = _clone_json_mapping(expected)
    replacement_payload = _clone_json_mapping(replacement)
    expected_revision = expected_payload.get("execution_revision", 0)
    replacement_revision = replacement_payload.get("execution_revision")
    same_revision_legacy = (
        type(expected_revision) is int
        and type(replacement_revision) is int
        and replacement_revision == expected_revision
        and not any(
            expected_payload.get(key) not in (None, "")
            or replacement_payload.get(key) not in (None, "")
            for key in (
                "active_material_task_id",
                "active_execution_id",
                "active_attempt_id",
                "active_attempt_kind",
                "active_control_id",
                "active_job_instance_id",
            )
        )
    )
    if (
        type(expected_revision) is not int
        or expected_revision < 0
        or type(replacement_revision) is not int
        or (
            replacement_revision != expected_revision + 1
            and not same_revision_legacy
        )
    ):
        raise MaterialExecutionClaimConflict(
            "compatibility CAS must advance execution_revision exactly once"
        )
    for key in ("execution_id", "execution_started_at", "claim_key"):
        if key in expected_payload and replacement_payload.get(key) != expected_payload[key]:
            raise MaterialExecutionClaimConflict(
                f"compatibility CAS cannot change immutable field {key}"
            )
    owner_keys = (
        "active_material_task_id",
        "active_execution_id",
        "active_attempt_id",
        "active_attempt_kind",
        "active_control_id",
        "active_job_instance_id",
    )
    expected_owner = tuple(expected_payload.get(key) for key in owner_keys)
    replacement_owner = tuple(replacement_payload.get(key) for key in owner_keys)
    expected_is_empty = all(value in (None, "") for value in expected_owner)
    replacement_is_empty = all(value in (None, "") for value in replacement_owner)
    if expected_is_empty:
        if not replacement_is_empty:
            raise MaterialExecutionClaimConflict(
                "compatibility CAS cannot create an active execution owner"
            )
    elif replacement_owner != expected_owner and not replacement_is_empty:
        raise MaterialExecutionClaimConflict(
            "compatibility CAS cannot replace an active execution owner"
        )


def _best_effort_rollback(session: Session) -> None:
    try:
        session.rollback()
    except BaseException:
        # The original database error is the useful evidence; a rollback
        # acknowledgement failure must not hide it.
        pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _release_mutex_or_unknown(mutex: WindowsNamedMutex) -> None:
    try:
        mutex.release()
    except BaseException as exc:
        raise MaterialExecutionClaimUnknown(
            "material-step mutex release acknowledgement is unknown"
        ) from exc


def _active_owner_transition_is_valid(
    payload: Mapping[str, Any],
    *,
    owner_material_task_id: str,
    owner_execution_id: str,
    owner_attempt_id: str,
    owner_attempt_kind: str,
    owner_control_id: str,
    owner_job_instance_id: str,
) -> bool:
    """Allow exact owner retention or an explicit all-fields terminal clear."""

    current = (
        payload.get("active_material_task_id"),
        payload.get("active_execution_id"),
        payload.get("active_attempt_id"),
        payload.get("active_attempt_kind"),
        payload.get("active_control_id"),
        payload.get("active_job_instance_id"),
    )
    owner = (
        owner_material_task_id,
        owner_execution_id,
        owner_attempt_id,
        owner_attempt_kind,
        owner_control_id,
        owner_job_instance_id,
    )
    if current == owner:
        return True
    active_keys = (
        "active_material_task_id",
        "active_execution_id",
        "active_attempt_id",
        "active_attempt_kind",
        "active_control_id",
        "active_job_instance_id",
    )
    if any(key not in payload for key in active_keys):
        return False
    return all(value is None or (type(value) is str and value == "") for value in current)
