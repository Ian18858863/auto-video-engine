from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import sys

import pytest

from app.materials.stock_rights_evidence import build_stock_rights_evidence
from app.pipeline import material_operation_protocol as protocol
from app.pipeline.material_operation_protocol import (
    MAX_API_KEY_CHARS,
    MAX_ARTIFACTS,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_REQUEST_BYTES,
    MAX_RESULT_BYTES,
    MAX_SECRET_BYTES,
    MAX_STRING_CHARS,
    CoverrSecrets,
    InfoMotionPayload,
    InfoMotionSecrets,
    MaterialOperationBlocked,
    MaterialOperationOutcome,
    MaterialOperationRequest,
    MaterialOperationSecrets,
    MaterialProtocolError,
    MaterialWorkerResult,
    OfficialCapturePayload,
    OfficialCaptureSecrets,
    PexelsSecrets,
    PixabaySecrets,
    StagedArtifact,
    StockSearchPayload,
    TerminationEvidence,
    decode_material_operation_request,
    decode_material_operation_secrets,
    decode_material_worker_result,
    encode_material_operation_request,
    encode_material_operation_secrets,
    encode_material_worker_result,
)


EXECUTION_ID = "123e4567-e89b-12d3-a456-426614174000"
ATTEMPT_ID = "123e4567-e89b-12d3-a456-426614174001"
OTHER_ATTEMPT_ID = "123e4567-e89b-12d3-a456-426614174002"
TEST_API_KEY = "test-canary-not-a-real-key"
PEXELS_TEST_API_KEY = "pexels-v2-canary-not-a-real-key"
PIXABAY_TEST_API_KEY = "pixabay-v2-canary-not-a-real-key"


def _json_bytes(value: object, *, sort_keys: bool = False) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=sort_keys,
        separators=(",", ":"),
    ).encode("utf-8")


def _request_for(operation: str) -> MaterialOperationRequest:
    if operation == "official_capture":
        payload = OfficialCapturePayload(
            source_url="https://example.com/news/1",
            source_name="Example",
            page_title="Example report",
        )
    elif operation in {"pexels", "pixabay", "coverr", "stock_candidates"}:
        payload = StockSearchPayload(query="technology office", per_page=10)
    elif operation == "info_motion":
        payload = InfoMotionPayload(
            project_name="Protocol test",
            title="How the workflow works",
        )
    else:
        raise AssertionError(f"unsupported test operation: {operation}")
    return MaterialOperationRequest(
        protocol_version=2 if operation == "stock_candidates" else 1,
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        operation=operation,  # type: ignore[arg-type]
        payload=payload,
    )


def _request_dict(operation: str = "official_capture") -> dict[str, object]:
    request = _request_for(operation)
    return json.loads(encode_material_operation_request(request))


def _artifact(path: str = "payload/video.mp4") -> StagedArtifact:
    return StagedArtifact(
        relative_path=path,
        media_kind="video",
        source_type="Pexels",
        source_url="https://example.com/video/1",
        evidence_kind="atmosphere",
        is_locally_generated=False,
        sha256="a" * 64,
        size_bytes=123,
    )


def _stock_candidate_artifact(
    index: int,
    *,
    source_type: str,
    media_kind: str = "video",
) -> StagedArtifact:
    suffix = "jpg" if media_kind == "image" else "mp4"
    provider_slug = source_type.casefold()
    if source_type == "Pexels":
        source_url = f"https://www.pexels.com/video/example-{index}/"
    else:
        source_url = f"https://pixabay.com/videos/id-{index}/"
    rights_evidence = build_stock_rights_evidence(
        provider=source_type,
        provider_asset_id=str(index),
        media_type="photo" if media_kind == "image" else "video",
        asset_page_url=source_url,
        contributor_name=f"{source_type} contributor {index}",
        search_query="technology office",
        observed_at="2026-07-18T00:00:00+00:00",
    )
    return StagedArtifact(
        relative_path=f"payload/candidates/{provider_slug}-{index}.{suffix}",
        media_kind=media_kind,  # type: ignore[arg-type]
        source_type=source_type,
        source_url=source_url,
        evidence_kind="atmosphere",
        is_locally_generated=False,
        sha256=f"{index:x}" * 64,
        size_bytes=100 + index,
        rights_evidence=rights_evidence,
    )


def _stock_candidates_result(
    artifacts: tuple[StagedArtifact, ...] | None = None,
    *,
    selected_artifact: str = "",
) -> MaterialWorkerResult:
    return MaterialWorkerResult(
        protocol_version=2,
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        operation="stock_candidates",
        status="succeeded",
        selected_artifact=selected_artifact,
        artifacts=artifacts
        or (
            _stock_candidate_artifact(1, source_type="Pexels"),
            _stock_candidate_artifact(2, source_type="Pexels", media_kind="image"),
            _stock_candidate_artifact(3, source_type="Pixabay"),
        ),
        reason_code="",
        reason="candidate batch acquired",
    )


def _succeeded_result() -> MaterialWorkerResult:
    return MaterialWorkerResult(
        protocol_version=1,
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        operation="pexels",
        status="succeeded",
        selected_artifact="payload/video.mp4",
        artifacts=(_artifact(),),
        reason_code="",
        reason="material acquired",
    )


def _result_dict() -> dict[str, object]:
    return json.loads(encode_material_worker_result(_succeeded_result()))


def _decode_result(raw: bytes) -> MaterialWorkerResult:
    return decode_material_worker_result(
        raw,
        expected_execution_id=EXECUTION_ID,
        expected_attempt_id=ATTEMPT_ID,
        expected_operation="pexels",
    )


def test_worker_result_round_trips_bounded_stock_rights_evidence() -> None:
    rights_evidence = build_stock_rights_evidence(
        provider="Pexels",
        provider_asset_id="123",
        media_type="video",
        asset_page_url="https://www.pexels.com/video/example-123/",
        contributor_id="7",
        contributor_name="Ada",
        contributor_profile_url="https://www.pexels.com/@ada/",
        search_query="technology office",
        observed_at="2026-07-18T09:30:00+00:00",
    )
    artifact = StagedArtifact(
        relative_path="payload/video.mp4",
        media_kind="video",
        source_type="Pexels",
        source_url="https://www.pexels.com/video/example-123/",
        evidence_kind="atmosphere",
        is_locally_generated=False,
        sha256="a" * 64,
        size_bytes=123,
        rights_evidence=rights_evidence,
    )
    result = MaterialWorkerResult(
        protocol_version=1,
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        operation="pexels",
        status="succeeded",
        selected_artifact=artifact.relative_path,
        artifacts=(artifact,),
        reason_code="",
        reason="material acquired",
    )

    encoded = encode_material_worker_result(result)
    decoded = _decode_result(encoded)

    assert decoded == result
    payload = json.loads(encoded)
    assert payload["artifacts"][0]["rights_evidence"]["provider"] == "Pexels"
    assert "download_url" not in encoded.decode("utf-8")


