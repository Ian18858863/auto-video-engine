from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import pytest

from app.pipeline.material_operation_control_protocol import (
    MAX_READY_BYTES,
    MaterialWorkerReady,
    decode_material_worker_ready_bound,
    decode_material_worker_ready,
    encode_material_worker_ready,
    validate_material_worker_ready_identity,
)
from app.pipeline.material_operation_protocol import MaterialProtocolError


EXECUTION_ID = "123e4567-e89b-12d3-a456-426614174000"
ATTEMPT_ID = "123e4567-e89b-12d3-a456-426614174001"
REQUEST_SHA256 = "a" * 64


def _ready(**overrides: object) -> MaterialWorkerReady:
    values: dict[str, object] = {
        "protocol_version": 1,
        "state": "ready",
        "execution_id": EXECUTION_ID,
        "attempt_id": ATTEMPT_ID,
        "operation": "official_capture",
        "root_pid": 4242,
        "request_sha256": REQUEST_SHA256,
    }
    values.update(overrides)
    return MaterialWorkerReady(**values)  # type: ignore[arg-type]


def test_ready_round_trip_is_typed_and_canonical() -> None:
    ready = _ready()

    encoded = encode_material_worker_ready(ready)

    assert encoded == (
        b'{"attempt_id":"123e4567-e89b-12d3-a456-426614174001",'
        b'"execution_id":"123e4567-e89b-12d3-a456-426614174000",'
        b'"operation":"official_capture","protocol_version":1,'
        b'"request_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"root_pid":4242,"state":"ready"}'
    )
    assert decode_material_worker_ready(encoded) == ready


def test_ready_schema_has_exact_fields_and_no_secret_surface() -> None:
    assert set(MaterialWorkerReady.__dataclass_fields__) == {
        "protocol_version",
        "state",
        "execution_id",
        "attempt_id",
        "operation",
        "root_pid",
        "request_sha256",
    }
    assert not any("secret" in name.casefold() for name in MaterialWorkerReady.__dataclass_fields__)
    assert not hasattr(_ready(), "__dict__")


def test_ready_is_frozen() -> None:
    ready = _ready()

    with pytest.raises(FrozenInstanceError):
        ready.root_pid = 7  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol_version", 2),
        ("protocol_version", True),
        ("state", "running"),
        ("state", "ready "),
        ("execution_id", "not-a-uuid"),
        ("execution_id", EXECUTION_ID.upper()),
        ("attempt_id", EXECUTION_ID),
        ("attempt_id", "123e4567-e89b-12d3-a456-42661417400Z"),
        ("operation", "unknown"),
        ("operation", "pexels "),
        ("root_pid", 0),
        ("root_pid", -1),
        ("root_pid", 2**32),
        ("root_pid", True),
        ("request_sha256", "A" * 64),
        ("request_sha256", "a" * 63),
        ("request_sha256", "a" * 65),
    ],
)
def test_ready_rejects_invalid_typed_fields(field: str, value: object) -> None:
    with pytest.raises(MaterialProtocolError):
        _ready(**{field: value})


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{}",
        b"{} ",
        b"\n{}",
        b"{}{}",
        b"\xef\xbb\xbf{}",
        b"\xff",
        b'{"protocol_version":1.0}',
        b'{"protocol_version":NaN}',
        b'{"protocol_version":1,"protocol_version":1}',
        b'[]',
    ],
)
def test_ready_decoder_rejects_non_strict_json(raw: bytes) -> None:
    with pytest.raises(MaterialProtocolError):
        decode_material_worker_ready(raw)


def test_malformed_ready_error_does_not_retain_attacker_document() -> None:
    canary = "secret-canary-must-not-survive-in-an-exception"
    raw = ('{"unexpected":"' + canary + '"').encode()

    with pytest.raises(MaterialProtocolError) as exc_info:
        decode_material_worker_ready(raw)

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert canary not in repr(exc_info.value)


def test_ready_decoder_rejects_missing_extra_and_secret_fields() -> None:
    value = json.loads(encode_material_worker_ready(_ready()))

    for mutation in (
        {key: item for key, item in value.items() if key != "root_pid"},
        {**value, "extra": "nope"},
        {**value, "secret": "must never cross this protocol"},
        {**value, "secret_sha256": REQUEST_SHA256},
    ):
        with pytest.raises(MaterialProtocolError):
            decode_material_worker_ready(json.dumps(mutation, separators=(",", ":")).encode())


def test_ready_decoder_can_pin_expected_identity() -> None:
    encoded = encode_material_worker_ready(_ready())

    assert decode_material_worker_ready(
        encoded,
        expected_execution_id=EXECUTION_ID,
        expected_attempt_id=ATTEMPT_ID,
        expected_operation="official_capture",
        expected_root_pid=4242,
        expected_request_sha256=REQUEST_SHA256,
    ) == _ready()

    with pytest.raises(MaterialProtocolError):
        decode_material_worker_ready(encoded, expected_root_pid=4243)


def test_parent_bound_decoder_requires_all_five_identity_values() -> None:
    encoded = encode_material_worker_ready(_ready())

    assert decode_material_worker_ready_bound(
        encoded,
        expected_execution_id=EXECUTION_ID,
        expected_attempt_id=ATTEMPT_ID,
        expected_operation="official_capture",
        expected_root_pid=4242,
        expected_request_sha256=REQUEST_SHA256,
    ) == _ready()
    with pytest.raises(TypeError):
        decode_material_worker_ready_bound(encoded)  # type: ignore[call-arg]


def test_typed_ready_identity_gate_rejects_mismatch() -> None:
    with pytest.raises(MaterialProtocolError):
        validate_material_worker_ready_identity(
            _ready(),
            expected_execution_id=EXECUTION_ID,
            expected_attempt_id=ATTEMPT_ID,
            expected_operation="official_capture",
            expected_root_pid=4243,
            expected_request_sha256=REQUEST_SHA256,
        )


def test_ready_encoder_rejects_wrong_runtime_type_and_oversized_payload() -> None:
    with pytest.raises(MaterialProtocolError):
        encode_material_worker_ready({})  # type: ignore[arg-type]

    oversized = _ready(request_sha256=REQUEST_SHA256)
    # The limit is an encoded-document limit, not a Python object limit.
    assert len(encode_material_worker_ready(oversized)) < MAX_READY_BYTES
    with pytest.raises(MaterialProtocolError):
        decode_material_worker_ready(b"{" + b"a" * MAX_READY_BYTES)
