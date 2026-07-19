from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import ipaddress
import json
from pathlib import Path
import re
from typing import Literal, TypeAlias
import unicodedata
from urllib.parse import urlparse
from uuid import UUID

from app.materials import provenance as material_provenance
from app.materials.stock_rights_evidence import (
    StockRightsEvidenceError,
    normalise_stock_rights_evidence,
)


PROTOCOL_VERSION = 1
STOCK_CANDIDATES_PROTOCOL_VERSION = 2
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_SECRET_BYTES = 16 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 4096
MAX_STRING_CHARS = 16 * 1024
MAX_ARTIFACTS = 128
MAX_API_KEY_CHARS = 4096
MAX_JSON_INTEGER_DIGITS = 20

_MAX_PATH_CHARS = 4096
_MAX_URL_CHARS = 8192
_MAX_LABEL_CHARS = 1024
_MAX_REASON_CHARS = MAX_STRING_CHARS
_MAX_QUERY_CHARS = 2048
_MAX_PER_PAGE = 100
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DRIVE_PREFIX_PATTERN = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
    | {f"COM{index}" for index in "¹²³"}
    | {f"LPT{index}" for index in "¹²³"}
)
_WINDOWS_FORBIDDEN_PATH_CHARACTERS = frozenset('?*|<>"')


MaterialOperationKind = Literal[
    "official_capture",
    "pexels",
    "pixabay",
    "coverr",
    "info_motion",
    "stock_candidates",
]
MaterialProtocolVersion: TypeAlias = Literal[1, 2]
MaterialWorkerStatus = Literal["succeeded", "failed"]

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


class MaterialProtocolError(ValueError):
    """A fail-closed validation error for the static worker protocol."""


@dataclass(frozen=True, slots=True)
class OfficialCapturePayload:
    source_url: str
    source_name: str
    page_title: str

    def __post_init__(self) -> None:
        _validate_http_url(self.source_url, "source_url", required=True)
        _validate_text(
            self.source_name,
            "source_name",
            required=True,
            max_chars=_MAX_LABEL_CHARS,
            trimmed=True,
        )
        _validate_text(
            self.page_title,
            "page_title",
            required=True,
            max_chars=_MAX_LABEL_CHARS,
            trimmed=True,
        )


@dataclass(frozen=True, slots=True)
class StockSearchPayload:
    query: str
    per_page: int

    def __post_init__(self) -> None:
        _validate_text(
            self.query,
            "query",
            required=True,
            max_chars=_MAX_QUERY_CHARS,
            trimmed=True,
        )
        _validate_positive_int(
            self.per_page,
            "per_page",
            maximum=_MAX_PER_PAGE,
        )


@dataclass(frozen=True, slots=True)
class InfoMotionPayload:
    project_name: str
    title: str

    def __post_init__(self) -> None:
        _validate_text(
            self.project_name,
            "project_name",
            required=True,
            max_chars=_MAX_LABEL_CHARS,
            trimmed=True,
        )
        _validate_text(
            self.title,
            "title",
            required=True,
            max_chars=_MAX_LABEL_CHARS,
            trimmed=True,
        )


MaterialRequestPayload: TypeAlias = (
    OfficialCapturePayload | StockSearchPayload | InfoMotionPayload
)


@dataclass(frozen=True, slots=True)
class MaterialOperationRequest:
    protocol_version: MaterialProtocolVersion
    execution_id: str
    attempt_id: str
    operation: MaterialOperationKind
    payload: MaterialRequestPayload

    def __post_init__(self) -> None:
        _validate_protocol_version(self.protocol_version, field="protocol_version")
        _validate_id_pair(self.execution_id, self.attempt_id)
        _validate_operation(self.operation)
        _validate_operation_protocol_version(
            self.operation,
            self.protocol_version,
            field="protocol_version",
        )
        expected_type = _request_payload_type(self.operation)
        if type(self.payload) is not expected_type:
            raise MaterialProtocolError(
                "request payload type does not match the selected operation"
            )
        self.payload.__post_init__()


@dataclass(frozen=True, slots=True)
class StagedArtifact:
    relative_path: str
    media_kind: Literal["video", "image"]
    source_type: str
    source_url: str
    evidence_kind: Literal["direct_evidence", "atmosphere", "explanation"]
    is_locally_generated: bool
    sha256: str
    size_bytes: int
    rights_evidence: dict[str, object] | None = None

    def __post_init__(self) -> None:
        _validate_artifact_relative_path(self.relative_path)
        if type(self.media_kind) is not str or self.media_kind not in {
            "video",
            "image",
        }:
            raise MaterialProtocolError("media_kind must be video or image")
        _validate_text(
            self.source_type,
            "source_type",
            required=True,
            max_chars=_MAX_LABEL_CHARS,
            trimmed=True,
        )
        _validate_http_url(self.source_url, "source_url", required=False)
        if type(self.evidence_kind) is not str or self.evidence_kind not in {
            "direct_evidence",
            "atmosphere",
            "explanation",
        }:
            raise MaterialProtocolError("evidence_kind is not allowed")
        if type(self.is_locally_generated) is not bool:
            raise MaterialProtocolError("is_locally_generated must be a boolean")
        if type(self.sha256) is not str or _SHA256_PATTERN.fullmatch(self.sha256) is None:
            raise MaterialProtocolError("sha256 must be 64 lowercase hexadecimal characters")
        _validate_positive_int(self.size_bytes, "size_bytes")
        provenance_invalid = False
        try:
            material_provenance.validate_execution_provenance(
                success=True,
                evidence_kind=self.evidence_kind,
                source_url=self.source_url,
                is_locally_generated=self.is_locally_generated,
            )
        except Exception:
            provenance_invalid = True
        if provenance_invalid:
            raise MaterialProtocolError("artifact provenance is invalid")
        if self.rights_evidence is not None:
            try:
                canonical_rights = normalise_stock_rights_evidence(
                    self.rights_evidence
                )
            except StockRightsEvidenceError as exc:
                raise MaterialProtocolError("artifact rights_evidence is invalid") from exc
            if self.source_url != canonical_rights["asset_page_url"]:
                raise MaterialProtocolError(
                    "artifact source_url must match rights_evidence asset_page_url"
                )
            object.__setattr__(self, "rights_evidence", canonical_rights)