def test_official_capture_request_round_trip_is_typed() -> None:
    request = MaterialOperationRequest(
        protocol_version=1,
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        operation="official_capture",
        payload=OfficialCapturePayload(
            source_url="https://example.com/news/1",
            source_name="Example",
            page_title="Example report",
        ),
    )

    encoded = encode_material_operation_request(request)

    assert decode_material_operation_request(encoded) == request
    assert encoded.startswith(b'{')
    assert not encoded.startswith(b'\xef\xbb\xbf')


@pytest.mark.parametrize(
    ("operation", "payload_type"),
    [
        ("official_capture", OfficialCapturePayload),
        ("pexels", StockSearchPayload),
        ("pixabay", StockSearchPayload),
        ("coverr", StockSearchPayload),
        ("info_motion", InfoMotionPayload),
    ],
)
def test_all_five_operations_round_trip_to_operation_specific_payloads(
    operation: str,
    payload_type: type[object],
) -> None:
    request = _request_for(operation)

    decoded = decode_material_operation_request(
        encode_material_operation_request(request)
    )

    assert decoded == request
    assert type(decoded.payload) is payload_type


def test_coverr_is_protocol_legal_without_enabling_any_default_chain() -> None:
    request = _request_for("coverr")

    decoded = decode_material_operation_request(
        encode_material_operation_request(request)
    )

    assert decoded.operation == "coverr"
    assert not hasattr(protocol, "DEFAULT_SOURCE_CHAIN")


def test_stock_candidates_request_is_strict_protocol_v2_only() -> None:
    request = _request_for("stock_candidates")

    encoded = encode_material_operation_request(request)
    decoded = decode_material_operation_request(encoded)

    assert decoded == request
    assert decoded.protocol_version == 2
    assert decoded.operation == "stock_candidates"
    assert json.loads(encoded)["payload"] == {
        "per_page": 10,
        "query": "technology office",
    }
    with pytest.raises(MaterialProtocolError, match="protocol_version"):
        MaterialOperationRequest(
            protocol_version=1,
            execution_id=EXECUTION_ID,
            attempt_id=ATTEMPT_ID,
            operation="stock_candidates",  # type: ignore[arg-type]
            payload=StockSearchPayload(query="technology office", per_page=10),
        )
    with pytest.raises(MaterialProtocolError, match="protocol_version"):
        MaterialOperationRequest(
            protocol_version=2,
            execution_id=EXECUTION_ID,
            attempt_id=ATTEMPT_ID,
            operation="pexels",
            payload=StockSearchPayload(query="technology office", per_page=10),
        )


def test_legacy_v1_request_encoding_remains_byte_for_byte_compatible() -> None:
    encoded = encode_material_operation_request(_request_for("pexels"))

    assert encoded == (
        f'{{"attempt_id":"{ATTEMPT_ID}","execution_id":"{EXECUTION_ID}",'
        '"operation":"pexels","payload":{"per_page":10,'
        '"query":"technology office"},"protocol_version":1}'
    ).encode("utf-8")

    secrets = encode_material_operation_secrets(
        _secrets_for("pexels"),
        request=_request_for("pexels"),
    )
    result = encode_material_worker_result(_succeeded_result())
    assert hashlib.sha256(secrets).hexdigest() == (
        "8828ce82384a90f62a8651f05c42fb1ac49a3a835442cce3f0c2ff84286aaa5f"
    )
    assert hashlib.sha256(result).hexdigest() == (
        "bfe3db05c861ad28eea861fcd51d36e251f091c338a293a1f11c054e13305c1f"
    )


def test_request_canonical_encoding_is_deterministic_utf8_without_bom() -> None:
    raw = (
        '{"payload":{"page_title":"中文标题","source_name":"Example",'
        '"source_url":"https://example.com/1"},"operation":"official_capture",'
        f'"attempt_id":"{ATTEMPT_ID}","execution_id":"{EXECUTION_ID}",'
        '"protocol_version":1}'
    ).encode("utf-8")
    request = decode_material_operation_request(raw)

    first = encode_material_operation_request(request)
    second = encode_material_operation_request(request)

    assert first == second
    assert first == _json_bytes(json.loads(first), sort_keys=True)
    assert "中文标题".encode("utf-8") in first
    assert not first.startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize(
    "raw",
    [
        b"\xef\xbb\xbf{}",
        b"{} ",
        b"\n{}",
        b"{}{}",
        b"\xff",
        b'{"protocol_version":NaN}',
        b'{"protocol_version":Infinity}',
        b'{"protocol_version":-Infinity}',
        b'{"protocol_version":1.0}',
    ],
)
def test_request_rejects_bom_non_utf8_float_constant_and_any_trailing_data(
    raw: bytes,
) -> None:
    with pytest.raises(MaterialProtocolError):
        decode_material_operation_request(raw)


@pytest.mark.parametrize(
    "raw",
    [
        (
            '{"protocol_version":1,'
            f'"execution_id":"{EXECUTION_ID}","attempt_id":"{ATTEMPT_ID}",'
            '"operation":"official_capture","payload":{"source_url":'
            '"https://example.com","source_name":"Example",'
            '"page_title":"\\ud800"}}'
        ).encode("ascii"),
        (
            '{"protocol_version":1,'
            f'"execution_id":"{EXECUTION_ID}","attempt_id":"{ATTEMPT_ID}",'
            '"operation":"official_capture","payload":{"source_url":'
            '"https://example.com","source_name":"Example","page_title":"Title"},'
            '"\\udfff":"value"}'
        ).encode("ascii"),
    ],
)
def test_request_rejects_escaped_unpaired_surrogate_keys_and_values(
    raw: bytes,
) -> None:
    with pytest.raises(MaterialProtocolError, match="surrogate"):
        decode_material_operation_request(raw)


def test_request_rejects_duplicate_keys_at_any_level() -> None:
    top_level = (
        '{"protocol_version":1,"protocol_version":1,'
        f'"execution_id":"{EXECUTION_ID}","attempt_id":"{ATTEMPT_ID}",'
        '"operation":"official_capture","payload":{"source_url":'
        '"https://example.com","source_name":"Example","page_title":"Title"}}'
    ).encode()
    nested = (
        '{"protocol_version":1,'
        f'"execution_id":"{EXECUTION_ID}","attempt_id":"{ATTEMPT_ID}",'
        '"operation":"official_capture","payload":{"source_url":'
        '"https://example.com","source_url":"https://example.com",'
        '"source_name":"Example","page_title":"Title"}}'
    ).encode()

    for raw in (top_level, nested):
        with pytest.raises(MaterialProtocolError, match="duplicate"):
            decode_material_operation_request(raw)


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "api_key",
        "secret",
        "session",
        "callable",
        "module",
        "class",
        "function",
        "output",
        "project_dir",
    ],
)
def test_request_rejects_malicious_capability_and_secret_fields(
    forbidden_key: str,
) -> None:
    value = _request_dict()
    value[forbidden_key] = "must-not-be-accepted"

    with pytest.raises(MaterialProtocolError, match="exact keys"):
        decode_material_operation_request(_json_bytes(value))


