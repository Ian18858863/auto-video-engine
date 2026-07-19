"""Contained Windows material-operation runner.

This module owns the D-drive attempt lifecycle, durable nine-phase launch
descriptor, isolated Job/pipe/worker happy path, typed result handoff, and the
fail-closed ownership boundary passed to reconciliation/quarantine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import base64
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from types import MappingProxyType
from typing import Literal, Mapping, get_args
from uuid import UUID, uuid4

from app.core.windows_durable_file import (
    DurableWriteReceipt,
    write_durable_bytes,
    write_durable_json,
)
from app.core.windows_storage_policy import (
    ApprovedPhysicalDirectory,
    approve_physical_d_write_root,
)
from app.pipeline.material_operation_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESULT_BYTES,
    MAX_SECRET_BYTES,
    MaterialOperationKind,
    MaterialOperationBlocked,
    MaterialOperationOutcome,
    MaterialOperationRequest,
    MaterialOperationSecrets,
    MaterialProtocolError,
    MaterialWorkerResult,
    TerminationEvidence,
    decode_material_operation_request,
    decode_material_worker_result,
    encode_material_operation_request,
    encode_material_operation_secrets,
    encode_material_worker_result,
)
from app.pipeline.material_operation_control_protocol import (
    MAX_READY_BYTES,
    MaterialWorkerReady,
    decode_material_worker_ready_bound,
    validate_material_worker_ready_identity,
)
from app.pipeline.windows_job_object import WindowsJobObject
from app.pipeline.windows_atomic_process import (
    SuspendedJobProcess,
    WindowsAtomicProcessResourceError,
    create_suspended_process_in_job,
)
from app.pipeline.windows_overlapped_pipe_io import (
    NativeOutcome,
    OperationKind,
    OverlappedPipeError,
    OwnedOverlappedPipeOperation,
)
from app.pipeline.material_runner_retirement import MaterialRunnerRetirementAttestation


if os.name == "nt":  # pragma: no branch - production is Windows-only.
    import pywintypes
    import win32api
    import win32con
    import win32event
    import win32file
    import win32job
    import win32pipe
    import win32process
    import win32security
else:  # Keep imports deterministic while the storage gate fails closed.
    pywintypes = None
    win32api = None
    win32con = None
    win32event = None
    win32file = None
    win32job = None
    win32pipe = None
    win32process = None
    win32security = None


DESCRIPTOR_PROTOCOL_VERSION = 1
CONTAINER_FILENAME = "container.json"
RETIRED_FILENAME = "retired.json"
RETIREMENT_RELEASE_AUDIT_FILENAME = "retirement-release-audit.json"
PUBLICATION_JOURNAL_FILENAME = "publication-journal.json"
REQUEST_FILENAME = "request.json"
TERMINATION_FILENAME = "termination.json"
TERMINATION_OUTCOME_FILENAME = "termination-outcome.json"
QUARANTINE_FILENAME = "quarantine.json"
QUARANTINE_RESOLUTION_FILENAME = "quarantine-resolution.json"
LAST_HANDLE_CLOSE_INTENT_FILENAME = "last-handle-close-intent.json"
LAST_HANDLE_CLOSED_FILENAME = "last-handle-closed.json"
MAX_DESCRIPTOR_BYTES = 64 * 1024
MAX_RETIREMENT_BYTES = 64 * 1024
MAX_RETIREMENT_RELEASE_AUDIT_BYTES = 64 * 1024
MAX_PUBLICATION_JOURNAL_BYTES = 2 * 1024 * 1024
MAX_PUBLICATION_TARGET_BYTES = 4 * 1024 * 1024 * 1024
MAX_TERMINATION_BYTES = 64 * 1024
MAX_TERMINATION_OUTCOME_BYTES = 64 * 1024
MAX_QUARANTINE_BYTES = 64 * 1024
MAX_QUARANTINE_RESOLUTION_BYTES = 64 * 1024
MAX_LAST_HANDLE_CLOSE_INTENT_BYTES = 64 * 1024
MAX_LAST_HANDLE_CLOSED_BYTES = 64 * 1024
UUID_GENERATION_ATTEMPTS = 32
PIPE_NONCE_GENERATION_ATTEMPTS = 32

_FILE_ATTRIBUTE_DIRECTORY = 0x0010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_SECRET_PIPE_USER_ACCESS = 0xC0100000
_SECRET_PIPE_SYSTEM_ACCESS = 0x10000000
_SECRET_PIPE_USER_ACCESS_EFFECTIVE = 0x0012019F
_SECRET_PIPE_SYSTEM_ACCESS_EFFECTIVE = 0x001F01FF
_SECRET_PIPE_PREFIX = r"\\.\pipe\AutoVideoEngine.Material."
_PLAYWRIGHT_BROWSERS_PATH = Path(r"D:\CodexData\Caches\ms-playwright")
_WORKER_BOOTSTRAP_PATH: Path | None = Path(__file__).with_name(
    "material_operation_worker.py"
)
_OPERATIONS = frozenset(get_args(MaterialOperationKind))
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_monotonic = time.monotonic
_DESCRIPTOR_COMMON_FIELDS = frozenset(
    {
        "protocol_version",
        "phase",
        "previous_phase",
        "previous_descriptor_sha256",
        "execution_id",
        "attempt_id",
        "control_id",
        "job_instance_id",
        "operation",
        "controller_pid",
        "controller_creation_time",
        "operation_may_have_started",
    }
)


class MaterialOperationRunnerError(RuntimeError):
    """A fail-closed runner lifecycle or evidence error."""

    __slots__ = ("code", "detail")

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class MaterialLaunchPhase(str, Enum):
    PLANNED = "planned"
    JOB_CONFIGURED = "job_configured"
    CREATE_INTENT = "create_intent"
    CREATED_IN_JOB_SUSPENDED = "created_in_job_suspended"
    RESUME_INTENT = "resume_intent"
    RESUMED_NO_SECRET = "resumed_no_secret"
    SECRET_SEND_INTENT = "secret_send_intent"
    SECRET_SENT = "secret_sent"
    READY = "ready"


MATERIAL_LAUNCH_PHASES = (
    MaterialLaunchPhase.PLANNED,
    MaterialLaunchPhase.JOB_CONFIGURED,
    MaterialLaunchPhase.CREATE_INTENT,
    MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
    MaterialLaunchPhase.RESUME_INTENT,
    MaterialLaunchPhase.RESUMED_NO_SECRET,
    MaterialLaunchPhase.SECRET_SEND_INTENT,
    MaterialLaunchPhase.SECRET_SENT,
    MaterialLaunchPhase.READY,
)
_PHASE_INDEX = {phase: index for index, phase in enumerate(MATERIAL_LAUNCH_PHASES)}
_REQUEST_HASH_INDEX = _PHASE_INDEX[MaterialLaunchPhase.JOB_CONFIGURED]
_ROOT_IDENTITY_INDEX = _PHASE_INDEX[
    MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED
]
_OPERATION_STARTED_INDEX = _PHASE_INDEX[
    MaterialLaunchPhase.SECRET_SEND_INTENT
]


@dataclass(frozen=True, slots=True)
class MaterialOperationContainerDescriptor:
    protocol_version: Literal[1]
    phase: MaterialLaunchPhase
    previous_phase: MaterialLaunchPhase | None
    previous_descriptor_sha256: str | None
    execution_id: str
    attempt_id: str
    control_id: str
    job_instance_id: str
    operation: MaterialOperationKind
    controller_pid: int
    controller_creation_time: str
    operation_may_have_started: bool
    request_sha256: str | None = None
    job_handle_inheritable: bool | None = None
    root_pid: int | None = None
    root_creation_time: str | None = None
    job_active_processes_after_create: int | None = None

    def __post_init__(self) -> None:
        _validate_container_descriptor(self)


@dataclass(slots=True)
class PreparedMaterialAttempt:
    execution_id: str
    attempt_id: str
    job_instance_id: str
    control_id: str
    control_directory: ApprovedPhysicalDirectory
    directory: ApprovedPhysicalDirectory
    pin_scope: tuple[ApprovedPhysicalDirectory, ...]
    container_hash: str


@dataclass(frozen=True, slots=True)
class MaterialRestartOwnershipGuard:
    """Same-thread ownership proof required around restart filesystem writes."""

    execution_id: str
    attempt_id: str
    control_id: str
    job_instance_id: str
    mutex: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.execution_id, "execution_id"),
            (self.attempt_id, "attempt_id"),
            (self.control_id, "control_id"),
            (self.job_instance_id, "job_instance_id"),
        ):
            _validate_uuid(value, label)
        if not callable(getattr(self.mutex, "assert_owned", None)):
            raise TypeError("mutex must expose an assert_owned method")
        if not callable(getattr(self.mutex, "release", None)):
            raise TypeError("mutex must expose a release method")

    def assert_owned(self) -> None:
        try:
            self.mutex.assert_owned()  # type: ignore[attr-defined]
        except MaterialOperationRunnerError:
            raise
        except BaseException as exc:
            raise MaterialOperationRunnerError(
                "restart_ownership_lost",
                "same-thread restart ownership could not be proven",
            ) from exc


@dataclass(frozen=True, slots=True)
class AdoptedMaterialAttempt:
    """Durable attempt pins and evidence rebuilt after controller restart."""

    prepared: PreparedMaterialAttempt
    descriptor: MaterialOperationContainerDescriptor
    request: MaterialOperationRequest
    reconciliation: ReconciliationEvidence
    reconciliation_sha256: str
    native_release_basis: Literal[
        "never_created_dead_controller",
        "queried_empty_dead_controller",
        "owner_exit_dead_controller",
        "last_handle_closed",
    ]
    outcome: MaterialOperationOutcome | None
    ownership_guard: MaterialRestartOwnershipGuard
    binding: MaterialRunnerClaimBinding | None = None

    def __post_init__(self) -> None:
        if type(self.prepared) is not PreparedMaterialAttempt:
            raise TypeError("prepared must be PreparedMaterialAttempt")
        if type(self.descriptor) is not MaterialOperationContainerDescriptor:
            raise TypeError("descriptor must be MaterialOperationContainerDescriptor")
        if type(self.request) is not MaterialOperationRequest:
            raise TypeError("request must be MaterialOperationRequest")
        if type(self.reconciliation) is not ReconciliationEvidence:
            raise TypeError("reconciliation must be ReconciliationEvidence")
        if type(self.ownership_guard) is not MaterialRestartOwnershipGuard:
            raise TypeError("ownership_guard must be MaterialRestartOwnershipGuard")
        _validate_sha256(
            self.reconciliation_sha256,
            code="adoption_reconciliation_invalid",
            detail="reconciliation_sha256 must be a lowercase SHA-256 digest",
        )
        if (
            self.prepared.execution_id != self.descriptor.execution_id
            or self.prepared.attempt_id != self.descriptor.attempt_id
            or self.prepared.control_id != self.descriptor.control_id
            or self.prepared.job_instance_id != self.descriptor.job_instance_id
            or self.prepared.container_hash != self.reconciliation.container_sha256
            or self.reconciliation.execution_id != self.descriptor.execution_id
            or self.reconciliation.attempt_id != self.descriptor.attempt_id
            or self.reconciliation.job_instance_id != self.descriptor.job_instance_id
            or self.reconciliation.root_pid != self.descriptor.root_pid
            or self.reconciliation.operation_may_have_started
            != (_PHASE_INDEX[self.descriptor.phase] >= _OPERATION_STARTED_INDEX)
            or self.request.execution_id != self.descriptor.execution_id
            or self.request.attempt_id != self.descriptor.attempt_id
            or self.request.operation != self.descriptor.operation
        ):
            raise ValueError("adopted descriptor and reconciliation identities differ")
        if (
            self.ownership_guard.execution_id != self.descriptor.execution_id
            or self.ownership_guard.attempt_id != self.descriptor.attempt_id
            or self.ownership_guard.control_id != self.descriptor.control_id
            or self.ownership_guard.job_instance_id != self.descriptor.job_instance_id
        ):
            raise ValueError("adopted attempt and ownership guard identities differ")
        descriptor_sha256 = hashlib.sha256(
            encode_material_container_descriptor(self.descriptor)
        ).hexdigest()
        if descriptor_sha256 != self.prepared.container_hash:
            raise ValueError("adopted descriptor bytes differ from prepared hash")
        expected_basis = {
            "never_created": "never_created_dead_controller",
            "queried_empty": "queried_empty_dead_controller",
            "owner_exit_kill_on_close": "owner_exit_dead_controller",
            "last_handle_close_kill_on_close": "last_handle_closed",
        }.get(self.reconciliation.method)
        if self.native_release_basis != expected_basis:
            raise ValueError("native release basis does not match reconciliation")
        if self.binding is not None:
            if type(self.binding) is not MaterialRunnerClaimBinding:
                raise TypeError("binding must be MaterialRunnerClaimBinding or None")
            if (
                self.prepared.execution_id != self.binding.execution_id
                or self.binding.operation != self.descriptor.operation
                or self.prepared.attempt_id != self.binding.attempt_id
                or self.prepared.control_id != self.binding.control_id
                or self.prepared.job_instance_id != self.binding.job_instance_id
            ):
                raise ValueError("adopted attempt and claim binding identities differ")
        if self.outcome is None:
            return
        if type(self.outcome) is not MaterialOperationOutcome:
            raise TypeError("outcome must be MaterialOperationOutcome or None")
        self.outcome.__post_init__()
        if (
            self.outcome.termination.root_pid != self.descriptor.root_pid
            or self.outcome.termination.root_pid != self.reconciliation.root_pid
        ):
            raise ValueError("adopted outcome termination belongs to another root")
        if self.reconciliation.method == "queried_empty" and (
            self.outcome.termination.observed_at != self.reconciliation.observed_at
            or self.outcome.termination.active_processes != 0
        ):
            raise ValueError("adopted outcome does not match queried-empty evidence")
        result = self.outcome.result
        if result is not None and (
            result.execution_id != self.descriptor.execution_id
            or result.attempt_id != self.descriptor.attempt_id
            or result.operation != self.descriptor.operation
        ):
            raise ValueError("adopted worker result belongs to another attempt")


@dataclass(frozen=True, slots=True)
class MaterialRunnerClaimBinding:
    """Typed database-claim identity bound to one prepared runner attempt.

    The binding is deliberately a receipt, not a database handle.  A
    lifecycle coordinator obtains it only after a fresh claim CAS and keeps
    the claim lease alive while the runner starts and completes the attempt.
    """

    material_task_id: str
    execution_id: str
    operation: MaterialOperationKind
    attempt_id: str
    control_id: str
    job_instance_id: str
    execution_revision: int
    payload_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.material_task_id) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", self.material_task_id)
            is None
        ):
            raise MaterialOperationRunnerError(
                "claim_binding_invalid",
                "material_task_id is not a safe immutable identifier",
            )
        _validate_uuid(self.execution_id, "execution_id")
        _validate_operation(self.operation)
        _validate_uuid(self.attempt_id, "attempt_id")
        _validate_uuid(self.control_id, "control_id")
        _validate_uuid(self.job_instance_id, "job_instance_id")
        if (
            type(self.execution_revision) is not int
            or self.execution_revision <= 0
        ):
            raise MaterialOperationRunnerError(
                "claim_binding_invalid",
                "execution_revision must be a positive integer",
            )
        _validate_sha256(
            self.payload_sha256,
            code="claim_binding_invalid",
            detail="payload_sha256 must be a lowercase SHA-256 digest",
        )


@dataclass(frozen=True, slots=True)
class MaterialRestartRetirementProof:
    """DB completion receipt narrowed to the runner retirement boundary."""

    status: Literal["failed", "terminated"]
    execution_id: str
    operation: MaterialOperationKind
    attempt_id: str
    execution_revision: int
    final_payload_sha256: str
    attempt_history_sha256: str
    result_sha256: str | None
    observed_at: str
    checkpoint_sha256: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"failed", "terminated"}:
            raise MaterialOperationRunnerError(
                "restart_retirement_proof_invalid",
                "restart retirement supports only failed or terminated completion",
            )
        _validate_uuid(self.execution_id, "execution_id")
        _validate_operation(self.operation)
        _validate_uuid(self.attempt_id, "attempt_id")
        if type(self.execution_revision) is not int or self.execution_revision <= 0:
            raise MaterialOperationRunnerError(
                "restart_retirement_proof_invalid",
                "execution_revision must be a positive integer",
            )
        for value, label in (
            (self.final_payload_sha256, "final_payload_sha256"),
            (self.attempt_history_sha256, "attempt_history_sha256"),
        ):
            _validate_sha256(
                value,
                code="restart_retirement_proof_invalid",
                detail=f"{label} must be a lowercase SHA-256 digest",
            )
        if self.result_sha256 is not None:
            _validate_sha256(
                self.result_sha256,
                code="restart_retirement_proof_invalid",
                detail="result_sha256 must be a lowercase SHA-256 digest",
            )
        if self.checkpoint_sha256:
            _validate_sha256(
                self.checkpoint_sha256,
                code="restart_retirement_proof_invalid",
                detail="checkpoint_sha256 must be a lowercase SHA-256 digest",
            )
        if self.status == "failed" and self.result_sha256 is None:
            raise MaterialOperationRunnerError(
                "restart_retirement_proof_invalid",
                "failed completion requires a worker-result receipt",
            )
        if self.status == "terminated" and self.result_sha256 is not None:
            raise MaterialOperationRunnerError(
                "restart_retirement_proof_invalid",
                "terminated completion cannot claim a worker-result receipt",
            )
        _validate_identity_time(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class MaterialRestartPublishedRetirementProof:
    """Final DB success receipt consumed by restart-only published retirement."""

    status: Literal["succeeded"]
    execution_id: str
    operation: MaterialOperationKind
    attempt_id: str
    execution_revision: int
    final_payload_sha256: str
    attempt_history_sha256: str
    publication_journal_sha256: str
    publication_intent_sha256: str
    publication_result_sha256: str
    publication_worker_result_sha256: str
    publication_reference_base_revision: int
    publication_reference_base_payload_sha256: str
    result_sha256: str
    observed_at: str
    checkpoint_sha256: str = ""

    def __post_init__(self) -> None:
        if self.status != "succeeded":
            raise MaterialOperationRunnerError(
                "restart_published_proof_invalid",
                "restart published retirement requires succeeded status",
            )
        _validate_uuid(self.execution_id, "execution_id")
        _validate_operation(self.operation)
        _validate_uuid(self.attempt_id, "attempt_id")
        if type(self.execution_revision) is not int or self.execution_revision <= 0:
            raise MaterialOperationRunnerError(
                "restart_published_proof_invalid",
                "execution_revision must be a positive integer",
            )
        for value, label in (
            (self.final_payload_sha256, "final_payload_sha256"),
            (self.attempt_history_sha256, "attempt_history_sha256"),
            (self.publication_journal_sha256, "publication_journal_sha256"),
            (self.publication_intent_sha256, "publication_intent_sha256"),
            (self.publication_result_sha256, "publication_result_sha256"),
            (
                self.publication_worker_result_sha256,
                "publication_worker_result_sha256",
            ),
            (
                self.publication_reference_base_payload_sha256,
                "publication_reference_base_payload_sha256",
            ),
            (self.result_sha256, "result_sha256"),
        ):
            _validate_sha256(
                value,
                code="restart_published_proof_invalid",
                detail=f"{label} must be a lowercase SHA-256 digest",
            )
        if self.checkpoint_sha256:
            _validate_sha256(
                self.checkpoint_sha256,
                code="restart_published_proof_invalid",
                detail="checkpoint_sha256 must be a lowercase SHA-256 digest",
            )
        if (
            type(self.publication_reference_base_revision) is not int
            or isinstance(self.publication_reference_base_revision, bool)
            or self.publication_reference_base_revision <= 0
        ):
            raise MaterialOperationRunnerError(
                "restart_published_proof_invalid",
                "publication_reference_base_revision must be a positive integer",
            )
        _validate_identity_time(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class MaterialAttemptRetirementReceipt:
    """Post-runner proof that a terminal attempt is fully retired.

    ``MaterialRestartRetirementProof`` proves the database terminal CAS.  It
    is intentionally not sufficient to install the next source attempt: the
    runner must first persist and read back its retirement tombstone and close
    every native/directory pin.  This receipt is emitted only after that
    boundary and binds the tombstone bytes to the same terminal proof.
    """

    status: Literal["failed", "terminated"]
    execution_id: str
    operation: MaterialOperationKind
    attempt_id: str
    control_id: str
    job_instance_id: str
    execution_revision: int
    final_payload_sha256: str
    attempt_history_sha256: str
    result_sha256: str | None
    observed_at: str
    retired_at: str
    tombstone_sha256: str
    retired: Literal[True] = True
    checkpoint_sha256: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"failed", "terminated"}:
            raise MaterialOperationRunnerError(
                "retirement_receipt_invalid",
                "retirement receipt supports only failed or terminated attempts",
            )
        _validate_uuid(self.execution_id, "execution_id")
        _validate_operation(self.operation)
        _validate_uuid(self.attempt_id, "attempt_id")
        _validate_uuid(self.control_id, "control_id")
        _validate_uuid(self.job_instance_id, "job_instance_id")
        if type(self.execution_revision) is not int or self.execution_revision <= 0:
            raise MaterialOperationRunnerError(
                "retirement_receipt_invalid",
                "execution_revision must be a positive integer",
            )
        for value, label in (
            (self.final_payload_sha256, "final_payload_sha256"),
            (self.attempt_history_sha256, "attempt_history_sha256"),
            (self.tombstone_sha256, "tombstone_sha256"),
        ):
            _validate_sha256(
                value,
                code="retirement_receipt_invalid",
                detail=f"{label} must be a lowercase SHA-256 digest",
            )
        if self.checkpoint_sha256:
            _validate_sha256(
                self.checkpoint_sha256,
                code="retirement_receipt_invalid",
                detail="checkpoint_sha256 must be a lowercase SHA-256 digest",
            )
        if self.result_sha256 is not None:
            _validate_sha256(
                self.result_sha256,
                code="retirement_receipt_invalid",
                detail="result_sha256 must be a lowercase SHA-256 digest",
            )
        if self.status == "failed" and self.result_sha256 is None:
            raise MaterialOperationRunnerError(
                "retirement_receipt_invalid",
                "failed retirement requires a worker-result receipt",
            )
        if self.status == "terminated" and self.result_sha256 is not None:
            raise MaterialOperationRunnerError(
                "retirement_receipt_invalid",
                "terminated retirement cannot carry a worker-result receipt",
            )
        if self.retired is not True:
            raise MaterialOperationRunnerError(
                "retirement_receipt_invalid",
                "retired must be exactly true",
            )
        _validate_identity_time(self.observed_at, "observed_at")
        _validate_identity_time(self.retired_at, "retired_at")


@dataclass(frozen=True, slots=True)
class ReconciliationEvidence:
    execution_id: str
    attempt_id: str
    job_instance_id: str
    method: Literal[
        "queried_empty",
        "never_created",
        "owner_exit_kill_on_close",
        "last_handle_close_kill_on_close",
    ]
    observation_basis: Literal[
        "queried_job",
        "pre_create_journal",
        "controller_exit_last_handle_inference",
        "controller_closed_last_handle_inference",
    ]
    active_processes: Literal[0] | None
    root_pid: int | None
    operation_may_have_started: bool
    observed_at: str
    container_sha256: str

    def __post_init__(self) -> None:
        _validate_uuid(self.execution_id, "execution_id")
        _validate_uuid(self.attempt_id, "attempt_id")
        _validate_uuid(self.job_instance_id, "job_instance_id")
        expected_basis = {
            "queried_empty": "queried_job",
            "never_created": "pre_create_journal",
            "owner_exit_kill_on_close": "controller_exit_last_handle_inference",
            "last_handle_close_kill_on_close": "controller_closed_last_handle_inference",
        }.get(self.method)
        if expected_basis is None or self.observation_basis != expected_basis:
            raise MaterialOperationRunnerError(
                "reconciliation_evidence_invalid",
                "reconciliation method and observation basis do not match",
            )
        if self.method == "queried_empty":
            if type(self.active_processes) is not int or self.active_processes != 0:
                raise MaterialOperationRunnerError(
                    "reconciliation_evidence_invalid",
                    "queried-empty evidence requires ActiveProcesses exactly zero",
                )
        elif self.active_processes is not None:
            raise MaterialOperationRunnerError(
                "reconciliation_evidence_invalid",
                "inference and never-created evidence cannot claim a Job query",
            )
        if self.root_pid is not None:
            _validate_windows_pid(self.root_pid, "root_pid")
        if self.method == "never_created" and (
            self.root_pid is not None or self.operation_may_have_started
        ):
            raise MaterialOperationRunnerError(
                "reconciliation_evidence_invalid",
                "never-created evidence cannot claim a root or operation start",
            )
        if type(self.operation_may_have_started) is not bool:
            raise MaterialOperationRunnerError(
                "reconciliation_evidence_invalid",
                "operation_may_have_started must be a boolean",
            )
        _validate_identity_time(self.observed_at, "observed_at")
        _validate_sha256(
            self.container_sha256,
            code="reconciliation_evidence_invalid",
            detail="container_sha256 must be a lowercase SHA-256 digest",
        )


@dataclass(frozen=True, slots=True)
class _ControllerIdentity:
    pid: int
    creation_time: str


@dataclass(frozen=True, slots=True)
class _FrozenMaterialRunInputs:
    request: MaterialOperationRequest = field(repr=False)
    request_bytes: bytes = field(repr=False)
    request_sha256: str
    secret_bytes: bytes = field(repr=False)
    secret_frame: bytes = field(repr=False)
    startup_deadline: float
    operation_timeout_seconds: float


@dataclass(slots=True)
class _SecretPipeOwner:
    name: str
    pipe_handle: object = field(repr=False)
    security_descriptor: object = field(repr=False)
    security_attributes: object = field(repr=False)
    active_operation: object | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        if self.active_operation is not None:
            raise MaterialOperationRunnerError(
                "secret_pipe_operation_live",
                "secret pipe cannot close while an overlapped operation is owned",
            )
        self.pipe_handle.Close()  # type: ignore[union-attr]
        self._closed = True


@dataclass(frozen=True, slots=True)
class _WorkerRuntimePreflight:
    application_path: Path
    worker_bootstrap: Path
    backend_root: Path
    site_packages: Path
    cwd: Path
    environment: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _WorkerLaunchSpec:
    application_path: Path
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str] = field(repr=False)


class _WindowsRunnerOps:
    """Private Windows capability seam; production methods return real owners."""

    def create_job(self) -> WindowsJobObject:
        return WindowsJobObject.create_unnamed_kill_on_close()

    def create_secret_pipe(self, *, forbidden_ids: tuple[str, ...]) -> object:
        return _create_secret_pipe_owner(forbidden_ids=forbidden_ids)

    def preflight_worker_runtime(
        self,
        *,
        prepared: PreparedMaterialAttempt,
    ) -> _WorkerRuntimePreflight:
        return _preflight_worker_runtime(prepared=prepared)

    def build_worker_launch_spec(
        self,
        *,
        prepared: PreparedMaterialAttempt,
        pipe_name: str,
        runtime_preflight: _WorkerRuntimePreflight,
    ) -> _WorkerLaunchSpec:
        return _build_worker_launch_spec(
            prepared=prepared,
            pipe_name=pipe_name,
            runtime_preflight=runtime_preflight,
        )

    def create_suspended_process(
        self,
        *,
        launch_spec: _WorkerLaunchSpec,
        job_owner: WindowsJobObject,
    ) -> SuspendedJobProcess:
        if type(launch_spec) is not _WorkerLaunchSpec:
            raise MaterialOperationRunnerError(
                "worker_launch_spec_invalid",
                "atomic create requires the frozen worker launch specification",
            )
        if type(job_owner) is not WindowsJobObject:
            raise MaterialOperationRunnerError(
                "job_owner_invalid",
                "atomic create requires the exact unnamed Job owner",
            )
        return create_suspended_process_in_job(
            application_path=launch_spec.application_path,
            argv=launch_spec.argv,
            cwd=launch_spec.cwd,
            environment=launch_spec.environment,
            job_handle=job_owner.handle,
        )

    def query_process_identity(self, *, pid: int) -> tuple[int, str]:
        return _query_process_identity(pid)

    def wait_exact_process_exit(
        self,
        *,
        pid: int,
        creation_time: str,
        timeout_seconds: float,
    ) -> bool:
        return _wait_exact_process_exit(
            pid=pid,
            creation_time=creation_time,
            timeout_seconds=timeout_seconds,
        )

    def verify_single_root_job(self, *, job_owner: object, root_pid: int) -> None:
        if type(job_owner) is not WindowsJobObject:
            raise MaterialOperationRunnerError(
                "job_owner_invalid",
                "membership proof requires the exact unnamed Job owner",
            )
        if (
            job_owner.active_process_count() != 1
            or job_owner.process_ids() != (root_pid,)
        ):
            raise MaterialOperationRunnerError(
                "job_membership_invalid",
                "suspended Job must contain exactly the queried root PID",
            )

    def connect_secret_pipe(
        self,
        *,
        pipe_owner: object,
        absolute_deadline: float,
    ) -> None:
        _connect_secret_pipe(
            pipe_owner=pipe_owner,
            absolute_deadline=absolute_deadline,
        )

    def verify_secret_pipe_client(
        self,
        *,
        pipe_owner: object,
        root_pid: int,
        root_creation_time: str,
        job_owner: object,
    ) -> None:
        _verify_secret_pipe_client(
            pipe_owner=pipe_owner,
            root_pid=root_pid,
            root_creation_time=root_creation_time,
            job_owner=job_owner,
        )

    def write_secret_frame(
        self,
        *,
        pipe_owner: object,
        immutable_frame: bytes,
        absolute_deadline: float,
    ) -> None:
        _write_secret_frame(
            pipe_owner=pipe_owner,
            immutable_frame=immutable_frame,
            absolute_deadline=absolute_deadline,
        )

    def wait_worker_ready(
        self,
        *,
        prepared: PreparedMaterialAttempt,
        expected_operation: MaterialOperationKind,
        expected_root_pid: int,
        expected_request_sha256: str,
        startup_deadline: float,
        operation_deadline: float,
    ) -> MaterialWorkerReady:
        return _wait_worker_ready(
            prepared=prepared,
            expected_operation=expected_operation,
            expected_root_pid=expected_root_pid,
            expected_request_sha256=expected_request_sha256,
            startup_deadline=startup_deadline,
            operation_deadline=operation_deadline,
        )

    def wait_job_empty(
        self,
        *,
        job_owner: object,
        operation_deadline: float,
    ) -> None:
        _wait_job_empty(
            job_owner=job_owner,
            operation_deadline=operation_deadline,
        )

    def read_worker_result(
        self,
        *,
        prepared: PreparedMaterialAttempt,
        expected_operation: MaterialOperationKind,
    ) -> MaterialWorkerResult:
        return _read_worker_result(
            prepared=prepared,
            expected_operation=expected_operation,
        )

    def collect_natural_termination(
        self,
        *,
        process_owner: object,
        root_pid: int,
    ) -> TerminationEvidence:
        return _collect_natural_termination(
            process_owner=process_owner,
            root_pid=root_pid,
        )

    def close_natural_completion_resources(
        self,
        *,
        process_owner: object,
        job_owner: object,
    ) -> None:
        _close_natural_completion_resources(
            process_owner=process_owner,
            job_owner=job_owner,
        )

    def close_last_handle_job(self, *, job_owner: object) -> None:
        """Close the exact sole Job owner for an intentional inference."""
        if type(job_owner) is not WindowsJobObject:
            raise MaterialOperationRunnerError(
                "job_owner_invalid",
                "last-handle inference requires the exact unnamed Job owner",
            )
        # ``WindowsJobObject.close`` is intentionally idempotent for ordinary
        # cleanup, which is unsafe for this inference boundary: an
        # already-closed handle must not masquerade as *this* close succeeding.
        # The runner is the sole owner, so it performs one strict transition
        # under the owner's own lock and treats every exception as unknown.
        with job_owner._lock:  # type: ignore[attr-defined]
            job_owner._ensure_open()  # type: ignore[attr-defined]
            job_owner._handle.Close()  # type: ignore[attr-defined]
            job_owner._closed = True  # type: ignore[attr-defined]

    def close_last_handle_process(self, *, process_owner: object) -> None:
        """Close process/thread handles after the Job close has succeeded."""
        if type(process_owner) is not SuspendedJobProcess:
            raise MaterialOperationRunnerError(
                "process_owner_invalid",
                "last-handle inference requires the exact root-process owner",
            )
        process_owner.close()

    def terminate_job_and_collect(
        self,
        *,
        job_owner: object,
        process_owner: object,
        root_pid: int,
        never_resumed: bool,
    ) -> TerminationEvidence:
        return _terminate_job_and_collect(
            job_owner=job_owner,
            process_owner=process_owner,
            root_pid=root_pid,
            never_resumed=never_resumed,
        )

    def terminate_job_for_quarantine(self, *, job_owner: object) -> bool:
        return _terminate_job_for_quarantine(job_owner=job_owner)


_WINDOWS_RUNNER_OPS = _WindowsRunnerOps()


@dataclass(slots=True)
class _PreparedRecord:
    prepared: PreparedMaterialAttempt
    operation: MaterialOperationKind
    controller: _ControllerIdentity
    execution_id: str
    attempt_id: str
    control_id: str
    job_instance_id: str
    control_directory: ApprovedPhysicalDirectory
    directory: ApprovedPhysicalDirectory
    pin_scope: tuple[ApprovedPhysicalDirectory, ...]
    container_hash: str
    phase: MaterialLaunchPhase = MaterialLaunchPhase.PLANNED
    previous_phase: MaterialLaunchPhase | None = None
    previous_descriptor_sha256: str | None = None
    request_sha256: str | None = None
    root_pid: int | None = None
    root_creation_time: str | None = None
    claim_bound: bool = False
    claim_binding: MaterialRunnerClaimBinding | None = field(default=None, repr=False)
    claim_acquired: bool = False
    adopted_after_restart: bool = False
    retired: bool = False
    frozen_run: _FrozenMaterialRunInputs | None = field(default=None, repr=False)
    job_owner: object | None = field(default=None, repr=False)
    pipe_owner: object | None = field(default=None, repr=False)
    process_owner: object | None = field(default=None, repr=False)
    process_quarantine_owner: object | None = field(default=None, repr=False)
    operation_deadline: float | None = None
    outcome: MaterialOperationOutcome | None = field(default=None, repr=False)
    completion_resources_closed: bool = False
    completion_close_attempted: bool = False
    reconciliation: ReconciliationEvidence | None = field(default=None, repr=False)
    reconciliation_hash: str | None = None
    quarantine_hash: str | None = None
    blocked: bool = False
    termination_outcome_hash: str | None = None
    containment_terminated: bool = False
    pipe_close_attempted: bool = False
    pipe_closed: bool = False
    pipe_close_unknown: bool = False
    io_drain_pending: bool = False
    process_quarantine_cleanup_attempted: bool = False
    process_quarantine_cleaned: bool = False
    quarantine_resolution_hash: str | None = None
    last_handle_close_intent_hash: str | None = None
    last_handle_closed_hash: str | None = None
    last_handle_close_attempted: bool = False
    last_handle_job_closed: bool = False
    last_handle_closed: bool = False


@dataclass(slots=True)
class _PendingPipeDrain:
    """Strong in-process ownership for an overlapped drain awaiting a later poll."""

    operation: object
    monitor_deadline: float
    polling: bool = False


class ProcessMaterialOperationRunner:
    """Own prepared attempt pins and advance only approved lifecycle stages."""

    def __init__(
        self,
        root: ApprovedPhysicalDirectory | Path | str,
    ) -> None:
        if isinstance(root, ApprovedPhysicalDirectory):
            root.verify()
            approved_root = root
            owns_root = False
        elif isinstance(root, (Path, str)):
            approved_root = approve_physical_d_write_root(Path(root))
            owns_root = True
        else:
            raise TypeError("root must be an approved D directory or absolute D path")
        try:
            controller = _current_controller_identity()
        except BaseException:
            if owns_root:
                approved_root.close()
            raise
        self._root = approved_root
        self._owns_root = owns_root
        self._controller = controller
        self._records: dict[int, _PreparedRecord] = {}
        self._pending_pipe_drains: dict[
            tuple[str, str, str], _PendingPipeDrain
        ] = {}
        self._lock = threading.RLock()
        self._closed = False

    def prepare_attempt(
        self,
        *,
        execution_id: str,
        operation: MaterialOperationKind,
    ) -> PreparedMaterialAttempt:
        _validate_uuid(execution_id, "execution_id")
        _validate_operation(operation)
        with self._lock:
            self._ensure_open()
            self._root.verify()
            pins: list[ApprovedPhysicalDirectory] = []
            try:
                jobs = _open_or_create_fixed_child(self._root, ".jobs")
                pins.append(jobs)
                controls = _open_or_create_fixed_child(
                    self._root,
                    ".jobs-control",
                )
                pins.append(controls)

                created = self._create_independent_attempt_roots(
                    execution_id=execution_id,
                    jobs=jobs,
                    controls=controls,
                )
                attempt_id, control_id, job_instance_id, attempt, control = created
                pins.extend((attempt, control))

                payload = attempt.create_exclusive_child("payload")
                pins.append(payload)
                temporary = attempt.create_exclusive_child("tmp")
                pins.append(temporary)
                profile = attempt.create_exclusive_child("profile")
                pins.append(profile)

                for pin in pins:
                    pin.verify()
                descriptor = _planned_descriptor(
                    execution_id=execution_id,
                    attempt_id=attempt_id,
                    control_id=control_id,
                    job_instance_id=job_instance_id,
                    operation=operation,
                    controller=self._controller,
                )
                receipt = write_durable_json(
                    parent=control,
                    filename=CONTAINER_FILENAME,
                    payload=_descriptor_to_mapping(descriptor),
                    replace=False,
                    max_bytes=MAX_DESCRIPTOR_BYTES,
                )
                control.verify()
                attempt.verify()

                prepared = PreparedMaterialAttempt(
                    execution_id=execution_id,
                    attempt_id=attempt_id,
                    job_instance_id=job_instance_id,
                    control_id=control_id,
                    control_directory=control,
                    directory=attempt,
                    pin_scope=tuple(pins),
                    container_hash=receipt.sha256,
                )
                record = _PreparedRecord(
                    prepared=prepared,
                    operation=operation,
                    controller=self._controller,
                    execution_id=execution_id,
                    attempt_id=attempt_id,
                    control_id=control_id,
                    job_instance_id=job_instance_id,
                    control_directory=control,
                    directory=attempt,
                    pin_scope=prepared.pin_scope,
                    container_hash=receipt.sha256,
                )
                self._records[id(prepared)] = record
                return prepared
            except BaseException:
                _close_pins(pins)
                raise

    def adopt_durable_attempt(
        self,
        *,
        ownership_guard: MaterialRestartOwnershipGuard,
        binding: MaterialRunnerClaimBinding | None = None,
        execution_id: str | None = None,
        attempt_id: str | None = None,
        control_id: str | None = None,
        job_instance_id: str | None = None,
        expected_operation: MaterialOperationKind,
        retirement_proof: (
            MaterialRestartRetirementProof
            | MaterialRestartPublishedRetirementProof
            | None
        ) = None,
    ) -> AdoptedMaterialAttempt:
        """Rebuild directory ownership for one terminated attempt after restart.

        This seam does not recreate a Job, process, or pipe and it does not
        prove database ownership by itself.  The lifecycle caller must first
        prove either the still-active claim (and pass ``binding``) or a final
        DB payload/history receipt (and pass the exact four durable IDs).  The
        latter covers the crash window after completion CAS cleared the active
        owner but before runner retirement.  Adoption succeeds only after the
        previous controller/root are no longer live and the on-disk identities
        still match those expected values.

        Native close flags intentionally remain false.  They are process-local
        acknowledgements and cannot be reconstructed honestly from a restart;
        a restart-specific retirement gate must use the durable journals
        instead of routing this record through the ordinary in-process retire
        methods.  A checkpoint-bound ``retirement_proof`` also permits a
        post-completion retry to adopt an exact existing tombstone so its newly
        opened pins can be closed without rewriting retirement history.
        """

        if binding is not None and type(binding) is not MaterialRunnerClaimBinding:
            raise TypeError("binding must be MaterialRunnerClaimBinding or None")
        if retirement_proof is not None:
            if type(retirement_proof) not in {
                MaterialRestartRetirementProof,
                MaterialRestartPublishedRetirementProof,
            }:
                raise TypeError(
                    "retirement_proof must be a restart retirement proof or None"
                )
            retirement_proof.__post_init__()
        if type(ownership_guard) is not MaterialRestartOwnershipGuard:
            raise TypeError("ownership_guard must be MaterialRestartOwnershipGuard")
        ownership_guard.__post_init__()
        if binding is not None:
            binding.__post_init__()
            supplied_ids = (execution_id, attempt_id, control_id, job_instance_id)
            bound_ids = (
                binding.execution_id,
                binding.attempt_id,
                binding.control_id,
                binding.job_instance_id,
            )
            if any(value is not None for value in supplied_ids) and supplied_ids != bound_ids:
                raise MaterialOperationRunnerError(
                    "adoption_identity_mismatch",
                    "explicit durable IDs differ from the active claim binding",
                )
            execution_id, attempt_id, control_id, job_instance_id = bound_ids
        else:
            for value, label in (
                (execution_id, "execution_id"),
                (attempt_id, "attempt_id"),
                (control_id, "control_id"),
                (job_instance_id, "job_instance_id"),
            ):
                _validate_uuid(value, label)
        _validate_operation(expected_operation)
        assert execution_id is not None
        assert attempt_id is not None
        assert control_id is not None
        assert job_instance_id is not None
        if (
            ownership_guard.execution_id != execution_id
            or ownership_guard.attempt_id != attempt_id
            or ownership_guard.control_id != control_id
            or ownership_guard.job_instance_id != job_instance_id
        ):
            raise MaterialOperationRunnerError(
                "adoption_identity_mismatch",
                "restart ownership guard does not match the durable attempt",
            )
        ownership_guard.assert_owned()
        with self._lock:
            self._ensure_open()
            for record in self._records.values():
                if (
                    record.execution_id == execution_id
                    and record.attempt_id == attempt_id
                    and record.control_id == control_id
                    and record.job_instance_id == job_instance_id
                ):
                    raise MaterialOperationRunnerError(
                        "attempt_already_owned",
                        "this runner already owns the durable attempt",
                    )

            pins: list[ApprovedPhysicalDirectory] = []
            try:
                self._root.verify()
                jobs = _open_existing_fixed_child(self._root, ".jobs")
                pins.append(jobs)
                controls = _open_existing_fixed_child(self._root, ".jobs-control")
                pins.append(controls)
                attempt = _open_existing_fixed_child(jobs, attempt_id)
                pins.append(attempt)
                control = _open_existing_fixed_child(controls, control_id)
                pins.append(control)
                for name in ("payload", "tmp", "profile"):
                    pins.append(_open_existing_fixed_child(attempt, name))
                for pin in pins:
                    pin.verify()
                ownership_guard.assert_owned()

                descriptor_bytes = _read_verified_bytes(
                    parent=control,
                    filename=CONTAINER_FILENAME,
                    max_bytes=MAX_DESCRIPTOR_BYTES,
                )
                descriptor_hash = hashlib.sha256(descriptor_bytes).hexdigest()
                descriptor = decode_material_container_descriptor(descriptor_bytes)
                if (
                    descriptor.execution_id != execution_id
                    or descriptor.attempt_id != attempt_id
                    or descriptor.control_id != control_id
                    or descriptor.job_instance_id != job_instance_id
                    or descriptor.operation != expected_operation
                ):
                    raise MaterialOperationRunnerError(
                        "adoption_identity_mismatch",
                        "durable descriptor does not match the claimed attempt",
                    )
                existing_retirement_bytes = _read_optional_verified_bytes(
                    parent=control,
                    filename=RETIRED_FILENAME,
                    max_bytes=MAX_RETIREMENT_BYTES,
                )
                if _read_optional_verified_bytes(
                    parent=control,
                    filename=RETIREMENT_RELEASE_AUDIT_FILENAME,
                    max_bytes=MAX_RETIREMENT_RELEASE_AUDIT_BYTES,
                ) is not None:
                    raise MaterialOperationRunnerError(
                        "adoption_blocked",
                        "retirement pin release is already in an unknown audit state",
                    )
                if existing_retirement_bytes is not None:
                    if (
                        retirement_proof is None
                        or not retirement_proof.checkpoint_sha256
                    ):
                        raise MaterialOperationRunnerError(
                            "attempt_already_retired",
                            "a durable retirement tombstone already owns this attempt",
                        )
                    _validate_restart_retirement_tombstone_for_adoption(
                        existing_retirement_bytes,
                        execution_id=execution_id,
                        attempt_id=attempt_id,
                        control_id=control_id,
                        job_instance_id=job_instance_id,
                        operation=expected_operation,
                        proof=retirement_proof,
                    )

                # A dead old controller is the restart ownership boundary.  A
                # queried-empty journal alone is not enough while that process
                # could still own a pipe or resume its own state machine.
                _prove_adopted_processes_exited(descriptor)

                # Keep the complete physical pin tuple alive while restart
                # reconciliation reads or creates journals.  Closing a
                # temporary control pin and reopening by path would permit a
                # same-bytes directory replacement between those operations.
                # The dead-controller proof above must happen before this call:
                # reconciliation may append a quarantine-resolution journal.
                reconciliation = self._reconcile_durable_control(
                    execution_id=execution_id,
                    attempt_id=attempt_id,
                    control_id=control_id,
                    expected_job_instance_id=job_instance_id,
                    expected_operation=expected_operation,
                    expected_descriptor_bytes=descriptor_bytes,
                    require_dead_controller=True,
                    ownership_guard=ownership_guard,
                    owned_control=control,
                    expected_retirement_bytes=existing_retirement_bytes,
                )
                current_descriptor_bytes = _read_verified_bytes(
                    parent=control,
                    filename=CONTAINER_FILENAME,
                    max_bytes=MAX_DESCRIPTOR_BYTES,
                )
                if current_descriptor_bytes != descriptor_bytes:
                    raise MaterialOperationRunnerError(
                        "adoption_descriptor_changed",
                        "durable descriptor changed during reconciliation",
                    )
                if descriptor_hash != reconciliation.container_sha256:
                    raise MaterialOperationRunnerError(
                        "adoption_descriptor_changed",
                        "durable descriptor changed after reconciliation",
                    )

                request_bytes = _read_adopted_request(
                    attempt=attempt,
                    descriptor=descriptor,
                    code_prefix="adoption",
                )
                request = (
                    None
                    if request_bytes is None
                    else decode_material_operation_request(request_bytes)
                )

                termination_bytes = _read_verified_bytes(
                    parent=control,
                    filename=TERMINATION_FILENAME,
                    max_bytes=MAX_TERMINATION_BYTES,
                )
                persisted_reconciliation = _decode_reconciliation_bytes(
                    termination_bytes
                )
                reconciliation_hash = hashlib.sha256(termination_bytes).hexdigest()
                if persisted_reconciliation != reconciliation:
                    raise MaterialOperationRunnerError(
                        "adoption_reconciliation_changed",
                        "durable reconciliation changed during adoption",
                    )

                prepared = PreparedMaterialAttempt(
                    execution_id=descriptor.execution_id,
                    attempt_id=descriptor.attempt_id,
                    job_instance_id=descriptor.job_instance_id,
                    control_id=descriptor.control_id,
                    control_directory=control,
                    directory=attempt,
                    pin_scope=tuple(pins),
                    container_hash=descriptor_hash,
                )
                outcome, termination_outcome_hash = _read_adopted_outcome(
                    prepared=prepared,
                    operation=descriptor.operation,
                )
                if outcome is None:
                    raise MaterialOperationRunnerError(
                        "adoption_blocked",
                        "restart adoption supports only durable failed or terminated outcomes, or a bound successful outcome",
                    )
                if request is None:
                    raise MaterialOperationRunnerError(
                        "adoption_request_unavailable",
                        "durable terminal attempt has no bound request receipt",
                    )
                if outcome.status == "succeeded":
                    if (
                        binding is None
                        or outcome.result is None
                        or outcome.result.status != "succeeded"
                        or outcome.termination.termination_method != "natural"
                        or descriptor.phase is not MaterialLaunchPhase.READY
                        or reconciliation.method != "queried_empty"
                        or reconciliation.active_processes != 0
                    ):
                        raise MaterialOperationRunnerError(
                            "adoption_blocked",
                            "successful restart adoption requires a bound publishing owner and queried-empty natural completion",
                        )
                elif outcome.status not in {"failed", "terminated"}:
                    raise MaterialOperationRunnerError(
                        "adoption_blocked",
                        "restart adoption outcome status is unsupported",
                    )
                elif reconciliation.method not in {
                    "queried_empty",
                    "owner_exit_kill_on_close",
                    "last_handle_close_kill_on_close",
                }:
                    raise MaterialOperationRunnerError(
                        "adoption_blocked",
                        "restart adoption has no safe terminal retirement gate for this reconciliation",
                    )
                quarantine_hash, quarantine_resolution_hash = (
                    _read_adopted_quarantine_state(
                        control=control,
                        descriptor=descriptor,
                        descriptor_hash=descriptor_hash,
                        termination_bytes=termination_bytes,
                    )
                )
                (
                    last_handle_close_intent_hash,
                    last_handle_closed_hash,
                ) = _read_adopted_last_handle_state(
                    control=control,
                    descriptor=descriptor,
                    descriptor_bytes=descriptor_bytes,
                    reconciliation=reconciliation,
                )

                record = _PreparedRecord(
                    prepared=prepared,
                    operation=descriptor.operation,
                    controller=_ControllerIdentity(
                        pid=descriptor.controller_pid,
                        creation_time=descriptor.controller_creation_time,
                    ),
                    execution_id=descriptor.execution_id,
                    attempt_id=descriptor.attempt_id,
                    control_id=descriptor.control_id,
                    job_instance_id=descriptor.job_instance_id,
                    control_directory=control,
                    directory=attempt,
                    pin_scope=prepared.pin_scope,
                    container_hash=descriptor_hash,
                    phase=descriptor.phase,
                    previous_phase=descriptor.previous_phase,
                    previous_descriptor_sha256=descriptor.previous_descriptor_sha256,
                    request_sha256=descriptor.request_sha256,
                    root_pid=descriptor.root_pid,
                    root_creation_time=descriptor.root_creation_time,
                    claim_bound=binding is not None,
                    claim_binding=binding,
                    claim_acquired=True,
                    adopted_after_restart=True,
                    outcome=outcome,
                    reconciliation=reconciliation,
                    reconciliation_hash=reconciliation_hash,
                    quarantine_hash=quarantine_hash,
                    blocked=quarantine_hash is not None,
                    termination_outcome_hash=termination_outcome_hash,
                    quarantine_resolution_hash=quarantine_resolution_hash,
                    last_handle_close_intent_hash=last_handle_close_intent_hash,
                    last_handle_closed_hash=last_handle_closed_hash,
                )
                _validate_prepared_snapshot(record, prepared)
                _match_current_descriptor(descriptor, record)
                _assert_durable_publication_reconciliation(record)
                if outcome is not None:
                    _validate_outcome_for_record(
                        record,
                        outcome,
                        code="adoption_outcome_invalid",
                    )
                    _assert_durable_termination_outcome(record, outcome)
                    _assert_outcome_reconciliation_binding(
                        record,
                        outcome,
                        allow_inference=True,
                    )
                _assert_restart_descriptor_tip(
                    control=control,
                    expected_descriptor_bytes=descriptor_bytes,
                )
                if _read_adopted_request(
                    attempt=attempt,
                    descriptor=descriptor,
                    code_prefix="adoption",
                ) != request_bytes:
                    raise MaterialOperationRunnerError(
                        "adoption_request_changed",
                        "durable request changed before adoption registration",
                    )
                if _read_verified_bytes(
                    parent=control,
                    filename=TERMINATION_FILENAME,
                    max_bytes=MAX_TERMINATION_BYTES,
                ) != termination_bytes:
                    raise MaterialOperationRunnerError(
                        "adoption_reconciliation_changed",
                        "termination journal changed before adoption registration",
                    )
                if existing_retirement_bytes is not None:
                    current_retirement_bytes = _read_verified_bytes(
                        parent=control,
                        filename=RETIRED_FILENAME,
                        max_bytes=MAX_RETIREMENT_BYTES,
                    )
                    if current_retirement_bytes != existing_retirement_bytes:
                        raise MaterialOperationRunnerError(
                            "restart_retirement_tombstone_mismatch",
                            "retirement tombstone changed during restart adoption",
                        )
                    assert retirement_proof is not None
                    _validate_restart_retirement_tombstone_for_adoption(
                        current_retirement_bytes,
                        execution_id=execution_id,
                        attempt_id=attempt_id,
                        control_id=control_id,
                        job_instance_id=job_instance_id,
                        operation=expected_operation,
                        proof=retirement_proof,
                    )
                current_outcome, current_outcome_hash = _read_adopted_outcome(
                    prepared=prepared,
                    operation=descriptor.operation,
                )
                if (
                    current_outcome != outcome
                    or current_outcome_hash != termination_outcome_hash
                ):
                    raise MaterialOperationRunnerError(
                        "adoption_outcome_changed",
                        "terminal outcome changed before adoption registration",
                    )
                current_quarantine = _read_adopted_quarantine_state(
                    control=control,
                    descriptor=descriptor,
                    descriptor_hash=descriptor_hash,
                    termination_bytes=termination_bytes,
                )
                if current_quarantine != (
                    quarantine_hash,
                    quarantine_resolution_hash,
                ):
                    raise MaterialOperationRunnerError(
                        "adoption_quarantine_changed",
                        "quarantine evidence changed before adoption registration",
                    )
                current_last_handle = _read_adopted_last_handle_state(
                    control=control,
                    descriptor=descriptor,
                    descriptor_bytes=descriptor_bytes,
                    reconciliation=reconciliation,
                )
                if current_last_handle != (
                    last_handle_close_intent_hash,
                    last_handle_closed_hash,
                ):
                    raise MaterialOperationRunnerError(
                        "adoption_reconciliation_changed",
                        "last-handle evidence changed before adoption registration",
                    )
                for pin in pins:
                    pin.verify()
                ownership_guard.assert_owned()
                self._records[id(prepared)] = record
                return AdoptedMaterialAttempt(
                    prepared=prepared,
                    descriptor=descriptor,
                    request=request,
                    reconciliation=reconciliation,
                    reconciliation_sha256=reconciliation_hash,
                    native_release_basis=_native_release_basis(reconciliation),
                    outcome=outcome,
                    binding=binding,
                    ownership_guard=ownership_guard,
                )
            except BaseException:
                _close_pins(pins)
                raise

    def bind_claim(
        self,
        prepared: PreparedMaterialAttempt,
        binding: MaterialRunnerClaimBinding,
    ) -> None:
        """Bind a fresh DB claim receipt before crossing the run boundary.

        This method never performs a database write and never releases a
        runner pin.  The caller must keep its claim lease/mutex alive while it
        subsequently calls :meth:`run` (or :meth:`run_claimed`).
        """

        if type(binding) is not MaterialRunnerClaimBinding:
            raise TypeError("binding must be MaterialRunnerClaimBinding")
        with self._lock:
            self._ensure_open()
            record = self._record_for(prepared)
            if record.retired:
                raise MaterialOperationRunnerError(
                    "attempt_already_retired",
                    "a retired attempt cannot accept a claim binding",
                )
            if record.claim_acquired or record.claim_bound:
                raise MaterialOperationRunnerError(
                    "claim_already_bound",
                    "this attempt already has a claim boundary",
                )
            _validate_prepared_snapshot(record, prepared)
            if (
                binding.execution_id != record.execution_id
                or binding.attempt_id != record.attempt_id
                or binding.control_id != record.control_id
                or binding.job_instance_id != record.job_instance_id
            ):
                raise MaterialOperationRunnerError(
                    "claim_binding_identity_mismatch",
                    "claim receipt identities do not match the prepared attempt",
                )
            descriptor_bytes = _read_verified_bytes(
                parent=record.control_directory,
                filename=CONTAINER_FILENAME,
                max_bytes=MAX_DESCRIPTOR_BYTES,
            )
            if hashlib.sha256(descriptor_bytes).hexdigest() != record.container_hash:
                raise MaterialOperationRunnerError(
                    "descriptor_changed",
                    "claim binding descriptor no longer matches its receipt",
                )
            descriptor = decode_material_container_descriptor(descriptor_bytes)
            _match_current_descriptor(descriptor, record)
            if descriptor.phase is not MaterialLaunchPhase.PLANNED:
                raise MaterialOperationRunnerError(
                    "claim_binding_descriptor_phase_invalid",
                    "a claim may bind only before the runner crosses its launch boundary",
                )
            record.claim_bound = True
            record.claim_binding = binding

    def claim_binding(
        self,
        prepared: PreparedMaterialAttempt,
    ) -> MaterialRunnerClaimBinding | None:
        """Return the immutable claim receipt bound to an attempt, if any."""

        with self._lock:
            record = self._record_for(prepared)
            _validate_prepared_snapshot(record, prepared)
            return record.claim_binding

    def run_claimed(
        self,
        prepared: PreparedMaterialAttempt,
        request: MaterialOperationRequest,
        *,
        binding: MaterialRunnerClaimBinding,
        secrets: MaterialOperationSecrets,
        startup_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> MaterialOperationOutcome:
        """Bind a typed claim and run without the legacy unclaimed escape hatch."""

        existing_binding = self.claim_binding(prepared)
        if existing_binding is None:
            self.bind_claim(prepared, binding)
        elif existing_binding != binding:
            raise MaterialOperationRunnerError(
                "claim_binding_identity_mismatch",
                "a different claim binding is already attached to this attempt",
            )
        return self.run(
            prepared,
            request,
            secrets=secrets,
            startup_timeout_seconds=startup_timeout_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
        )

    def retire_unstarted_attempt(self, prepared: PreparedMaterialAttempt) -> None:
        with self._lock:
            self._ensure_open()
            record = self._record_for(prepared)
            if record.retired:
                raise MaterialOperationRunnerError(
                    "attempt_already_retired",
                    "the unstarted attempt already has a retirement tombstone",
                )
            if record.claim_acquired or record.claim_bound:
                raise MaterialOperationRunnerError(
                    "claim_already_acquired",
                    "only an attempt without a database claim may retire here",
                )
            _validate_prepared_snapshot(record, prepared)
            for pin in record.pin_scope:
                pin.verify()

            descriptor_bytes = _read_verified_bytes(
                parent=record.control_directory,
                filename=CONTAINER_FILENAME,
                max_bytes=MAX_DESCRIPTOR_BYTES,
            )
            descriptor_sha256 = hashlib.sha256(descriptor_bytes).hexdigest()
            if descriptor_sha256 != record.container_hash:
                raise MaterialOperationRunnerError(
                    "descriptor_changed",
                    "planned descriptor bytes no longer match the prepared receipt",
                )
            descriptor = decode_material_container_descriptor(descriptor_bytes)
            if descriptor.phase is not MaterialLaunchPhase.PLANNED:
                raise MaterialOperationRunnerError(
                    "descriptor_phase_invalid",
                    "only planned may retire before claim acquisition",
                )
            _match_current_descriptor(descriptor, record)

            retirement = {
                "protocol_version": DESCRIPTOR_PROTOCOL_VERSION,
                "state": "retired",
                "reason_code": "claim_not_acquired",
                "execution_id": record.execution_id,
                "attempt_id": record.attempt_id,
                "control_id": record.control_id,
                "job_instance_id": record.job_instance_id,
                "operation": record.operation,
                "controller_pid": record.controller.pid,
                "controller_creation_time": record.controller.creation_time,
                "container_sha256": record.container_hash,
                "retired_at": _utc_now(),
            }
            self._persist_retirement_and_release(
                record,
                retirement,
            )

    def retire_published_attempt(
        self,
        prepared: PreparedMaterialAttempt,
        *,
        publication_journal_sha256: str,
        completion_proof: MaterialRestartPublishedRetirementProof | None = None,
    ) -> MaterialRunnerRetirementAttestation | None:
        """Durably retire a successfully published claimed attempt.

        This is the only public runner seam allowed to release pins after a
        worker crossed the claim boundary.  The caller must have completed the
        publisher's database completion CAS and pass the exact hash of the
        committed publication journal.  A failed tombstone write or any
        identity mismatch leaves the pins owned and the attempt non-retired.
        """

        _validate_sha256(
            publication_journal_sha256,
            code="publication_journal_invalid",
            detail="publication journal hash must be a lowercase SHA-256 digest",
        )
        if completion_proof is not None:
            if type(completion_proof) is not MaterialRestartPublishedRetirementProof:
                raise TypeError(
                    "completion_proof must be MaterialRestartPublishedRetirementProof"
                )
            completion_proof.__post_init__()
            if (
                not completion_proof.checkpoint_sha256
                or completion_proof.publication_journal_sha256
                != publication_journal_sha256
            ):
                raise MaterialOperationRunnerError(
                    "publication_retirement_proof_invalid",
                    "published retirement requires the exact checkpoint and journal proof",
                )
        with self._lock:
            self._ensure_open()
            record = self._record_for(prepared)
            if record.retired:
                raise MaterialOperationRunnerError(
                    "attempt_already_retired",
                    "the claimed attempt already has a retirement tombstone",
                )
            _validate_prepared_snapshot(record, prepared)
            if not record.claim_acquired:
                raise MaterialOperationRunnerError(
                    "claim_not_acquired",
                    "published retirement requires a claimed attempt",
                )
            if record.blocked:
                raise MaterialOperationRunnerError(
                    "publication_attempt_blocked",
                    "a quarantined or blocked attempt cannot be published",
                )
            outcome = record.outcome
            if outcome is None:
                raise MaterialOperationRunnerError(
                    "publication_outcome_unavailable",
                    "published retirement requires a terminal worker outcome",
                )
            _validate_outcome_for_record(
                record,
                outcome,
                code="publication_outcome_invalid",
            )
            if completion_proof is not None:
                binding = record.claim_binding
                if (
                    binding is None
                    or completion_proof.execution_id != record.execution_id
                    or completion_proof.attempt_id != record.attempt_id
                    or completion_proof.operation != record.operation
                    or completion_proof.execution_revision
                    != binding.execution_revision + 2
                    or completion_proof.publication_reference_base_revision
                    != binding.execution_revision
                    or completion_proof.publication_reference_base_payload_sha256
                    != binding.payload_sha256
                    or completion_proof.final_payload_sha256
                    == binding.payload_sha256
                    or completion_proof.observed_at
                    != outcome.termination.observed_at
                ):
                    raise MaterialOperationRunnerError(
                        "publication_retirement_proof_mismatch",
                        "completion proof does not bind the in-process publication owner",
                    )
                result_sha256 = hashlib.sha256(
                    encode_material_worker_result(outcome.result)
                ).hexdigest()
                if (
                    completion_proof.result_sha256 != result_sha256
                    or completion_proof.publication_worker_result_sha256
                    != result_sha256
                ):
                    raise MaterialOperationRunnerError(
                        "publication_retirement_proof_mismatch",
                        "completion proof differs from the durable worker result",
                    )
            if record.termination_outcome_hash is None:
                raise MaterialOperationRunnerError(
                    "publication_outcome_unavailable",
                    "published retirement requires durable termination evidence",
                )
            _assert_durable_termination_outcome(record, outcome)
            if (
                outcome.status != "succeeded"
                or outcome.result is None
                or outcome.result.status != "succeeded"
                or outcome.termination.termination_method != "natural"
            ):
                raise MaterialOperationRunnerError(
                    "publication_outcome_invalid",
                    "only a naturally completed successful outcome may retire",
                )
            if record.phase is not MaterialLaunchPhase.READY:
                raise MaterialOperationRunnerError(
                    "publication_descriptor_phase_invalid",
                    "published retirement requires the READY descriptor tip",
                )
            if record.reconciliation is None or record.reconciliation_hash is None:
                raise MaterialOperationRunnerError(
                    "publication_reconciliation_unavailable",
                    "published retirement requires durable reconciliation evidence",
                )
            record.reconciliation.__post_init__()
            _assert_durable_publication_reconciliation(record)
            _assert_outcome_reconciliation_binding(record, outcome)
            if (
                record.reconciliation.active_processes != 0
                or record.reconciliation.method != "queried_empty"
            ):
                raise MaterialOperationRunnerError(
                    "publication_reconciliation_invalid",
                    "published retirement requires a queried-empty zero-process proof",
                )
            _assert_native_release_state(record, code="publication_native_ownership_invalid")
            if (
                not record.completion_resources_closed
                or not record.completion_close_attempted
                or record.io_drain_pending
                or record.pipe_close_unknown
                or not record.pipe_close_attempted
                or not record.pipe_closed
                or (
                    record.process_quarantine_owner is not None
                    and not record.process_quarantine_cleaned
                )
                or (
                    record.last_handle_close_attempted
                    and not record.last_handle_closed
                )
            ):
                raise MaterialOperationRunnerError(
                    "publication_native_ownership_unresolved",
                    "native runner ownership is not fully closed",
                )
            for pin in record.pin_scope:
                pin.verify()

            descriptor_bytes = _read_verified_bytes(
                parent=record.control_directory,
                filename=CONTAINER_FILENAME,
                max_bytes=MAX_DESCRIPTOR_BYTES,
            )
            descriptor_hash = hashlib.sha256(descriptor_bytes).hexdigest()
            if descriptor_hash != record.container_hash:
                raise MaterialOperationRunnerError(
                    "descriptor_changed",
                    "published retirement descriptor no longer matches its receipt",
                )
            descriptor = decode_material_container_descriptor(descriptor_bytes)
            _match_current_descriptor(descriptor, record)
            if descriptor.phase is not MaterialLaunchPhase.READY:
                raise MaterialOperationRunnerError(
                    "publication_descriptor_phase_invalid",
                    "published retirement descriptor is not READY",
                )

            journal_bytes = _read_verified_bytes(
                parent=record.directory,
                filename=PUBLICATION_JOURNAL_FILENAME,
                max_bytes=MAX_PUBLICATION_JOURNAL_BYTES,
            )
            if hashlib.sha256(journal_bytes).hexdigest() != publication_journal_sha256:
                raise MaterialOperationRunnerError(
                    "publication_journal_changed",
                    "publication journal bytes do not match the supplied receipt",
                )
            _validate_committed_publication_journal(
                journal_bytes,
                record=record,
            )
            _assert_result_matches_publication_journal(
                journal_bytes,
                result=outcome.result,
                record=record,
            )
            _verify_committed_publication_targets(journal_bytes, record=record)

            retirement = {
                "protocol_version": DESCRIPTOR_PROTOCOL_VERSION,
                "state": "retired",
                "reason_code": "publication_committed",
                "execution_id": record.execution_id,
                "attempt_id": record.attempt_id,
                "control_id": record.control_id,
                "job_instance_id": record.job_instance_id,
                "operation": record.operation,
                "controller_pid": record.controller.pid,
                "controller_creation_time": record.controller.creation_time,
                "container_sha256": record.container_hash,
                "reconciliation_sha256": record.reconciliation_hash,
                "publication_journal_sha256": publication_journal_sha256,
                "retired_at": _utc_now(),
            }
            if completion_proof is not None:
                retirement.update(
                    {
                        "attempt_history_sha256": (
                            completion_proof.attempt_history_sha256
                        ),
                        "final_payload_sha256": completion_proof.final_payload_sha256,
                        "completion_revision": completion_proof.execution_revision,
                        "completion_result_sha256": completion_proof.result_sha256,
                        "completion_observed_at": completion_proof.observed_at,
                        "checkpoint_sha256": completion_proof.checkpoint_sha256,
                    }
                )
            persisted_bytes = self._persist_retirement_and_release(
                record,
                retirement,
            )
            if completion_proof is None:
                return None
            return _build_runner_retirement_attestation(
                record=record,
                proof=completion_proof,
                persisted_bytes=persisted_bytes,
                release_basis="in_process_close",
            )

    def attest_completed_published_attempt(
        self,
        prepared: PreparedMaterialAttempt,
        *,
        proof: MaterialRestartPublishedRetirementProof,
    ) -> MaterialRunnerRetirementAttestation:
        """Retire one completed publication and return its ack-ready proof."""

        if type(proof) is not MaterialRestartPublishedRetirementProof:
            raise TypeError("proof must be MaterialRestartPublishedRetirementProof")
        proof.__post_init__()
        if not proof.checkpoint_sha256:
            raise MaterialOperationRunnerError(
                "publication_retirement_proof_invalid",
                "completion proof has no runner-retirement checkpoint",
            )
        attestation = self.retire_published_attempt(
            prepared,
            publication_journal_sha256=proof.publication_journal_sha256,
            completion_proof=proof,
        )
        if type(attestation) is not MaterialRunnerRetirementAttestation:
            raise MaterialOperationRunnerError(
                "publication_retirement_attestation_missing",
                "published retirement returned no typed attestation",
            )
        return attestation

    def retire_adopted_published_attempt(
        self,
        adopted: AdoptedMaterialAttempt,
        *,
        proof: MaterialRestartPublishedRetirementProof,
        publication_ownership: object,
    ) -> MaterialRunnerRetirementAttestation | None:
        """Retire a published success adopted after controller restart.

        The publication itself is never replayed here.  The caller must hold
        the recovery owner and publisher target locks, and must first finish
        the exact database completion CAS.  This gate then re-proves the
        immutable worker result, completion intent, committed destinations,
        and final DB receipt before it writes the runner tombstone and releases
        the adopted directory pins.
        """

        if type(adopted) is not AdoptedMaterialAttempt:
            raise TypeError("adopted must be AdoptedMaterialAttempt")
        if type(proof) is not MaterialRestartPublishedRetirementProof:
            raise TypeError(
                "proof must be MaterialRestartPublishedRetirementProof"
            )
        if not callable(
            getattr(publication_ownership, "assert_retained_ownership", None)
        ):
            raise TypeError(
                "publication_ownership must expose assert_retained_ownership"
            )
        adopted.__post_init__()
        proof.__post_init__()
        with self._lock:
            self._ensure_open()
            adopted.ownership_guard.assert_owned()
            record = self._record_for(adopted.prepared)
            _validate_prepared_snapshot(record, adopted.prepared)
            if record.retired:
                raise MaterialOperationRunnerError(
                    "attempt_already_retired",
                    "the adopted published attempt already has a retirement tombstone",
                )
            if not record.adopted_after_restart:
                raise MaterialOperationRunnerError(
                    "restart_publication_retirement_invalid",
                    "only a restart-adopted record may use this retirement gate",
                )
            binding = record.claim_binding
            if binding is None or adopted.binding != binding:
                raise MaterialOperationRunnerError(
                    "restart_publication_binding_missing",
                    "published restart retirement requires the adopted publishing owner",
                )
            if (
                adopted.reconciliation != record.reconciliation
                or adopted.reconciliation_sha256 != record.reconciliation_hash
                or adopted.outcome != record.outcome
                or adopted.request.execution_id != record.execution_id
                or adopted.request.attempt_id != record.attempt_id
                or adopted.request.operation != record.operation
            ):
                raise MaterialOperationRunnerError(
                    "restart_publication_adoption_mismatch",
                    "public adoption receipt differs from the runner-owned record",
                )
            if (
                proof.execution_id != record.execution_id
                or proof.attempt_id != record.attempt_id
                or proof.operation != record.operation
            ):
                raise MaterialOperationRunnerError(
                    "restart_published_proof_mismatch",
                    "database completion proof belongs to another attempt",
                )
            if proof.execution_revision != binding.execution_revision + 1:
                raise MaterialOperationRunnerError(
                    "restart_published_proof_stale",
                    "database completion revision did not advance exactly once from publishing",
                )
            if (
                binding.execution_revision
                != proof.publication_reference_base_revision + 1
                or proof.publication_worker_result_sha256 != proof.result_sha256
            ):
                raise MaterialOperationRunnerError(
                    "restart_published_proof_mismatch",
                    "database publication receipts do not bind the publishing revision and worker result",
                )
            if proof.final_payload_sha256 == binding.payload_sha256:
                raise MaterialOperationRunnerError(
                    "restart_published_proof_stale",
                    "database completion payload did not change from publishing",
                )
            if record.blocked or not record.claim_acquired:
                raise MaterialOperationRunnerError(
                    "restart_publication_attempt_blocked",
                    "the adopted attempt is not an active unblocked publishing owner",
                )
            outcome = record.outcome
            if (
                outcome is None
                or outcome.status != "succeeded"
                or outcome.result is None
                or outcome.result.status != "succeeded"
                or outcome.termination.termination_method != "natural"
            ):
                raise MaterialOperationRunnerError(
                    "restart_publication_outcome_invalid",
                    "published restart retirement requires natural worker success",
                )
            if outcome.termination.observed_at != proof.observed_at:
                raise MaterialOperationRunnerError(
                    "restart_published_proof_mismatch",
                    "database completion time differs from durable termination",
                )
            result_sha256 = hashlib.sha256(
                encode_material_worker_result(outcome.result)
            ).hexdigest()
            if result_sha256 != proof.result_sha256:
                raise MaterialOperationRunnerError(
                    "restart_published_proof_mismatch",
                    "database result receipt differs from the durable worker result",
                )
            if (
                record.reconciliation is None
                or record.reconciliation_hash is None
                or record.termination_outcome_hash is None
            ):
                raise MaterialOperationRunnerError(
                    "restart_publication_evidence_unavailable",
                    "adopted publication evidence is incomplete",
                )
            if (
                record.reconciliation.method != "queried_empty"
                or record.reconciliation.active_processes != 0
                or adopted.native_release_basis
                != "queried_empty_dead_controller"
            ):
                raise MaterialOperationRunnerError(
                    "restart_publication_reconciliation_invalid",
                    "published restart retirement requires a queried-empty process tree",
                )
            if any(
                owner is not None
                for owner in (
                    record.job_owner,
                    record.pipe_owner,
                    record.process_owner,
                    record.process_quarantine_owner,
                )
            ) or record.io_drain_pending:
                raise MaterialOperationRunnerError(
                    "restart_publication_native_owner_present",
                    "restart retirement cannot own live process-local resources",
                )

            descriptor_bytes = _read_verified_bytes(
                parent=record.control_directory,
                filename=CONTAINER_FILENAME,
                max_bytes=MAX_DESCRIPTOR_BYTES,
            )
            if hashlib.sha256(descriptor_bytes).hexdigest() != record.container_hash:
                raise MaterialOperationRunnerError(
                    "descriptor_changed",
                    "restart publication descriptor differs from adoption",
                )
            descriptor = decode_material_container_descriptor(descriptor_bytes)
            _match_current_descriptor(descriptor, record)
            if (
                descriptor != adopted.descriptor
                or descriptor.phase is not MaterialLaunchPhase.READY
            ):
                raise MaterialOperationRunnerError(
                    "adoption_descriptor_changed",
                    "restart publication descriptor differs from adopted READY evidence",
                )
            request_bytes = _read_adopted_request(
                attempt=record.directory,
                descriptor=descriptor,
                code_prefix="restart_publication_retirement",
            )
            if request_bytes != encode_material_operation_request(adopted.request):
                raise MaterialOperationRunnerError(
                    "restart_publication_request_changed",
                    "durable request bytes changed after adoption",
                )
            _prove_adopted_processes_exited(descriptor)
            _assert_durable_publication_reconciliation(record)
            _assert_durable_termination_outcome(record, outcome)
            _validate_outcome_for_record(
                record,
                outcome,
                code="restart_publication_outcome_invalid",
            )
            _assert_outcome_reconciliation_binding(
                record,
                outcome,
                allow_inference=True,
            )
            current_outcome, current_outcome_hash = _read_adopted_outcome(
                prepared=record.prepared,
                operation=record.operation,
            )
            if (
                current_outcome != outcome
                or current_outcome_hash != record.termination_outcome_hash
            ):
                raise MaterialOperationRunnerError(
                    "restart_publication_outcome_changed",
                    "durable successful outcome changed after adoption",
                )
            termination_bytes = _read_verified_bytes(
                parent=record.control_directory,
                filename=TERMINATION_FILENAME,
                max_bytes=MAX_TERMINATION_BYTES,
            )
            if _read_adopted_quarantine_state(
                control=record.control_directory,
                descriptor=descriptor,
                descriptor_hash=record.container_hash,
                termination_bytes=termination_bytes,
            ) != (record.quarantine_hash, record.quarantine_resolution_hash):
                raise MaterialOperationRunnerError(
                    "restart_publication_quarantine_changed",
                    "durable quarantine state changed after adoption",
                )
            if _read_adopted_last_handle_state(
                control=record.control_directory,
                descriptor=descriptor,
                descriptor_bytes=descriptor_bytes,
                reconciliation=record.reconciliation,
            ) != (
                record.last_handle_close_intent_hash,
                record.last_handle_closed_hash,
            ):
                raise MaterialOperationRunnerError(
                    "restart_publication_reconciliation_changed",
                    "last-handle state changed after adoption",
                )

            _assert_retained_publication_owner(
                publication_ownership,
                record=record,
                proof=proof,
            )
            journal_bytes = _read_verified_bytes(
                parent=record.directory,
                filename=PUBLICATION_JOURNAL_FILENAME,
                max_bytes=MAX_PUBLICATION_JOURNAL_BYTES,
            )
            if (
                hashlib.sha256(journal_bytes).hexdigest()
                != proof.publication_journal_sha256
            ):
                raise MaterialOperationRunnerError(
                    "publication_journal_changed",
                    "publication journal bytes do not match the final DB proof",
                )
            _validate_committed_publication_journal(journal_bytes, record=record)
            journal_value = _decode_strict_bounded_json(
                journal_bytes,
                max_bytes=MAX_PUBLICATION_JOURNAL_BYTES,
                code="publication_journal_invalid",
                detail="publication journal is not strict bounded JSON",
            )
            if (
                type(journal_value) is not dict
                or type(journal_value.get("completion_intent")) is not dict
            ):
                raise MaterialOperationRunnerError(
                    "publication_journal_invalid",
                    "restart publication requires a durable completion intent",
                )
            _assert_result_matches_publication_journal(
                journal_bytes,
                result=outcome.result,
                record=record,
                proof=proof,
            )
            _verify_committed_publication_targets(journal_bytes, record=record)
            _assert_restart_descriptor_tip(
                control=record.control_directory,
                expected_descriptor_bytes=descriptor_bytes,
            )
            _assert_retained_publication_owner(
                publication_ownership,
                record=record,
                proof=proof,
            )
            for pin in record.pin_scope:
                pin.verify()
            adopted.ownership_guard.assert_owned()
            retirement = {
                "protocol_version": DESCRIPTOR_PROTOCOL_VERSION,
                "state": "retired",
                "reason_code": "publication_committed_after_restart",
                "execution_id": record.execution_id,
                "attempt_id": record.attempt_id,
                "control_id": record.control_id,
                "job_instance_id": record.job_instance_id,
                "operation": record.operation,
                "controller_pid": record.controller.pid,
                "controller_creation_time": record.controller.creation_time,
                "container_sha256": record.container_hash,
                "reconciliation_sha256": record.reconciliation_hash,
                "termination_outcome_sha256": record.termination_outcome_hash,
                "publication_journal_sha256": proof.publication_journal_sha256,
                "attempt_history_sha256": proof.attempt_history_sha256,
                "final_payload_sha256": proof.final_payload_sha256,
                "completion_revision": proof.execution_revision,
                "completion_result_sha256": proof.result_sha256,
                "completion_observed_at": proof.observed_at,
                "native_release_basis": adopted.native_release_basis,
                "retired_at": _utc_now(),
            }
            if proof.checkpoint_sha256:
                retirement["checkpoint_sha256"] = proof.checkpoint_sha256
            persisted_bytes = self._persist_retirement_and_release(
                record,
                retirement,
                ownership_guard=adopted.ownership_guard,
            )
            if not proof.checkpoint_sha256:
                return None
            return _build_runner_retirement_attestation(
                record=record,
                proof=proof,
                persisted_bytes=persisted_bytes,
                release_basis="dead_controller_after_tombstone",
            )

    def retire_terminal_attempt(
        self,
        prepared: PreparedMaterialAttempt,
        *,
        attempt_history_sha256: str | None = None,
        completion_proof: MaterialRestartRetirementProof | None = None,
    ) -> MaterialAttemptRetirementReceipt | None:
        """Retire a claimed failed/terminated attempt after tree-empty proof.

        The caller must complete its exact database history CAS before calling
        this seam.  This method only owns the runner-side durable tombstone and
        pin release; it never starts fallback or edits the database.
        """

        if completion_proof is not None:
            if type(completion_proof) is not MaterialRestartRetirementProof:
                raise TypeError("completion_proof must be MaterialRestartRetirementProof")
            completion_proof.__post_init__()
            if attempt_history_sha256 is not None and (
                attempt_history_sha256 != completion_proof.attempt_history_sha256
            ):
                raise MaterialOperationRunnerError(
                    "terminal_retirement_invalid",
                    "completion proof and attempt history receipt differ",
                )
            attempt_history_sha256 = completion_proof.attempt_history_sha256
        if attempt_history_sha256 is not None:
            _validate_sha256(
                attempt_history_sha256,
                code="terminal_retirement_invalid",
                detail="attempt history receipt must be a lowercase SHA-256 digest",
            )
        with self._lock:
            self._ensure_open()
            record = self._record_for(prepared)
            if record.retired:
                raise MaterialOperationRunnerError(
                    "attempt_already_retired",
                    "the claimed attempt already has a retirement tombstone",
                )
            _validate_prepared_snapshot(record, prepared)
            if not record.claim_acquired:
                raise MaterialOperationRunnerError(
                    "claim_not_acquired",
                    "terminal retirement requires a claimed attempt",
                )
            if record.blocked:
                raise MaterialOperationRunnerError(
                    "terminal_attempt_blocked",
                    "a quarantined or blocked attempt cannot be retired as ordinary terminal work",
                )
            outcome = record.outcome
            if outcome is None or outcome.status not in {"failed", "terminated"}:
                raise MaterialOperationRunnerError(
                    "terminal_outcome_invalid",
                    "terminal retirement requires a failed or terminated outcome",
                )
            _validate_outcome_for_record(
                record,
                outcome,
                code="terminal_outcome_invalid",
            )
            if outcome.status == "failed" and (
                outcome.result is None
                or outcome.result.status != "failed"
                or outcome.termination.termination_method != "natural"
            ):
                raise MaterialOperationRunnerError(
                    "terminal_outcome_invalid",
                    "failed retirement requires a naturally completed failed result",
                )
            if outcome.status == "terminated" and outcome.result is not None:
                raise MaterialOperationRunnerError(
                    "terminal_outcome_invalid",
                    "terminated retirement cannot carry a worker result",
                )
            if completion_proof is not None:
                if (
                    completion_proof.execution_id != record.execution_id
                    or completion_proof.attempt_id != record.attempt_id
                    or completion_proof.operation != record.operation
                    or completion_proof.status != outcome.status
                    or completion_proof.observed_at != outcome.termination.observed_at
                ):
                    raise MaterialOperationRunnerError(
                        "terminal_retirement_proof_mismatch",
                        "completion proof does not belong to the terminal runner attempt",
                    )
                if record.claim_binding is not None and (
                    completion_proof.execution_revision
                    != record.claim_binding.execution_revision + 1
                    or completion_proof.final_payload_sha256
                    == record.claim_binding.payload_sha256
                ):
                    raise MaterialOperationRunnerError(
                        "terminal_retirement_proof_stale",
                        "completion proof is not the next fresh database revision",
                    )
                result_sha256 = (
                    None
                    if outcome.result is None
                    else hashlib.sha256(
                        encode_material_worker_result(outcome.result)
                    ).hexdigest()
                )
                if result_sha256 != completion_proof.result_sha256:
                    raise MaterialOperationRunnerError(
                        "terminal_retirement_proof_mismatch",
                        "completion proof result receipt differs from the durable outcome",
                    )
            if record.termination_outcome_hash is None:
                raise MaterialOperationRunnerError(
                    "terminal_outcome_unavailable",
                    "terminal retirement requires durable termination evidence",
                )
            _assert_durable_termination_outcome(record, outcome)
            if record.reconciliation is None or record.reconciliation_hash is None:
                raise MaterialOperationRunnerError(
                    "terminal_reconciliation_unavailable",
                    "terminal retirement requires durable reconciliation evidence",
                )
            record.reconciliation.__post_init__()
            _assert_durable_publication_reconciliation(record)
            _assert_outcome_reconciliation_binding(
                record,
                outcome,
                allow_inference=True,
            )
            if record.reconciliation.method == "queried_empty":
                reconciliation_is_terminal = record.reconciliation.active_processes == 0
            elif record.reconciliation.method in {
                "owner_exit_kill_on_close",
                "last_handle_close_kill_on_close",
            }:
                reconciliation_is_terminal = (
                    outcome.status == "terminated"
                    and record.reconciliation.active_processes is None
                )
            else:
                reconciliation_is_terminal = False
            if not reconciliation_is_terminal:
                raise MaterialOperationRunnerError(
                    "terminal_reconciliation_invalid",
                    "terminal retirement requires a typed zero-process or owner-exit proof",
                )
            _assert_native_release_state(record, code="terminal_native_ownership_invalid")
            if (
                not record.completion_resources_closed
                or not record.completion_close_attempted
                or record.io_drain_pending
                or record.pipe_close_unknown
                or not record.pipe_close_attempted
                or not record.pipe_closed
                or (
                    record.process_quarantine_owner is not None
                    and not record.process_quarantine_cleaned
                )
                or (
                    record.last_handle_close_attempted
                    and not record.last_handle_closed
                )
            ):
                raise MaterialOperationRunnerError(
                    "terminal_native_ownership_unresolved",
                    "terminal retirement requires fully closed native ownership",
                )
            _assert_no_committed_publication_journal(record)
            if attempt_history_sha256 is None:
                raise MaterialOperationRunnerError(
                    "terminal_history_unavailable",
                    "terminal retirement requires an exact database history receipt",
                )
            descriptor_bytes = _read_verified_bytes(
                parent=record.control_directory,
                filename=CONTAINER_FILENAME,
                max_bytes=MAX_DESCRIPTOR_BYTES,
            )
            if hashlib.sha256(descriptor_bytes).hexdigest() != record.container_hash:
                raise MaterialOperationRunnerError(
                    "descriptor_changed",
                    "terminal retirement descriptor no longer matches its receipt",
                )
            descriptor = decode_material_container_descriptor(descriptor_bytes)
            _match_current_descriptor(descriptor, record)
            retirement = {
                "protocol_version": DESCRIPTOR_PROTOCOL_VERSION,
                "state": "retired",
                "reason_code": (
                    "attempt_failed" if outcome.status == "failed" else "attempt_terminated"
                ),
                "execution_id": record.execution_id,
                "attempt_id": record.attempt_id,
                "control_id": record.control_id,
                "job_instance_id": record.job_instance_id,
                "operation": record.operation,
                "controller_pid": record.controller.pid,
                "controller_creation_time": record.controller.creation_time,
                "container_sha256": record.container_hash,
                "reconciliation_sha256": record.reconciliation_hash,
                "termination_outcome_sha256": record.termination_outcome_hash,
                "attempt_history_sha256": attempt_history_sha256,
                "retired_at": _utc_now(),
            }
            if completion_proof is not None:
                retirement.update(
                    {
                        "final_payload_sha256": completion_proof.final_payload_sha256,
                        "completion_revision": completion_proof.execution_revision,
                        "completion_result_sha256": completion_proof.result_sha256,
                        "completion_observed_at": completion_proof.observed_at,
                    }
                )
                if completion_proof.checkpoint_sha256:
                    retirement["checkpoint_sha256"] = (
                        completion_proof.checkpoint_sha256
                    )
            persisted_bytes = self._persist_retirement_and_release(record, retirement)
            if completion_proof is None:
                return None
            return _build_attempt_retirement_receipt(
                record=record,
                proof=completion_proof,
                retirement=retirement,
                persisted_bytes=persisted_bytes,
            )

    def retire_terminal_attempt_with_proof(
        self,
        prepared: PreparedMaterialAttempt,
        *,
        proof: MaterialRestartRetirementProof,
    ) -> MaterialAttemptRetirementReceipt:
        """Retire an in-process terminal attempt and return post-runner proof."""

        receipt = self.retire_terminal_attempt(
            prepared,
            attempt_history_sha256=proof.attempt_history_sha256,
            completion_proof=proof,
        )
        if type(receipt) is not MaterialAttemptRetirementReceipt:
            raise MaterialOperationRunnerError(
                "retirement_receipt_missing",
                "terminal retirement completed without a typed post-runner receipt",
            )
        return receipt

    def attest_completed_terminal_attempt(
        self,
        prepared: PreparedMaterialAttempt,
        *,
        proof: MaterialRestartRetirementProof,
    ) -> MaterialRunnerRetirementAttestation:
        """Retire one final terminal attempt and return its ack-ready proof."""

        if type(proof) is not MaterialRestartRetirementProof:
            raise TypeError("proof must be MaterialRestartRetirementProof")
        proof.__post_init__()
        if not proof.checkpoint_sha256:
            raise MaterialOperationRunnerError(
                "terminal_retirement_proof_invalid",
                "completion proof has no runner-retirement checkpoint",
            )
        receipt = self.retire_terminal_attempt_with_proof(prepared, proof=proof)
        return MaterialRunnerRetirementAttestation(
            status=receipt.status,
            execution_id=receipt.execution_id,
            operation=receipt.operation,
            attempt_id=receipt.attempt_id,
            control_id=receipt.control_id,
            job_instance_id=receipt.job_instance_id,
            completion_revision=receipt.execution_revision,
            completion_payload_sha256=receipt.final_payload_sha256,
            checkpoint_sha256=proof.checkpoint_sha256,
            attempt_history_sha256=receipt.attempt_history_sha256,
            tombstone_sha256=receipt.tombstone_sha256,
            retired_at=receipt.retired_at,
            release_basis="in_process_close",
        )

    def retire_adopted_terminal_attempt(
        self,
        adopted: AdoptedMaterialAttempt,
        *,
        proof: MaterialRestartRetirementProof,
    ) -> MaterialAttemptRetirementReceipt:
        """Retire a restart-adopted terminal attempt from durable DB proof.

        This is deliberately separate from ``retire_terminal_attempt``.  The
        ordinary path relies on process-local close acknowledgements; after a
        controller restart those flags cannot be reconstructed.  Here the
        release basis is re-proven from immutable journals plus exact old
        controller/root exit, and the caller-supplied proof must come from the
        already-completed whole-payload CAS.
        """

        if type(adopted) is not AdoptedMaterialAttempt:
            raise TypeError("adopted must be AdoptedMaterialAttempt")
        if type(proof) is not MaterialRestartRetirementProof:
            raise TypeError("proof must be MaterialRestartRetirementProof")
        adopted.__post_init__()
        proof.__post_init__()
        with self._lock:
            self._ensure_open()
            adopted.ownership_guard.assert_owned()
            record = self._record_for(adopted.prepared)
            _validate_prepared_snapshot(record, adopted.prepared)
            if record.retired:
                raise MaterialOperationRunnerError(
                    "attempt_already_retired",
                    "the adopted attempt already has a retirement tombstone",
                )
            if not record.adopted_after_restart:
                raise MaterialOperationRunnerError(
                    "restart_retirement_invalid",
                    "only a restart-adopted record may use this retirement gate",
                )
            if record.reconciliation is None or record.reconciliation_hash is None:
                raise MaterialOperationRunnerError(
                    "restart_retirement_evidence_unavailable",
                    "adopted reconciliation evidence is incomplete",
                )
            native_release_basis = _native_release_basis(record.reconciliation)
            if (
                adopted.reconciliation != record.reconciliation
                or adopted.reconciliation_sha256 != record.reconciliation_hash
                or adopted.outcome != record.outcome
                or adopted.binding != record.claim_binding
                or adopted.native_release_basis != native_release_basis
            ):
                raise MaterialOperationRunnerError(
                    "restart_retirement_adoption_mismatch",
                    "public adoption receipt differs from the runner-owned record",
                )
            if (
                proof.execution_id != record.execution_id
                or proof.attempt_id != record.attempt_id
                or proof.operation != record.operation
                or proof.status not in {"failed", "terminated"}
            ):
                raise MaterialOperationRunnerError(
                    "restart_retirement_proof_mismatch",
                    "database completion proof belongs to another attempt",
                )
            if record.claim_binding is not None:
                if (
                    proof.execution_revision
                    != record.claim_binding.execution_revision + 1
                ):
                    raise MaterialOperationRunnerError(
                        "restart_retirement_proof_stale",
                        "database completion revision did not advance exactly once from the active claim",
                    )
                if proof.final_payload_sha256 == record.claim_binding.payload_sha256:
                    raise MaterialOperationRunnerError(
                        "restart_retirement_proof_stale",
                        "database completion payload did not change from the active claim",
                    )
            outcome = record.outcome
            if outcome is None or outcome.status != proof.status:
                raise MaterialOperationRunnerError(
                    "restart_retirement_outcome_unavailable",
                    "database completion proof has no matching durable outcome",
                )
            if outcome.termination.observed_at != proof.observed_at:
                raise MaterialOperationRunnerError(
                    "restart_retirement_proof_mismatch",
                    "database completion time differs from durable termination",
                )
            result_sha256 = (
                None
                if outcome.result is None
                else hashlib.sha256(
                    encode_material_worker_result(outcome.result)
                ).hexdigest()
            )
            if result_sha256 != proof.result_sha256:
                raise MaterialOperationRunnerError(
                    "restart_retirement_proof_mismatch",
                    "database result receipt differs from the durable worker result",
                )
            if record.termination_outcome_hash is None:
                raise MaterialOperationRunnerError(
                    "restart_retirement_evidence_unavailable",
                    "adopted terminal evidence is incomplete",
                )
            if any(
                owner is not None
                for owner in (
                    record.job_owner,
                    record.pipe_owner,
                    record.process_owner,
                    record.process_quarantine_owner,
                )
            ) or record.io_drain_pending:
                raise MaterialOperationRunnerError(
                    "restart_retirement_native_owner_present",
                    "restart retirement cannot own live process-local resources",
                )

            descriptor_bytes = _read_verified_bytes(
                parent=record.control_directory,
                filename=CONTAINER_FILENAME,
                max_bytes=MAX_DESCRIPTOR_BYTES,
            )
            if hashlib.sha256(descriptor_bytes).hexdigest() != record.container_hash:
                raise MaterialOperationRunnerError(
                    "descriptor_changed",
                    "restart retirement descriptor differs from adoption",
                )
            descriptor = decode_material_container_descriptor(descriptor_bytes)
            _match_current_descriptor(descriptor, record)
            if descriptor != adopted.descriptor:
                raise MaterialOperationRunnerError(
                    "adoption_descriptor_changed",
                    "restart retirement descriptor differs from adopted evidence",
                )
            _read_adopted_request(
                attempt=record.directory,
                descriptor=descriptor,
                code_prefix="restart_retirement",
            )
            _prove_adopted_processes_exited(descriptor)
            _assert_durable_publication_reconciliation(record)
            _assert_durable_termination_outcome(record, outcome)
            _validate_outcome_for_record(
                record,
                outcome,
                code="restart_retirement_outcome_invalid",
            )
            _assert_outcome_reconciliation_binding(
                record,
                outcome,
                allow_inference=True,
            )
            if record.reconciliation.method == "queried_empty":
                terminal_proof = record.reconciliation.active_processes == 0
            elif record.reconciliation.method in {
                "owner_exit_kill_on_close",
                "last_handle_close_kill_on_close",
            }:
                terminal_proof = record.reconciliation.active_processes is None
            else:
                terminal_proof = False
            if not terminal_proof:
                raise MaterialOperationRunnerError(
                    "restart_retirement_reconciliation_invalid",
                    "restart retirement has no approved terminal process-tree proof",
                )

            current_outcome, current_outcome_hash = _read_adopted_outcome(
                prepared=record.prepared,
                operation=record.operation,
            )
            if (
                current_outcome != outcome
                or current_outcome_hash != record.termination_outcome_hash
            ):
                raise MaterialOperationRunnerError(
                    "restart_retirement_outcome_changed",
                    "durable terminal outcome changed after adoption",
                )
            termination_bytes = _read_verified_bytes(
                parent=record.control_directory,
                filename=TERMINATION_FILENAME,
                max_bytes=MAX_TERMINATION_BYTES,
            )
            current_quarantine = _read_adopted_quarantine_state(
                control=record.control_directory,
                descriptor=descriptor,
                descriptor_hash=record.container_hash,
                termination_bytes=termination_bytes,
            )
            if current_quarantine != (
                record.quarantine_hash,
                record.quarantine_resolution_hash,
            ):
                raise MaterialOperationRunnerError(
                    "restart_retirement_quarantine_changed",
                    "durable quarantine state changed after adoption",
                )
            current_last_handle = _read_adopted_last_handle_state(
                control=record.control_directory,
                descriptor=descriptor,
                descriptor_bytes=descriptor_bytes,
                reconciliation=record.reconciliation,
            )
            if current_last_handle != (
                record.last_handle_close_intent_hash,
                record.last_handle_closed_hash,
            ):
                raise MaterialOperationRunnerError(
                    "restart_retirement_reconciliation_changed",
                    "last-handle state changed after adoption",
                )
            _assert_no_committed_publication_journal(record)
            _assert_restart_descriptor_tip(
                control=record.control_directory,
                expected_descriptor_bytes=descriptor_bytes,
            )
            for pin in record.pin_scope:
                pin.verify()
            adopted.ownership_guard.assert_owned()
            retirement = {
                "protocol_version": DESCRIPTOR_PROTOCOL_VERSION,
                "state": "retired",
                "reason_code": (
                    "attempt_failed_after_restart"
                    if outcome.status == "failed"
                    else "attempt_terminated_after_restart"
                ),
                "execution_id": record.execution_id,
                "attempt_id": record.attempt_id,
                "control_id": record.control_id,
                "job_instance_id": record.job_instance_id,
                "operation": record.operation,
                "controller_pid": record.controller.pid,
                "controller_creation_time": record.controller.creation_time,
                "container_sha256": record.container_hash,
                "reconciliation_sha256": record.reconciliation_hash,
                "termination_outcome_sha256": record.termination_outcome_hash,
                "attempt_history_sha256": proof.attempt_history_sha256,
                "final_payload_sha256": proof.final_payload_sha256,
                "completion_revision": proof.execution_revision,
                "completion_result_sha256": proof.result_sha256,
                "completion_observed_at": proof.observed_at,
                "native_release_basis": native_release_basis,
                "retired_at": _utc_now(),
            }
            if proof.checkpoint_sha256:
                retirement["checkpoint_sha256"] = proof.checkpoint_sha256
            persisted_bytes = self._persist_retirement_and_release(
                record,
                retirement,
                ownership_guard=adopted.ownership_guard,
            )
            return _build_attempt_retirement_receipt(
                record=record,
                proof=proof,
                retirement=retirement,
                persisted_bytes=persisted_bytes,
            )

    def attest_completed_adopted_terminal_attempt(
        self,
        adopted: AdoptedMaterialAttempt,
        *,
        proof: MaterialRestartRetirementProof,
    ) -> MaterialRunnerRetirementAttestation:
        """Retire one adopted terminal attempt and return its ack-ready proof."""

        if type(proof) is not MaterialRestartRetirementProof:
            raise TypeError("proof must be MaterialRestartRetirementProof")
        proof.__post_init__()
        if not proof.checkpoint_sha256:
            raise MaterialOperationRunnerError(
                "terminal_retirement_proof_invalid",
                "completion proof has no runner-retirement checkpoint",
            )
        receipt = self.retire_adopted_terminal_attempt(adopted, proof=proof)
        return MaterialRunnerRetirementAttestation(
            status=receipt.status,
            execution_id=receipt.execution_id,
            operation=receipt.operation,
            attempt_id=receipt.attempt_id,
            control_id=receipt.control_id,
            job_instance_id=receipt.job_instance_id,
            completion_revision=receipt.execution_revision,
            completion_payload_sha256=receipt.final_payload_sha256,
            checkpoint_sha256=proof.checkpoint_sha256,
            attempt_history_sha256=receipt.attempt_history_sha256,
            tombstone_sha256=receipt.tombstone_sha256,
            retired_at=receipt.retired_at,
            release_basis="dead_controller_after_tombstone",
        )

    def _persist_retirement_and_release(
        self,
        record: _PreparedRecord,
        retirement: Mapping[str, object],
        *,
        ownership_guard: MaterialRestartOwnershipGuard | None = None,
    ) -> bytes:
        """Persist one exact tombstone, adopt an uncertain write, then release pins."""

        try:
            if ownership_guard is not None:
                ownership_guard.assert_owned()
            receipt = write_durable_json(
                parent=record.control_directory,
                filename=RETIRED_FILENAME,
                payload=retirement,
                replace=False,
                max_bytes=MAX_RETIREMENT_BYTES,
            )
        except BaseException as write_error:
            # The native replace may have reached disk before its
            # acknowledgement failed.  Adopt only an exact, fresh read-back
            # of this same tombstone; otherwise preserve the uncertainty.
            try:
                persisted_bytes = _read_verified_bytes(
                    parent=record.control_directory,
                    filename=RETIRED_FILENAME,
                    max_bytes=MAX_RETIREMENT_BYTES,
                )
            except BaseException:
                raise write_error
            try:
                _validate_published_retirement_bytes(
                    persisted_bytes,
                    expected=retirement,
                )
            except BaseException as evidence_error:
                try:
                    _validate_persisted_retirement_identity(
                        persisted_bytes,
                        expected=retirement,
                    )
                except BaseException:
                    raise MaterialOperationRunnerError(
                        "publication_retirement_changed",
                        "retirement tombstone after an unknown write is not owned",
                    ) from evidence_error
        else:
            try:
                persisted_bytes = _read_verified_bytes(
                    parent=record.control_directory,
                    filename=RETIRED_FILENAME,
                    max_bytes=MAX_RETIREMENT_BYTES,
                )
            except BaseException as exc:
                raise MaterialOperationRunnerError(
                    "retirement_tombstone_unknown",
                    "retirement tombstone was written but could not be read back",
                ) from exc
            if hashlib.sha256(persisted_bytes).hexdigest() != receipt.sha256:
                raise MaterialOperationRunnerError(
                    "publication_retirement_changed",
                    "retirement tombstone read-back does not match its receipt",
                )
            _validate_published_retirement_bytes(
                persisted_bytes,
                expected=retirement,
            )
        try:
            if ownership_guard is not None:
                ownership_guard.assert_owned()
            for pin in record.pin_scope:
                pin.verify()
        except BaseException as exc:
            # A failed identity check happens before any close call.  Keep the
            # record live-but-blocked so the runner cannot silently abandon
            # the remaining native directory pins; an operator/recovery path
            # must inspect the durable audit sidecar first.
            record.blocked = True
            self._write_retirement_release_audit(
                record,
                persisted_bytes=persisted_bytes,
                reason_code="pin_identity_changed",
            )
            raise MaterialOperationRunnerError(
                "retirement_release_unknown",
                "retirement tombstone is durable but pin ownership changed before close",
            ) from exc
        try:
            if ownership_guard is not None:
                ownership_guard.assert_owned()
            _close_pins(
                record.pin_scope,
                audit_pin=record.control_directory,
            )
        except BaseException as exc:
            # The tombstone is durable, but ownership release is not fully
            # acknowledged.  This is a terminal audit state: a partial close
            # cannot safely be retried from an in-memory pin tuple because a
            # native handle may already have been consumed.
            record.retired = True
            self._write_retirement_release_audit(
                record,
                persisted_bytes=persisted_bytes,
                reason_code="pin_release_unknown",
            )
            raise MaterialOperationRunnerError(
                "retirement_release_unknown",
                "retirement tombstone is durable but one or more pins did not close",
            ) from exc
        record.retired = True
        return persisted_bytes

    def _write_retirement_release_audit(
        self,
        record: _PreparedRecord,
        *,
        persisted_bytes: bytes,
        reason_code: str,
    ) -> None:
        try:
            write_durable_json(
                parent=record.control_directory,
                filename=RETIREMENT_RELEASE_AUDIT_FILENAME,
                payload={
                    "protocol_version": DESCRIPTOR_PROTOCOL_VERSION,
                    "state": "audit_required",
                    "reason_code": reason_code,
                    "execution_id": record.execution_id,
                    "attempt_id": record.attempt_id,
                    "retired_sha256": hashlib.sha256(persisted_bytes).hexdigest(),
                },
                replace=False,
                max_bytes=MAX_RETIREMENT_RELEASE_AUDIT_BYTES,
            )
        except BaseException:
            # The durable tombstone and in-memory retired marker remain the
            # primary safety boundary; a missing sidecar is itself an
            # operator-visible audit concern, but must not cause a retry.
            pass

    def _advance_launch_descriptor(
        self,
        prepared: PreparedMaterialAttempt,
        *,
        phase: MaterialLaunchPhase | str,
        request_sha256: str | None = None,
        root_pid: int | None = None,
        root_creation_time: str | None = None,
    ) -> str:
        """Durably advance exactly one launch phase.

        This controller-internal seam is deliberately not a service API.  The
        later runner slice calls it immediately around each frozen native
        boundary; tests exercise it directly without creating a process.
        """

        target_phase = _coerce_launch_phase(phase)
        with self._lock:
            self._ensure_open()
            record = self._record_for(prepared)
            if record.retired:
                raise MaterialOperationRunnerError(
                    "attempt_already_retired",
                    "a retired attempt cannot advance its launch descriptor",
                )
            _validate_prepared_snapshot(record, prepared)
            expected_index = _PHASE_INDEX[record.phase] + 1
            if (
                expected_index >= len(MATERIAL_LAUNCH_PHASES)
                or MATERIAL_LAUNCH_PHASES[expected_index] is not target_phase
            ):
                raise MaterialOperationRunnerError(
                    "descriptor_transition_invalid",
                    "launch descriptors may advance only to the immediate next phase",
                )

            # Any valid transition beyond planned belongs to an externally
            # claimed attempt.  Commit this conservative ownership fact before
            # any validation or durable write can fail.
            record.claim_acquired = True
            next_request_sha256 = _request_hash_for_transition(
                record=record,
                target_phase=target_phase,
                supplied=request_sha256,
            )
            next_root_pid, next_root_creation_time = _root_for_transition(
                record=record,
                target_phase=target_phase,
                supplied_pid=root_pid,
                supplied_creation_time=root_creation_time,
            )

            for pin in record.pin_scope:
                pin.verify()
            current_bytes = _read_verified_bytes(
                parent=record.control_directory,
                filename=CONTAINER_FILENAME,
                max_bytes=MAX_DESCRIPTOR_BYTES,
            )
            current_sha256 = hashlib.sha256(current_bytes).hexdigest()
            if current_sha256 != record.container_hash:
                raise MaterialOperationRunnerError(
                    "descriptor_changed",
                    "current descriptor bytes no longer match runner ownership",
                )
            current = decode_material_container_descriptor(current_bytes)
            _match_current_descriptor(current, record)

            # Preserve identities even if the durable replacement becomes
            # uncertain; cleanup/quarantine must not forget a receipt or root
            # merely because the next descriptor did not acknowledge it.
            record.request_sha256 = next_request_sha256
            record.root_pid = next_root_pid
            record.root_creation_time = next_root_creation_time
            next_descriptor = MaterialOperationContainerDescriptor(
                protocol_version=DESCRIPTOR_PROTOCOL_VERSION,
                phase=target_phase,
                previous_phase=record.phase,
                previous_descriptor_sha256=record.container_hash,
                execution_id=record.execution_id,
                attempt_id=record.attempt_id,
                control_id=record.control_id,
                job_instance_id=record.job_instance_id,
                operation=record.operation,
                controller_pid=record.controller.pid,
                controller_creation_time=record.controller.creation_time,
                operation_may_have_started=(
                    _PHASE_INDEX[target_phase] >= _OPERATION_STARTED_INDEX
                ),
                request_sha256=next_request_sha256,
                job_handle_inheritable=False,
                root_pid=next_root_pid,
                root_creation_time=next_root_creation_time,
                job_active_processes_after_create=(
                    1 if _PHASE_INDEX[target_phase] >= _ROOT_IDENTITY_INDEX else None
                ),
            )
            old_phase = record.phase
            old_hash = record.container_hash
            receipt = write_durable_json(
                parent=record.control_directory,
                filename=CONTAINER_FILENAME,
                payload=_descriptor_to_mapping(next_descriptor),
                replace=True,
                max_bytes=MAX_DESCRIPTOR_BYTES,
            )
            record.previous_phase = old_phase
            record.previous_descriptor_sha256 = old_hash
            record.phase = target_phase
            record.container_hash = receipt.sha256
            prepared.container_hash = receipt.sha256
            return receipt.sha256

    def run(
        self,
        prepared: PreparedMaterialAttempt,
        request: MaterialOperationRequest,
        *,
        secrets: MaterialOperationSecrets,
        startup_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> MaterialOperationOutcome:
        """Run one attempt and contain every post-claim unexpected failure."""
        try:
            return self._run_impl(
                prepared,
                request,
                secrets=secrets,
                startup_timeout_seconds=startup_timeout_seconds,
                operation_timeout_seconds=operation_timeout_seconds,
            )
        except MaterialOperationBlocked:
            raise
        except BaseException as error:
            failure = (
                error
                if isinstance(error, MaterialOperationRunnerError)
                else MaterialOperationRunnerError(
                    "run_unexpected_failure",
                    "an unexpected post-claim runner failure occurred",
                )
            )
            try:
                record = self._record_for(prepared)
            except Exception:
                raise
            with self._lock:
                if record.outcome is not None or record.blocked:
                    raise
                # Reversible validation and the explicit test seam that stops
                # immediately after Job configuration remain ordinary errors.
                # Once an exact process/root owner exists, however, no native
                # failure may escape without a containment attempt.
                if not record.claim_acquired:
                    raise
                if (
                    record.job_owner is None
                    and record.pipe_owner is None
                    and record.process_owner is None
                    and record.process_quarantine_owner is None
                ):
                    # The request publication boundary has no native owner
                    # yet; the caller must receive its original durable-file
                    # error and may leave the claimed attempt for recovery.
                    raise
            self._handle_unexpected_failure(prepared, failure)
            raise AssertionError("unexpected failure handler returned")

    def _run_impl(
        self,
        prepared: PreparedMaterialAttempt,
        request: MaterialOperationRequest,
        *,
        secrets: MaterialOperationSecrets,
        startup_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> MaterialOperationOutcome:
        startup_timeout = _validate_run_timeout(
            startup_timeout_seconds,
            "startup_timeout_seconds",
        )
        operation_timeout = _validate_run_timeout(
            operation_timeout_seconds,
            "operation_timeout_seconds",
        )
        try:
            startup_started_at = _monotonic()
        except BaseException:
            raise MaterialOperationRunnerError(
                "run_clock_unavailable",
                "the monotonic startup clock could not be sampled",
            ) from None
        startup_deadline = startup_started_at + startup_timeout
        if not math.isfinite(startup_deadline):
            raise MaterialOperationRunnerError(
                "run_timeout_invalid",
                "startup timeout cannot form a finite monotonic deadline",
            )

        with self._lock:
            self._ensure_open()
            record = self._record_for(prepared)
            if record.retired:
                raise MaterialOperationRunnerError(
                    "attempt_already_retired",
                    "a retired attempt cannot be started",
                )
            if record.claim_acquired:
                raise MaterialOperationRunnerError(
                    "run_already_started",
                    "this attempt already crossed its claim boundary",
                )
            if record.claim_bound and record.claim_binding is None:
                raise MaterialOperationRunnerError(
                    "claim_binding_missing",
                    "attempt claim boundary is marked bound without a typed receipt",
                )
            _validate_prepared_snapshot(record, prepared)
            frozen = _freeze_material_run_inputs(
                record=record,
                request=request,
                secrets=secrets,
                startup_deadline=startup_deadline,
                operation_timeout_seconds=operation_timeout,
            )
            runtime_preflight = _WINDOWS_RUNNER_OPS.preflight_worker_runtime(
                prepared=prepared,
            )
            # Canonical input validation is the last reversible entry gate.
            # From here onward, a partial durable write may exist and the
            # attempt must never be treated as claim_not_acquired.  A bound
            # claim has already crossed the database boundary; an unbound
            # legacy caller crosses it here for compatibility only.
            record.claim_acquired = True
            record.frozen_run = frozen

        receipt = write_durable_bytes(
            parent=record.directory,
            filename=REQUEST_FILENAME,
            payload=frozen.request_bytes,
            replace=False,
            max_bytes=MAX_REQUEST_BYTES,
        )
        _validate_request_receipt(
            receipt=receipt,
            record=record,
            frozen=frozen,
        )

        job_owner = _WINDOWS_RUNNER_OPS.create_job()
        with self._lock:
            current = self._record_for(prepared)
            if current is not record or record.job_owner is not None:
                raise MaterialOperationRunnerError(
                    "run_owner_state_invalid",
                    "Job ownership cannot be installed twice or on another attempt",
                )
            record.job_owner = job_owner
        self._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.JOB_CONFIGURED,
            request_sha256=frozen.request_sha256,
        )

        pipe_owner = _WINDOWS_RUNNER_OPS.create_secret_pipe(
            forbidden_ids=(
                record.execution_id,
                record.attempt_id,
                record.control_id,
                record.job_instance_id,
            )
        )
        with self._lock:
            current = self._record_for(prepared)
            if current is not record or record.pipe_owner is not None:
                raise MaterialOperationRunnerError(
                    "run_owner_state_invalid",
                    "secret-pipe ownership cannot be installed twice or elsewhere",
                )
            record.pipe_owner = pipe_owner

        pipe_name = getattr(pipe_owner, "name", None)
        if type(pipe_name) is not str or not pipe_name:
            raise MaterialOperationRunnerError(
                "secret_pipe_owner_invalid",
                "secret-pipe owner does not expose its fixed name",
            )
        launch_spec = _WINDOWS_RUNNER_OPS.build_worker_launch_spec(
            prepared=prepared,
            pipe_name=pipe_name,
            runtime_preflight=runtime_preflight,
        )
        self._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.CREATE_INTENT,
        )
        try:
            process_owner = _WINDOWS_RUNNER_OPS.create_suspended_process(
                launch_spec=launch_spec,
                job_owner=job_owner,
            )
        except WindowsAtomicProcessResourceError as error:
            resource_owner = error.resource_owner
            try:
                quarantine_owner = resource_owner.transfer_to_quarantine()
            except BaseException as transfer_error:
                with self._lock:
                    current = self._record_for(prepared)
                    if current is record and record.process_quarantine_owner is None:
                        record.process_quarantine_owner = resource_owner
                raise MaterialOperationRunnerError(
                    "process_quarantine_handoff_failed",
                    "atomic process resources could not be transferred to runner quarantine",
                ) from BaseExceptionGroup(
                    "atomic process resource handoff failures",
                    [error, transfer_error],
                )
            with self._lock:
                current = self._record_for(prepared)
                if current is not record or record.process_quarantine_owner is not None:
                    raise MaterialOperationRunnerError(
                        "run_owner_state_invalid",
                        "atomic process quarantine ownership cannot be installed twice",
                    ) from error
                record.process_quarantine_owner = quarantine_owner
            raise
        with self._lock:
            current = self._record_for(prepared)
            if current is not record or record.process_owner is not None:
                raise MaterialOperationRunnerError(
                    "run_owner_state_invalid",
                    "process ownership cannot be installed twice or elsewhere",
                )
            record.process_owner = process_owner
        root_pid_value = getattr(process_owner, "pid", None)
        _validate_windows_pid(root_pid_value, "root_pid")
        root_pid, root_creation_time = _WINDOWS_RUNNER_OPS.query_process_identity(
            pid=root_pid_value,
        )
        if root_pid != root_pid_value:
            raise MaterialOperationRunnerError(
                "root_identity_mismatch",
                "queried root PID does not match the atomic process owner",
            )
        _validate_identity_time(root_creation_time, "root_creation_time")
        _WINDOWS_RUNNER_OPS.verify_single_root_job(
            job_owner=job_owner,
            root_pid=root_pid,
        )
        self._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.CREATED_IN_JOB_SUSPENDED,
            root_pid=root_pid,
            root_creation_time=root_creation_time,
        )
        self._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.RESUME_INTENT,
        )
        previous_suspend_count = process_owner.resume()  # type: ignore[union-attr]
        if type(previous_suspend_count) is not int or previous_suspend_count != 1:
            raise MaterialOperationRunnerError(
                "resume_count_invalid",
                "ResumeThread must return a previous suspend count of exactly one",
            )
        self._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.RESUMED_NO_SECRET,
        )
        process_owner.close_thread_handle()  # type: ignore[union-attr]
        _WINDOWS_RUNNER_OPS.connect_secret_pipe(
            pipe_owner=pipe_owner,
            absolute_deadline=frozen.startup_deadline,
        )
        _WINDOWS_RUNNER_OPS.verify_secret_pipe_client(
            pipe_owner=pipe_owner,
            root_pid=root_pid,
            root_creation_time=root_creation_time,
            job_owner=job_owner,
        )
        self._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.SECRET_SEND_INTENT,
        )
        try:
            operation_started_at = _monotonic()
        except BaseException:
            raise MaterialOperationRunnerError(
                "run_clock_unavailable",
                "the monotonic operation clock could not be sampled",
            ) from None
        operation_deadline = (
            operation_started_at + frozen.operation_timeout_seconds
        )
        if not math.isfinite(operation_deadline):
            raise MaterialOperationRunnerError(
                "run_timeout_invalid",
                "operation timeout cannot form a finite monotonic deadline",
            )
        with self._lock:
            current = self._record_for(prepared)
            if current is not record or record.operation_deadline is not None:
                raise MaterialOperationRunnerError(
                    "run_owner_state_invalid",
                    "operation deadline cannot be installed twice or elsewhere",
                )
            record.operation_deadline = operation_deadline
        _WINDOWS_RUNNER_OPS.write_secret_frame(
            pipe_owner=pipe_owner,
            immutable_frame=frozen.secret_frame,
            absolute_deadline=min(
                frozen.startup_deadline,
                operation_deadline,
            ),
        )
        self._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.SECRET_SENT,
        )
        with self._lock:
            if record.pipe_close_attempted:
                raise MaterialOperationRunnerError(
                    "pipe_close_already_attempted",
                    "the secret pipe close transition was already started",
                )
            record.pipe_close_attempted = True
        try:
            pipe_owner.close()  # type: ignore[union-attr]
        except BaseException:
            with self._lock:
                record.pipe_close_unknown = True
            raise
        with self._lock:
            record.pipe_closed = True
        try:
            ready = _WINDOWS_RUNNER_OPS.wait_worker_ready(
                prepared=prepared,
                expected_operation=record.operation,
                expected_root_pid=root_pid,
                expected_request_sha256=frozen.request_sha256,
                startup_deadline=frozen.startup_deadline,
                operation_deadline=operation_deadline,
            )
            try:
                validate_material_worker_ready_identity(
                    ready,
                    expected_execution_id=record.execution_id,
                    expected_attempt_id=record.attempt_id,
                    expected_operation=record.operation,
                    expected_root_pid=root_pid,
                    expected_request_sha256=frozen.request_sha256,
                )
            except MaterialProtocolError:
                raise MaterialOperationRunnerError(
                    "worker_ready_identity_invalid",
                    "worker-ready message failed the complete parent identity gate",
                ) from None
        except Exception as error:
            failure = (
                error
                if isinstance(error, MaterialOperationRunnerError)
                else MaterialOperationRunnerError(
                    "worker_ready_unavailable",
                    "worker ready could not be read or validated",
                )
            )
            ready_failure_outcome = self._handle_ready_failure(prepared, failure)
            if ready_failure_outcome is not None:
                return ready_failure_outcome
            raise AssertionError("ready failure handler returned without a terminal result")
        self._advance_launch_descriptor(
            prepared,
            phase=MaterialLaunchPhase.READY,
        )
        try:
            _WINDOWS_RUNNER_OPS.wait_job_empty(
                job_owner=job_owner,
                operation_deadline=operation_deadline,
            )
        except Exception as error:
            failure = (
                error
                if isinstance(error, MaterialOperationRunnerError)
                else MaterialOperationRunnerError(
                    "operation_unavailable",
                    "the worker Job could not be conclusively observed",
                )
            )
            operation_failure_outcome = self._handle_operation_failure(
                prepared,
                failure,
            )
            if operation_failure_outcome is not None:
                return operation_failure_outcome
            raise AssertionError(
                "operation failure handler returned without a terminal result"
            )
        try:
            result = _WINDOWS_RUNNER_OPS.read_worker_result(
                prepared=prepared,
                expected_operation=record.operation,
            )
            try:
                result.__post_init__()
            except MaterialProtocolError:
                raise MaterialOperationRunnerError(
                    "worker_result_invalid",
                    "worker result failed its typed protocol validation",
                ) from None
            if (
                result.execution_id != record.execution_id
                or result.attempt_id != record.attempt_id
                or result.operation != record.operation
            ):
                raise MaterialOperationRunnerError(
                    "worker_result_identity_invalid",
                    "worker result does not match the complete parent identity",
                )
        except Exception as error:
            failure = (
                error
                if isinstance(error, MaterialOperationRunnerError)
                else MaterialOperationRunnerError(
                    "worker_result_unavailable",
                    "worker result could not be read or validated",
                )
            )
            self._raise_blocked_after_result_failure(prepared, failure)
        try:
            termination = _WINDOWS_RUNNER_OPS.collect_natural_termination(
                process_owner=process_owner,
                root_pid=root_pid,
            )
            termination.__post_init__()
            if (
                termination.root_pid != root_pid
                or termination.termination_method != "natural"
                or termination.active_processes != 0
            ):
                raise MaterialOperationRunnerError(
                    "termination_evidence_invalid",
                    "natural termination evidence does not match the completed root Job",
                )
            reconciliation = self._commit_queried_empty_reconciliation(
                prepared,
                termination,
            )
        except Exception as error:
            failure = (
                error
                if isinstance(error, MaterialOperationRunnerError)
                else MaterialOperationRunnerError(
                    "termination_evidence_invalid",
                    "natural termination evidence could not be proven",
                )
            )
            self._handle_operation_failure(prepared, failure)
            raise AssertionError(
                "natural termination failure handler returned without a terminal result"
            )
        try:
            outcome = MaterialOperationOutcome(
                status=result.status,
                result=result,
                termination=termination,
            )
        except MaterialProtocolError:
            raise MaterialOperationRunnerError(
                "operation_outcome_invalid",
                "worker result and termination evidence cannot form an outcome",
            ) from None
        # Keep an exact, durable termination proof for successful and failed
        # natural outcomes as well as timeout outcomes.  Retirement can then
        # compare every termination field (platform, exit code, method, root,
        # active count, and timestamp) instead of trusting a mutable object.
        self._commit_termination_outcome(
            prepared,
            outcome,
            allow_natural=True,
        )
        self._close_completion_resources_once(record)
        with self._lock:
            current = self._record_for(prepared)
            if (
                current is not record
                or record.outcome is not None
            ):
                raise MaterialOperationRunnerError(
                    "run_owner_state_invalid",
                    "natural completion cannot be installed twice or elsewhere",
                )
            record.outcome = outcome
        return outcome

    def _close_completion_resources_once(self, record: _PreparedRecord) -> None:
        """Attempt the paired process/Job close at most once.

        The lower-level helper deliberately tries both handles and may report
        a grouped error after a partial close.  Retrying it would risk a
        second close against a handle whose native state is unknown, so the
        record keeps an explicit attempt bit separate from the all-closed bit.
        """
        with self._lock:
            if record.completion_resources_closed:
                return
            if record.last_handle_close_attempted:
                raise MaterialOperationRunnerError(
                    "completion_close_after_last_handle",
                    "intentional last-handle close already owns the Job transition",
                )
            if record.completion_close_attempted:
                raise MaterialOperationRunnerError(
                    "completion_close_already_attempted",
                    "completion handles have an unresolved prior close attempt",
                )
            record.completion_close_attempted = True
            process_owner = record.process_owner
            job_owner = record.job_owner
        if process_owner is None or job_owner is None:
            raise MaterialOperationRunnerError(
                "completion_owner_unavailable",
                "completion close requires both process and Job owners",
            )
        _WINDOWS_RUNNER_OPS.close_natural_completion_resources(
            process_owner=process_owner,
            job_owner=job_owner,
        )
        with self._lock:
            record.completion_resources_closed = True

    def _commit_intentional_last_handle_close(
        self,
        prepared: PreparedMaterialAttempt,
    ) -> ReconciliationEvidence:
        """Close the sole Job owner under a durable, one-shot intent journal.

        This is deliberately a narrow recovery path.  It is legal only after
        overlapped I/O is terminal and before any paired completion close has
        been attempted.  A native close exception permanently leaves the
        attempt blocked; a successful close may be retried only for durable
        journal adoption or for waiting on the already-closing root process.
        """
        record = self._record_for(prepared)
        cached_reconciliation: ReconciliationEvidence | None = None
        expected_job_owner: object | None = None
        expected_process_owner: object | None = None
        with self._lock:
            self._ensure_open()
            _validate_prepared_snapshot(record, prepared)
            if record.retired:
                raise MaterialOperationRunnerError(
                    "attempt_already_retired",
                    "a retired attempt cannot close its last Job handle",
                )
            if record.reconciliation is not None:
                if record.reconciliation.method != "last_handle_close_kill_on_close":
                    raise MaterialOperationRunnerError(
                        "last_handle_close_unavailable",
                        "an actual reconciliation proof already owns this attempt",
                    )
                cached_reconciliation = record.reconciliation
            else:
                if _PHASE_INDEX[record.phase] < _PHASE_INDEX[MaterialLaunchPhase.CREATE_INTENT]:
                    raise MaterialOperationRunnerError(
                        "last_handle_close_illegal_phase",
                        "intentional last-handle close is illegal before create_intent",
                    )
                if record.io_drain_pending or (
                    record.pipe_close_attempted and not record.pipe_closed
                ):
                    raise MaterialOperationRunnerError(
                        "last_handle_close_io_pending",
                        "secret-pipe I/O has an unresolved cancellation or close attempt",
                    )
                if record.pipe_owner is not None and not record.pipe_closed:
                    raise MaterialOperationRunnerError(
                        "last_handle_close_pipe_open",
                        "intentional last-handle close requires a closed secret pipe",
                    )
                if (
                    record.process_quarantine_owner is not None
                    and not record.process_quarantine_cleaned
                ):
                    raise MaterialOperationRunnerError(
                        "last_handle_close_launch_cleanup_pending",
                        "atomic launch resources remain in quarantine ownership",
                    )
                if (
                    record.completion_close_attempted
                    and not record.last_handle_closed
                ):
                    raise MaterialOperationRunnerError(
                        "last_handle_close_after_completion_attempt",
                        "the paired completion close already crossed its native boundary",
                    )
                if record.job_owner is None:
                    raise MaterialOperationRunnerError(
                        "last_handle_close_owner_unavailable",
                        "intentional last-handle close requires the live Job owner",
                    )
                expected_job_owner = record.job_owner
                expected_process_owner = record.process_owner

        _assert_owned_io_drain_quarantine(record)
        if cached_reconciliation is not None:
            return self._resume_committed_last_handle_reconciliation(
                record,
                cached_reconciliation,
            )

        # Adopt an exact durable descriptor tip before publishing the intent.
        self._synchronize_descriptor_tip_after_lost_receipt(record, prepared)
        descriptor_bytes = _read_verified_bytes(
            parent=record.control_directory,
            filename=CONTAINER_FILENAME,
            max_bytes=MAX_DESCRIPTOR_BYTES,
        )
        descriptor = decode_material_container_descriptor(descriptor_bytes)
        _match_current_descriptor(descriptor, record)
        intent_hash = self._commit_last_handle_close_intent(record)
        _assert_owned_descriptor_tip(record, descriptor_bytes)
        _assert_owned_io_drain_quarantine(record)

        with self._lock:
            _validate_prepared_snapshot(record, prepared)
            if record.last_handle_closed:
                if not (
                    record.last_handle_close_attempted
                    and record.last_handle_job_closed
                    and record.completion_close_attempted
                    and record.completion_resources_closed
                ):
                    raise MaterialOperationRunnerError(
                        "last_handle_close_state_changed",
                        "completed last-handle state is internally inconsistent",
                    )
            elif (
                record.reconciliation is not None
                or record.job_owner is not expected_job_owner
                or record.process_owner is not expected_process_owner
                or record.io_drain_pending
                or (record.pipe_close_attempted and not record.pipe_closed)
                or (record.pipe_owner is not None and not record.pipe_closed)
                or (
                    record.process_quarantine_owner is not None
                    and not record.process_quarantine_cleaned
                )
                or record.completion_close_attempted
                or record.container_hash
                != hashlib.sha256(descriptor_bytes).hexdigest()
            ):
                raise MaterialOperationRunnerError(
                    "last_handle_close_state_changed",
                    "native ownership changed after the durable close intent",
                )
            if record.last_handle_close_attempted and not record.last_handle_job_closed:
                raise MaterialOperationRunnerError(
                    "last_handle_close_unknown",
                    "the prior Job close attempt has unknown native state",
                )
            job_owner = None
            if not record.last_handle_close_attempted:
                record.last_handle_close_attempted = True
                job_owner = expected_job_owner
        if job_owner is not None:
            try:
                _WINDOWS_RUNNER_OPS.close_last_handle_job(job_owner=job_owner)
            except BaseException as error:
                raise MaterialOperationRunnerError(
                    "last_handle_close_unknown",
                    "the intentional Job close did not return success",
                ) from error
            with self._lock:
                record.last_handle_job_closed = True

        with self._lock:
            if not record.last_handle_job_closed:
                raise MaterialOperationRunnerError(
                    "last_handle_close_unknown",
                    "the intentional Job close has no success acknowledgement",
                )
            root_pid = record.root_pid
            root_creation_time = record.root_creation_time
        if root_pid is not None:
            if root_creation_time is None:
                raise MaterialOperationRunnerError(
                    "last_handle_close_root_identity_invalid",
                    "the owned root identity is incomplete",
                )
            try:
                root_exited = _WINDOWS_RUNNER_OPS.wait_exact_process_exit(
                    pid=root_pid,
                    creation_time=root_creation_time,
                    timeout_seconds=10.0,
                )
            except MaterialOperationRunnerError as error:
                raise MaterialOperationRunnerError(
                    "last_handle_close_root_unproven",
                    "root exit could not be proven after the Job close",
                ) from error
            if not root_exited:
                raise MaterialOperationRunnerError(
                    "last_handle_close_root_unproven",
                    "the recorded root process is still alive after the Job close",
                )

        process_owner = None
        with self._lock:
            if not record.last_handle_closed:
                if record.process_owner is None:
                    record.completion_close_attempted = True
                    record.completion_resources_closed = True
                elif record.completion_close_attempted:
                    if not record.completion_resources_closed:
                        raise MaterialOperationRunnerError(
                            "last_handle_process_close_unknown",
                            "the process-handle close has unknown native state",
                        )
                else:
                    record.completion_close_attempted = True
                    process_owner = record.process_owner
        if process_owner is not None:
            try:
                _WINDOWS_RUNNER_OPS.close_last_handle_process(
                    process_owner=process_owner,
                )
            except BaseException as error:
                raise MaterialOperationRunnerError(
                    "last_handle_process_close_unknown",
                    "the root process handles did not close successfully",
                ) from error
            with self._lock:
                record.completion_resources_closed = True
        with self._lock:
            record.last_handle_closed = True

        closed_hash = self._commit_last_handle_closed(record, intent_hash)
        del closed_hash  # the record retains the durable hash for restart checks.
        _assert_owned_descriptor_tip(record, descriptor_bytes)
        evidence = ReconciliationEvidence(
            execution_id=record.execution_id,
            attempt_id=record.attempt_id,
            job_instance_id=record.job_instance_id,
            method="last_handle_close_kill_on_close",
            observation_basis="controller_closed_last_handle_inference",
            active_processes=None,
            root_pid=record.root_pid,
            operation_may_have_started=(
                _PHASE_INDEX[record.phase] >= _OPERATION_STARTED_INDEX
            ),
            observed_at=_utc_now(),
            container_sha256=record.container_hash,
        )
        committed = self._commit_reconciliation(prepared, evidence)
        self._commit_quarantine_resolution(record)
        return self._resume_committed_last_handle_reconciliation(
            record,
            committed,
        )

    def _resume_committed_last_handle_reconciliation(
        self,
        record: _PreparedRecord,
        evidence: ReconciliationEvidence,
    ) -> ReconciliationEvidence:
        """Revalidate close proof and finish/adopt its quarantine resolution."""
        descriptor_bytes = _read_verified_bytes(
            parent=record.control_directory,
            filename=CONTAINER_FILENAME,
            max_bytes=MAX_DESCRIPTOR_BYTES,
        )
        descriptor = decode_material_container_descriptor(descriptor_bytes)
        _match_current_descriptor(descriptor, record)
        intent_bytes, closed_bytes = _read_last_handle_close_journal_pair(
            control=record.control_directory,
            descriptor=descriptor,
            descriptor_bytes=descriptor_bytes,
        )
        if intent_bytes is None or closed_bytes is None:
            raise MaterialOperationRunnerError(
                "reconciliation_blocked",
                "cached last-handle evidence has no complete close journals",
            )
        record.last_handle_close_intent_hash = hashlib.sha256(intent_bytes).hexdigest()
        record.last_handle_closed_hash = hashlib.sha256(closed_bytes).hexdigest()
        if (
            evidence.execution_id != record.execution_id
            or evidence.attempt_id != record.attempt_id
            or evidence.job_instance_id != record.job_instance_id
            or evidence.root_pid != record.root_pid
            or evidence.container_sha256 != record.container_hash
            or evidence.active_processes is not None
        ):
            raise MaterialOperationRunnerError(
                "reconciliation_changed",
                "cached last-handle evidence does not match the owned descriptor",
            )
        if record.root_pid is not None:
            if record.root_creation_time is None:
                raise MaterialOperationRunnerError(
                    "reconciliation_changed",
                    "cached last-handle root identity is incomplete",
                )
            try:
                root_exited = _WINDOWS_RUNNER_OPS.wait_exact_process_exit(
                    pid=record.root_pid,
                    creation_time=record.root_creation_time,
                    timeout_seconds=10.0,
                )
            except MaterialOperationRunnerError as error:
                raise MaterialOperationRunnerError(
                    "reconciliation_blocked",
                    "cached last-handle root exit could not be re-proven",
                ) from error
            if not root_exited:
                raise MaterialOperationRunnerError(
                    "reconciliation_blocked",
                    "cached last-handle root process is still alive",
                )
        _assert_owned_descriptor_tip(record, descriptor_bytes)
        _assert_last_handle_pair_unchanged(
            control=record.control_directory,
            expected_intent_bytes=intent_bytes,
            expected_closed_bytes=closed_bytes,
        )
        termination_bytes = _read_verified_bytes(
            parent=record.control_directory,
            filename=TERMINATION_FILENAME,
            max_bytes=MAX_TERMINATION_BYTES,
        )
        if _decode_reconciliation_bytes(termination_bytes) != evidence:
            raise MaterialOperationRunnerError(
                "reconciliation_changed",
                "cached last-handle evidence differs from its durable journal",
            )
        record.reconciliation_hash = hashlib.sha256(termination_bytes).hexdigest()
        _assert_owned_io_drain_quarantine(record)
        self._commit_quarantine_resolution(record)
        _assert_owned_descriptor_tip(record, descriptor_bytes)
        _assert_last_handle_pair_unchanged(
            control=record.control_directory,
            expected_intent_bytes=intent_bytes,
            expected_closed_bytes=closed_bytes,
        )
        return evidence

    def _commit_last_handle_close_intent(self, record: _PreparedRecord) -> str:
        payload = _last_handle_close_mapping(record, state="intent")
        if record.last_handle_close_intent_hash is not None:
            existing_bytes = _read_verified_bytes(
                parent=record.control_directory,
                filename=LAST_HANDLE_CLOSE_INTENT_FILENAME,
                max_bytes=MAX_LAST_HANDLE_CLOSE_INTENT_BYTES,
            )
            existing = _decode_last_handle_close_intent_bytes(existing_bytes)
            _assert_last_handle_mapping_matches(existing, payload, closed=False)
            record.last_handle_close_intent_hash = hashlib.sha256(existing_bytes).hexdigest()
            return record.last_handle_close_intent_hash
        try:
            existing_bytes = _read_verified_bytes(
                parent=record.control_directory,
                filename=LAST_HANDLE_CLOSE_INTENT_FILENAME,
                max_bytes=MAX_LAST_HANDLE_CLOSE_INTENT_BYTES,
            )
        except Exception as error:
            if getattr(error, "winerror", None) not in {2, 3}:
                raise
        else:
            existing = _decode_last_handle_close_intent_bytes(existing_bytes)
            _assert_last_handle_mapping_matches(existing, payload, closed=False)
            record.last_handle_close_intent_hash = hashlib.sha256(existing_bytes).hexdigest()
            return record.last_handle_close_intent_hash
        try:
            receipt = write_durable_json(
                parent=record.control_directory,
                filename=LAST_HANDLE_CLOSE_INTENT_FILENAME,
                payload=payload,
                replace=False,
                max_bytes=MAX_LAST_HANDLE_CLOSE_INTENT_BYTES,
            )
        except Exception as error:
            try:
                existing_bytes = _read_verified_bytes(
                    parent=record.control_directory,
                    filename=LAST_HANDLE_CLOSE_INTENT_FILENAME,
                    max_bytes=MAX_LAST_HANDLE_CLOSE_INTENT_BYTES,
                )
                existing = _decode_last_handle_close_intent_bytes(existing_bytes)
                _assert_last_handle_mapping_matches(existing, payload, closed=False)
            except Exception:
                raise error
            record.last_handle_close_intent_hash = hashlib.sha256(existing_bytes).hexdigest()
        else:
            record.last_handle_close_intent_hash = receipt.sha256
        return record.last_handle_close_intent_hash

    def _commit_last_handle_closed(self, record: _PreparedRecord, intent_hash: str) -> str:
        payload = _last_handle_close_mapping(
            record,
            state="closed",
            intent_sha256=intent_hash,
        )
        try:
            existing_bytes = _read_verified_bytes(
                parent=record.control_directory,
                filename=LAST_HANDLE_CLOSED_FILENAME,
                max_bytes=MAX_LAST_HANDLE_CLOSED_BYTES,
            )
        except Exception as error:
            if getattr(error, "winerror", None) not in {2, 3}:
                raise
        else:
            existing = _decode_last_handle_closed_bytes(existing_bytes)
            _assert_last_handle_mapping_matches(existing, payload, closed=True)
            record.last_handle_closed_hash = hashlib.sha256(existing_bytes).hexdigest()
            return record.last_handle_closed_hash
        try:
            receipt = write_durable_json(
                parent=record.control_directory,
                filename=LAST_HANDLE_CLOSED_FILENAME,
                payload=payload,
                replace=False,
                max_bytes=MAX_LAST_HANDLE_CLOSED_BYTES,
            )
        except Exception as error:
            try:
                existing_bytes = _read_verified_bytes(
                    parent=record.control_directory,
                    filename=LAST_HANDLE_CLOSED_FILENAME,
                    max_bytes=MAX_LAST_HANDLE_CLOSED_BYTES,
                )
                existing = _decode_last_handle_closed_bytes(existing_bytes)
                _assert_last_handle_mapping_matches(existing, payload, closed=True)
            except Exception:
                raise error
            record.last_handle_closed_hash = hashlib.sha256(existing_bytes).hexdigest()
        else:
            record.last_handle_closed_hash = receipt.sha256
        return record.last_handle_closed_hash

    def _terminate_job_for_quarantine(
        self,
        record: _PreparedRecord,
        errors: list[BaseException],
    ) -> bool:
        """Kill the Job even when exact root evidence is unavailable.

        This path deliberately returns only a containment fact.  It never
        fabricates ``TerminationEvidence`` or permits handle close/reconcile
        to treat the result as a queried-empty proof.
        """
        if record.containment_terminated:
            return True
        if record.job_owner is None:
            return False
        try:
            _WINDOWS_RUNNER_OPS.terminate_job_for_quarantine(
                job_owner=record.job_owner,
            )
        except BaseException as error:
            errors.append(error)
            return False
        with self._lock:
            record.containment_terminated = True
        return True

    def _drain_pipe_for_quarantine(
        self,
        record: _PreparedRecord,
        errors: list[BaseException],
        *,
        allow_retry: bool = False,
    ) -> bool:
        """Drain/close any owned overlapped pipe before native owner close."""
        owner = record.pipe_owner
        if owner is None:
            return True
        with self._lock:
            if record.io_drain_pending and not allow_retry:
                errors.append(
                    MaterialOperationRunnerError(
                        "io_drain_pending",
                        "a previous overlapped-pipe cancellation has no terminal acknowledgement",
                    )
                )
                return False
            if allow_retry:
                record.io_drain_pending = False
            if record.pipe_close_attempted and not record.pipe_closed:
                errors.append(
                    MaterialOperationRunnerError(
                        "pipe_close_unknown",
                        "a previous secret-pipe close attempt has unknown native state",
                    )
                )
                return False
        operation = getattr(owner, "active_operation", None)
        if operation is not None:
            with self._lock:
                record.io_drain_pending = True
            try:
                operation.cancel_and_drain()
                operation.close()
                owner.active_operation = None
            except BaseException as error:
                if isinstance(error, OverlappedPipeError):
                    successor = getattr(error, "resource_owner", None)
                    if successor is not None:
                        owner.active_operation = successor
                    # 3B-2 reports a terminal native completion by raising the
                    # typed deadline/native error *with* Completion attached.
                    # That is a failed operation, not a pending drain.  Close
                    # the exact active generation and continue pipe cleanup.
                    if error.completion is not None:
                        terminal_owner = (
                            successor if successor is not None else operation
                        )
                        try:
                            terminal_owner.close()
                        except BaseException as close_error:
                            errors.extend([error, close_error])
                            self._register_pending_pipe_drain(
                                record,
                                terminal_owner,
                                errors,
                            )
                            return False
                        owner.active_operation = None
                        with self._lock:
                            record.io_drain_pending = False
                    else:
                        errors.append(error)
                        self._register_pending_pipe_drain(
                            record,
                            successor if successor is not None else operation,
                            errors,
                        )
                        return False
                else:
                    errors.append(error)
                    self._register_pending_pipe_drain(record, operation, errors)
                    return False
            with self._lock:
                record.io_drain_pending = False
        with self._lock:
            if record.pipe_close_attempted:
                return record.pipe_closed
            # The close transition is one-shot: a native failure leaves the
            # handle state unknown and must never be retried by a later error
            # path.
            record.pipe_close_attempted = True
        try:
            owner.close()
        except BaseException as error:
            with self._lock:
                record.pipe_close_unknown = True
            errors.append(error)
            return False
        with self._lock:
            record.pipe_closed = True
            record.pipe_close_unknown = False
        return True

    def _cleanup_process_quarantine_owner(
        self,
        record: _PreparedRecord,
        errors: list[BaseException],
    ) -> bool:
        """Close a transferred atomic-launch owner exactly once after containment."""
        owner = record.process_quarantine_owner
        if owner is None:
            return True
        with self._lock:
            if record.process_quarantine_cleanup_attempted:
                if record.process_quarantine_cleaned:
                    return True
                errors.append(
                    MaterialOperationRunnerError(
                        "launch_cleanup_unknown",
                        "atomic launch-resource cleanup has unresolved native state",
                    )
                )
                return False
            record.process_quarantine_cleanup_attempted = True
        cleanup = getattr(owner, "cleanup_all_stop_on_failure", None)
        if not callable(cleanup):
            errors.append(
                MaterialOperationRunnerError(
                    "launch_cleanup_unavailable",
                    "transferred atomic launch owner has no cleanup operation",
                )
            )
            return False
        try:
            cleanup_errors = cleanup()
        except BaseException as error:
            errors.append(error)
            return False
        if type(cleanup_errors) is not list:
            errors.append(
                MaterialOperationRunnerError(
                    "launch_cleanup_invalid",
                    "atomic launch cleanup did not return its exact error list",
                )
            )
            return False
        if cleanup_errors:
            errors.extend(cleanup_errors)
            return False
        with self._lock:
            record.process_quarantine_cleaned = True
        return True

    def _register_pending_pipe_drain(
        self,
        record: _PreparedRecord,
        operation: object,
        errors: list[BaseException],
    ) -> None:
        """Retain a pending overlapped owner for a bounded later poll."""
        try:
            now = _monotonic()
        except BaseException as error:
            errors.append(error)
            return
        key = (record.execution_id, record.attempt_id, record.control_id)
        with self._lock:
            existing = self._pending_pipe_drains.get(key)
            deadline = (
                existing.monitor_deadline
                if existing is not None
                else now + 10.0
            )
            self._pending_pipe_drains[key] = _PendingPipeDrain(
                operation=operation,
                monitor_deadline=deadline,
            )
            record.io_drain_pending = True

    def poll_io_drain(
        self,
        prepared: PreparedMaterialAttempt,
    ) -> Literal["pending", "resolved"]:
        """Retry one quarantined overlapped drain without inventing a terminal proof.

        The registry is intentionally process-local.  A controller restart must
        still treat the durable ``io_drain_blocked`` quarantine as unresolved.
        """
        with self._lock:
            self._ensure_open()
            record = self._record_for(prepared)
            _validate_prepared_snapshot(record, prepared)
        key = (record.execution_id, record.attempt_id, record.control_id)
        with self._lock:
            pending = self._pending_pipe_drains.get(key)
            if pending is not None:
                if pending.polling:
                    return "pending"
                pending.polling = True
        if pending is None:
            if record.pipe_closed:
                return "resolved"
            raise MaterialOperationRunnerError(
                "io_drain_not_pending",
                "the attempt has no registered pending overlapped drain",
            )
        try:
            # ``monitor_deadline`` bounds the first controller-side monitor;
            # it must not become a tombstone that prevents a later nonblocking
            # poll from observing a terminal completion.
            try:
                _monotonic()
            except BaseException:
                raise MaterialOperationRunnerError(
                    "run_clock_unavailable",
                    "the pending drain monitor clock could not be sampled",
                ) from None
            owner = record.pipe_owner
            if owner is not None:
                active_operation = getattr(owner, "active_operation", None)
                if (
                    active_operation is not None
                    and active_operation is not pending.operation
                ) or (
                    active_operation is None
                    and not record.pipe_closed
                ):
                    raise MaterialOperationRunnerError(
                        "io_drain_owner_changed",
                        "the pending drain no longer owns the active overlapped generation",
                    )
                errors: list[BaseException] = []
                resolved = self._drain_pipe_for_quarantine(
                    record,
                    errors,
                    allow_retry=True,
                )
            else:
                resolved = True
            if not resolved:
                with self._lock:
                    record.io_drain_pending = True
                return "pending"

            # If the original failure had no Query=0 proof, the now-terminal
            # io-drain quarantine may use its one approved inference path.
            # Keep the registry entry until the intent/close/termination/
            # resolution chain is fully durable.
            if (
                record.reconciliation is None
                and record.quarantine_hash is not None
                and record.job_owner is not None
            ):
                try:
                    self._commit_intentional_last_handle_close(record.prepared)
                except MaterialOperationRunnerError:
                    return "pending"

            # A previously proven Job-empty reconciliation may now safely
            # release the paired native process/Job handles.  Keep the pending
            # registry entry until this close and its durable acknowledgement
            # both succeed; otherwise a later poll could falsely report a
            # resolved generation after a close failure.
            if record.reconciliation is not None:
                if not record.completion_resources_closed:
                    if record.completion_close_attempted:
                        # The one-shot close already crossed the native
                        # boundary without an all-closed acknowledgement.
                        # Never write a resolution journal for that state.
                        with self._lock:
                            record.io_drain_pending = False
                        return "pending"
                    try:
                        self._close_completion_resources_once(record)
                    except BaseException:
                        with self._lock:
                            record.io_drain_pending = False
                        return "pending"
                if not record.completion_resources_closed:
                    return "pending"

            if (
                record.reconciliation is not None
                and record.quarantine_hash is not None
                and record.reconciliation_hash is not None
            ):
                self._commit_quarantine_resolution(record)

            with self._lock:
                self._pending_pipe_drains.pop(key, None)
                record.io_drain_pending = False
            return "resolved"
        finally:
            with self._lock:
                current = self._pending_pipe_drains.get(key)
                if current is pending:
                    current.polling = False

    def _handle_unexpected_failure(
        self,
        prepared: PreparedMaterialAttempt,
        failure: MaterialOperationRunnerError,
    ) -> None:
        """Fail closed for native failures outside the typed happy-path seams."""
        record = self._record_for(prepared)
        containment_errors: list[BaseException] = []
        termination: TerminationEvidence | None = None
        pipe_drained = self._drain_pipe_for_quarantine(record, containment_errors)
        completion_close_unknown = (
            record.completion_close_attempted
            and not record.completion_resources_closed
        )
        close_unknown = completion_close_unknown or record.pipe_close_unknown
        if not completion_close_unknown and (
            record.job_owner is not None
            and record.process_owner is not None
            and record.root_pid is not None
        ):
            try:
                termination = _WINDOWS_RUNNER_OPS.terminate_job_and_collect(
                    job_owner=record.job_owner,
                    process_owner=record.process_owner,
                    root_pid=record.root_pid,
                    never_resumed=(
                        _PHASE_INDEX[record.phase]
                        < _PHASE_INDEX[MaterialLaunchPhase.RESUMED_NO_SECRET]
                    ),
                )
            except BaseException as error:
                containment_errors.append(error)

        if termination is None and not completion_close_unknown:
            self._terminate_job_for_quarantine(record, containment_errors)

        launch_cleaned = True
        if record.process_quarantine_owner is not None:
            if termination is not None or record.containment_terminated:
                launch_cleaned = self._cleanup_process_quarantine_owner(
                    record,
                    containment_errors,
                )
            else:
                launch_cleaned = False

        if termination is not None:
            try:
                self._commit_queried_empty_reconciliation(prepared, termination)
            except BaseException as error:
                containment_errors.append(error)

        # Once a durable queried-empty proof exists, a paired native close is
        # safe to attempt.  Do it before writing the final quarantine record so
        # a partial close is represented as ``close_unknown`` on the first
        # durable journal, rather than leaving an irreversible stale reason.
        if (
            termination is not None
            and not containment_errors
            and pipe_drained
            and not record.completion_close_attempted
        ):
            try:
                self._close_completion_resources_once(record)
            except BaseException as error:
                containment_errors.append(error)
                close_unknown = True

        reason_code = (
            "close_unknown"
            if close_unknown
            else (
                "io_drain_blocked"
                if not pipe_drained
                else ("launch_cleanup_blocked" if not launch_cleaned else "run_blocked")
            )
        )

        try:
            quarantine_dir = self._commit_quarantine(
                prepared,
                reason_code=reason_code,
                evidence=(record.reconciliation if close_unknown else termination),
            )
        except BaseException as error:
            containment_errors.append(error)
            raise MaterialOperationRunnerError(
                "quarantine_commit_failed",
                "unexpected runner failure could not be durably quarantined",
            ) from BaseExceptionGroup(
                "unexpected runner failure quarantine failures",
                [failure, *containment_errors],
            )

        blocked = MaterialOperationBlocked(
            execution_id=record.execution_id,
            attempt_id=record.attempt_id,
            reason_code=reason_code,
            job_instance_id=record.job_instance_id,
            quarantine_dir=quarantine_dir,
        )
        if containment_errors:
            raise blocked from BaseExceptionGroup(
                "unexpected runner failure containment",
                [failure, *containment_errors],
            )
        raise blocked from failure

    def _handle_operation_failure(
        self,
        prepared: PreparedMaterialAttempt,
        failure: MaterialOperationRunnerError,
    ) -> MaterialOperationOutcome | None:
        """Contain a post-launch Job failure before exposing a typed result."""
        record = self._record_for(prepared)
        containment_errors: list[BaseException] = []
        termination: TerminationEvidence | None = None
        pipe_drained = self._drain_pipe_for_quarantine(record, containment_errors)
        try:
            if record.job_owner is None or record.process_owner is None or record.root_pid is None:
                raise MaterialOperationRunnerError(
                    "operation_containment_unavailable",
                    "operation failure has no exact Job/process/root ownership",
                )
            termination = _WINDOWS_RUNNER_OPS.terminate_job_and_collect(
                job_owner=record.job_owner,
                process_owner=record.process_owner,
                root_pid=record.root_pid,
                never_resumed=(
                    _PHASE_INDEX[record.phase]
                    < _PHASE_INDEX[MaterialLaunchPhase.RESUMED_NO_SECRET]
                ),
            )
        except BaseException as error:
            containment_errors.append(error)

        if termination is None:
            self._terminate_job_for_quarantine(record, containment_errors)
            reason_code = "io_drain_blocked" if not pipe_drained else "operation_blocked"
            try:
                quarantine_dir = self._commit_quarantine(
                    prepared,
                    reason_code=reason_code,
                    evidence=None,
                )
            except BaseException as error:
                containment_errors.append(error)
                raise MaterialOperationRunnerError(
                    "quarantine_commit_failed",
                    "operation failure could not be durably entered into quarantine",
                ) from BaseExceptionGroup(
                    "operation failure quarantine failures",
                    [failure, *containment_errors],
                )
            blocked = MaterialOperationBlocked(
                execution_id=record.execution_id,
                attempt_id=record.attempt_id,
                reason_code=reason_code,
                job_instance_id=record.job_instance_id,
                quarantine_dir=quarantine_dir,
            )
            raise blocked from BaseExceptionGroup(
                "operation failure containment unavailable",
                [failure, *containment_errors],
            )

        reconciliation_committed = False
        try:
            self._commit_queried_empty_reconciliation(prepared, termination)
            reconciliation_committed = True
        except BaseException as error:
            containment_errors.append(error)

        # A native Query=0 held only in memory is not enough to justify
        # closing the last controller handles.  Preserve the typed
        # termination proof in quarantine, but retain the owners for a later
        # reconciliation attempt when the canonical journal write failed.
        if not reconciliation_committed:
            try:
                quarantine_dir = self._commit_quarantine(
                    prepared,
                    reason_code="reconciliation_blocked",
                    evidence=termination,
                )
            except BaseException as error:
                containment_errors.append(error)
                raise MaterialOperationRunnerError(
                    "quarantine_commit_failed",
                    "operation failure could not preserve its termination proof",
                ) from BaseExceptionGroup(
                    "operation reconciliation failures",
                    [failure, *containment_errors],
                )
            blocked = MaterialOperationBlocked(
                execution_id=record.execution_id,
                attempt_id=record.attempt_id,
                reason_code="reconciliation_blocked",
                job_instance_id=record.job_instance_id,
                quarantine_dir=quarantine_dir,
            )
            raise blocked from BaseExceptionGroup(
                "operation reconciliation failures",
                [failure, *containment_errors],
            )

        is_timeout = failure.code == "operation_timeout"
        if is_timeout and pipe_drained and not containment_errors:
            outcome = MaterialOperationOutcome(
                status="terminated",
                result=None,
                termination=termination,
            )
            try:
                self._commit_termination_outcome(prepared, outcome)
                self._close_completion_resources_once(record)
                with self._lock:
                    record.outcome = outcome
                return outcome
            except BaseException as error:
                containment_errors.append(error)

        close_unknown = False
        if (
            pipe_drained
            and not record.completion_resources_closed
            and not record.completion_close_attempted
            and not containment_errors
        ):
            try:
                self._close_completion_resources_once(record)
            except BaseException as error:
                containment_errors.append(error)
                close_unknown = True
        fallback_reason = (
            "termination_close_blocked"
            if close_unknown
            or (record.completion_close_attempted and not record.completion_resources_closed)
            else ("io_drain_blocked" if not pipe_drained else "operation_blocked")
        )
        try:
            quarantine_dir = self._commit_quarantine(
                prepared,
                reason_code=fallback_reason,
                evidence=termination,
            )
        except BaseException as error:
            containment_errors.append(error)
            raise MaterialOperationRunnerError(
                "quarantine_commit_failed",
                "operation failure could not be durably quarantined",
            ) from BaseExceptionGroup(
                "operation failure quarantine failures",
                [failure, *containment_errors],
            )
        blocked = MaterialOperationBlocked(
            execution_id=record.execution_id,
            attempt_id=record.attempt_id,
            reason_code=fallback_reason,
            job_instance_id=record.job_instance_id,
            quarantine_dir=quarantine_dir,
        )
        if containment_errors:
            raise blocked from BaseExceptionGroup(
                "operation failure quarantine containment",
                [failure, *containment_errors],
            )
        raise blocked from failure

    def _handle_ready_failure(
        self,
        prepared: PreparedMaterialAttempt,
        failure: MaterialOperationRunnerError,
    ) -> MaterialOperationOutcome | None:
        record = self._record_for(prepared)
        containment_errors: list[BaseException] = []
        termination: TerminationEvidence | None = None
        pipe_drained = self._drain_pipe_for_quarantine(record, containment_errors)
        try:
            if record.job_owner is None or record.process_owner is None or record.root_pid is None:
                raise MaterialOperationRunnerError(
                    "ready_containment_unavailable",
                    "ready failure has no exact Job/process/root ownership",
                )
            termination = _WINDOWS_RUNNER_OPS.terminate_job_and_collect(
                job_owner=record.job_owner,
                process_owner=record.process_owner,
                root_pid=record.root_pid,
                never_resumed=(
                    _PHASE_INDEX[record.phase]
                    < _PHASE_INDEX[MaterialLaunchPhase.RESUMED_NO_SECRET]
                ),
            )
        except BaseException as error:
            containment_errors.append(error)

        if termination is None:
            self._terminate_job_for_quarantine(record, containment_errors)
            reason_code = "io_drain_blocked" if not pipe_drained else "worker_ready_blocked"
            try:
                quarantine_dir = self._commit_quarantine(
                    prepared,
                    reason_code=reason_code,
                    evidence=None,
                )
            except BaseException as error:
                containment_errors.append(error)
                raise MaterialOperationRunnerError(
                    "quarantine_commit_failed",
                    "ready failure could not be durably entered into quarantine",
                ) from BaseExceptionGroup(
                    "ready failure quarantine failures",
                    [failure, *containment_errors],
                )
            blocked = MaterialOperationBlocked(
                execution_id=record.execution_id,
                attempt_id=record.attempt_id,
                reason_code=reason_code,
                job_instance_id=record.job_instance_id,
                quarantine_dir=quarantine_dir,
            )
            raise blocked from BaseExceptionGroup(
                "ready failure containment unavailable",
                [failure, *containment_errors],
            )

        reconciliation_committed = False
        try:
            self._commit_queried_empty_reconciliation(prepared, termination)
            reconciliation_committed = True
        except BaseException as error:
            containment_errors.append(error)

        if not reconciliation_committed:
            try:
                quarantine_dir = self._commit_quarantine(
                    prepared,
                    reason_code="reconciliation_blocked",
                    evidence=termination,
                )
            except BaseException as error:
                containment_errors.append(error)
                raise MaterialOperationRunnerError(
                    "quarantine_commit_failed",
                    "ready failure could not preserve its termination proof",
                ) from BaseExceptionGroup(
                    "ready reconciliation failures",
                    [failure, *containment_errors],
                )
            blocked = MaterialOperationBlocked(
                execution_id=record.execution_id,
                attempt_id=record.attempt_id,
                reason_code="reconciliation_blocked",
                job_instance_id=record.job_instance_id,
                quarantine_dir=quarantine_dir,
            )
            raise blocked from BaseExceptionGroup(
                "ready reconciliation failures",
                [failure, *containment_errors],
            )

        is_timeout = failure.code in {
            "worker_ready_timeout",
            "operation_timeout",
        }
        if is_timeout and pipe_drained and not containment_errors:
            outcome = MaterialOperationOutcome(
                status="terminated",
                result=None,
                termination=termination,
            )
            try:
                self._commit_termination_outcome(prepared, outcome)
                self._close_completion_resources_once(record)
                with self._lock:
                    record.outcome = outcome
                return outcome
            except BaseException as error:
                containment_errors.append(error)
            fallback_reason = (
                "termination_close_blocked"
                if record.completion_close_attempted
                and not record.completion_resources_closed
                else ("io_drain_blocked" if not pipe_drained else "worker_ready_invalid")
            )
            try:
                quarantine_dir = self._commit_quarantine(
                    prepared,
                    reason_code=fallback_reason,
                    evidence=termination,
                )
            except BaseException as error:
                containment_errors.append(error)
                raise MaterialOperationRunnerError(
                    "quarantine_commit_failed",
                    "terminated outcome could not be durably quarantined",
                ) from BaseExceptionGroup(
                    "terminated outcome quarantine failures",
                    [failure, *containment_errors],
                )
            blocked = MaterialOperationBlocked(
                execution_id=record.execution_id,
                attempt_id=record.attempt_id,
                reason_code=fallback_reason,
                job_instance_id=record.job_instance_id,
                quarantine_dir=quarantine_dir,
            )
            raise blocked from BaseExceptionGroup(
                "terminated outcome close failures",
                [failure, *containment_errors],
            )

        close_unknown = False
        if pipe_drained and not containment_errors:
            try:
                self._close_completion_resources_once(record)
            except BaseException as error:
                containment_errors.append(error)
                close_unknown = True
        reason_code = (
            "termination_close_blocked"
            if close_unknown
            or (record.completion_close_attempted and not record.completion_resources_closed)
            else ("io_drain_blocked" if not pipe_drained else "worker_ready_invalid")
        )
        try:
            quarantine_dir = self._commit_quarantine(
                prepared,
                reason_code=reason_code,
                evidence=termination,
            )
        except BaseException as error:
            containment_errors.append(error)
            raise MaterialOperationRunnerError(
                "quarantine_commit_failed",
                "ready failure could not be durably quarantined",
            ) from BaseExceptionGroup(
                "ready failure quarantine failures",
                [failure, *containment_errors],
            )
        blocked = MaterialOperationBlocked(
            execution_id=record.execution_id,
            attempt_id=record.attempt_id,
            reason_code=reason_code,
            job_instance_id=record.job_instance_id,
            quarantine_dir=quarantine_dir,
        )
        if containment_errors:
            raise blocked from BaseExceptionGroup(
                "ready failure close failures",
                [failure, *containment_errors],
            )
        raise blocked from failure

    def _commit_termination_outcome(
        self,
        prepared: PreparedMaterialAttempt,
        outcome: MaterialOperationOutcome,
        *,
        allow_natural: bool = False,
    ) -> None:
        if (
            outcome.status != "terminated"
            and not (
                allow_natural
                and outcome.status in {"succeeded", "failed"}
                and outcome.result is not None
            )
        ):
            raise MaterialOperationRunnerError(
                "termination_outcome_invalid",
                "termination journal requires a terminal outcome",
            )
        if outcome.status == "terminated" and outcome.result is not None:
            raise MaterialOperationRunnerError(
                "termination_outcome_invalid",
                "terminated outcome journal requires a result-less outcome",
            )
        if type(outcome.termination) is not TerminationEvidence:
            raise MaterialOperationRunnerError(
                "termination_outcome_invalid",
                "termination journal requires typed termination evidence",
            )
        outcome.termination.__post_init__()
        with self._lock:
            self._ensure_open()
            record = self._record_for(prepared)
            _validate_prepared_snapshot(record, prepared)
            if record.retired:
                raise MaterialOperationRunnerError(
                    "attempt_already_retired",
                    "a retired attempt cannot commit a termination outcome",
                )
            termination = outcome.termination
            if termination.root_pid != record.root_pid:
                raise MaterialOperationRunnerError(
                    "termination_outcome_identity_invalid",
                    "termination evidence root PID does not match the owned attempt",
                )
            if record.termination_outcome_hash is not None:
                existing_bytes = _read_verified_bytes(
                    parent=record.control_directory,
                    filename=TERMINATION_OUTCOME_FILENAME,
                    max_bytes=MAX_TERMINATION_OUTCOME_BYTES,
                )
                (
                    existing_execution_id,
                    existing_attempt_id,
                    existing_job_instance_id,
                    existing_termination,
                    existing_observed_at,
                ) = _decode_termination_outcome_bytes(existing_bytes)
                if (
                    existing_execution_id != record.execution_id
                    or existing_attempt_id != record.attempt_id
                    or existing_job_instance_id != record.job_instance_id
                    or existing_termination != termination
                    or existing_observed_at != termination.observed_at
                ):
                    raise MaterialOperationRunnerError(
                        "termination_outcome_changed",
                        "owned termination outcome differs from durable journal",
                    )
                record.termination_outcome_hash = hashlib.sha256(
                    existing_bytes
                ).hexdigest()
                return
            payload = {
                "protocol_version": DESCRIPTOR_PROTOCOL_VERSION,
                "state": "terminated",
                "execution_id": record.execution_id,
                "attempt_id": record.attempt_id,
                "job_instance_id": record.job_instance_id,
                "termination": _termination_to_mapping(termination),
                "observed_at": termination.observed_at,
            }
            try:
                existing_bytes = _read_verified_bytes(
                    parent=record.control_directory,
                    filename=TERMINATION_OUTCOME_FILENAME,
                    max_bytes=MAX_TERMINATION_OUTCOME_BYTES,
                )
            except Exception as error:
                if getattr(error, "winerror", None) not in {2, 3}:
                    raise
            else:
                (
                    existing_execution_id,
                    existing_attempt_id,
                    existing_job_instance_id,
                    existing_termination,
                    existing_observed_at,
                ) = _decode_termination_outcome_bytes(existing_bytes)
                if (
                    existing_execution_id != record.execution_id
                    or existing_attempt_id != record.attempt_id
                    or existing_job_instance_id != record.job_instance_id
                    or existing_termination != termination
                    or existing_observed_at != termination.observed_at
                ):
                    raise MaterialOperationRunnerError(
                        "termination_outcome_changed",
                        "durable termination outcome belongs to another result",
                    )
                record.termination_outcome_hash = hashlib.sha256(
                    existing_bytes
                ).hexdigest()
                return
            try:
                if allow_natural:
                    encoded_payload = json.dumps(
                        payload,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8", errors="strict")
                    receipt = write_durable_bytes(
                        parent=record.control_directory,
                        filename=TERMINATION_OUTCOME_FILENAME,
                        payload=encoded_payload,
                        replace=False,
                        max_bytes=MAX_TERMINATION_OUTCOME_BYTES,
                    )
                else:
                    receipt = write_durable_json(
                        parent=record.control_directory,
                        filename=TERMINATION_OUTCOME_FILENAME,
                        payload=payload,
                        replace=False,
                        max_bytes=MAX_TERMINATION_OUTCOME_BYTES,
                    )
            except Exception as error:
                # A controller crash can occur after the move but before the
                # in-memory receipt is installed.  Re-open once and accept
                # only the exact typed journal, never a guessed success.
                try:
                    existing_bytes = _read_verified_bytes(
                        parent=record.control_directory,
                        filename=TERMINATION_OUTCOME_FILENAME,
                        max_bytes=MAX_TERMINATION_OUTCOME_BYTES,
                    )
                except Exception:
                    raise error
                (
                    existing_execution_id,
                    existing_attempt_id,
                    existing_job_instance_id,
                    existing_termination,
                    existing_observed_at,
                ) = _decode_termination_outcome_bytes(existing_bytes)
                if (
                    existing_execution_id != record.execution_id
                    or existing_attempt_id != record.attempt_id
                    or existing_job_instance_id != record.job_instance_id
                    or existing_termination != termination
                    or existing_observed_at != termination.observed_at
                ):
                    raise MaterialOperationRunnerError(
                        "termination_outcome_changed",
                        "durable termination outcome belongs to another result",
                    ) from error
                record.termination_outcome_hash = hashlib.sha256(
                    existing_bytes
                ).hexdigest()
                return
            record.termination_outcome_hash = receipt.sha256

    def _commit_reconciliation(
        self,
        prepared: PreparedMaterialAttempt,
        evidence: ReconciliationEvidence,
    ) -> ReconciliationEvidence:
        if type(evidence) is not ReconciliationEvidence:
            raise MaterialOperationRunnerError(
                "reconciliation_evidence_invalid",
                "reconciliation commit requires typed evidence",
            )
        evidence.__post_init__()
        with self._lock:
            self._ensure_open()
            record = self._record_for(prepared)
            _validate_prepared_snapshot(record, prepared)
            if record.retired:
                raise MaterialOperationRunnerError(
                    "attempt_already_retired",
                    "a retired attempt cannot commit reconciliation evidence",
                )
            if record.reconciliation is not None:
                if record.reconciliation == evidence:
                    persisted_bytes = _read_verified_bytes(
                        parent=record.control_directory,
                        filename=TERMINATION_FILENAME,
                        max_bytes=MAX_TERMINATION_BYTES,
                    )
                    persisted = _decode_reconciliation_bytes(persisted_bytes)
                    if persisted != evidence:
                        raise MaterialOperationRunnerError(
                            "reconciliation_changed",
                            "durable termination evidence differs from owned evidence",
                        )
                    record.reconciliation_hash = hashlib.sha256(
                        persisted_bytes
                    ).hexdigest()
                    return persisted
                raise MaterialOperationRunnerError(
                    "reconciliation_already_committed",
                    "another reconciliation result already owns this attempt",
                )
            if (
                evidence.execution_id != record.execution_id
                or evidence.attempt_id != record.attempt_id
                or evidence.job_instance_id != record.job_instance_id
                or evidence.container_sha256 != record.container_hash
                or evidence.root_pid != record.root_pid
                or evidence.operation_may_have_started
                != (_PHASE_INDEX[record.phase] >= _OPERATION_STARTED_INDEX)
            ):
                raise MaterialOperationRunnerError(
                    "reconciliation_identity_invalid",
                    "reconciliation evidence does not match the owned descriptor tip",
                )
            descriptor_bytes = _read_verified_bytes(
                parent=record.control_directory,
                filename=CONTAINER_FILENAME,
                max_bytes=MAX_DESCRIPTOR_BYTES,
            )
            if hashlib.sha256(descriptor_bytes).hexdigest() != record.container_hash:
                raise MaterialOperationRunnerError(
                    "descriptor_changed",
                    "current descriptor bytes no longer match the owned tip",
                )
            descriptor = decode_material_container_descriptor(descriptor_bytes)
            _match_current_descriptor(descriptor, record)
            try:
                existing_bytes = _read_verified_bytes(
                    parent=record.control_directory,
                    filename=TERMINATION_FILENAME,
                    max_bytes=MAX_TERMINATION_BYTES,
                )
            except Exception as error:
                if getattr(error, "winerror", None) not in {2, 3}:
                    raise
            else:
                existing = _decode_reconciliation_bytes(existing_bytes)
                if existing != evidence:
                    raise MaterialOperationRunnerError(
                        "reconciliation_changed",
                        "durable termination evidence belongs to another result",
                    )
                record.reconciliation = existing
                record.reconciliation_hash = hashlib.sha256(existing_bytes).hexdigest()
                return existing
            payload = _reconciliation_to_mapping(evidence)
            try:
                receipt = write_durable_json(
                    parent=record.control_directory,
                    filename=TERMINATION_FILENAME,
                    payload=payload,
                    replace=False,
                    max_bytes=MAX_TERMINATION_BYTES,
                )
            except Exception as error:
                # A durable move may have succeeded before the controller
                # received its receipt.  Re-read and accept only an exact
                # typed match; never infer success from the write exception.
                try:
                    existing_bytes = _read_verified_bytes(
                        parent=record.control_directory,
                        filename=TERMINATION_FILENAME,
                        max_bytes=MAX_TERMINATION_BYTES,
                    )
                except Exception:
                    raise error
                existing = _decode_reconciliation_bytes(existing_bytes)
                if existing != evidence:
                    raise MaterialOperationRunnerError(
                        "reconciliation_changed",
                        "durable termination evidence belongs to another result",
                    ) from error
                record.reconciliation = existing
                record.reconciliation_hash = hashlib.sha256(
                    existing_bytes
                ).hexdigest()
                return existing
            record.reconciliation = evidence
            record.reconciliation_hash = receipt.sha256
            return evidence

    def _commit_queried_empty_reconciliation(
        self,
        prepared: PreparedMaterialAttempt,
        termination: TerminationEvidence,
    ) -> ReconciliationEvidence:
        if type(termination) is not TerminationEvidence:
            raise MaterialOperationRunnerError(
                "termination_evidence_invalid",
                "queried-empty reconciliation requires typed termination evidence",
        )
        termination.__post_init__()
        record = self._record_for(prepared)
        self._synchronize_descriptor_tip_after_lost_receipt(record, prepared)
        if termination.root_pid != record.root_pid or termination.active_processes != 0:
            raise MaterialOperationRunnerError(
                "reconciliation_identity_invalid",
                "termination evidence does not match the owned root and empty Job",
            )
        evidence = ReconciliationEvidence(
            execution_id=record.execution_id,
            attempt_id=record.attempt_id,
            job_instance_id=record.job_instance_id,
            method="queried_empty",
            observation_basis="queried_job",
            active_processes=0,
            root_pid=termination.root_pid,
            operation_may_have_started=(
                _PHASE_INDEX[record.phase] >= _OPERATION_STARTED_INDEX
            ),
            observed_at=termination.observed_at,
            container_sha256=record.container_hash,
        )
        return self._commit_reconciliation(prepared, evidence)

    def _synchronize_descriptor_tip_after_lost_receipt(
        self,
        record: _PreparedRecord,
        prepared: PreparedMaterialAttempt,
    ) -> None:
        """Adopt only an exact durable immediate successor after lost acknowledgement."""
        descriptor_bytes = _read_verified_bytes(
            parent=record.control_directory,
            filename=CONTAINER_FILENAME,
            max_bytes=MAX_DESCRIPTOR_BYTES,
        )
        durable_hash = hashlib.sha256(descriptor_bytes).hexdigest()
        descriptor = decode_material_container_descriptor(descriptor_bytes)
        if durable_hash == record.container_hash:
            _match_current_descriptor(descriptor, record)
            return
        if not _matches_unacknowledged_successor(descriptor, record):
            raise MaterialOperationRunnerError(
                "descriptor_changed",
                "quarantine cannot bind to an unrecognized durable descriptor tip",
            )
        old_phase = record.phase
        old_hash = record.container_hash
        record.previous_phase = old_phase
        record.previous_descriptor_sha256 = old_hash
        record.phase = descriptor.phase
        record.container_hash = durable_hash
        prepared.container_hash = durable_hash

    def _commit_quarantine(
        self,
        prepared: PreparedMaterialAttempt,
        *,
        reason_code: str,
        evidence: ReconciliationEvidence | TerminationEvidence | None,
    ) -> Path:
        if type(reason_code) is not str or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,63}", reason_code
        ):
            raise MaterialOperationRunnerError(
                "quarantine_reason_invalid",
                "quarantine reason code is not a bounded lower-case token",
            )
        with self._lock:
            self._ensure_open()
            record = self._record_for(prepared)
            _validate_prepared_snapshot(record, prepared)
            if record.retired:
                raise MaterialOperationRunnerError(
                    "attempt_already_retired",
                    "a retired attempt cannot enter quarantine",
                )
            self._synchronize_descriptor_tip_after_lost_receipt(record, prepared)
            if record.quarantine_hash is not None:
                existing_bytes = _read_verified_bytes(
                    parent=record.control_directory,
                    filename=QUARANTINE_FILENAME,
                    max_bytes=MAX_QUARANTINE_BYTES,
                )
                existing = _decode_quarantine_bytes(existing_bytes)
                existing_termination = existing["termination"]
                if (
                    existing["reason_code"] != reason_code
                    or existing["execution_id"] != record.execution_id
                    or existing["attempt_id"] != record.attempt_id
                    or existing["control_id"] != record.control_id
                    or existing["job_instance_id"] != record.job_instance_id
                    or existing["phase"] != record.phase.value
                    or existing["operation_may_have_started"]
                    != (_PHASE_INDEX[record.phase] >= _OPERATION_STARTED_INDEX)
                    or existing["container_sha256"] != record.container_hash
                    or (
                        evidence is None
                        and existing_termination is not None
                    )
                    or (
                        evidence is not None
                        and existing_termination != evidence
                    )
                ):
                    raise MaterialOperationRunnerError(
                        "quarantine_changed",
                        "owned quarantine journal differs from requested evidence",
                    )
                record.quarantine_hash = hashlib.sha256(
                    existing_bytes
                ).hexdigest()
                record.blocked = True
                return record.control_directory.path
            payload = {
                "protocol_version": DESCRIPTOR_PROTOCOL_VERSION,
                "state": "blocked",
                "reason_code": reason_code,
                "execution_id": record.execution_id,
                "attempt_id": record.attempt_id,
                "control_id": record.control_id,
                "job_instance_id": record.job_instance_id,
                "phase": record.phase.value,
                "operation_may_have_started": (
                    _PHASE_INDEX[record.phase] >= _OPERATION_STARTED_INDEX
                ),
                "container_sha256": record.container_hash,
                "termination": (
                    None
                    if evidence is None
                    else (
                        _reconciliation_to_mapping(evidence)
                        if type(evidence) is ReconciliationEvidence
                        else _termination_to_mapping(evidence)
                    )
                ),
                "quarantined_at": _utc_now(),
            }
            try:
                receipt = write_durable_json(
                    parent=record.control_directory,
                    filename=QUARANTINE_FILENAME,
                    payload=payload,
                    replace=False,
                    max_bytes=MAX_QUARANTINE_BYTES,
                )
            except Exception as error:
                # Recover a receipt lost after an atomic move only by reading
                # and validating the complete controller journal.
                try:
                    existing_bytes = _read_verified_bytes(
                        parent=record.control_directory,
                        filename=QUARANTINE_FILENAME,
                        max_bytes=MAX_QUARANTINE_BYTES,
                    )
                except Exception:
                    raise error
                existing = _decode_quarantine_bytes(existing_bytes)
                expected_termination = evidence
                existing_termination = existing["termination"]
                if (
                    existing["reason_code"] != reason_code
                    or existing["execution_id"] != record.execution_id
                    or existing["attempt_id"] != record.attempt_id
                    or existing["control_id"] != record.control_id
                    or existing["job_instance_id"] != record.job_instance_id
                    or existing["phase"] != record.phase.value
                    or existing["operation_may_have_started"]
                    != (_PHASE_INDEX[record.phase] >= _OPERATION_STARTED_INDEX)
                    or existing["container_sha256"] != record.container_hash
                    or (
                        expected_termination is None
                        and existing_termination is not None
                    )
                    or (
                        expected_termination is not None
                        and existing_termination != expected_termination
                    )
                ):
                    raise MaterialOperationRunnerError(
                        "quarantine_changed",
                        "durable quarantine journal belongs to another failure",
                    ) from error
                record.quarantine_hash = hashlib.sha256(
                    existing_bytes
                ).hexdigest()
                record.blocked = True
                return record.control_directory.path
            record.quarantine_hash = receipt.sha256
            record.blocked = True
            return record.control_directory.path

    def _commit_quarantine_resolution(self, record: _PreparedRecord) -> None:
        """Durably acknowledge that a previously pending pipe drain resolved."""
        if record.quarantine_hash is None or record.reconciliation_hash is None:
            raise MaterialOperationRunnerError(
                "quarantine_resolution_unavailable",
                "a pending drain needs both quarantine and termination journals",
            )
        if record.reconciliation is None:
            raise MaterialOperationRunnerError(
                "quarantine_resolution_unavailable",
                "a pending drain needs typed reconciliation evidence",
            )
        descriptor_bytes = _read_verified_bytes(
            parent=record.control_directory,
            filename=CONTAINER_FILENAME,
            max_bytes=MAX_DESCRIPTOR_BYTES,
        )
        descriptor_hash = hashlib.sha256(descriptor_bytes).hexdigest()
        if descriptor_hash != record.container_hash:
            raise MaterialOperationRunnerError(
                "descriptor_changed",
                "quarantine resolution cannot bind to a changed descriptor tip",
            )
        descriptor = decode_material_container_descriptor(descriptor_bytes)
        _match_current_descriptor(descriptor, record)
        quarantine_bytes = _read_verified_bytes(
            parent=record.control_directory,
            filename=QUARANTINE_FILENAME,
            max_bytes=MAX_QUARANTINE_BYTES,
        )
        if hashlib.sha256(quarantine_bytes).hexdigest() != record.quarantine_hash:
            raise MaterialOperationRunnerError(
                "quarantine_changed",
                "quarantine resolution does not bind to its owned journal",
            )
        quarantine = _decode_quarantine_bytes(quarantine_bytes)
        if (
            quarantine["reason_code"] != "io_drain_blocked"
            or quarantine["execution_id"] != record.execution_id
            or quarantine["attempt_id"] != record.attempt_id
            or quarantine["control_id"] != record.control_id
            or quarantine["job_instance_id"] != record.job_instance_id
            or quarantine["container_sha256"] != record.container_hash
        ):
            raise MaterialOperationRunnerError(
                "quarantine_resolution_unavailable",
                "only the owned io-drain quarantine may be resolved",
            )
        termination_bytes = _read_verified_bytes(
            parent=record.control_directory,
            filename=TERMINATION_FILENAME,
            max_bytes=MAX_TERMINATION_BYTES,
        )
        if hashlib.sha256(termination_bytes).hexdigest() != record.reconciliation_hash:
            raise MaterialOperationRunnerError(
                "reconciliation_changed",
                "quarantine resolution does not bind to its termination journal",
            )
        termination = _decode_reconciliation_bytes(termination_bytes)
        if termination != record.reconciliation:
            raise MaterialOperationRunnerError(
                "reconciliation_changed",
                "quarantine resolution termination evidence differs from the owned result",
            )
        nested = quarantine["termination"]
        if isinstance(nested, ReconciliationEvidence):
            if nested != record.reconciliation:
                raise MaterialOperationRunnerError(
                    "quarantine_changed",
                    "quarantine resolution nested evidence differs from the owned result",
                )
        elif isinstance(nested, TerminationEvidence):
            if (
                nested.root_pid != record.reconciliation.root_pid
                or nested.active_processes != record.reconciliation.active_processes
            ):
                raise MaterialOperationRunnerError(
                    "quarantine_changed",
                    "quarantine resolution nested termination differs from the owned result",
                )
        elif (
            nested is None
            and record.reconciliation.method
            == "last_handle_close_kill_on_close"
            and record.last_handle_closed_hash is not None
        ):
            # The original io-drain quarantine can precede the later
            # intentional close.  Its separate resolution journal binds the
            # newly durable termination and close journals to that immutable
            # quarantine without rewriting history.
            _, closed_bytes = _read_last_handle_close_journal_pair(
                control=record.control_directory,
                descriptor=descriptor,
                descriptor_bytes=descriptor_bytes,
            )
            if (
                closed_bytes is None
                or hashlib.sha256(closed_bytes).hexdigest()
                != record.last_handle_closed_hash
            ):
                raise MaterialOperationRunnerError(
                    "last_handle_journal_changed",
                    "io-drain resolution does not bind to the closed Job journal",
                )
        else:
            raise MaterialOperationRunnerError(
                "quarantine_changed",
                "io-drain quarantine has no terminal evidence to resolve",
            )
        payload = {
            "protocol_version": DESCRIPTOR_PROTOCOL_VERSION,
            "state": "resolved",
            "execution_id": record.execution_id,
            "attempt_id": record.attempt_id,
            "control_id": record.control_id,
            "job_instance_id": record.job_instance_id,
            "quarantine_sha256": record.quarantine_hash,
            "container_sha256": record.container_hash,
            "termination_sha256": record.reconciliation_hash,
            "resolved_at": _utc_now(),
        }
        try:
            existing_bytes = _read_verified_bytes(
                parent=record.control_directory,
                filename=QUARANTINE_RESOLUTION_FILENAME,
                max_bytes=MAX_QUARANTINE_RESOLUTION_BYTES,
            )
        except Exception as error:
            if getattr(error, "winerror", None) not in {2, 3}:
                raise
        else:
            existing = _decode_quarantine_resolution_bytes(existing_bytes)
            if any(
                existing[key] != payload[key]
                for key in payload
                if key != "resolved_at"
            ):
                raise MaterialOperationRunnerError(
                    "quarantine_resolution_changed",
                    "an existing quarantine resolution belongs to another tip",
                )
            record.quarantine_resolution_hash = hashlib.sha256(
                existing_bytes
            ).hexdigest()
            return
        try:
            receipt = write_durable_json(
                parent=record.control_directory,
                filename=QUARANTINE_RESOLUTION_FILENAME,
                payload=payload,
                replace=False,
                max_bytes=MAX_QUARANTINE_RESOLUTION_BYTES,
            )
        except Exception as error:
            try:
                existing_bytes = _read_verified_bytes(
                    parent=record.control_directory,
                    filename=QUARANTINE_RESOLUTION_FILENAME,
                    max_bytes=MAX_QUARANTINE_RESOLUTION_BYTES,
                )
                existing = _decode_quarantine_resolution_bytes(existing_bytes)
            except Exception:
                raise error
            if any(
                existing[key] != payload[key]
                for key in payload
                if key != "resolved_at"
            ):
                raise MaterialOperationRunnerError(
                    "quarantine_resolution_changed",
                    "a raced quarantine resolution belongs to another tip",
                ) from error
            resolution_hash = hashlib.sha256(existing_bytes).hexdigest()
        else:
            resolution_hash = receipt.sha256
        record.quarantine_resolution_hash = resolution_hash

    def _raise_blocked_after_result_failure(
        self,
        prepared: PreparedMaterialAttempt,
        failure: MaterialOperationRunnerError,
    ) -> None:
        record = self._record_for(prepared)
        evidence: ReconciliationEvidence | None = record.reconciliation
        termination: TerminationEvidence | None = None
        containment_errors: list[BaseException] = []
        pipe_drained = self._drain_pipe_for_quarantine(record, containment_errors)
        if evidence is None:
            if (
                record.job_owner is None
                or record.process_owner is None
                or record.root_pid is None
            ):
                containment_errors.append(
                    MaterialOperationRunnerError(
                        "result_containment_unavailable",
                        "result failure has no exact Job/process/root ownership",
                    )
                )
            else:
                # The Job-empty wait normally means the worker exited
                # naturally, so preserve that stronger natural evidence when
                # available.  A malformed result can race the final root
                # signal; in that case terminate-and-collect closes the gap
                # instead of merely issuing a zero-time query.
                natural_error: BaseException | None = None
                try:
                    candidate = _WINDOWS_RUNNER_OPS.collect_natural_termination(
                        process_owner=record.process_owner,
                        root_pid=record.root_pid,
                    )
                    candidate.__post_init__()
                    if (
                        candidate.root_pid != record.root_pid
                        or candidate.termination_method != "natural"
                        or candidate.active_processes != 0
                    ):
                        raise MaterialOperationRunnerError(
                            "termination_evidence_invalid",
                            "natural result-failure evidence is not a completed root",
                        )
                    termination = candidate
                except BaseException as error:
                    natural_error = error
                if termination is None:
                    try:
                        termination = _WINDOWS_RUNNER_OPS.terminate_job_and_collect(
                            job_owner=record.job_owner,
                            process_owner=record.process_owner,
                            root_pid=record.root_pid,
                            never_resumed=False,
                        )
                    except BaseException as error:
                        containment_errors.extend(
                            [
                                natural_error
                                if natural_error is not None
                                else MaterialOperationRunnerError(
                                    "result_containment_unavailable",
                                    "natural termination evidence was unavailable",
                                ),
                                error,
                            ]
                        )
                if termination is not None:
                    try:
                        evidence = self._commit_queried_empty_reconciliation(
                            prepared,
                            termination,
                        )
                    except BaseException as error:
                        containment_errors.append(error)
                        evidence = None
            if termination is None:
                self._terminate_job_for_quarantine(record, containment_errors)
        close_unknown = False
        if (
            evidence is not None
            and pipe_drained
            and not containment_errors
            and not record.completion_resources_closed
            and not record.completion_close_attempted
        ):
            try:
                if record.process_owner is None or record.job_owner is None:
                    raise MaterialOperationRunnerError(
                        "result_containment_unavailable",
                        "result failure lacks exact process or Job ownership",
                    )
                self._close_completion_resources_once(record)
            except BaseException as error:
                containment_errors.append(error)
                close_unknown = True
        reason_code = (
            "termination_close_blocked"
            if close_unknown
            or (record.completion_close_attempted and not record.completion_resources_closed)
            else (
                "io_drain_blocked"
                if not pipe_drained
                else (
                    "result_containment_blocked"
                    if evidence is None and termination is None
                    else "worker_result_invalid"
                )
            )
        )
        try:
            quarantine_dir = self._commit_quarantine(
                prepared,
                reason_code=reason_code,
                evidence=(evidence if evidence is not None else termination),
            )
        except BaseException as error:
            containment_errors.append(error)
            raise MaterialOperationRunnerError(
                "quarantine_commit_failed",
                "result failure could not be durably entered into quarantine",
            ) from BaseExceptionGroup(
                "result quarantine commit failures",
                [failure, *containment_errors],
            )
        blocked = MaterialOperationBlocked(
            execution_id=record.execution_id,
            attempt_id=record.attempt_id,
            reason_code=reason_code,
            job_instance_id=record.job_instance_id,
            quarantine_dir=quarantine_dir,
        )
        if containment_errors:
            raise blocked from BaseExceptionGroup(
                "result failure quarantine containment",
                [failure, *containment_errors],
            )
        raise blocked from failure

    def reconcile(
        self,
        *,
        execution_id: str,
        attempt_id: str,
        control_id: str,
    ) -> ReconciliationEvidence:
        _validate_uuid(execution_id, "execution_id")
        _validate_uuid(attempt_id, "attempt_id")
        _validate_uuid(control_id, "control_id")
        with self._lock:
            self._ensure_open()
            matches = [
                record
                for record in self._records.values()
                if record.execution_id == execution_id
                and record.attempt_id == attempt_id
                and record.control_id == control_id
            ]
            if len(matches) != 1:
                return self._reconcile_durable_control(
                    execution_id=execution_id,
                    attempt_id=attempt_id,
                    control_id=control_id,
                )
            record = matches[0]
            _validate_prepared_snapshot(record, record.prepared)
            if record.retired:
                raise MaterialOperationRunnerError(
                    "attempt_already_retired",
                    "retired attempts cannot be reconciled by the runner",
                )
            if record.reconciliation is not None:
                if (
                    record.reconciliation.method
                    == "last_handle_close_kill_on_close"
                ):
                    return self._resume_committed_last_handle_reconciliation(
                        record,
                        record.reconciliation,
                    )
                if (
                    record.completion_close_attempted
                    and not record.completion_resources_closed
                ):
                    raise MaterialOperationRunnerError(
                        "reconciliation_blocked",
                        "completion handles have an unresolved close attempt",
                    )
                if record.io_drain_pending or (
                    record.pipe_close_attempted and not record.pipe_closed
                ):
                    raise MaterialOperationRunnerError(
                        "reconciliation_blocked",
                        "secret-pipe I/O has an unresolved cancellation or close attempt",
                    )
                if (
                    record.process_quarantine_owner is not None
                    and not record.process_quarantine_cleaned
                ):
                    raise MaterialOperationRunnerError(
                        "reconciliation_blocked",
                        "atomic launch resources remain in quarantine ownership",
                    )
                try:
                    persisted_bytes = _read_verified_bytes(
                        parent=record.control_directory,
                        filename=TERMINATION_FILENAME,
                        max_bytes=MAX_TERMINATION_BYTES,
                    )
                except Exception:
                    raise MaterialOperationRunnerError(
                        "reconciliation_invalid",
                        "cached reconciliation has no readable durable journal",
                    ) from None
                persisted = _decode_reconciliation_bytes(persisted_bytes)
                if persisted != record.reconciliation:
                    raise MaterialOperationRunnerError(
                        "reconciliation_changed",
                        "cached reconciliation differs from its durable journal",
                    )
                if (
                    record.reconciliation_hash is None
                    or hashlib.sha256(persisted_bytes).hexdigest()
                    != record.reconciliation_hash
                ):
                    raise MaterialOperationRunnerError(
                        "reconciliation_changed",
                        "cached reconciliation bytes differ from the owned hash",
                    )
                try:
                    quarantine_bytes = _read_verified_bytes(
                        parent=record.control_directory,
                        filename=QUARANTINE_FILENAME,
                        max_bytes=MAX_QUARANTINE_BYTES,
                    )
                except Exception as quarantine_error:
                    if getattr(quarantine_error, "winerror", None) not in {2, 3}:
                        raise
                    if record.quarantine_hash is not None:
                        raise MaterialOperationRunnerError(
                            "reconciliation_changed",
                            "cached reconciliation lost its durable quarantine journal",
                        ) from quarantine_error
                    quarantine_bytes = None
                if quarantine_bytes is not None:
                    if (
                        record.quarantine_hash is None
                        or hashlib.sha256(quarantine_bytes).hexdigest()
                        != record.quarantine_hash
                    ):
                        raise MaterialOperationRunnerError(
                            "reconciliation_changed",
                            "cached quarantine bytes differ from the owned hash",
                        )
                    descriptor_bytes = _read_verified_bytes(
                        parent=record.control_directory,
                        filename=CONTAINER_FILENAME,
                        max_bytes=MAX_DESCRIPTOR_BYTES,
                    )
                    descriptor = decode_material_container_descriptor(
                        descriptor_bytes
                    )
                    _match_current_descriptor(descriptor, record)
                    quarantine = _decode_quarantine_bytes(quarantine_bytes)
                    if (
                        quarantine["execution_id"] != record.execution_id
                        or quarantine["attempt_id"] != record.attempt_id
                        or quarantine["control_id"] != record.control_id
                        or quarantine["job_instance_id"] != record.job_instance_id
                        or quarantine["phase"] != record.phase.value
                        or quarantine["container_sha256"] != record.container_hash
                    ):
                        raise MaterialOperationRunnerError(
                            "reconciliation_changed",
                            "cached quarantine does not bind to its descriptor tip",
                        )
                    if (
                        quarantine["reason_code"] != "io_drain_blocked"
                        or record.reconciliation.method
                        not in {
                            "owner_exit_kill_on_close",
                            "last_handle_close_kill_on_close",
                            "queried_empty",
                        }
                    ):
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "cached reconciliation has unresolved native ownership",
                        )
                    if (
                        quarantine["termination"] is None
                        and record.reconciliation.method
                        not in {
                            "owner_exit_kill_on_close",
                            "last_handle_close_kill_on_close",
                        }
                    ):
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "cached io-drain quarantine lacks terminal evidence",
                        )
                    _commit_restart_io_drain_resolution(
                        control=record.control_directory,
                        descriptor=descriptor,
                        expected_descriptor_bytes=descriptor_bytes,
                        evidence=record.reconciliation,
                        termination_bytes=persisted_bytes,
                        quarantine_bytes=quarantine_bytes,
                    )
                    resolution_bytes = _read_verified_bytes(
                        parent=record.control_directory,
                        filename=QUARANTINE_RESOLUTION_FILENAME,
                        max_bytes=MAX_QUARANTINE_RESOLUTION_BYTES,
                    )
                    record.quarantine_resolution_hash = hashlib.sha256(
                        resolution_bytes
                    ).hexdigest()
                return persisted
            if (
                record.phase is not MaterialLaunchPhase.PLANNED
                or record.claim_acquired
                or record.job_owner is not None
                or record.pipe_owner is not None
                or record.process_owner is not None
                or record.process_quarantine_owner is not None
                or record.root_pid is not None
            ):
                raise MaterialOperationRunnerError(
                    "reconciliation_blocked",
                    "this slice cannot honestly prove never-created or queried-empty",
                )
            evidence = ReconciliationEvidence(
                execution_id=record.execution_id,
                attempt_id=record.attempt_id,
                job_instance_id=record.job_instance_id,
                method="never_created",
                observation_basis="pre_create_journal",
                active_processes=None,
                root_pid=None,
                operation_may_have_started=False,
                observed_at=_utc_now(),
                container_sha256=record.container_hash,
            )
            return self._commit_reconciliation(record.prepared, evidence)

    def _reconcile_durable_control(
        self,
        *,
        execution_id: str,
        attempt_id: str,
        control_id: str,
        expected_job_instance_id: str | None = None,
        expected_operation: MaterialOperationKind | None = None,
        expected_descriptor_bytes: bytes | None = None,
        require_dead_controller: bool = False,
        ownership_guard: MaterialRestartOwnershipGuard | None = None,
        owned_control: ApprovedPhysicalDirectory | None = None,
        expected_retirement_bytes: bytes | None = None,
    ) -> ReconciliationEvidence:
        """Recover an already-durable reconciliation after controller restart."""
        control: ApprovedPhysicalDirectory | None = owned_control
        close_control = owned_control is None
        try:
            if control is None:
                control = approve_physical_d_write_root(
                    self._root.path / ".jobs-control" / control_id
                )
            else:
                control.verify()
                expected_path = self._root.path / ".jobs-control" / control_id
                if _normcase(str(control.path)) != _normcase(str(expected_path)):
                    raise MaterialOperationRunnerError(
                        "reconciliation_attempt_not_owned",
                        "owned control pin is not the requested durable directory",
                    )
            descriptor_bytes = _read_verified_bytes(
                parent=control,
                filename=CONTAINER_FILENAME,
                max_bytes=MAX_DESCRIPTOR_BYTES,
            )
            if (
                expected_descriptor_bytes is not None
                and descriptor_bytes != expected_descriptor_bytes
            ):
                raise MaterialOperationRunnerError(
                    "reconciliation_changed",
                    "durable descriptor changed before restart reconciliation",
                )
            descriptor = decode_material_container_descriptor(descriptor_bytes)
            if (
                descriptor.execution_id != execution_id
                or descriptor.attempt_id != attempt_id
                or descriptor.control_id != control_id
                or (
                    expected_job_instance_id is not None
                    and descriptor.job_instance_id != expected_job_instance_id
                )
                or (
                    expected_operation is not None
                    and descriptor.operation != expected_operation
                )
            ):
                raise MaterialOperationRunnerError(
                    "reconciliation_attempt_not_owned",
                    "durable container identity does not match the requested attempt",
                )
            current_retirement_bytes = _read_optional_verified_bytes(
                parent=control,
                filename=RETIRED_FILENAME,
                max_bytes=MAX_RETIREMENT_BYTES,
            )
            if (
                expected_retirement_bytes is None
                and current_retirement_bytes is not None
            ):
                raise MaterialOperationRunnerError(
                    "attempt_already_retired",
                    "retired attempts cannot create or adopt reconciliation evidence",
                )
            if (
                expected_retirement_bytes is not None
                and current_retirement_bytes != expected_retirement_bytes
            ):
                raise MaterialOperationRunnerError(
                    "restart_retirement_tombstone_mismatch",
                    "retirement tombstone changed before reconciliation adoption",
                )
            if _read_optional_verified_bytes(
                parent=control,
                filename=RETIREMENT_RELEASE_AUDIT_FILENAME,
                max_bytes=MAX_RETIREMENT_RELEASE_AUDIT_BYTES,
            ) is not None:
                raise MaterialOperationRunnerError(
                    "reconciliation_blocked",
                    "retirement pin release is already in an unknown audit state",
                )
            if require_dead_controller:
                _prove_adopted_processes_exited(descriptor)
            try:
                termination_bytes = _read_verified_bytes(
                    parent=control,
                    filename=TERMINATION_FILENAME,
                    max_bytes=MAX_TERMINATION_BYTES,
                )
            except Exception as error:
                if getattr(error, "winerror", None) not in {2, 3}:
                    raise
                descriptor_hash = hashlib.sha256(descriptor_bytes).hexdigest()
                phase_index = _PHASE_INDEX[descriptor.phase]
                if descriptor.phase is MaterialLaunchPhase.PLANNED:
                    # The controller claims the attempt before publishing the
                    # first request and before advancing the descriptor.  A
                    # crash can therefore leave a live, already-created Job
                    # behind while the durable tip still says ``planned``.
                    # Treat this exactly like ``job_configured``: only an
                    # exact dead-controller proof permits never-created
                    # recovery.
                    try:
                        controller_exited = _WINDOWS_RUNNER_OPS.wait_exact_process_exit(
                            pid=descriptor.controller_pid,
                            creation_time=descriptor.controller_creation_time,
                            timeout_seconds=0.0,
                        )
                    except MaterialOperationRunnerError as identity_error:
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "controller exit could not be proven for a planned attempt",
                        ) from identity_error
                    if not controller_exited:
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "the planned attempt's controller is still alive",
                        )
                    evidence = ReconciliationEvidence(
                        execution_id=execution_id,
                        attempt_id=attempt_id,
                        job_instance_id=descriptor.job_instance_id,
                        method="never_created",
                        observation_basis="pre_create_journal",
                        active_processes=None,
                        root_pid=None,
                        operation_may_have_started=False,
                        observed_at=_utc_now(),
                        container_sha256=descriptor_hash,
                    )
                elif descriptor.phase is MaterialLaunchPhase.JOB_CONFIGURED:
                    # The Job exists, but the durable create_intent boundary
                    # has not been crossed.  Only an exited controller may be
                    # considered abandoned; a live controller could still
                    # legitimately continue this attempt.
                    try:
                        controller_exited = _WINDOWS_RUNNER_OPS.wait_exact_process_exit(
                            pid=descriptor.controller_pid,
                            creation_time=descriptor.controller_creation_time,
                            timeout_seconds=0.0,
                        )
                    except MaterialOperationRunnerError as identity_error:
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "controller exit could not be proven for a configured Job",
                        ) from identity_error
                    if not controller_exited:
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "the recorded controller is still alive",
                        )
                    evidence = ReconciliationEvidence(
                        execution_id=execution_id,
                        attempt_id=attempt_id,
                        job_instance_id=descriptor.job_instance_id,
                        method="never_created",
                        observation_basis="pre_create_journal",
                        active_processes=None,
                        root_pid=None,
                        operation_may_have_started=False,
                        observed_at=_utc_now(),
                        container_sha256=descriptor_hash,
                    )
                elif phase_index >= _PHASE_INDEX[MaterialLaunchPhase.CREATE_INTENT]:
                    try:
                        controller_exited = _WINDOWS_RUNNER_OPS.wait_exact_process_exit(
                            pid=descriptor.controller_pid,
                            creation_time=descriptor.controller_creation_time,
                            timeout_seconds=0.0,
                        )
                    except MaterialOperationRunnerError as identity_error:
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "controller exit could not be proven for owner-exit inference",
                        ) from identity_error
                    if not controller_exited:
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "the recorded controller is still alive",
                        )
                    root_pid = descriptor.root_pid
                    if root_pid is not None:
                        if descriptor.root_creation_time is None:
                            raise MaterialOperationRunnerError(
                                "reconciliation_changed",
                                "durable root identity is incomplete",
                            )
                        try:
                            root_exited = _WINDOWS_RUNNER_OPS.wait_exact_process_exit(
                                pid=root_pid,
                                creation_time=descriptor.root_creation_time,
                                timeout_seconds=10.0,
                            )
                        except MaterialOperationRunnerError as identity_error:
                            raise MaterialOperationRunnerError(
                                "reconciliation_blocked",
                                "root exit could not be proven for owner-exit inference",
                            ) from identity_error
                        if not root_exited:
                            raise MaterialOperationRunnerError(
                                "reconciliation_blocked",
                                "the recorded root process is still alive",
                            )
                    evidence = ReconciliationEvidence(
                        execution_id=execution_id,
                        attempt_id=attempt_id,
                        job_instance_id=descriptor.job_instance_id,
                        method="owner_exit_kill_on_close",
                        observation_basis="controller_exit_last_handle_inference",
                        active_processes=None,
                        root_pid=root_pid,
                        operation_may_have_started=(
                            phase_index >= _OPERATION_STARTED_INDEX
                        ),
                        observed_at=_utc_now(),
                        container_sha256=descriptor_hash,
                    )
                else:
                    raise MaterialOperationRunnerError(
                        "reconciliation_attempt_not_owned",
                        "the durable attempt has no termination proof after controller restart",
                    ) from None
                try:
                    quarantine_bytes = _read_verified_bytes(
                        parent=control,
                        filename=QUARANTINE_FILENAME,
                        max_bytes=MAX_QUARANTINE_BYTES,
                    )
                except Exception as quarantine_error:
                    if getattr(quarantine_error, "winerror", None) not in {2, 3}:
                        raise
                    quarantine_bytes = None
                if quarantine_bytes is not None:
                    quarantine = _decode_quarantine_bytes(quarantine_bytes)
                    if (
                        quarantine["execution_id"] != execution_id
                        or quarantine["attempt_id"] != attempt_id
                        or quarantine["control_id"] != control_id
                        or quarantine["job_instance_id"]
                        != descriptor.job_instance_id
                        or quarantine["phase"] != descriptor.phase.value
                        or quarantine["container_sha256"] != descriptor_hash
                    ):
                        raise MaterialOperationRunnerError(
                            "reconciliation_changed",
                            "durable quarantine does not bind to its container tip",
                        )
                    if (
                        evidence.method != "owner_exit_kill_on_close"
                        or quarantine["reason_code"] != "io_drain_blocked"
                        or quarantine["termination"] is not None
                    ):
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "durable quarantine cannot be resolved by owner-exit proof",
                        )
                committed = _commit_restart_reconciliation(
                    ownership_guard=ownership_guard,
                    control=control,
                    evidence=evidence,
                    expected_descriptor_bytes=descriptor_bytes,
                )
                if quarantine_bytes is not None:
                    termination_bytes = _read_verified_bytes(
                        parent=control,
                        filename=TERMINATION_FILENAME,
                        max_bytes=MAX_TERMINATION_BYTES,
                    )
                    if _decode_reconciliation_bytes(termination_bytes) != committed:
                        raise MaterialOperationRunnerError(
                            "reconciliation_changed",
                            "restart termination changed before io-drain resolution",
                        )
                    _commit_restart_io_drain_resolution(
                        ownership_guard=ownership_guard,
                        control=control,
                        descriptor=descriptor,
                        expected_descriptor_bytes=descriptor_bytes,
                        evidence=committed,
                        termination_bytes=termination_bytes,
                        quarantine_bytes=quarantine_bytes,
                    )
                return committed
            evidence = _decode_reconciliation_bytes(termination_bytes)
            if (
                evidence.execution_id != execution_id
                or evidence.attempt_id != attempt_id
                or evidence.job_instance_id != descriptor.job_instance_id
                or evidence.container_sha256
                != hashlib.sha256(descriptor_bytes).hexdigest()
            ):
                raise MaterialOperationRunnerError(
                    "reconciliation_changed",
                    "durable reconciliation does not bind to its container tip",
                )
            operation_started = (
                _PHASE_INDEX[descriptor.phase] >= _OPERATION_STARTED_INDEX
            )
            if evidence.operation_may_have_started != operation_started:
                raise MaterialOperationRunnerError(
                    "reconciliation_changed",
                    "durable reconciliation start-state disagrees with its descriptor phase",
                )
            last_handle_pair: tuple[bytes, bytes] | None = None
            if evidence.method == "never_created":
                if (
                    descriptor.phase
                    not in {
                        MaterialLaunchPhase.PLANNED,
                        MaterialLaunchPhase.JOB_CONFIGURED,
                    }
                    or evidence.active_processes is not None
                    or evidence.root_pid is not None
                    or evidence.operation_may_have_started
                ):
                    raise MaterialOperationRunnerError(
                        "reconciliation_changed",
                        "never-created evidence is incompatible with the durable descriptor",
                    )
                if descriptor.phase in {
                    MaterialLaunchPhase.PLANNED,
                    MaterialLaunchPhase.JOB_CONFIGURED,
                }:
                    try:
                        controller_exited = _WINDOWS_RUNNER_OPS.wait_exact_process_exit(
                            pid=descriptor.controller_pid,
                            creation_time=descriptor.controller_creation_time,
                            timeout_seconds=0.0,
                        )
                    except MaterialOperationRunnerError as identity_error:
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "controller exit could not be re-proven for never-created evidence",
                        ) from identity_error
                    if not controller_exited:
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "the configured Job controller is still alive",
                        )
            elif evidence.method == "owner_exit_kill_on_close":
                if (
                    _PHASE_INDEX[descriptor.phase]
                    < _PHASE_INDEX[MaterialLaunchPhase.CREATE_INTENT]
                    or evidence.active_processes is not None
                    or evidence.root_pid != descriptor.root_pid
                ):
                    raise MaterialOperationRunnerError(
                        "reconciliation_changed",
                        "owner-exit evidence is incompatible with the durable descriptor",
                    )
                try:
                    controller_exited = _WINDOWS_RUNNER_OPS.wait_exact_process_exit(
                        pid=descriptor.controller_pid,
                        creation_time=descriptor.controller_creation_time,
                        timeout_seconds=0.0,
                    )
                except MaterialOperationRunnerError as identity_error:
                    raise MaterialOperationRunnerError(
                        "reconciliation_blocked",
                        "controller exit could not be re-proven for owner-exit evidence",
                    ) from identity_error
                if not controller_exited:
                    raise MaterialOperationRunnerError(
                        "reconciliation_blocked",
                        "the recorded controller is still alive",
                    )
                if descriptor.root_pid is not None:
                    if descriptor.root_creation_time is None:
                        raise MaterialOperationRunnerError(
                            "reconciliation_changed",
                            "durable root identity is incomplete",
                        )
                    try:
                        root_exited = _WINDOWS_RUNNER_OPS.wait_exact_process_exit(
                            pid=descriptor.root_pid,
                            creation_time=descriptor.root_creation_time,
                            timeout_seconds=10.0,
                        )
                    except MaterialOperationRunnerError as identity_error:
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "root exit could not be re-proven for owner-exit evidence",
                        ) from identity_error
                    if not root_exited:
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "the recorded root process is still alive",
                        )
            elif evidence.method == "last_handle_close_kill_on_close":
                intent_bytes, closed_bytes = _read_last_handle_close_journal_pair(
                    control=control,
                    descriptor=descriptor,
                    descriptor_bytes=descriptor_bytes,
                )
                if intent_bytes is None or closed_bytes is None:
                    raise MaterialOperationRunnerError(
                        "reconciliation_blocked",
                        "last-handle evidence has no complete durable close proof",
                    )
                last_handle_pair = (intent_bytes, closed_bytes)
                if evidence.root_pid != descriptor.root_pid:
                    raise MaterialOperationRunnerError(
                        "reconciliation_changed",
                        "last-handle evidence is not bound to the durable root identity",
                    )
                if descriptor.root_pid is not None:
                    if descriptor.root_creation_time is None:
                        raise MaterialOperationRunnerError(
                            "reconciliation_changed",
                            "last-handle durable root identity is incomplete",
                        )
                    try:
                        root_exited = _WINDOWS_RUNNER_OPS.wait_exact_process_exit(
                            pid=descriptor.root_pid,
                            creation_time=descriptor.root_creation_time,
                            timeout_seconds=10.0,
                        )
                    except MaterialOperationRunnerError as identity_error:
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "root exit could not be re-proven for last-handle evidence",
                        ) from identity_error
                    if not root_exited:
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "the recorded root is still alive after last-handle close",
                        )
            elif evidence.method == "queried_empty":
                if (
                    descriptor.root_pid is None
                    or evidence.active_processes != 0
                    or evidence.root_pid != descriptor.root_pid
                ):
                    raise MaterialOperationRunnerError(
                        "reconciliation_changed",
                        "queried-empty evidence is not bound to the durable root identity",
                    )
            try:
                quarantine_bytes = _read_verified_bytes(
                    parent=control,
                    filename=QUARANTINE_FILENAME,
                    max_bytes=MAX_QUARANTINE_BYTES,
                )
            except Exception as error:
                if getattr(error, "winerror", None) not in {2, 3}:
                    raise
                if evidence.method == "last_handle_close_kill_on_close":
                    raise MaterialOperationRunnerError(
                        "reconciliation_blocked",
                        "last-handle evidence has no durable quarantine boundary",
                    ) from None
            else:
                quarantine = _decode_quarantine_bytes(quarantine_bytes)
                if (
                    quarantine["execution_id"] != execution_id
                    or quarantine["attempt_id"] != attempt_id
                    or quarantine["control_id"] != control_id
                    or quarantine["job_instance_id"] != descriptor.job_instance_id
                    or quarantine["phase"] != descriptor.phase.value
                    or quarantine["container_sha256"]
                    != hashlib.sha256(descriptor_bytes).hexdigest()
                ):
                    raise MaterialOperationRunnerError(
                        "reconciliation_changed",
                        "durable quarantine does not bind to its container tip",
                    )
                if evidence.method == "last_handle_close_kill_on_close" and (
                    quarantine["reason_code"] != "io_drain_blocked"
                    or quarantine["termination"] is not None
                ):
                    raise MaterialOperationRunnerError(
                        "reconciliation_blocked",
                        "last-handle evidence is outside its exact io-drain quarantine",
                    )
                if quarantine["termination"] is not None:
                    quarantined_evidence = quarantine["termination"]
                    if isinstance(quarantined_evidence, ReconciliationEvidence):
                        if quarantined_evidence != evidence:
                            raise MaterialOperationRunnerError(
                                "reconciliation_changed",
                                "quarantine reconciliation differs from termination journal",
                            )
                    elif isinstance(quarantined_evidence, TerminationEvidence):
                        if (
                            quarantined_evidence.root_pid != evidence.root_pid
                            or quarantined_evidence.active_processes
                            != evidence.active_processes
                        ):
                            raise MaterialOperationRunnerError(
                                "reconciliation_changed",
                                "quarantine termination differs from termination journal",
                            )
                if quarantine["reason_code"] in {
                    "io_drain_blocked",
                    "close_unknown",
                    "termination_close_blocked",
                    "reconciliation_blocked",
                    "result_containment_blocked",
                    "launch_cleanup_blocked",
                }:
                    # A pending overlapped drain is the only quarantine state
                    # that can be resolved after the original controller has
                    # closed its native handles.  The resolution is a separate
                    # immutable journal bound to the exact quarantine,
                    # descriptor tip, and termination journal.  Other close
                    # or containment-unknown states remain permanently
                    # blocked; accepting them would turn inference into proof.
                    if quarantine["reason_code"] != "io_drain_blocked":
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "durable quarantine records unresolved native ownership",
                        )
                    if (
                        quarantine["termination"] is None
                        and evidence.method
                        not in {
                            "owner_exit_kill_on_close",
                            "last_handle_close_kill_on_close",
                        }
                    ):
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "io-drain quarantine lacks terminal evidence",
                        )
                    try:
                        resolution_bytes = _read_verified_bytes(
                            parent=control,
                            filename=QUARANTINE_RESOLUTION_FILENAME,
                            max_bytes=MAX_QUARANTINE_RESOLUTION_BYTES,
                        )
                        resolution = _decode_quarantine_resolution_bytes(
                            resolution_bytes
                        )
                    except Exception:
                        if evidence.method not in {
                            "owner_exit_kill_on_close",
                            "last_handle_close_kill_on_close",
                            "queried_empty",
                        }:
                            raise MaterialOperationRunnerError(
                                "reconciliation_blocked",
                                "io-drain quarantine has no valid durable resolution",
                            ) from None
                        _commit_restart_io_drain_resolution(
                            ownership_guard=ownership_guard,
                            control=control,
                            descriptor=descriptor,
                            expected_descriptor_bytes=descriptor_bytes,
                            evidence=evidence,
                            termination_bytes=termination_bytes,
                            quarantine_bytes=quarantine_bytes,
                        )
                        try:
                            resolution_bytes = _read_verified_bytes(
                                parent=control,
                                filename=QUARANTINE_RESOLUTION_FILENAME,
                                max_bytes=MAX_QUARANTINE_RESOLUTION_BYTES,
                            )
                            resolution = _decode_quarantine_resolution_bytes(
                                resolution_bytes
                            )
                        except Exception:
                            raise MaterialOperationRunnerError(
                                "reconciliation_blocked",
                                "io-drain quarantine resolution could not be recovered",
                            ) from None
                    descriptor_hash = hashlib.sha256(descriptor_bytes).hexdigest()
                    termination_hash = hashlib.sha256(termination_bytes).hexdigest()
                    quarantine_hash = hashlib.sha256(quarantine_bytes).hexdigest()
                    if (
                        resolution["execution_id"] != execution_id
                        or resolution["attempt_id"] != attempt_id
                        or resolution["control_id"] != control_id
                        or resolution["job_instance_id"]
                        != descriptor.job_instance_id
                        or resolution["container_sha256"] != descriptor_hash
                        or resolution["quarantine_sha256"] != quarantine_hash
                        or resolution["termination_sha256"] != termination_hash
                        or resolution["container_sha256"] != evidence.container_sha256
                    ):
                        raise MaterialOperationRunnerError(
                            "reconciliation_blocked",
                            "io-drain resolution does not bind to the durable evidence tip",
                        )
            if last_handle_pair is not None:
                _assert_last_handle_pair_unchanged(
                    control=control,
                    expected_intent_bytes=last_handle_pair[0],
                    expected_closed_bytes=last_handle_pair[1],
                )
            _assert_restart_descriptor_tip(
                control=control,
                expected_descriptor_bytes=descriptor_bytes,
            )
            return evidence
        except MaterialOperationRunnerError:
            raise
        except Exception:
            raise MaterialOperationRunnerError(
                "reconciliation_attempt_not_owned",
                "no recoverable durable reconciliation belongs to this attempt",
            ) from None
        finally:
            if close_control and control is not None:
                control.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if any(not record.retired for record in self._records.values()):
                raise MaterialOperationRunnerError(
                    "live_attempts_owned",
                    "runner still owns a prepared or claimed attempt",
                )
            self._closed = True
            if self._owns_root:
                self._root.close()

    def _create_independent_attempt_roots(
        self,
        *,
        execution_id: str,
        jobs: ApprovedPhysicalDirectory,
        controls: ApprovedPhysicalDirectory,
    ) -> tuple[
        str,
        str,
        str,
        ApprovedPhysicalDirectory,
        ApprovedPhysicalDirectory,
    ]:
        attempt_id, control_id, job_instance_id = _new_independent_ids(
            execution_id
        )
        attempt: ApprovedPhysicalDirectory | None = None
        control: ApprovedPhysicalDirectory | None = None
        try:
            attempt = jobs.create_exclusive_child(attempt_id)
            control = controls.create_exclusive_child(control_id)
            return (
                attempt_id,
                control_id,
                job_instance_id,
                attempt,
                control,
            )
        except FileExistsError:
            if control is not None:
                control.close()
            if attempt is not None:
                attempt.close()
            raise MaterialOperationRunnerError(
                "exclusive_directory_collision",
                "attempt or control UUID directory already exists",
            ) from None
        except BaseException:
            if control is not None:
                control.close()
            if attempt is not None:
                attempt.close()
            raise

    def _record_for(self, prepared: PreparedMaterialAttempt) -> _PreparedRecord:
        if type(prepared) is not PreparedMaterialAttempt:
            raise MaterialOperationRunnerError(
                "prepared_type_invalid",
                "prepared must be PreparedMaterialAttempt",
            )
        record = self._records.get(id(prepared))
        if record is None or record.prepared is not prepared:
            raise MaterialOperationRunnerError(
                "foreign_prepared_attempt",
                "prepared attempt is not owned by this runner",
            )
        return record

    def _ensure_open(self) -> None:
        if self._closed:
            raise MaterialOperationRunnerError(
                "runner_closed",
                "runner ownership has already been closed",
            )

    def __enter__(self) -> ProcessMaterialOperationRunner:
        self._ensure_open()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def _planned_descriptor(
    *,
    execution_id: str,
    attempt_id: str,
    control_id: str,
    job_instance_id: str,
    operation: MaterialOperationKind,
    controller: _ControllerIdentity,
) -> MaterialOperationContainerDescriptor:
    return MaterialOperationContainerDescriptor(
        protocol_version=DESCRIPTOR_PROTOCOL_VERSION,
        phase=MaterialLaunchPhase.PLANNED,
        previous_phase=None,
        previous_descriptor_sha256=None,
        execution_id=execution_id,
        attempt_id=attempt_id,
        control_id=control_id,
        job_instance_id=job_instance_id,
        operation=operation,
        controller_pid=controller.pid,
        controller_creation_time=controller.creation_time,
        operation_may_have_started=False,
    )


def encode_material_container_descriptor(
    descriptor: MaterialOperationContainerDescriptor,
) -> bytes:
    if type(descriptor) is not MaterialOperationContainerDescriptor:
        raise MaterialOperationRunnerError(
            "descriptor_type_invalid",
            "descriptor must be MaterialOperationContainerDescriptor",
        )
    descriptor.__post_init__()
    try:
        encoded = json.dumps(
            _descriptor_to_mapping(descriptor),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        raise MaterialOperationRunnerError(
            "descriptor_encoding_failed",
            "descriptor cannot be encoded as strict UTF-8 JSON",
        ) from None
    if len(encoded) > MAX_DESCRIPTOR_BYTES:
        raise MaterialOperationRunnerError(
            "descriptor_size_invalid",
            "descriptor exceeds its 64 KiB byte limit",
        )
    return encoded


def decode_material_container_descriptor(
    raw: bytes,
) -> MaterialOperationContainerDescriptor:
    value = _decode_strict_descriptor_json(raw)
    if type(value) is not dict:
        raise MaterialOperationRunnerError(
            "descriptor_invalid",
            "descriptor must be a JSON object",
        )
    phase = _coerce_launch_phase(value.get("phase"))
    expected_fields = set(_DESCRIPTOR_COMMON_FIELDS)
    phase_index = _PHASE_INDEX[phase]
    if phase_index >= _REQUEST_HASH_INDEX:
        expected_fields.add("request_sha256")
        expected_fields.add("job_handle_inheritable")
    if phase_index >= _ROOT_IDENTITY_INDEX:
        expected_fields.update(
            (
                "root_pid",
                "root_creation_time",
                "job_active_processes_after_create",
            )
        )
    if set(value) != expected_fields:
        raise MaterialOperationRunnerError(
            "descriptor_fields_invalid",
            "descriptor fields are not exact for its launch phase",
        )
    previous_raw = value["previous_phase"]
    previous_phase = (
        None if previous_raw is None else _coerce_launch_phase(previous_raw)
    )
    return MaterialOperationContainerDescriptor(
        protocol_version=value["protocol_version"],  # type: ignore[arg-type]
        phase=phase,
        previous_phase=previous_phase,
        previous_descriptor_sha256=value["previous_descriptor_sha256"],  # type: ignore[arg-type]
        execution_id=value["execution_id"],  # type: ignore[arg-type]
        attempt_id=value["attempt_id"],  # type: ignore[arg-type]
        control_id=value["control_id"],  # type: ignore[arg-type]
        job_instance_id=value["job_instance_id"],  # type: ignore[arg-type]
        operation=value["operation"],  # type: ignore[arg-type]
        controller_pid=value["controller_pid"],  # type: ignore[arg-type]
        controller_creation_time=value["controller_creation_time"],  # type: ignore[arg-type]
        operation_may_have_started=value["operation_may_have_started"],  # type: ignore[arg-type]
        request_sha256=value.get("request_sha256"),  # type: ignore[arg-type]
        job_handle_inheritable=value.get("job_handle_inheritable"),  # type: ignore[arg-type]
        root_pid=value.get("root_pid"),  # type: ignore[arg-type]
        root_creation_time=value.get("root_creation_time"),  # type: ignore[arg-type]
        job_active_processes_after_create=value.get(
            "job_active_processes_after_create"
        ),  # type: ignore[arg-type]
    )


def _reconciliation_to_mapping(
    evidence: ReconciliationEvidence,
) -> dict[str, object]:
    evidence.__post_init__()
    return {
        "protocol_version": DESCRIPTOR_PROTOCOL_VERSION,
        "execution_id": evidence.execution_id,
        "attempt_id": evidence.attempt_id,
        "job_instance_id": evidence.job_instance_id,
        "method": evidence.method,
        "observation_basis": evidence.observation_basis,
        "active_processes": evidence.active_processes,
        "root_pid": evidence.root_pid,
        "operation_may_have_started": evidence.operation_may_have_started,
        "observed_at": evidence.observed_at,
        "container_sha256": evidence.container_sha256,
    }


def _commit_restart_reconciliation(
    *,
    ownership_guard: MaterialRestartOwnershipGuard | None = None,
    control: ApprovedPhysicalDirectory,
    evidence: ReconciliationEvidence,
    expected_descriptor_bytes: bytes,
) -> ReconciliationEvidence:
    """Create or adopt one exact termination journal after controller restart."""
    evidence.__post_init__()
    _assert_restart_descriptor_tip(
        control=control,
        expected_descriptor_bytes=expected_descriptor_bytes,
    )
    payload = _reconciliation_to_mapping(evidence)
    try:
        if ownership_guard is not None:
            ownership_guard.assert_owned()
        write_durable_json(
            parent=control,
            filename=TERMINATION_FILENAME,
            payload=payload,
            replace=False,
            max_bytes=MAX_TERMINATION_BYTES,
        )
    except Exception as error:
        try:
            existing_bytes = _read_verified_bytes(
                parent=control,
                filename=TERMINATION_FILENAME,
                max_bytes=MAX_TERMINATION_BYTES,
            )
            existing = _decode_reconciliation_bytes(existing_bytes)
        except Exception:
            raise error
        if existing != evidence:
            raise MaterialOperationRunnerError(
                "reconciliation_changed",
                "a raced restart reconciliation journal does not match the owned descriptor",
            ) from error
        _assert_restart_descriptor_tip(
            control=control,
            expected_descriptor_bytes=expected_descriptor_bytes,
        )
        return existing
    _assert_restart_descriptor_tip(
        control=control,
        expected_descriptor_bytes=expected_descriptor_bytes,
    )
    return evidence


def _commit_restart_io_drain_resolution(
    *,
    ownership_guard: MaterialRestartOwnershipGuard | None = None,
    control: ApprovedPhysicalDirectory,
    descriptor: MaterialOperationContainerDescriptor,
    expected_descriptor_bytes: bytes,
    evidence: ReconciliationEvidence,
    termination_bytes: bytes,
    quarantine_bytes: bytes,
) -> None:
    """Create/adopt the resolution after controller-exit or closed-handle proof."""
    descriptor_hash = hashlib.sha256(expected_descriptor_bytes).hexdigest()
    if evidence.method not in {
        "owner_exit_kill_on_close",
        "last_handle_close_kill_on_close",
        "queried_empty",
    }:
        raise MaterialOperationRunnerError(
            "reconciliation_blocked",
            "io-drain restart resolution requires an approved inference method",
        )
    if _decode_reconciliation_bytes(termination_bytes) != evidence:
        raise MaterialOperationRunnerError(
            "reconciliation_changed",
            "io-drain resolution termination bytes changed",
        )
    quarantine = _decode_quarantine_bytes(quarantine_bytes)
    if (
        quarantine["reason_code"] != "io_drain_blocked"
        or quarantine["execution_id"] != descriptor.execution_id
        or quarantine["attempt_id"] != descriptor.attempt_id
        or quarantine["control_id"] != descriptor.control_id
        or quarantine["job_instance_id"] != descriptor.job_instance_id
        or quarantine["phase"] != descriptor.phase.value
        or quarantine["container_sha256"] != descriptor_hash
    ):
        raise MaterialOperationRunnerError(
            "reconciliation_blocked",
            "io-drain quarantine cannot be resolved by this restart evidence",
        )
    nested_termination = quarantine["termination"]
    if nested_termination is None:
        if evidence.method not in {
            "owner_exit_kill_on_close",
            "last_handle_close_kill_on_close",
        }:
            raise MaterialOperationRunnerError(
                "reconciliation_blocked",
                "queried-empty resolution lacks nested terminal evidence",
            )
    elif isinstance(nested_termination, ReconciliationEvidence):
        if nested_termination != evidence:
            raise MaterialOperationRunnerError(
                "reconciliation_changed",
                "io-drain resolution nested evidence differs from termination",
            )
    elif isinstance(nested_termination, TerminationEvidence):
        if (
            nested_termination.root_pid != evidence.root_pid
            or nested_termination.active_processes != evidence.active_processes
        ):
            raise MaterialOperationRunnerError(
                "reconciliation_changed",
                "io-drain resolution nested termination differs from termination",
            )
    else:
        raise MaterialOperationRunnerError(
            "reconciliation_changed",
            "io-drain resolution nested evidence is not typed",
        )
    payload = {
        "protocol_version": DESCRIPTOR_PROTOCOL_VERSION,
        "state": "resolved",
        "execution_id": descriptor.execution_id,
        "attempt_id": descriptor.attempt_id,
        "control_id": descriptor.control_id,
        "job_instance_id": descriptor.job_instance_id,
        "quarantine_sha256": hashlib.sha256(quarantine_bytes).hexdigest(),
        "container_sha256": descriptor_hash,
        "termination_sha256": hashlib.sha256(termination_bytes).hexdigest(),
        "resolved_at": _utc_now(),
    }
    existing_bytes: bytes | None = None
    try:
        existing_bytes = _read_verified_bytes(
            parent=control,
            filename=QUARANTINE_RESOLUTION_FILENAME,
            max_bytes=MAX_QUARANTINE_RESOLUTION_BYTES,
        )
    except Exception as error:
        if getattr(error, "winerror", None) not in {2, 3}:
            raise
    else:
        existing = _decode_quarantine_resolution_bytes(existing_bytes)
        if any(
            existing[key] != expected_value
            for key, expected_value in payload.items()
            if key != "resolved_at"
        ):
            raise MaterialOperationRunnerError(
                "reconciliation_blocked",
                "existing io-drain resolution belongs to another durable tip",
            )
        _assert_restart_descriptor_tip(
            control=control,
            expected_descriptor_bytes=expected_descriptor_bytes,
        )
    if existing_bytes is None:
        if ownership_guard is not None:
            ownership_guard.assert_owned()
        if evidence.method == "queried_empty":
            try:
                controller_exited = _WINDOWS_RUNNER_OPS.wait_exact_process_exit(
                    pid=descriptor.controller_pid,
                    creation_time=descriptor.controller_creation_time,
                    timeout_seconds=0.0,
                )
            except MaterialOperationRunnerError as identity_error:
                raise MaterialOperationRunnerError(
                    "reconciliation_blocked",
                    "controller exit could not be proven before io-drain recovery",
                ) from identity_error
            if not controller_exited:
                raise MaterialOperationRunnerError(
                    "reconciliation_blocked",
                    "the original controller is still alive before io-drain recovery",
                )
        try:
            if ownership_guard is not None:
                ownership_guard.assert_owned()
            write_durable_json(
                parent=control,
                filename=QUARANTINE_RESOLUTION_FILENAME,
                payload=payload,
                replace=False,
                max_bytes=MAX_QUARANTINE_RESOLUTION_BYTES,
            )
        except Exception as error:
            try:
                existing_bytes = _read_verified_bytes(
                    parent=control,
                    filename=QUARANTINE_RESOLUTION_FILENAME,
                    max_bytes=MAX_QUARANTINE_RESOLUTION_BYTES,
                )
                existing = _decode_quarantine_resolution_bytes(existing_bytes)
            except Exception:
                raise error
            if any(
                existing[key] != expected_value
                for key, expected_value in payload.items()
                if key != "resolved_at"
            ):
                raise MaterialOperationRunnerError(
                    "reconciliation_blocked",
                    "raced io-drain resolution belongs to another durable tip",
                ) from error
    _assert_restart_descriptor_tip(
        control=control,
        expected_descriptor_bytes=expected_descriptor_bytes,
    )
    try:
        current_quarantine = _read_verified_bytes(
            parent=control,
            filename=QUARANTINE_FILENAME,
            max_bytes=MAX_QUARANTINE_BYTES,
        )
        current_termination = _read_verified_bytes(
            parent=control,
            filename=TERMINATION_FILENAME,
            max_bytes=MAX_TERMINATION_BYTES,
        )
    except Exception as error:
        raise MaterialOperationRunnerError(
            "reconciliation_changed",
            "io-drain evidence disappeared while its resolution was committed",
        ) from error
    if (
        current_quarantine != quarantine_bytes
        or current_termination != termination_bytes
    ):
        raise MaterialOperationRunnerError(
            "reconciliation_changed",
            "io-drain evidence changed while its resolution was committed",
        )


def _assert_restart_descriptor_tip(
    *,
    control: ApprovedPhysicalDirectory,
    expected_descriptor_bytes: bytes,
) -> None:
    """Reject a descriptor rewrite that raced an exit wait or journal write."""
    if type(expected_descriptor_bytes) is not bytes:
        raise MaterialOperationRunnerError(
            "reconciliation_changed",
            "restart reconciliation has no immutable descriptor snapshot",
        )
    try:
        current_bytes = _read_verified_bytes(
            parent=control,
            filename=CONTAINER_FILENAME,
            max_bytes=MAX_DESCRIPTOR_BYTES,
        )
    except Exception as error:
        raise MaterialOperationRunnerError(
            "reconciliation_changed",
            "restart reconciliation could not re-read its descriptor tip",
        ) from error
    if current_bytes != expected_descriptor_bytes:
        raise MaterialOperationRunnerError(
            "reconciliation_changed",
            "restart reconciliation descriptor tip changed while owners were exiting",
        )


def _termination_to_mapping(
    evidence: TerminationEvidence,
) -> dict[str, object]:
    """Serialize the typed native termination proof without adding inference."""
    if type(evidence) is not TerminationEvidence:
        raise MaterialOperationRunnerError(
            "termination_evidence_invalid",
            "termination mapping requires typed TerminationEvidence",
        )
    evidence.__post_init__()
    return {
        "platform": evidence.platform,
        "root_pid": evidence.root_pid,
        "exit_code": evidence.exit_code,
        "termination_method": evidence.termination_method,
        "active_processes": evidence.active_processes,
        "observed_at": evidence.observed_at,
    }


def _decode_termination_outcome_bytes(
    raw: bytes,
) -> tuple[str, str, str, TerminationEvidence, str]:
    """Decode the controller-only terminated-outcome journal strictly."""
    try:
        value = _decode_strict_descriptor_json(raw)
    except MaterialOperationRunnerError:
        raise MaterialOperationRunnerError(
            "termination_outcome_invalid",
            "termination outcome is not strict bounded JSON",
        ) from None
    if type(value) is not dict or set(value) != {
        "protocol_version",
        "state",
        "execution_id",
        "attempt_id",
        "job_instance_id",
        "termination",
        "observed_at",
    }:
        raise MaterialOperationRunnerError(
            "termination_outcome_invalid",
            "termination outcome has unknown or missing fields",
        )
    if value["protocol_version"] != DESCRIPTOR_PROTOCOL_VERSION or value["state"] != "terminated":
        raise MaterialOperationRunnerError(
            "termination_outcome_invalid",
            "termination outcome protocol or state is unsupported",
        )
    termination_value = value["termination"]
    if type(termination_value) is not dict or set(termination_value) != {
        "platform",
        "root_pid",
        "exit_code",
        "termination_method",
        "active_processes",
        "observed_at",
    }:
        raise MaterialOperationRunnerError(
            "termination_outcome_invalid",
            "termination outcome evidence has unknown or missing fields",
        )
    try:
        _validate_uuid(value["execution_id"], "execution_id")
        _validate_uuid(value["attempt_id"], "attempt_id")
        _validate_uuid(value["job_instance_id"], "job_instance_id")
        termination = TerminationEvidence(
            platform=termination_value["platform"],  # type: ignore[arg-type]
            root_pid=termination_value["root_pid"],  # type: ignore[arg-type]
            exit_code=termination_value["exit_code"],  # type: ignore[arg-type]
            termination_method=termination_value["termination_method"],  # type: ignore[arg-type]
            active_processes=termination_value["active_processes"],  # type: ignore[arg-type]
            observed_at=termination_value["observed_at"],  # type: ignore[arg-type]
        )
        _validate_identity_time(value["observed_at"], "observed_at")
        if value["observed_at"] != termination.observed_at:
            raise MaterialOperationRunnerError(
                "termination_outcome_invalid",
                "journal and nested termination timestamps differ",
            )
    except (MaterialOperationRunnerError, MaterialProtocolError, TypeError, ValueError):
        raise MaterialOperationRunnerError(
            "termination_outcome_invalid",
            "termination outcome failed typed validation",
        ) from None
    return (
        value["execution_id"],  # type: ignore[return-value]
        value["attempt_id"],  # type: ignore[return-value]
        value["job_instance_id"],  # type: ignore[return-value]
        termination,
        value["observed_at"],  # type: ignore[return-value]
    )


def _decode_quarantine_bytes(raw: bytes) -> dict[str, object]:
    """Decode a controller quarantine journal with typed nested evidence."""
    try:
        value = _decode_strict_descriptor_json(raw)
    except MaterialOperationRunnerError:
        raise MaterialOperationRunnerError(
            "quarantine_invalid",
            "quarantine journal is not strict bounded JSON",
        ) from None
    expected = {
        "protocol_version",
        "state",
        "reason_code",
        "execution_id",
        "attempt_id",
        "control_id",
        "job_instance_id",
        "phase",
        "operation_may_have_started",
        "container_sha256",
        "termination",
        "quarantined_at",
    }
    if type(value) is not dict or set(value) != expected:
        raise MaterialOperationRunnerError(
            "quarantine_invalid",
            "quarantine journal has unknown or missing fields",
        )
    try:
        if value["protocol_version"] != DESCRIPTOR_PROTOCOL_VERSION:
            raise MaterialOperationRunnerError(
                "quarantine_invalid",
                "quarantine protocol version is unsupported",
            )
        if value["state"] != "blocked":
            raise MaterialOperationRunnerError(
                "quarantine_invalid",
                "quarantine state is unsupported",
            )
        if type(value["reason_code"]) is not str or re.fullmatch(
            r"[a-z][a-z0-9_]{0,63}", value["reason_code"]
        ) is None:
            raise MaterialOperationRunnerError(
                "quarantine_invalid",
                "quarantine reason code is invalid",
            )
        for field_name in (
            "execution_id",
            "attempt_id",
            "control_id",
            "job_instance_id",
        ):
            _validate_uuid(value[field_name], field_name)
        phase = _coerce_launch_phase(value["phase"])
        if type(value["operation_may_have_started"]) is not bool:
            raise MaterialOperationRunnerError(
                "quarantine_invalid",
                "quarantine operation state is invalid",
            )
        if value["operation_may_have_started"] is not (
            _PHASE_INDEX[phase] >= _OPERATION_STARTED_INDEX
        ):
            raise MaterialOperationRunnerError(
                "quarantine_invalid",
                "quarantine operation state disagrees with its phase",
            )
        _validate_sha256(
            value["container_sha256"],
            code="quarantine_invalid",
            detail="quarantine container hash is invalid",
        )
        _validate_identity_time(value["quarantined_at"], "quarantined_at")
        termination_value = value["termination"]
        typed_termination: ReconciliationEvidence | TerminationEvidence | None
        if termination_value is None:
            typed_termination = None
        elif type(termination_value) is dict and "method" in termination_value:
            typed_termination = _decode_reconciliation_bytes(
                json.dumps(
                    {
                        "protocol_version": DESCRIPTOR_PROTOCOL_VERSION,
                        **termination_value,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        elif type(termination_value) is dict:
            if set(termination_value) != {
                "platform",
                "root_pid",
                "exit_code",
                "termination_method",
                "active_processes",
                "observed_at",
            }:
                raise MaterialOperationRunnerError(
                    "quarantine_invalid",
                    "quarantine termination evidence is malformed",
                )
            typed_termination = TerminationEvidence(
                platform=termination_value["platform"],  # type: ignore[arg-type]
                root_pid=termination_value["root_pid"],  # type: ignore[arg-type]
                exit_code=termination_value["exit_code"],  # type: ignore[arg-type]
                termination_method=termination_value["termination_method"],  # type: ignore[arg-type]
                active_processes=termination_value["active_processes"],  # type: ignore[arg-type]
                observed_at=termination_value["observed_at"],  # type: ignore[arg-type]
            )
        else:
            raise MaterialOperationRunnerError(
                "quarantine_invalid",
                "quarantine termination evidence is malformed",
            )
        if isinstance(typed_termination, ReconciliationEvidence):
            if (
                typed_termination.execution_id != value["execution_id"]
                or typed_termination.attempt_id != value["attempt_id"]
                or typed_termination.job_instance_id != value["job_instance_id"]
                or typed_termination.container_sha256 != value["container_sha256"]
                or typed_termination.operation_may_have_started
                != value["operation_may_have_started"]
            ):
                raise MaterialOperationRunnerError(
                    "quarantine_invalid",
                    "nested reconciliation evidence does not match quarantine identity",
                )
    except (MaterialOperationRunnerError, MaterialProtocolError, TypeError, ValueError):
        raise MaterialOperationRunnerError(
            "quarantine_invalid",
            "quarantine journal failed typed validation",
        ) from None
    return {
        "protocol_version": DESCRIPTOR_PROTOCOL_VERSION,
        "state": "blocked",
        "reason_code": value["reason_code"],
        "execution_id": value["execution_id"],
        "attempt_id": value["attempt_id"],
        "control_id": value["control_id"],
        "job_instance_id": value["job_instance_id"],
        "phase": phase.value,
        "operation_may_have_started": value["operation_may_have_started"],
        "container_sha256": value["container_sha256"],
        "termination": typed_termination,
        "quarantined_at": value["quarantined_at"],
    }


def _decode_quarantine_resolution_bytes(raw: bytes) -> dict[str, object]:
    try:
        value = _decode_strict_descriptor_json(raw)
        expected = {
            "protocol_version",
            "state",
            "execution_id",
            "attempt_id",
            "control_id",
            "job_instance_id",
            "quarantine_sha256",
            "container_sha256",
            "termination_sha256",
            "resolved_at",
        }
        if type(value) is not dict or set(value) != expected:
            raise MaterialOperationRunnerError(
                "quarantine_resolution_invalid",
                "quarantine resolution has unknown or missing fields",
            )
        if value["protocol_version"] != DESCRIPTOR_PROTOCOL_VERSION:
            raise MaterialOperationRunnerError(
                "quarantine_resolution_invalid",
                "quarantine resolution protocol version is unsupported",
            )
        if value["state"] != "resolved":
            raise MaterialOperationRunnerError(
                "quarantine_resolution_invalid",
                "quarantine resolution state is unsupported",
            )
        for field_name in (
            "execution_id",
            "attempt_id",
            "control_id",
            "job_instance_id",
        ):
            _validate_uuid(value[field_name], field_name)
        _validate_sha256(
            value["quarantine_sha256"],
            code="quarantine_resolution_invalid",
            detail="quarantine resolution hash is invalid",
        )
        _validate_sha256(
            value["container_sha256"],
            code="quarantine_resolution_invalid",
            detail="quarantine resolution container hash is invalid",
        )
        _validate_sha256(
            value["termination_sha256"],
            code="quarantine_resolution_invalid",
            detail="quarantine resolution termination hash is invalid",
        )
        _validate_identity_time(value["resolved_at"], "resolved_at")
    except (MaterialOperationRunnerError, TypeError, ValueError):
        raise MaterialOperationRunnerError(
            "quarantine_resolution_invalid",
            "quarantine resolution failed typed validation",
        ) from None
    return value


def _decode_last_handle_close_intent_bytes(raw: bytes) -> dict[str, object]:
    return _decode_last_handle_close_journal(raw, closed=False)


def _decode_last_handle_closed_bytes(raw: bytes) -> dict[str, object]:
    return _decode_last_handle_close_journal(raw, closed=True)


def _decode_last_handle_close_journal(
    raw: bytes,
    *,
    closed: bool,
) -> dict[str, object]:
    code = "last_handle_closed_invalid" if closed else "last_handle_intent_invalid"
    timestamp_field = "closed_at" if closed else "intent_at"
    expected = {
        "protocol_version",
        "state",
        "execution_id",
        "attempt_id",
        "control_id",
        "job_instance_id",
        "phase",
        "operation_may_have_started",
        "container_sha256",
        "controller_pid",
        "controller_creation_time",
        "root_pid",
        "root_creation_time",
        timestamp_field,
    }
    if closed:
        expected.add("intent_sha256")
    try:
        value = _decode_strict_descriptor_json(raw)
        if type(value) is not dict or set(value) != expected:
            raise MaterialOperationRunnerError(
                code,
                "last-handle journal has unknown or missing fields",
            )
        if value["protocol_version"] != DESCRIPTOR_PROTOCOL_VERSION:
            raise MaterialOperationRunnerError(
                code,
                "last-handle journal protocol version is unsupported",
            )
        if value["state"] != ("closed" if closed else "intent"):
            raise MaterialOperationRunnerError(
                code,
                "last-handle journal state is unsupported",
            )
        for field_name in (
            "execution_id",
            "attempt_id",
            "control_id",
            "job_instance_id",
        ):
            _validate_uuid(value[field_name], field_name)
        phase = _coerce_launch_phase(value["phase"])
        if (
            _PHASE_INDEX[phase]
            < _PHASE_INDEX[MaterialLaunchPhase.CREATE_INTENT]
        ):
            raise MaterialOperationRunnerError(
                code,
                "last-handle close is illegal before create_intent",
            )
        expected_started = _PHASE_INDEX[phase] >= _OPERATION_STARTED_INDEX
        if value["operation_may_have_started"] is not expected_started:
            raise MaterialOperationRunnerError(
                code,
                "last-handle journal operation state disagrees with its phase",
            )
        _validate_sha256(
            value["container_sha256"],
            code=code,
            detail="last-handle container hash is invalid",
        )
        _validate_windows_pid(value["controller_pid"], "controller_pid")
        _validate_identity_time(
            value["controller_creation_time"],
            "controller_creation_time",
        )
        root_pid = value["root_pid"]
        root_creation_time = value["root_creation_time"]
        if _PHASE_INDEX[phase] < _ROOT_IDENTITY_INDEX:
            if root_pid is not None or root_creation_time is not None:
                raise MaterialOperationRunnerError(
                    code,
                    "last-handle journal has an early root identity",
                )
        else:
            _validate_windows_pid(root_pid, "root_pid")
            _validate_identity_time(root_creation_time, "root_creation_time")
        if closed:
            _validate_sha256(
                value["intent_sha256"],
                code=code,
                detail="last-handle intent hash is invalid",
            )
        _validate_identity_time(value[timestamp_field], timestamp_field)
    except (MaterialOperationRunnerError, TypeError, ValueError):
        raise MaterialOperationRunnerError(
            code,
            "last-handle journal failed typed validation",
        ) from None
    return value


def _last_handle_close_mapping(
    record: _PreparedRecord,
    *,
    state: Literal["intent", "closed"],
    intent_sha256: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "protocol_version": DESCRIPTOR_PROTOCOL_VERSION,
        "state": state,
        "execution_id": record.execution_id,
        "attempt_id": record.attempt_id,
        "control_id": record.control_id,
        "job_instance_id": record.job_instance_id,
        "phase": record.phase.value,
        "operation_may_have_started": (
            _PHASE_INDEX[record.phase] >= _OPERATION_STARTED_INDEX
        ),
        "container_sha256": record.container_hash,
        "controller_pid": record.controller.pid,
        "controller_creation_time": record.controller.creation_time,
        "root_pid": record.root_pid,
        "root_creation_time": record.root_creation_time,
        ("intent_at" if state == "intent" else "closed_at"): _utc_now(),
    }
    if state == "closed":
        if type(intent_sha256) is not str:
            raise MaterialOperationRunnerError(
                "last_handle_closed_invalid",
                "closed journal requires the exact intent hash",
            )
        payload["intent_sha256"] = intent_sha256
    return payload


def _assert_last_handle_mapping_matches(
    existing: Mapping[str, object],
    expected: Mapping[str, object],
    *,
    closed: bool,
) -> None:
    # The timestamp is generated on each retry attempt in memory.  It is an
    # audit field, not an ownership key; all identity/phase/hash fields must
    # remain exact while an already-durable timestamp may be adopted.
    ignored = {"closed_at", "intent_at"}
    for key, expected_value in expected.items():
        if key in ignored:
            continue
        if existing.get(key) != expected_value:
            raise MaterialOperationRunnerError(
                "last_handle_journal_changed",
                "last-handle journal does not match the owned descriptor tip",
            )


def _assert_owned_descriptor_tip(
    record: _PreparedRecord,
    expected_descriptor_bytes: bytes,
) -> None:
    current_bytes = _read_verified_bytes(
        parent=record.control_directory,
        filename=CONTAINER_FILENAME,
        max_bytes=MAX_DESCRIPTOR_BYTES,
    )
    if current_bytes != expected_descriptor_bytes:
        raise MaterialOperationRunnerError(
            "descriptor_changed",
            "last-handle close raced a durable descriptor advancement",
        )
    if hashlib.sha256(current_bytes).hexdigest() != record.container_hash:
        raise MaterialOperationRunnerError(
            "descriptor_changed",
            "last-handle close descriptor bytes do not match the owned tip",
        )


def _assert_owned_io_drain_quarantine(record: _PreparedRecord) -> None:
    if not record.blocked or record.quarantine_hash is None:
        raise MaterialOperationRunnerError(
            "last_handle_close_not_quarantined",
            "intentional last-handle close requires the owned io-drain quarantine",
        )
    quarantine_bytes = _read_verified_bytes(
        parent=record.control_directory,
        filename=QUARANTINE_FILENAME,
        max_bytes=MAX_QUARANTINE_BYTES,
    )
    if hashlib.sha256(quarantine_bytes).hexdigest() != record.quarantine_hash:
        raise MaterialOperationRunnerError(
            "quarantine_changed",
            "intentional last-handle close quarantine bytes changed",
        )
    quarantine = _decode_quarantine_bytes(quarantine_bytes)
    if (
        quarantine["reason_code"] != "io_drain_blocked"
        or quarantine["execution_id"] != record.execution_id
        or quarantine["attempt_id"] != record.attempt_id
        or quarantine["control_id"] != record.control_id
        or quarantine["job_instance_id"] != record.job_instance_id
        or quarantine["phase"] != record.phase.value
        or quarantine["container_sha256"] != record.container_hash
        or quarantine["termination"] is not None
    ):
        raise MaterialOperationRunnerError(
            "last_handle_close_not_quarantined",
            "only the exact owned io-drain quarantine permits last-handle close",
        )


def _read_last_handle_close_journal_pair(
    *,
    control: ApprovedPhysicalDirectory,
    descriptor: MaterialOperationContainerDescriptor,
    descriptor_bytes: bytes,
) -> tuple[bytes | None, bytes | None]:
    """Read and cross-bind the immutable last-handle intent/closed pair."""
    paths = (
        (LAST_HANDLE_CLOSE_INTENT_FILENAME, MAX_LAST_HANDLE_CLOSE_INTENT_BYTES),
        (LAST_HANDLE_CLOSED_FILENAME, MAX_LAST_HANDLE_CLOSED_BYTES),
    )
    raw: list[bytes | None] = []
    for filename, max_bytes in paths:
        try:
            value = _read_verified_bytes(
                parent=control,
                filename=filename,
                max_bytes=max_bytes,
            )
        except Exception as error:
            if getattr(error, "winerror", None) in {2, 3}:
                value = None
            else:
                raise
        raw.append(value)
    intent_bytes, closed_bytes = raw
    if intent_bytes is None and closed_bytes is None:
        return None, None
    if intent_bytes is None or closed_bytes is None:
        # An intent without a closed acknowledgement is a legitimate crash
        # window, so the caller may still fall back to owner-exit inference.
        # A closed acknowledgement without its intent is never admissible.
        if closed_bytes is not None:
            raise MaterialOperationRunnerError(
                "reconciliation_blocked",
                "last-handle closed journal has no durable intent predecessor",
            )
        return intent_bytes, None
    intent = _decode_last_handle_close_intent_bytes(intent_bytes)
    closed = _decode_last_handle_closed_bytes(closed_bytes)
    expected_container_hash = hashlib.sha256(descriptor_bytes).hexdigest()
    identity_fields = (
        "execution_id",
        "attempt_id",
        "control_id",
        "job_instance_id",
    )
    for field_name in identity_fields:
        if (
            intent[field_name] != closed[field_name]
            or intent[field_name] != getattr(descriptor, field_name)
        ):
            raise MaterialOperationRunnerError(
                "reconciliation_blocked",
                "last-handle journals disagree on their durable identity",
            )
    # Descriptor fields use Enum values and a different name for the
    # container hash; compare those explicitly rather than relying on the
    # compact loop above.
    descriptor_values = {
        "phase": descriptor.phase.value,
        "operation_may_have_started": descriptor.operation_may_have_started,
        "container_sha256": expected_container_hash,
        "controller_pid": descriptor.controller_pid,
        "controller_creation_time": descriptor.controller_creation_time,
        "root_pid": descriptor.root_pid,
        "root_creation_time": descriptor.root_creation_time,
    }
    for field_name, expected_value in descriptor_values.items():
        if (
            intent[field_name] != expected_value
            or closed[field_name] != expected_value
        ):
            raise MaterialOperationRunnerError(
                "reconciliation_blocked",
                "last-handle journals do not bind to the current descriptor tip",
            )
    if descriptor.job_handle_inheritable is not False:
        raise MaterialOperationRunnerError(
            "reconciliation_blocked",
            "last-handle inference lacks a durable non-inheritable Job proof",
        )
    if closed["intent_sha256"] != hashlib.sha256(intent_bytes).hexdigest():
        raise MaterialOperationRunnerError(
            "reconciliation_blocked",
            "last-handle closed journal does not bind to its intent bytes",
        )
    try:
        intent_at = datetime.fromisoformat(
            str(intent["intent_at"]).replace("Z", "+00:00")
        )
        closed_at = datetime.fromisoformat(
            str(closed["closed_at"]).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        raise MaterialOperationRunnerError(
            "reconciliation_blocked",
            "last-handle journal timestamps are not comparable",
        ) from None
    if closed_at < intent_at:
        raise MaterialOperationRunnerError(
            "reconciliation_blocked",
            "last-handle closed journal predates its intent",
        )
    return intent_bytes, closed_bytes


def _assert_last_handle_pair_unchanged(
    *,
    control: ApprovedPhysicalDirectory,
    expected_intent_bytes: bytes,
    expected_closed_bytes: bytes,
) -> None:
    try:
        current_intent = _read_verified_bytes(
            parent=control,
            filename=LAST_HANDLE_CLOSE_INTENT_FILENAME,
            max_bytes=MAX_LAST_HANDLE_CLOSE_INTENT_BYTES,
        )
        current_closed = _read_verified_bytes(
            parent=control,
            filename=LAST_HANDLE_CLOSED_FILENAME,
            max_bytes=MAX_LAST_HANDLE_CLOSED_BYTES,
        )
    except Exception as error:
        raise MaterialOperationRunnerError(
            "reconciliation_blocked",
            "last-handle close journals disappeared during reconciliation",
        ) from error
    if (
        current_intent != expected_intent_bytes
        or current_closed != expected_closed_bytes
    ):
        raise MaterialOperationRunnerError(
            "reconciliation_blocked",
            "last-handle close journals changed during reconciliation",
        )


def _decode_reconciliation_bytes(raw: bytes) -> ReconciliationEvidence:
    try:
        value = _decode_strict_descriptor_json(raw)
    except MaterialOperationRunnerError:
        raise MaterialOperationRunnerError(
            "reconciliation_invalid",
            "termination evidence is not strict bounded JSON",
        ) from None
    if type(value) is not dict or set(value) != {
        "protocol_version",
        "execution_id",
        "attempt_id",
        "job_instance_id",
        "method",
        "observation_basis",
        "active_processes",
        "root_pid",
        "operation_may_have_started",
        "observed_at",
        "container_sha256",
    }:
        raise MaterialOperationRunnerError(
            "reconciliation_invalid",
            "termination evidence has unknown or missing fields",
        )
    if value["protocol_version"] != DESCRIPTOR_PROTOCOL_VERSION:
        raise MaterialOperationRunnerError(
            "reconciliation_invalid",
            "termination evidence protocol version is unsupported",
        )
    try:
        return ReconciliationEvidence(
            execution_id=value["execution_id"],  # type: ignore[arg-type]
            attempt_id=value["attempt_id"],  # type: ignore[arg-type]
            job_instance_id=value["job_instance_id"],  # type: ignore[arg-type]
            method=value["method"],  # type: ignore[arg-type]
            observation_basis=value["observation_basis"],  # type: ignore[arg-type]
            active_processes=value["active_processes"],  # type: ignore[arg-type]
            root_pid=value["root_pid"],  # type: ignore[arg-type]
            operation_may_have_started=value["operation_may_have_started"],  # type: ignore[arg-type]
            observed_at=value["observed_at"],  # type: ignore[arg-type]
            container_sha256=value["container_sha256"],  # type: ignore[arg-type]
        )
    except (MaterialOperationRunnerError, TypeError, ValueError):
        raise MaterialOperationRunnerError(
            "reconciliation_invalid",
            "termination evidence failed typed validation",
        ) from None


def _descriptor_to_mapping(
    descriptor: MaterialOperationContainerDescriptor,
) -> dict[str, object]:
    descriptor.__post_init__()
    value: dict[str, object] = {
        "protocol_version": descriptor.protocol_version,
        "phase": descriptor.phase.value,
        "previous_phase": (
            None
            if descriptor.previous_phase is None
            else descriptor.previous_phase.value
        ),
        "previous_descriptor_sha256": descriptor.previous_descriptor_sha256,
        "execution_id": descriptor.execution_id,
        "attempt_id": descriptor.attempt_id,
        "control_id": descriptor.control_id,
        "job_instance_id": descriptor.job_instance_id,
        "operation": descriptor.operation,
        "controller_pid": descriptor.controller_pid,
        "controller_creation_time": descriptor.controller_creation_time,
        "operation_may_have_started": descriptor.operation_may_have_started,
    }
    phase_index = _PHASE_INDEX[descriptor.phase]
    if phase_index >= _REQUEST_HASH_INDEX:
        value["request_sha256"] = descriptor.request_sha256
        value["job_handle_inheritable"] = descriptor.job_handle_inheritable
    if phase_index >= _ROOT_IDENTITY_INDEX:
        value["root_pid"] = descriptor.root_pid
        value["root_creation_time"] = descriptor.root_creation_time
        value["job_active_processes_after_create"] = (
            descriptor.job_active_processes_after_create
        )
    return value


def _validate_container_descriptor(
    descriptor: MaterialOperationContainerDescriptor,
) -> None:
    if (
        type(descriptor.protocol_version) is not int
        or descriptor.protocol_version != DESCRIPTOR_PROTOCOL_VERSION
    ):
        raise MaterialOperationRunnerError(
            "descriptor_version_invalid",
            "descriptor protocol_version must be integer 1",
        )
    if type(descriptor.phase) is not MaterialLaunchPhase:
        raise MaterialOperationRunnerError(
            "descriptor_phase_invalid",
            "descriptor phase must be a frozen launch phase",
        )
    phase_index = _PHASE_INDEX[descriptor.phase]
    for field, value in (
        ("execution_id", descriptor.execution_id),
        ("attempt_id", descriptor.attempt_id),
        ("control_id", descriptor.control_id),
        ("job_instance_id", descriptor.job_instance_id),
    ):
        _validate_uuid(value, field)
    if len(
        {
            descriptor.execution_id,
            descriptor.attempt_id,
            descriptor.control_id,
            descriptor.job_instance_id,
        }
    ) != 4:
        raise MaterialOperationRunnerError(
            "descriptor_identity_invalid",
            "execution, attempt, control, and Job IDs must be independent",
        )
    _validate_operation(descriptor.operation)
    _validate_windows_pid(descriptor.controller_pid, "controller_pid")
    _validate_identity_time(
        descriptor.controller_creation_time,
        "controller_creation_time",
    )
    if type(descriptor.operation_may_have_started) is not bool:
        raise MaterialOperationRunnerError(
            "descriptor_operation_state_invalid",
            "operation_may_have_started must be a boolean",
        )

    if descriptor.phase is MaterialLaunchPhase.PLANNED:
        if (
            descriptor.previous_phase is not None
            or descriptor.previous_descriptor_sha256 is not None
        ):
            raise MaterialOperationRunnerError(
                "descriptor_chain_invalid",
                "planned must not claim a predecessor",
            )
    else:
        expected_previous = MATERIAL_LAUNCH_PHASES[phase_index - 1]
        if descriptor.previous_phase is not expected_previous:
            raise MaterialOperationRunnerError(
                "descriptor_chain_invalid",
                "descriptor previous_phase is not the immediate predecessor",
            )
        _validate_sha256(
            descriptor.previous_descriptor_sha256,
            code="descriptor_chain_invalid",
            detail="previous descriptor hash must be lowercase SHA-256",
        )

    if phase_index < _REQUEST_HASH_INDEX:
        if (
            descriptor.request_sha256 is not None
            or descriptor.job_handle_inheritable is not None
        ):
            raise MaterialOperationRunnerError(
                "request_hash_unexpected",
                "planned must not claim a request receipt or Job handle state",
            )
    else:
        _validate_sha256(
            descriptor.request_sha256,
            code="request_hash_invalid",
            detail="request receipt hash must be lowercase SHA-256",
        )
        if descriptor.job_handle_inheritable is not False:
            raise MaterialOperationRunnerError(
                "job_handle_state_invalid",
                "configured Job handle must be explicitly non-inheritable",
            )

    if phase_index < _ROOT_IDENTITY_INDEX:
        if (
            descriptor.root_pid is not None
            or descriptor.root_creation_time is not None
            or descriptor.job_active_processes_after_create is not None
        ):
            raise MaterialOperationRunnerError(
                "root_identity_unexpected",
                "root identity must be absent before suspended creation is proven",
            )
    else:
        _validate_windows_pid(descriptor.root_pid, "root_pid")
        _validate_identity_time(descriptor.root_creation_time, "root_creation_time")
        if (
            type(descriptor.job_active_processes_after_create) is not int
            or descriptor.job_active_processes_after_create != 1
        ):
            raise MaterialOperationRunnerError(
                "job_membership_evidence_invalid",
                "created_in_job_suspended must preserve Query ActiveProcesses=1",
            )

    expected_started = phase_index >= _OPERATION_STARTED_INDEX
    if descriptor.operation_may_have_started is not expected_started:
        raise MaterialOperationRunnerError(
            "descriptor_operation_state_invalid",
            "operation_may_have_started disagrees with the launch phase",
        )


def _decode_strict_bounded_json(
    raw: bytes,
    *,
    max_bytes: int,
    code: str,
    detail: str,
) -> object:
    """Decode one bounded JSON value without accepting ambiguous syntax."""

    if type(raw) is not bytes or not raw or len(raw) > max_bytes:
        raise MaterialOperationRunnerError(code, detail)
    if raw.startswith(b"\xef\xbb\xbf"):
        raise MaterialOperationRunnerError(code, detail)
    try:
        text = raw.decode("utf-8", errors="strict")
        decoder = json.JSONDecoder(
            object_pairs_hook=_descriptor_object_without_duplicates,
            parse_int=_parse_descriptor_integer,
            parse_float=_reject_descriptor_float,
            parse_constant=_reject_descriptor_constant,
        )
        value, end = decoder.raw_decode(text)
    except MaterialOperationRunnerError:
        raise MaterialOperationRunnerError(code, detail) from None
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise MaterialOperationRunnerError(code, detail) from None
    if end != len(text):
        raise MaterialOperationRunnerError(code, detail)
    return value


def _assert_retained_publication_owner(
    publication_ownership: object,
    *,
    record: _PreparedRecord,
    proof: MaterialRestartPublishedRetirementProof,
) -> None:
    assert_owned = getattr(
        publication_ownership,
        "assert_retained_ownership",
        None,
    )
    if not callable(assert_owned):
        raise MaterialOperationRunnerError(
            "restart_publication_ownership_invalid",
            "publisher target-lock ownership cannot be asserted",
        )
    try:
        assert_owned()
        request = getattr(publication_ownership, "request", None)
        journal_sha256 = getattr(
            publication_ownership,
            "committed_journal_sha256",
            None,
        )
    except BaseException as exc:
        raise MaterialOperationRunnerError(
            "restart_publication_ownership_lost",
            "publisher target-lock ownership could not be proved",
        ) from exc
    if (
        getattr(request, "execution_id", None) != record.execution_id
        or getattr(request, "attempt_id", None) != record.attempt_id
        or getattr(request, "operation", None) != record.operation
        or journal_sha256 != proof.publication_journal_sha256
    ):
        raise MaterialOperationRunnerError(
            "restart_publication_ownership_mismatch",
            "publisher ownership receipt belongs to another publication",
        )


def _validate_committed_publication_journal(
    raw: bytes,
    *,
    record: _PreparedRecord,
) -> None:
    value = _decode_strict_bounded_json(
        raw,
        max_bytes=MAX_PUBLICATION_JOURNAL_BYTES,
        code="publication_journal_invalid",
        detail="publication journal is not strict bounded JSON",
    )
    if type(value) is not dict:
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "publication journal must be a JSON object",
        )
    required_fields = {
        "format",
        "execution_id",
        "attempt_id",
        "operation",
        "phase",
        "attestation",
        "moves",
        "registry",
    }
    if frozenset(value) not in {
        frozenset(required_fields),
        frozenset({*required_fields, "completion_intent"}),
    } or (
        value["format"] != "material-publication-v1"
        or value["execution_id"] != record.execution_id
        or value["attempt_id"] != record.attempt_id
        or value["operation"] != record.operation
        or value["phase"] != "committed"
        or value["attestation"] != "committed"
        or type(value["moves"]) is not list
        or type(value["registry"]) is not dict
    ):
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "publication journal is not a committed receipt for this attempt",
        )
    moves = value["moves"]
    if not moves:
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "committed publication journal has no applied artifact moves",
        )
    move_fields = {
        "destination",
        "part_path",
        "sha256",
        "size_bytes",
        "target_kind",
        "ordinal",
        "target_label",
        "owned",
        "part_identity",
        "destination_identity",
        "state",
    }
    seen_moves: set[tuple[object, object, object]] = set()
    seen_destinations: set[str] = set()
    for move in moves:
        if type(move) is not dict or set(move) != move_fields:
            raise MaterialOperationRunnerError(
                "publication_journal_invalid",
                "committed publication move has an unexpected schema",
            )
        destination = move["destination"]
        part_path = move["part_path"]
        state = move["state"]
        if (
            type(destination) is not str
            or type(part_path) is not str
            or not _is_absolute_d_path(destination)
            or (
                state != "reused"
                and not _is_absolute_d_path(part_path)
            )
            or (state == "reused" and part_path not in {"", destination})
            or not _SHA256_PATTERN.fullmatch(str(move["sha256"]))
            or type(move["size_bytes"]) is not int
            or isinstance(move["size_bytes"], bool)
            or move["size_bytes"] <= 0
            or move["target_kind"] not in {"canonical", "project"}
            or type(move["ordinal"]) is not int
            or isinstance(move["ordinal"], bool)
            or move["ordinal"] < 0
            or type(move["target_label"]) is not str
            or state not in {"move_applied", "reused"}
            or move["owned"] is not (state != "reused")
            or not _valid_publication_identity(move["part_identity"])
            or not _valid_publication_identity(move["destination_identity"])
        ):
            raise MaterialOperationRunnerError(
                "publication_journal_invalid",
                "committed publication move is not durably applied",
            )
        destination_key = _normcase(str(destination))
        if destination_key in seen_destinations:
            raise MaterialOperationRunnerError(
                "publication_journal_invalid",
                "committed publication journal repeats a destination path",
            )
        seen_destinations.add(destination_key)
        move_key = (move["target_kind"], move["target_label"], move["ordinal"])
        if move_key in seen_moves:
            raise MaterialOperationRunnerError(
                "publication_journal_invalid",
                "committed publication journal repeats a move identity",
            )
        seen_moves.add(move_key)

    registry = value["registry"]
    if set(registry) != {
        "path",
        "old_sha256",
        "old_bytes_b64",
        "old_identity",
        "new_sha256",
        "new_identity",
        "state",
    }:
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "committed publication registry receipt has an unexpected schema",
        )
    if (
        type(registry["path"]) is not str
        or not _is_absolute_d_path(registry["path"])
        or registry["old_sha256"] is not None
        and not _SHA256_PATTERN.fullmatch(str(registry["old_sha256"]))
        or not _SHA256_PATTERN.fullmatch(str(registry["new_sha256"]))
        or registry["old_bytes_b64"] is not None
        and type(registry["old_bytes_b64"]) is not str
        or registry["state"] != "registry_replace_applied"
        or not _valid_publication_identity(registry["new_identity"])
        or not _valid_publication_identity(registry["old_identity"], allow_none=True)
    ):
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "committed publication registry receipt is not durably applied",
        )
    _validate_registry_snapshot_receipt(
        registry,
        code="publication_journal_invalid",
    )
    if _normcase(str(registry["path"])) in seen_destinations:
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "committed publication registry path collides with an artifact destination",
        )


def _verify_committed_publication_targets(
    raw: bytes,
    *,
    record: _PreparedRecord,
) -> None:
    """Verify the bytes and identities named by a committed publication.

    The journal is a receipt, not proof by itself.  Before the runner releases
    its attempt pins, every destination and the replacement registry must still
    be a plain, single-link D-drive file with the recorded size, identity, and
    SHA-256.  This is intentionally a fresh attestation; the production caller
    must keep the publisher's target/registry locks held across its final CAS
    and this runner seam to close the remaining TOCTOU window.
    """

    value = _decode_strict_bounded_json(
        raw,
        max_bytes=MAX_PUBLICATION_JOURNAL_BYTES,
        code="publication_journal_invalid",
        detail="publication journal is not strict bounded JSON",
    )
    if type(value) is not dict:
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "publication journal must be a JSON object",
        )
    moves = value.get("moves")
    registry = value.get("registry")
    if type(moves) is not list or type(registry) is not dict:
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "publication journal target evidence is not structured",
        )
    try:
        for move in moves:
            # Schema validation above guarantees these keys and value types.
            _verify_publication_file_attestation(
                Path(str(move["destination"])),
                expected_sha256=str(move["sha256"]),
                expected_size=int(move["size_bytes"]),
                expected_identity=move["destination_identity"],
                role="publication destination",
            )
            if move["state"] == "move_applied":
                _assert_publication_part_absent(Path(str(move["part_path"])))
        _verify_publication_file_attestation(
            Path(str(registry["path"])),
            expected_sha256=str(registry["new_sha256"]),
            expected_size=None,
            expected_identity=registry["new_identity"],
            role="publication registry",
        )
    except MaterialOperationRunnerError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise MaterialOperationRunnerError(
            "publication_target_invalid",
            "committed publication target evidence is malformed",
        ) from exc


def _assert_result_matches_publication_journal(
    raw: bytes,
    *,
    result: MaterialWorkerResult,
    record: _PreparedRecord,
    proof: MaterialRestartPublishedRetirementProof | None = None,
) -> None:
    """Bind the worker artifact manifest to every published move fingerprint."""

    value = _decode_strict_bounded_json(
        raw,
        max_bytes=MAX_PUBLICATION_JOURNAL_BYTES,
        code="publication_journal_invalid",
        detail="publication journal is not strict bounded JSON",
    )
    if type(value) is not dict or type(value.get("moves")) is not list:
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "publication journal has no artifact move manifest",
        )
    artifact_fingerprints = {
        (artifact.sha256, artifact.size_bytes) for artifact in result.artifacts
    }
    try:
        move_fingerprints = {
            (str(move["sha256"]), int(move["size_bytes"]))
            for move in value["moves"]
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "publication move manifest is malformed",
        ) from exc
    if not artifact_fingerprints or move_fingerprints != artifact_fingerprints:
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "published move fingerprints do not match the worker artifact manifest",
        )
    intent = value.get("completion_intent")
    if intent is None:
        return
    if type(intent) is not dict or set(intent) != {
        "format",
        "claim_receipt",
        "worker_result_sha256",
        "published_result",
        "published_result_sha256",
        "intent_sha256",
    }:
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "publication completion intent schema is invalid",
        )
    claim = intent.get("claim_receipt")
    if type(claim) is not dict or set(claim) != {
        "material_task_id",
        "execution_id",
        "attempt_id",
        "operation",
        "control_id",
        "job_instance_id",
        "reference_base_revision",
        "reference_base_payload_sha256",
    }:
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "publication completion intent claim receipt is invalid",
        )
    published_result = intent.get("published_result")
    if type(published_result) is not dict:
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "publication completion intent result is invalid",
        )
    if not _valid_published_result_schema(
        published_result,
        operation=record.operation,
        worker_artifact_count=len(result.artifacts),
    ):
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "publication completion result does not match the operation schema",
        )
    intent_without_hash = dict(intent)
    intent_sha256 = intent_without_hash.pop("intent_sha256", None)
    binding = record.claim_binding
    expected_base_revision = None
    expected_base_payload_sha256 = None
    if binding is not None:
        expected_base_revision = (
            binding.execution_revision - 1
            if record.adopted_after_restart
            else binding.execution_revision
        )
        if not record.adopted_after_restart:
            expected_base_payload_sha256 = binding.payload_sha256
    if (
        intent.get("format") != "material-publication-completion-intent-v1"
        or intent_sha256 != _canonical_json_sha256(intent_without_hash)
        or intent.get("published_result_sha256")
        != _canonical_json_sha256(published_result)
        or intent.get("worker_result_sha256")
        != hashlib.sha256(encode_material_worker_result(result)).hexdigest()
        or claim.get("execution_id") != record.execution_id
        or claim.get("attempt_id") != record.attempt_id
        or claim.get("operation") != record.operation
        or claim.get("control_id") != record.control_id
        or claim.get("job_instance_id") != record.job_instance_id
        or binding is not None
        and claim.get("material_task_id") != binding.material_task_id
        or expected_base_revision is not None
        and claim.get("reference_base_revision") != expected_base_revision
        or expected_base_payload_sha256 is not None
        and claim.get("reference_base_payload_sha256")
        != expected_base_payload_sha256
        or not _SHA256_PATTERN.fullmatch(
            str(claim.get("reference_base_payload_sha256"))
        )
    ):
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "publication completion intent does not bind the durable worker result",
        )
    if proof is not None and (
        intent_sha256 != proof.publication_intent_sha256
        or intent.get("published_result_sha256")
        != proof.publication_result_sha256
        or intent.get("worker_result_sha256")
        != proof.publication_worker_result_sha256
        or claim.get("reference_base_revision")
        != proof.publication_reference_base_revision
        or claim.get("reference_base_payload_sha256")
        != proof.publication_reference_base_payload_sha256
    ):
        raise MaterialOperationRunnerError(
            "publication_journal_invalid",
            "publication completion intent differs from the final database proof",
        )


def _valid_published_result_schema(
    value: Mapping[str, object],
    *,
    operation: MaterialOperationKind,
    worker_artifact_count: int,
) -> bool:
    if operation == "stock_candidates":
        return (
            set(value)
            == {"kind", "success", "status", "reason", "candidates"}
            and value.get("kind") == "stock_candidate_batch"
            and value.get("success") is True
            and value.get("status") == "需要人工确认"
            and type(value.get("reason")) is str
            and type(value.get("candidates")) is list
            and len(value["candidates"]) == worker_artifact_count
            and 3 <= worker_artifact_count <= 5
            and all(type(candidate) is dict for candidate in value["candidates"])
        )
    required = {
        "kind",
        "success",
        "status",
        "material_path",
        "material_type",
        "source_type",
        "reason",
        "evidence_kind",
        "source_url",
        "is_locally_generated",
        "rights_evidence",
    }
    return (
        set(value) == required
        and value.get("kind") == "material_execution_result"
        and value.get("success") is True
        and type(value.get("status")) is str
        and bool(str(value.get("status")).strip())
        and type(value.get("material_path")) is str
        and _is_absolute_d_path(str(value.get("material_path")))
        and type(value.get("material_type")) is str
        and type(value.get("source_type")) is str
        and type(value.get("reason")) is str
        and type(value.get("evidence_kind")) is str
        and type(value.get("source_url")) is str
        and type(value.get("is_locally_generated")) is bool
        and (
            value.get("rights_evidence") is None
            or type(value.get("rights_evidence")) is dict
        )
    )


def _validate_outcome_for_record(
    record: _PreparedRecord,
    outcome: MaterialOperationOutcome,
    *,
    code: str,
) -> None:
    """Bind a worker outcome to the prepared attempt before releasing pins."""

    if type(outcome) is not MaterialOperationOutcome:
        raise MaterialOperationRunnerError(code, "attempt outcome has an invalid type")
    try:
        outcome.__post_init__()
    except Exception as exc:
        raise MaterialOperationRunnerError(
            code,
            "attempt outcome failed typed validation",
        ) from exc
    termination = outcome.termination
    if termination.root_pid != record.root_pid:
        raise MaterialOperationRunnerError(
            code,
            "attempt outcome termination belongs to another root process",
        )
    result = outcome.result
    if result is not None:
        try:
            result.__post_init__()
        except Exception as exc:
            raise MaterialOperationRunnerError(
                code,
                "attempt worker result failed typed validation",
            ) from exc
        if (
            result.execution_id != record.execution_id
            or result.attempt_id != record.attempt_id
            or result.operation != record.operation
        ):
            raise MaterialOperationRunnerError(
                code,
                "attempt worker result identity does not match the prepared attempt",
            )
        for artifact in result.artifacts:
            try:
                artifact.__post_init__()
            except Exception as exc:
                raise MaterialOperationRunnerError(
                    code,
                    "attempt artifact failed nested typed validation",
                ) from exc


def _assert_outcome_reconciliation_binding(
    record: _PreparedRecord,
    outcome: MaterialOperationOutcome,
    *,
    allow_inference: bool = False,
) -> None:
    """Bind the in-memory terminal proof to the exact durable empty-tree proof."""

    evidence = record.reconciliation
    if evidence is None:
        raise MaterialOperationRunnerError(
            "reconciliation_invalid",
            "terminal outcome has no reconciliation evidence to bind",
        )
    termination = outcome.termination
    if evidence.root_pid != termination.root_pid:
        raise MaterialOperationRunnerError(
            "reconciliation_invalid",
            "terminal outcome root does not match the durable proof",
        )
    if evidence.method == "queried_empty":
        matches = (
            evidence.observed_at == termination.observed_at
            and evidence.active_processes == termination.active_processes
        )
    elif allow_inference and evidence.method in {
        "owner_exit_kill_on_close",
        "last_handle_close_kill_on_close",
    }:
        # Inference journals intentionally have no Job ActiveProcesses query.
        # The exact termination fields are instead bound by the durable
        # termination-outcome journal checked immediately before this helper.
        matches = evidence.active_processes is None
    else:
        matches = False
    if not matches:
        raise MaterialOperationRunnerError(
            "reconciliation_invalid",
            "terminal outcome does not match the durable reconciliation proof",
        )


def _assert_native_release_state(record: _PreparedRecord, *, code: str) -> None:
    """Reject impossible/downgraded native-close flag combinations."""

    invalid = (
        (record.completion_resources_closed and not record.completion_close_attempted)
        or (record.pipe_closed and not record.pipe_close_attempted)
        or (record.pipe_closed and record.pipe_close_unknown)
        or (record.last_handle_job_closed and not record.last_handle_close_attempted)
        or (
            record.last_handle_closed
            and (
                not record.last_handle_close_attempted
                or not record.last_handle_job_closed
                or not record.completion_resources_closed
                or record.last_handle_close_intent_hash is None
                or record.last_handle_closed_hash is None
            )
        )
        or (record.process_quarantine_cleaned and not record.process_quarantine_cleanup_attempted)
        or (
            record.process_quarantine_cleanup_attempted
            and not record.process_quarantine_cleaned
        )
    )
    if invalid:
        raise MaterialOperationRunnerError(
            code,
            "native ownership flags are internally inconsistent",
        )


def _verify_publication_file_attestation(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int | None,
    expected_identity: Mapping[str, object],
    role: str,
) -> None:
    if not _is_absolute_d_path(str(path)):
        raise MaterialOperationRunnerError(
            "publication_target_invalid",
            f"{role} must be an absolute D-drive path",
        )
    if not _valid_publication_identity(expected_identity):
        raise MaterialOperationRunnerError(
            "publication_target_invalid",
            f"{role} identity is invalid",
        )
    try:
        before = path.lstat()
    except OSError as exc:
        raise MaterialOperationRunnerError(
            "publication_target_unavailable",
            f"{role} cannot be inspected",
        ) from exc
    attributes = int(getattr(before, "st_file_attributes", 0))
    if (
        path.is_symlink()
        or attributes & _FILE_ATTRIBUTE_REPARSE_POINT
        or not stat.S_ISREG(before.st_mode)
        or int(before.st_nlink) != 1
    ):
        raise MaterialOperationRunnerError(
            "publication_target_invalid",
            f"{role} is not an owned plain file",
        )
    actual_identity = {
        "device": int(before.st_dev),
        "inode": int(before.st_ino),
    }
    if actual_identity != dict(expected_identity):
        raise MaterialOperationRunnerError(
            "publication_target_changed",
            f"{role} identity does not match its journal receipt",
        )
    size = int(before.st_size)
    if size <= 0 or size > MAX_PUBLICATION_TARGET_BYTES:
        raise MaterialOperationRunnerError(
            "publication_target_invalid",
            f"{role} exceeds the publication size bounds",
        )
    if expected_size is not None and size != expected_size:
        raise MaterialOperationRunnerError(
            "publication_target_changed",
            f"{role} size does not match its journal receipt",
        )
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                total += len(chunk)
                if total > MAX_PUBLICATION_TARGET_BYTES:
                    raise MaterialOperationRunnerError(
                        "publication_target_invalid",
                        f"{role} exceeds the publication size bounds while reading",
                    )
                digest.update(chunk)
    except MaterialOperationRunnerError:
        raise
    except OSError as exc:
        raise MaterialOperationRunnerError(
            "publication_target_unavailable",
            f"{role} could not be read",
        ) from exc
    if total != size or digest.hexdigest() != expected_sha256:
        raise MaterialOperationRunnerError(
            "publication_target_changed",
            f"{role} bytes do not match its journal receipt",
        )
    try:
        after = path.lstat()
    except OSError as exc:
        raise MaterialOperationRunnerError(
            "publication_target_changed",
            f"{role} disappeared during attestation",
        ) from exc
    stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
    if any(getattr(after, field) != getattr(before, field) for field in stable):
        raise MaterialOperationRunnerError(
            "publication_target_changed",
            f"{role} metadata changed during attestation",
        )


def _assert_publication_part_absent(path: Path) -> None:
    _assert_publication_path_absent(path, role="published part")


def _assert_publication_path_absent(path: Path, *, role: str) -> None:
    if not _is_absolute_d_path(str(path)):
        raise MaterialOperationRunnerError(
            "publication_target_invalid",
            f"{role} path must be an absolute D-drive path",
        )
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise MaterialOperationRunnerError(
            "publication_target_unavailable",
            f"{role} path cannot be inspected",
        ) from exc
    raise MaterialOperationRunnerError(
        "publication_target_changed",
        f"{role} file still exists after rollback",
    )


def _is_absolute_d_path(value: str) -> bool:
    drive, tail = ntpath.splitdrive(value)
    return drive.upper() == "D:" and ntpath.isabs(value) and bool(tail)


def _valid_publication_identity(value: object, *, allow_none: bool = False) -> bool:
    if value is None:
        return allow_none
    if type(value) is not dict or set(value) != {"device", "inode"}:
        return False
    return all(
        type(value[key]) is int and not isinstance(value[key], bool) and value[key] >= 0
        for key in ("device", "inode")
    )


def _assert_durable_publication_reconciliation(record: _PreparedRecord) -> None:
    """Rebind the in-memory result to the durable termination journal."""

    _assert_reconciliation_belongs_to_record(record, record.reconciliation)

    try:
        persisted_bytes = _read_verified_bytes(
            parent=record.control_directory,
            filename=TERMINATION_FILENAME,
            max_bytes=MAX_TERMINATION_BYTES,
        )
    except BaseException as exc:
        raise MaterialOperationRunnerError(
            "publication_reconciliation_unavailable",
            "published retirement cannot read the durable termination journal",
        ) from exc
    if hashlib.sha256(persisted_bytes).hexdigest() != record.reconciliation_hash:
        raise MaterialOperationRunnerError(
            "publication_reconciliation_changed",
            "durable termination journal bytes differ from the owned hash",
        )
    try:
        persisted = _decode_reconciliation_bytes(persisted_bytes)
    except BaseException as exc:
        raise MaterialOperationRunnerError(
            "publication_reconciliation_invalid",
            "durable termination journal is not valid reconciliation evidence",
        ) from exc
    if persisted != record.reconciliation:
        raise MaterialOperationRunnerError(
            "publication_reconciliation_changed",
            "durable termination journal differs from the owned reconciliation",
        )
    _assert_reconciliation_belongs_to_record(record, persisted)


def _assert_reconciliation_belongs_to_record(
    record: _PreparedRecord,
    evidence: ReconciliationEvidence | None,
) -> None:
    if evidence is None:
        raise MaterialOperationRunnerError(
            "publication_reconciliation_unavailable",
            "reconciliation evidence is missing",
        )
    expected_started = _PHASE_INDEX[record.phase] >= _OPERATION_STARTED_INDEX
    if (
        evidence.execution_id != record.execution_id
        or evidence.attempt_id != record.attempt_id
        or evidence.job_instance_id != record.job_instance_id
        or evidence.container_sha256 != record.container_hash
        or evidence.operation_may_have_started != expected_started
        or evidence.root_pid != record.root_pid
        or (
            evidence.method == "queried_empty"
            and (evidence.root_pid is None or record.root_pid is None)
        )
    ):
        raise MaterialOperationRunnerError(
            "publication_reconciliation_invalid",
            "reconciliation evidence identity does not match the prepared attempt",
        )


def _assert_durable_termination_outcome(
    record: _PreparedRecord,
    outcome: MaterialOperationOutcome,
) -> None:
    """Require a terminated result to carry its exact durable outcome journal."""

    if record.termination_outcome_hash is None:
        raise MaterialOperationRunnerError(
            "terminal_outcome_unavailable",
            "terminated retirement requires a durable termination outcome",
        )
    try:
        raw = _read_verified_bytes(
            parent=record.control_directory,
            filename=TERMINATION_OUTCOME_FILENAME,
            max_bytes=MAX_TERMINATION_OUTCOME_BYTES,
        )
    except BaseException as exc:
        raise MaterialOperationRunnerError(
            "terminal_outcome_unavailable",
            "terminated retirement cannot read its durable outcome",
        ) from exc
    if hashlib.sha256(raw).hexdigest() != record.termination_outcome_hash:
        raise MaterialOperationRunnerError(
            "terminal_outcome_changed",
            "durable termination outcome bytes differ from the owned hash",
        )
    try:
        execution_id, attempt_id, job_instance_id, termination, observed_at = (
            _decode_termination_outcome_bytes(raw)
        )
    except BaseException as exc:
        raise MaterialOperationRunnerError(
            "terminal_outcome_invalid",
            "durable termination outcome is not typed evidence",
        ) from exc
    if (
        execution_id != record.execution_id
        or attempt_id != record.attempt_id
        or job_instance_id != record.job_instance_id
        or termination != outcome.termination
        or observed_at != outcome.termination.observed_at
    ):
        raise MaterialOperationRunnerError(
            "terminal_outcome_changed",
            "durable termination outcome does not belong to this attempt",
        )


def _assert_no_committed_publication_journal(record: _PreparedRecord) -> None:
    """Prevent terminal fallback retirement from hiding a publication boundary."""

    try:
        raw = _read_verified_bytes(
            parent=record.directory,
            filename=PUBLICATION_JOURNAL_FILENAME,
            max_bytes=MAX_PUBLICATION_JOURNAL_BYTES,
        )
    except BaseException as exc:
        if isinstance(exc, FileNotFoundError) or getattr(exc, "winerror", None) in {2, 3}:
            return
        raise MaterialOperationRunnerError(
            "terminal_publication_state_unknown",
            "terminal retirement cannot establish publication journal absence",
        ) from exc
    value = _decode_strict_bounded_json(
        raw,
        max_bytes=MAX_PUBLICATION_JOURNAL_BYTES,
        code="terminal_publication_state_invalid",
        detail="publication journal is not strict bounded JSON",
    )
    if type(value) is not dict or value.get("phase") != "rolled_back":
        raise MaterialOperationRunnerError(
            "terminal_publication_state_present",
            "terminal retirement found a publication journal that is not rolled back",
        )
    _validate_rolled_back_publication_journal(value, record=record)
    _verify_rolled_back_publication_effects(value)


def _validate_rolled_back_publication_journal(
    value: Mapping[str, object],
    *,
    record: _PreparedRecord,
) -> None:
    """Validate the complete rollback receipt before terminal retirement."""

    required = {
        "format",
        "execution_id",
        "attempt_id",
        "operation",
        "phase",
        "moves",
        "registry",
    }
    allowed = required | {"attestation", "reason_code"}
    if (
        set(value) - allowed
        or not required.issubset(value)
        or value.get("format") != "material-publication-v1"
        or value.get("execution_id") != record.execution_id
        or value.get("attempt_id") != record.attempt_id
        or value.get("operation") != record.operation
        or value.get("phase") != "rolled_back"
        or value.get("attestation") != "rolled_back"
        or type(value.get("moves")) is not list
        or type(value.get("registry")) is not dict
    ):
        raise MaterialOperationRunnerError(
            "terminal_publication_state_invalid",
            "rolled-back publication journal has an invalid control-plane schema",
        )
    moves = value["moves"]
    assert isinstance(moves, list)
    move_fields = {
        "destination",
        "part_path",
        "sha256",
        "size_bytes",
        "target_kind",
        "ordinal",
        "target_label",
        "owned",
        "part_identity",
        "destination_identity",
        "state",
    }
    seen: set[tuple[object, object, object]] = set()
    seen_destinations: set[str] = set()
    for move in moves:
        if type(move) is not dict or set(move) != move_fields:
            raise MaterialOperationRunnerError(
                "terminal_publication_state_invalid",
                "rolled-back publication move has an invalid schema",
            )
        state = move["state"]
        part_path = move["part_path"]
        if (
            type(move["destination"]) is not str
            or not _is_absolute_d_path(move["destination"])
            or (state != "reused" and (type(part_path) is not str or not _is_absolute_d_path(part_path)))
            or (state == "reused" and part_path not in {"", move["destination"]})
            or not _SHA256_PATTERN.fullmatch(str(move["sha256"]))
            or type(move["size_bytes"]) is not int
            or isinstance(move["size_bytes"], bool)
            or move["size_bytes"] <= 0
            or move["target_kind"] not in {"canonical", "project"}
            or type(move["ordinal"]) is not int
            or isinstance(move["ordinal"], bool)
            or move["ordinal"] < 0
            or type(move["target_label"]) is not str
            or state not in {"move_intent", "move_applied", "reused"}
            or move["owned"] is not (state != "reused")
            or not _valid_publication_identity(move["part_identity"])
            or not _valid_publication_identity(
                move["destination_identity"],
                allow_none=state == "move_intent",
            )
        ):
            raise MaterialOperationRunnerError(
                "terminal_publication_state_invalid",
                "rolled-back publication move is not a trusted receipt",
            )
        destination_key = _normcase(str(move["destination"]))
        if destination_key in seen_destinations:
            raise MaterialOperationRunnerError(
                "terminal_publication_state_invalid",
                "rolled-back publication journal repeats a destination path",
            )
        seen_destinations.add(destination_key)
        key = (move["target_kind"], move["target_label"], move["ordinal"])
        if key in seen:
            raise MaterialOperationRunnerError(
                "terminal_publication_state_invalid",
                "rolled-back publication journal repeats a move identity",
            )
        seen.add(key)
    registry = value["registry"]
    assert isinstance(registry, dict)
    if not registry:
        return
    registry_fields = {
        "path",
        "old_sha256",
        "old_bytes_b64",
        "old_identity",
        "new_sha256",
        "new_identity",
        "state",
    }
    if set(registry) != registry_fields or (
        type(registry["path"]) is not str
        or not _is_absolute_d_path(registry["path"])
        or registry["old_sha256"] is not None
        and not _SHA256_PATTERN.fullmatch(str(registry["old_sha256"]))
        or registry["old_bytes_b64"] is not None
        and type(registry["old_bytes_b64"]) is not str
        or not _SHA256_PATTERN.fullmatch(str(registry["new_sha256"]))
        or registry["state"] != "registry_replace_applied"
        or not _valid_publication_identity(registry["new_identity"])
        or not _valid_publication_identity(registry["old_identity"], allow_none=True)
        or (
            registry["old_sha256"] is None
            and (
                registry["old_bytes_b64"] is not None
                or registry["old_identity"] is not None
            )
        )
        or (
            registry["old_sha256"] is not None
            and (
                registry["old_bytes_b64"] is None
                or registry["old_identity"] is None
            )
        )
    ):
        raise MaterialOperationRunnerError(
            "terminal_publication_state_invalid",
            "rolled-back publication registry receipt is not trusted",
        )
    _validate_registry_snapshot_receipt(
        registry,
        code="terminal_publication_state_invalid",
    )
    if _normcase(str(registry["path"])) in seen_destinations:
        raise MaterialOperationRunnerError(
            "terminal_publication_state_invalid",
            "rolled-back registry path collides with an artifact destination",
        )


def _validate_registry_snapshot_receipt(
    registry: Mapping[str, object],
    *,
    code: str,
) -> None:
    """Cross-check the durable old-registry snapshot, not just its shape."""

    old_sha = registry.get("old_sha256")
    old_bytes_b64 = registry.get("old_bytes_b64")
    old_identity = registry.get("old_identity")
    if old_sha is None:
        if old_bytes_b64 is not None or old_identity is not None:
            raise MaterialOperationRunnerError(
                code,
                "registry receipt claims no old snapshot but carries old bytes/identity",
            )
        return
    if (
        type(old_sha) is not str
        or _SHA256_PATTERN.fullmatch(old_sha) is None
        or type(old_bytes_b64) is not str
        or not _valid_publication_identity(old_identity)
    ):
        raise MaterialOperationRunnerError(
            code,
            "registry receipt old snapshot is incomplete",
        )
    try:
        old_bytes = base64.b64decode(old_bytes_b64, validate=True)
    except (ValueError, TypeError, base64.binascii.Error) as exc:
        raise MaterialOperationRunnerError(
            code,
            "registry receipt old snapshot is not strict base64",
        ) from exc
    if hashlib.sha256(old_bytes).hexdigest() != old_sha:
        raise MaterialOperationRunnerError(
            code,
            "registry receipt old snapshot hash is inconsistent",
        )


def _verify_rolled_back_publication_effects(value: Mapping[str, object]) -> None:
    """Prove that a rolled-back journal no longer owns published artifacts."""

    moves = value["moves"]
    assert isinstance(moves, list)
    for move in moves:
        assert isinstance(move, dict)
        state = move["state"]
        destination = Path(str(move["destination"]))
        if state == "reused":
            _verify_publication_file_attestation(
                destination,
                expected_sha256=str(move["sha256"]),
                expected_size=int(move["size_bytes"]),
                expected_identity=move["destination_identity"],
                role="rolled-back reused destination",
            )
        else:
            _assert_publication_path_absent(destination, role="rolled-back destination")
        part_path = str(move["part_path"])
        if part_path:
            _assert_publication_path_absent(
                Path(part_path),
                role="rolled-back publication part",
            )
    registry = value["registry"]
    assert isinstance(registry, dict)
    if not registry:
        return
    registry_path = Path(str(registry["path"]))
    old_sha = registry["old_sha256"]
    if old_sha is None:
        _assert_publication_path_absent(registry_path, role="rolled-back registry")
        return
    old_bytes_b64 = registry["old_bytes_b64"]
    if not isinstance(old_bytes_b64, str):
        raise MaterialOperationRunnerError(
            "terminal_publication_state_invalid",
            "rolled-back registry has no old snapshot bytes",
        )
    try:
        old_bytes = base64.b64decode(old_bytes_b64, validate=True)
    except (ValueError, TypeError, base64.binascii.Error) as exc:
        raise MaterialOperationRunnerError(
            "terminal_publication_state_invalid",
            "rolled-back registry old snapshot is not strict base64",
        ) from exc
    if hashlib.sha256(old_bytes).hexdigest() != old_sha:
        raise MaterialOperationRunnerError(
            "terminal_publication_state_invalid",
            "rolled-back registry old snapshot hash is inconsistent",
        )
    _verify_publication_file_attestation(
        registry_path,
        expected_sha256=str(old_sha),
        expected_size=len(old_bytes),
        expected_identity=registry["old_identity"],
        role="rolled-back registry",
    )


def _validate_restart_retirement_tombstone_for_adoption(
    raw: bytes,
    *,
    execution_id: str,
    attempt_id: str,
    control_id: str,
    job_instance_id: str,
    operation: MaterialOperationKind,
    proof: MaterialRestartRetirementProof | MaterialRestartPublishedRetirementProof,
) -> None:
    """Validate the minimum immutable identity/completion gate for a retry."""

    value = _decode_strict_bounded_json(
        raw,
        max_bytes=MAX_RETIREMENT_BYTES,
        code="restart_retirement_tombstone_mismatch",
        detail="restart retirement tombstone is not strict bounded JSON",
    )
    if type(proof) is MaterialRestartPublishedRetirementProof:
        expected_reason = "publication_committed_after_restart"
        publication_mismatch = (
            value.get("publication_journal_sha256")
            != proof.publication_journal_sha256
            if type(value) is dict
            else True
        )
    else:
        expected_reason = f"attempt_{proof.status}_after_restart"
        publication_mismatch = False
    if (
        type(value) is not dict
        or value.get("protocol_version") != DESCRIPTOR_PROTOCOL_VERSION
        or value.get("state") != "retired"
        or value.get("reason_code") != expected_reason
        or proof.execution_id != execution_id
        or proof.attempt_id != attempt_id
        or proof.operation != operation
        or value.get("execution_id") != execution_id
        or value.get("attempt_id") != attempt_id
        or value.get("control_id") != control_id
        or value.get("job_instance_id") != job_instance_id
        or value.get("operation") != operation
        or value.get("attempt_history_sha256") != proof.attempt_history_sha256
        or value.get("final_payload_sha256") != proof.final_payload_sha256
        or value.get("completion_revision") != proof.execution_revision
        or value.get("completion_result_sha256") != proof.result_sha256
        or value.get("completion_observed_at") != proof.observed_at
        or value.get("checkpoint_sha256") != proof.checkpoint_sha256
        or publication_mismatch
        or type(value.get("retired_at")) is not str
    ):
        raise MaterialOperationRunnerError(
            "restart_retirement_tombstone_mismatch",
            "retirement tombstone does not match the checkpointed completion proof",
        )
    try:
        _validate_identity_time(value["retired_at"], "retired_at")
    except MaterialOperationRunnerError as exc:
        raise MaterialOperationRunnerError(
            "restart_retirement_tombstone_mismatch",
            "retirement tombstone has an invalid retirement timestamp",
        ) from exc


def _validate_published_retirement_bytes(
    raw: bytes,
    *,
    expected: Mapping[str, object],
) -> None:
    value = _decode_strict_bounded_json(
        raw,
        max_bytes=MAX_RETIREMENT_BYTES,
        code="publication_retirement_changed",
        detail="publication retirement tombstone is not strict bounded JSON",
    )
    if type(value) is not dict or value != expected:
        raise MaterialOperationRunnerError(
            "publication_retirement_changed",
            "publication retirement tombstone does not match this attempt",
        )


def _validate_persisted_retirement_identity(
    raw: bytes,
    *,
    expected: Mapping[str, object],
) -> None:
    """Accept only an exact same-attempt tombstone when its write timestamp raced.

    ``retired_at`` is intentionally generated at the call boundary.  If the
    native write acknowledgement is lost, a retry must be able to adopt the
    already durable tombstone without manufacturing a second timestamp, while
    every identity/control field remains byte-level equivalent.
    """

    value = _decode_strict_bounded_json(
        raw,
        max_bytes=MAX_RETIREMENT_BYTES,
        code="publication_retirement_changed",
        detail="retirement tombstone is not strict bounded JSON",
    )
    if type(value) is not dict or set(value) != set(expected):
        raise MaterialOperationRunnerError(
            "publication_retirement_changed",
            "retirement tombstone has an unexpected identity schema",
        )
    for key, expected_value in expected.items():
        if key != "retired_at" and value.get(key) != expected_value:
            raise MaterialOperationRunnerError(
                "publication_retirement_changed",
                "retirement tombstone belongs to another attempt",
            )
    _validate_identity_time(value.get("retired_at"), "retired_at")


def _decode_strict_descriptor_json(raw: bytes) -> object:
    if type(raw) is not bytes:
        raise MaterialOperationRunnerError(
            "descriptor_invalid",
            "descriptor must be UTF-8 bytes",
        )
    if not raw or len(raw) > MAX_DESCRIPTOR_BYTES:
        raise MaterialOperationRunnerError(
            "descriptor_size_invalid",
            "descriptor is empty or exceeds its 64 KiB byte limit",
        )
    if raw.startswith(b"\xef\xbb\xbf"):
        raise MaterialOperationRunnerError(
            "descriptor_invalid",
            "descriptor must not contain a UTF-8 BOM",
        )
    malformed = False
    value: object = None
    end = 0
    try:
        text = raw.decode("utf-8", errors="strict")
        decoder = json.JSONDecoder(
            object_pairs_hook=_descriptor_object_without_duplicates,
            parse_int=_parse_descriptor_integer,
            parse_float=_reject_descriptor_float,
            parse_constant=_reject_descriptor_constant,
        )
        value, end = decoder.raw_decode(text)
    except MaterialOperationRunnerError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        malformed = True
    if malformed:
        raise MaterialOperationRunnerError(
            "descriptor_invalid",
            "descriptor is not strict UTF-8 JSON",
        )
    if end != len(text):
        raise MaterialOperationRunnerError(
            "descriptor_invalid",
            "descriptor must not contain leading or trailing data",
        )
    return value


def _descriptor_object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MaterialOperationRunnerError(
                "descriptor_invalid",
                "duplicate descriptor keys are forbidden",
            )
        result[key] = value
    return result


def _parse_descriptor_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > 20:
        raise MaterialOperationRunnerError(
            "descriptor_invalid",
            "descriptor integer exceeds its digit limit",
        )
    return int(value)


def _reject_descriptor_float(_value: str) -> object:
    raise MaterialOperationRunnerError(
        "descriptor_invalid",
        "floating-point descriptor values are forbidden",
    )


def _reject_descriptor_constant(_value: str) -> object:
    raise MaterialOperationRunnerError(
        "descriptor_invalid",
        "NaN and Infinity are forbidden",
    )


def _coerce_launch_phase(value: object) -> MaterialLaunchPhase:
    if type(value) is MaterialLaunchPhase:
        return value
    if type(value) is str:
        try:
            return MaterialLaunchPhase(value)
        except ValueError:
            pass
    raise MaterialOperationRunnerError(
        "descriptor_transition_invalid",
        "launch phase is unknown",
    )


def _open_or_create_fixed_child(
    parent: ApprovedPhysicalDirectory,
    name: str,
) -> ApprovedPhysicalDirectory:
    parent.verify()
    try:
        child = parent.create_exclusive_child(name)
    except FileExistsError:
        child = approve_physical_d_write_root(parent.path / name)
    try:
        child.verify()
        parent.verify()
        if (
            _normcase(str(child.path.parent)) != _normcase(str(parent.path))
            or child.identity.volume_guid.casefold()
            != parent.identity.volume_guid.casefold()
        ):
            raise MaterialOperationRunnerError(
                "fixed_layout_identity_mismatch",
                "shared jobs directory escaped its approved parent",
            )
        return child
    except BaseException:
        child.close()
        raise


def _open_existing_fixed_child(
    parent: ApprovedPhysicalDirectory,
    name: str,
) -> ApprovedPhysicalDirectory:
    """Open one already-created fixed-layout directory without creating it.

    Restart adoption must never turn a missing or moved directory into a new
    attempt.  This is intentionally separate from ``_open_or_create...`` so a
    recovery path cannot accidentally create a directory while inspecting a
    durable record.
    """

    parent.verify()
    # ``ApprovedPhysicalDirectory`` performs the physical D-drive, reparse,
    # and identity checks for the complete path.  We still verify the direct
    # parent and volume relation here because the caller is reconstructing a
    # multi-root ownership tuple from untrusted on-disk names.
    try:
        child = approve_physical_d_write_root(parent.path / name)
    except FileNotFoundError:
        raise MaterialOperationRunnerError(
            "adoption_layout_missing",
            f"durable fixed-layout directory is missing: {name}",
        ) from None
    except BaseException as error:
        if getattr(error, "winerror", None) in {2, 3} or getattr(
            error, "code", None
        ) == "storage_path_unavailable":
            raise MaterialOperationRunnerError(
                "adoption_layout_missing",
                f"durable fixed-layout directory is missing: {name}",
            ) from None
        raise
    try:
        child.verify()
        parent.verify()
        if (
            _normcase(str(child.path.parent)) != _normcase(str(parent.path))
            or child.identity.volume_guid.casefold()
            != parent.identity.volume_guid.casefold()
        ):
            raise MaterialOperationRunnerError(
                "fixed_layout_identity_mismatch",
                "durable fixed-layout directory escaped its approved parent",
            )
        return child
    except BaseException:
        child.close()
        raise


def _new_independent_ids(execution_id: str) -> tuple[str, str, str]:
    for _ in range(UUID_GENERATION_ATTEMPTS):
        values = (str(uuid4()), str(uuid4()), str(uuid4()))
        if len({execution_id, *values}) == 4:
            return values
    raise MaterialOperationRunnerError(
        "uuid_generation_failed",
        "independent operation identifiers could not be generated",
    )


def _current_controller_identity() -> _ControllerIdentity:
    if (
        os.name != "nt"
        or win32api is None
        or win32process is None
    ):
        raise MaterialOperationRunnerError(
            "controller_identity_unavailable",
            "exact controller identity requires Windows process APIs",
        )
    try:
        pid = int(win32api.GetCurrentProcessId())
        process_times = win32process.GetProcessTimes(win32api.GetCurrentProcess())
        creation = process_times["CreationTime"]
        if not isinstance(creation, datetime) or creation.tzinfo is None:
            raise ValueError("creation time is not timezone-aware")
        creation_time = (
            creation.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    except Exception:
        raise MaterialOperationRunnerError(
            "controller_identity_unavailable",
            "Windows did not return a stable controller PID and creation time",
        ) from None
    if not 0 < pid <= 2**32 - 1:
        raise MaterialOperationRunnerError(
            "controller_identity_invalid",
            "controller PID is outside the Windows DWORD range",
        )
    return _ControllerIdentity(pid=pid, creation_time=creation_time)


def _preflight_worker_runtime(
    *,
    prepared: PreparedMaterialAttempt,
) -> _WorkerRuntimePreflight:
    if type(prepared) is not PreparedMaterialAttempt:
        raise MaterialOperationRunnerError(
            "prepared_type_invalid",
            "worker runtime preflight requires the owned prepared attempt",
        )
    prepared.directory.verify()
    if _WORKER_BOOTSTRAP_PATH is None:
        raise MaterialOperationRunnerError(
            "worker_bootstrap_unavailable",
            "Task 4 worker bootstrap is not installed for this runner",
        )
    application_path = _require_existing_d_path(
        Path(sys._base_executable),
        field="base_python",
        expect_file=True,
    )
    worker_bootstrap = _require_existing_d_path(
        _WORKER_BOOTSTRAP_PATH,
        field="worker_bootstrap",
        expect_file=True,
    )
    backend_root = _require_existing_d_path(
        Path(__file__).resolve().parents[2],
        field="backend_root",
        expect_file=False,
    )
    site_packages = _require_existing_d_path(
        Path(sys.prefix) / "Lib" / "site-packages",
        field="site_packages",
        expect_file=False,
    )
    cwd = _require_existing_d_path(
        prepared.directory.path,
        field="attempt_directory",
        expect_file=False,
    )
    temporary = _require_existing_d_path(
        prepared.directory.path / "tmp",
        field="attempt_tmp",
        expect_file=False,
    )
    profile = _require_existing_d_path(
        prepared.directory.path / "profile",
        field="attempt_profile",
        expect_file=False,
    )
    _require_existing_d_path(
        _PLAYWRIGHT_BROWSERS_PATH.parent,
        field="playwright_cache_parent",
        expect_file=False,
    )
    system_root = os.environ.get("SystemRoot")
    if type(system_root) is not str or not system_root or "\x00" in system_root:
        raise MaterialOperationRunnerError(
            "worker_environment_invalid",
            "Windows SystemRoot is unavailable for the isolated worker",
        )
    environment = MappingProxyType(
        {
            "SystemRoot": system_root,
            "WINDIR": system_root,
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "TMPDIR": str(temporary),
            "HOME": str(profile),
            "USERPROFILE": str(profile),
            "LOCALAPPDATA": str(profile),
            "APPDATA": str(profile),
            "PLAYWRIGHT_BROWSERS_PATH": str(_PLAYWRIGHT_BROWSERS_PATH),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    return _WorkerRuntimePreflight(
        application_path=application_path,
        worker_bootstrap=worker_bootstrap,
        backend_root=backend_root,
        site_packages=site_packages,
        cwd=cwd,
        environment=environment,
    )


def _build_worker_launch_spec(
    *,
    prepared: PreparedMaterialAttempt,
    pipe_name: str,
    runtime_preflight: _WorkerRuntimePreflight,
) -> _WorkerLaunchSpec:
    if type(prepared) is not PreparedMaterialAttempt:
        raise MaterialOperationRunnerError(
            "prepared_type_invalid",
            "worker launch requires the owned prepared attempt",
        )
    prepared.directory.verify()
    if type(runtime_preflight) is not _WorkerRuntimePreflight:
        raise MaterialOperationRunnerError(
            "worker_runtime_preflight_invalid",
            "worker launch requires the exact frozen runtime preflight",
        )
    if _normcase(str(runtime_preflight.cwd)) != _normcase(
        str(prepared.directory.path.resolve(strict=True))
    ):
        raise MaterialOperationRunnerError(
            "worker_runtime_preflight_invalid",
            "worker runtime preflight belongs to another attempt directory",
        )
    if (
        type(pipe_name) is not str
        or not pipe_name.startswith(_SECRET_PIPE_PREFIX)
        or "\x00" in pipe_name
        or len(pipe_name) > 256
    ):
        raise MaterialOperationRunnerError(
            "secret_pipe_name_invalid",
            "worker launch requires the fixed independent pipe name",
        )
    argv = (
        str(runtime_preflight.application_path),
        "-I",
        "-S",
        str(runtime_preflight.worker_bootstrap),
        "--backend-root",
        str(runtime_preflight.backend_root),
        "--site-packages",
        str(runtime_preflight.site_packages),
        "--attempt-dir",
        str(runtime_preflight.cwd),
        "--pipe-name",
        pipe_name,
    )
    return _WorkerLaunchSpec(
        application_path=runtime_preflight.application_path,
        argv=argv,
        cwd=runtime_preflight.cwd,
        environment=runtime_preflight.environment,
    )


def _require_existing_d_path(
    value: Path,
    *,
    field: str,
    expect_file: bool,
) -> Path:
    if not isinstance(value, Path):
        raise MaterialOperationRunnerError(
            "worker_path_invalid",
            f"{field} must be a Path",
        )
    raw = str(value)
    drive, tail = ntpath.splitdrive(raw)
    if (
        "\x00" in raw
        or raw.startswith(("\\\\", "//"))
        or drive.casefold() != "d:"
        or not tail.startswith(("\\", "/"))
    ):
        raise MaterialOperationRunnerError(
            "worker_path_invalid",
            f"{field} must be an absolute D path",
        )
    try:
        resolved = value.resolve(strict=True)
    except OSError:
        raise MaterialOperationRunnerError(
            "worker_path_invalid",
            f"{field} does not exist",
        ) from None
    resolved_drive, _ = ntpath.splitdrive(str(resolved))
    if resolved_drive.casefold() != "d:":
        raise MaterialOperationRunnerError(
            "worker_path_invalid",
            f"{field} escaped the approved D volume",
        )
    if expect_file and not resolved.is_file():
        raise MaterialOperationRunnerError(
            "worker_path_invalid",
            f"{field} must be an existing file",
        )
    if not expect_file and not resolved.is_dir():
        raise MaterialOperationRunnerError(
            "worker_path_invalid",
            f"{field} must be an existing directory",
        )
    return resolved


def _query_process_identity(pid: int) -> tuple[int, str]:
    _validate_windows_pid(pid, "root_pid")
    if win32api is None or win32con is None or win32process is None:
        raise MaterialOperationRunnerError(
            "root_identity_unavailable",
            "root identity query requires Windows process APIs",
        )
    handle = None
    try:
        handle = win32api.OpenProcess(0x1000, False, pid)
        actual_pid = int(win32process.GetProcessId(handle))
        times = win32process.GetProcessTimes(handle)
        creation = times["CreationTime"]
        if actual_pid != pid or not isinstance(creation, datetime) or creation.tzinfo is None:
            raise MaterialOperationRunnerError(
                "root_identity_invalid",
                "Windows returned an unstable root process identity",
            )
        creation_time = (
            creation.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    except MaterialOperationRunnerError:
        raise
    except Exception:
        raise MaterialOperationRunnerError(
            "root_identity_unavailable",
            "Windows could not open and identify the root process",
        ) from None
    finally:
        if handle is not None:
            handle.Close()
    return actual_pid, creation_time


def _wait_exact_process_exit(
    *,
    pid: int,
    creation_time: str,
    timeout_seconds: float,
) -> bool:
    """Prove that an exact PID/creation-time process is no longer alive.

    A reused PID is treated as proof that the recorded process identity has
    exited.  Access-denied and other API failures remain blocking: they do not
    become an inference merely because the process cannot be inspected.
    """
    _validate_windows_pid(pid, "process_pid")
    _validate_identity_time(creation_time, "process_creation_time")
    try:
        expected_creation = datetime.fromisoformat(
            creation_time.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise MaterialOperationRunnerError(
            "process_exit_identity_invalid",
            "recorded process creation time cannot be compared",
        ) from None
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds < 0
    ):
        raise MaterialOperationRunnerError(
            "process_exit_timeout_invalid",
            "process exit timeout must be a finite non-negative number",
        )
    if (
        win32api is None
        or win32con is None
        or win32process is None
        or win32event is None
    ):
        raise MaterialOperationRunnerError(
            "process_exit_unavailable",
            "exact process-exit proof requires Windows process APIs",
        )
    handle = None
    try:
        desired_access = 0x00001000 | 0x00100000  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        try:
            handle = win32api.OpenProcess(desired_access, False, pid)
        except Exception as error:
            # Once the PID is gone, Windows commonly reports either
            # ERROR_INVALID_PARAMETER or ERROR_NOT_FOUND.  Both are safe
            # evidence that this exact recorded identity is no longer live;
            # access-denied and every other failure remain blocked.
            if getattr(error, "winerror", None) in {87, 1168}:
                return True
            raise MaterialOperationRunnerError(
                "process_exit_unavailable",
                "Windows could not open the recorded process identity",
            ) from None
        actual_pid = int(win32process.GetProcessId(handle))
        times = win32process.GetProcessTimes(handle)
        actual_creation = times["CreationTime"]
        if (
            actual_pid != pid
            or not isinstance(actual_creation, datetime)
            or actual_creation.tzinfo is None
        ):
            raise MaterialOperationRunnerError(
                "process_exit_identity_invalid",
                "Windows returned an unstable recorded process identity",
            )
        if actual_creation.astimezone(timezone.utc) != expected_creation:
            return True
        timeout_milliseconds = min(
            0xFFFFFFFE,
            int(math.ceil(float(timeout_seconds) * 1000.0)),
        )
        wait_result = win32event.WaitForSingleObject(
            handle,
            timeout_milliseconds,
        )
        if wait_result == win32event.WAIT_OBJECT_0:
            return True
        if wait_result == win32event.WAIT_TIMEOUT:
            return False
        raise MaterialOperationRunnerError(
            "process_exit_unavailable",
            "Windows did not return a valid process wait result",
        )
    except MaterialOperationRunnerError:
        raise
    except Exception:
        raise MaterialOperationRunnerError(
            "process_exit_unavailable",
            "Windows could not prove the recorded process exit",
        ) from None
    finally:
        if handle is not None:
            try:
                handle.Close()
            except Exception as error:
                raise MaterialOperationRunnerError(
                    "process_exit_close_unknown",
                    "the exact process identity handle could not be closed",
                ) from error


def _connect_secret_pipe(
    *,
    pipe_owner: object,
    absolute_deadline: float,
) -> None:
    if type(pipe_owner) is not _SecretPipeOwner:
        raise MaterialOperationRunnerError(
            "secret_pipe_owner_invalid",
            "connect requires the exact secret-pipe owner",
        )
    if pipe_owner.active_operation is not None:
        raise MaterialOperationRunnerError(
            "secret_pipe_operation_live",
            "secret pipe already owns an overlapped operation",
        )
    try:
        operation = OwnedOverlappedPipeOperation.start_connect(
            pipe_handle=int(pipe_owner.pipe_handle),
            absolute_deadline=absolute_deadline,
        )
    except OverlappedPipeError as error:
        if type(error.resource_owner) is OwnedOverlappedPipeOperation:
            pipe_owner.active_operation = error.resource_owner
        raise
    pipe_owner.active_operation = operation
    completion = operation.wait_to_deadline()
    if (
        completion.operation is not OperationKind.CONNECT
        or completion.native_outcome is not NativeOutcome.SUCCESS
        or completion.deadline_exceeded
    ):
        raise MaterialOperationRunnerError(
            "secret_pipe_connect_invalid",
            "secret-pipe connect did not reach an exact successful terminal state",
        )
    operation.close()
    pipe_owner.active_operation = None


def _verify_secret_pipe_client(
    *,
    pipe_owner: object,
    root_pid: int,
    root_creation_time: str,
    job_owner: object,
) -> None:
    if type(pipe_owner) is not _SecretPipeOwner:
        raise MaterialOperationRunnerError(
            "secret_pipe_owner_invalid",
            "client verification requires the exact secret-pipe owner",
        )
    if type(job_owner) is not WindowsJobObject:
        raise MaterialOperationRunnerError(
            "job_owner_invalid",
            "client verification requires the exact unnamed Job owner",
        )
    _validate_windows_pid(root_pid, "root_pid")
    _validate_identity_time(root_creation_time, "root_creation_time")
    _require_secret_pipe_windows()
    try:
        client_pid = int(win32pipe.GetNamedPipeClientProcessId(pipe_owner.pipe_handle))
    except Exception:
        raise MaterialOperationRunnerError(
            "secret_pipe_client_unavailable",
            "Windows could not identify the connected pipe client",
        ) from None
    if client_pid != root_pid:
        raise MaterialOperationRunnerError(
            "secret_pipe_client_mismatch",
            "connected pipe client PID is not the exact suspended root identity",
        )
    client_handle = None
    try:
        client_handle = win32api.OpenProcess(0x1000, False, client_pid)
        actual_pid = int(win32process.GetProcessId(client_handle))
        if actual_pid != root_pid:
            raise MaterialOperationRunnerError(
                "secret_pipe_client_mismatch",
                "opened pipe client PID changed before verification",
            )
        times = win32process.GetProcessTimes(client_handle)
        creation = times["CreationTime"]
        if not isinstance(creation, datetime) or creation.tzinfo is None:
            raise MaterialOperationRunnerError(
                "secret_pipe_client_identity_invalid",
                "pipe client creation time is unavailable",
            )
        actual_creation_time = (
            creation.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        if actual_creation_time != root_creation_time:
            raise MaterialOperationRunnerError(
                "secret_pipe_client_mismatch",
                "pipe client creation time is not the exact root identity",
            )
        try:
            in_job = bool(
                win32job.IsProcessInJob(client_handle, job_owner.handle)
            )
        except Exception:
            raise MaterialOperationRunnerError(
                "secret_pipe_client_membership_unavailable",
                "Windows could not verify pipe client Job membership",
            ) from None
        if not in_job:
            raise MaterialOperationRunnerError(
                "secret_pipe_client_not_in_job",
                "connected pipe client is not in the target unnamed Job",
            )
    finally:
        if client_handle is not None:
            client_handle.Close()


def _write_secret_frame(
    *,
    pipe_owner: object,
    immutable_frame: bytes,
    absolute_deadline: float,
) -> None:
    if type(pipe_owner) is not _SecretPipeOwner:
        raise MaterialOperationRunnerError(
            "secret_pipe_owner_invalid",
            "secret write requires the exact secret-pipe owner",
        )
    if type(immutable_frame) is not bytes or not 5 <= len(immutable_frame) <= 4 + MAX_SECRET_BYTES:
        raise MaterialOperationRunnerError(
            "secret_frame_invalid",
            "secret frame must be one bounded length-prefixed bytes object",
        )
    payload_length = int.from_bytes(immutable_frame[:4], "big")
    if payload_length != len(immutable_frame) - 4 or not 1 <= payload_length <= MAX_SECRET_BYTES:
        raise MaterialOperationRunnerError(
            "secret_frame_invalid",
            "secret frame length prefix does not match its exact payload",
        )
    if pipe_owner.active_operation is not None:
        raise MaterialOperationRunnerError(
            "secret_pipe_operation_live",
            "secret pipe already owns an overlapped operation",
        )
    try:
        operation = OwnedOverlappedPipeOperation.start_write(
            pipe_handle=int(pipe_owner.pipe_handle),
            immutable_frame=immutable_frame,
            absolute_deadline=absolute_deadline,
        )
    except OverlappedPipeError as error:
        if type(error.resource_owner) is OwnedOverlappedPipeOperation:
            pipe_owner.active_operation = error.resource_owner
        raise
    pipe_owner.active_operation = operation
    completion = operation.wait_to_deadline()
    if (
        completion.operation is not OperationKind.WRITE
        or completion.native_outcome is not NativeOutcome.SUCCESS
        or completion.bytes_transferred != len(immutable_frame)
        or completion.deadline_exceeded
    ):
        raise MaterialOperationRunnerError(
            "secret_write_incomplete",
            "secret pipe write did not reach an exact full terminal transfer",
        )
    operation.close()
    pipe_owner.active_operation = None


def _wait_worker_ready(
    *,
    prepared: PreparedMaterialAttempt,
    expected_operation: MaterialOperationKind,
    expected_root_pid: int,
    expected_request_sha256: str,
    startup_deadline: float,
    operation_deadline: float,
) -> MaterialWorkerReady:
    if type(prepared) is not PreparedMaterialAttempt:
        raise MaterialOperationRunnerError(
            "prepared_type_invalid",
            "ready wait requires the owned prepared attempt",
        )
    absolute_deadline = min(startup_deadline, operation_deadline)
    while True:
        try:
            before_read = _monotonic()
        except BaseException:
            raise MaterialOperationRunnerError(
                "run_clock_unavailable",
                "the ready wait monotonic clock could not be sampled",
            ) from None
        if before_read >= absolute_deadline:
            raise MaterialOperationRunnerError(
                "worker_ready_timeout",
                "worker ready did not appear before the active deadlines",
            )
        prepared.directory.verify()
        try:
            raw = _read_verified_bytes(
                parent=prepared.directory,
                filename="ready.json",
                max_bytes=MAX_READY_BYTES,
            )
        except Exception as error:
            winerror = getattr(error, "winerror", None)
            # A durable publisher may have created the final name while its
            # no-share handle is still flushing/closing.  Missing-path and
            # sharing/lock violations are the only retryable publication states.
            if winerror not in {2, 3, 32, 33}:
                raise
            try:
                now = _monotonic()
            except BaseException:
                raise MaterialOperationRunnerError(
                    "run_clock_unavailable",
                    "the ready wait monotonic clock could not be sampled",
                ) from None
            if now >= absolute_deadline:
                raise MaterialOperationRunnerError(
                    "worker_ready_timeout",
                    "worker ready did not appear before the active deadlines",
                )
            time.sleep(min(0.01, absolute_deadline - now))
            continue
        try:
            ready = decode_material_worker_ready_bound(
                raw,
                expected_execution_id=prepared.execution_id,
                expected_attempt_id=prepared.attempt_id,
                expected_operation=expected_operation,
                expected_root_pid=expected_root_pid,
                expected_request_sha256=expected_request_sha256,
            )
        except MaterialProtocolError:
            raise MaterialOperationRunnerError(
                "worker_ready_invalid",
                "worker ready is malformed or does not match parent identity",
            ) from None
        try:
            after_read = _monotonic()
        except BaseException:
            raise MaterialOperationRunnerError(
                "run_clock_unavailable",
                "the ready wait monotonic clock could not be sampled",
            ) from None
        if after_read >= absolute_deadline:
            raise MaterialOperationRunnerError(
                "worker_ready_timeout",
                "worker ready was observed after the active deadlines",
            )
        return ready


def _wait_job_empty(
    *,
    job_owner: object,
    operation_deadline: float,
) -> None:
    if type(job_owner) is not WindowsJobObject:
        raise MaterialOperationRunnerError(
            "job_owner_invalid",
            "Job-empty proof requires the exact unnamed Job owner",
        )
    try:
        now = _monotonic()
    except BaseException:
        raise MaterialOperationRunnerError(
            "run_clock_unavailable",
            "the Job wait monotonic clock could not be sampled",
        ) from None
    remaining = operation_deadline - now
    if remaining <= 0 or not job_owner.wait_until_empty(remaining):
        raise MaterialOperationRunnerError(
            "operation_timeout",
            "the Job tree did not become empty before its operation deadline",
        )
    try:
        after_wait = _monotonic()
    except BaseException:
        raise MaterialOperationRunnerError(
            "run_clock_unavailable",
            "the Job-empty post-wait monotonic clock could not be sampled",
        ) from None
    if after_wait >= operation_deadline:
        raise MaterialOperationRunnerError(
            "operation_timeout",
            "the Job became empty only after its operation deadline",
        )
    active_processes = job_owner.active_process_count()
    try:
        after_query = _monotonic()
    except BaseException:
        raise MaterialOperationRunnerError(
            "run_clock_unavailable",
            "the Job-empty post-query monotonic clock could not be sampled",
        ) from None
    if after_query >= operation_deadline:
        raise MaterialOperationRunnerError(
            "operation_timeout",
            "the final Job query completed only after its operation deadline",
        )
    if active_processes != 0:
        raise MaterialOperationRunnerError(
            "job_empty_unproven",
            "the final Job query did not prove ActiveProcesses equals zero",
        )


def _read_worker_result(
    *,
    prepared: PreparedMaterialAttempt,
    expected_operation: MaterialOperationKind,
) -> MaterialWorkerResult:
    if type(prepared) is not PreparedMaterialAttempt:
        raise MaterialOperationRunnerError(
            "prepared_type_invalid",
            "result read requires the owned prepared attempt",
        )
    try:
        raw = _read_verified_bytes(
            parent=prepared.directory,
            filename="result.json",
            max_bytes=MAX_RESULT_BYTES,
        )
    except MaterialOperationRunnerError:
        raise
    except Exception:
        raise MaterialOperationRunnerError(
            "worker_result_unavailable",
            "worker result could not be opened after the Job became empty",
        ) from None
    try:
        return decode_material_worker_result(
            raw,
            expected_execution_id=prepared.execution_id,
            expected_attempt_id=prepared.attempt_id,
            expected_operation=expected_operation,
        )
    except MaterialProtocolError:
        raise MaterialOperationRunnerError(
            "worker_result_invalid",
            "worker result is malformed or does not match parent identity",
        ) from None


def _collect_natural_termination(
    *,
    process_owner: object,
    root_pid: int,
) -> TerminationEvidence:
    if type(process_owner) is not SuspendedJobProcess:
        raise MaterialOperationRunnerError(
            "process_owner_invalid",
            "natural termination proof requires the exact root-process owner",
        )
    _validate_windows_pid(root_pid, "root_pid")
    if process_owner.pid != root_pid:
        raise MaterialOperationRunnerError(
            "root_identity_mismatch",
            "natural termination owner is not the exact root PID",
        )
    try:
        root_signaled = process_owner.wait_until_exit(0.0)
        exit_code = process_owner.exit_code() if root_signaled else None
    except Exception:
        raise MaterialOperationRunnerError(
            "root_termination_unavailable",
            "Windows could not prove the root process exit after Job empty",
        ) from None
    if not root_signaled:
        raise MaterialOperationRunnerError(
            "root_termination_unproven",
            "Job empty did not coincide with a signaled root process handle",
        )
    try:
        return TerminationEvidence(
            platform="windows_job",
            root_pid=root_pid,
            exit_code=exit_code,
            termination_method="natural",
            active_processes=0,
            observed_at=_utc_now(),
        )
    except MaterialProtocolError:
        raise MaterialOperationRunnerError(
            "termination_evidence_invalid",
            "Windows returned an invalid natural-termination exit code",
        ) from None


def _close_natural_completion_resources(
    *,
    process_owner: object,
    job_owner: object,
) -> None:
    if type(process_owner) is not SuspendedJobProcess:
        raise MaterialOperationRunnerError(
            "process_owner_invalid",
            "natural completion close requires the exact root-process owner",
        )
    if type(job_owner) is not WindowsJobObject:
        raise MaterialOperationRunnerError(
            "job_owner_invalid",
            "natural completion close requires the exact unnamed Job owner",
        )
    errors: list[BaseException] = []
    try:
        process_owner.close_process_handle()
    except BaseException as error:
        errors.append(error)
    try:
        job_owner.close()
    except BaseException as error:
        errors.append(error)
    if errors:
        raise MaterialOperationRunnerError(
            "completion_resource_close_failed",
            "natural completion resources could not all be closed",
        ) from BaseExceptionGroup(
            "natural completion resource close failures",
            errors,
        )


def _terminate_job_and_collect(
    *,
    job_owner: object,
    process_owner: object,
    root_pid: int,
    never_resumed: bool,
) -> TerminationEvidence:
    if type(job_owner) is not WindowsJobObject:
        raise MaterialOperationRunnerError(
            "job_owner_invalid",
            "termination requires the exact unnamed Job owner",
        )
    if type(process_owner) is not SuspendedJobProcess:
        raise MaterialOperationRunnerError(
            "process_owner_invalid",
            "termination requires the exact root-process owner",
        )
    _validate_windows_pid(root_pid, "root_pid")
    if process_owner.pid != root_pid:
        raise MaterialOperationRunnerError(
            "root_identity_mismatch",
            "termination owner is not the exact recorded root PID",
        )
    try:
        monitor_deadline = _monotonic() + 10.0
        job_owner.terminate(0xE00003B6)
        if _monotonic() >= monitor_deadline:
            raise MaterialOperationRunnerError(
                "termination_timeout",
                "Job termination syscall exceeded the quarantine monitor window",
            )
        if not job_owner.wait_until_empty(
            max(0.0, monitor_deadline - _monotonic())
        ):
            raise MaterialOperationRunnerError(
                "termination_timeout",
                "terminated Job did not become empty during the quarantine monitor",
            )
        if job_owner.active_process_count() != 0:
            raise MaterialOperationRunnerError(
                "termination_unproven",
                "terminated Job query did not prove ActiveProcesses equals zero",
            )
        if _monotonic() >= monitor_deadline:
            raise MaterialOperationRunnerError(
                "termination_timeout",
                "Job empty proof exceeded the quarantine monitor window",
            )
        # TerminateJobObject can report the Job empty just before the root
        # process handle transitions to signaled.  Give that independently
        # owned handle a bounded containment wait before declaring the proof
        # unavailable; a zero-time probe creates a false quarantine on the
        # normal Windows scheduling race.
        if not process_owner.wait_until_exit(
            max(0.0, monitor_deadline - _monotonic())
        ):
            raise MaterialOperationRunnerError(
                "root_termination_unproven",
                "terminated root process is not signaled after Job empty",
            )
        exit_code = process_owner.exit_code()
    except MaterialOperationRunnerError:
        raise
    except Exception:
        raise MaterialOperationRunnerError(
            "termination_unavailable",
            "Windows could not terminate and verify the worker Job",
        ) from None
    try:
        return TerminationEvidence(
            platform="windows_job",
            root_pid=root_pid,
            exit_code=exit_code,
            termination_method="never_resumed" if never_resumed else "job_terminate",
            active_processes=0,
            observed_at=_utc_now(),
        )
    except MaterialProtocolError:
        raise MaterialOperationRunnerError(
            "termination_evidence_invalid",
            "terminated worker returned invalid typed evidence",
        ) from None


def _terminate_job_for_quarantine(*, job_owner: object) -> bool:
    """Terminate and observe Job emptiness without claiming root identity."""
    if type(job_owner) is not WindowsJobObject:
        raise MaterialOperationRunnerError(
            "job_owner_invalid",
            "quarantine termination requires the exact unnamed Job owner",
        )
    try:
        monitor_deadline = _monotonic() + 10.0
        job_owner.terminate(0xE00003B6)
        if _monotonic() >= monitor_deadline:
            raise MaterialOperationRunnerError(
                "termination_timeout",
                "quarantine termination syscall exceeded the monitor window",
            )
        remaining = max(0.0, monitor_deadline - _monotonic())
        if not job_owner.wait_until_empty(remaining):
            raise MaterialOperationRunnerError(
                "termination_timeout",
                "quarantine Job did not become empty during the monitor window",
            )
        if job_owner.active_process_count() != 0:
            raise MaterialOperationRunnerError(
                "termination_unproven",
                "quarantine Job query did not prove ActiveProcesses equals zero",
            )
        if _monotonic() >= monitor_deadline:
            raise MaterialOperationRunnerError(
                "termination_timeout",
                "quarantine Job empty proof exceeded the monitor window",
            )
    except MaterialOperationRunnerError:
        raise
    except Exception:
        raise MaterialOperationRunnerError(
            "termination_unavailable",
            "Windows could not contain the worker Job for quarantine",
        ) from None
    return True


def _create_secret_pipe_owner(
    *,
    forbidden_ids: tuple[str, ...],
) -> _SecretPipeOwner:
    _require_secret_pipe_windows()
    name = _new_secret_pipe_name(forbidden_ids)
    descriptor, attributes = _secret_pipe_security_attributes()
    open_mode = (
        win32pipe.PIPE_ACCESS_OUTBOUND
        | _FILE_FLAG_FIRST_PIPE_INSTANCE
        | win32file.FILE_FLAG_OVERLAPPED
    )
    pipe_mode = (
        win32pipe.PIPE_TYPE_MESSAGE
        | win32pipe.PIPE_READMODE_MESSAGE
        | win32pipe.PIPE_WAIT
        | win32pipe.PIPE_REJECT_REMOTE_CLIENTS
    )
    handle = None
    try:
        handle = win32pipe.CreateNamedPipe(
            name,
            open_mode,
            pipe_mode,
            1,
            4 + MAX_SECRET_BYTES,
            4 + MAX_SECRET_BYTES,
            0,
            attributes,
        )
        win32api.SetHandleInformation(handle, win32con.HANDLE_FLAG_INHERIT, 0)
        if win32api.GetHandleInformation(handle) & win32con.HANDLE_FLAG_INHERIT:
            raise MaterialOperationRunnerError(
                "secret_pipe_inheritable",
                "secret pipe handle remained inheritable",
            )
        _verify_secret_pipe_security(handle)
        return _SecretPipeOwner(
            name=name,
            pipe_handle=handle,
            security_descriptor=descriptor,
            security_attributes=attributes,
        )
    except BaseException as primary:
        if handle is not None:
            try:
                handle.Close()
            except BaseException as close_error:
                raise MaterialOperationRunnerError(
                    "secret_pipe_close_failed",
                    "secret pipe creation failed and its handle could not close",
                ) from BaseExceptionGroup(
                    "secret pipe creation and close failures",
                    [primary, close_error],
                )
        raise


def _new_secret_pipe_name(forbidden_ids: tuple[str, ...]) -> str:
    if type(forbidden_ids) is not tuple or len(forbidden_ids) != 4:
        raise MaterialOperationRunnerError(
            "pipe_identity_invalid",
            "pipe nonce requires the four immutable operation identities",
        )
    forbidden: set[UUID] = set()
    for index, value in enumerate(forbidden_ids):
        _validate_uuid(value, f"forbidden_id_{index}")
        forbidden.add(UUID(value))
    if len(forbidden) != 4:
        raise MaterialOperationRunnerError(
            "pipe_identity_invalid",
            "pipe nonce comparison identities must remain independent",
        )
    for _ in range(PIPE_NONCE_GENERATION_ATTEMPTS):
        nonce = uuid4()
        if type(nonce) is UUID and nonce not in forbidden:
            return _SECRET_PIPE_PREFIX + nonce.hex
    raise MaterialOperationRunnerError(
        "pipe_nonce_generation_failed",
        "an independent 128-bit secret-pipe nonce could not be generated",
    )


def _secret_pipe_security_attributes() -> tuple[object, object]:
    _require_secret_pipe_windows()
    user_sid = _current_user_sid()
    system_sid = win32security.CreateWellKnownSid(
        win32security.WinLocalSystemSid,
        None,
    )
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(
        win32con.ACL_REVISION,
        _signed_dword(_SECRET_PIPE_USER_ACCESS),
        user_sid,
    )
    dacl.AddAccessAllowedAce(
        win32con.ACL_REVISION,
        _signed_dword(_SECRET_PIPE_SYSTEM_ACCESS),
        system_sid,
    )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(user_sid, False)
    descriptor.SetSecurityDescriptorDacl(True, dacl, False)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    attributes.bInheritHandle = False
    return descriptor, attributes


def _verify_secret_pipe_security(handle: object) -> None:
    descriptor = win32security.GetKernelObjectSecurity(
        handle,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION,
    )
    owner_sid = descriptor.GetSecurityDescriptorOwner()
    expected_user = str(
        win32security.ConvertSidToStringSid(_current_user_sid())
    )
    if owner_sid is None or str(
        win32security.ConvertSidToStringSid(owner_sid)
    ) != expected_user:
        raise MaterialOperationRunnerError(
            "secret_pipe_security_invalid",
            "secret pipe owner is not the current user",
        )
    dacl = descriptor.GetSecurityDescriptorDacl()
    if dacl is None or dacl.GetAceCount() != 2:
        raise MaterialOperationRunnerError(
            "secret_pipe_security_invalid",
            "secret pipe DACL is absent or not exact",
        )
    expected = {
        expected_user: _SECRET_PIPE_USER_ACCESS_EFFECTIVE,
        "S-1-5-18": _SECRET_PIPE_SYSTEM_ACCESS_EFFECTIVE,
    }
    actual: dict[str, int] = {}
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        (ace_type, _ace_flags), access_mask, sid = ace
        if ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE:
            raise MaterialOperationRunnerError(
                "secret_pipe_security_invalid",
                "secret pipe DACL contains a non-allow ACE",
            )
        sid_text = str(win32security.ConvertSidToStringSid(sid))
        if sid_text in actual:
            raise MaterialOperationRunnerError(
                "secret_pipe_security_invalid",
                "secret pipe DACL repeats an identity",
            )
        actual[sid_text] = int(access_mask) & 0xFFFFFFFF
    if actual != expected:
        raise MaterialOperationRunnerError(
            "secret_pipe_security_invalid",
            "secret pipe DACL is not current-user plus SYSTEM exact access",
        )


def _current_user_sid() -> object:
    _require_secret_pipe_windows()
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32con.TOKEN_QUERY,
    )
    try:
        sid, _attributes = win32security.GetTokenInformation(
            token,
            win32security.TokenUser,
        )
        return sid
    finally:
        token.Close()


def _require_secret_pipe_windows() -> None:
    if (
        os.name != "nt"
        or pywintypes is None
        or win32api is None
        or win32con is None
        or win32file is None
        or win32pipe is None
        or win32security is None
    ):
        raise MaterialOperationRunnerError(
            "secret_pipe_unsupported",
            "secure named pipes require Windows pywin32 APIs",
        )


def _signed_dword(value: int) -> int:
    return value if value <= 0x7FFFFFFF else value - 0x100000000


def _read_verified_bytes(
    *,
    parent: ApprovedPhysicalDirectory,
    filename: str,
    max_bytes: int,
) -> bytes:
    if win32api is None or win32con is None or win32file is None:
        raise MaterialOperationRunnerError(
            "descriptor_read_unsupported",
            "verified descriptor reads require Windows file APIs",
        )
    parent.verify()
    path = Path(parent.stable_volume_path) / filename
    handle = None
    try:
        handle = win32file.CreateFile(
            str(path),
            win32con.GENERIC_READ,
            win32file.FILE_SHARE_READ,
            None,
            win32con.OPEN_EXISTING,
            win32file.FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        win32api.SetHandleInformation(handle, win32con.HANDLE_FLAG_INHERIT, 0)
        before = win32file.GetFileInformationByHandle(handle)
        attributes = int(before[0])
        byte_count = (int(before[5]) << 32) | int(before[6])
        file_index = (int(before[8]) << 32) | int(before[9])
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise MaterialOperationRunnerError(
                "descriptor_reparse_forbidden",
                "descriptor is a reparse point",
            )
        if attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise MaterialOperationRunnerError(
                "descriptor_not_file",
                "descriptor is not a regular file",
            )
        if (
            int(before[4]) != parent.identity.volume_serial
            or file_index == 0
            or int(before[7]) != 1
        ):
            raise MaterialOperationRunnerError(
                "descriptor_identity_invalid",
                "descriptor identity or link count is not trusted",
            )
        if not 0 < byte_count <= max_bytes:
            raise MaterialOperationRunnerError(
                "descriptor_size_invalid",
                "descriptor is empty or exceeds its byte limit",
            )
        status, payload = win32file.ReadFile(handle, byte_count)
        if status not in {0, None} or len(payload) != byte_count:
            raise MaterialOperationRunnerError(
                "descriptor_read_incomplete",
                "descriptor could not be read exactly once",
            )
        after = win32file.GetFileInformationByHandle(handle)
        stable_indexes = (0, 4, 5, 6, 7, 8, 9)
        if tuple(after[index] for index in stable_indexes) != tuple(
            before[index] for index in stable_indexes
        ):
            raise MaterialOperationRunnerError(
                "descriptor_identity_changed",
                "descriptor metadata changed during verification",
            )
        parent.verify()
        result = bytes(payload)
    except BaseException as primary:
        if handle is not None:
            try:
                handle.Close()
            except BaseException as close_error:
                raise MaterialOperationRunnerError(
                    "descriptor_close_failed",
                    "descriptor handle could not be closed after a read failure",
                ) from BaseExceptionGroup(
                    "descriptor read and close failures",
                    [primary, close_error],
                )
        raise
    else:
        try:
            handle.Close()
        except BaseException:
            raise MaterialOperationRunnerError(
                "descriptor_close_failed",
                "descriptor handle could not be closed after verification",
            ) from None
        return result


def _read_optional_verified_bytes(
    *,
    parent: ApprovedPhysicalDirectory,
    filename: str,
    max_bytes: int,
) -> bytes | None:
    """Read an optional owned file while preserving every non-missing error."""

    try:
        return _read_verified_bytes(
            parent=parent,
            filename=filename,
            max_bytes=max_bytes,
        )
    except FileNotFoundError:
        return None
    except Exception as error:
        if getattr(error, "winerror", None) in {2, 3}:
            return None
        raise


def _read_adopted_request(
    *,
    attempt: ApprovedPhysicalDirectory,
    descriptor: MaterialOperationContainerDescriptor,
    code_prefix: Literal[
        "adoption",
        "restart_retirement",
        "restart_publication_retirement",
    ],
) -> bytes | None:
    """Read and bind the immutable request receipt for restart ownership."""

    if descriptor.request_sha256 is None:
        return None
    request_bytes = _read_verified_bytes(
        parent=attempt,
        filename=REQUEST_FILENAME,
        max_bytes=MAX_REQUEST_BYTES,
    )
    if hashlib.sha256(request_bytes).hexdigest() != descriptor.request_sha256:
        raise MaterialOperationRunnerError(
            f"{code_prefix}_request_changed",
            "durable request bytes do not match the descriptor receipt",
        )
    try:
        request = decode_material_operation_request(request_bytes)
    except MaterialProtocolError:
        raise MaterialOperationRunnerError(
            f"{code_prefix}_request_invalid",
            "durable request failed the frozen material protocol",
        ) from None
    if (
        request.execution_id != descriptor.execution_id
        or request.attempt_id != descriptor.attempt_id
        or request.operation != descriptor.operation
    ):
        raise MaterialOperationRunnerError(
            f"{code_prefix}_request_identity_mismatch",
            "durable request belongs to another attempt",
        )
    return request_bytes


def _read_adopted_outcome(
    *,
    prepared: PreparedMaterialAttempt,
    operation: MaterialOperationKind,
) -> tuple[MaterialOperationOutcome | None, str | None]:
    """Rebuild only an outcome backed by its immutable controller journal."""

    termination_bytes = _read_optional_verified_bytes(
        parent=prepared.control_directory,
        filename=TERMINATION_OUTCOME_FILENAME,
        max_bytes=MAX_TERMINATION_OUTCOME_BYTES,
    )
    result_bytes = _read_optional_verified_bytes(
        parent=prepared.directory,
        filename="result.json",
        max_bytes=MAX_RESULT_BYTES,
    )
    if termination_bytes is None:
        if result_bytes is not None:
            raise MaterialOperationRunnerError(
                "adoption_outcome_incomplete",
                "worker result has no durable termination-outcome predecessor",
            )
        return None, None
    (
        execution_id,
        attempt_id,
        job_instance_id,
        termination,
        _observed_at,
    ) = _decode_termination_outcome_bytes(termination_bytes)
    if (
        execution_id != prepared.execution_id
        or attempt_id != prepared.attempt_id
        or job_instance_id != prepared.job_instance_id
    ):
        raise MaterialOperationRunnerError(
            "adoption_outcome_identity_mismatch",
            "termination outcome belongs to another durable attempt",
        )
    result: MaterialWorkerResult | None = None
    if result_bytes is not None:
        try:
            result = decode_material_worker_result(
                result_bytes,
                expected_execution_id=prepared.execution_id,
                expected_attempt_id=prepared.attempt_id,
                expected_operation=operation,
            )
        except MaterialProtocolError:
            raise MaterialOperationRunnerError(
                "adoption_outcome_invalid",
                "worker result failed the frozen material protocol",
            ) from None
    try:
        outcome = MaterialOperationOutcome(
            status=("terminated" if result is None else result.status),
            result=result,
            termination=termination,
        )
    except MaterialProtocolError:
        raise MaterialOperationRunnerError(
            "adoption_outcome_invalid",
            "termination outcome and worker result are inconsistent",
        ) from None
    return outcome, hashlib.sha256(termination_bytes).hexdigest()


def _read_adopted_quarantine_state(
    *,
    control: ApprovedPhysicalDirectory,
    descriptor: MaterialOperationContainerDescriptor,
    descriptor_hash: str,
    termination_bytes: bytes,
) -> tuple[str | None, str | None]:
    """Accept only the one quarantine state restart reconciliation can resolve."""

    quarantine_bytes = _read_optional_verified_bytes(
        parent=control,
        filename=QUARANTINE_FILENAME,
        max_bytes=MAX_QUARANTINE_BYTES,
    )
    resolution_bytes = _read_optional_verified_bytes(
        parent=control,
        filename=QUARANTINE_RESOLUTION_FILENAME,
        max_bytes=MAX_QUARANTINE_RESOLUTION_BYTES,
    )
    if quarantine_bytes is None:
        if resolution_bytes is not None:
            raise MaterialOperationRunnerError(
                "adoption_quarantine_changed",
                "quarantine resolution has no immutable quarantine predecessor",
            )
        return None, None
    quarantine = _decode_quarantine_bytes(quarantine_bytes)
    if (
        quarantine["execution_id"] != descriptor.execution_id
        or quarantine["attempt_id"] != descriptor.attempt_id
        or quarantine["control_id"] != descriptor.control_id
        or quarantine["job_instance_id"] != descriptor.job_instance_id
        or quarantine["phase"] != descriptor.phase.value
        or quarantine["container_sha256"] != descriptor_hash
    ):
        raise MaterialOperationRunnerError(
            "adoption_quarantine_changed",
            "durable quarantine does not bind to the adopted descriptor",
        )
    if quarantine["reason_code"] != "io_drain_blocked":
        raise MaterialOperationRunnerError(
            "adoption_blocked",
            "durable quarantine records unresolved native ownership",
        )
    if resolution_bytes is None:
        raise MaterialOperationRunnerError(
            "adoption_blocked",
            "io-drain quarantine has no durable resolution",
        )
    resolution = _decode_quarantine_resolution_bytes(resolution_bytes)
    quarantine_hash = hashlib.sha256(quarantine_bytes).hexdigest()
    termination_hash = hashlib.sha256(termination_bytes).hexdigest()
    if (
        resolution["execution_id"] != descriptor.execution_id
        or resolution["attempt_id"] != descriptor.attempt_id
        or resolution["control_id"] != descriptor.control_id
        or resolution["job_instance_id"] != descriptor.job_instance_id
        or resolution["container_sha256"] != descriptor_hash
        or resolution["quarantine_sha256"] != quarantine_hash
        or resolution["termination_sha256"] != termination_hash
    ):
        raise MaterialOperationRunnerError(
            "adoption_quarantine_changed",
            "io-drain resolution does not bind to the adopted evidence tip",
        )
    return quarantine_hash, hashlib.sha256(resolution_bytes).hexdigest()


def _read_adopted_last_handle_state(
    *,
    control: ApprovedPhysicalDirectory,
    descriptor: MaterialOperationContainerDescriptor,
    descriptor_bytes: bytes,
    reconciliation: ReconciliationEvidence,
) -> tuple[str | None, str | None]:
    intent_bytes, closed_bytes = _read_last_handle_close_journal_pair(
        control=control,
        descriptor=descriptor,
        descriptor_bytes=descriptor_bytes,
    )
    if reconciliation.method == "last_handle_close_kill_on_close":
        if intent_bytes is None or closed_bytes is None:
            raise MaterialOperationRunnerError(
                "adoption_blocked",
                "last-handle reconciliation has no complete close journal pair",
            )
        return (
            hashlib.sha256(intent_bytes).hexdigest(),
            hashlib.sha256(closed_bytes).hexdigest(),
        )
    if closed_bytes is not None:
        raise MaterialOperationRunnerError(
            "adoption_reconciliation_changed",
            "last-handle closed proof conflicts with the reconciliation method",
        )
    # An unmatched intent can precede owner-exit inference.  It is retained as
    # evidence but cannot be promoted to a close acknowledgement.
    return (
        None if intent_bytes is None else hashlib.sha256(intent_bytes).hexdigest(),
        None,
    )


def _native_release_basis(
    evidence: ReconciliationEvidence,
) -> Literal[
    "never_created_dead_controller",
    "queried_empty_dead_controller",
    "owner_exit_dead_controller",
    "last_handle_closed",
]:
    mapping = {
        "never_created": "never_created_dead_controller",
        "queried_empty": "queried_empty_dead_controller",
        "owner_exit_kill_on_close": "owner_exit_dead_controller",
        "last_handle_close_kill_on_close": "last_handle_closed",
    }
    try:
        return mapping[evidence.method]  # type: ignore[return-value]
    except KeyError:
        raise MaterialOperationRunnerError(
            "adoption_reconciliation_invalid",
            "reconciliation has no approved native-release basis",
        ) from None


def _prove_adopted_processes_exited(
    descriptor: MaterialOperationContainerDescriptor,
) -> None:
    """Prove the old controller and any recorded root are no longer live."""

    try:
        controller_exited = _WINDOWS_RUNNER_OPS.wait_exact_process_exit(
            pid=descriptor.controller_pid,
            creation_time=descriptor.controller_creation_time,
            timeout_seconds=0.0,
        )
    except MaterialOperationRunnerError as error:
        raise MaterialOperationRunnerError(
            "adoption_blocked",
            "the previous controller exit could not be proven",
        ) from error
    if not controller_exited:
        raise MaterialOperationRunnerError(
            "adoption_blocked",
            "the previous controller is still alive",
        )
    if descriptor.root_pid is None:
        return
    if descriptor.root_creation_time is None:
        raise MaterialOperationRunnerError(
            "adoption_descriptor_changed",
            "durable root identity is incomplete",
        )
    try:
        root_exited = _WINDOWS_RUNNER_OPS.wait_exact_process_exit(
            pid=descriptor.root_pid,
            creation_time=descriptor.root_creation_time,
            timeout_seconds=10.0,
        )
    except MaterialOperationRunnerError as error:
        raise MaterialOperationRunnerError(
            "adoption_blocked",
            "the durable root exit could not be proven",
        ) from error
    if not root_exited:
        raise MaterialOperationRunnerError(
            "adoption_blocked",
            "the durable root process is still alive",
        )


def _match_current_descriptor(
    descriptor: MaterialOperationContainerDescriptor,
    record: _PreparedRecord,
) -> None:
    phase_index = _PHASE_INDEX[record.phase]
    expected = MaterialOperationContainerDescriptor(
        protocol_version=DESCRIPTOR_PROTOCOL_VERSION,
        phase=record.phase,
        previous_phase=record.previous_phase,
        previous_descriptor_sha256=record.previous_descriptor_sha256,
        execution_id=record.execution_id,
        attempt_id=record.attempt_id,
        control_id=record.control_id,
        job_instance_id=record.job_instance_id,
        operation=record.operation,
        controller_pid=record.controller.pid,
        controller_creation_time=record.controller.creation_time,
        operation_may_have_started=phase_index >= _OPERATION_STARTED_INDEX,
        request_sha256=(
            record.request_sha256 if phase_index >= _REQUEST_HASH_INDEX else None
        ),
        job_handle_inheritable=(
            False if phase_index >= _REQUEST_HASH_INDEX else None
        ),
        root_pid=(record.root_pid if phase_index >= _ROOT_IDENTITY_INDEX else None),
        root_creation_time=(
            record.root_creation_time
            if phase_index >= _ROOT_IDENTITY_INDEX
            else None
        ),
        job_active_processes_after_create=(
            1 if phase_index >= _ROOT_IDENTITY_INDEX else None
        ),
    )
    if descriptor != expected:
        raise MaterialOperationRunnerError(
            "descriptor_identity_mismatch",
            "descriptor does not match runner identities or hash-chain state",
        )


def _matches_unacknowledged_successor(
    descriptor: MaterialOperationContainerDescriptor,
    record: _PreparedRecord,
) -> bool:
    """Return whether disk holds the exact next descriptor whose receipt was lost."""
    target_index = _PHASE_INDEX[record.phase] + 1
    if target_index >= len(MATERIAL_LAUNCH_PHASES):
        return False
    target_phase = MATERIAL_LAUNCH_PHASES[target_index]
    expected = MaterialOperationContainerDescriptor(
        protocol_version=DESCRIPTOR_PROTOCOL_VERSION,
        phase=target_phase,
        previous_phase=record.phase,
        previous_descriptor_sha256=record.container_hash,
        execution_id=record.execution_id,
        attempt_id=record.attempt_id,
        control_id=record.control_id,
        job_instance_id=record.job_instance_id,
        operation=record.operation,
        controller_pid=record.controller.pid,
        controller_creation_time=record.controller.creation_time,
        operation_may_have_started=(
            _PHASE_INDEX[target_phase] >= _OPERATION_STARTED_INDEX
        ),
        request_sha256=(
            record.request_sha256
            if _PHASE_INDEX[target_phase] >= _REQUEST_HASH_INDEX
            else None
        ),
        job_handle_inheritable=(
            False if _PHASE_INDEX[target_phase] >= _REQUEST_HASH_INDEX else None
        ),
        root_pid=(
            record.root_pid
            if _PHASE_INDEX[target_phase] >= _ROOT_IDENTITY_INDEX
            else None
        ),
        root_creation_time=(
            record.root_creation_time
            if _PHASE_INDEX[target_phase] >= _ROOT_IDENTITY_INDEX
            else None
        ),
        job_active_processes_after_create=(
            1 if _PHASE_INDEX[target_phase] >= _ROOT_IDENTITY_INDEX else None
        ),
    )
    return descriptor == expected


def _request_hash_for_transition(
    *,
    record: _PreparedRecord,
    target_phase: MaterialLaunchPhase,
    supplied: str | None,
) -> str:
    if _PHASE_INDEX[target_phase] < _REQUEST_HASH_INDEX:
        if supplied is not None:
            raise MaterialOperationRunnerError(
                "request_hash_unexpected",
                "request hash is not legal before Job configuration",
            )
        raise MaterialOperationRunnerError(
            "descriptor_transition_invalid",
            "no transition before Job configuration is available",
        )
    if record.request_sha256 is None:
        _validate_sha256(
            supplied,
            code="request_hash_invalid",
            detail="job_configured requires a lowercase request SHA-256",
        )
        return supplied  # type: ignore[return-value]
    if supplied is not None:
        _validate_sha256(
            supplied,
            code="request_hash_invalid",
            detail="request hash must remain lowercase SHA-256",
        )
        if supplied != record.request_sha256:
            raise MaterialOperationRunnerError(
                "request_hash_mismatch",
                "request receipt hash cannot change after Job configuration",
            )
    return record.request_sha256


def _validate_run_timeout(value: object, field: str) -> float:
    normalized: float | None = None
    if not isinstance(value, bool) and isinstance(value, (int, float)):
        try:
            normalized = float(value)
        except (OverflowError, ValueError):
            normalized = None
    if normalized is None or not math.isfinite(normalized) or normalized <= 0:
        raise MaterialOperationRunnerError(
            "run_timeout_invalid",
            f"{field} must be a finite positive number",
        )
    return normalized


def _freeze_material_run_inputs(
    *,
    record: _PreparedRecord,
    request: MaterialOperationRequest,
    secrets: MaterialOperationSecrets,
    startup_deadline: float,
    operation_timeout_seconds: float,
) -> _FrozenMaterialRunInputs:
    if type(request) is not MaterialOperationRequest:
        raise MaterialOperationRunnerError(
            "run_input_type_invalid",
            "request must be an exact MaterialOperationRequest",
        )
    if type(secrets) is not MaterialOperationSecrets:
        raise MaterialOperationRunnerError(
            "run_input_type_invalid",
            "secrets must be exact typed MaterialOperationSecrets",
        )
    expected_identity = (
        record.execution_id,
        record.attempt_id,
        record.operation,
    )
    if (
        (request.execution_id, request.attempt_id, request.operation)
        != expected_identity
        or (secrets.execution_id, secrets.attempt_id, secrets.operation)
        != expected_identity
    ):
        raise MaterialOperationRunnerError(
            "run_identity_mismatch",
            "prepared, request, and secrets identities must match exactly",
        )
    try:
        request_bytes = encode_material_operation_request(request)
        secret_bytes = encode_material_operation_secrets(
            secrets,
            request=request,
        )
    except MaterialProtocolError:
        raise MaterialOperationRunnerError(
            "run_protocol_invalid",
            "request or secrets failed the frozen material protocol",
        ) from None
    if not 0 < len(secret_bytes) <= MAX_SECRET_BYTES:
        raise MaterialOperationRunnerError(
            "run_protocol_invalid",
            "secret envelope size is outside the frozen framing bound",
        )
    request_sha256 = hashlib.sha256(request_bytes).hexdigest()
    secret_frame = len(secret_bytes).to_bytes(4, "big") + secret_bytes
    return _FrozenMaterialRunInputs(
        request=request,
        request_bytes=request_bytes,
        request_sha256=request_sha256,
        secret_bytes=secret_bytes,
        secret_frame=secret_frame,
        startup_deadline=startup_deadline,
        operation_timeout_seconds=operation_timeout_seconds,
    )


def _validate_request_receipt(
    *,
    receipt: DurableWriteReceipt,
    record: _PreparedRecord,
    frozen: _FrozenMaterialRunInputs,
) -> None:
    expected_path = Path(record.directory.stable_volume_path) / REQUEST_FILENAME
    if (
        type(receipt) is not DurableWriteReceipt
        or receipt.path != expected_path
        or receipt.sha256 != frozen.request_sha256
        or receipt.byte_count != len(frozen.request_bytes)
    ):
        raise MaterialOperationRunnerError(
            "request_receipt_invalid",
            "durable request receipt does not match the frozen canonical bytes",
        )


def _root_for_transition(
    *,
    record: _PreparedRecord,
    target_phase: MaterialLaunchPhase,
    supplied_pid: int | None,
    supplied_creation_time: str | None,
) -> tuple[int | None, str | None]:
    target_index = _PHASE_INDEX[target_phase]
    if target_index < _ROOT_IDENTITY_INDEX:
        if supplied_pid is not None or supplied_creation_time is not None:
            raise MaterialOperationRunnerError(
                "root_identity_unexpected",
                "root identity is illegal before suspended creation is proven",
            )
        return None, None
    if record.root_pid is None or record.root_creation_time is None:
        if supplied_pid is None or supplied_creation_time is None:
            raise MaterialOperationRunnerError(
                "root_identity_required",
                "created_in_job_suspended requires exact root identity",
            )
        _validate_windows_pid(supplied_pid, "root_pid")
        _validate_identity_time(supplied_creation_time, "root_creation_time")
        return supplied_pid, supplied_creation_time
    if supplied_pid is not None or supplied_creation_time is not None:
        if (
            supplied_pid != record.root_pid
            or supplied_creation_time != record.root_creation_time
        ):
            raise MaterialOperationRunnerError(
                "root_identity_mismatch",
                "root PID and creation time cannot change after creation",
            )
    return record.root_pid, record.root_creation_time


def _validate_prepared_snapshot(
    record: _PreparedRecord,
    prepared: PreparedMaterialAttempt,
) -> None:
    if (
        prepared.execution_id != record.execution_id
        or prepared.attempt_id != record.attempt_id
        or prepared.control_id != record.control_id
        or prepared.job_instance_id != record.job_instance_id
        or prepared.container_hash != record.container_hash
        or prepared.control_directory is not record.control_directory
        or prepared.directory is not record.directory
        or len(prepared.pin_scope) != len(record.pin_scope)
        or any(
            current is not expected
            for current, expected in zip(prepared.pin_scope, record.pin_scope)
        )
    ):
        raise MaterialOperationRunnerError(
            "prepared_attempt_changed",
            "prepared attempt fields no longer match runner ownership",
        )


def _decode_uuid(value: object) -> UUID:
    if type(value) is not str:
        raise ValueError
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError
    return parsed


def _validate_uuid(value: object, field: str) -> None:
    try:
        _decode_uuid(value)
    except (ValueError, AttributeError, TypeError):
        raise MaterialOperationRunnerError(
            "identity_invalid",
            f"{field} must be a canonical UUID",
        ) from None


def _validate_operation(value: object) -> None:
    if type(value) is not str or value not in _OPERATIONS:
        raise MaterialOperationRunnerError(
            "operation_invalid",
            "operation is not part of the frozen material protocol",
        )


def _canonical_json_sha256(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_sha256(
    value: object,
    *,
    code: str,
    detail: str,
) -> None:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise MaterialOperationRunnerError(code, detail)


def _validate_windows_pid(value: object, field: str) -> None:
    if type(value) is not int or not 0 < value <= 2**32 - 1:
        raise MaterialOperationRunnerError(
            "descriptor_identity_invalid",
            f"{field} must be a positive Windows DWORD PID",
        )


def _validate_identity_time(value: object, field: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or value != value.strip()
        or any(ord(character) <= 0x1F for character in value)
    ):
        raise MaterialOperationRunnerError(
            "descriptor_identity_invalid",
            f"{field} must be a bounded timezone-aware timestamp",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MaterialOperationRunnerError(
            "descriptor_identity_invalid",
            f"{field} must be a timezone-aware ISO-8601 timestamp",
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MaterialOperationRunnerError(
            "descriptor_identity_invalid",
            f"{field} must include a timezone offset",
        )


def _close_pins(
    pins: tuple[ApprovedPhysicalDirectory, ...] | list[ApprovedPhysicalDirectory],
    *,
    audit_pin: ApprovedPhysicalDirectory | None = None,
) -> None:
    seen: set[int] = set()
    errors: list[BaseException] = []
    ordered: list[ApprovedPhysicalDirectory] = []
    for pin in reversed(pins):
        identity = id(pin)
        if identity in seen:
            continue
        seen.add(identity)
        if audit_pin is not None and pin is audit_pin:
            continue
        ordered.append(pin)
    for pin in ordered:
        try:
            pin.close()
        except BaseException as exc:
            errors.append(exc)
    if errors:
        raise BaseExceptionGroup("one or more directory pins failed to close", errors)
    if audit_pin is not None:
        audit_pin.close()


def _build_runner_retirement_attestation(
    *,
    record: _PreparedRecord,
    proof: MaterialRestartRetirementProof | MaterialRestartPublishedRetirementProof,
    persisted_bytes: bytes,
    release_basis: Literal[
        "in_process_close",
        "dead_controller_after_tombstone",
    ],
) -> MaterialRunnerRetirementAttestation:
    persisted = _decode_strict_bounded_json(
        persisted_bytes,
        max_bytes=MAX_RETIREMENT_BYTES,
        code="retirement_attestation_invalid",
        detail="persisted retirement tombstone is not strict bounded JSON",
    )
    if type(persisted) is not dict:
        raise MaterialOperationRunnerError(
            "retirement_attestation_invalid",
            "persisted retirement tombstone is not a JSON object",
        )
    if (
        not proof.checkpoint_sha256
        or persisted.get("state") != "retired"
        or persisted.get("execution_id") != record.execution_id
        or persisted.get("attempt_id") != record.attempt_id
        or persisted.get("control_id") != record.control_id
        or persisted.get("job_instance_id") != record.job_instance_id
        or persisted.get("operation") != record.operation
        or persisted.get("attempt_history_sha256")
        != proof.attempt_history_sha256
        or persisted.get("final_payload_sha256") != proof.final_payload_sha256
        or persisted.get("completion_revision") != proof.execution_revision
        or persisted.get("completion_result_sha256") != proof.result_sha256
        or persisted.get("completion_observed_at") != proof.observed_at
        or persisted.get("checkpoint_sha256") != proof.checkpoint_sha256
        or type(persisted.get("retired_at")) is not str
    ):
        raise MaterialOperationRunnerError(
            "retirement_attestation_invalid",
            "retirement tombstone does not bind the completion checkpoint",
        )
    return MaterialRunnerRetirementAttestation(
        status=proof.status,
        execution_id=proof.execution_id,
        operation=proof.operation,
        attempt_id=proof.attempt_id,
        control_id=record.control_id,
        job_instance_id=record.job_instance_id,
        completion_revision=proof.execution_revision,
        completion_payload_sha256=proof.final_payload_sha256,
        checkpoint_sha256=proof.checkpoint_sha256,
        attempt_history_sha256=proof.attempt_history_sha256,
        tombstone_sha256=hashlib.sha256(persisted_bytes).hexdigest(),
        retired_at=persisted["retired_at"],
        release_basis=release_basis,
    )


def _build_attempt_retirement_receipt(
    *,
    record: _PreparedRecord,
    proof: MaterialRestartRetirementProof,
    retirement: Mapping[str, object],
    persisted_bytes: bytes,
) -> MaterialAttemptRetirementReceipt:
    """Bind a durable tombstone read-back to the already verified proof."""

    # ``_persist_retirement_and_release`` may adopt an already durable
    # same-attempt tombstone after a no-clobber write acknowledgement is lost.
    # That adoption intentionally permits a different ``retired_at`` (the
    # existing tombstone owns that timestamp).  Always derive the receipt's
    # timestamp from the bytes whose hash we return, rather than from the
    # freshly generated in-memory mapping used for the attempted write.
    persisted = _decode_strict_bounded_json(
        persisted_bytes,
        max_bytes=MAX_RETIREMENT_BYTES,
        code="retirement_receipt_invalid",
        detail="persisted retirement tombstone is not strict bounded JSON",
    )
    if type(persisted) is not dict:
        raise MaterialOperationRunnerError(
            "retirement_receipt_invalid",
            "persisted retirement tombstone is not a JSON object",
        )

    tombstone_sha256 = hashlib.sha256(persisted_bytes).hexdigest()
    if persisted.get("state") != "retired":
        raise MaterialOperationRunnerError(
            "retirement_receipt_invalid",
            "retirement tombstone is not terminal",
        )
    if (
        persisted.get("execution_id") != record.execution_id
        or persisted.get("attempt_id") != record.attempt_id
        or persisted.get("control_id") != record.control_id
        or persisted.get("job_instance_id") != record.job_instance_id
        or persisted.get("operation") != record.operation
        or persisted.get("attempt_history_sha256") != proof.attempt_history_sha256
        or persisted.get("final_payload_sha256") != proof.final_payload_sha256
        or persisted.get("completion_revision") != proof.execution_revision
        or persisted.get("completion_result_sha256") != proof.result_sha256
        or persisted.get("completion_observed_at") != proof.observed_at
    ):
        raise MaterialOperationRunnerError(
            "retirement_receipt_invalid",
            "retirement tombstone does not bind the terminal completion proof",
        )
    retired_at = persisted.get("retired_at")
    if type(retired_at) is not str:
        raise MaterialOperationRunnerError(
            "retirement_receipt_invalid",
            "retirement tombstone has no retired_at timestamp",
        )
    return MaterialAttemptRetirementReceipt(
        status=proof.status,
        execution_id=proof.execution_id,
        operation=proof.operation,
        attempt_id=proof.attempt_id,
        control_id=record.control_id,
        job_instance_id=record.job_instance_id,
        execution_revision=proof.execution_revision,
        final_payload_sha256=proof.final_payload_sha256,
        attempt_history_sha256=proof.attempt_history_sha256,
        result_sha256=proof.result_sha256,
        observed_at=proof.observed_at,
        retired_at=retired_at,
        tombstone_sha256=tombstone_sha256,
        checkpoint_sha256=proof.checkpoint_sha256,
    )


def _normcase(value: str) -> str:
    return ntpath.normcase(value.rstrip("\\"))


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "AdoptedMaterialAttempt",
    "CONTAINER_FILENAME",
    "DESCRIPTOR_PROTOCOL_VERSION",
    "MATERIAL_LAUNCH_PHASES",
    "MAX_DESCRIPTOR_BYTES",
    "MaterialLaunchPhase",
    "MaterialOperationContainerDescriptor",
    "MaterialOperationRunnerError",
    "MaterialAttemptRetirementReceipt",
    "MaterialRestartOwnershipGuard",
    "MaterialRestartPublishedRetirementProof",
    "MaterialRestartRetirementProof",
    "MaterialRunnerClaimBinding",
    "PreparedMaterialAttempt",
    "ProcessMaterialOperationRunner",
    "RETIRED_FILENAME",
    "ReconciliationEvidence",
    "TERMINATION_FILENAME",
    "TERMINATION_OUTCOME_FILENAME",
    "QUARANTINE_FILENAME",
    "QUARANTINE_RESOLUTION_FILENAME",
    "decode_material_container_descriptor",
    "encode_material_container_descriptor",
]
