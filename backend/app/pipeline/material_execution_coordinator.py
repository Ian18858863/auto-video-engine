"""Typed lifecycle coordinator contract for the next material execution route.

The legacy material executor is intentionally not imported here.  This module
only provides the order-sensitive coordinator that a production adapter must
implement after the material-task identity/status migration is complete.
Keeping the contract separate prevents a partial wrapper around
``AutoMaterialTaskExecutor`` from being mistaken for an atomic claim/runner/
publisher route.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal, Protocol, Sequence

from app.pipeline.material_artifact_publisher import (
    CanonicalMaterialPublication,
    PublicationRejected,
)
from app.pipeline.material_operation_protocol import (
    MaterialOperationBlocked,
    MaterialOperationKind,
    MaterialOperationOutcome,
    MaterialOperationRequest,
    MaterialOperationSecrets,
    MaterialWorkerResult,
)
from app.pipeline.material_operation_runner import (
    MaterialAttemptRetirementReceipt,
    MaterialRestartPublishedRetirementProof,
    MaterialRestartRetirementProof,
    MaterialRunnerClaimBinding,
    PreparedMaterialAttempt,
    ProcessMaterialOperationRunner,
)
from app.pipeline.material_runner_retirement import (
    MaterialRunnerRetirementAcknowledgement,
    MaterialRunnerRetirementAttestation,
)


CoordinatorStatus = Literal["succeeded", "failed", "terminated", "blocked"]


class MaterialExecutionCoordinatorError(RuntimeError):
    """A lifecycle transition could not be completed safely."""


class MaterialExecutionCoordinatorUnknown(MaterialExecutionCoordinatorError):
    """A side effect was acknowledged ambiguously; evidence must be audited."""


class MaterialClaimLease(Protocol):
    """The small lease surface the coordinator is allowed to use."""

    execution_id: str
    active_attempt_id: str
    active_control_id: str
    active_job_instance_id: str
    expected_revision: int

    def assert_owned(self, session: object | None = None) -> None:
        ...

    def release(self) -> None:
        ...


@dataclass(frozen=True, slots=True)
class PreparedMaterialClaim:
    """The three objects that must travel together through one attempt."""

    prepared: PreparedMaterialAttempt
    binding: MaterialRunnerClaimBinding
    lease: MaterialClaimLease

    def __post_init__(self) -> None:
        if type(self.prepared) is not PreparedMaterialAttempt:
            raise TypeError("prepared must be PreparedMaterialAttempt")
        if type(self.binding) is not MaterialRunnerClaimBinding:
            raise TypeError("binding must be MaterialRunnerClaimBinding")
        if (
            self.prepared.execution_id != self.binding.execution_id
            or self.prepared.attempt_id != self.binding.attempt_id
            or self.prepared.control_id != self.binding.control_id
            or self.prepared.job_instance_id != self.binding.job_instance_id
        ):
            raise ValueError("prepared attempt and claim binding identities differ")
        if (
            self.lease.execution_id != self.binding.execution_id
            or self.lease.active_attempt_kind != self.binding.operation
            or self.lease.active_attempt_id != self.binding.attempt_id
            or self.lease.active_control_id != self.binding.control_id
            or self.lease.active_job_instance_id != self.binding.job_instance_id
        ):
            raise ValueError("claim lease and binding identities differ")
        if self.lease.expected_revision != self.binding.execution_revision:
            raise ValueError("claim lease and binding revisions differ")


@dataclass(frozen=True, slots=True)
class MaterialCompletionReceipt:
    """Typed proof returned by the final whole-blob completion CAS."""

    status: CoordinatorStatus
    execution_id: str
    operation: MaterialOperationKind
    attempt_id: str
    execution_revision: int
    final_payload_sha256: str
    publication_journal_sha256: str | None = None
    attempt_history_sha256: str | None = None
    result_sha256: str | None = None
    termination_outcome_sha256: str | None = None
    publication_intent_sha256: str | None = None
    publication_result_sha256: str | None = None
    publication_worker_result_sha256: str | None = None
    publication_reference_base_revision: int | None = None
    publication_reference_base_payload_sha256: str | None = None
    runner_retirement_checkpoint_sha256: str | None = None
    observed_at: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed", "terminated", "blocked"}:
            raise ValueError("completion status is not allowed")
        _validate_uuid(self.execution_id, "execution_id")
        if self.operation not in {
            "official_capture",
            "pexels",
            "pixabay",
            "coverr",
            "info_motion",
            "stock_candidates",
        }:
            raise ValueError("completion operation is not allowed")
        _validate_uuid(self.attempt_id, "attempt_id")
        if (
            type(self.execution_revision) is not int
            or self.execution_revision <= 0
        ):
            raise ValueError("execution_revision must be a positive integer")
        _validate_sha256(self.final_payload_sha256, "final_payload_sha256")
        for value, label in (
            (self.publication_journal_sha256, "publication_journal_sha256"),
            (self.attempt_history_sha256, "attempt_history_sha256"),
            (self.result_sha256, "result_sha256"),
            (self.termination_outcome_sha256, "termination_outcome_sha256"),
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
            (
                self.runner_retirement_checkpoint_sha256,
                "runner_retirement_checkpoint_sha256",
            ),
        ):
            if value is not None:
                _validate_sha256(value, label)
        if self.publication_reference_base_revision is not None and (
            type(self.publication_reference_base_revision) is not int
            or isinstance(self.publication_reference_base_revision, bool)
            or self.publication_reference_base_revision <= 0
        ):
            raise ValueError(
                "publication_reference_base_revision must be a positive integer"
            )
        recovery_receipts = (
            self.publication_intent_sha256,
            self.publication_result_sha256,
            self.publication_worker_result_sha256,
            self.publication_reference_base_revision,
            self.publication_reference_base_payload_sha256,
        )
        if any(value is not None for value in recovery_receipts) and any(
            value is None for value in recovery_receipts
        ):
            raise ValueError("publication recovery receipts must be complete")
        if any(value is not None for value in recovery_receipts):
            if self.status != "succeeded":
                raise ValueError(
                    "only successful completion may carry publication recovery receipts"
                )
            if self.publication_worker_result_sha256 != self.result_sha256:
                raise ValueError(
                    "publication worker receipt must match the completion result"
                )
        if self.status == "succeeded":
            if self.publication_journal_sha256 is None or self.result_sha256 is None:
                raise ValueError(
                    "successful completion requires publication and result receipts"
                )
        elif self.status in {"failed", "terminated"}:
            if self.attempt_history_sha256 is None:
                raise ValueError("terminal completion requires an attempt history receipt")
        elif any(
            value is not None
            for value in (
                self.publication_journal_sha256,
                self.attempt_history_sha256,
                self.result_sha256,
            )
        ):
            raise ValueError("blocked completion cannot claim a final publication/history")
        if self.observed_at:
            try:
                parsed = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("observed_at must be ISO-8601") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("observed_at must include a timezone")


class MaterialLifecycleAdapter(Protocol):
    """Database/recovery side of the coordinator.

    Implementations must use an independent fresh-session CAS store.  The
    methods are intentionally typed around receipts rather than naked hashes.
    """

    def complete_success(
        self,
        claim: PreparedMaterialClaim,
        *,
        outcome: MaterialOperationOutcome,
        publication: CanonicalMaterialPublication,
    ) -> MaterialCompletionReceipt:
        ...

    def complete_terminal(
        self,
        claim: PreparedMaterialClaim,
        *,
        outcome: MaterialOperationOutcome,
    ) -> MaterialCompletionReceipt:
        ...

    def record_blocked(
        self,
        claim: PreparedMaterialClaim,
        *,
        reason: str,
    ) -> None:
        ...

    def acknowledge_runner_retirement(
        self,
        claim: PreparedMaterialClaim,
        *,
        completion: MaterialCompletionReceipt,
        attestation: MaterialRunnerRetirementAttestation,
    ) -> MaterialRunnerRetirementAcknowledgement:
        ...


class MaterialFallbackLifecycleAdapter(MaterialLifecycleAdapter, Protocol):
    """Additional gates required by a same-execution source chain."""

    def record_terminal_attempt_for_fallback(
        self,
        claim: PreparedMaterialClaim,
        *,
        outcome: MaterialOperationOutcome,
    ) -> MaterialCompletionReceipt:
        ...

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
    ) -> object:
        ...

    def finalize_terminal_attempt_after_fallback(
        self,
        claim: PreparedMaterialClaim,
        *,
        terminal_receipt: MaterialCompletionReceipt,
        retirement_receipt: MaterialAttemptRetirementReceipt,
    ) -> MaterialCompletionReceipt:
        ...


class MaterialPublicationFactory(Protocol):
    def __call__(
        self,
        claim: PreparedMaterialClaim,
        request: MaterialOperationRequest,
        outcome: MaterialOperationOutcome,
    ) -> CanonicalMaterialPublication:
        ...


@dataclass(frozen=True, slots=True)
class MaterialCoordinatorResult:
    status: CoordinatorStatus
    outcome: MaterialOperationOutcome | None
    completion: MaterialCompletionReceipt | None


class MaterialExecutionCoordinator:
    """Run one already-prepared claim through the only safe release order.

    This class is an integration contract, not a production route.  A caller
    must obtain :class:`PreparedMaterialClaim` from a future
    ``prepare_and_claim`` adapter while holding the material-step mutex.  It
    deliberately has no fallback loop: a new source requires a new typed
    attempt transition.
    """

    def __init__(
        self,
        *,
        runner: ProcessMaterialOperationRunner,
        lifecycle: MaterialLifecycleAdapter,
        publication_factory: MaterialPublicationFactory,
    ) -> None:
        if not all(
            callable(getattr(runner, name, None))
            for name in ("run_claimed", "retire_published_attempt", "retire_terminal_attempt")
        ):
            raise TypeError("runner does not expose the contained lifecycle surface")
        if not callable(publication_factory):
            raise TypeError("publication_factory must be callable")
        self.runner = runner
        self.lifecycle = lifecycle
        self.publication_factory = publication_factory

    def run_once(
        self,
        claim: PreparedMaterialClaim,
        request: MaterialOperationRequest,
        secrets: MaterialOperationSecrets,
        *,
        startup_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> MaterialCoordinatorResult:
        """Execute one attempt and preserve evidence on every unknown edge."""

        outcome = self.run_claimed_attempt(
            claim,
            request,
            secrets,
            startup_timeout_seconds=startup_timeout_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
        )

        if outcome.status == "succeeded":
            if outcome.result is None:
                raise MaterialExecutionCoordinatorUnknown(
                    "successful runner outcome has no worker result"
                )
            try:
                publication = self.publication_factory(
                    claim,
                    request,
                    outcome,
                )
            except PublicationRejected:
                outcome = _publication_rejected_outcome(outcome)
            else:
                try:
                    publication.commit(retain_ownership=True)
                    completion = self.lifecycle.complete_success(
                        claim,
                        outcome=outcome,
                        publication=publication,
                    )
                    self._assert_completion_matches_claim(claim, completion)
                    if completion.status != "succeeded":
                        raise MaterialExecutionCoordinatorUnknown(
                            "success completion CAS returned a non-success receipt"
                        )
                    if completion.publication_journal_sha256 is None:
                        raise MaterialExecutionCoordinatorUnknown(
                            "success completion receipt has no publication journal hash"
                        )
                    attestation = self.runner.attest_completed_published_attempt(
                        claim.prepared,
                        proof=_published_retirement_proof(completion),
                    )
                    self._ack_runner_retirement(claim, completion, attestation)
                    publication.release_after_retirement()
                except BaseException:
                    # Do not release retained publisher pins or the claim lease on
                    # an unknown completion/retirement edge. Recovery must adopt
                    # the durable evidence first.
                    raise
                claim.lease.release()
                return MaterialCoordinatorResult(
                    status="succeeded",
                    outcome=outcome,
                    completion=completion,
                )

        if outcome.status in {"failed", "terminated"}:
            completion = self.lifecycle.complete_terminal(
                claim,
                outcome=outcome,
            )
            self._assert_completion_matches_claim(claim, completion)
            if completion.status != outcome.status:
                raise MaterialExecutionCoordinatorUnknown(
                    "terminal completion receipt does not match runner outcome"
                )
            if completion.attempt_history_sha256 is None:
                raise MaterialExecutionCoordinatorUnknown(
                    "terminal completion receipt has no history hash"
                )
            attestation = self.runner.attest_completed_terminal_attempt(
                claim.prepared,
                proof=_terminal_retirement_proof(completion),
            )
            self._ack_runner_retirement(claim, completion, attestation)
            claim.lease.release()
            return MaterialCoordinatorResult(
                status=outcome.status,
                outcome=outcome,
                completion=completion,
            )

        raise MaterialExecutionCoordinatorUnknown(
            f"runner returned unsupported outcome status: {outcome.status!r}"
        )

    def run_claimed_attempt(
        self,
        claim: PreparedMaterialClaim,
        request: MaterialOperationRequest,
        secrets: MaterialOperationSecrets,
        *,
        startup_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> MaterialOperationOutcome:
        """Run one bound attempt without choosing its terminal transition.

        Production fallback orchestration uses this seam so a failed or
        confirmed-terminated attempt can first pass through the explicit
        DB-clear -> runner-retirement -> next-attempt rotation gates.  The
        existing :meth:`run_once` remains the final single-attempt adapter.
        """

        try:
            claim.lease.assert_owned()
        except BaseException as exc:
            raise MaterialExecutionCoordinatorUnknown(
                "material claim mutex is not owned by this coordinator thread"
            ) from exc

        if request.execution_id != claim.binding.execution_id:
            raise ValueError("request execution_id does not match the claim")
        if request.attempt_id != claim.binding.attempt_id:
            raise ValueError("request attempt_id does not match the claim")
        if secrets.execution_id != claim.binding.execution_id:
            raise ValueError("secret execution_id does not match the claim")
        if secrets.attempt_id != claim.binding.attempt_id:
            raise ValueError("secret attempt_id does not match the claim")

        try:
            outcome = self.runner.run_claimed(
                claim.prepared,
                request,
                binding=claim.binding,
                secrets=secrets,
                startup_timeout_seconds=startup_timeout_seconds,
                operation_timeout_seconds=operation_timeout_seconds,
            )
        except BaseException as exc:
            # A blocked/unknown runner owns durable evidence.  Do not call a
            # terminal retirement seam without a typed completion receipt.
            if isinstance(exc, MaterialOperationBlocked):
                try:
                    self.lifecycle.record_blocked(claim, reason=str(exc))
                except BaseException as record_error:
                    raise MaterialExecutionCoordinatorUnknown(
                        "blocked outcome could not be recorded"
                    ) from record_error
            raise
        return outcome

    def run_with_fallback(
        self,
        claim: PreparedMaterialClaim,
        request: MaterialOperationRequest,
        secrets: MaterialOperationSecrets,
        *,
        fallback_operations: Sequence[MaterialOperationKind],
        prepare_next_attempt: Callable[
            [MaterialOperationKind, str], PreparedMaterialAttempt
        ],
        bind_rotated_attempt: Callable[
            [PreparedMaterialClaim, PreparedMaterialAttempt, MaterialOperationKind],
            PreparedMaterialClaim,
        ],
        request_factory: Callable[
            [MaterialOperationKind, PreparedMaterialAttempt],
            tuple[MaterialOperationRequest, MaterialOperationSecrets],
        ],
        startup_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> MaterialCoordinatorResult:
        """Run a same-execution source chain behind explicit proof gates.

        A terminal source is never replaced in one database write.  The
        order is fixed and observable: terminal history/CAS clear, runner
        retirement receipt, prepare the next unclaimed roots, rotation CAS,
        then bind and launch the next worker.  A blocked or unknown edge is
        raised with the current lease and all evidence still owned by the
        caller's lane.
        """

        if type(fallback_operations) not in (tuple, list):
            raise TypeError("fallback_operations must be a sequence")
        operations = tuple(fallback_operations)
        for operation in operations:
            if operation not in {
                "official_capture",
                "pexels",
                "pixabay",
                "coverr",
                "info_motion",
            }:
                raise ValueError("fallback operation is not allowed")
        if not callable(prepare_next_attempt):
            raise TypeError("prepare_next_attempt must be callable")
        if not callable(bind_rotated_attempt):
            raise TypeError("bind_rotated_attempt must be callable")
        if not callable(request_factory):
            raise TypeError("request_factory must be callable")

        current_claim = claim
        current_request = request
        current_secrets = secrets
        operation_index = 0
        while True:
            outcome = self.run_claimed_attempt(
                current_claim,
                current_request,
                current_secrets,
                startup_timeout_seconds=startup_timeout_seconds,
                operation_timeout_seconds=operation_timeout_seconds,
            )
            if outcome.status == "succeeded":
                if outcome.result is None:
                    raise MaterialExecutionCoordinatorUnknown(
                        "successful runner outcome has no worker result"
                    )
                try:
                    publication = self.publication_factory(
                        current_claim,
                        current_request,
                        outcome,
                    )
                except PublicationRejected:
                    outcome = _publication_rejected_outcome(outcome)
                else:
                    try:
                        publication.commit(retain_ownership=True)
                        completion = self.lifecycle.complete_success(
                            current_claim,
                            outcome=outcome,
                            publication=publication,
                        )
                        self._assert_completion_matches_claim(current_claim, completion)
                        if completion.status != "succeeded":
                            raise MaterialExecutionCoordinatorUnknown(
                                "success completion CAS returned a non-success receipt"
                            )
                        if completion.publication_journal_sha256 is None:
                            raise MaterialExecutionCoordinatorUnknown(
                                "success completion receipt has no publication journal hash"
                            )
                        attestation = self.runner.attest_completed_published_attempt(
                            current_claim.prepared,
                            proof=_published_retirement_proof(completion),
                        )
                        self._ack_runner_retirement(
                            current_claim,
                            completion,
                            attestation,
                        )
                        publication.release_after_retirement()
                    except BaseException:
                        raise
                    current_claim.lease.release()
                    return MaterialCoordinatorResult(
                        status="succeeded",
                        outcome=outcome,
                        completion=completion,
                    )

            if outcome.status not in {"failed", "terminated"}:
                raise MaterialExecutionCoordinatorUnknown(
                    f"runner returned unsupported outcome status: {outcome.status!r}"
                )

            terminal_receipt_factory = getattr(
                self.lifecycle, "record_terminal_attempt_for_fallback", None
            )
            if not callable(terminal_receipt_factory):
                raise MaterialExecutionCoordinatorUnknown(
                    "lifecycle does not expose the fallback terminal CAS"
                )
            terminal_receipt = terminal_receipt_factory(
                current_claim,
                outcome=outcome,
            )
            if type(terminal_receipt) is not MaterialCompletionReceipt:
                raise MaterialExecutionCoordinatorUnknown(
                    "fallback terminal CAS did not return a typed receipt"
                )
            self._assert_completion_matches_claim(current_claim, terminal_receipt)
            if terminal_receipt.status != outcome.status:
                raise MaterialExecutionCoordinatorUnknown(
                    "fallback terminal receipt does not match runner outcome"
                )
            proof = MaterialRestartRetirementProof(
                status=terminal_receipt.status,
                execution_id=terminal_receipt.execution_id,
                operation=terminal_receipt.operation,
                attempt_id=terminal_receipt.attempt_id,
                execution_revision=terminal_receipt.execution_revision,
                final_payload_sha256=terminal_receipt.final_payload_sha256,
                attempt_history_sha256=terminal_receipt.attempt_history_sha256 or "",
                result_sha256=terminal_receipt.result_sha256,
                observed_at=terminal_receipt.observed_at,
            )
            retire_with_proof = getattr(
                self.runner, "retire_terminal_attempt_with_proof", None
            )
            if not callable(retire_with_proof):
                raise MaterialExecutionCoordinatorUnknown(
                    "runner does not expose post-runner retirement proof"
                )
            retirement_receipt = retire_with_proof(
                current_claim.prepared,
                proof=proof,
            )
            if type(retirement_receipt) is not MaterialAttemptRetirementReceipt:
                raise MaterialExecutionCoordinatorUnknown(
                    "runner retirement did not return a typed receipt"
                )
            if (
                retirement_receipt.status != proof.status
                or retirement_receipt.execution_id != proof.execution_id
                or retirement_receipt.operation != proof.operation
                or retirement_receipt.attempt_id != proof.attempt_id
                or retirement_receipt.execution_revision != proof.execution_revision
                or retirement_receipt.final_payload_sha256
                != proof.final_payload_sha256
                or retirement_receipt.attempt_history_sha256
                != proof.attempt_history_sha256
                or retirement_receipt.result_sha256 != proof.result_sha256
                or retirement_receipt.observed_at != proof.observed_at
                or retirement_receipt.retired is not True
            ):
                raise MaterialExecutionCoordinatorUnknown(
                    "runner retirement receipt does not bind the terminal proof"
                )

            if operation_index >= len(operations):
                finalize = getattr(
                    self.lifecycle, "finalize_terminal_attempt_after_fallback", None
                )
                if not callable(finalize):
                    raise MaterialExecutionCoordinatorUnknown(
                        "lifecycle does not expose exhausted-chain finalization"
                    )
                completion = finalize(
                    current_claim,
                    terminal_receipt=terminal_receipt,
                    retirement_receipt=retirement_receipt,
                )
                self._assert_completion_matches_claim(current_claim, completion)
                current_claim.lease.release()
                return MaterialCoordinatorResult(
                    status=outcome.status,
                    outcome=outcome,
                    completion=completion,
                )

            next_operation = operations[operation_index]
            operation_index += 1
            next_prepared = prepare_next_attempt(
                next_operation,
                current_claim.binding.execution_id,
            )
            if type(next_prepared) is not PreparedMaterialAttempt:
                raise MaterialExecutionCoordinatorUnknown(
                    "next-attempt factory did not return PreparedMaterialAttempt"
                )
            if next_prepared.execution_id != current_claim.binding.execution_id:
                raise MaterialExecutionCoordinatorUnknown(
                    "next attempt changed the execution identity"
                )
            rotation = getattr(self.lifecycle, "rotate_terminal_attempt", None)
            if not callable(rotation):
                raise MaterialExecutionCoordinatorUnknown(
                    "lifecycle does not expose proof-gated attempt rotation"
                )
            rotation(
                current_claim,
                terminal_receipt=terminal_receipt,
                retirement_receipt=retirement_receipt,
                attempt_id=next_prepared.attempt_id,
                attempt_kind=next_operation,
                control_id=next_prepared.control_id,
                job_instance_id=next_prepared.job_instance_id,
            )
            next_claim = bind_rotated_attempt(
                current_claim,
                next_prepared,
                next_operation,
            )
            next_request, next_secrets = request_factory(next_operation, next_prepared)
            current_claim = next_claim
            current_request = next_request
            current_secrets = next_secrets

    @staticmethod
    def _assert_completion_matches_claim(
        claim: PreparedMaterialClaim,
        completion: MaterialCompletionReceipt,
    ) -> None:
        if type(completion) is not MaterialCompletionReceipt:
            raise MaterialExecutionCoordinatorUnknown(
                "lifecycle adapter did not return a typed completion receipt"
            )
        if (
            completion.execution_id != claim.binding.execution_id
            or completion.operation != claim.binding.operation
            or completion.attempt_id != claim.binding.attempt_id
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "completion receipt identities do not match the active claim"
            )
        if completion.execution_revision <= claim.binding.execution_revision:
            raise MaterialExecutionCoordinatorUnknown(
                "completion receipt revision did not advance the claim"
            )

    def _ack_runner_retirement(
        self,
        claim: PreparedMaterialClaim,
        completion: MaterialCompletionReceipt,
        attestation: object,
    ) -> MaterialRunnerRetirementAcknowledgement:
        if type(attestation) is not MaterialRunnerRetirementAttestation:
            raise MaterialExecutionCoordinatorUnknown(
                "runner retirement returned no typed attestation"
            )
        acknowledge = getattr(self.lifecycle, "acknowledge_runner_retirement", None)
        if not callable(acknowledge):
            raise MaterialExecutionCoordinatorUnknown(
                "lifecycle does not expose the runner retirement ack CAS"
            )
        receipt = acknowledge(
            claim,
            completion=completion,
            attestation=attestation,
        )
        if type(receipt) is not MaterialRunnerRetirementAcknowledgement:
            raise MaterialExecutionCoordinatorUnknown(
                "runner retirement ack returned no typed receipt"
            )
        if (
            receipt.completion_revision != completion.execution_revision
            or receipt.completion_payload_sha256 != completion.final_payload_sha256
            or receipt.checkpoint_sha256
            != completion.runner_retirement_checkpoint_sha256
            or receipt.tombstone_sha256 != attestation.tombstone_sha256
        ):
            raise MaterialExecutionCoordinatorUnknown(
                "runner retirement ack receipt differs from completion evidence"
            )
        return receipt


def _terminal_retirement_proof(
    completion: MaterialCompletionReceipt,
) -> MaterialRestartRetirementProof:
    if (
        completion.status not in {"failed", "terminated"}
        or completion.attempt_history_sha256 is None
        or completion.runner_retirement_checkpoint_sha256 is None
        or not completion.observed_at
    ):
        raise MaterialExecutionCoordinatorUnknown(
            "terminal completion lacks runner retirement receipts"
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
        checkpoint_sha256=completion.runner_retirement_checkpoint_sha256,
    )


def _published_retirement_proof(
    completion: MaterialCompletionReceipt,
) -> MaterialRestartPublishedRetirementProof:
    required = (
        completion.publication_journal_sha256,
        completion.attempt_history_sha256,
        completion.result_sha256,
        completion.publication_intent_sha256,
        completion.publication_result_sha256,
        completion.publication_worker_result_sha256,
        completion.publication_reference_base_revision,
        completion.publication_reference_base_payload_sha256,
        completion.runner_retirement_checkpoint_sha256,
        completion.observed_at,
    )
    if completion.status != "succeeded" or any(value in (None, "") for value in required):
        raise MaterialExecutionCoordinatorUnknown(
            "successful completion lacks runner retirement receipts"
        )
    assert completion.publication_journal_sha256 is not None
    assert completion.attempt_history_sha256 is not None
    assert completion.result_sha256 is not None
    assert completion.publication_intent_sha256 is not None
    assert completion.publication_result_sha256 is not None
    assert completion.publication_worker_result_sha256 is not None
    assert completion.publication_reference_base_revision is not None
    assert completion.publication_reference_base_payload_sha256 is not None
    assert completion.runner_retirement_checkpoint_sha256 is not None
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
        checkpoint_sha256=completion.runner_retirement_checkpoint_sha256,
    )


def _validate_uuid(value: object, label: str) -> None:
    from uuid import UUID

    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical UUID")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UUID") from exc


def _publication_rejected_outcome(
    outcome: MaterialOperationOutcome,
) -> MaterialOperationOutcome:
    result = outcome.result
    if outcome.status != "succeeded" or type(result) is not MaterialWorkerResult:
        raise MaterialExecutionCoordinatorUnknown(
            "publication rejection does not bind a successful worker result"
        )
    failed_result = MaterialWorkerResult(
        protocol_version=result.protocol_version,
        execution_id=result.execution_id,
        attempt_id=result.attempt_id,
        operation=result.operation,
        status="failed",
        selected_artifact="",
        artifacts=(),
        reason_code="publication_rejected",
        reason=(
            "parent publication rejected staged media before any durable "
            "publication write"
        ),
    )
    return MaterialOperationOutcome(
        status="failed",
        result=failed_result,
        termination=outcome.termination,
    )


def _validate_sha256(value: object, label: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


__all__ = [
    "MaterialCompletionReceipt",
    "MaterialCoordinatorResult",
    "MaterialExecutionCoordinator",
    "MaterialExecutionCoordinatorError",
    "MaterialExecutionCoordinatorUnknown",
    "MaterialLifecycleAdapter",
    "MaterialFallbackLifecycleAdapter",
    "MaterialClaimLease",
    "MaterialPublicationFactory",
    "PreparedMaterialClaim",
]