@pytest.mark.parametrize(
    "operation",
    ["shell", "python", "dynamic", "Official_Capture", ""],
)
def test_request_rejects_unknown_or_noncanonical_operation(operation: str) -> None:
    value = _request_dict()
    value["operation"] = operation

    with pytest.raises(MaterialProtocolError, match="operation"):
        decode_material_operation_request(_json_bytes(value))


def test_request_requires_exact_keys_and_operation_payload_shape() -> None:
    missing = _request_dict()
    del missing["attempt_id"]
    extra_payload = _request_dict()
    assert isinstance(extra_payload["payload"], dict)
    extra_payload["payload"]["url"] = "https://unexpected.example"
    wrong_payload = _request_dict("pexels")
    wrong_payload["payload"] = {
        "source_url": "https://example.com",
        "source_name": "Example",
        "page_title": "Title",
    }

    for value in (missing, extra_payload, wrong_payload):
        with pytest.raises(MaterialProtocolError, match="exact keys"):
            decode_material_operation_request(_json_bytes(value))


def test_request_rejects_bool_as_int_noncanonical_or_equal_ids() -> None:
    bool_version = _request_dict()
    bool_version["protocol_version"] = True
    bool_per_page = _request_dict("pexels")
    assert isinstance(bool_per_page["payload"], dict)
    bool_per_page["payload"]["per_page"] = True
    uppercase_uuid = _request_dict()
    uppercase_uuid["execution_id"] = EXECUTION_ID.upper()
    equal_ids = _request_dict()
    equal_ids["attempt_id"] = EXECUTION_ID

    for value in (bool_version, bool_per_page, uppercase_uuid, equal_ids):
        with pytest.raises(MaterialProtocolError):
            decode_material_operation_request(_json_bytes(value))


def test_operation_and_payload_types_must_match_even_for_direct_construction() -> None:
    with pytest.raises(MaterialProtocolError, match="payload"):
        MaterialOperationRequest(
            protocol_version=1,
            execution_id=EXECUTION_ID,
            attempt_id=ATTEMPT_ID,
            operation="pexels",
            payload=OfficialCapturePayload(
                source_url="https://example.com",
                source_name="Example",
                page_title="Title",
            ),
        )


@pytest.mark.parametrize("control", ["\x01", "\x1f", "\x7f", "\x80", "\x85", "\x9f", "\r\n"])
def test_all_protocol_text_rejects_c0_c1_and_line_controls(control: str) -> None:
    with pytest.raises(MaterialProtocolError, match="control"):
        StockSearchPayload(query=f"technology{control}office", per_page=10)
    with pytest.raises(MaterialProtocolError, match="control"):
        InfoMotionPayload(project_name="Project", title=f"Title{control}")
    with pytest.raises(MaterialProtocolError, match="control"):
        OfficialCapturePayload(
            source_url="https://example.com",
            source_name=f"Example{control}",
            page_title="Title",
        )
    with pytest.raises(MaterialProtocolError, match="control"):
        PexelsSecrets(api_key=f"fake{control}key")
    with pytest.raises(MaterialProtocolError, match="control"):
        _artifact(f"payload/clips/a{control}b.mp4")
    with pytest.raises(MaterialProtocolError, match="control"):
        MaterialWorkerResult(
            protocol_version=1,
            execution_id=EXECUTION_ID,
            attempt_id=ATTEMPT_ID,
            operation="pexels",
            status="succeeded",
            selected_artifact="payload/video.mp4",
            artifacts=(_artifact(),),
            reason_code="",
            reason=f"done{control}",
        )


def test_json_object_keys_reject_c0_and_c1_controls_before_schema_matching() -> None:
    value = _request_dict()
    value["unsafe\u0085key"] = "value"

    with pytest.raises(MaterialProtocolError, match="control"):
        decode_material_operation_request(_json_bytes(value))


def test_encoders_revalidate_nested_frozen_payloads_after_bypass() -> None:
    request = _request_for("pexels")
    assert type(request.payload) is StockSearchPayload
    object.__setattr__(request.payload, "per_page", True)

    with pytest.raises(MaterialProtocolError, match="per_page"):
        encode_material_operation_request(request)

    clean_request = _request_for("pexels")
    secrets = _secrets_for("pexels")
    assert type(secrets.payload) is PexelsSecrets
    object.__setattr__(secrets.payload, "api_key", "")

    with pytest.raises(MaterialProtocolError, match="api_key"):
        encode_material_operation_secrets(secrets, request=clean_request)


def test_request_enforces_byte_depth_node_and_string_limits() -> None:
    with pytest.raises(MaterialProtocolError, match="64 KiB"):
        decode_material_operation_request(b"x" * (MAX_REQUEST_BYTES + 1))

    deep: object = "leaf"
    for _ in range(MAX_JSON_DEPTH + 2):
        deep = {"x": deep}
    deep_value = _request_dict()
    deep_value["unexpected"] = deep
    with pytest.raises(MaterialProtocolError, match="depth"):
        decode_material_operation_request(_json_bytes(deep_value))

    many_nodes = _request_dict()
    many_nodes["unexpected"] = list(range(MAX_JSON_NODES + 1))
    with pytest.raises(MaterialProtocolError, match="node"):
        decode_material_operation_request(_json_bytes(many_nodes))

    long_string = _request_dict()
    assert isinstance(long_string["payload"], dict)
    long_string["payload"]["page_title"] = "x" * (MAX_STRING_CHARS + 1)
    with pytest.raises(MaterialProtocolError, match="string"):
        decode_material_operation_request(_json_bytes(long_string))


@pytest.mark.parametrize("sign", ["", "-"])
@pytest.mark.parametrize("python_int_limit", [0, 4300])
def test_json_integer_is_bounded_before_python_int_conversion(
    sign: str,
    python_int_limit: int,
) -> None:
    raw = (
        '{"protocol_version":'
        + sign
        + ("9" * 5000)
        + f',"execution_id":"{EXECUTION_ID}","attempt_id":"{ATTEMPT_ID}",'
        + '"operation":"official_capture","payload":{"source_url":'
        + '"https://example.com","source_name":"Example","page_title":"Title"}}'
    ).encode("ascii")
    previous_limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(python_int_limit)
        with pytest.raises(MaterialProtocolError, match="integer") as captured:
            decode_material_operation_request(raw)
    finally:
        sys.set_int_max_str_digits(previous_limit)

    assert type(captured.value) is MaterialProtocolError
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert len(str(captured.value)) < 200


