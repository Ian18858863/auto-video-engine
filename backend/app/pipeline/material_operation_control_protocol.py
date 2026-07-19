"""Small, fail-closed control messages for a material worker.

This module deliberately contains only the worker-ready message.  It is kept
separate from the service layer so a worker and its controller can validate
the same bytes before either side creates native resources.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Literal, get_args
from uuid import UUID

from app.pipeline.material_operation_protocol import (
    MaterialOperationKind,
    MaterialProtocolError,
    PROTOCOL_VERSION,
)


CONTROL_PROTOCOL_VERSION = PROTOCOL_VERSION
MAX_READY_BYTES = 4096
MAX_READY_JSON_DEPTH = 8
MAX_READY_JSON_NODES = 32
MAX_READY_JSON_INTEGER_DIGITS = 10
MAX_READY_STRING_CHARS = 256
MAX_ROOT_PID = 2**32 - 1

READY_STATE: Literal["ready"] = "ready"
_READY_FIELDS = frozenset(
    {
        "protocol_version",
        "state",
        "execution_id",
        "attempt_id",
        "operation",
        "root_pid",
        "request_sha256",
    }
)
_OPERATIONS = frozenset(get_args(MaterialOperationKind))
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class MaterialWorkerReady:
    """The only worker-to-controller readiness assertion.

    The message intentionally carries no credentials, credential hashes, or
    mutable process metadata beyond the root PID that the controller must
    independently verify.
    """

    protocol_version: Literal[1]
    state: Literal["ready"]
    execution_id: str
    attempt_id: str
    operation: MaterialOperationKind
    root_pid: int
    request_sha256: str

    def __post_init__(self) -> None:
        _validate_protocol_version(self.protocol_version)
        _validate_state(self.state)
        _validate_id_pair(self.execution_id, self.attempt_id)
        _validate_operation(self.operation)
        _validate_root_pid(self.root_pid)
        _validate_sha256(self.request_sha256, "request_sha256")


def encode_material_worker_ready(ready: MaterialWorkerReady) -> bytes:
    """Encode a ready message into deterministic, strict UTF-8 JSON."""

    if type(ready) is not MaterialWorkerReady:
        raise MaterialProtocolError("ready must be MaterialWorkerReady")
    ready.__post_init__()
    value = {
        "protocol_version": ready.protocol_version,
        "state": ready.state,
        "execution_id": ready.execution_id,
        "attempt_id": ready.attempt_id,
        "operation": ready.operation,
        "root_pid": ready.root_pid,
        "request_sha256": ready.request_sha256,
    }
    _enforce_shape_limits(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise MaterialProtocolError(
            "ready cannot be encoded as strict UTF-8 JSON"
        ) from exc
    if len(encoded) > MAX_READY_BYTES:
        raise MaterialProtocolError("ready exceeds the 4 KiB limit")
    return encoded


def decode_material_worker_ready(
    raw: bytes,
    *,
    expected_execution_id: str | None = None,
    expected_attempt_id: str | None = None,
    expected_operation: MaterialOperationKind | None = None,
    expected_root_pid: int | None = None,
    expected_request_sha256: str | None = None,
) -> MaterialWorkerReady:
    """Decode and optionally pin a ready message to controller identity."""

    value = _decode_strict_json(raw)
    if type(value) is not dict:
        raise MaterialProtocolError("ready must be a JSON object")
    if set(value) != _READY_FIELDS:
        raise MaterialProtocolError("ready must contain exact keys")

    ready = MaterialWorkerReady(
        protocol_version=value["protocol_version"],  # type: ignore[arg-type]
        state=value["state"],  # type: ignore[arg-type]
        execution_id=value["execution_id"],  # type: ignore[arg-type]
        attempt_id=value["attempt_id"],  # type: ignore[arg-type]
        operation=value["operation"],  # type: ignore[arg-type]
        root_pid=value["root_pid"],  # type: ignore[arg-type]
        request_sha256=value["request_sha256"],  # type: ignore[arg-type]
    )

    if expected_execution_id is not None:
        _validate_uuid(expected_execution_id, "expected_execution_id")
        if ready.execution_id != expected_execution_id:
            raise MaterialProtocolError("ready execution_id does not match")
    if expected_attempt_id is not None:
        _validate_uuid(expected_attempt_id, "expected_attempt_id")
        if ready.attempt_id != expected_attempt_id:
            raise MaterialProtocolError("ready attempt_id does not match")
    if expected_operation is not None:
        _validate_operation(expected_operation)
        if ready.operation != expected_operation:
            raise MaterialProtocolError("ready operation does not match")
    if expected_root_pid is not None:
        _validate_root_pid(expected_root_pid, field="expected_root_pid")
        if ready.root_pid != expected_root_pid:
            raise MaterialProtocolError("ready root_pid does not match")
    if expected_request_sha256 is not None:
        _validate_sha256(expected_request_sha256, "expected_request_sha256")
        if ready.request_sha256 != expected_request_sha256:
            raise MaterialProtocolError("ready request_sha256 does not match")
    return ready


def decode_material_worker_ready_bound(
    raw: bytes,
    *,
    expected_execution_id: str,
    expected_attempt_id: str,
    expected_operation: MaterialOperationKind,
    expected_root_pid: int,
    expected_request_sha256: str,
) -> MaterialWorkerReady:
    """Decode a ready message only when every parent identity is supplied.

    The unbound decoder is useful at the worker boundary, but a controller
    must use this entry point before it persists a ``ready`` descriptor.
    """

    return decode_material_worker_ready(
        raw,
        expected_execution_id=expected_execution_id,
        expected_attempt_id=expected_attempt_id,
        expected_operation=expected_operation,
        expected_root_pid=expected_root_pid,
        expected_request_sha256=expected_request_sha256,
    )


def validate_material_worker_ready_identity(
    ready: MaterialWorkerReady,
    *,
    expected_execution_id: str,
    expected_attempt_id: str,
    expected_operation: MaterialOperationKind,
    expected_root_pid: int,
    expected_request_sha256: str,
) -> None:
    """Apply the controller's all-fields identity gate to a typed message."""

    if type(ready) is not MaterialWorkerReady:
        raise MaterialProtocolError("ready must be MaterialWorkerReady")
    encoded = encode_material_worker_ready(ready)
    decode_material_worker_ready_bound(
        encoded,
        expected_execution_id=expected_execution_id,
        expected_attempt_id=expected_attempt_id,
        expected_operation=expected_operation,
        expected_root_pid=expected_root_pid,
        expected_request_sha256=expected_request_sha256,
    )