@dataclass(frozen=True, slots=True)
class MaterialWorkerResult:
    protocol_version: MaterialProtocolVersion
    execution_id: str
    attempt_id: str
    operation: MaterialOperationKind
    status: MaterialWorkerStatus
    selected_artifact: str
    artifacts: tuple[StagedArtifact, ...]
    reason_code: str
    reason: str

    def __post_init__(self) -> None:
        _validate_protocol_version(self.protocol_version, field="protocol_version")
        _validate_id_pair(self.execution_id, self.attempt_id)
        _validate_operation(self.operation)
        _validate_operation_protocol_version(
            self.operation,
            self.protocol_version,
            field="protocol_version",
        )
        if type(self.status) is not str or self.status not in {"succeeded", "failed"}:
            raise MaterialProtocolError("result status must be succeeded or failed")
        _validate_text(
            self.selected_artifact,
            "selected_artifact",
            required=False,
            max_chars=_MAX_PATH_CHARS,
        )
        if type(self.artifacts) is not tuple:
            raise MaterialProtocolError("artifacts must be a tuple of StagedArtifact values")
        if len(self.artifacts) > MAX_ARTIFACTS:
            raise MaterialProtocolError("result exceeds the artifact limit")
        if any(type(item) is not StagedArtifact for item in self.artifacts):
            raise MaterialProtocolError("artifacts must contain only StagedArtifact values")
        paths = tuple(item.relative_path for item in self.artifacts)
        if len(set(paths)) != len(paths):
            raise MaterialProtocolError(
                "selected_artifact must match exactly one unique artifact"
            )
        if len({path.casefold() for path in paths}) != len(paths):
            raise MaterialProtocolError(
                "artifact paths must be unique under Windows case-insensitive matching"
            )
        _validate_reason_code(self.reason_code, required=self.status == "failed")
        _validate_text(
            self.reason,
            "reason",
            required=self.status == "failed",
            max_chars=_MAX_REASON_CHARS,
        )
        if self.status == "succeeded":
            if not self.artifacts:
                raise MaterialProtocolError(
                    "succeeded result requires a non-empty artifact tuple"
                )
            if self.operation == "stock_candidates":
                _validate_stock_candidate_batch(
                    self.artifacts,
                    selected_artifact=self.selected_artifact,
                )
            else:
                _validate_artifact_relative_path(self.selected_artifact)
                if paths.count(self.selected_artifact) != 1:
                    raise MaterialProtocolError(
                        "selected_artifact must match exactly one unique artifact"
                    )
        else:
            if self.selected_artifact != "":
                raise MaterialProtocolError(
                    "failed result requires an empty selected_artifact"
                )
            if self.artifacts:
                raise MaterialProtocolError(
                    "failed result requires an empty artifact tuple"
                )


@dataclass(frozen=True, slots=True)
class TerminationEvidence:
    platform: Literal["windows_job"]
    root_pid: int
    exit_code: int | None
    termination_method: Literal["natural", "job_terminate", "never_resumed"]
    active_processes: Literal[0]
    observed_at: str

    def __post_init__(self) -> None:
        if self.platform != "windows_job":
            raise MaterialProtocolError("termination platform must be windows_job")
        _validate_positive_int(self.root_pid, "root_pid")
        if self.exit_code is not None:
            if (
                type(self.exit_code) is not int
                or self.exit_code < 0
                or self.exit_code > 0xFFFFFFFF
            ):
                raise MaterialProtocolError("exit_code must be a Windows exit code or null")
        if self.termination_method not in {
            "natural",
            "job_terminate",
            "never_resumed",
        }:
            raise MaterialProtocolError("termination_method is not allowed")
        if type(self.active_processes) is not int or self.active_processes != 0:
            raise MaterialProtocolError("active_processes must be exactly zero")
        _validate_timestamp(self.observed_at)


@dataclass(frozen=True, slots=True)
class MaterialOperationOutcome:
    status: Literal["succeeded", "failed", "terminated"]
    result: MaterialWorkerResult | None
    termination: TerminationEvidence

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed", "terminated"}:
            raise MaterialProtocolError("outcome status is not allowed")
        if type(self.termination) is not TerminationEvidence:
            raise MaterialProtocolError("outcome requires TerminationEvidence")
        if self.status == "terminated":
            if self.result is not None:
                raise MaterialProtocolError(
                    "outcome status and result must be consistent"
                )
            if self.termination.termination_method not in {
                "job_terminate",
                "never_resumed",
            }:
                raise MaterialProtocolError(
                    "terminated outcome requires job_terminate or never_resumed"
                )
            return
        if type(self.result) is not MaterialWorkerResult:
            raise MaterialProtocolError("outcome status and result must be consistent")
        if self.result.status != self.status:
            raise MaterialProtocolError("outcome status and result must be consistent")
        if self.termination.termination_method != "natural":
            raise MaterialProtocolError(
                "succeeded and failed outcomes require natural termination"
            )