@pytest.mark.parametrize(
    "path",
    [
        "",
        "payload",
        "payload/",
        "/payload/a.mp4",
        "//server/share/a.mp4",
        "C:/payload/a.mp4",
        "payload\\a.mp4",
        "payload//a.mp4",
        "payload/./a.mp4",
        "payload/../a.mp4",
        "./payload/a.mp4",
        "payload/a:b.mp4",
        "payload/\x00a.mp4",
        "payload/a.mp4.",
        "payload/a.mp4 ",
        "payload/CON.mp4",
        "payload/a?.mp4",
        "payload/a*.mp4",
        "payload/a|b.mp4",
        "payload/a<b.mp4",
        "payload/a>b.mp4",
        'payload/a"b.mp4',
        "payload/COM¹.txt",
        "payload/com².mp4",
        "payload/Com³",
        "payload/LPT¹.txt",
        "payload/lpt².mp4",
        "payload/Lpt³",
        "payload/CONIN$.txt",
        "payload/conout$.mp4",
    ],
)
def test_staged_artifact_rejects_hostile_or_noncanonical_paths(path: str) -> None:
    with pytest.raises(MaterialProtocolError, match="relative_path"):
        _artifact(path)


def test_staged_artifact_accepts_normalized_posix_payload_path() -> None:
    artifact = _artifact("payload/镜头/clip-01.mp4")

    assert artifact.relative_path == "payload/镜头/clip-01.mp4"


def test_staged_artifact_requires_lowercase_sha_positive_non_bool_size() -> None:
    base = {
        "relative_path": "payload/video.mp4",
        "media_kind": "video",
        "source_type": "Pexels",
        "source_url": "https://example.com/video/1",
        "evidence_kind": "atmosphere",
        "is_locally_generated": False,
    }

    for sha256, size in (("A" * 64, 1), ("a" * 63, 1), ("a" * 64, 0), ("a" * 64, True)):
        with pytest.raises(MaterialProtocolError):
            StagedArtifact(**base, sha256=sha256, size_bytes=size)  # type: ignore[arg-type]


def test_staged_artifact_calls_public_execution_provenance_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    original = protocol.material_provenance.validate_execution_provenance

    def spy(**kwargs: object) -> None:
        calls.append(kwargs)
        original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        protocol.material_provenance,
        "validate_execution_provenance",
        spy,
    )

    _artifact()

    assert calls == [
        {
            "success": True,
            "evidence_kind": "atmosphere",
            "source_url": "https://example.com/video/1",
            "is_locally_generated": False,
        }
    ]


def test_staged_artifact_rejects_invalid_success_provenance() -> None:
    with pytest.raises(MaterialProtocolError, match="provenance"):
        StagedArtifact(
            relative_path="payload/video.mp4",
            media_kind="video",
            source_type="fake",
            source_url="",
            evidence_kind="direct_evidence",
            is_locally_generated=True,
            sha256="a" * 64,
            size_bytes=1,
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://",
        "https://:443/path",
        "https://user:password@example.com/path",
        "https://example.com:99999/path",
        "https://example.com:not-a-port/path",
        "https://exa\\mple.com/path",
        "https://example.com/white space",
        "https://[invalid/path",
        "https://e\u0301xample.com/path",
    ],
)
def test_http_url_validation_rejects_ambiguous_or_credentialed_urls(
    url: str,
) -> None:
    with pytest.raises(MaterialProtocolError) as captured:
        OfficialCapturePayload(
            source_url=url,
            source_name="Example",
            page_title="Title",
        )

    assert type(captured.value) is MaterialProtocolError
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_http_url_errors_do_not_retain_userinfo_or_invalid_port_credentials() -> None:
    credential = "url-port-credential-canary"
    for url in (
        f"https://user:{credential}@example.com/path",
        f"https://example.com:{credential}/path",
    ):
        with pytest.raises(MaterialProtocolError) as captured:
            OfficialCapturePayload(
                source_url=url,
                source_name="Example",
                page_title="Title",
            )

        assert credential not in _exception_graph_text(captured.value)
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None


def test_http_url_validation_accepts_normalized_idna_hostname() -> None:
    payload = OfficialCapturePayload(
        source_url="https://例子.测试/路径",
        source_name="Example",
        page_title="Title",
    )

    assert payload.source_url == "https://例子.测试/路径"


def test_worker_result_round_trip_is_typed_canonical_and_id_bound() -> None:
    result = _succeeded_result()

    encoded = encode_material_worker_result(result)
    decoded = _decode_result(encoded)

    assert decoded == result
    assert type(decoded) is MaterialWorkerResult
    assert type(decoded.artifacts) is tuple
    assert type(decoded.artifacts[0]) is StagedArtifact
    assert encoded == _json_bytes(json.loads(encoded), sort_keys=True)
    with pytest.raises(MaterialProtocolError, match="execution_id"):
        decode_material_worker_result(
            encoded,
            expected_execution_id="123e4567-e89b-12d3-a456-426614174009",
            expected_attempt_id=ATTEMPT_ID,
            expected_operation="pexels",
        )
    with pytest.raises(MaterialProtocolError, match="attempt_id"):
        decode_material_worker_result(
            encoded,
            expected_execution_id=EXECUTION_ID,
            expected_attempt_id=OTHER_ATTEMPT_ID,
            expected_operation="pexels",
        )


def test_worker_result_operation_is_required_allowed_and_request_bound() -> None:
    encoded = encode_material_worker_result(_succeeded_result())
    with pytest.raises(MaterialProtocolError, match="operation"):
        decode_material_worker_result(
            encoded,
            expected_execution_id=EXECUTION_ID,
            expected_attempt_id=ATTEMPT_ID,
            expected_operation="pixabay",
        )
    with pytest.raises(MaterialProtocolError, match="operation"):
        decode_material_worker_result(
            encoded,
            expected_execution_id=EXECUTION_ID,
            expected_attempt_id=ATTEMPT_ID,
            expected_operation="unknown",  # type: ignore[arg-type]
        )

    missing = _result_dict()
    del missing["operation"]
    with pytest.raises(MaterialProtocolError, match="exact keys"):
        _decode_result(_json_bytes(missing))
    for invalid in ("unknown", 7, None):
        changed = _result_dict()
        changed["operation"] = invalid
        with pytest.raises(MaterialProtocolError, match="operation"):
            _decode_result(_json_bytes(changed))


