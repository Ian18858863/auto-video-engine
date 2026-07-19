"""Route-free HTTP-shaped adapter for durable material submissions.

This module intentionally contains no FastAPI router and does not alter the
legacy ``/api/pipeline/{pipeline_id}/material-tasks/run-next`` endpoint.  It
gives a future route a small, testable translation boundary:

* submission returns a JSON-safe ``202``-shaped result without waiting;
* status reads only the durable coordinator projection;
* reconcile is explicit and never turns ``unknown`` into an automatic retry;
* internal handles, sessions and raw owner-lane state never enter a response.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping

from app.pipeline.material_execution_application import (
    MaterialExecutionLaneError,
)
from app.pipeline.material_execution_submission_coordinator import (
    MaterialExecutionSubmissionCoordinator,
    MaterialExecutionSubmissionCoordinatorError,
    MaterialExecutionSubmissionReconcileRejected,
    MaterialExecutionSubmissionRetryRejected,
    MaterialExecutionSubmissionStatus,
)
from app.pipeline.material_execution_submission_owner_lease import (
    MaterialExecutionOwnerLeaseBusy,
    MaterialExecutionOwnerLeaseError,
    MaterialExecutionOwnerLeaseUnavailable,
)
from app.pipeline.material_execution_submission_store import (
    MaterialExecutionSubmissionError,
    MaterialExecutionSubmissionIdentityConflict,
    MaterialExecutionSubmissionNotFound,
    MaterialExecutionSubmissionPathError,
    MaterialExecutionSubmissionSchemaError,
    MaterialExecutionSubmissionStateConflict,
    MaterialExecutionSubmissionTransitionError,
)
from app.pipeline.material_execution_api_context import (
    MaterialExecutionSubmissionContext,
)


_TERMINAL_STATES = frozenset({"succeeded", "failed"})
_MAX_DETAIL = 2000
_WINDOWS_PATH_RE = re.compile(r"(?i)(?:[A-Z]:[\\/]|\\\\)[^\s<>\"']+")


def _copy_json(value: Any) -> Any:
    try:
        return json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TypeError("submission API result is not JSON-safe") from exc


def _detail(exc: BaseException) -> str:
    try:
        value = str(exc).strip() or "operation failed"
    except BaseException:  # pragma: no cover - hostile exception text
        value = "operation failed"
    return _WINDOWS_PATH_RE.sub("[path]", value)[:_MAX_DETAIL]


@dataclass(frozen=True, slots=True)
class MaterialExecutionHttpResult:
    """A framework-neutral status/body pair for a future HTTP route."""

    status_code: int
    body: Mapping[str, Any]

    def __post_init__(self) -> None:
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise ValueError("status_code must be a valid HTTP status")
        copied = _copy_json(dict(self.body))
        if not isinstance(copied, dict):  # pragma: no cover - defensive
            raise TypeError("HTTP body must be a JSON object")
        object.__setattr__(self, "body", copied)

    def to_json(self) -> dict[str, Any]:
        value = {"status_code": self.status_code, "body": self.body}
        copied = _copy_json(value)
        if not isinstance(copied, dict):  # pragma: no cover - defensive
            raise TypeError("HTTP result must be a JSON object")
        return copied


def _status_result(status: MaterialExecutionSubmissionStatus) -> MaterialExecutionHttpResult:
    code = 200 if status.state in _TERMINAL_STATES else 202
    return MaterialExecutionHttpResult(code, status.to_json())


def _error_result(status_code: int, code: str, detail: str, *, recovery_required: bool = False) -> MaterialExecutionHttpResult:
    return MaterialExecutionHttpResult(
        status_code,
        {
            "error": {
                "code": code,
                "detail": detail[:_MAX_DETAIL],
                "retryable": False,
                "recovery_required": recovery_required,
            }
        },
    )


def _map_error(exc: BaseException) -> MaterialExecutionHttpResult:
    if isinstance(exc, MaterialExecutionOwnerLeaseBusy):
        return _error_result(
            409,
            "material_submission_owner_busy",
            "submission owner is already active; retry after the current owner releases it",
            recovery_required=True,
        )
    if isinstance(exc, MaterialExecutionOwnerLeaseUnavailable):
        return _error_result(
            503,
            "material_submission_owner_unavailable",
            "submission owner is temporarily unavailable",
            recovery_required=True,
        )
    if isinstance(exc, MaterialExecutionOwnerLeaseError):
        return _error_result(
            503,
            "material_submission_owner_error",
            "submission owner could not be established",
            recovery_required=True,
        )
    if isinstance(exc, MaterialExecutionSubmissionNotFound):
        return _error_result(404, "material_submission_not_found", _detail(exc))
    if isinstance(exc, (MaterialExecutionSubmissionRetryRejected, MaterialExecutionSubmissionReconcileRejected)):
        return _error_result(409, "material_submission_reconcile_required", _detail(exc), recovery_required=True)
    if isinstance(exc, (MaterialExecutionSubmissionIdentityConflict, MaterialExecutionSubmissionStateConflict, MaterialExecutionSubmissionTransitionError)):
        return _error_result(409, "material_submission_conflict", _detail(exc), recovery_required=True)
    if isinstance(exc, (ValueError, TypeError)):
        return _error_result(422, "invalid_material_submission_request", _detail(exc))
    if isinstance(exc, (MaterialExecutionSubmissionSchemaError, MaterialExecutionSubmissionPathError)):
        return _error_result(
            503,
            "material_submission_store_unavailable",
            "submission store is unavailable",
            recovery_required=True,
        )
    if isinstance(exc, (MaterialExecutionLaneError, MaterialExecutionSubmissionCoordinatorError, MaterialExecutionSubmissionError)):
        return _error_result(503, "material_submission_unavailable", _detail(exc), recovery_required=True)
    return _error_result(500, "material_submission_error", "material submission failed", recovery_required=True)


def submit_material_execution(
    coordinator: MaterialExecutionSubmissionCoordinator,
    context: MaterialExecutionSubmissionContext,
    *,
    submission_id: str | None = None,
) -> MaterialExecutionHttpResult:
    """Submit asynchronously and return a framework-neutral HTTP result."""

    try:
        status = coordinator.submit(context, submission_id=submission_id)
    except Exception as exc:
        return _map_error(exc)
    return _status_result(status)


def get_material_execution_status(
    coordinator: MaterialExecutionSubmissionCoordinator,
    submission_id: str,
) -> MaterialExecutionHttpResult:
    """Read durable status without touching a Future or owner lane."""

    try:
        status = coordinator.status(submission_id)
    except Exception as exc:
        return _map_error(exc)
    if status is None:
        return _error_result(404, "material_submission_not_found", submission_id)
    return _status_result(status)


def reconcile_material_execution(
    coordinator: MaterialExecutionSubmissionCoordinator,
    submission_id: str,
) -> MaterialExecutionHttpResult:
    """Start an explicit live-lane reconcile; unknown is never retried."""

    try:
        status = coordinator.reconcile(submission_id)
    except Exception as exc:
        return _map_error(exc)
    return _status_result(status)


def close_unknown_material_execution(
    coordinator: MaterialExecutionSubmissionCoordinator,
    submission_id: str,
) -> MaterialExecutionHttpResult:
    """Expose the audited manual close action; this never retries work."""

    try:
        status = coordinator.close_unknown(submission_id)
    except Exception as exc:
        return _map_error(exc)
    return _status_result(status)


# Descriptive aliases for a future route module.
post_material_execution = submit_material_execution
get_material_execution = get_material_execution_status
post_material_execution_reconcile = reconcile_material_execution
post_material_execution_unknown_close = close_unknown_material_execution


__all__ = [
    "MaterialExecutionHttpResult",
    "close_unknown_material_execution",
    "get_material_execution",
    "get_material_execution_status",
    "post_material_execution",
    "post_material_execution_reconcile",
    "post_material_execution_unknown_close",
    "reconcile_material_execution",
    "submit_material_execution",
]