# Short aliases make the protocol convenient for a worker entry point while
# keeping the explicit names above as the stable public ABI.
encode_worker_ready = encode_material_worker_ready
decode_worker_ready = decode_material_worker_ready
MaterialWorkerReadyError = MaterialProtocolError


def _decode_strict_json(raw: bytes) -> object:
    if type(raw) is not bytes:
        raise MaterialProtocolError("ready must be UTF-8 bytes")
    if len(raw) > MAX_READY_BYTES:
        raise MaterialProtocolError("ready exceeds the 4 KiB limit")
    if not raw:
        raise MaterialProtocolError("ready cannot be empty")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise MaterialProtocolError("ready must not contain a UTF-8 BOM")
    malformed = False
    value: object = None
    end = 0
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
        # JSONDecodeError keeps the original document on ``.doc``.  Raise
        # outside its except block so neither __cause__ nor __context__ can
        # retain attacker-supplied bytes for a logger to serialize.
        raise MaterialProtocolError("ready is not valid strict UTF-8 JSON")
    if end != len(text):
        raise MaterialProtocolError("ready must not contain leading or trailing data")
    _enforce_shape_limits(value)
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
    if len(digits) > MAX_READY_JSON_INTEGER_DIGITS:
        raise MaterialProtocolError("JSON integer digit limit exceeded")
    return int(value)


def _enforce_shape_limits(value: object) -> None:
    nodes = 0
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > MAX_READY_JSON_NODES:
            raise MaterialProtocolError("JSON node limit exceeded")
        if depth > MAX_READY_JSON_DEPTH:
            raise MaterialProtocolError("JSON depth limit exceeded")
        if type(current) is str:
            if len(current) > MAX_READY_STRING_CHARS:
                raise MaterialProtocolError("JSON string limit exceeded")
            _reject_bad_string(current)
            continue
        if type(current) is dict:
            for key, item in current.items():
                if type(key) is not str:
                    raise MaterialProtocolError("JSON object keys must be strings")
                nodes += 1
                if nodes > MAX_READY_JSON_NODES:
                    raise MaterialProtocolError("JSON node limit exceeded")
                if len(key) > MAX_READY_STRING_CHARS:
                    raise MaterialProtocolError("JSON string limit exceeded")
                _reject_bad_string(key)
                pending.append((item, depth + 1))
            continue
        if type(current) is list:
            pending.extend((item, depth + 1) for item in current)
            continue
        if current is None or type(current) is bool:
            continue
        if type(current) is int:
            if abs(current) >= 10**MAX_READY_JSON_INTEGER_DIGITS:
                raise MaterialProtocolError("JSON integer digit limit exceeded")
            continue
        raise MaterialProtocolError("JSON contains an unsupported value type")


def _reject_bad_string(value: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise MaterialProtocolError("JSON string contains an unpaired surrogate")
    if any(
        ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F
        for character in value
    ):
        raise MaterialProtocolError("JSON string contains a forbidden control character")


def _validate_protocol_version(value: object) -> None:
    if type(value) is not int or value != CONTROL_PROTOCOL_VERSION:
        raise MaterialProtocolError("protocol_version must be integer 1")


def _validate_state(value: object) -> None:
    if type(value) is not str or value != READY_STATE:
        raise MaterialProtocolError('state must be "ready"')


def _validate_id_pair(execution_id: object, attempt_id: object) -> None:
    _validate_uuid(execution_id, "execution_id")
    _validate_uuid(attempt_id, "attempt_id")
    if execution_id == attempt_id:
        raise MaterialProtocolError("execution_id and attempt_id must be different")


def _validate_uuid(value: object, field: str) -> None:
    if type(value) is not str:
        raise MaterialProtocolError(f"{field} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise MaterialProtocolError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise MaterialProtocolError(f"{field} must be a canonical UUID")


def _validate_operation(value: object) -> None:
    if type(value) is not str or value not in _OPERATIONS:
        raise MaterialProtocolError("operation is not allowed")


def _validate_root_pid(value: object, *, field: str = "root_pid") -> None:
    if type(value) is not int or not 0 < value <= MAX_ROOT_PID:
        raise MaterialProtocolError(f"{field} must be a positive Windows PID")


def _validate_sha256(value: object, field: str) -> None:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise MaterialProtocolError(
            f"{field} must be 64 lowercase hexadecimal characters"
        )


__all__ = [
    "CONTROL_PROTOCOL_VERSION",
    "MAX_READY_BYTES",
    "MAX_READY_JSON_DEPTH",
    "MAX_READY_JSON_NODES",
    "MAX_READY_STRING_CHARS",
    "MAX_ROOT_PID",
    "MaterialProtocolError",
    "MaterialWorkerReady",
    "MaterialWorkerReadyError",
    "decode_material_worker_ready",
    "decode_material_worker_ready_bound",
    "decode_worker_ready",
    "encode_material_worker_ready",
    "encode_worker_ready",
    "validate_material_worker_ready_identity",
]