def test_worker_result_succeeded_requires_nonempty_artifacts_and_unique_selection() -> None:
    with pytest.raises(MaterialProtocolError, match="non-empty"):
        MaterialWorkerResult(
            protocol_version=1,
            execution_id=EXECUTION_ID,
            attempt_id=ATTEMPT_ID,
            operation="pexels",
            status="succeeded",
            selected_artifact="payload/video.mp4",
            artifacts=(),
            reason_code="",
            reason="",
        )
    with pytest.raises(MaterialProtocolError, match="exactly one"):
        MaterialWorkerResult(
            protocol_version=1,
            execution_id=EXECUTION_ID,
            attempt_id=ATTEMPT_ID,
            operation="pexels",
            status="succeeded",
            selected_artifact="payload/missing.mp4",
            artifacts=(_artifact(),),
            reason_code="",
            reason="",
        )
    with pytest.raises(MaterialProtocolError, match="exactly one"):
        MaterialWorkerResult(
            protocol_version=1,
            execution_id=EXECUTION_ID,
            attempt_id=ATTEMPT_ID,
            operation="pexels",
            status="succeeded",
            selected_artifact="payload/video.mp4",
            artifacts=(_artifact(), _artifact()),
            reason_code="",
            reason="",
        )


def test_stock_candidates_success_round_trips_without_automatic_selection() -> None:
    result = _stock_candidates_result()

    encoded = encode_material_worker_result(result)
    decoded = decode_material_worker_result(
        encoded,
        expected_execution_id=EXECUTION_ID,
        expected_attempt_id=ATTEMPT_ID,
        expected_operation="stock_candidates",
    )

    assert decoded == result
    assert decoded.protocol_version == 2
    assert decoded.selected_artifact == ""
    assert {item.source_type for item in decoded.artifacts} == {"Pexels", "Pixabay"}


@pytest.mark.parametrize("count", [2, 6])
def test_stock_candidates_success_requires_three_to_five_candidates(count: int) -> None:
    artifacts = tuple(
        _stock_candidate_artifact(
            index,
            source_type="Pixabay" if index == count else "Pexels",
        )
        for index in range(1, count + 1)
    )

    with pytest.raises(MaterialProtocolError, match="3 to 5"):
        _stock_candidates_result(artifacts)


def test_stock_candidates_success_requires_both_sources_and_unique_fingerprints() -> None:
    pexels_only = tuple(
        _stock_candidate_artifact(index, source_type="Pexels")
        for index in range(1, 4)
    )
    duplicate = list(_stock_candidates_result().artifacts)
    duplicate[2] = StagedArtifact(
        relative_path=duplicate[2].relative_path,
        media_kind=duplicate[2].media_kind,
        source_type=duplicate[2].source_type,
        source_url=duplicate[2].source_url,
        evidence_kind=duplicate[2].evidence_kind,
        is_locally_generated=duplicate[2].is_locally_generated,
        sha256=duplicate[0].sha256,
        size_bytes=duplicate[2].size_bytes,
    )

    with pytest.raises(MaterialProtocolError, match="Pexels and Pixabay"):
        _stock_candidates_result(pexels_only)
    with pytest.raises(MaterialProtocolError, match="fingerprint"):
        _stock_candidates_result(tuple(duplicate))
    with pytest.raises(MaterialProtocolError, match="empty selected_artifact"):
        _stock_candidates_result(selected_artifact="payload/candidates/pexels-1.mp4")


def test_stock_candidates_success_rejects_placeholder_or_generated_artifacts() -> None:
    artifacts = list(_stock_candidates_result().artifacts)
    artifacts[0] = StagedArtifact(
        relative_path=artifacts[0].relative_path,
        media_kind=artifacts[0].media_kind,
        source_type=artifacts[0].source_type,
        source_url="",
        evidence_kind="explanation",
        is_locally_generated=True,
        sha256=artifacts[0].sha256,
        size_bytes=artifacts[0].size_bytes,
    )

    with pytest.raises(MaterialProtocolError, match="real provider artifacts"):
        _stock_candidates_result(tuple(artifacts))


def test_stock_candidates_success_requires_rights_evidence_bound_to_artifact() -> None:
    artifacts = list(_stock_candidates_result().artifacts)
    first = artifacts[0]
    artifacts[0] = StagedArtifact(
        relative_path=first.relative_path,
        media_kind=first.media_kind,
        source_type=first.source_type,
        source_url=first.source_url,
        evidence_kind=first.evidence_kind,
        is_locally_generated=first.is_locally_generated,
        sha256=first.sha256,
        size_bytes=first.size_bytes,
        rights_evidence=None,
    )

    with pytest.raises(MaterialProtocolError, match="rights_evidence"):
        _stock_candidates_result(tuple(artifacts))


@pytest.mark.parametrize(
    ("source_type", "media_kind", "rights_provider", "rights_media_type"),
    [
        ("Pexels", "video", "Pixabay", "video"),
        ("Pexels", "image", "Pexels", "video"),
    ],
)
def test_stock_candidates_success_rejects_mismatched_rights_binding(
    source_type: str,
    media_kind: str,
    rights_provider: str,
    rights_media_type: str,
) -> None:
    artifacts = list(_stock_candidates_result().artifacts)
    source_url = (
        "https://www.pexels.com/video/mismatch-99/"
        if rights_provider == "Pexels"
        else "https://pixabay.com/videos/id-99/"
    )
    rights = build_stock_rights_evidence(
        provider=rights_provider,
        provider_asset_id="99",
        media_type=rights_media_type,
        asset_page_url=source_url,
        contributor_name="Mismatch contributor",
        search_query="technology office",
        observed_at="2026-07-18T00:00:00+00:00",
    )
    artifacts[0] = StagedArtifact(
        relative_path=artifacts[0].relative_path,
        media_kind=media_kind,  # type: ignore[arg-type]
        source_type=source_type,
        source_url=source_url,
        evidence_kind=artifacts[0].evidence_kind,
        is_locally_generated=False,
        sha256=artifacts[0].sha256,
        size_bytes=artifacts[0].size_bytes,
        rights_evidence=rights,
    )

    with pytest.raises(MaterialProtocolError, match="rights_evidence"):
        _stock_candidates_result(tuple(artifacts))


def test_worker_result_rejects_windows_case_insensitive_duplicate_paths() -> None:
    first = _artifact("payload/Clips/Video.mp4")
    second = StagedArtifact(
        relative_path="payload/clips/video.MP4",
        media_kind="video",
        source_type="Pexels",
        source_url="https://example.com/video/2",
        evidence_kind="atmosphere",
        is_locally_generated=False,
        sha256="b" * 64,
        size_bytes=456,
    )

    with pytest.raises(MaterialProtocolError, match="case-insensitive"):
        MaterialWorkerResult(
            protocol_version=1,
            execution_id=EXECUTION_ID,
            attempt_id=ATTEMPT_ID,
            operation="pexels",
            status="succeeded",
            selected_artifact=first.relative_path,
            artifacts=(first, second),
            reason_code="",
            reason="",
        )