@dataclass(frozen=True, slots=True)
class OfficialCaptureSecrets:
    pass


@dataclass(frozen=True, slots=True)
class PexelsSecrets:
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        _validate_api_key(self.api_key)

    def __repr__(self) -> str:
        return "PexelsSecrets(api_key=<redacted>)"


@dataclass(frozen=True, slots=True)
class PixabaySecrets:
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        _validate_api_key(self.api_key)

    def __repr__(self) -> str:
        return "PixabaySecrets(api_key=<redacted>)"


@dataclass(frozen=True, slots=True)
class StockCandidateSecrets:
    pexels_api_key: str = field(repr=False)
    pixabay_api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        _validate_api_key(self.pexels_api_key, field="pexels_api_key")
        _validate_api_key(self.pixabay_api_key, field="pixabay_api_key")

    def __repr__(self) -> str:
        return (
            "StockCandidateSecrets(pexels_api_key=<redacted>, "
            "pixabay_api_key=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class CoverrSecrets:
    pass


@dataclass(frozen=True, slots=True)
class InfoMotionSecrets:
    pass


MaterialSecretPayload: TypeAlias = (
    OfficialCaptureSecrets
    | PexelsSecrets
    | PixabaySecrets
    | StockCandidateSecrets
    | CoverrSecrets
    | InfoMotionSecrets
)


@dataclass(frozen=True, slots=True)
class MaterialOperationSecrets:
    version: MaterialProtocolVersion
    execution_id: str
    attempt_id: str
    operation: MaterialOperationKind
    payload: MaterialSecretPayload = field(repr=False)

    def __post_init__(self) -> None:
        _validate_protocol_version(self.version, field="version")
        _validate_id_pair(self.execution_id, self.attempt_id)
        _validate_operation(self.operation)
        _validate_operation_protocol_version(
            self.operation,
            self.version,
            field="version",
        )
        expected_type = _secret_payload_type(self.operation)
        if type(self.payload) is not expected_type:
            raise MaterialProtocolError(
                "secret payload type does not match the selected operation"
            )
        if type(self.payload) in {
            PexelsSecrets,
            PixabaySecrets,
            StockCandidateSecrets,
        }:
            self.payload.__post_init__()

    def __repr__(self) -> str:
        return (
            "MaterialOperationSecrets("
            f"version={self.version!r}, "
            f"execution_id={self.execution_id!r}, "
            f"attempt_id={self.attempt_id!r}, "
            f"operation={self.operation!r}, "
            "payload=<redacted>)"
        )


class MaterialOperationBlocked(RuntimeError):
    """Raised when process-tree termination cannot be proven."""

    __slots__ = (
        "execution_id",
        "attempt_id",
        "reason_code",
        "job_instance_id",
        "quarantine_dir",
    )

    def __init__(
        self,
        *,
        execution_id: str,
        attempt_id: str,
        reason_code: str,
        job_instance_id: str,
        quarantine_dir: Path,
    ) -> None:
        _validate_id_pair(execution_id, attempt_id)
        _validate_canonical_uuid(job_instance_id, "job_instance_id")
        _validate_reason_code(reason_code, required=True)
        if not isinstance(quarantine_dir, Path):
            raise MaterialProtocolError("quarantine_dir must be a Path")
        self.execution_id = execution_id
        self.attempt_id = attempt_id
        self.reason_code = reason_code
        self.job_instance_id = job_instance_id
        self.quarantine_dir = quarantine_dir
        super().__init__(f"material operation blocked: {reason_code}")


def encode_material_operation_request(request: MaterialOperationRequest) -> bytes:
    if type(request) is not MaterialOperationRequest:
        raise MaterialProtocolError("request must be MaterialOperationRequest")
    request.__post_init__()
    value = {
        "protocol_version": request.protocol_version,
        "execution_id": request.execution_id,
        "attempt_id": request.attempt_id,
        "operation": request.operation,
        "payload": _request_payload_to_json(request.operation, request.payload),
    }
    return _encode_canonical_json(
        value,
        maximum_bytes=MAX_REQUEST_BYTES,
        size_label="64 KiB request",
    )


def decode_material_operation_request(raw: bytes) -> MaterialOperationRequest:
    value = _decode_strict_json(
        raw,
        maximum_bytes=MAX_REQUEST_BYTES,
        size_label="64 KiB request",
        document_label="request",
    )
    obj = _require_object(value, "request")
    _require_exact_keys(
        obj,
        {
            "protocol_version",
            "execution_id",
            "attempt_id",
            "operation",
            "payload",
        },
        "request",
    )
    operation = _validated_operation_value(obj["operation"])
    payload = _decode_request_payload(operation, obj["payload"])
    return MaterialOperationRequest(
        protocol_version=obj["protocol_version"],  # type: ignore[arg-type]
        execution_id=obj["execution_id"],  # type: ignore[arg-type]
        attempt_id=obj["attempt_id"],  # type: ignore[arg-type]
        operation=operation,
        payload=payload,
    )


def encode_material_worker_result(result: MaterialWorkerResult) -> bytes:
    if type(result) is not MaterialWorkerResult:
        raise MaterialProtocolError("result must be MaterialWorkerResult")
    result.__post_init__()
    value = {
        "protocol_version": result.protocol_version,
        "execution_id": result.execution_id,
        "attempt_id": result.attempt_id,
        "operation": result.operation,
        "status": result.status,
        "selected_artifact": result.selected_artifact,
        "artifacts": [_artifact_to_json(item) for item in result.artifacts],
        "reason_code": result.reason_code,
        "reason": result.reason,
    }
    return _encode_canonical_json(
        value,
        maximum_bytes=MAX_RESULT_BYTES,
        size_label="1 MiB result",
    )


def decode_material_worker_result(
    raw: bytes,
    *,
    expected_execution_id: str,
    expected_attempt_id: str,
    expected_operation: MaterialOperationKind,
) -> MaterialWorkerResult:
    _validate_id_pair(expected_execution_id, expected_attempt_id)
    _validate_operation(expected_operation)
    value = _decode_strict_json(
        raw,
        maximum_bytes=MAX_RESULT_BYTES,
        size_label="1 MiB result",
        document_label="result",
    )
    obj = _require_object(value, "result")
    _require_exact_keys(
        obj,
        {
            "protocol_version",
            "execution_id",
            "attempt_id",
            "operation",
            "status",
            "selected_artifact",
            "artifacts",
            "reason_code",
            "reason",
        },
        "result",
    )
    raw_artifacts = obj["artifacts"]
    if type(raw_artifacts) is not list:
        raise MaterialProtocolError("result artifacts must be a JSON array")
    if len(raw_artifacts) > MAX_ARTIFACTS:
        raise MaterialProtocolError("result exceeds the artifact limit")
    artifacts = tuple(_decode_artifact(item) for item in raw_artifacts)
    operation = _validated_operation_value(obj["operation"])
    result = MaterialWorkerResult(
        protocol_version=obj["protocol_version"],  # type: ignore[arg-type]
        execution_id=obj["execution_id"],  # type: ignore[arg-type]
        attempt_id=obj["attempt_id"],  # type: ignore[arg-type]
        operation=operation,
        status=obj["status"],  # type: ignore[arg-type]
        selected_artifact=obj["selected_artifact"],  # type: ignore[arg-type]
        artifacts=artifacts,
        reason_code=obj["reason_code"],  # type: ignore[arg-type]
        reason=obj["reason"],  # type: ignore[arg-type]
    )
    if result.execution_id != expected_execution_id:
        raise MaterialProtocolError("result execution_id does not match the request")
    if result.attempt_id != expected_attempt_id:
        raise MaterialProtocolError("result attempt_id does not match the request")
    if result.operation != expected_operation:
        raise MaterialProtocolError("result operation does not match the request")
    return result


def encode_material_operation_secrets(
    secrets: MaterialOperationSecrets,
    *,
    request: MaterialOperationRequest,
) -> bytes:
    if type(secrets) is not MaterialOperationSecrets:
        raise MaterialProtocolError("secrets must be MaterialOperationSecrets")
    if type(request) is not MaterialOperationRequest:
        raise MaterialProtocolError("request must be MaterialOperationRequest")
    secrets.__post_init__()
    request.__post_init__()
    _validate_secret_request_match(secrets, request)
    value = {
        "version": secrets.version,
        "execution_id": secrets.execution_id,
        "attempt_id": secrets.attempt_id,
        "operation": secrets.operation,
        "payload": _secret_payload_to_json(secrets.operation, secrets.payload),
    }
    return _encode_canonical_json(
        value,
        maximum_bytes=MAX_SECRET_BYTES,
        size_label="16 KiB secret envelope",
    )


def decode_material_operation_secrets(
    raw: bytes,
    *,
    request: MaterialOperationRequest,
) -> MaterialOperationSecrets:
    if type(request) is not MaterialOperationRequest:
        raise MaterialProtocolError("request must be MaterialOperationRequest")
    request.__post_init__()
    value = _decode_strict_json(
        raw,
        maximum_bytes=MAX_SECRET_BYTES,
        size_label="16 KiB secret envelope",
        document_label="secret envelope",
    )
    obj = _require_object(value, "secret envelope")
    _require_exact_keys(
        obj,
        {"version", "execution_id", "attempt_id", "operation", "payload"},
        "secret envelope",
    )
    operation = _validated_operation_value(obj["operation"])
    payload = _decode_secret_payload(operation, obj["payload"])
    secrets = MaterialOperationSecrets(
        version=obj["version"],  # type: ignore[arg-type]
        execution_id=obj["execution_id"],  # type: ignore[arg-type]
        attempt_id=obj["attempt_id"],  # type: ignore[arg-type]
        operation=operation,
        payload=payload,
    )
    _validate_secret_request_match(secrets, request)
    return secrets


def _request_payload_type(operation: str) -> type[object]:
    match operation:
        case "official_capture":
            return OfficialCapturePayload
        case "pexels" | "pixabay" | "coverr" | "stock_candidates":
            return StockSearchPayload
        case "info_motion":
            return InfoMotionPayload
        case _:
            raise MaterialProtocolError("operation is not allowed")


def _secret_payload_type(operation: str) -> type[object]:
    match operation:
        case "official_capture":
            return OfficialCaptureSecrets
        case "pexels":
            return PexelsSecrets
        case "pixabay":
            return PixabaySecrets
        case "stock_candidates":
            return StockCandidateSecrets
        case "coverr":
            return CoverrSecrets
        case "info_motion":
            return InfoMotionSecrets
        case _:
            raise MaterialProtocolError("operation is not allowed")


def _request_payload_to_json(
    operation: str,
    payload: MaterialRequestPayload,
) -> dict[str, object]:
    expected_type = _request_payload_type(operation)
    if type(payload) is not expected_type:
        raise MaterialProtocolError(
            "request payload type does not match the selected operation"
        )
    match payload:
        case OfficialCapturePayload():
            return {
                "source_url": payload.source_url,
                "source_name": payload.source_name,
                "page_title": payload.page_title,
            }
        case StockSearchPayload():
            return {"query": payload.query, "per_page": payload.per_page}
        case InfoMotionPayload():
            return {"project_name": payload.project_name, "title": payload.title}
        case _:
            raise MaterialProtocolError("request payload is not allowed")


def _decode_request_payload(
    operation: MaterialOperationKind,
    value: object,
) -> MaterialRequestPayload:
    obj = _require_object(value, "request payload")
    match operation:
        case "official_capture":
            _require_exact_keys(
                obj,
                {"source_url", "source_name", "page_title"},
                "official_capture payload",
            )
            return OfficialCapturePayload(
                source_url=obj["source_url"],  # type: ignore[arg-type]
                source_name=obj["source_name"],  # type: ignore[arg-type]
                page_title=obj["page_title"],  # type: ignore[arg-type]
            )
        case "pexels" | "pixabay" | "coverr" | "stock_candidates":
            _require_exact_keys(obj, {"query", "per_page"}, "stock payload")
            return StockSearchPayload(
                query=obj["query"],  # type: ignore[arg-type]
                per_page=obj["per_page"],  # type: ignore[arg-type]
            )
        case "info_motion":
            _require_exact_keys(
                obj,
                {"project_name", "title"},
                "info_motion payload",
            )
            return InfoMotionPayload(
                project_name=obj["project_name"],  # type: ignore[arg-type]
                title=obj["title"],  # type: ignore[arg-type]
            )


def _secret_payload_to_json(
    operation: str,
    payload: MaterialSecretPayload,
) -> dict[str, object]:
    expected_type = _secret_payload_type(operation)
    if type(payload) is not expected_type:
        raise MaterialProtocolError(
            "secret payload type does not match the selected operation"
        )
    match payload:
        case PexelsSecrets():
            return {"api_key": payload.api_key}
        case PixabaySecrets():
            return {"api_key": payload.api_key}
        case StockCandidateSecrets():
            return {
                "pexels_api_key": payload.pexels_api_key,
                "pixabay_api_key": payload.pixabay_api_key,
            }
        case OfficialCaptureSecrets() | CoverrSecrets() | InfoMotionSecrets():
            return {}
        case _:
            raise MaterialProtocolError("secret payload is not allowed")


def _decode_secret_payload(
    operation: MaterialOperationKind,
    value: object,
) -> MaterialSecretPayload:
    obj = _require_object(value, "secret payload")
    match operation:
        case "official_capture":
            _require_exact_keys(obj, set(), "official_capture secret payload")
            return OfficialCaptureSecrets()
        case "pexels":
            _require_exact_keys(obj, {"api_key"}, "pexels secret payload")
            return PexelsSecrets(api_key=obj["api_key"])  # type: ignore[arg-type]
        case "pixabay":
            _require_exact_keys(obj, {"api_key"}, "pixabay secret payload")
            return PixabaySecrets(api_key=obj["api_key"])  # type: ignore[arg-type]
        case "stock_candidates":
            _require_exact_keys(
                obj,
                {"pexels_api_key", "pixabay_api_key"},
                "stock_candidates secret payload",
            )
            return StockCandidateSecrets(
                pexels_api_key=obj["pexels_api_key"],  # type: ignore[arg-type]
                pixabay_api_key=obj["pixabay_api_key"],  # type: ignore[arg-type]
            )
        case "coverr":
            _require_exact_keys(obj, set(), "coverr secret payload")
            return CoverrSecrets()
        case "info_motion":
            _require_exact_keys(obj, set(), "info_motion secret payload")
            return InfoMotionSecrets()


def _artifact_to_json(artifact: StagedArtifact) -> dict[str, object]:
    if type(artifact) is not StagedArtifact:
        raise MaterialProtocolError("artifacts must contain StagedArtifact values")
    artifact.__post_init__()
    result: dict[str, object] = {
        "relative_path": artifact.relative_path,
        "media_kind": artifact.media_kind,
        "source_type": artifact.source_type,
        "source_url": artifact.source_url,
        "evidence_kind": artifact.evidence_kind,
        "is_locally_generated": artifact.is_locally_generated,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
    }
    if artifact.rights_evidence is not None:
        result["rights_evidence"] = normalise_stock_rights_evidence(
            artifact.rights_evidence
        )
    return result


def _decode_artifact(value: object) -> StagedArtifact:
    obj = _require_object(value, "artifact")
    legacy_fields = {
        "relative_path",
        "media_kind",
        "source_type",
        "source_url",
        "evidence_kind",
        "is_locally_generated",
        "sha256",
        "size_bytes",
    }
    if set(obj) not in {frozenset(legacy_fields), frozenset(legacy_fields | {"rights_evidence"})}:
        raise MaterialProtocolError(
            "artifact must contain exact keys: legacy fields and optional rights_evidence"
        )
    return StagedArtifact(
        relative_path=obj["relative_path"],  # type: ignore[arg-type]
        media_kind=obj["media_kind"],  # type: ignore[arg-type]
        source_type=obj["source_type"],  # type: ignore[arg-type]
        source_url=obj["source_url"],  # type: ignore[arg-type]
        evidence_kind=obj["evidence_kind"],  # type: ignore[arg-type]
        is_locally_generated=obj["is_locally_generated"],  # type: ignore[arg-type]
        sha256=obj["sha256"],  # type: ignore[arg-type]
        size_bytes=obj["size_bytes"],  # type: ignore[arg-type]
        rights_evidence=obj.get("rights_evidence"),  # type: ignore[arg-type]
    )


def _validate_secret_request_match(
    secrets: MaterialOperationSecrets,
    request: MaterialOperationRequest,
) -> None:
    if secrets.execution_id != request.execution_id:
        raise MaterialProtocolError(
            "secret envelope execution_id does not match the request"
        )
    if secrets.attempt_id != request.attempt_id:
        raise MaterialProtocolError(
            "secret envelope attempt_id does not match the request"
        )
    if secrets.operation != request.operation:
        raise MaterialProtocolError(
            "secret envelope operation does not match the request"
        )
    if secrets.version != request.protocol_version:
        raise MaterialProtocolError(
            "secret envelope version does not match the request protocol_version"
        )


def _encode_canonical_json(
    value: object,
    *,
    maximum_bytes: int,
    size_label: str,
) -> bytes:
    _enforce_json_shape_limits(value)
    encoding_failed = False
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        encoding_failed = True
    if encoding_failed:
        raise MaterialProtocolError(
            "protocol value cannot be encoded as strict UTF-8 JSON"
        )
    if len(encoded) > maximum_bytes:
        raise MaterialProtocolError(f"document exceeds the {size_label} limit")
    return encoded


def _decode_strict_json(
    raw: bytes,
    *,
    maximum_bytes: int,
    size_label: str,
    document_label: str,
) -> object:
    if type(raw) is not bytes:
        raise MaterialProtocolError(f"{document_label} must be UTF-8 bytes")
    if len(raw) > maximum_bytes:
        raise MaterialProtocolError(f"document exceeds the {size_label} limit")
    if not raw:
        raise MaterialProtocolError(f"{document_label} cannot be empty")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise MaterialProtocolError(f"{document_label} must not contain a UTF-8 BOM")
    malformed = False
    try:
        text = raw.decode("utf-8", errors="strict")
        decoder = json.JSONDecoder(
            object_pairs_hook=_object_without_duplicate_keys,
            parse_int=_parse_bounded_json_int,
            parse_float=_reject_json_float,
            parse_constant=_reject_json_constant,
        )
        value, end = decoder.raw_decode(text)
    except MaterialProtocolError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        malformed = True
    if malformed:
        raise MaterialProtocolError(
            f"{document_label} is not valid strict UTF-8 JSON"
        )
    if end != len(text):
        raise MaterialProtocolError(
            f"{document_label} must not contain leading or trailing data"
        )
    _enforce_json_shape_limits(value)
    return value


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MaterialProtocolError("duplicate JSON keys are not allowed")
        result[key] = value
    return result


def _reject_json_float(_value: str) -> object:
    raise MaterialProtocolError("floating-point JSON values are not allowed")


def _reject_json_constant(_value: str) -> object:
    raise MaterialProtocolError("NaN and Infinity are not allowed")


def _parse_bounded_json_int(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise MaterialProtocolError("JSON integer digit limit exceeded")
    return int(value)


def _enforce_json_shape_limits(value: object) -> None:
    nodes = 0
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise MaterialProtocolError("JSON node limit exceeded")
        if depth > MAX_JSON_DEPTH:
            raise MaterialProtocolError("JSON depth limit exceeded")
        if type(current) is str:
            if len(current) > MAX_STRING_CHARS:
                raise MaterialProtocolError("JSON string limit exceeded")
            _reject_unpaired_surrogates(current, "JSON string")
            _reject_forbidden_controls(current, "JSON string")
            continue
        if type(current) is dict:
            for key, item in current.items():
                nodes += 1
                if nodes > MAX_JSON_NODES:
                    raise MaterialProtocolError("JSON node limit exceeded")
                if type(key) is not str:
                    raise MaterialProtocolError("JSON object keys must be strings")
                if len(key) > MAX_STRING_CHARS:
                    raise MaterialProtocolError("JSON string limit exceeded")
                _reject_unpaired_surrogates(key, "JSON object key")
                _reject_forbidden_controls(key, "JSON object key")
                pending.append((item, depth + 1))
            continue
        if type(current) is list:
            pending.extend((item, depth + 1) for item in current)
            continue
        if current is None or type(current) is bool:
            continue
        if type(current) is int:
            if abs(current) >= 10**MAX_JSON_INTEGER_DIGITS:
                raise MaterialProtocolError("JSON integer digit limit exceeded")
            continue
        raise MaterialProtocolError("JSON contains an unsupported value type")


def _require_object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise MaterialProtocolError(f"{label} must be a JSON object")
    return value


def _require_exact_keys(
    value: dict[str, object],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        expected_label = ", ".join(sorted(expected)) if expected else "no fields"
        raise MaterialProtocolError(
            f"{label} must contain exact keys: {expected_label}"
        )


def _validated_operation_value(value: object) -> MaterialOperationKind:
    _validate_operation(value)
    return value  # type: ignore[return-value]


def _validate_operation(value: object) -> None:
    if type(value) is not str or value not in _OPERATIONS:
        raise MaterialProtocolError("operation is not allowed")


def _validate_protocol_version(value: object, *, field: str) -> None:
    if type(value) is not int or value not in {
        PROTOCOL_VERSION,
        STOCK_CANDIDATES_PROTOCOL_VERSION,
    }:
        raise MaterialProtocolError(f"{field} must be integer 1 or 2")


def _validate_operation_protocol_version(
    operation: MaterialOperationKind,
    version: object,
    *,
    field: str,
) -> None:
    expected = (
        STOCK_CANDIDATES_PROTOCOL_VERSION
        if operation == "stock_candidates"
        else PROTOCOL_VERSION
    )
    if version != expected:
        raise MaterialProtocolError(
            f"{operation} {field} must be integer {expected}"
        )


def _validate_id_pair(execution_id: object, attempt_id: object) -> None:
    _validate_canonical_uuid(execution_id, "execution_id")
    _validate_canonical_uuid(attempt_id, "attempt_id")
    if execution_id == attempt_id:
        raise MaterialProtocolError("execution_id and attempt_id must be different")


def _validate_canonical_uuid(value: object, field: str) -> None:
    if type(value) is not str:
        raise MaterialProtocolError(f"{field} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise MaterialProtocolError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise MaterialProtocolError(f"{field} must be a canonical UUID")


def _validate_text(
    value: object,
    field: str,
    *,
    required: bool,
    max_chars: int,
    trimmed: bool = False,
) -> None:
    if type(value) is not str:
        raise MaterialProtocolError(f"{field} must be a string")
    if len(value) > max_chars:
        raise MaterialProtocolError(f"{field} exceeds its string limit")
    _reject_unpaired_surrogates(value, field)
    _reject_forbidden_controls(value, field)
    if required and not value:
        raise MaterialProtocolError(f"{field} must not be empty")
    if trimmed and value != value.strip():
        raise MaterialProtocolError(f"{field} must not have surrounding whitespace")
    if required and not value.strip():
        raise MaterialProtocolError(f"{field} must not be empty")


def _validate_http_url(value: object, field: str, *, required: bool) -> None:
    _validate_text(
        value,
        field,
        required=required,
        max_chars=_MAX_URL_CHARS,
        trimmed=True,
    )
    if value == "":
        return
    if (
        "\\" in value
        or any(character.isspace() for character in value)
        or unicodedata.normalize("NFC", value) != value
    ):
        raise MaterialProtocolError(f"{field} must be an absolute http/https URL")
    invalid_url = False
    parsed = None
    hostname = None
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        _ = parsed.port
    except (ValueError, UnicodeError):
        invalid_url = True
    if invalid_url:
        raise MaterialProtocolError(f"{field} must be an absolute http/https URL")
    if (
        parsed is None
        or parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or "%" in hostname
    ):
        raise MaterialProtocolError(f"{field} must be an absolute http/https URL")

    is_ip_address = True
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        is_ip_address = False
    if is_ip_address:
        return

    idna_invalid = False
    idna_hostname = ""
    try:
        idna_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        idna_invalid = True
    if idna_invalid:
        raise MaterialProtocolError(f"{field} must be an absolute http/https URL")
    labels = idna_hostname.rstrip(".").split(".")
    if (
        not idna_hostname
        or len(idna_hostname.encode("ascii")) > 253
        or not labels
        or any(not label or len(label.encode("ascii")) > 63 for label in labels)
    ):
        raise MaterialProtocolError(f"{field} must be an absolute http/https URL")


def _validate_positive_int(
    value: object,
    field: str,
    *,
    maximum: int | None = None,
) -> None:
    if type(value) is not int or value <= 0:
        raise MaterialProtocolError(f"{field} must be a positive integer")
    if maximum is not None and value > maximum:
        raise MaterialProtocolError(f"{field} exceeds its integer limit")


def _validate_stock_candidate_batch(
    artifacts: tuple[StagedArtifact, ...],
    *,
    selected_artifact: str,
) -> None:
    if selected_artifact != "":
        raise MaterialProtocolError(
            "stock_candidates succeeded result requires an empty selected_artifact"
        )
    if not 3 <= len(artifacts) <= 5:
        raise MaterialProtocolError(
            "stock_candidates succeeded result requires 3 to 5 artifacts"
        )
    sources = {artifact.source_type for artifact in artifacts}
    if not sources.issubset({"Pexels", "Pixabay"}) or not {
        "Pexels",
        "Pixabay",
    }.issubset(sources):
        raise MaterialProtocolError(
            "stock_candidates succeeded result requires Pexels and Pixabay artifacts"
        )
    fingerprints = tuple(artifact.sha256 for artifact in artifacts)
    if len(set(fingerprints)) != len(fingerprints):
        raise MaterialProtocolError(
            "stock_candidates artifact fingerprints must be unique"
        )
    source_identities = tuple(
        (artifact.source_type, artifact.source_url) for artifact in artifacts
    )
    if len(set(source_identities)) != len(source_identities):
        raise MaterialProtocolError(
            "stock_candidates provider source identities must be unique"
        )
    if any(
        artifact.is_locally_generated
        or artifact.evidence_kind == "test_placeholder"
        or not artifact.source_url
        for artifact in artifacts
    ):
        raise MaterialProtocolError(
            "stock_candidates requires real provider artifacts without placeholders"
        )
    for artifact in artifacts:
        rights = artifact.rights_evidence
        if not isinstance(rights, dict):
            raise MaterialProtocolError(
                "stock_candidates artifact rights_evidence is required"
            )
        if rights.get("provider") != artifact.source_type:
            raise MaterialProtocolError(
                "stock_candidates artifact rights_evidence provider must match source_type"
            )
        media_type = rights.get("media_type")
        media_matches = (
            media_type == "video"
            if artifact.media_kind == "video"
            else media_type in {"photo", "illustration", "vector"}
        )
        if not media_matches:
            raise MaterialProtocolError(
                "stock_candidates artifact rights_evidence media_type must match media_kind"
            )


def _validate_reason_code(value: object, *, required: bool) -> None:
    if type(value) is not str:
        raise MaterialProtocolError("reason_code must be a string")
    if value == "":
        if required:
            raise MaterialProtocolError("reason_code must not be empty")
        return
    if _REASON_CODE_PATTERN.fullmatch(value) is None:
        raise MaterialProtocolError("reason_code must be a lowercase protocol token")


def _validate_api_key(value: object, *, field: str = "api_key") -> None:
    if type(value) is not str:
        raise MaterialProtocolError(f"{field} must be a string")
    if not value or not value.strip():
        raise MaterialProtocolError(f"{field} must not be empty")
    if value != value.strip():
        raise MaterialProtocolError(f"{field} must not have surrounding whitespace")
    if len(value) > MAX_API_KEY_CHARS:
        raise MaterialProtocolError(f"{field} exceeds its length limit")
    _reject_unpaired_surrogates(value, field)
    _reject_forbidden_controls(value, field)


def _validate_artifact_relative_path(value: object) -> None:
    if type(value) is not str:
        raise MaterialProtocolError("relative_path must be a string")
    if not value or len(value) > _MAX_PATH_CHARS:
        raise MaterialProtocolError("relative_path is not a valid payload path")
    _reject_unpaired_surrogates(value, "relative_path")
    _reject_forbidden_controls(value, "relative_path")
    if "\\" in value or value.startswith("/") or _DRIVE_PREFIX_PATTERN.match(value):
        raise MaterialProtocolError("relative_path is not a valid payload path")
    parts = value.split("/")
    if len(parts) < 2 or parts[0] != "payload":
        raise MaterialProtocolError("relative_path must be below payload/")
    for part in parts:
        if (
            part in {"", ".", ".."}
            or ":" in part
            or any(character in _WINDOWS_FORBIDDEN_PATH_CHARACTERS for character in part)
        ):
            raise MaterialProtocolError("relative_path contains an unsafe path segment")
        if part.endswith((".", " ")):
            raise MaterialProtocolError("relative_path contains an unsafe path segment")
        windows_stem = part.split(".", 1)[0].upper()
        if windows_stem in _WINDOWS_RESERVED_NAMES:
            raise MaterialProtocolError("relative_path contains an unsafe path segment")


def _validate_timestamp(value: object) -> None:
    _validate_text(
        value,
        "observed_at",
        required=True,
        max_chars=128,
        trimmed=True,
    )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MaterialProtocolError("observed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MaterialProtocolError("observed_at must include a timezone offset")


def _reject_unpaired_surrogates(value: str, field: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise MaterialProtocolError(f"{field} contains an unpaired surrogate")


def _reject_forbidden_controls(value: str, field: str) -> None:
    if any(
        ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F
        for character in value
    ):
        raise MaterialProtocolError(f"{field} contains a forbidden control character")


__all__ = [
    "MAX_API_KEY_CHARS",
    "MAX_ARTIFACTS",
    "MAX_JSON_DEPTH",
    "MAX_JSON_NODES",
    "MAX_REQUEST_BYTES",
    "MAX_RESULT_BYTES",
    "MAX_SECRET_BYTES",
    "MAX_STRING_CHARS",
    "PROTOCOL_VERSION",
    "STOCK_CANDIDATES_PROTOCOL_VERSION",
    "CoverrSecrets",
    "InfoMotionPayload",
    "InfoMotionSecrets",
    "MaterialOperationBlocked",
    "MaterialOperationKind",
    "MaterialProtocolVersion",
    "MaterialOperationOutcome",
    "MaterialOperationRequest",
    "MaterialOperationSecrets",
    "MaterialProtocolError",
    "MaterialWorkerResult",
    "OfficialCapturePayload",
    "OfficialCaptureSecrets",
    "PexelsSecrets",
    "PixabaySecrets",
    "StockCandidateSecrets",
    "StagedArtifact",
    "StockSearchPayload",
    "TerminationEvidence",
    "decode_material_operation_request",
    "decode_material_operation_secrets",
    "decode_material_worker_result",
    "encode_material_operation_request",
    "encode_material_operation_secrets",
    "encode_material_worker_result",
]
