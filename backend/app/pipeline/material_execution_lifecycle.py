"""Same-mutex preparation and fresh-session completion for material attempts.

This module is the database/filesystem ownership adapter for the typed material
coordinator.  It does not import or call the legacy ``AutoMaterialTaskExecutor``
and it does not switch a public route by itself.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Literal, Mapping, Protocol
from uuid import UUID

from app.pipeline.material_artifact_publisher import CanonicalMaterialPublication
from app.pipeline.material_candidate_contract import (
    CandidateContractError,
    CandidateSet,
    replace_candidates,
)
from app.pipeline.material_execution_claims import (
    MaterialExecutionClaimConflict,
    MaterialExecutionClaimError,
    MaterialExecutionClaimUnknown,
)
from app.pipeline.material_execution_coordinator import (
    MaterialCompletionReceipt,
    MaterialExecutionCoordinatorUnknown,
    PreparedMaterialClaim,
)
from app.pipeline.material_execution_service import (
    MaterialCandidateBatchResult,
    MaterialExecutionResult,
)
from app.pipeline.material_operation_protocol import (
    MaterialOperationKind,
    MaterialOperationOutcome,
    encode_material_worker_result,
)
from app.pipeline.material_operation_runner import (
    AdoptedMaterialAttempt,
    MaterialAttemptRetirementReceipt,
    MaterialRestartOwnershipGuard,
    MaterialRestartPublishedRetirementProof,
    MaterialRestartRetirementProof,
    MaterialRunnerClaimBinding,
    PreparedMaterialAttempt,
)
from app.pipeline.material_runner_retirement import (
    MaterialRunnerRetirementAcknowledgement,
    MaterialRunnerRetirementAttestation,
    MaterialRunnerRetirementCheckpoint,
    install_pending_runner_retirement,
    json_payload_sha256,
    pending_runner_retirement_from_payload,
)


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
_WAITING_STATUSES = frozenset(
    {
        "等待",
        "待执行",
        "未开始",
        "待处理",
        "待搜索",
        "pending",
        "idle",
        "queued",
        "ready",
    }
)
_ACTIVE_OWNER_FIELDS = (
    "active_material_task_id",
    "active_execution_id",
    "active_attempt_id",
    "active_attempt_kind",
    "active_control_id",
    "active_job_instance_id",
)
_MAX_ATTEMPT_HISTORY = 50


@dataclass(frozen=True, slots=True)
class MaterialAttemptRotationReceipt:
    """Proof for one terminal-attempt -> next-attempt whole-blob CAS.

    The receipt is intentionally not a completion receipt: the execution is
    still active and owns the step after this transition.  It binds both
    attempt identities so a caller cannot accidentally use the old terminal
    attempt as the new worker owner.
    """

    status: Literal["failed", "terminated"]
    execution_id: str
    terminal_operation: MaterialOperationKind
    terminal_attempt_id: str
    next_operation: MaterialOperationKind
    next_attempt_id: str
    execution_revision: int
    attempt_history_sha256: str
    final_payload_sha256: str
    observed_at: str

    def __post_init__(self) -> None:
        if self.status not in {"failed", "terminated"}:
            raise ValueError("rotation status must be failed or terminated")
        _validate_uuid(self.execution_id, "execution_id")
        _validate_uuid(self.terminal_attempt_id, "terminal_attempt_id")
        _validate_uuid(self.next_attempt_id, "next_attempt_id")
        if self.terminal_attempt_id == self.next_attempt_id:
            raise ValueError("rotation requires distinct attempt identities")
        if self.terminal_operation not in _OPERATIONS:
            raise ValueError("terminal_operation is not allowed")
        if self.next_operation not in _OPERATIONS:
            raise ValueError("next_operation is not allowed")
        if type(self.execution_revision) is not int or self.execution_revision <= 0:
            raise ValueError("execution_revision must be a positive integer")
        _validate_sha256(self.attempt_history_sha256, "attempt history")
        _validate_sha256(self.final_payload_sha256, "final payload")
        if type(self.observed_at) is not str or not self.observed_at:
            raise ValueError("observed_at is required")

    @property
    def operation(self) -> MaterialOperationKind:
        """Compatibility view of the terminal operation."""

        return self.terminal_operation

    @property
    def attempt_id(self) -> str:
        """Compatibility view of the terminal attempt identity."""

        return self.terminal_attempt_id


class MaterialPreparationStore(Protocol):
    def acquire_claim_mutex(self, *, timeout_ms: int = 0) -> Any:
        ...

    def claim_lease_with_owned_mutex(self, *, mutex: Any, **values: Any) -> Any:
        ...


class ClaimLeasePublicationDatabase:
    """Publisher DB surface backed only by the already-owned claim lease.

    It never acquires a second named mutex.  ``assert_owned`` performs the
    lease's fresh-session equality proof, and ``compare_and_swap`` advances the
    same immutable lease token that final completion will later consume.
    """

    def __init__(self, lease: Any) -> None:
        if not callable(getattr(lease, "assert_owned", None)) or not callable(
            getattr(lease, "compare_and_swap", None)
        ):
            raise TypeError("lease does not expose owned fresh-session CAS")
        if not hasattr(lease, "expected_payload"):
            raise TypeError("lease does not expose an expected payload token")
        self.lease = lease

    def fresh_read(self) -> dict[str, Any]:
        self.lease.assert_owned()
        return _json_clone(self.lease.expected_payload)

    def compare_and_swap(
        self,
        expected: Mapping[str, Any],
        replacement: Mapping[str, Any],
    ) -> bool:
        expected_payload = _json_clone(expected)
        replacement_payload = _json_clone(replacement)
        self.lease.assert_owned()
        if expected_payload != _json_clone(self.lease.expected_payload):
            return False
        try:
            self.lease.compare_and_swap(replacement_payload)
        except MaterialExecutionClaimConflict:
            return False
        return True


@dataclass(frozen=True, slots=True)
class MaterialPreparedClaimRecovery:
    """In-process ownership retained after an unknowable claim edge.

    There is intentionally no generic ``release`` method.  The database and
    durable attempt must first be reconciled; blindly releasing here could let
    a second worker start against an ambiguously committed claim.
    """

    prepared: PreparedMaterialAttempt
    mutex: Any
    lease: Any | None = None
    phase: Literal["claim_unknown", "binding_unknown", "cleanup_unknown"] = (
        "claim_unknown"
    )

    @property
    def ownership_retained(self) -> bool:
        owner = self.lease if self.lease is not None else self.mutex
        assert_owned = getattr(owner, "assert_owned", None)
        if not callable(assert_owned):
            return False
        try:
            assert_owned()
        except Exception:
            return False
        return True


class MaterialPrepareAndClaimUnknown(MaterialExecutionCoordinatorUnknown):
    """Preparation produced evidence whose database claim is not yet decidable."""

    def __init__(self, message: str, *, recovery: MaterialPreparedClaimRecovery):
        super().__init__(message)
        self.recovery = recovery


class MaterialExecutionClaimPreparer:
    """Create an attempt and claim it without releasing the step mutex."""

    def __init__(self, *, store: MaterialPreparationStore, runner: Any) -> None:
        if not callable(getattr(store, "acquire_claim_mutex", None)) or not callable(
            getattr(store, "claim_lease_with_owned_mutex", None)
        ):
            raise TypeError("store does not expose the same-mutex claim surface")
        if not callable(getattr(runner, "prepare_attempt", None)) or not callable(
            getattr(runner, "retire_unstarted_attempt", None)
        ) or not callable(getattr(runner, "bind_claim", None)):
            raise TypeError("runner does not expose preparation and retirement")
        self.store = store
        self.runner = runner

    def prepare_and_claim(
        self,
        *,
        material_task_id: str,
        execution_id: str,
        operation: str,
        timeout_ms: int = 0,
    ) -> PreparedMaterialClaim:
        if operation not in _OPERATIONS:
            raise ValueError("operation is not an allowed material operation")
        mutex = self.store.acquire_claim_mutex(timeout_ms=timeout_ms)
        return self.prepare_and_claim_with_owned_mutex(
            mutex=mutex,
            material_task_id=material_task_id,
            execution_id=execution_id,
            operation=operation,
        )

    def prepare_and_claim_with_owned_mutex(
        self,
        *,
        mutex: Any,
        material_task_id: str,
        execution_id: str,
        operation: str,
    ) -> PreparedMaterialClaim:
        """Prepare and claim using a mutex already owned by this thread.

        The caller must keep the same thread alive when this method raises
        ``MaterialPrepareAndClaimUnknown``; its recovery object deliberately
        retains the handle and prepared roots for the owner lane.
        """
        if operation not in _OPERATIONS:
            raise ValueError("operation is not an allowed material operation")
        if mutex is None:
            raise TypeError("mutex is required")
        assert_owned = getattr(mutex, "assert_owned", None)
        if not callable(assert_owned):
            raise TypeError("mutex must prove current-thread ownership")
        try:
            assert_owned()
        except BaseException as exc:
            # Never create filesystem evidence before this exact owner thread
            # proves the material-step serialization boundary.  Do not try to
            # release a handle whose owner could be another Windows thread.
            raise MaterialExecutionCoordinatorUnknown(
                "material step mutex is not owned by the current thread"
            ) from exc
        prepared: PreparedMaterialAttempt | None = None
        try:
            prepared = self.runner.prepare_attempt(
                execution_id=execution_id,
                operation=operation,
            )
        except BaseException:
            _release_preclaim_mutex_or_unknown(mutex, prepared=None)
            raise

        try:
            lease = self.store.claim_lease_with_owned_mutex(
                mutex=mutex,
                material_task_id=material_task_id,
                execution_id=execution_id,
                attempt_id=prepared.attempt_id,
                attempt_kind=operation,
                control_id=prepared.control_id,
                job_instance_id=prepared.job_instance_id,
            )
        except MaterialExecutionClaimUnknown as exc:
            recovery = MaterialPreparedClaimRecovery(
                prepared=prepared,
                mutex=mutex,
                phase="claim_unknown",
            )
            raise MaterialPrepareAndClaimUnknown(
                "material claim acknowledgement is unknown; ownership was retained",
                recovery=recovery,
            ) from exc
        except MaterialExecutionClaimError:
            self._retire_known_unclaimed(prepared, mutex)
            raise
        except BaseException as exc:
            recovery = MaterialPreparedClaimRecovery(
                prepared=prepared,
                mutex=mutex,
                phase="claim_unknown",
            )
            raise MaterialPrepareAndClaimUnknown(
                "unexpected material claim failure; ownership was retained",
                recovery=recovery,
            ) from exc

        try:
            payload = lease.expected_payload
            binding = MaterialRunnerClaimBinding(
                material_task_id=material_task_id,
                execution_id=prepared.execution_id,
                operation=operation,
                attempt_id=prepared.attempt_id,
                control_id=prepared.control_id,
                job_instance_id=prepared.job_instance_id,
                execution_revision=lease.expected_revision,
                payload_sha256=_json_sha256(payload),
            )
            # Bind before returning so a caller cannot accidentally retire the
            # prepared roots through the pre-claim seam after the DB CAS has
            # already succeeded. ``run_claimed`` accepts this exact pre-bind.
            self.runner.bind_claim(prepared, binding)
            return PreparedMaterialClaim(
                prepared=prepared,
                binding=binding,
                lease=lease,
            )
        except BaseException as exc:
            recovery = MaterialPreparedClaimRecovery(
                prepared=prepared,
                mutex=mutex,
                lease=lease,
                phase="binding_unknown",
            )
            raise MaterialPrepareAndClaimUnknown(
                "claimed attempt could not form its typed binding; ownership was retained",
                recovery=recovery,
            ) from exc

    def prepare_next_attempt_with_owned_mutex(
        self,
        *,
        mutex: Any,
        execution_id: str,
        operation: str,
    ) -> PreparedMaterialAttempt:
        """Prepare an unclaimed next-source attempt under the live owner lane.

        This is intentionally weaker than ``prepare_and_claim``: the caller
        must invoke it only after the previous runner has emitted a typed
        retirement receipt, and must install the returned identity through
        ``rotate_terminal_attempt`` before binding or launching it.  No DB
        owner is installed by this method.
        """

        if operation not in _OPERATIONS:
            raise ValueError("operation is not an allowed material operation")
        if mutex is None or not callable(getattr(mutex, "assert_owned", None)):
            raise TypeError("mutex must prove current-thread ownership")
        try:
            mutex.assert_owned()
        except BaseException as exc:
            raise MaterialExecutionCoordinatorUnknown(
                "material step mutex is not owned by the current thread"
            ) from exc
        prepared = self.runner.prepare_attempt(
            execution_id=execution_id,
            operation=operation,
        )
        if type(prepared) is not PreparedMaterialAttempt:
            raise MaterialExecutionCoordinatorUnknown(
                "runner did not return a typed next prepared attempt"
            )
        if prepared.execution_id != execution_id:
            raise MaterialExecutionCoordinatorUnknown(
                "next prepared attempt changed the execution identity"
            )
        return prepared

    def bind_rotated_attempt(
        self,
        *,
        claim: PreparedMaterialClaim,
        prepared: PreparedMaterialAttempt,
        operation: str,
    ) -> PreparedMaterialClaim:
        """Bind a prepared next-source attempt after the rotation CAS."""

        if type(prepared) is not PreparedMaterialAttempt:
            raise TypeError("prepared must be PreparedMaterialAttempt")
        if operation not in _OPERATIONS:
            raise ValueError("operation is not an allowed material operation")
        try:
            claim.lease.assert_owned()
        except BaseException as exc:
            raise MaterialExecutionCoordinatorUnknown(
                "claim lease ownership could not be proven for rotated binding"
            ) from exc
        if (
            prepared.execution_id != claim.binding.execution_id
            or claim.lease.active_attempt_id != prepared.attempt_id
            or claim.lease.active_attempt_kind != operation
            or claim.lease.active_control_id != prepared.control_id
            or claim.lease.active_job_instance_id != prepared.job_instance_id
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "rotated prepared attempt does not match the installed claim owner"
            )
        payload = claim.lease.expected_payload
        binding = MaterialRunnerClaimBinding(
            material_task_id=claim.binding.material_task_id,
            execution_id=prepared.execution_id,
            operation=operation,  # type: ignore[arg-type]
            attempt_id=prepared.attempt_id,
            control_id=prepared.control_id,
            job_instance_id=prepared.job_instance_id,
            execution_revision=claim.lease.expected_revision,
            payload_sha256=_json_sha256(payload),
        )
        self.runner.bind_claim(prepared, binding)
        return PreparedMaterialClaim(
            prepared=prepared,
            binding=binding,
            lease=claim.lease,
        )

    def _retire_known_unclaimed(
        self, prepared: PreparedMaterialAttempt, mutex: Any
    ) -> None:
        try:
            self.runner.retire_unstarted_attempt(prepared)
            _release_preclaim_mutex_or_unknown(mutex, prepared=prepared)
        except MaterialPrepareAndClaimUnknown:
            raise
        except BaseException as exc:
            recovery = MaterialPreparedClaimRecovery(
                prepared=prepared,
                mutex=mutex,
                phase="cleanup_unknown",
            )
            raise MaterialPrepareAndClaimUnknown(
                "known unclaimed attempt could not be retired safely",
                recovery=recovery,
            ) from exc


class SqlAlchemyMaterialLifecycleAdapter:
    """Complete a claim through its independent fresh-session lease CAS."""

    def complete_success(
        self,
        claim: PreparedMaterialClaim,
        *,
        outcome: MaterialOperationOutcome,
        publication: CanonicalMaterialPublication,
        _recovered: bool = False,
    ) -> MaterialCompletionReceipt:
        if outcome.status != "succeeded" or outcome.result is None:
            raise MaterialExecutionCoordinatorUnknown(
                "success completion requires a successful worker outcome"
            )
        if (
            outcome.result.execution_id != claim.binding.execution_id
            or outcome.result.attempt_id != claim.binding.attempt_id
            or outcome.result.operation != claim.binding.operation
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "successful worker outcome does not belong to the active claim"
            )
        if getattr(publication, "phase", None) != "committed" or not bool(
            getattr(publication, "ownership_retained", False)
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "success completion requires retained committed publication ownership"
            )
        journal_sha256 = getattr(publication, "committed_journal_sha256", None)
        _validate_sha256(journal_sha256, "publication journal")
        published_result = getattr(publication, "result", None)
        if published_result is None or getattr(published_result, "success", None) is not True:
            raise MaterialExecutionCoordinatorUnknown(
                "committed publication has no successful material result"
            )
        publication_request = getattr(publication, "request", None)
        if (
            getattr(publication_request, "execution_id", None)
            != claim.binding.execution_id
            or getattr(publication_request, "attempt_id", None)
            != claim.binding.attempt_id
            or getattr(publication_request, "operation", None)
            != outcome.result.operation
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "committed publication does not belong to the worker outcome"
            )

        expected = _owned_payload(claim)
        _require_transition_gate(
            expected,
            claim,
            publication_phase="db_committed",
            execution_state="publishing",
            revision_offset=0 if _recovered else 1,
            label="success completion",
        )
        _validate_completion_intent_reference(
            expected,
            claim=claim,
            outcome=outcome,
            publication=publication,
            recovered=_recovered,
        )
        journal_path = expected.get("publication_journal")
        if (
            type(journal_path) is not str
            or not journal_path
            or not journal_path.replace("\\", "/").casefold().endswith(
                "/publication-journal.json"
            )
            or not journal_path.replace("\\", "/").casefold().startswith("d:/")
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "success completion has no D-drive publication journal reference"
            )
        attempt_dir = getattr(publication, "attempt_dir", None)
        attempt_path = getattr(attempt_dir, "path", None)
        if attempt_path is not None and journal_path != str(
            attempt_path / "publication-journal.json"
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "publication journal reference does not belong to the retained attempt"
            )
        observed_at = outcome.termination.observed_at
        result_sha256 = hashlib.sha256(
            encode_material_worker_result(outcome.result)
        ).hexdigest()
        entry = _attempt_history_entry(
            claim,
            status="succeeded",
            observed_at=observed_at,
            result_sha256=result_sha256,
            publication_journal_sha256=journal_sha256,
            reason_code="",
            reason="",
        )
        replacement = copy.deepcopy(expected)
        task = _owned_task(replacement, claim.binding.material_task_id)
        if claim.binding.operation == "stock_candidates":
            _apply_stock_candidate_batch(
                replacement,
                task,
                published_result,
                claim=claim,
                observed_at=observed_at,
            )
        else:
            task.update(
                {
                    "status": "已完成",
                    "material_path": str(
                        getattr(published_result, "material_path", "")
                    ),
                    "material_type": str(
                        getattr(published_result, "material_type", "")
                    ),
                    "source_type": str(
                        getattr(published_result, "source_type", "")
                    ),
                    "evidence_kind": str(
                        getattr(published_result, "evidence_kind", "")
                    ),
                    "source_url": str(
                        getattr(published_result, "source_url", "")
                    ),
                    "is_locally_generated": bool(
                        getattr(published_result, "is_locally_generated", False)
                    ),
                    "notes": str(getattr(published_result, "reason", "")),
                    "reject_reason": "",
                }
            )
            rights_evidence = getattr(published_result, "rights_evidence", None)
            if rights_evidence is None:
                task.pop("rights_evidence", None)
            else:
                task["rights_evidence"] = copy.deepcopy(rights_evidence)
            _append_task_status(task, "已完成")
        _append_attempt_history(replacement, entry)
        _finish_payload(
            replacement,
            expected=expected,
            status="succeeded",
            publication_phase="committed",
            observed_at=observed_at,
        )
        retirement_checkpoint = MaterialRunnerRetirementCheckpoint.create(
            material_task_id=claim.binding.material_task_id,
            execution_id=claim.binding.execution_id,
            attempt_id=claim.binding.attempt_id,
            operation=claim.binding.operation,
            control_id=claim.binding.control_id,
            job_instance_id=claim.binding.job_instance_id,
            completion_status="succeeded",
            completion_revision=_required_revision(expected) + 1,
            completion_observed_at=observed_at,
            reference_revision=_required_revision(expected),
            reference_payload_sha256=_json_sha256(expected),
            attempt_history_sha256=entry["entry_sha256"],
            result_sha256=result_sha256,
            publication_journal_sha256=journal_sha256,
            publication_intent_sha256=expected.get("publication_intent_sha256"),
            publication_result_sha256=expected.get("publication_result_sha256"),
            publication_worker_result_sha256=expected.get(
                "publication_worker_result_sha256"
            ),
            publication_reference_base_revision=expected.get(
                "publication_reference_base_revision"
            ),
            publication_reference_base_payload_sha256=expected.get(
                "publication_reference_base_payload_sha256"
            ),
        )
        install_pending_runner_retirement(replacement, retirement_checkpoint)
        claim.lease.compare_and_swap(replacement)
        final = claim.lease.expected_payload
        return MaterialCompletionReceipt(
            status="succeeded",
            execution_id=claim.binding.execution_id,
            operation=claim.binding.operation,
            attempt_id=claim.binding.attempt_id,
            execution_revision=_required_revision(final),
            final_payload_sha256=_json_sha256(final),
            publication_journal_sha256=journal_sha256,
            attempt_history_sha256=entry["entry_sha256"],
            result_sha256=result_sha256,
            publication_intent_sha256=final.get("publication_intent_sha256"),
            publication_result_sha256=final.get("publication_result_sha256"),
            publication_worker_result_sha256=final.get(
                "publication_worker_result_sha256"
            ),
            publication_reference_base_revision=final.get(
                "publication_reference_base_revision"
            ),
            publication_reference_base_payload_sha256=final.get(
                "publication_reference_base_payload_sha256"
            ),
            runner_retirement_checkpoint_sha256=(
                retirement_checkpoint.checkpoint_sha256
            ),
            observed_at=observed_at,
        )

    def complete_recovered_success(
        self,
        claim: PreparedMaterialClaim,
        *,
        outcome: MaterialOperationOutcome,
        publication: CanonicalMaterialPublication,
    ) -> MaterialCompletionReceipt:
        """Complete an adopted ``r+1`` publishing lease with one ``r+2`` CAS."""

        return self.complete_success(
            claim,
            outcome=outcome,
            publication=publication,
            _recovered=True,
        )

    def complete_terminal(
        self,
        claim: PreparedMaterialClaim,
        *,
        outcome: MaterialOperationOutcome,
    ) -> MaterialCompletionReceipt:
        if outcome.status not in {"failed", "terminated"}:
            raise MaterialExecutionCoordinatorUnknown(
                "terminal completion requires failed or terminated outcome"
            )
        if outcome.result is not None and (
            outcome.result.execution_id != claim.binding.execution_id
            or outcome.result.attempt_id != claim.binding.attempt_id
            or outcome.result.operation != claim.binding.operation
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "terminal worker outcome does not belong to the active claim"
            )
        expected = _owned_payload(claim)
        _require_transition_gate(
            expected,
            claim,
            publication_phase="preparing",
            execution_state="claimed",
            revision_offset=0,
            label="terminal completion",
        )
        observed_at = outcome.termination.observed_at
        if outcome.result is None:
            result_sha256 = None
            reason_code = "worker_terminated"
            reason = "执行进程已安全终止，等待后续重试。"
        else:
            result_sha256 = hashlib.sha256(
                encode_material_worker_result(outcome.result)
            ).hexdigest()
            reason_code = outcome.result.reason_code
            reason = outcome.result.reason
        entry = _attempt_history_entry(
            claim,
            status=outcome.status,
            observed_at=observed_at,
            result_sha256=result_sha256,
            publication_journal_sha256=None,
            reason_code=reason_code,
            reason=reason,
        )
        replacement = copy.deepcopy(expected)
        task = _owned_task(replacement, claim.binding.material_task_id)
        task.update(
            {
                "status": "失败",
                "material_path": "",
                "notes": "",
                "reject_reason": reason,
            }
        )
        _append_task_status(task, "失败")
        _append_attempt_history(replacement, entry)
        replacement["publication_journal"] = None
        _finish_payload(
            replacement,
            expected=expected,
            status=outcome.status,
            publication_phase="rolled_back",
            observed_at=observed_at,
        )
        retirement_checkpoint = MaterialRunnerRetirementCheckpoint.create(
            material_task_id=claim.binding.material_task_id,
            execution_id=claim.binding.execution_id,
            attempt_id=claim.binding.attempt_id,
            operation=claim.binding.operation,
            control_id=claim.binding.control_id,
            job_instance_id=claim.binding.job_instance_id,
            completion_status=outcome.status,
            completion_revision=_required_revision(expected) + 1,
            completion_observed_at=observed_at,
            reference_revision=_required_revision(expected),
            reference_payload_sha256=_json_sha256(expected),
            attempt_history_sha256=entry["entry_sha256"],
            result_sha256=result_sha256,
        )
        install_pending_runner_retirement(replacement, retirement_checkpoint)
        claim.lease.compare_and_swap(replacement)
        final = claim.lease.expected_payload
        return MaterialCompletionReceipt(
            status=outcome.status,
            execution_id=claim.binding.execution_id,
            operation=claim.binding.operation,
            attempt_id=claim.binding.attempt_id,
            execution_revision=_required_revision(final),
            final_payload_sha256=_json_sha256(final),
            attempt_history_sha256=entry["entry_sha256"],
            result_sha256=result_sha256,
            runner_retirement_checkpoint_sha256=(
                retirement_checkpoint.checkpoint_sha256
            ),
            observed_at=observed_at,
        )

    def acknowledge_runner_retirement(
        self,
        claim: PreparedMaterialClaim,
        *,
        completion: MaterialCompletionReceipt,
        attestation: MaterialRunnerRetirementAttestation,
    ) -> MaterialRunnerRetirementAcknowledgement:
        """Commit the only transition that makes a completed step claimable."""

        if type(claim) is not PreparedMaterialClaim:
            raise TypeError("claim must be PreparedMaterialClaim")
        if type(completion) is not MaterialCompletionReceipt:
            raise TypeError("completion must be MaterialCompletionReceipt")
        if type(attestation) is not MaterialRunnerRetirementAttestation:
            raise TypeError("attestation must be MaterialRunnerRetirementAttestation")
        return self.acknowledge_pending_runner_retirement(
            claim.lease,
            completion=completion,
            attestation=attestation,
        )

    def acknowledge_pending_runner_retirement(
        self,
        lease: Any,
        *,
        completion: MaterialCompletionReceipt,
        attestation: MaterialRunnerRetirementAttestation,
    ) -> MaterialRunnerRetirementAcknowledgement:
        """Ack an in-process or restart-adopted exact pending lease."""

        if not callable(getattr(lease, "assert_owned", None)) or not callable(
            getattr(lease, "compare_and_swap", None)
        ):
            raise TypeError("lease must expose owned whole-blob CAS")
        if type(completion) is not MaterialCompletionReceipt:
            raise TypeError("completion must be MaterialCompletionReceipt")
        if type(attestation) is not MaterialRunnerRetirementAttestation:
            raise TypeError("attestation must be MaterialRunnerRetirementAttestation")
        lease.assert_owned()
        current = lease.expected_payload
        checkpoint = pending_runner_retirement_from_payload(current)
        completion_payload_sha256 = json_payload_sha256(current)
        if (
            completion.status != checkpoint.completion_status
            or completion.execution_id != checkpoint.execution_id
            or completion.operation != checkpoint.operation
            or completion.attempt_id != checkpoint.attempt_id
            or completion.execution_revision != checkpoint.completion_revision
            or completion.final_payload_sha256 != completion_payload_sha256
            or completion.attempt_history_sha256
            != checkpoint.attempt_history_sha256
            or completion.runner_retirement_checkpoint_sha256
            != checkpoint.checkpoint_sha256
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "completion receipt does not match the pending runner retirement"
            )
        if (
            attestation.status != checkpoint.completion_status
            or attestation.execution_id != checkpoint.execution_id
            or attestation.operation != checkpoint.operation
            or attestation.attempt_id != checkpoint.attempt_id
            or attestation.control_id != checkpoint.control_id
            or attestation.job_instance_id != checkpoint.job_instance_id
            or attestation.completion_revision != checkpoint.completion_revision
            or attestation.completion_payload_sha256 != completion_payload_sha256
            or attestation.checkpoint_sha256 != checkpoint.checkpoint_sha256
            or attestation.attempt_history_sha256
            != checkpoint.attempt_history_sha256
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "runner attestation does not match the pending completion"
            )

        acknowledged_at = _utc_now()
        ack_revision = checkpoint.completion_revision + 1
        replacement = copy.deepcopy(current)
        replacement["execution_revision"] = ack_revision
        replacement["runner_retirement"] = {
            "format": "material-runner-retirement-v1",
            "state": "attested",
            "checkpoint": checkpoint.to_json(),
            "attestation": {
                "completion_payload_sha256": completion_payload_sha256,
                "tombstone_sha256": attestation.tombstone_sha256,
                "retired_at": attestation.retired_at,
                "release_basis": attestation.release_basis,
                "ack_revision": ack_revision,
                "acknowledged_at": acknowledged_at,
            },
        }
        lease.compare_and_swap(replacement)
        if lease.expected_payload != replacement:
            raise MaterialExecutionCoordinatorUnknown(
                "runner retirement ack CAS could not be freshly verified"
            )
        return MaterialRunnerRetirementAcknowledgement(
            completion_revision=checkpoint.completion_revision,
            ack_revision=ack_revision,
            completion_payload_sha256=completion_payload_sha256,
            checkpoint_sha256=checkpoint.checkpoint_sha256,
            tombstone_sha256=attestation.tombstone_sha256,
            acknowledged_at=acknowledged_at,
        )

    def record_terminal_attempt_for_fallback(
        self,
        claim: PreparedMaterialClaim,
        *,
        outcome: MaterialOperationOutcome,
    ) -> MaterialCompletionReceipt:
        """CAS terminal history and clear the active owner for retirement.

        This is deliberately a *first* phase.  It keeps ``execution_id`` and
        marks ``running/rolled_back + attempt_transition=retiring`` so neither
        the legacy claim helper nor a second execution can start while the
        runner still owns the old attempt's filesystem/process pins.
        """

        if type(outcome) is not MaterialOperationOutcome:
            raise TypeError("outcome must be MaterialOperationOutcome")
        if outcome.status not in {"failed", "terminated"}:
            raise MaterialExecutionCoordinatorUnknown(
                "fallback terminal recording requires failed or terminated outcome"
            )
        if outcome.result is not None and (
            outcome.result.execution_id != claim.binding.execution_id
            or outcome.result.attempt_id != claim.binding.attempt_id
            or outcome.result.operation != claim.binding.operation
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "terminal worker outcome does not belong to the active claim"
            )
        expected = _owned_payload(claim)
        _require_transition_gate(
            expected,
            claim,
            publication_phase="preparing",
            execution_state="claimed",
            revision_offset=0,
            label="fallback terminal recording",
        )
        observed_at = outcome.termination.observed_at
        if outcome.result is None:
            result_sha256 = None
            reason_code = "worker_terminated"
            reason = "执行进程已安全终止，等待后续重试。"
        else:
            result_sha256 = hashlib.sha256(
                encode_material_worker_result(outcome.result)
            ).hexdigest()
            reason_code = outcome.result.reason_code
            reason = outcome.result.reason
        entry = _attempt_history_entry(
            claim,
            status=outcome.status,
            observed_at=observed_at,
            result_sha256=result_sha256,
            publication_journal_sha256=None,
            reason_code=reason_code,
            reason=reason,
        )
        replacement = copy.deepcopy(expected)
        task = _owned_task(replacement, claim.binding.material_task_id)
        task.update(
            {
                "status": "失败",
                "material_path": "",
                "notes": "",
                "reject_reason": reason,
            }
        )
        _append_task_status(task, "失败")
        task["status"] = "进行中"
        _append_task_status(task, "进行中")
        task["notes"] = ""
        task["reject_reason"] = ""
        _append_attempt_history(replacement, entry)
        replacement.update(
            {
                "publication_journal": None,
                "publication_phase": "rolled_back",
                "execution_state": "running",
                "attempt_transition": "retiring",
                "last_execution_status": outcome.status,
                "execution_completed_at": None,
                "blocked_reason": "",
                "next_material_task_id": None,
                "active_material_task_id": None,
                "active_execution_id": None,
                "active_attempt_id": None,
                "active_attempt_kind": None,
                "active_control_id": None,
                "active_job_instance_id": None,
                "execution_revision": _required_revision(expected) + 1,
            }
        )
        claim.lease.clear_attempt_for_fallback(replacement=replacement)
        final = _json_clone(claim.lease.expected_payload)
        if (
            final.get("execution_id") != claim.binding.execution_id
            or final.get("execution_state") != "running"
            or final.get("publication_phase") != "rolled_back"
            or final.get("attempt_transition") != "retiring"
            or any(
                final.get(field) not in (None, "") for field in _ACTIVE_OWNER_FIELDS
            )
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "fallback terminal recording returned an invalid cleared owner"
            )
        return MaterialCompletionReceipt(
            status=outcome.status,
            execution_id=claim.binding.execution_id,
            operation=claim.binding.operation,
            attempt_id=claim.binding.attempt_id,
            execution_revision=_required_revision(final),
            final_payload_sha256=_json_sha256(final),
            attempt_history_sha256=entry["entry_sha256"],
            result_sha256=result_sha256,
            observed_at=observed_at,
        )

    def complete_terminal_for_fallback(
        self,
        claim: PreparedMaterialClaim,
        *,
        outcome: MaterialOperationOutcome,
    ) -> MaterialCompletionReceipt:
        """Compatibility alias for the first (clear-owner) fallback phase."""

        return self.record_terminal_attempt_for_fallback(claim, outcome=outcome)

    def rotate_terminal_attempt(
        self,
        claim: PreparedMaterialClaim,
        *,
        terminal_receipt: MaterialCompletionReceipt,
        retirement_receipt: MaterialAttemptRetirementReceipt,
        attempt_id: str,
        attempt_kind: MaterialOperationKind,
        control_id: str,
        job_instance_id: str,
    ) -> MaterialAttemptRotationReceipt:
        """Install a next attempt only after explicit runner-retirement proof."""

        if type(terminal_receipt) is not MaterialCompletionReceipt:
            raise TypeError("terminal_receipt must be MaterialCompletionReceipt")
        if type(retirement_receipt) is not MaterialAttemptRetirementReceipt:
            raise TypeError(
                "retirement_receipt must be MaterialAttemptRetirementReceipt"
            )
        if terminal_receipt.status not in {"failed", "terminated"}:
            raise MaterialExecutionCoordinatorUnknown(
                "rotation requires a failed or terminated terminal receipt"
            )
        _validate_uuid(attempt_id, "attempt_id")
        _validate_uuid(control_id, "control_id")
        _validate_uuid(job_instance_id, "job_instance_id")
        if attempt_kind not in _OPERATIONS:
            raise ValueError("attempt_kind is not an allowed material operation")
        if (
            terminal_receipt.execution_id != claim.binding.execution_id
            or terminal_receipt.attempt_id != claim.binding.attempt_id
            or terminal_receipt.operation != claim.binding.operation
            or retirement_receipt.status != terminal_receipt.status
            or retirement_receipt.execution_id != terminal_receipt.execution_id
            or retirement_receipt.attempt_id != terminal_receipt.attempt_id
            or retirement_receipt.operation != terminal_receipt.operation
            or retirement_receipt.execution_revision != terminal_receipt.execution_revision
            or retirement_receipt.final_payload_sha256
            != terminal_receipt.final_payload_sha256
            or retirement_receipt.attempt_history_sha256
            != terminal_receipt.attempt_history_sha256
            or retirement_receipt.result_sha256 != terminal_receipt.result_sha256
            or retirement_receipt.observed_at != terminal_receipt.observed_at
            or retirement_receipt.control_id != claim.binding.control_id
            or retirement_receipt.job_instance_id != claim.binding.job_instance_id
            or retirement_receipt.retired is not True
            or attempt_id == terminal_receipt.attempt_id
            or control_id in {
                terminal_receipt.attempt_id,
                claim.binding.control_id,
                claim.binding.job_instance_id,
            }
            or job_instance_id in {
                terminal_receipt.attempt_id,
                claim.binding.control_id,
                claim.binding.job_instance_id,
            }
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "terminal receipt or retirement receipt does not authorize rotation"
            )
        assert_owned = getattr(claim.lease, "assert_owned", None)
        if not callable(assert_owned):
            raise MaterialExecutionCoordinatorUnknown(
                "claim lease cannot prove ownership for rotation"
            )
        assert_owned()
        current = _json_clone(claim.lease.expected_payload)
        _assert_terminal_receipt_payload(current, terminal_receipt, claim)
        task = _owned_task(current, claim.binding.material_task_id)
        task["status"] = "进行中"
        _append_task_status(task, "进行中")
        task["notes"] = ""
        task["reject_reason"] = ""
        current.update(
            {
                "publication_journal": None,
                "publication_phase": "preparing",
                "execution_state": "claimed",
                "attempt_transition": None,
                "execution_completed_at": None,
                "blocked_reason": "",
                "next_material_task_id": None,
            }
        )
        claim.lease.install_rotated_attempt(
            attempt_id=attempt_id,
            attempt_kind=attempt_kind,
            control_id=control_id,
            job_instance_id=job_instance_id,
            terminal_receipt=terminal_receipt,
            retirement_receipt=retirement_receipt,
            replacement=current,
        )
        final = _json_clone(claim.lease.expected_payload)
        if (
            final.get("execution_id") != claim.binding.execution_id
            or final.get("active_execution_id") != claim.binding.execution_id
            or final.get("active_attempt_id") != attempt_id
            or final.get("active_attempt_kind") != attempt_kind
            or final.get("active_control_id") != control_id
            or final.get("active_job_instance_id") != job_instance_id
            or final.get("execution_state") != "claimed"
            or final.get("publication_phase") != "preparing"
            or final.get("attempt_transition") not in (None, "")
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "rotation CAS returned an invalid active owner"
            )
        return MaterialAttemptRotationReceipt(
            status=terminal_receipt.status,
            execution_id=terminal_receipt.execution_id,
            terminal_operation=terminal_receipt.operation,
            terminal_attempt_id=terminal_receipt.attempt_id,
            next_operation=attempt_kind,
            next_attempt_id=attempt_id,
            execution_revision=_required_revision(final),
            attempt_history_sha256=terminal_receipt.attempt_history_sha256 or "",
            final_payload_sha256=_json_sha256(final),
            observed_at=terminal_receipt.observed_at,
        )

    def install_rotated_attempt(
        self,
        claim: PreparedMaterialClaim,
        *,
        terminal_receipt: MaterialCompletionReceipt,
        retirement_receipt: MaterialAttemptRetirementReceipt,
        attempt_id: str,
        attempt_kind: MaterialOperationKind,
        control_id: str,
        job_instance_id: str,
    ) -> MaterialAttemptRotationReceipt:
        """Descriptive alias for the proof-gated second phase."""

        return self.rotate_terminal_attempt(
            claim,
            terminal_receipt=terminal_receipt,
            retirement_receipt=retirement_receipt,
            attempt_id=attempt_id,
            attempt_kind=attempt_kind,
            control_id=control_id,
            job_instance_id=job_instance_id,
        )

    def finalize_terminal_attempt_after_fallback(
        self,
        claim: PreparedMaterialClaim,
        *,
        terminal_receipt: MaterialCompletionReceipt,
        retirement_receipt: MaterialAttemptRetirementReceipt,
    ) -> MaterialCompletionReceipt:
        """Close an exhausted same-execution fallback chain.

        ``record_terminal_attempt_for_fallback`` intentionally leaves the
        execution in ``running/rolled_back + retiring`` until the runner has
        written its retirement tombstone and released every native pin.  If
        there is no next source, this method is the final database CAS that
        turns that proof-backed intermediate state into the ordinary terminal
        idle state.  It is separate from ``complete_terminal`` so the latter
        can never be mistaken for proof that the runner has retired its files.
        """

        if type(terminal_receipt) is not MaterialCompletionReceipt:
            raise TypeError("terminal_receipt must be MaterialCompletionReceipt")
        if type(retirement_receipt) is not MaterialAttemptRetirementReceipt:
            raise TypeError(
                "retirement_receipt must be MaterialAttemptRetirementReceipt"
            )
        if terminal_receipt.status not in {"failed", "terminated"}:
            raise MaterialExecutionCoordinatorUnknown(
                "fallback finalization requires a failed or terminated receipt"
            )
        if (
            terminal_receipt.execution_id != claim.binding.execution_id
            or terminal_receipt.operation != claim.binding.operation
            or terminal_receipt.attempt_id != claim.binding.attempt_id
            or retirement_receipt.status != terminal_receipt.status
            or retirement_receipt.execution_id != terminal_receipt.execution_id
            or retirement_receipt.operation != terminal_receipt.operation
            or retirement_receipt.attempt_id != terminal_receipt.attempt_id
            or retirement_receipt.control_id != claim.binding.control_id
            or retirement_receipt.job_instance_id != claim.binding.job_instance_id
            or retirement_receipt.execution_revision
            != terminal_receipt.execution_revision
            or retirement_receipt.final_payload_sha256
            != terminal_receipt.final_payload_sha256
            or retirement_receipt.attempt_history_sha256
            != terminal_receipt.attempt_history_sha256
            or retirement_receipt.result_sha256 != terminal_receipt.result_sha256
            or retirement_receipt.observed_at != terminal_receipt.observed_at
            or retirement_receipt.retired is not True
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "terminal or retirement receipt does not authorize finalization"
            )

        try:
            claim.lease.assert_owned()
        except BaseException as exc:
            raise MaterialExecutionCoordinatorUnknown(
                "claim lease ownership could not be proven for fallback finalization"
            ) from exc
        current = _json_clone(claim.lease.expected_payload)
        _assert_terminal_receipt_payload(current, terminal_receipt, claim)
        task = _owned_task(current, claim.binding.material_task_id)
        task["status"] = "失败"
        if terminal_receipt.result_sha256 is not None:
            task["reject_reason"] = str(task.get("reject_reason") or "来源未返回可用素材")
        else:
            task["reject_reason"] = str(
                task.get("reject_reason") or "来源进程已停止，但未获得可用素材"
            )
        task["material_path"] = ""
        task["notes"] = ""
        _append_task_status(task, "失败")
        _finish_payload(
            current,
            expected=current,
            status=terminal_receipt.status,
            publication_phase="rolled_back",
            observed_at=terminal_receipt.observed_at,
        )
        current["attempt_transition"] = None
        claim.lease.compare_and_swap(current)
        final = _json_clone(claim.lease.expected_payload)
        if (
            final.get("execution_id") != claim.binding.execution_id
            or final.get("execution_state") != "idle"
            or final.get("publication_phase") != "rolled_back"
            or final.get("attempt_transition") not in (None, "")
            or any(final.get(field) not in (None, "") for field in _ACTIVE_OWNER_FIELDS)
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "fallback finalization returned an invalid terminal payload"
            )
        return MaterialCompletionReceipt(
            status=terminal_receipt.status,
            execution_id=terminal_receipt.execution_id,
            operation=terminal_receipt.operation,
            attempt_id=terminal_receipt.attempt_id,
            execution_revision=_required_revision(final),
            final_payload_sha256=_json_sha256(final),
            attempt_history_sha256=terminal_receipt.attempt_history_sha256,
            result_sha256=terminal_receipt.result_sha256,
            observed_at=terminal_receipt.observed_at,
        )

    def record_blocked(
        self,
        claim: PreparedMaterialClaim,
        *,
        reason: str,
    ) -> None:
        if type(reason) is not str or not reason.strip() or len(reason) > 2000:
            raise ValueError("blocked reason must be non-empty and bounded")
        expected = _owned_payload(claim)
        _require_transition_gate(
            expected,
            claim,
            publication_phase="preparing",
            execution_state="claimed",
            revision_offset=0,
            label="blocked transition",
        )
        observed_at = _utc_now()
        entry = _attempt_history_entry(
            claim,
            status="blocked",
            observed_at=observed_at,
            result_sha256=None,
            publication_journal_sha256=None,
            reason_code="reconciliation_required",
            reason=reason,
        )
        replacement = copy.deepcopy(expected)
        task = _owned_task(replacement, claim.binding.material_task_id)
        task.update(
            {
                "status": "被阻止",
                "notes": reason,
                "reject_reason": "",
            }
        )
        _append_task_status(task, "被阻止")
        _append_attempt_history(replacement, entry)
        replacement.update(
            {
                "publication_phase": "audit_required",
                "execution_state": "blocked",
                "last_execution_status": "blocked",
                "blocked_reason": reason,
                "execution_revision": _required_revision(expected) + 1,
            }
        )
        claim.lease.compare_and_swap(replacement)


def completion_receipt_from_pending_payload(
    payload: Mapping[str, Any],
) -> MaterialCompletionReceipt:
    """Reconstruct the exact completion proof carried by a pending blob."""

    checkpoint = pending_runner_retirement_from_payload(payload)
    return MaterialCompletionReceipt(
        status=checkpoint.completion_status,
        execution_id=checkpoint.execution_id,
        operation=checkpoint.operation,
        attempt_id=checkpoint.attempt_id,
        execution_revision=checkpoint.completion_revision,
        final_payload_sha256=json_payload_sha256(payload),
        publication_journal_sha256=checkpoint.publication_journal_sha256,
        attempt_history_sha256=checkpoint.attempt_history_sha256,
        result_sha256=checkpoint.result_sha256,
        publication_intent_sha256=checkpoint.publication_intent_sha256,
        publication_result_sha256=checkpoint.publication_result_sha256,
        publication_worker_result_sha256=(
            checkpoint.publication_worker_result_sha256
        ),
        publication_reference_base_revision=(
            checkpoint.publication_reference_base_revision
        ),
        publication_reference_base_payload_sha256=(
            checkpoint.publication_reference_base_payload_sha256
        ),
        runner_retirement_checkpoint_sha256=checkpoint.checkpoint_sha256,
        observed_at=checkpoint.completion_observed_at,
    )


def restart_retirement_proof_from_completion(
    completion: MaterialCompletionReceipt,
) -> MaterialRestartRetirementProof:
    """Convert a fresh terminal DB CAS receipt into runner retirement proof."""

    if type(completion) is not MaterialCompletionReceipt:
        raise MaterialExecutionCoordinatorUnknown(
            "restart retirement requires a typed completion receipt"
        )
    if completion.status not in {"failed", "terminated"}:
        raise MaterialExecutionCoordinatorUnknown(
            "only failed or terminated completion can retire a restart-adopted attempt"
        )
    if completion.attempt_history_sha256 is None or not completion.observed_at:
        raise MaterialExecutionCoordinatorUnknown(
            "terminal completion lacks the durable history or observation receipt"
        )
    if completion.status == "failed" and completion.result_sha256 is None:
        raise MaterialExecutionCoordinatorUnknown(
            "failed completion lacks its worker-result receipt"
        )
    if completion.status == "terminated" and completion.result_sha256 is not None:
        raise MaterialExecutionCoordinatorUnknown(
            "terminated completion cannot carry a worker-result receipt"
        )
    return MaterialRestartRetirementProof(
        status=completion.status,
        execution_id=completion.execution_id,
        operation=completion.operation,
        attempt_id=completion.attempt_id,
        execution_revision=completion.execution_revision,
        final_payload_sha256=completion.final_payload_sha256,
        attempt_history_sha256=completion.attempt_history_sha256,
        result_sha256=completion.result_sha256,
        observed_at=completion.observed_at,
        checkpoint_sha256=(
            completion.runner_retirement_checkpoint_sha256 or ""
        ),
    )


def restart_published_retirement_proof_from_completion(
    completion: MaterialCompletionReceipt,
) -> MaterialRestartPublishedRetirementProof:
    """Convert the recovered success CAS receipt into runner proof."""

    if type(completion) is not MaterialCompletionReceipt:
        raise MaterialExecutionCoordinatorUnknown(
            "restart publication retirement requires a typed completion receipt"
        )
    if completion.status != "succeeded":
        raise MaterialExecutionCoordinatorUnknown(
            "restart publication retirement requires successful completion"
        )
    if (
        completion.publication_journal_sha256 is None
        or completion.attempt_history_sha256 is None
        or completion.result_sha256 is None
        or completion.publication_intent_sha256 is None
        or completion.publication_result_sha256 is None
        or completion.publication_worker_result_sha256 is None
        or completion.publication_reference_base_revision is None
        or completion.publication_reference_base_payload_sha256 is None
        or not completion.observed_at
    ):
        raise MaterialExecutionCoordinatorUnknown(
            "successful completion lacks publication, history, result, or time receipts"
        )
    return MaterialRestartPublishedRetirementProof(
        status="succeeded",
        execution_id=completion.execution_id,
        operation=completion.operation,
        attempt_id=completion.attempt_id,
        execution_revision=completion.execution_revision,
        final_payload_sha256=completion.final_payload_sha256,
        attempt_history_sha256=completion.attempt_history_sha256,
        publication_journal_sha256=completion.publication_journal_sha256,
        publication_intent_sha256=completion.publication_intent_sha256,
        publication_result_sha256=completion.publication_result_sha256,
        publication_worker_result_sha256=(
            completion.publication_worker_result_sha256
        ),
        publication_reference_base_revision=(
            completion.publication_reference_base_revision
        ),
        publication_reference_base_payload_sha256=(
            completion.publication_reference_base_payload_sha256
        ),
        result_sha256=completion.result_sha256,
        observed_at=completion.observed_at,
        checkpoint_sha256=(
            completion.runner_retirement_checkpoint_sha256 or ""
        ),
    )


class MaterialRestartRecoveryUnknown(MaterialExecutionCoordinatorUnknown):
    """Recovery kept same-thread ownership but could not finish its proof."""

    def __init__(
        self,
        message: str,
        *,
        mutex: Any,
        adopted: AdoptedMaterialAttempt | None,
        lease: Any | None = None,
        publication: CanonicalMaterialPublication | Any | None = None,
        completion: MaterialCompletionReceipt | None = None,
    ) -> None:
        super().__init__(message)
        self.mutex = mutex
        self.adopted = adopted
        self.lease = lease
        self.publication = publication
        self.completion = completion


@dataclass(frozen=True, slots=True)
class PendingMaterialRetirementClaim:
    """Final DB owner plus the publishing reference used by runner recovery."""

    prepared: PreparedMaterialAttempt
    binding: MaterialRunnerClaimBinding | None
    lease: Any
    completion: MaterialCompletionReceipt

    def __post_init__(self) -> None:
        if type(self.prepared) is not PreparedMaterialAttempt:
            raise TypeError("prepared must be PreparedMaterialAttempt")
        if self.binding is not None:
            if type(self.binding) is not MaterialRunnerClaimBinding:
                raise TypeError("binding must be MaterialRunnerClaimBinding or None")
            if (
                self.prepared.execution_id != self.binding.execution_id
                or self.prepared.attempt_id != self.binding.attempt_id
                or self.prepared.control_id != self.binding.control_id
                or self.prepared.job_instance_id != self.binding.job_instance_id
            ):
                raise ValueError("prepared attempt and retirement binding disagree")
        if type(self.completion) is not MaterialCompletionReceipt:
            raise TypeError("completion must be MaterialCompletionReceipt")
        if (
            self.prepared.execution_id != self.completion.execution_id
            or self.prepared.attempt_id != self.completion.attempt_id
        ):
            raise ValueError("prepared attempt and completion receipt disagree")


class MaterialRestartRecoveryCoordinator:
    """Bridge a fresh terminal DB receipt to restart-only runner retirement.

    The caller must acquire the recovery mutex on the same thread and pass the
    still-owned handle.  This class never moves the handle to a background
    task, and it deliberately leaves the handle/recovered pins reachable on an
    unknown edge for an explicit operator or process-level recovery decision.
    """

    def __init__(
        self,
        *,
        store: Any,
        runner: Any,
        lifecycle: Any | None = None,
        publication_recovery_factory: Any | None = None,
    ) -> None:
        if not callable(getattr(store, "fresh_read", None)):
            raise TypeError("store must expose fresh_read")
        if not callable(getattr(runner, "adopt_durable_attempt", None)) or not callable(
            getattr(runner, "retire_adopted_terminal_attempt", None)
        ):
            raise TypeError("runner does not expose restart adoption and retirement")
        self.store = store
        self.runner = runner
        self.lifecycle = lifecycle
        self.publication_recovery_factory = publication_recovery_factory

    def complete_published_attempt(
        self,
        *,
        mutex: Any,
        material_task_id: str,
        execution_id: str,
        attempt_id: str,
        control_id: str,
        job_instance_id: str,
        operation: str,
    ) -> MaterialCompletionReceipt:
        """Finish an abandoned ``db_committed`` publication without replay.

        The exact ``r+1`` publishing blob is adopted as a lease, the dead
        worker attempt and already-published targets are re-attested, and only
        then may the lifecycle perform the single ``r+2`` completion CAS.
        Publisher and claim ownership remain live until runner retirement.
        """

        if not callable(
            getattr(self.store, "adopt_publishing_lease_with_owned_recovery_mutex", None)
        ):
            raise TypeError("store does not expose publishing lease adoption")
        if not callable(getattr(self.runner, "retire_adopted_published_attempt", None)):
            raise TypeError("runner does not expose published restart retirement")
        if not callable(
            getattr(self.lifecycle, "complete_recovered_success", None)
        ):
            raise TypeError("lifecycle does not expose recovered success completion")
        if not callable(
            getattr(self.lifecycle, "acknowledge_runner_retirement", None)
        ):
            raise TypeError("lifecycle does not expose runner retirement acknowledgement")
        if not callable(self.publication_recovery_factory):
            raise TypeError("publication_recovery_factory must be callable")

        guard: MaterialRestartOwnershipGuard | None = None
        lease: Any | None = None
        adopted: AdoptedMaterialAttempt | Any | None = None
        publication: CanonicalMaterialPublication | Any | None = None
        completion: MaterialCompletionReceipt | None = None
        try:
            guard = MaterialRestartOwnershipGuard(
                execution_id=execution_id,
                attempt_id=attempt_id,
                control_id=control_id,
                job_instance_id=job_instance_id,
                mutex=mutex,
            )
            assert_recovery_mutex = getattr(self.store, "assert_recovery_mutex", None)
            if callable(assert_recovery_mutex):
                assert_recovery_mutex(mutex)
            guard.assert_owned()
            lease = self.store.adopt_publishing_lease_with_owned_recovery_mutex(
                mutex=mutex,
                material_task_id=material_task_id,
                execution_id=execution_id,
                attempt_id=attempt_id,
                attempt_kind=operation,
                control_id=control_id,
                job_instance_id=job_instance_id,
            )
            publishing_payload = lease.expected_payload
            binding = MaterialRunnerClaimBinding(
                material_task_id=material_task_id,
                execution_id=execution_id,
                operation=operation,
                attempt_id=attempt_id,
                control_id=control_id,
                job_instance_id=job_instance_id,
                execution_revision=lease.expected_revision,
                payload_sha256=_json_sha256(publishing_payload),
            )
            guard.assert_owned()
            adopted = self.runner.adopt_durable_attempt(
                ownership_guard=guard,
                binding=binding,
                expected_operation=operation,
            )
            prepared = getattr(adopted, "prepared", None)
            outcome = getattr(adopted, "outcome", None)
            if type(prepared) is not PreparedMaterialAttempt:
                raise MaterialExecutionCoordinatorUnknown(
                    "runner adoption returned no typed prepared attempt"
                )
            if (
                type(outcome) is not MaterialOperationOutcome
                or outcome.status != "succeeded"
                or outcome.result is None
            ):
                raise MaterialExecutionCoordinatorUnknown(
                    "runner adoption returned no durable successful outcome"
                )
            claim = PreparedMaterialClaim(
                prepared=prepared,
                binding=binding,
                lease=lease,
            )
            guard.assert_owned()
            publication = self.publication_recovery_factory(claim, adopted)
            if (
                getattr(publication, "phase", None) != "committed"
                or not bool(getattr(publication, "ownership_retained", False))
            ):
                raise MaterialExecutionCoordinatorUnknown(
                    "publication recovery returned no retained committed owner"
                )
            guard.assert_owned()
            completion = self.lifecycle.complete_recovered_success(
                claim,
                outcome=outcome,
                publication=publication,
            )
            proof = restart_published_retirement_proof_from_completion(completion)
            if (
                proof.execution_id != execution_id
                or proof.attempt_id != attempt_id
                or proof.operation != operation
            ):
                raise MaterialExecutionCoordinatorUnknown(
                    "recovered completion proof belongs to another attempt"
                )
            guard.assert_owned()
            _assert_restart_published_completion_payload(
                self.store.fresh_read(),
                proof=proof,
                material_task_id=material_task_id,
                control_id=control_id,
                job_instance_id=job_instance_id,
                operation=operation,
            )
            attestation = self.runner.retire_adopted_published_attempt(
                adopted,
                proof=proof,
                publication_ownership=publication,
            )
            if type(attestation) is not MaterialRunnerRetirementAttestation:
                raise MaterialExecutionCoordinatorUnknown(
                    "restart publication retirement returned no typed attestation"
                )
            guard.assert_owned()
            acknowledgement = self.lifecycle.acknowledge_runner_retirement(
                claim,
                completion=completion,
                attestation=attestation,
            )
            if type(acknowledgement) is not MaterialRunnerRetirementAcknowledgement:
                raise MaterialExecutionCoordinatorUnknown(
                    "restart publication retirement returned no typed acknowledgement"
                )
            publication.release_after_retirement()
            guard.assert_owned()
            lease.release()
            return completion
        except MaterialRestartRecoveryUnknown:
            raise
        except BaseException as exc:
            raise MaterialRestartRecoveryUnknown(
                "restart publication recovery is unknown; owned recovery evidence remains reachable",
                mutex=mutex,
                adopted=adopted,
                lease=lease,
                publication=publication,
                completion=completion,
            ) from exc

    def complete_pending_runner_retirement(
        self,
        *,
        mutex: Any,
        material_task_id: str,
        execution_id: str,
        attempt_id: str,
        control_id: str,
        job_instance_id: str,
        operation: str,
        completion_status: str,
        completion_revision: int,
        checkpoint_sha256: str,
    ) -> MaterialCompletionReceipt:
        """Ack one final pending completion without replaying worker/publication."""

        adopt_lease = getattr(
            self.store,
            "adopt_pending_retirement_lease_with_owned_recovery_mutex",
            None,
        )
        if not callable(adopt_lease):
            raise TypeError("store does not expose pending retirement lease adoption")
        acknowledge = getattr(
            self.lifecycle,
            "acknowledge_pending_runner_retirement",
            None,
        )
        if not callable(acknowledge):
            raise TypeError("lifecycle does not expose pending retirement acknowledgement")

        guard: MaterialRestartOwnershipGuard | None = None
        lease: Any | None = None
        adopted: AdoptedMaterialAttempt | Any | None = None
        publication: CanonicalMaterialPublication | Any | None = None
        completion: MaterialCompletionReceipt | None = None
        try:
            guard = MaterialRestartOwnershipGuard(
                execution_id=execution_id,
                attempt_id=attempt_id,
                control_id=control_id,
                job_instance_id=job_instance_id,
                mutex=mutex,
            )
            assert_recovery_mutex = getattr(self.store, "assert_recovery_mutex", None)
            if callable(assert_recovery_mutex):
                assert_recovery_mutex(mutex)
            guard.assert_owned()
            lease = adopt_lease(
                mutex=mutex,
                material_task_id=material_task_id,
                execution_id=execution_id,
                attempt_id=attempt_id,
                attempt_kind=operation,
                control_id=control_id,
                job_instance_id=job_instance_id,
            )
            checkpoint = getattr(lease, "checkpoint", None)
            if type(checkpoint) is not MaterialRunnerRetirementCheckpoint:
                raise MaterialExecutionCoordinatorUnknown(
                    "pending retirement lease returned no typed checkpoint"
                )
            if (
                checkpoint.completion_status != completion_status
                or checkpoint.completion_revision != completion_revision
                or checkpoint.checkpoint_sha256 != checkpoint_sha256
            ):
                raise MaterialExecutionCoordinatorUnknown(
                    "pending retirement changed after startup discovery"
                )
            completion = completion_receipt_from_pending_payload(
                lease.expected_payload
            )
            proof: (
                MaterialRestartRetirementProof
                | MaterialRestartPublishedRetirementProof
            )
            binding: MaterialRunnerClaimBinding | None = None
            if completion.status == "succeeded":
                proof = restart_published_retirement_proof_from_completion(completion)
                binding = MaterialRunnerClaimBinding(
                    material_task_id=material_task_id,
                    execution_id=execution_id,
                    operation=operation,
                    attempt_id=attempt_id,
                    control_id=control_id,
                    job_instance_id=job_instance_id,
                    execution_revision=checkpoint.reference_revision,
                    payload_sha256=checkpoint.reference_payload_sha256,
                )
                adoption_values = {
                    "ownership_guard": guard,
                    "binding": binding,
                    "expected_operation": operation,
                    "retirement_proof": proof,
                }
            else:
                proof = restart_retirement_proof_from_completion(completion)
                adoption_values = {
                    "ownership_guard": guard,
                    "binding": None,
                    "execution_id": execution_id,
                    "attempt_id": attempt_id,
                    "control_id": control_id,
                    "job_instance_id": job_instance_id,
                    "expected_operation": operation,
                    "retirement_proof": proof,
                }
            guard.assert_owned()
            adopted = self.runner.adopt_durable_attempt(**adoption_values)
            prepared = getattr(adopted, "prepared", None)
            outcome = getattr(adopted, "outcome", None)
            if type(prepared) is not PreparedMaterialAttempt:
                raise MaterialExecutionCoordinatorUnknown(
                    "runner retirement adoption returned no prepared attempt"
                )
            if (
                type(outcome) is not MaterialOperationOutcome
                or outcome.status != completion.status
            ):
                raise MaterialExecutionCoordinatorUnknown(
                    "runner retirement adoption returned no matching durable outcome"
                )
            pending_claim = PendingMaterialRetirementClaim(
                prepared=prepared,
                binding=binding,
                lease=lease,
                completion=completion,
            )
            guard.assert_owned()
            if completion.status == "succeeded":
                if not callable(self.publication_recovery_factory):
                    raise TypeError(
                        "successful retirement requires publication recovery"
                    )
                publication = self.publication_recovery_factory(
                    pending_claim,
                    adopted,
                )
                if (
                    getattr(publication, "phase", None) != "committed"
                    or not bool(getattr(publication, "ownership_retained", False))
                ):
                    raise MaterialExecutionCoordinatorUnknown(
                        "completed publication recovery retained no committed owner"
                    )
                retire = getattr(
                    self.runner,
                    "retire_adopted_published_attempt",
                    None,
                )
                if not callable(retire):
                    raise TypeError(
                        "runner does not expose adopted publication retirement"
                    )
                attestation = retire(
                    adopted,
                    proof=proof,
                    publication_ownership=publication,
                )
            else:
                retire = getattr(
                    self.runner,
                    "attest_completed_adopted_terminal_attempt",
                    None,
                )
                if not callable(retire):
                    raise TypeError(
                        "runner does not expose completed terminal attestation"
                    )
                attestation = retire(adopted, proof=proof)
            if type(attestation) is not MaterialRunnerRetirementAttestation:
                raise MaterialExecutionCoordinatorUnknown(
                    "runner returned no typed retirement attestation"
                )
            guard.assert_owned()
            acknowledgement = acknowledge(
                lease,
                completion=completion,
                attestation=attestation,
            )
            if type(acknowledgement) is not MaterialRunnerRetirementAcknowledgement:
                raise MaterialExecutionCoordinatorUnknown(
                    "lifecycle returned no typed retirement acknowledgement"
                )
            guard.assert_owned()
            if publication is not None:
                publication.release_after_retirement()
            lease.release()
            return completion
        except MaterialRestartRecoveryUnknown:
            raise
        except BaseException as exc:
            raise MaterialRestartRecoveryUnknown(
                "pending runner retirement is unknown; owned recovery evidence remains reachable",
                mutex=mutex,
                adopted=adopted,
                lease=lease,
                publication=publication,
                completion=completion,
            ) from exc

    def retire_terminal_attempt(
        self,
        *,
        mutex: Any,
        material_task_id: str,
        execution_id: str,
        attempt_id: str,
        control_id: str,
        job_instance_id: str,
        operation: str,
        completion: MaterialCompletionReceipt,
    ) -> MaterialRestartRetirementProof:
        if type(completion) is not MaterialCompletionReceipt or (
            completion.runner_retirement_checkpoint_sha256 is None
        ):
            raise MaterialRestartRecoveryUnknown(
                "terminal recovery requires the pending retirement checkpoint",
                mutex=mutex,
                adopted=None,
                completion=completion,
            )
        recovered = self.complete_pending_runner_retirement(
            mutex=mutex,
            material_task_id=material_task_id,
            execution_id=execution_id,
            attempt_id=attempt_id,
            control_id=control_id,
            job_instance_id=job_instance_id,
            operation=operation,
            completion_status=completion.status,
            completion_revision=completion.execution_revision,
            checkpoint_sha256=(
                completion.runner_retirement_checkpoint_sha256
            ),
        )
        return restart_retirement_proof_from_completion(recovered)


def _assert_restart_published_completion_payload(
    payload: Mapping[str, Any],
    *,
    proof: MaterialRestartPublishedRetirementProof,
    material_task_id: str,
    control_id: str,
    job_instance_id: str,
    operation: str,
) -> dict[str, Any]:
    current = _json_clone(payload)
    if (
        current.get("execution_id") != proof.execution_id
        or _required_revision(current) != proof.execution_revision
        or _json_sha256(current) != proof.final_payload_sha256
    ):
        raise MaterialExecutionCoordinatorUnknown(
            "fresh successful payload does not match the completion proof"
        )
    if (
        current.get("publication_intent_sha256")
        != proof.publication_intent_sha256
        or current.get("publication_result_sha256")
        != proof.publication_result_sha256
        or current.get("publication_worker_result_sha256")
        != proof.publication_worker_result_sha256
        or current.get("publication_reference_base_revision")
        != proof.publication_reference_base_revision
        or current.get("publication_reference_base_payload_sha256")
        != proof.publication_reference_base_payload_sha256
    ):
        raise MaterialExecutionCoordinatorUnknown(
            "successful completion publication receipts changed after the CAS"
        )
    if any(current.get(field) is not None for field in _ACTIVE_OWNER_FIELDS):
        raise MaterialExecutionCoordinatorUnknown(
            "successful completion still has an active claim owner"
        )
    if (
        current.get("execution_state") != "idle"
        or current.get("publication_phase") != "committed"
        or current.get("last_execution_status") != "succeeded"
        or current.get("execution_completed_at") != proof.observed_at
    ):
        raise MaterialExecutionCoordinatorUnknown(
            "successful completion is not in the committed idle phase"
        )
    tasks = current.get("material_tasks")
    matches = (
        []
        if not isinstance(tasks, list)
        else [
            item
            for item in tasks
            if isinstance(item, Mapping)
            and item.get("material_task_id") == material_task_id
        ]
    )
    expected_task_status = (
        "需要人工确认" if operation == "stock_candidates" else "已完成"
    )
    if len(matches) != 1 or matches[0].get("status") != expected_task_status:
        raise MaterialExecutionCoordinatorUnknown(
            "successful completion task status does not match the recovered operation"
        )
    history = current.get("material_attempt_history")
    if not isinstance(history, list):
        raise MaterialExecutionCoordinatorUnknown(
            "successful completion has no attempt history"
        )
    history_matches = [
        item
        for item in history
        if isinstance(item, Mapping)
        and item.get("material_task_id") == material_task_id
        and item.get("execution_id") == proof.execution_id
        and item.get("operation") == operation
        and item.get("attempt_id") == proof.attempt_id
        and item.get("control_id") == control_id
        and item.get("job_instance_id") == job_instance_id
        and item.get("status") == "succeeded"
        and item.get("observed_at") == proof.observed_at
        and item.get("result_sha256") == proof.result_sha256
        and item.get("publication_journal_sha256")
        == proof.publication_journal_sha256
        and item.get("entry_sha256") == proof.attempt_history_sha256
    ]
    if len(history_matches) != 1:
        raise MaterialExecutionCoordinatorUnknown(
            "successful completion history does not match the recovery proof"
        )
    entry = dict(history_matches[0])
    entry_sha256 = entry.pop("entry_sha256", None)
    if entry_sha256 != _json_sha256(entry):
        raise MaterialExecutionCoordinatorUnknown(
            "successful completion history receipt is not self-consistent"
        )
    return current


def _assert_restart_completion_payload(
    payload: Mapping[str, Any],
    *,
    proof: MaterialRestartRetirementProof,
    material_task_id: str,
    control_id: str,
    operation: str,
) -> dict[str, Any]:
    current = _json_clone(payload)
    if (
        current.get("execution_id") != proof.execution_id
        or _required_revision(current) != proof.execution_revision
        or _json_sha256(current) != proof.final_payload_sha256
    ):
        raise MaterialExecutionCoordinatorUnknown(
            "fresh terminal payload does not match the completion proof"
        )
    if any(current.get(field) is not None for field in _ACTIVE_OWNER_FIELDS):
        raise MaterialExecutionCoordinatorUnknown(
            "terminal completion still has an active claim owner"
        )
    if current.get("execution_state") != "idle" or current.get(
        "publication_phase"
    ) != "rolled_back":
        raise MaterialExecutionCoordinatorUnknown(
            "terminal completion is not in the rolled-back idle phase"
        )
    history = current.get("material_attempt_history")
    if not isinstance(history, list):
        raise MaterialExecutionCoordinatorUnknown(
            "terminal completion has no attempt history"
        )
    matches = [
        item
        for item in history
        if isinstance(item, Mapping)
        and item.get("entry_sha256") == proof.attempt_history_sha256
    ]
    if len(matches) != 1:
        raise MaterialExecutionCoordinatorUnknown(
            "completion history receipt is not unique in the fresh payload"
        )
    entry = dict(matches[0])
    entry_sha256 = entry.pop("entry_sha256", None)
    if (
        entry_sha256 != proof.attempt_history_sha256
        or _json_sha256(entry) != proof.attempt_history_sha256
        or entry.get("material_task_id") != material_task_id
        or entry.get("execution_id") != proof.execution_id
        or entry.get("attempt_id") != proof.attempt_id
        or entry.get("control_id") != control_id
        or entry.get("operation") != operation
        or entry.get("status") != proof.status
        or entry.get("observed_at") != proof.observed_at
        or entry.get("result_sha256") != proof.result_sha256
    ):
        raise MaterialExecutionCoordinatorUnknown(
            "completion history receipt does not bind to the requested attempt"
        )
    return current


def _validate_completion_intent_reference(
    expected: Mapping[str, Any],
    *,
    claim: PreparedMaterialClaim,
    outcome: MaterialOperationOutcome,
    publication: CanonicalMaterialPublication,
    recovered: bool,
) -> None:
    if type(recovered) is not bool:
        raise TypeError("recovered must be a boolean")
    try:
        intent = publication.completion_intent
    except BaseException as exc:
        raise MaterialExecutionCoordinatorUnknown(
            "success completion has no durable completion intent"
        ) from exc
    if type(intent) is not dict or set(intent) != {
        "format",
        "claim_receipt",
        "worker_result_sha256",
        "published_result",
        "published_result_sha256",
        "intent_sha256",
    }:
        raise MaterialExecutionCoordinatorUnknown(
            "success completion intent schema is invalid"
        )
    claim_receipt = intent.get("claim_receipt")
    if type(claim_receipt) is not dict or set(claim_receipt) != {
        "material_task_id",
        "execution_id",
        "attempt_id",
        "operation",
        "control_id",
        "job_instance_id",
        "reference_base_revision",
        "reference_base_payload_sha256",
    }:
        raise MaterialExecutionCoordinatorUnknown(
            "success completion intent claim receipt is invalid"
        )
    result = getattr(publication, "result", None)
    if type(result) is MaterialCandidateBatchResult:
        published_result: dict[str, Any] = {
            "kind": "stock_candidate_batch",
            "success": result.success,
            "status": result.status,
            "reason": result.reason,
            "candidates": [candidate.to_dict() for candidate in result.candidates],
        }
    elif type(result) is MaterialExecutionResult:
        published_result = {
            "kind": "material_execution_result",
            "success": result.success,
            "status": result.status,
            "material_path": result.material_path,
            "material_type": result.material_type,
            "source_type": result.source_type,
            "reason": result.reason,
            "evidence_kind": result.evidence_kind,
            "source_url": result.source_url,
            "is_locally_generated": result.is_locally_generated,
            "rights_evidence": copy.deepcopy(result.rights_evidence),
        }
    else:
        raise MaterialExecutionCoordinatorUnknown(
            "success completion publication result type is invalid"
        )
    intent_without_hash = copy.deepcopy(intent)
    intent_sha256 = intent_without_hash.pop("intent_sha256", None)
    worker_result = outcome.result
    if worker_result is None:
        raise MaterialExecutionCoordinatorUnknown(
            "success completion outcome has no worker result"
        )
    worker_result_sha256 = hashlib.sha256(
        encode_material_worker_result(worker_result)
    ).hexdigest()
    reference_base_revision = (
        claim.binding.execution_revision - 1
        if recovered
        else claim.binding.execution_revision
    )
    if (
        intent.get("format") != "material-publication-completion-intent-v1"
        or intent_sha256 != _json_sha256(intent_without_hash)
        or intent.get("published_result") != published_result
        or intent.get("published_result_sha256") != _json_sha256(published_result)
        or intent.get("worker_result_sha256") != worker_result_sha256
        or claim_receipt.get("material_task_id")
        != claim.binding.material_task_id
        or claim_receipt.get("execution_id") != claim.binding.execution_id
        or claim_receipt.get("attempt_id") != claim.binding.attempt_id
        or claim_receipt.get("operation") != claim.binding.operation
        or claim_receipt.get("control_id") != claim.binding.control_id
        or claim_receipt.get("job_instance_id") != claim.binding.job_instance_id
        or claim_receipt.get("reference_base_revision")
        != reference_base_revision
        or expected.get("publication_intent_sha256") != intent_sha256
        or expected.get("publication_result_sha256")
        != intent.get("published_result_sha256")
        or expected.get("publication_worker_result_sha256")
        != worker_result_sha256
        or expected.get("publication_reference_base_revision")
        != reference_base_revision
        or expected.get("publication_reference_base_payload_sha256")
        != claim_receipt.get("reference_base_payload_sha256")
    ):
        raise MaterialExecutionCoordinatorUnknown(
            "success completion intent does not match the active publishing owner"
        )


def _owned_payload(claim: PreparedMaterialClaim) -> dict[str, Any]:
    if type(claim) is not PreparedMaterialClaim:
        raise TypeError("claim must be PreparedMaterialClaim")
    assert_owned = getattr(claim.lease, "assert_owned", None)
    if not callable(assert_owned):
        raise MaterialExecutionCoordinatorUnknown(
            "claim lease cannot prove same-thread mutex ownership"
        )
    try:
        assert_owned()
        payload = _json_clone(claim.lease.expected_payload)
    except BaseException as exc:
        raise MaterialExecutionCoordinatorUnknown(
            "claim lease ownership or fresh payload could not be proved"
        ) from exc
    expected_owner = (
        claim.binding.material_task_id,
        claim.binding.execution_id,
        claim.binding.attempt_id,
        claim.binding.control_id,
        claim.binding.job_instance_id,
    )
    actual_owner = (
        payload.get("active_material_task_id"),
        payload.get("active_execution_id"),
        payload.get("active_attempt_id"),
        payload.get("active_control_id"),
        payload.get("active_job_instance_id"),
    )
    if actual_owner != expected_owner or payload.get("execution_id") != claim.binding.execution_id:
        raise MaterialExecutionCoordinatorUnknown(
            "fresh completion payload does not belong to the prepared claim"
        )
    if _required_revision(payload) < claim.binding.execution_revision:
        raise MaterialExecutionCoordinatorUnknown(
            "fresh completion payload predates the claim binding"
        )
    _owned_task(payload, claim.binding.material_task_id)
    return payload


def _owned_task(payload: dict[str, Any], material_task_id: str) -> dict[str, Any]:
    tasks = payload.get("material_tasks")
    if not isinstance(tasks, list):
        raise MaterialExecutionCoordinatorUnknown("material task list is missing")
    matches = [
        item
        for item in tasks
        if isinstance(item, dict) and item.get("material_task_id") == material_task_id
    ]
    if len(matches) != 1:
        raise MaterialExecutionCoordinatorUnknown(
            "active material task is not unique in the fresh payload"
        )
    return matches[0]


def _require_transition_gate(
    payload: Mapping[str, Any],
    claim: PreparedMaterialClaim,
    *,
    publication_phase: str,
    execution_state: str,
    revision_offset: int,
    label: str,
) -> None:
    expected_revision = claim.binding.execution_revision + revision_offset
    if (
        payload.get("publication_phase") != publication_phase
        or payload.get("execution_state") != execution_state
        or _required_revision(payload) != expected_revision
    ):
        raise MaterialExecutionCoordinatorUnknown(
            f"{label} requires exact phase, state, and revision ownership"
        )


def _attempt_history_entry(
    claim: PreparedMaterialClaim,
    *,
    status: str,
    observed_at: str,
    result_sha256: str | None,
    publication_journal_sha256: str | None,
    reason_code: str,
    reason: str,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "material_task_id": claim.binding.material_task_id,
        "execution_id": claim.binding.execution_id,
        "operation": claim.binding.operation,
        "attempt_id": claim.binding.attempt_id,
        "control_id": claim.binding.control_id,
        "job_instance_id": claim.binding.job_instance_id,
        "status": status,
        "observed_at": observed_at,
        "result_sha256": result_sha256,
        "publication_journal_sha256": publication_journal_sha256,
        "reason_code": reason_code,
        "reason": reason,
    }
    entry = copy.deepcopy(base)
    entry["entry_sha256"] = _json_sha256(base)
    return entry


def _append_attempt_history(payload: dict[str, Any], entry: Mapping[str, Any]) -> None:
    history = payload.get("material_attempt_history")
    if history is None:
        history = []
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise MaterialExecutionCoordinatorUnknown("material attempt history is invalid")
    updated = [copy.deepcopy(item) for item in history]
    updated.append(copy.deepcopy(dict(entry)))
    payload["material_attempt_history"] = updated[-_MAX_ATTEMPT_HISTORY:]


def _apply_stock_candidate_batch(
    payload: dict[str, Any],
    task: dict[str, Any],
    published_result: Any,
    *,
    claim: PreparedMaterialClaim,
    observed_at: str,
) -> None:
    if type(published_result) is not MaterialCandidateBatchResult:
        raise MaterialExecutionCoordinatorUnknown(
            "stock candidate completion requires an exact published candidate batch"
        )
    if published_result.status != "需要人工确认":
        raise MaterialExecutionCoordinatorUnknown(
            "stock candidate completion has an invalid review status"
        )
    shot_id = task.get("shot_id")
    if not isinstance(shot_id, str) or not shot_id.strip():
        raise MaterialExecutionCoordinatorUnknown(
            "stock candidate completion task has no shot_id"
        )
    shot_id = shot_id.strip()
    try:
        pools = _candidate_pool_map(payload.get("candidate_pool"))
        current_value = pools.get(shot_id)
        if current_value is None:
            state = CandidateSet.from_candidates(
                shot_id,
                published_result.candidates,
            )
        else:
            current = CandidateSet.from_dict(current_value)
            if current.shot_id != shot_id:
                raise CandidateContractError(
                    "candidate pool shot_id does not match the material task"
                )
            # Regeneration is never an implicit re-selection operation.  Even
            # an identical locked row must be preserved until Ian explicitly
            # unlocks/reselects through the candidate review contract.
            if current.locked_candidate is not None:
                raise CandidateContractError(
                    "locked candidate pool cannot be overwritten by regeneration"
                )
            state = replace_candidates(current, published_result.candidates)
    except CandidateContractError as exc:
        raise MaterialExecutionCoordinatorUnknown(str(exc)) from exc

    pools[shot_id] = state.to_dict()
    payload["candidate_pool"] = pools
    for field in (
        "material_path",
        "material_type",
        "source_type",
        "evidence_kind",
        "source_url",
        "is_locally_generated",
        "rights_evidence",
        "selected_candidate_id",
        "locked_candidate_id",
    ):
        task.pop(field, None)
    task.update(
        {
            "status": "需要人工确认",
            "notes": published_result.reason,
            "reject_reason": "",
        }
    )
    _append_task_status(task, "需要人工确认")

    history = payload.get("candidate_generation_history")
    if history is None:
        history = []
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise MaterialExecutionCoordinatorUnknown(
            "candidate generation history is invalid"
        )
    event: dict[str, Any] = {
        "shot_id": shot_id,
        "material_task_id": claim.binding.material_task_id,
        "execution_id": claim.binding.execution_id,
        "attempt_id": claim.binding.attempt_id,
        "candidate_revision": state.revision,
        "candidate_count": len(state.candidates),
        "providers": sorted({item.source for item in state.candidates}),
        "observed_at": observed_at,
    }
    event["entry_sha256"] = _json_sha256(event)
    payload["candidate_generation_history"] = [
        *(copy.deepcopy(item) for item in history),
        event,
    ][-_MAX_ATTEMPT_HISTORY:]


def _candidate_pool_map(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CandidateContractError("candidate_pool must be an object")
    if "shot_id" in value and "candidates" in value:
        shot_id = value.get("shot_id")
        if not isinstance(shot_id, str) or not shot_id.strip():
            raise CandidateContractError("candidate_pool shot_id is required")
        return {shot_id.strip(): copy.deepcopy(dict(value))}
    pools: dict[str, dict[str, Any]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise CandidateContractError("candidate_pool keys must be shot IDs")
        if not isinstance(item, Mapping):
            raise CandidateContractError("candidate_pool values must be objects")
        pools[key.strip()] = copy.deepcopy(dict(item))
    return pools


def _finish_payload(
    payload: dict[str, Any],
    *,
    expected: Mapping[str, Any],
    status: str,
    publication_phase: str,
    observed_at: str,
) -> None:
    next_material_task_id = _next_waiting_task_id(payload)
    if next_material_task_id is not None:
        next_task = _owned_task(payload, next_material_task_id)
        next_task["status"] = "待执行"
        _append_task_status(next_task, "待执行")
    for field in _ACTIVE_OWNER_FIELDS:
        payload[field] = None
    payload.update(
        {
            "publication_phase": publication_phase,
            "execution_state": "idle",
            "last_execution_status": status,
            "execution_completed_at": observed_at,
            "blocked_reason": "",
            "execution_revision": _required_revision(expected) + 1,
            "next_material_task_id": next_material_task_id,
        }
    )


def _next_waiting_task_id(payload: Mapping[str, Any]) -> str | None:
    tasks = payload.get("material_tasks")
    if not isinstance(tasks, list):
        raise MaterialExecutionCoordinatorUnknown("material task list is missing")
    for item in tasks:
        if not isinstance(item, Mapping):
            raise MaterialExecutionCoordinatorUnknown("material task item is invalid")
        task_id = item.get("material_task_id")
        if (
            type(task_id) is str
            and task_id
            and item.get("status") in _WAITING_STATUSES
            and not item.get("material_path")
        ):
            return task_id
    return None


def _append_task_status(task: dict[str, Any], status: str) -> None:
    history = task.get("status_history")
    if history is None:
        history = []
    if not isinstance(history, list) or any(type(item) is not str for item in history):
        raise MaterialExecutionCoordinatorUnknown("material task status history is invalid")
    if not history or history[-1] != status:
        history.append(status)
    task["status_history"] = history[-20:]


def _assert_terminal_receipt_payload(
    payload: Mapping[str, Any],
    receipt: MaterialCompletionReceipt,
    claim: PreparedMaterialClaim,
) -> None:
    if (
        payload.get("execution_id") != receipt.execution_id
        or _required_revision(payload) != receipt.execution_revision
        or _json_sha256(payload) != receipt.final_payload_sha256
        or payload.get("execution_state") != "running"
        or payload.get("publication_phase") != "rolled_back"
        or payload.get("attempt_transition") != "retiring"
        or any(payload.get(field) not in (None, "") for field in _ACTIVE_OWNER_FIELDS)
    ):
        raise MaterialExecutionCoordinatorUnknown(
            "terminal receipt does not match the cleared retiring payload"
        )
    history = payload.get("material_attempt_history")
    if not isinstance(history, list):
        raise MaterialExecutionCoordinatorUnknown(
            "terminal receipt payload has no attempt history"
        )
    matches = [
        item
        for item in history
        if isinstance(item, Mapping)
        and item.get("entry_sha256") == receipt.attempt_history_sha256
    ]
    if len(matches) != 1:
        raise MaterialExecutionCoordinatorUnknown(
            "terminal receipt history is not unique in the cleared payload"
        )
    entry = dict(matches[0])
    entry_sha256 = entry.pop("entry_sha256", None)
    if (
        entry_sha256 != receipt.attempt_history_sha256
        or _json_sha256(entry) != receipt.attempt_history_sha256
        or entry.get("material_task_id") != claim.binding.material_task_id
        or entry.get("execution_id") != receipt.execution_id
        or entry.get("attempt_id") != receipt.attempt_id
        or entry.get("operation") != receipt.operation
        or entry.get("status") != receipt.status
        or entry.get("observed_at") != receipt.observed_at
        or entry.get("result_sha256") != receipt.result_sha256
    ):
        raise MaterialExecutionCoordinatorUnknown(
            "terminal receipt history does not bind to the cleared attempt"
        )


def _required_revision(payload: Mapping[str, Any]) -> int:
    revision = payload.get("execution_revision")
    if type(revision) is not int or revision <= 0:
        raise MaterialExecutionCoordinatorUnknown("execution revision is invalid")
    return revision


def _json_clone(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("payload must be a JSON object")
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
        raise TypeError("payload must contain only JSON values") from exc
    if not isinstance(decoded, dict):
        raise TypeError("payload must be a JSON object")
    return decoded


def _json_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_uuid(value: object, label: str) -> None:
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError(f"{label} must be a canonical UUID")


def _validate_sha256(value: object, label: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise MaterialExecutionCoordinatorUnknown(
            f"{label} is not a lowercase SHA-256 receipt"
        )


def _release_preclaim_mutex_or_unknown(
    mutex: Any, *, prepared: PreparedMaterialAttempt | None
) -> None:
    try:
        mutex.release()
    except BaseException as exc:
        if prepared is None:
            raise MaterialExecutionCoordinatorUnknown(
                "pre-claim mutex release acknowledgement is unknown"
            ) from exc
        recovery = MaterialPreparedClaimRecovery(
            prepared=prepared,
            mutex=mutex,
            phase="cleanup_unknown",
        )
        raise MaterialPrepareAndClaimUnknown(
            "pre-claim mutex release acknowledgement is unknown",
            recovery=recovery,
        ) from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


__all__ = [
    "ClaimLeasePublicationDatabase",
    "MaterialAttemptRotationReceipt",
    "MaterialExecutionClaimPreparer",
    "PendingMaterialRetirementClaim",
    "MaterialRestartRecoveryCoordinator",
    "MaterialRestartRecoveryUnknown",
    "MaterialPrepareAndClaimUnknown",
    "MaterialPreparedClaimRecovery",
    "completion_receipt_from_pending_payload",
    "restart_published_retirement_proof_from_completion",
    "restart_retirement_proof_from_completion",
    "SqlAlchemyMaterialLifecycleAdapter",
]