def test_worker_result_failed_requires_empty_selected_artifact() -> None:
    result = MaterialWorkerResult(
        protocol_version=1,
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        operation="pexels",
        status="failed",
        selected_artifact="",
        artifacts=(),
        reason_code="source_unavailable",
        reason="No suitable material was found.",
    )

    assert _decode_result(encode_material_worker_result(result)) == result
    with pytest.raises(MaterialProtocolError, match="empty selected_artifact"):
        MaterialWorkerResult(
            protocol_version=1,
            execution_id=EXECUTION_ID,
            attempt_id=ATTEMPT_ID,
            operation="pexels",
            status="failed",
            selected_artifact="payload/video.mp4",
            artifacts=(_artifact(),),
            reason_code="source_unavailable",
            reason="failed",
        )
    with pytest.raises(MaterialProtocolError, match="empty artifact tuple"):
        MaterialWorkerResult(
            protocol_version=1,
            execution_id=EXECUTION_ID,
            attempt_id=ATTEMPT_ID,
            operation="pexels",
            status="failed",
            selected_artifact="",
            artifacts=(_artifact(),),
            reason_code="source_unavailable",
            reason="failed",
        )


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "api_key",
        "secret",
        "session",
        "callable",
        "module",
        "class",
        "function",
        "output",
        "project_dir",
    ],
)
def test_result_rejects_malicious_capability_and_secret_fields(
    forbidden_key: str,
) -> None:
    value = _result_dict()
    value[forbidden_key] = "must-not-be-accepted"

    with pytest.raises(MaterialProtocolError, match="exact keys"):
        _decode_result(_json_bytes(value))


def test_result_artifact_rejects_unknown_fields_and_bool_size() -> None:
    unknown = _result_dict()
    assert isinstance(unknown["artifacts"], list)
    assert isinstance(unknown["artifacts"][0], dict)
    unknown["artifacts"][0]["session"] = "forbidden"
    bool_size = _result_dict()
    assert isinstance(bool_size["artifacts"], list)
    assert isinstance(bool_size["artifacts"][0], dict)
    bool_size["artifacts"][0]["size_bytes"] = True

    with pytest.raises(MaterialProtocolError, match="exact keys"):
        _decode_result(_json_bytes(unknown))
    with pytest.raises(MaterialProtocolError, match="size_bytes"):
        _decode_result(_json_bytes(bool_size))


@pytest.mark.parametrize("location", ["value", "key"])
def test_result_rejects_escaped_unpaired_surrogate_keys_and_values(
    location: str,
) -> None:
    value = _result_dict()
    if location == "value":
        assert isinstance(value["artifacts"], list)
        assert isinstance(value["artifacts"][0], dict)
        value["artifacts"][0]["source_type"] = "SURROGATE"
        raw = _json_bytes(value).replace(b'"SURROGATE"', b'"\\ud800"')
    else:
        raw = _json_bytes(value)[:-1] + b',"\\udfff":"value"}'

    with pytest.raises(MaterialProtocolError, match="surrogate"):
        _decode_result(raw)


def test_result_enforces_byte_and_artifact_limits() -> None:
    with pytest.raises(MaterialProtocolError, match="1 MiB"):
        _decode_result(b"x" * (MAX_RESULT_BYTES + 1))

    value = _result_dict()
    artifact = value["artifacts"][0]  # type: ignore[index]
    value["artifacts"] = [
        {
            **artifact,  # type: ignore[arg-type]
            "relative_path": f"payload/items/{index}.mp4",
        }
        for index in range(MAX_ARTIFACTS + 1)
    ]
    value["selected_artifact"] = "payload/items/0.mp4"
    with pytest.raises(MaterialProtocolError, match="artifact"):
        _decode_result(_json_bytes(value))


def _secrets_for(operation: str) -> MaterialOperationSecrets:
    payload: object
    if operation == "official_capture":
        payload = OfficialCaptureSecrets()
    elif operation == "pexels":
        payload = PexelsSecrets(api_key=TEST_API_KEY)
    elif operation == "pixabay":
        payload = PixabaySecrets(api_key=TEST_API_KEY)
    elif operation == "stock_candidates":
        payload = protocol.StockCandidateSecrets(
            pexels_api_key=PEXELS_TEST_API_KEY,
            pixabay_api_key=PIXABAY_TEST_API_KEY,
        )
    elif operation == "coverr":
        payload = CoverrSecrets()
    elif operation == "info_motion":
        payload = InfoMotionSecrets()
    else:
        raise AssertionError(operation)
    return MaterialOperationSecrets(
        version=2 if operation == "stock_candidates" else 1,
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        operation=operation,  # type: ignore[arg-type]
        payload=payload,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("operation", "payload_type"),
    [
        ("official_capture", OfficialCaptureSecrets),
        ("pexels", PexelsSecrets),
        ("pixabay", PixabaySecrets),
        ("coverr", CoverrSecrets),
        ("info_motion", InfoMotionSecrets),
    ],
)
def test_secret_envelope_round_trip_is_exact_typed_and_request_bound(
    operation: str,
    payload_type: type[object],
) -> None:
    request = _request_for(operation)
    secrets = _secrets_for(operation)

    encoded = encode_material_operation_secrets(secrets, request=request)
    decoded = decode_material_operation_secrets(encoded, request=request)

    assert decoded == secrets
    assert type(decoded.payload) is payload_type
    assert len(encoded) <= MAX_SECRET_BYTES
    assert encoded == _json_bytes(json.loads(encoded), sort_keys=True)


def test_stock_candidate_dual_secret_round_trip_is_v2_strict_and_redacted() -> None:
    request = _request_for("stock_candidates")
    secrets = _secrets_for("stock_candidates")

    encoded = encode_material_operation_secrets(secrets, request=request)
    decoded = decode_material_operation_secrets(encoded, request=request)
    value = json.loads(encoded)

    assert decoded == secrets
    assert decoded.version == 2
    assert type(decoded.payload).__name__ == "StockCandidateSecrets"
    assert set(value["payload"]) == {"pexels_api_key", "pixabay_api_key"}
    for secret in (PEXELS_TEST_API_KEY, PIXABAY_TEST_API_KEY):
        assert secret not in repr(decoded)
        assert secret not in repr(decoded.payload)
    assert repr(decoded.payload) == (
        "StockCandidateSecrets(pexels_api_key=<redacted>, "
        "pixabay_api_key=<redacted>)"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"pexels_api_key": PEXELS_TEST_API_KEY},
        {
            "pexels_api_key": PEXELS_TEST_API_KEY,
            "pixabay_api_key": PIXABAY_TEST_API_KEY,
            "api_key": TEST_API_KEY,
        },
        {
            "pexels_api_key": {
                "nested": PEXELS_TEST_API_KEY,
            },
            "pixabay_api_key": PIXABAY_TEST_API_KEY,
        },
    ],
)
def test_stock_candidate_dual_secret_payload_tree_is_exact(payload: object) -> None:
    request = _request_for("stock_candidates")
    value = {
        "version": 2,
        "execution_id": EXECUTION_ID,
        "attempt_id": ATTEMPT_ID,
        "operation": "stock_candidates",
        "payload": payload,
    }

    with pytest.raises(MaterialProtocolError) as captured:
        decode_material_operation_secrets(_json_bytes(value), request=request)

    for secret in (PEXELS_TEST_API_KEY, PIXABAY_TEST_API_KEY):
        assert secret not in _exception_graph_text(captured.value)


def test_stock_candidate_secret_version_must_match_request_version() -> None:
    request = _request_for("stock_candidates")
    secrets = _secrets_for("stock_candidates")
    object.__setattr__(secrets, "version", 1)

    with pytest.raises(MaterialProtocolError, match="version"):
        encode_material_operation_secrets(secrets, request=request)


def test_secret_envelope_requires_exact_ids_operation_and_payload_shape() -> None:
    request = _request_for("pexels")
    secrets = _secrets_for("pexels")
    value = json.loads(encode_material_operation_secrets(secrets, request=request))

    mismatch_cases = []
    for key, replacement in (
        ("execution_id", "123e4567-e89b-12d3-a456-426614174009"),
        ("attempt_id", OTHER_ATTEMPT_ID),
        ("operation", "pixabay"),
    ):
        changed = dict(value)
        changed[key] = replacement
        mismatch_cases.append(changed)

    for changed in mismatch_cases:
        with pytest.raises(MaterialProtocolError):
            decode_material_operation_secrets(_json_bytes(changed), request=request)

    missing_key = dict(value)
    assert isinstance(missing_key["payload"], dict)
    missing_key["payload"] = {}
    with pytest.raises(MaterialProtocolError, match="exact keys"):
        decode_material_operation_secrets(_json_bytes(missing_key), request=request)

    empty_operation_payload = json.loads(
        encode_material_operation_secrets(
            _secrets_for("official_capture"),
            request=_request_for("official_capture"),
        )
    )
    empty_operation_payload["payload"] = {"api_key": TEST_API_KEY}
    with pytest.raises(MaterialProtocolError, match="exact keys"):
        decode_material_operation_secrets(
            _json_bytes(empty_operation_payload),
            request=_request_for("official_capture"),
        )


@pytest.mark.parametrize(
    "api_key",
    ["", "   ", " leading", "trailing ", "x" * (MAX_API_KEY_CHARS + 1)],
)
def test_stock_secret_api_key_is_nonempty_trimmed_and_strictly_bounded(
    api_key: str,
) -> None:
    with pytest.raises(MaterialProtocolError, match="api_key"):
        PexelsSecrets(api_key=api_key)
    with pytest.raises(MaterialProtocolError, match="api_key"):
        PixabaySecrets(api_key=api_key)


def test_secret_failures_never_echo_secret_values() -> None:
    request = _request_for("pexels")
    value = {
        "version": 1,
        "execution_id": EXECUTION_ID,
        "attempt_id": ATTEMPT_ID,
        "operation": "pexels",
        "payload": {"api_key": TEST_API_KEY, "unexpected": TEST_API_KEY},
    }

    with pytest.raises(MaterialProtocolError) as captured:
        decode_material_operation_secrets(_json_bytes(value), request=request)

    assert TEST_API_KEY not in str(captured.value)


def _exception_graph_text(error: BaseException) -> str:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    rendered: list[str] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        rendered.extend(
            [
                str(current),
                repr(current),
                repr(current.args),
                repr(getattr(current, "__dict__", {})),
            ]
        )
        for attribute in ("object", "doc", "credential", "secret", "url"):
            if hasattr(current, attribute):
                rendered.append(repr(getattr(current, attribute)))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return "\n".join(rendered)


def test_secret_payload_and_envelope_repr_are_always_redacted() -> None:
    request = _request_for("pexels")
    secrets = _secrets_for("pexels")

    for value in (secrets.payload, secrets):
        rendered = repr(value)
        assert TEST_API_KEY not in rendered
        assert "<redacted>" in rendered
    assert TEST_API_KEY not in repr(
        decode_material_operation_secrets(
            encode_material_operation_secrets(secrets, request=request),
            request=request,
        )
    )


@pytest.mark.parametrize("failure_kind", ["utf8", "json"])
def test_secret_decode_errors_do_not_retain_raw_input_anywhere(
    failure_kind: str,
) -> None:
    request = _request_for("pexels")
    if failure_kind == "utf8":
        raw = (TEST_API_KEY.encode("utf-8") + b"\xff")
    else:
        raw = (
            '{"version":1,"payload":{"api_key":"'
            + TEST_API_KEY
            + '"'
        ).encode("utf-8")

    with pytest.raises(MaterialProtocolError) as captured:
        decode_material_operation_secrets(raw, request=request)

    assert TEST_API_KEY not in _exception_graph_text(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_secret_encode_error_does_not_retain_encoder_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CredentialEncodingError(UnicodeError):
        def __init__(self, secret: str) -> None:
            super().__init__(f"failed to encode {secret}")
            self.secret = secret

    request = _request_for("pexels")
    secrets = _secrets_for("pexels")

    def fail_dumps(*_args: object, **_kwargs: object) -> str:
        raise CredentialEncodingError(TEST_API_KEY)

    monkeypatch.setattr(protocol.json, "dumps", fail_dumps)

    with pytest.raises(MaterialProtocolError) as captured:
        encode_material_operation_secrets(secrets, request=request)

    assert TEST_API_KEY not in _exception_graph_text(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_provenance_error_does_not_retain_credentialed_url_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = "url-credential-canary"
    credentialed_url = f"https://example.com/video?token={credential}"

    class CredentialProvenanceError(ValueError):
        def __init__(self, url: str) -> None:
            super().__init__(f"invalid provenance for {url}")
            self.url = url

    def fail_provenance(**_kwargs: object) -> None:
        raise CredentialProvenanceError(credentialed_url)

    monkeypatch.setattr(
        protocol.material_provenance,
        "validate_execution_provenance",
        fail_provenance,
    )

    with pytest.raises(MaterialProtocolError) as captured:
        StagedArtifact(
            relative_path="payload/video.mp4",
            media_kind="video",
            source_type="Pexels",
            source_url=credentialed_url,
            evidence_kind="atmosphere",
            is_locally_generated=False,
            sha256="a" * 64,
            size_bytes=1,
        )

    assert credential not in _exception_graph_text(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_secret_envelope_rejects_duplicate_bom_trailing_and_size_overflow() -> None:
    request = _request_for("pexels")
    duplicate = (
        '{"version":1,'
        f'"execution_id":"{EXECUTION_ID}","attempt_id":"{ATTEMPT_ID}",'
        '"operation":"pexels","payload":{"api_key":"one","api_key":"two"}}'
    ).encode()

    for raw in (duplicate, b"\xef\xbb\xbf{}", b"{} "):
        with pytest.raises(MaterialProtocolError):
            decode_material_operation_secrets(raw, request=request)
    with pytest.raises(MaterialProtocolError, match="16 KiB"):
        decode_material_operation_secrets(
            b"x" * (MAX_SECRET_BYTES + 1),
            request=request,
        )


@pytest.mark.parametrize("location", ["value", "key"])
def test_secret_rejects_escaped_unpaired_surrogate_keys_and_values(
    location: str,
) -> None:
    request = _request_for("pexels")
    value = json.loads(
        encode_material_operation_secrets(_secrets_for("pexels"), request=request)
    )
    if location == "value":
        assert isinstance(value["payload"], dict)
        value["payload"]["api_key"] = "SURROGATE"
        raw = _json_bytes(value).replace(b'"SURROGATE"', b'"\\ud800"')
    else:
        raw = _json_bytes(value)[:-1] + b',"\\udfff":"value"}'

    with pytest.raises(MaterialProtocolError, match="surrogate"):
        decode_material_operation_secrets(raw, request=request)


def test_valid_emoji_round_trips_stably_across_all_documents() -> None:
    request = MaterialOperationRequest(
        protocol_version=1,
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        operation="pexels",
        payload=StockSearchPayload(query="科技 🚀", per_page=10),
    )
    secret = MaterialOperationSecrets(
        version=1,
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        operation="pexels",
        payload=PexelsSecrets(api_key="fake-key-🔒"),
    )
    result = MaterialWorkerResult(
        protocol_version=1,
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        operation="pexels",
        status="succeeded",
        selected_artifact="payload/镜头-🎬.mp4",
        artifacts=(
            StagedArtifact(
                relative_path="payload/镜头-🎬.mp4",
                media_kind="video",
                source_type="素材 🚀",
                source_url="https://example.com/video/1",
                evidence_kind="atmosphere",
                is_locally_generated=False,
                sha256="a" * 64,
                size_bytes=1,
            ),
        ),
        reason_code="",
        reason="完成 ✅",
    )

    request_bytes = encode_material_operation_request(request)
    secret_bytes = encode_material_operation_secrets(secret, request=request)
    result_bytes = encode_material_worker_result(result)

    assert encode_material_operation_request(
        decode_material_operation_request(request_bytes)
    ) == request_bytes
    assert encode_material_operation_secrets(
        decode_material_operation_secrets(secret_bytes, request=request),
        request=request,
    ) == secret_bytes
    assert encode_material_worker_result(
        decode_material_worker_result(
            result_bytes,
            expected_execution_id=EXECUTION_ID,
            expected_attempt_id=ATTEMPT_ID,
            expected_operation="pexels",
        )
    ) == result_bytes


def test_protocol_objects_are_frozen_and_outcome_requires_consistent_result() -> None:
    request = _request_for("official_capture")
    with pytest.raises(FrozenInstanceError):
        request.operation = "coverr"  # type: ignore[misc]

    termination = TerminationEvidence(
        platform="windows_job",
        root_pid=1234,
        exit_code=0,
        termination_method="natural",
        active_processes=0,
        observed_at="2026-07-12T00:00:00+00:00",
    )
    outcome = MaterialOperationOutcome(
        status="succeeded",
        result=_succeeded_result(),
        termination=termination,
    )

    assert outcome.termination.active_processes == 0
    with pytest.raises(MaterialProtocolError, match="consistent"):
        MaterialOperationOutcome(
            status="failed",
            result=_succeeded_result(),
            termination=termination,
        )
    with pytest.raises(MaterialProtocolError, match="root_pid"):
        TerminationEvidence(
            platform="windows_job",
            root_pid=True,  # type: ignore[arg-type]
            exit_code=None,
            termination_method="never_resumed",
            active_processes=0,
            observed_at="2026-07-12T00:00:00+00:00",
        )


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_completed_outcomes_require_natural_termination(status: str) -> None:
    result = _succeeded_result()
    if status == "failed":
        result = MaterialWorkerResult(
            protocol_version=1,
            execution_id=EXECUTION_ID,
            attempt_id=ATTEMPT_ID,
            operation="pexels",
            status="failed",
            selected_artifact="",
            artifacts=(),
            reason_code="source_unavailable",
            reason="failed",
        )
    for method in ("job_terminate", "never_resumed"):
        termination = TerminationEvidence(
            platform="windows_job",
            root_pid=1234,
            exit_code=1,
            termination_method=method,
            active_processes=0,
            observed_at="2026-07-12T00:00:00+00:00",
        )
        with pytest.raises(MaterialProtocolError, match="natural"):
            MaterialOperationOutcome(
                status=status,  # type: ignore[arg-type]
                result=result,
                termination=termination,
            )


@pytest.mark.parametrize("method", ["job_terminate", "never_resumed"])
def test_terminated_outcome_requires_no_result_and_non_natural_method(
    method: str,
) -> None:
    termination = TerminationEvidence(
        platform="windows_job",
        root_pid=1234,
        exit_code=1,
        termination_method=method,  # type: ignore[arg-type]
        active_processes=0,
        observed_at="2026-07-12T00:00:00+00:00",
    )
    outcome = MaterialOperationOutcome(
        status="terminated",
        result=None,
        termination=termination,
    )

    assert outcome.status == "terminated"


def test_terminated_outcome_rejects_natural_termination() -> None:
    termination = TerminationEvidence(
        platform="windows_job",
        root_pid=1234,
        exit_code=0,
        termination_method="natural",
        active_processes=0,
        observed_at="2026-07-12T00:00:00+00:00",
    )

    with pytest.raises(MaterialProtocolError, match="job_terminate or never_resumed"):
        MaterialOperationOutcome(
            status="terminated",
            result=None,
            termination=termination,
        )


def test_blocked_exception_is_typed_and_contains_only_control_metadata() -> None:
    blocked = MaterialOperationBlocked(
        execution_id=EXECUTION_ID,
        attempt_id=ATTEMPT_ID,
        reason_code="job_not_empty",
        job_instance_id="123e4567-e89b-12d3-a456-426614174003",
        quarantine_dir=Path("D:/CodexData/jobs/quarantine"),
    )

    assert blocked.execution_id == EXECUTION_ID
    assert blocked.attempt_id == ATTEMPT_ID
    assert blocked.reason_code == "job_not_empty"
    assert blocked.job_instance_id.endswith("003")
    assert blocked.quarantine_dir == Path("D:/CodexData/jobs/quarantine")
    assert TEST_API_KEY not in str(blocked)
