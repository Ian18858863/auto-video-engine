from __future__ import annotations

import ast
import ctypes
import gc
import hashlib
import inspect
import json
import math
import os
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Iterator, cast

import pytest

from app.core import windows_durable_file as durable
from app.core.windows_durable_file import (
    DurableTempOwner,
    DurableWriteError,
    DurableWriteReceipt,
    write_durable_bytes,
    write_durable_json,
)
from app.core.windows_storage_policy import (
    ApprovedPhysicalDirectory,
    approve_physical_d_write_root,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP_ROOT = REPO_ROOT / ".test-tmp" / "task7-durable-file"


pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="the durable file primitive is Windows-only"
)


@pytest.fixture
def approved_parent() -> Iterator[ApprovedPhysicalDirectory]:
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    case = TEST_TMP_ROOT / f"case {os.getpid()} {os.urandom(12).hex()} 中文"
    case.mkdir()
    parent = approve_physical_d_write_root(case)
    try:
        yield parent
    finally:
        parent.close()
        if case.exists():
            children = list(case.iterdir())
            assert all(child.is_file() or child.is_symlink() for child in children)
            for child in children:
                child.unlink()
            case.rmdir()


def _visible_files(parent: ApprovedPhysicalDirectory) -> list[Path]:
    return sorted(parent.path.iterdir(), key=lambda path: path.name)


def _identity(path: Path) -> tuple[int, int]:
    import win32con
    import win32file

    handle = win32file.CreateFile(
        str(path),
        win32con.GENERIC_READ,
        win32file.FILE_SHARE_READ,
        None,
        win32con.OPEN_EXISTING,
        win32file.FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    try:
        info = win32file.GetFileInformationByHandle(handle)
        return int(info[4]), (int(info[8]) << 32) | int(info[9])
    finally:
        handle.Close()


def _track_successful_raw_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> list[int]:
    real_close = durable.win32api.CloseHandle
    close_calls: list[int] = []

    def track_close(raw_handle: int) -> None:
        close_calls.append(int(raw_handle))
        assert real_close(raw_handle) is None
        return None

    monkeypatch.setattr(durable.win32api, "CloseHandle", track_close)
    return close_calls


def test_writes_exact_bytes_to_unicode_space_path_and_returns_frozen_receipt(
    approved_parent: ApprovedPhysicalDirectory,
) -> None:
    payload = b"\x00durable\xff" + "乔布斯级".encode()

    receipt = write_durable_bytes(
        parent=approved_parent,
        filename="结果 文件.json",
        payload=payload,
        replace=False,
        max_bytes=len(payload),
    )

    assert isinstance(receipt, DurableWriteReceipt)
    assert receipt.path == Path(approved_parent.stable_volume_path) / "结果 文件.json"
    assert receipt.sha256 == hashlib.sha256(payload).hexdigest()
    assert receipt.byte_count == len(payload)
    assert receipt.path.read_bytes() == payload
    assert _visible_files(approved_parent) == [approved_parent.path / "结果 文件.json"]
    with pytest.raises(FrozenInstanceError):
        receipt.byte_count = 0  # type: ignore[misc]


def test_success_treats_none_from_flush_and_raw_close_as_success(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_flush = durable.win32file.FlushFileBuffers
    original_close = durable.win32api.CloseHandle
    calls: list[str] = []

    def flush(handle: object) -> None:
        calls.append("flush")
        assert original_flush(handle) is None
        return None

    def close(raw_handle: int) -> None:
        calls.append("close")
        assert original_close(raw_handle) is None
        return None

    monkeypatch.setattr(durable.win32file, "FlushFileBuffers", flush)
    monkeypatch.setattr(durable.win32api, "CloseHandle", close)

    receipt = write_durable_bytes(
        parent=approved_parent,
        filename="none-success.bin",
        payload=b"none-is-success",
        replace=False,
        max_bytes=64,
    )

    assert calls == ["flush", "flush", "close"]
    assert receipt.path.read_bytes() == b"none-is-success"


def test_fixed_order_is_write_two_flushes_rewind_then_raw_close(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, int]] = []
    real_write = durable.win32file.WriteFile
    real_flush = durable.win32file.FlushFileBuffers
    real_rewind = durable.win32file.SetFilePointer
    real_close = durable.win32api.CloseHandle

    def tracking_write(handle: object, *args: object, **kwargs: object):
        events.append(("write", int(handle)))
        return real_write(handle, *args, **kwargs)

    def tracking_flush(handle: object) -> None:
        events.append(("flush", int(handle)))
        return real_flush(handle)

    def tracking_rewind(handle: object, *args: object, **kwargs: object):
        events.append(("rewind", int(handle)))
        return real_rewind(handle, *args, **kwargs)

    def tracking_close(raw_handle: int) -> None:
        events.append(("close", int(raw_handle)))
        return real_close(raw_handle)

    monkeypatch.setattr(durable.win32file, "WriteFile", tracking_write)
    monkeypatch.setattr(durable.win32file, "FlushFileBuffers", tracking_flush)
    monkeypatch.setattr(durable.win32file, "SetFilePointer", tracking_rewind)
    monkeypatch.setattr(durable.win32api, "CloseHandle", tracking_close)

    write_durable_bytes(
        parent=approved_parent,
        filename="ordered.bin",
        payload=b"ordered",
        replace=False,
        max_bytes=64,
    )

    assert [name for name, _raw in events] == [
        "write",
        "flush",
        "flush",
        "rewind",
        "close",
    ]
    assert len({raw for _name, raw in events}) == 1


def test_close_result_unknown_is_typed_and_quarantined_without_receipt(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_close = durable.win32api.CloseHandle
    close_calls: list[int] = []

    def close_then_registration_fails(raw_handle: int) -> None:
        close_calls.append(int(raw_handle))
        real_close(raw_handle)
        raise OSError("simulated CloseHandle result-registration failure")

    monkeypatch.setattr(
        durable.win32api, "CloseHandle", close_then_registration_fails
    )

    with pytest.raises(DurableWriteError) as exc_info:
        write_durable_bytes(
            parent=approved_parent,
            filename="close-failed.bin",
            payload=b"close-failed",
            replace=False,
            max_bytes=64,
        )

    error = exc_info.value
    assert error.stage == durable.DurableWriteStage.CLOSE
    assert isinstance(error.evidence_owner, durable.CloseUnknownToken)
    assert error.handle_state == durable.DurableHandleState.CLOSE_UNKNOWN
    assert close_calls and len(close_calls) == 1
    assert error.target is not None
    assert error.target.read_bytes() == b"close-failed"


def test_parent_is_reverified_at_each_durable_boundary(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_verify = ApprovedPhysicalDirectory.verify
    real_flush = durable.win32file.FlushFileBuffers

    def verify(parent: ApprovedPhysicalDirectory) -> None:
        if parent is approved_parent:
            events.append("verify")
        real_verify(parent)

    def flush(handle: int) -> None:
        events.append("flush")
        return real_flush(handle)

    monkeypatch.setattr(ApprovedPhysicalDirectory, "verify", verify)
    monkeypatch.setattr(durable.win32file, "FlushFileBuffers", flush)

    write_durable_bytes(
        parent=approved_parent,
        filename="verified.bin",
        payload=b"verified",
        replace=False,
        max_bytes=64,
    )

    assert events == ["verify", "verify", "flush", "verify", "verify", "flush"]


def test_both_flushes_receive_same_live_pyhandle_then_raw_identity_is_closed(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import win32api

    live_handles: list[int] = []
    real_flush = durable.win32file.FlushFileBuffers

    def inspect_live_handle(handle: object) -> None:
        win32api.GetHandleInformation(handle)
        live_handles.append(int(handle))
        return real_flush(handle)

    monkeypatch.setattr(durable.win32file, "FlushFileBuffers", inspect_live_handle)

    write_durable_bytes(
        parent=approved_parent,
        filename="borrowed.bin",
        payload=b"borrowed",
        replace=False,
        max_bytes=64,
    )

    assert len(live_handles) == 2
    assert live_handles[0] == live_handles[1]
    with pytest.raises(durable.pywintypes.error) as closed_after_raw_close:
        win32api.GetHandleInformation(live_handles[0])
    assert closed_after_raw_close.value.winerror == 6


def test_json_is_one_deterministic_strict_utf8_encoding_without_newline(
    approved_parent: ApprovedPhysicalDirectory,
) -> None:
    payload = {"z": 3, "nested": {"z": 2, "a": 1}, "a": "中文"}
    expected = b'{"a":"\xe4\xb8\xad\xe6\x96\x87","nested":{"a":1,"z":2},"z":3}'

    receipt = write_durable_json(
        parent=approved_parent,
        filename="stable.json",
        payload=payload,
        replace=False,
        max_bytes=256,
    )

    assert receipt.path.read_bytes() == expected
    assert receipt.byte_count == len(expected)
    assert receipt.sha256 == hashlib.sha256(expected).hexdigest()
    assert not expected.endswith(b"\n")


def test_json_serializes_once_then_delegates_exact_bytes_to_bytes_primitive(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    dump_calls: list[dict[str, object]] = []
    byte_calls: list[dict[str, object]] = []
    real_dumps = json.dumps
    expected_receipt = DurableWriteReceipt(
        path=approved_parent.path / "delegated.json",
        sha256="0" * 64,
        byte_count=2,
    )

    def dumps(value: object, **kwargs: object) -> str:
        dump_calls.append(dict(kwargs))
        return real_dumps(value, **kwargs)

    def bytes_writer(**kwargs: object) -> DurableWriteReceipt:
        byte_calls.append(dict(kwargs))
        return expected_receipt

    monkeypatch.setattr(durable.json, "dumps", dumps)
    monkeypatch.setattr(durable, "write_durable_bytes", bytes_writer)

    receipt = write_durable_json(
        parent=approved_parent,
        filename="delegated.json",
        payload={"b": 2, "a": 1},
        replace=False,
        max_bytes=32,
    )

    assert receipt is expected_receipt
    assert dump_calls == [
        {
            "allow_nan": False,
            "ensure_ascii": False,
            "separators": (",", ":"),
            "sort_keys": True,
        }
    ]
    assert byte_calls == [
        {
            "parent": approved_parent,
            "filename": "delegated.json",
            "payload": b'{"a":1,"b":2}',
            "replace": False,
            "max_bytes": 32,
        }
    ]


@pytest.mark.parametrize("bad_number", [math.nan, math.inf, -math.inf])
def test_json_rejects_non_finite_numbers_before_creating_temp(
    approved_parent: ApprovedPhysicalDirectory, bad_number: float
) -> None:
    with pytest.raises(ValueError):
        write_durable_json(
            parent=approved_parent,
            filename="non-finite.json",
            payload={"value": bad_number},
            replace=False,
            max_bytes=128,
        )

    assert _visible_files(approved_parent) == []


@pytest.mark.parametrize(
    ("invalid_case", "expected_error"),
    [
        pytest.param("mapping_cycle", ValueError, id="mapping-cycle"),
        pytest.param("nested_sequence_cycle", ValueError, id="nested-sequence-cycle"),
        pytest.param("value_isolated_surrogate", ValueError, id="value-surrogate"),
        pytest.param("key_isolated_surrogate", ValueError, id="key-surrogate"),
        pytest.param("nested_non_string_key", TypeError, id="nested-non-string-key"),
        pytest.param("encoded_oversize", ValueError, id="encoded-oversize"),
    ],
)
def test_json_rejection_is_secret_safe_and_precedes_parent_or_file_io(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    invalid_case: str,
    expected_error: type[Exception],
) -> None:
    secret_marker = f"E2_SECRET_{invalid_case}_A91F73D4"
    payload: dict[object, object] = {"secret": secret_marker}
    max_bytes = 4096

    if invalid_case == "mapping_cycle":
        payload["cycle"] = payload
    elif invalid_case == "nested_sequence_cycle":
        cycle: list[object] = []
        cycle.append(cycle)
        payload["nested"] = ["level", cycle]
    elif invalid_case == "value_isolated_surrogate":
        payload["nested"] = {"value": "\ud800"}
    elif invalid_case == "key_isolated_surrogate":
        payload["\udfff"] = "invalid-key"
    elif invalid_case == "nested_non_string_key":
        payload["nested"] = {17: "invalid-key-type"}
    else:
        assert invalid_case == "encoded_oversize"
        payload["value"] = secret_marker * 4
        max_bytes = 1

    forbidden_calls = {
        "verify": 0,
        "GetVolumeInformation": 0,
        "CreateFile": 0,
        "WriteFile": 0,
    }
    real_verify = ApprovedPhysicalDirectory.verify

    def forbidden_verify(self: ApprovedPhysicalDirectory) -> None:
        if self is approved_parent:
            forbidden_calls["verify"] += 1
            raise AssertionError("parent verification must follow JSON acceptance")
        real_verify(self)

    def forbidden_volume(*_args: object, **_kwargs: object):
        forbidden_calls["GetVolumeInformation"] += 1
        raise AssertionError("volume I/O must follow JSON acceptance")

    def forbidden_create(*_args: object, **_kwargs: object):
        forbidden_calls["CreateFile"] += 1
        raise AssertionError("file creation must follow JSON acceptance")

    def forbidden_write(*_args: object, **_kwargs: object):
        forbidden_calls["WriteFile"] += 1
        raise AssertionError("file writes must follow JSON acceptance")

    monkeypatch.setattr(ApprovedPhysicalDirectory, "verify", forbidden_verify)
    monkeypatch.setattr(durable.win32api, "GetVolumeInformation", forbidden_volume)
    monkeypatch.setattr(durable.win32file, "CreateFile", forbidden_create)
    monkeypatch.setattr(durable.win32file, "WriteFile", forbidden_write)

    with pytest.raises((TypeError, ValueError)) as exc_info:
        write_durable_json(
            parent=approved_parent,
            filename=f"rejected-{invalid_case}.json",
            payload=cast(dict[str, object], payload),
            replace=False,
            max_bytes=max_bytes,
        )

    error = exc_info.value
    assert type(error) is expected_error
    assert secret_marker not in str(error)
    assert secret_marker not in repr(error)
    assert forbidden_calls == {
        "verify": 0,
        "GetVolumeInformation": 0,
        "CreateFile": 0,
        "WriteFile": 0,
    }
    assert _visible_files(approved_parent) == []


@pytest.mark.parametrize(
    "filename",
    [
        "",
        ".",
        "..",
        "../escape",
        r"nested\escape",
        r"C:\escape.txt",
        "ads:stream",
        "trailing.",
        "trailing ",
        "bad\x00name",
        "bad\x01name",
        "CON",
        "con.txt",
        "NUL.log",
        "COM1",
        "LPT9.txt",
        "COM\u00b9.log",
    ],
)
def test_rejects_non_single_or_ambiguous_filename_before_creating_temp(
    approved_parent: ApprovedPhysicalDirectory, filename: str
) -> None:
    with pytest.raises((TypeError, ValueError, DurableWriteError)):
        write_durable_bytes(
            parent=approved_parent,
            filename=filename,
            payload=b"never-write",
            replace=False,
            max_bytes=64,
        )

    assert _visible_files(approved_parent) == []


@pytest.mark.parametrize(
    ("payload", "replace", "max_bytes"),
    [
        (bytearray(b"bytes-only"), False, 64),
        (b"ok", 1, 64),
        (b"ok", False, True),
        (b"ok", False, 0),
        (b"too-large", False, 3),
    ],
)
def test_rejects_invalid_types_and_limits_before_creating_temp(
    approved_parent: ApprovedPhysicalDirectory,
    payload: object,
    replace: object,
    max_bytes: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        write_durable_bytes(
            parent=approved_parent,
            filename="invalid.bin",
            payload=cast(bytes, payload),
            replace=cast(bool, replace),
            max_bytes=cast(int, max_bytes),
        )

    assert _visible_files(approved_parent) == []


def test_requires_a_live_approved_physical_directory(
    approved_parent: ApprovedPhysicalDirectory,
) -> None:
    approved_parent.close()

    with pytest.raises(DurableWriteError) as exc_info:
        write_durable_bytes(
            parent=approved_parent,
            filename="closed.bin",
            payload=b"closed",
            replace=False,
            max_bytes=64,
        )

    assert exc_info.value.stage == "validate_parent"
    assert exc_info.value.temp_owner is None
    assert exc_info.value.target is None
    with pytest.raises(TypeError):
        write_durable_bytes(
            parent=cast(ApprovedPhysicalDirectory, object()),
            filename="forged.bin",
            payload=b"forged",
            replace=False,
            max_bytes=64,
        )


@pytest.mark.parametrize(
    ("flush_ordinal", "expected_stage", "expected_path_state"),
    [
        (
            1,
            durable.DurableWriteStage.FLUSH_PRE_RENAME,
            durable.DurablePathState.TEMP_CONFIRMED,
        ),
        (
            2,
            durable.DurableWriteStage.FLUSH_POST_RENAME,
            durable.DurablePathState.TARGET_CONFIRMED,
        ),
    ],
    ids=["pre-rename", "post-rename"],
)
def test_flush_failure_is_typed_and_preserves_actual_location(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    flush_ordinal: int,
    expected_stage: object,
    expected_path_state: object,
) -> None:
    payload = b"flush-failure-evidence"
    target_name = f"flush-{flush_ordinal}.bin"
    target = Path(approved_parent.stable_volume_path) / target_name
    real_flush = durable.win32file.FlushFileBuffers
    flush_calls = 0
    close_calls = _track_successful_raw_closes(monkeypatch)

    def fail_selected_flush(handle: object) -> None:
        nonlocal flush_calls
        flush_calls += 1
        if flush_calls == flush_ordinal:
            raise OSError(f"simulated FlushFileBuffers failure #{flush_ordinal}")
        assert real_flush(handle) is None
        return None

    monkeypatch.setattr(durable.win32file, "FlushFileBuffers", fail_selected_flush)

    with pytest.raises(DurableWriteError) as exc_info:
        write_durable_bytes(
            parent=approved_parent,
            filename=target_name,
            payload=payload,
            replace=False,
            max_bytes=64,
        )

    error = exc_info.value
    assert error.stage is expected_stage
    assert error.target == target
    assert error.path_state is expected_path_state
    assert error.handle_state is durable.DurableHandleState.CLOSED
    assert len(close_calls) == 1
    assert close_calls[0] != 0
    assert flush_calls == flush_ordinal

    if expected_path_state is durable.DurablePathState.TEMP_CONFIRMED:
        assert isinstance(error.temp_owner, DurableTempOwner)
        assert error.temp_owner.path.read_bytes() == payload
        assert error.temp_owner.sha256 == hashlib.sha256(payload).hexdigest()
        assert error.temp_owner.byte_count == len(payload)
        assert not target.exists()
        assert _visible_files(approved_parent) == [
            approved_parent.path / error.temp_owner.path.name
        ]
    else:
        assert error.temp_owner is None
        assert target.read_bytes() == payload
        assert _visible_files(approved_parent) == [approved_parent.path / target_name]


def test_operation_failure_final_createfile_noncollision_has_no_owner_evidence(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_calls = 0
    close_calls: list[int] = []

    def fail_create(*_args: object, **_kwargs: object):
        nonlocal create_calls
        create_calls += 1
        raise durable.pywintypes.error(5, "CreateFileW", "simulated access denied")

    def forbidden_close(raw_handle: int) -> None:
        close_calls.append(int(raw_handle))
        raise AssertionError("an unreturned CreateFile handle must not be closed")

    monkeypatch.setattr(durable.win32file, "CreateFile", fail_create)
    monkeypatch.setattr(durable.win32api, "CloseHandle", forbidden_close)

    with pytest.raises(DurableWriteError) as exc_info:
        write_durable_bytes(
            parent=approved_parent,
            filename="create-failed.bin",
            payload=b"never-created",
            replace=False,
            max_bytes=64,
        )

    error = exc_info.value
    assert create_calls == 1
    assert close_calls == []
    assert error.stage is durable.DurableWriteStage.CREATE_TEMP
    assert error.path_state is durable.DurablePathState.CREATE_UNKNOWN
    assert error.handle_state is durable.DurableHandleState.CREATE_IN_FLIGHT
    assert error.evidence_owner is None
    assert error.temp_owner is None
    assert error.target == (
        Path(approved_parent.stable_volume_path) / "create-failed.bin"
    )
    assert not error.target.exists()
    assert _visible_files(approved_parent) == []


def test_operation_failure_real_write_prefix_then_partial_count_retains_prefix(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"real-prefix-then-short-count"
    prefix = payload[:7]
    real_write = durable.win32file.WriteFile
    close_calls = _track_successful_raw_closes(monkeypatch)

    def write_only_prefix(handle: object, _payload: bytes):
        status, written = real_write(handle, prefix)
        assert status in (0, None)
        assert int(written) == len(prefix)
        return status, written

    monkeypatch.setattr(durable.win32file, "WriteFile", write_only_prefix)

    with pytest.raises(DurableWriteError) as exc_info:
        write_durable_bytes(
            parent=approved_parent,
            filename="partial-write.bin",
            payload=payload,
            replace=False,
            max_bytes=64,
        )

    error = exc_info.value
    assert error.stage is durable.DurableWriteStage.WRITE_TEMP
    assert error.path_state is durable.DurablePathState.TEMP_CONFIRMED
    assert error.handle_state is durable.DurableHandleState.CLOSED
    assert len(close_calls) == 1
    assert close_calls[0] != 0
    assert error.target is not None
    assert not error.target.exists()
    assert isinstance(error.temp_owner, DurableTempOwner)
    assert error.temp_owner.path.read_bytes() == prefix
    assert _visible_files(approved_parent) == [
        approved_parent.path / error.temp_owner.path.name
    ]


@pytest.mark.parametrize(
    (
        "verify_ordinal",
        "expected_stage",
        "expected_path_state",
        "expected_handle_state",
    ),
    [
        (1, durable.DurableWriteStage.VALIDATE_PARENT, None, None),
        (
            2,
            durable.DurableWriteStage.VERIFY_PARENT_BEFORE_FLUSH,
            durable.DurablePathState.TEMP_CONFIRMED,
            durable.DurableHandleState.CLOSED,
        ),
        (
            3,
            durable.DurableWriteStage.VERIFY_PARENT_BEFORE_RENAME,
            durable.DurablePathState.TEMP_CONFIRMED,
            durable.DurableHandleState.CLOSED,
        ),
        (
            4,
            durable.DurableWriteStage.VERIFY_PARENT_AFTER_RENAME,
            durable.DurablePathState.TARGET_CONFIRMED,
            durable.DurableHandleState.CLOSED,
        ),
    ],
    ids=["initial", "before-flush", "before-rename", "after-rename"],
)
def test_operation_failure_each_parent_verify_reports_exact_stage_and_location(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    verify_ordinal: int,
    expected_stage: object,
    expected_path_state: object | None,
    expected_handle_state: object | None,
) -> None:
    payload = b"verify-stage-matrix"
    target_name = f"verify-{verify_ordinal}.bin"
    target = Path(approved_parent.stable_volume_path) / target_name
    real_verify = ApprovedPhysicalDirectory.verify
    verify_calls = 0
    injected_failure = OSError(f"simulated parent verify failure #{verify_ordinal}")
    close_calls = _track_successful_raw_closes(monkeypatch)

    def fail_selected_verify(self: ApprovedPhysicalDirectory) -> None:
        nonlocal verify_calls
        if self is approved_parent:
            verify_calls += 1
            if verify_calls == verify_ordinal:
                raise injected_failure
        real_verify(self)

    monkeypatch.setattr(ApprovedPhysicalDirectory, "verify", fail_selected_verify)

    with pytest.raises(DurableWriteError) as exc_info:
        write_durable_bytes(
            parent=approved_parent,
            filename=target_name,
            payload=payload,
            replace=False,
            max_bytes=64,
        )

    error = exc_info.value
    assert verify_calls == verify_ordinal
    assert error.stage is expected_stage
    assert error.cause is injected_failure
    assert error.path_state is expected_path_state
    assert error.handle_state is expected_handle_state

    if verify_ordinal == 1:
        assert error.target is None
        assert error.evidence_owner is None
        assert error.temp_owner is None
        assert close_calls == []
        assert _visible_files(approved_parent) == []
    elif expected_path_state is durable.DurablePathState.TEMP_CONFIRMED:
        assert error.target == target
        assert len(close_calls) == 1
        assert close_calls[0] != 0
        assert isinstance(error.temp_owner, DurableTempOwner)
        assert error.temp_owner.path.read_bytes() == payload
        assert not target.exists()
        assert _visible_files(approved_parent) == [
            approved_parent.path / error.temp_owner.path.name
        ]
    else:
        assert error.target == target
        assert len(close_calls) == 1
        assert close_calls[0] != 0
        assert error.temp_owner is None
        assert target.read_bytes() == payload
        assert _visible_files(approved_parent) == [approved_parent.path / target_name]


@pytest.mark.parametrize("rewind_failure", ["nonzero", "raises"])
def test_operation_failure_rewind_reports_target_and_closes_once(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    rewind_failure: str,
) -> None:
    payload = b"rewind-failure-matrix"
    target_name = f"rewind-{rewind_failure}.bin"
    target = Path(approved_parent.stable_volume_path) / target_name
    real_rewind = durable.win32file.SetFilePointer
    rewind_calls = 0
    close_calls = _track_successful_raw_closes(monkeypatch)

    def fail_rewind(handle: object, *args: object, **kwargs: object):
        nonlocal rewind_calls
        rewind_calls += 1
        if rewind_failure == "nonzero":
            assert real_rewind(handle, *args, **kwargs) == 0
            return 1
        raise OSError("simulated SetFilePointer failure")

    monkeypatch.setattr(durable.win32file, "SetFilePointer", fail_rewind)

    with pytest.raises(DurableWriteError) as exc_info:
        write_durable_bytes(
            parent=approved_parent,
            filename=target_name,
            payload=payload,
            replace=False,
            max_bytes=64,
        )

    error = exc_info.value
    assert rewind_calls == 1
    assert error.stage is durable.DurableWriteStage.REWIND
    assert error.path_state is durable.DurablePathState.TARGET_CONFIRMED
    assert error.handle_state is durable.DurableHandleState.CLOSED
    assert len(close_calls) == 1
    assert close_calls[0] != 0
    assert error.temp_owner is None
    assert error.target == target
    assert target.read_bytes() == payload
    assert _visible_files(approved_parent) == [approved_parent.path / target_name]


@pytest.mark.parametrize(
    "post_verify_fault",
    ["wrong_path", "wrong_link_count", "wrong_size", "same_length_wrong_hash"],
)
def test_operation_failure_post_verify_rejects_each_target_mismatch(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    post_verify_fault: str,
) -> None:
    payload = b"post-verify-mismatch"
    target_name = f"post-{post_verify_fault}.bin"
    target = Path(approved_parent.stable_volume_path) / target_name
    real_create = durable.win32file.CreateFile
    real_snapshot = durable._snapshot_handle
    real_final_path = durable.win32file.GetFinalPathNameByHandle
    real_read = durable.win32file.ReadFile
    source_raw: int | None = None
    snapshot_calls = 0
    corrupt_read_calls = 0
    close_calls = _track_successful_raw_closes(monkeypatch)

    def track_create(*args: object, **kwargs: object):
        nonlocal source_raw
        handle = real_create(*args, **kwargs)
        source_raw = int(handle)
        return handle

    def fault_post_snapshot(handle: object):
        nonlocal snapshot_calls
        snapshot_calls += 1
        snapshot = real_snapshot(handle)
        if snapshot_calls != 2:
            return snapshot
        if post_verify_fault == "wrong_link_count":
            return durable._HandleSnapshot(
                identity=snapshot.identity,
                attributes=snapshot.attributes,
                link_count=snapshot.link_count + 1,
                byte_count=snapshot.byte_count,
            )
        if post_verify_fault == "wrong_size":
            return durable._HandleSnapshot(
                identity=snapshot.identity,
                attributes=snapshot.attributes,
                link_count=snapshot.link_count,
                byte_count=snapshot.byte_count + 1,
            )
        return snapshot

    def fault_final_path(handle: object, *args: object, **kwargs: object):
        if post_verify_fault == "wrong_path" and int(handle) == source_raw:
            return str(Path(approved_parent.stable_volume_path) / "different.bin")
        return real_final_path(handle, *args, **kwargs)

    def fault_read(handle: object, size: int, *args: object):
        nonlocal corrupt_read_calls
        if post_verify_fault == "same_length_wrong_hash" and int(handle) == source_raw:
            corrupt_read_calls += 1
            if corrupt_read_calls == 1:
                corrupt = bytes([payload[0] ^ 1]) + payload[1:]
                assert len(corrupt) == len(payload) <= size
                return 0, corrupt
            return 0, b""
        return real_read(handle, size, *args)

    monkeypatch.setattr(durable.win32file, "CreateFile", track_create)
    monkeypatch.setattr(durable, "_snapshot_handle", fault_post_snapshot)
    monkeypatch.setattr(durable.win32file, "GetFinalPathNameByHandle", fault_final_path)
    monkeypatch.setattr(durable.win32file, "ReadFile", fault_read)

    with pytest.raises(DurableWriteError) as exc_info:
        write_durable_bytes(
            parent=approved_parent,
            filename=target_name,
            payload=payload,
            replace=False,
            max_bytes=64,
        )

    error = exc_info.value
    assert source_raw is not None and source_raw != 0
    assert snapshot_calls == 2
    assert error.stage is durable.DurableWriteStage.VERIFY_TARGET
    assert error.path_state is durable.DurablePathState.TARGET_CONFIRMED
    assert error.handle_state is durable.DurableHandleState.CLOSED
    assert len(close_calls) == 1
    assert close_calls == [source_raw]
    assert error.temp_owner is None
    assert error.target == target
    assert target.read_bytes() == payload
    assert _visible_files(approved_parent) == [approved_parent.path / target_name]
    if post_verify_fault == "same_length_wrong_hash":
        assert corrupt_read_calls == 2


def test_no_clobber_preserves_existing_target_identity_hash_and_failed_temp(
    approved_parent: ApprovedPhysicalDirectory,
) -> None:
    original = b"immutable-original"
    replacement = b"must-not-clobber"
    first = write_durable_bytes(
        parent=approved_parent,
        filename="one-winner.bin",
        payload=original,
        replace=False,
        max_bytes=64,
    )
    before_identity = _identity(first.path)
    before_hash = hashlib.sha256(first.path.read_bytes()).hexdigest()

    with pytest.raises(DurableWriteError) as exc_info:
        write_durable_bytes(
            parent=approved_parent,
            filename="one-winner.bin",
            payload=replacement,
            replace=False,
            max_bytes=64,
        )

    error = exc_info.value
    assert error.stage == durable.DurableWriteStage.NATIVE_RENAME
    assert error.temp_owner is not None
    assert error.temp_owner.path.read_bytes() == replacement
    assert isinstance(error.cause, durable.NativeRenameStatusError)
    assert error.cause.u32_status == 0xC0000035
    assert error.cause.mapped_winerror == 183
    assert _identity(first.path) == before_identity
    assert hashlib.sha256(first.path.read_bytes()).hexdigest() == before_hash
    assert first.path.read_bytes() == original
    assert len(_visible_files(approved_parent)) == 2


def test_replace_sets_only_native_replace_if_exists_on_class_10_buffer(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_durable_bytes(
        parent=approved_parent,
        filename="replace.bin",
        payload=b"old",
        replace=False,
        max_bytes=64,
    )
    real_native = durable._NtSetInformationFile
    replace_seen: list[int] = []
    classes_seen: list[int] = []

    def inspect_native(
        handle: object,
        iosb: object,
        buffer: object,
        length: int,
        information_class: int,
    ) -> int:
        head = ctypes.cast(
            buffer, ctypes.POINTER(durable._FILE_RENAME_INFORMATION_HEAD)
        ).contents
        replace_seen.append(int(head.ReplaceIfExists))
        classes_seen.append(int(information_class))
        return int(real_native(handle, iosb, buffer, length, information_class))

    monkeypatch.setattr(durable, "_NtSetInformationFile", inspect_native)

    receipt = write_durable_bytes(
        parent=approved_parent,
        filename="replace.bin",
        payload=b"new",
        replace=True,
        max_bytes=64,
    )

    assert replace_seen == [1]
    assert classes_seen == [10]
    assert receipt.path.read_bytes() == b"new"
    assert _visible_files(approved_parent) == [approved_parent.path / "replace.bin"]


def test_post_rename_same_hash_different_handle_identity_is_rejected(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"same-hash-but-different-file-identity"
    real_snapshot = durable._snapshot_handle
    snapshot_calls = 0

    def changed_identity(handle: object):
        nonlocal snapshot_calls
        snapshot_calls += 1
        snapshot = real_snapshot(handle)
        if snapshot_calls < 2:
            return snapshot
        return durable._HandleSnapshot(
            identity=durable.DurableFileIdentity(
                volume_serial=snapshot.identity.volume_serial,
                file_index=snapshot.identity.file_index + 1,
            ),
            attributes=snapshot.attributes,
            link_count=snapshot.link_count,
            byte_count=snapshot.byte_count,
        )

    monkeypatch.setattr(durable, "_snapshot_handle", changed_identity)

    with pytest.raises(DurableWriteError) as exc_info:
        write_durable_bytes(
            parent=approved_parent,
            filename="identity.bin",
            payload=payload,
            replace=False,
            max_bytes=64,
        )

    assert exc_info.value.stage == durable.DurableWriteStage.VERIFY_TARGET
    assert exc_info.value.temp_owner is None
    assert exc_info.value.target is not None
    assert exc_info.value.target.read_bytes() == payload


def test_post_rename_corrupt_oversize_read_is_rejected_without_receipt(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        durable.win32file,
        "ReadFile",
        lambda _handle, _size, *_args: (0, b"corrupt-and-too-long"),
    )

    with pytest.raises(DurableWriteError) as exc_info:
        write_durable_bytes(
            parent=approved_parent,
            filename="corrupted.bin",
            payload=b"short",
            replace=False,
            max_bytes=5,
        )

    assert exc_info.value.stage == durable.DurableWriteStage.VERIFY_TARGET
    assert exc_info.value.temp_owner is None
    assert exc_info.value.target is not None
    assert exc_info.value.target.read_bytes() == b"short"


def test_destination_reverification_reads_at_most_max_plus_one_through_open_handle(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    max_bytes = 71
    requested_sizes: list[int] = []
    returned = 0
    real_read = durable.win32file.ReadFile

    def bounded_read(handle: object, size: int, overlapped: object = None):
        nonlocal returned
        assert 1 <= size <= max_bytes + 1 - returned
        requested_sizes.append(size)
        result = real_read(handle, size, overlapped)
        returned += len(result[1])
        assert returned <= max_bytes + 1
        return result

    monkeypatch.setattr(durable.win32file, "ReadFile", bounded_read)

    receipt = write_durable_bytes(
        parent=approved_parent,
        filename="bounded.bin",
        payload=b"x" * max_bytes,
        replace=False,
        max_bytes=max_bytes,
    )

    assert receipt.byte_count == max_bytes
    assert requested_sizes
    assert returned == max_bytes


def test_parallel_no_clobber_has_exactly_one_winner_and_retains_loser_evidence(
    approved_parent: ApprovedPhysicalDirectory,
) -> None:
    worker_count = 8
    barrier = threading.Barrier(worker_count)
    payloads = [f"contender-{index}".encode() for index in range(worker_count)]

    def contend(payload: bytes) -> tuple[str, object]:
        barrier.wait(timeout=10)
        try:
            receipt = write_durable_bytes(
                parent=approved_parent,
                filename="concurrent.bin",
                payload=payload,
                replace=False,
                max_bytes=64,
            )
            return "won", receipt
        except DurableWriteError as exc:
            return "lost", exc

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        outcomes = list(executor.map(contend, payloads))

    winners = [value for state, value in outcomes if state == "won"]
    losers = [value for state, value in outcomes if state == "lost"]
    assert len(winners) == 1
    assert len(losers) == worker_count - 1
    winner = cast(DurableWriteReceipt, winners[0])
    assert winner.path.read_bytes() in payloads
    assert winner.sha256 == hashlib.sha256(winner.path.read_bytes()).hexdigest()
    for loser in cast(list[DurableWriteError], losers):
        assert loser.stage == durable.DurableWriteStage.NATIVE_RENAME
        assert loser.temp_owner is not None
        assert loser.temp_owner.path.exists()
        assert loser.temp_owner.path.read_bytes() in payloads
    assert len(_visible_files(approved_parent)) == worker_count


def test_different_targets_reach_real_native_rename_concurrently_without_global_lock(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = [
        ("parallel-alpha.bin", b"alpha-independent-target"),
        ("parallel-beta.bin", b"beta-independent-target"),
    ]
    native_barrier = threading.Barrier(2, timeout=10)
    observation_lock = threading.Lock()
    real_native = durable._NtSetInformationFile
    entered: list[tuple[int, int]] = []
    passed: list[tuple[int, int, int]] = []

    def rendezvous_then_real_native(
        handle: object,
        iosb: object,
        buffer: object,
        length: int,
        information_class: int,
    ) -> int:
        thread_id = threading.get_ident()
        raw_handle = int(handle)
        with observation_lock:
            entered.append((thread_id, raw_handle))
        ticket = native_barrier.wait()
        with observation_lock:
            passed.append((thread_id, raw_handle, ticket))
        return int(real_native(handle, iosb, buffer, length, information_class))

    monkeypatch.setattr(durable, "_NtSetInformationFile", rendezvous_then_real_native)

    def write_independent_target(case: tuple[str, bytes]) -> DurableWriteReceipt:
        filename, payload = case
        return write_durable_bytes(
            parent=approved_parent,
            filename=filename,
            payload=payload,
            replace=False,
            max_bytes=64,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(write_independent_target, cases))

    assert len(entered) == len(passed) == 2
    assert len({thread_id for thread_id, _raw_handle in entered}) == 2
    assert len({raw_handle for _thread_id, raw_handle in entered}) == 2
    assert sorted(ticket for _thread_id, _raw_handle, ticket in passed) == [0, 1]
    assert {
        (thread_id, raw_handle) for thread_id, raw_handle, _ticket in passed
    } == set(entered)
    assert not native_barrier.broken
    assert native_barrier.n_waiting == 0

    identities: list[tuple[int, int]] = []
    for receipt, (filename, payload) in zip(receipts, cases, strict=True):
        assert isinstance(receipt, DurableWriteReceipt)
        assert receipt.path == Path(approved_parent.stable_volume_path) / filename
        assert receipt.path.read_bytes() == payload
        assert receipt.byte_count == len(payload)
        assert receipt.sha256 == hashlib.sha256(payload).hexdigest()
        identities.append(_identity(receipt.path))

    assert identities[0] != identities[1]
    visible = _visible_files(approved_parent)
    assert {path.name for path in visible} == {filename for filename, _payload in cases}
    assert len(visible) == 2
    assert not any(path.name.startswith(".durable-write-") for path in visible)


# Task 3B-1 native-sibling-rename amendment RED.  Keep this prefix stable so
# the amendment gate can be run without treating the historical MoveFileEx
# tests above as evidence for the new design.


def test_native_amendment_bans_legacy_move_python_open_and_reopen_surface() -> None:
    source = inspect.getsource(durable)
    tree = ast.parse(source)
    banned_calls: list[str] = []
    banned_attributes = {
        "MoveFileEx",
        "ReplaceFile",
        "ReplaceFileW",
        "SetFileInformationByHandle",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "open":
                banned_calls.append("open")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in banned_attributes:
                banned_calls.append(node.func.attr)
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.func.attr in {"replace", "rename", "link"}
            ):
                banned_calls.append(f"os.{node.func.attr}")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported = [alias.name for alias in node.names]
            if "msvcrt" in imported:
                banned_calls.append("msvcrt")

    assert not banned_calls, (
        "native amendment forbids Python-open/path-based rename/reopen: "
        f"{sorted(set(banned_calls))}"
    )
    assert "NtSetInformationFile" in source
    assert "RtlNtStatusToDosError" in source


def test_native_amendment_ntdll_binding_and_x64_abi_are_exact() -> None:
    required = (
        "_NTSTATUS",
        "_ULONG_PTR",
        "_IO_STATUS_BLOCK",
        "_FILE_RENAME_INFORMATION_HEAD",
        "_FILE_RENAME_INFORMATION_CLASS",
        "_NtSetInformationFile",
        "_RtlNtStatusToDosError",
    )
    missing = [name for name in required if not hasattr(durable, name)]
    assert not missing, f"native ABI surface is missing: {missing}"

    ntstatus = durable._NTSTATUS
    ulong_ptr = durable._ULONG_PTR
    iosb = durable._IO_STATUS_BLOCK
    rename_head = durable._FILE_RENAME_INFORMATION_HEAD
    assert ctypes.sizeof(ntstatus) == 4
    assert ntstatus(-1).value == -1
    assert ctypes.sizeof(ulong_ptr) == ctypes.sizeof(ctypes.c_void_p)
    assert durable._FILE_RENAME_INFORMATION_CLASS == 10

    source = inspect.getsource(durable)
    tree = ast.parse(source)
    win_dll_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "WinDLL"
    ]
    assert len(win_dll_calls) == 1
    win_dll = win_dll_calls[0]
    assert len(win_dll.args) == 1
    assert isinstance(win_dll.args[0], ast.Constant)
    assert str(win_dll.args[0].value).casefold() == "ntdll"
    use_last_error = {
        keyword.arg: keyword.value for keyword in win_dll.keywords
    }.get("use_last_error")
    assert isinstance(use_last_error, ast.Constant)
    assert use_last_error.value is False
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "errcheck"
        for node in ast.walk(tree)
    )

    assert len(durable._NtSetInformationFile.argtypes) == 5
    assert durable._NtSetInformationFile.restype is ntstatus
    assert len(durable._RtlNtStatusToDosError.argtypes) == 1

    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(rename_head) == 24
        assert ctypes.alignment(rename_head) == 8
        first_field_name = rename_head._fields_[0][0]
        assert getattr(rename_head, first_field_name).offset == 0
        assert rename_head.RootDirectory.offset == 8
        assert rename_head.FileNameLength.offset == 16
        assert rename_head.FileName.offset == 20
        assert ctypes.sizeof(iosb) == 16
        assert iosb.Information.offset == 8


def test_native_amendment_uses_one_live_handle_in_frozen_ntfs_order(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import win32con
    import win32file

    events: list[tuple[str, int | None]] = []
    create_contract: list[tuple[object, ...]] = []
    semantic_events: list[tuple[str, int | None]] = []
    source_syscalls: list[tuple[str, int]] = []
    snapshots: list[object] = []
    source_raw_seen: int | None = None
    current_native_owner: object | None = None
    verify_count = 0
    flush_count = 0

    def raw(handle: object) -> int:
        return int(handle)

    real_verify = ApprovedPhysicalDirectory.verify
    real_volume = durable.win32api.GetVolumeInformation
    real_create = durable.win32file.CreateFile
    real_set_handle = durable.win32api.SetHandleInformation
    real_get_handle = durable.win32api.GetHandleInformation
    real_write = durable.win32file.WriteFile
    real_flush = durable.win32file.FlushFileBuffers
    real_info = durable.win32file.GetFileInformationByHandle
    real_final_path = durable.win32file.GetFinalPathNameByHandle
    real_rewind = durable.win32file.SetFilePointer
    real_read = durable.win32file.ReadFile
    real_close = durable.win32api.CloseHandle
    real_guard_init = durable.DurableHandleAdoptionGuard.__init__
    real_guard_hold = durable.DurableHandleAdoptionGuard.hold_returned
    real_guard_commit = durable.DurableHandleAdoptionGuard.commit
    real_snapshot = durable._snapshot_handle
    real_native_sibling = durable._native_sibling_rename
    real_native = durable._NtSetInformationFile
    real_native_hook = durable._after_native_return_before_status_commit
    real_bounded_hash = durable._bounded_hash_from_handle
    real_close_owner = durable._close_owner
    real_detach_hook = durable._after_detach_identity_before_close

    def track_verify(self: ApprovedPhysicalDirectory) -> None:
        nonlocal verify_count
        if self is approved_parent:
            verify_count += 1
        real_verify(self)
        if self is approved_parent:
            events.append(("verify", None))
            semantic_events.append(
                (
                    (
                        "verify_entry",
                        "verify_before_preflush",
                        "verify_before_native",
                        "verify_after_rename",
                    )[verify_count - 1],
                    None,
                )
            )

    def track_volume(*args: object, **kwargs: object):
        events.append(("volume", None))
        result = real_volume(*args, **kwargs)
        semantic_events.append(("volume_gate", None))
        return result

    def track_guard_init(self: object, *args: object, **kwargs: object) -> None:
        semantic_events.append(("guard_construct", None))
        real_guard_init(self, *args, **kwargs)

    def track_guard_hold(self: object, handle: object) -> None:
        real_guard_hold(self, handle)
        semantic_events.append(("guard_hold", raw(handle)))

    def track_guard_commit(self: object):
        owner = real_guard_commit(self)
        assert owner.handle is not None
        assert owner.raw_handle == raw(owner.handle)
        semantic_events.append(("guard_commit", owner.raw_handle))
        return owner

    def track_create(*args: object, **kwargs: object):
        nonlocal source_raw_seen
        handle = real_create(*args, **kwargs)
        source_raw_seen = raw(handle)
        events.append(("create", source_raw_seen))
        semantic_events.append(("create_return", source_raw_seen))
        create_contract.append(tuple(args))
        return handle

    def track_set_handle(handle: object, *args: object, **kwargs: object):
        result = real_set_handle(handle, *args, **kwargs)
        handle_raw = raw(handle)
        events.append(("set_noninherit", handle_raw))
        source_syscalls.append(("SetHandleInformation", handle_raw))
        semantic_events.append(("noninherit_set", handle_raw))
        return result

    def track_get_handle(handle: object, *args: object, **kwargs: object):
        result = real_get_handle(handle, *args, **kwargs)
        handle_raw = raw(handle)
        events.append(("get_noninherit", handle_raw))
        source_syscalls.append(("GetHandleInformation", handle_raw))
        semantic_events.append(("noninherit_readback", handle_raw))
        return result

    def track_write(handle: object, *args: object, **kwargs: object):
        result = real_write(handle, *args, **kwargs)
        handle_raw = raw(handle)
        events.append(("write", handle_raw))
        source_syscalls.append(("WriteFile", handle_raw))
        semantic_events.append(("write_exact", handle_raw))
        return result

    def track_flush(handle: object, *args: object, **kwargs: object):
        nonlocal flush_count
        result = real_flush(handle, *args, **kwargs)
        flush_count += 1
        handle_raw = raw(handle)
        events.append(("flush", handle_raw))
        source_syscalls.append(("FlushFileBuffers", handle_raw))
        semantic_events.append(
            (("preflush", "postflush")[flush_count - 1], handle_raw)
        )
        return result

    def track_info(handle: object, *args: object, **kwargs: object):
        handle_raw = raw(handle)
        events.append(("info", handle_raw))
        if source_raw_seen is not None and handle_raw == source_raw_seen:
            source_syscalls.append(("GetFileInformationByHandle", handle_raw))
        return real_info(handle, *args, **kwargs)

    def track_snapshot(handle: object):
        snapshot = real_snapshot(handle)
        snapshots.append(snapshot)
        semantic_events.append(
            (("pre_snapshot", "post_snapshot")[len(snapshots) - 1], raw(handle))
        )
        return snapshot

    def track_native_sibling(**kwargs: object) -> None:
        nonlocal current_native_owner
        current_native_owner = kwargs["owner"]
        real_native_sibling(**kwargs)
        owner = current_native_owner
        assert owner.path_state is durable.DurablePathState.TARGET_CONFIRMED
        semantic_events.append(("target_commit", owner.raw_handle))

    def track_native(
        handle: object,
        iosb: object,
        buffer: object,
        length: int,
        information_class: int,
    ) -> int:
        owner = current_native_owner
        assert owner is not None
        handle_raw = raw(handle)
        assert handle_raw == owner.raw_handle
        assert owner.path_state is durable.DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
        assert owner.handle_state is durable.DurableHandleState.OPEN
        assert owner.native_storage is not None
        assert owner.native_iosb is getattr(iosb, "_obj", None)
        assert owner.native_name is not None
        assert int(owner.native_iosb.Status) & 0xFFFFFFFF == durable._STATUS_PENDING
        assert int(owner.native_iosb.Information) == 0
        assert ctypes.cast(buffer, ctypes.c_void_p).value == ctypes.addressof(
            owner.native_storage
        )
        assert length == ctypes.sizeof(durable._FILE_RENAME_INFORMATION_HEAD) + len(
            owner.native_name
        )
        assert information_class == durable._FILE_RENAME_INFORMATION_CLASS
        source_syscalls.append(("NtSetInformationFile", handle_raw))
        semantic_events.append(("native_in_flight_buffer_iosb", handle_raw))
        returned = int(real_native(handle, iosb, buffer, length, information_class))
        assert durable._signed_ntstatus(returned) == durable._STATUS_SUCCESS
        semantic_events.append(("native_syscall_status_zero", handle_raw))
        return returned

    def track_native_hook(owner: object, status: int) -> None:
        assert owner is current_native_owner
        assert status == durable._STATUS_SUCCESS
        assert owner.path_state is durable.DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
        assert int(owner.native_iosb.Status) == durable._STATUS_SUCCESS
        semantic_events.append(("native_return_hook_status_zero", owner.raw_handle))
        real_native_hook(owner, status)

    def track_final_path(handle: object, *args: object, **kwargs: object):
        result = real_final_path(handle, *args, **kwargs)
        handle_raw = raw(handle)
        events.append(("final_path", handle_raw))
        if source_raw_seen is not None and handle_raw == source_raw_seen:
            source_syscalls.append(("GetFinalPathNameByHandle", handle_raw))
            semantic_events.append(("post_path", handle_raw))
        return result

    def track_rewind(handle: object, *args: object, **kwargs: object):
        result = real_rewind(handle, *args, **kwargs)
        handle_raw = raw(handle)
        events.append(("rewind", handle_raw))
        source_syscalls.append(("SetFilePointer", handle_raw))
        assert result == 0
        semantic_events.append(("rewind_zero", handle_raw))
        return result

    def track_read(handle: object, *args: object, **kwargs: object):
        handle_raw = raw(handle)
        events.append(("read", handle_raw))
        source_syscalls.append(("ReadFile", handle_raw))
        return real_read(handle, *args, **kwargs)

    def track_bounded_hash(**kwargs: object):
        result = real_bounded_hash(**kwargs)
        handle_raw = raw(kwargs["handle"])
        assert result == (hashlib.sha256(payload).hexdigest(), len(payload))
        semantic_events.append(("bounded_hash", handle_raw))
        return result

    def track_close_owner(owner: object):
        assert owner.handle is not None
        frozen_raw = raw(owner.handle)
        assert owner.raw_handle == frozen_raw != 0
        semantic_events.append(("freeze_raw", frozen_raw))
        close_error = real_close_owner(owner)
        assert close_error is None
        assert owner.handle_state is durable.DurableHandleState.CLOSED
        semantic_events.append(("closed", frozen_raw))
        return close_error

    def track_detach(owner: object, detached_raw: int) -> None:
        assert owner.handle is None
        assert owner.raw_handle == detached_raw == source_raw_seen
        source_syscalls.append(("Detach", int(detached_raw)))
        semantic_events.append(("detach_exact", int(detached_raw)))
        real_detach_hook(owner, detached_raw)

    def track_close(handle: object, *args: object, **kwargs: object):
        handle_raw = raw(handle)
        result = real_close(handle, *args, **kwargs)
        assert result is None
        events.append(("close", handle_raw))
        source_syscalls.append(("CloseHandle", handle_raw))
        semantic_events.append(("close_none", handle_raw))
        return result

    monkeypatch.setattr(ApprovedPhysicalDirectory, "verify", track_verify)
    monkeypatch.setattr(durable.DurableHandleAdoptionGuard, "__init__", track_guard_init)
    monkeypatch.setattr(
        durable.DurableHandleAdoptionGuard, "hold_returned", track_guard_hold
    )
    monkeypatch.setattr(
        durable.DurableHandleAdoptionGuard, "commit", track_guard_commit
    )
    monkeypatch.setattr(durable.win32api, "GetVolumeInformation", track_volume)
    monkeypatch.setattr(durable.win32file, "CreateFile", track_create)
    monkeypatch.setattr(durable.win32api, "SetHandleInformation", track_set_handle)
    monkeypatch.setattr(durable.win32api, "GetHandleInformation", track_get_handle)
    monkeypatch.setattr(durable.win32file, "WriteFile", track_write)
    monkeypatch.setattr(durable.win32file, "FlushFileBuffers", track_flush)
    monkeypatch.setattr(durable.win32file, "GetFileInformationByHandle", track_info)
    monkeypatch.setattr(durable.win32file, "GetFinalPathNameByHandle", track_final_path)
    monkeypatch.setattr(durable.win32file, "SetFilePointer", track_rewind)
    monkeypatch.setattr(durable.win32file, "ReadFile", track_read)
    monkeypatch.setattr(durable.win32api, "CloseHandle", track_close)
    monkeypatch.setattr(durable, "_snapshot_handle", track_snapshot)
    monkeypatch.setattr(durable, "_native_sibling_rename", track_native_sibling)
    monkeypatch.setattr(durable, "_NtSetInformationFile", track_native)
    monkeypatch.setattr(
        durable, "_after_native_return_before_status_commit", track_native_hook
    )
    monkeypatch.setattr(durable, "_bounded_hash_from_handle", track_bounded_hash)
    monkeypatch.setattr(durable, "_close_owner", track_close_owner)
    monkeypatch.setattr(durable, "_after_detach_identity_before_close", track_detach)

    payload = b"one-live-handle"
    receipt = write_durable_bytes(
        parent=approved_parent,
        filename="native-one-handle.bin",
        payload=payload,
        replace=False,
        max_bytes=64,
    )
    semantic_events.append(("receipt", None))

    assert receipt.path.read_bytes() == payload
    create_events = [event for event in events if event[0] == "create"]
    assert len(create_events) == 1
    source_raw = cast(int, create_events[0][1])
    labels = [
        label
        for label, event_raw in events
        if event_raw is None or event_raw == source_raw
    ]
    assert labels.count("verify") == 4
    assert labels.count("volume") == 1
    assert labels.count("create") == 1
    assert labels.count("write") == 1
    assert labels.count("flush") == 2
    assert labels.count("rewind") == 1
    assert labels.count("close") == 1
    assert labels[-1] == "close"

    verify_positions = [i for i, label in enumerate(labels) if label == "verify"]
    flush_positions = [i for i, label in enumerate(labels) if label == "flush"]
    assert verify_positions[0] < labels.index("volume") < labels.index("create")
    assert (
        labels.index("create")
        < labels.index("set_noninherit")
        < labels.index("get_noninherit")
        < labels.index("write")
        < verify_positions[1]
        < flush_positions[0]
    )
    assert any(
        flush_positions[0] < i < verify_positions[2]
        for i, label in enumerate(labels)
        if label == "info"
    )
    assert verify_positions[2] < verify_positions[3] < flush_positions[1]
    assert flush_positions[1] < labels.index("rewind") < labels.index("read")
    assert labels.index("rewind") < labels.index("final_path") < labels.index("close")
    assert all(
        event_raw == source_raw
        for label, event_raw in events
        if label
        in {
            "set_noninherit",
            "get_noninherit",
            "write",
            "flush",
            "rewind",
            "read",
            "close",
        }
    )

    assert len(create_contract) == 1
    create_args = create_contract[0]
    assert str(create_args[0]).startswith(approved_parent.stable_volume_path)
    assert create_args[1] == (
        win32con.GENERIC_READ
        | win32con.GENERIC_WRITE
        | win32con.DELETE
        | durable._FILE_READ_ATTRIBUTES
        | win32con.SYNCHRONIZE
    )
    assert create_args[2] == win32file.FILE_SHARE_READ
    assert create_args[4] == win32con.CREATE_NEW
    assert create_args[5] == (
        win32con.FILE_ATTRIBUTE_NORMAL
        | win32file.FILE_FLAG_OPEN_REPARSE_POINT
        | win32file.FILE_FLAG_WRITE_THROUGH
    )

    assert len(snapshots) == 2
    pre_snapshot, post_snapshot = snapshots
    assert pre_snapshot.identity == post_snapshot.identity
    assert pre_snapshot.identity.volume_serial == approved_parent.identity.volume_serial
    assert pre_snapshot.identity.file_index != 0
    assert pre_snapshot.link_count == post_snapshot.link_count == 1
    assert pre_snapshot.byte_count == post_snapshot.byte_count == len(payload)
    assert [label for label, _event_raw in semantic_events] == [
        "verify_entry",
        "volume_gate",
        "guard_construct",
        "create_return",
        "guard_hold",
        "guard_commit",
        "noninherit_set",
        "noninherit_readback",
        "write_exact",
        "verify_before_preflush",
        "preflush",
        "pre_snapshot",
        "verify_before_native",
        "native_in_flight_buffer_iosb",
        "native_syscall_status_zero",
        "native_return_hook_status_zero",
        "target_commit",
        "verify_after_rename",
        "postflush",
        "rewind_zero",
        "post_snapshot",
        "post_path",
        "bounded_hash",
        "freeze_raw",
        "detach_exact",
        "close_none",
        "closed",
        "receipt",
    ]
    assert source_raw_seen == source_raw != 0
    assert all(
        event_raw is None or event_raw == source_raw
        for _label, event_raw in semantic_events
    )
    assert source_syscalls
    assert all(event_raw == source_raw for _label, event_raw in source_syscalls)


@pytest.mark.parametrize("gate_failure", ["serial_mismatch", "non_ntfs"])
def test_native_amendment_volume_gate_fails_before_create_with_zero_files(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    gate_failure: str,
) -> None:
    volume_calls: list[str] = []
    create_calls: list[str] = []
    serial = approved_parent.identity.volume_serial

    def rejected_volume(path: str):
        volume_calls.append(path)
        if gate_failure == "serial_mismatch":
            return ("D", serial + 1, 255, 0, "NTFS")
        return ("D", serial, 255, 0, "exFAT")

    def forbidden_create(*_args: object, **_kwargs: object):
        create_calls.append("create")
        raise AssertionError("CreateFile must not run after a failed volume gate")

    monkeypatch.setattr(durable.win32api, "GetVolumeInformation", rejected_volume)
    monkeypatch.setattr(durable.win32file, "CreateFile", forbidden_create)

    with pytest.raises(DurableWriteError):
        write_durable_bytes(
            parent=approved_parent,
            filename=f"{gate_failure}.bin",
            payload=b"must-not-be-created",
            replace=False,
            max_bytes=64,
        )

    assert volume_calls == [approved_parent.identity.volume_guid]
    assert create_calls == []
    assert _visible_files(approved_parent) == []


def test_native_amendment_create_returned_before_attach_unknown_is_failfast() -> None:
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    case = TEST_TMP_ROOT / f"adoption-child-{os.getpid()}-{os.urandom(8).hex()}"
    marker = TEST_TMP_ROOT / f"adoption-marker-{os.getpid()}-{os.urandom(8).hex()}.txt"
    child_tmp = TEST_TMP_ROOT / "subprocess-temp"
    case.mkdir()
    child_tmp.mkdir(exist_ok=True)
    script = textwrap.dedent(
        r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_RED_CASE"])
        marker = Path(os.environ["DURABLE_RED_MARKER"])

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        nonowning_raw_handle = None

        def after_create_return_before_attach(handle, *_args, **_kwargs):
            global nonowning_raw_handle
            raw_handle = int(handle)
            durable.win32api.GetHandleInformation(raw_handle)
            nonowning_raw_handle = raw_handle
            record(f"after-create-live:{raw_handle}")
            raise KeyboardInterrupt("formal attach commit interrupted")

        def adoption_failfast(*_args, **_kwargs):
            assert nonowning_raw_handle is not None
            durable.win32api.GetHandleInformation(nonowning_raw_handle)
            record(f"adoption-failfast-live:{nonowning_raw_handle}")
            os._exit(73)

        durable._after_create_return_before_attach = after_create_return_before_attach
        durable._failfast_unadopted_create = adoption_failfast
        parent = approve_physical_d_write_root(case)
        receipt = durable.write_durable_bytes(
            parent=parent,
            filename="must-not-receipt.bin",
            payload=b"candidate-evidence",
            replace=False,
            max_bytes=64,
        )
        record("receipt:" + str(receipt.path))
        parent.close()
        """
    )
    env = os.environ.copy()
    env.update(
        {
            "DURABLE_RED_CASE": str(case),
            "DURABLE_RED_MARKER": str(marker),
            "PYTHONPATH": str(REPO_ROOT / "backend"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(child_tmp),
            "TMP": str(child_tmp),
        }
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=REPO_ROOT / "backend",
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        marker_lines = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
        candidates = list(case.iterdir())
        candidate_snapshot: list[dict[str, object]] = []
        for candidate in candidates:
            payload = candidate.read_bytes()
            volume_serial, file_index = _identity(candidate)
            candidate_snapshot.append(
                {
                    "name": candidate.name,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "volume_serial": volume_serial,
                    "file_index": file_index,
                    "content_prefix_hex": payload[:32].hex(),
                }
            )

        after_create_markers = [
            line for line in marker_lines if line.startswith("after-create-live:")
        ]
        failfast_markers = [
            line for line in marker_lines if line.startswith("adoption-failfast-live:")
        ]
        expected_empty_sha256 = hashlib.sha256(b"").hexdigest()
        contract_satisfied = (
            completed.returncode == 73
            and len(after_create_markers) == 1
            and len(failfast_markers) == 1
            and after_create_markers[0].partition(":")[2]
            == failfast_markers[0].partition(":")[2]
            and len(marker_lines) == 2
            and len(candidate_snapshot) == 1
            and str(candidate_snapshot[0]["name"]).startswith(".durable-write-")
            and candidate_snapshot[0]["size"] == 0
            and candidate_snapshot[0]["sha256"] == expected_empty_sha256
            and candidate_snapshot[0]["content_prefix_hex"] == ""
            and not (case / "must-not-receipt.bin").exists()
        )
        red_evidence = {
            "candidate_snapshot_before_cleanup": candidate_snapshot,
            "marker_lines": marker_lines,
            "returncode": completed.returncode,
            "stderr_tail": completed.stderr[-1200:],
            "stdout_tail": completed.stdout[-1200:],
        }
        assert contract_satisfied, json.dumps(
            red_evidence, ensure_ascii=False, sort_keys=True
        )
    finally:
        if marker.exists():
            marker.unlink()
        for child in list(case.iterdir()):
            child.unlink()
        case.rmdir()


def _walk_exception_graph(error: BaseException) -> list[BaseException]:
    found: list[BaseException] = []
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        cause = getattr(current, "cause", None)
        if isinstance(cause, BaseException):
            pending.append(cause)
        if isinstance(current.__cause__, BaseException):
            pending.append(current.__cause__)
    return found


def test_native_amendment_main_baseexception_and_close_failure_are_grouped(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_close = durable.win32api.CloseHandle
    close_calls: list[int] = []

    def interrupted_write(*_args: object, **_kwargs: object):
        raise KeyboardInterrupt("write interrupted")

    def close_then_registration_fails(raw_handle: int):
        close_calls.append(int(raw_handle))
        real_close(raw_handle)
        raise OSError("CloseHandle result registration interrupted")

    monkeypatch.setattr(durable.win32file, "WriteFile", interrupted_write)
    monkeypatch.setattr(durable.win32api, "CloseHandle", close_then_registration_fails)

    with pytest.raises(BaseExceptionGroup) as captured:
        write_durable_bytes(
            parent=approved_parent,
            filename="baseexception-group.bin",
            payload=b"interrupt",
            replace=False,
            max_bytes=64,
        )

    graph = _walk_exception_graph(captured.value)
    assert type(captured.value) is BaseExceptionGroup
    assert any(
        isinstance(error, DurableWriteError)
        for error in captured.value.exceptions
    )
    assert any(
        isinstance(error, KeyboardInterrupt)
        for error in captured.value.exceptions
    )
    assert any(isinstance(error, KeyboardInterrupt) for error in graph)
    assert any(isinstance(error, OSError) for error in graph)
    assert len(close_calls) == 1


class _ObservedNativeFailFast(BaseException):
    def __init__(self, exit_code: int):
        super().__init__(exit_code)
        self.exit_code = exit_code


class _FakeAdoptedHandle:
    def __init__(self, raw_handle: int, detached_handle: int):
        self.raw_handle = raw_handle
        self.detached_handle = detached_handle
        self.detach_calls = 0

    def __int__(self) -> int:
        return self.raw_handle

    def Detach(self) -> int:
        self.detach_calls += 1
        return self.detached_handle


def _install_preclose_failure_harness(
    *,
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    handle: _FakeAdoptedHandle,
    close_calls: list[int],
    close_error: BaseException | None,
) -> None:
    real_verify = ApprovedPhysicalDirectory.verify
    real_file_info = durable.win32file.GetFileInformationByHandle
    verify_calls = 0

    def fail_second_verify(self: ApprovedPhysicalDirectory) -> None:
        nonlocal verify_calls
        if self is approved_parent:
            verify_calls += 1
            if verify_calls > 1:
                raise RuntimeError("injected pre-flush parent failure")
        real_verify(self)

    def fake_close(raw_handle: int) -> None:
        close_calls.append(int(raw_handle))
        if close_error is not None:
            raise close_error
        return None

    monkeypatch.setattr(ApprovedPhysicalDirectory, "verify", fail_second_verify)
    monkeypatch.setattr(
        durable.win32api,
        "GetVolumeInformation",
        lambda _path: (
            "D",
            approved_parent.identity.volume_serial,
            255,
            0,
            "NTFS",
        ),
    )
    monkeypatch.setattr(durable.win32file, "CreateFile", lambda *_a, **_k: handle)
    monkeypatch.setattr(durable.win32api, "SetHandleInformation", lambda *_a, **_k: None)
    monkeypatch.setattr(durable.win32api, "GetHandleInformation", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        durable.win32file,
        "WriteFile",
        lambda _handle, payload, *_a, **_k: (0, len(payload)),
    )
    def fake_file_info(candidate: object):
        if candidate is not handle:
            return real_file_info(candidate)
        return (
            0,
            None,
            None,
            None,
            approved_parent.identity.volume_serial,
            0,
            1,
            1,
            0,
            handle.raw_handle,
        )

    monkeypatch.setattr(
        durable.win32file, "GetFileInformationByHandle", fake_file_info
    )
    monkeypatch.setattr(durable.win32api, "CloseHandle", fake_close)
    monkeypatch.setattr(
        durable.os,
        "_exit",
        lambda exit_code: (_ for _ in ()).throw(_ObservedNativeFailFast(exit_code)),
    )


def test_native_amendment_detach_identity_mismatch_failfasts_without_close(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = _FakeAdoptedHandle(raw_handle=0x1234, detached_handle=0x4321)
    close_calls: list[int] = []
    _install_preclose_failure_harness(
        approved_parent=approved_parent,
        monkeypatch=monkeypatch,
        handle=handle,
        close_calls=close_calls,
        close_error=None,
    )

    with pytest.raises(BaseExceptionGroup) as captured:
        write_durable_bytes(
            parent=approved_parent,
            filename="wrong-detach.bin",
            payload=b"x",
            replace=False,
            max_bytes=64,
        )

    graph = _walk_exception_graph(captured.value)
    observed = [error for error in graph if isinstance(error, _ObservedNativeFailFast)]
    operation_errors = [
        error
        for error in captured.value.exceptions
        if type(error) is DurableWriteError
    ]
    assert len(observed) == 1
    assert observed[0].exit_code == durable._FAILFAST_DETACH_IDENTITY
    assert len(operation_errors) == 1
    assert (
        operation_errors[0].stage
        is durable.DurableWriteStage.VERIFY_PARENT_BEFORE_FLUSH
    )
    assert handle.detach_calls == 1
    assert close_calls == []


def test_native_amendment_incomplete_close_quarantine_is_failfast(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert durable._CLOSE_UNKNOWN_REGISTRY_CAPACITY == 32

    class ExplodingRegistryLock:
        def __enter__(self):
            raise KeyboardInterrupt("quarantine lock acquisition interrupted")

        def __exit__(self, *_exc_info: object) -> None:
            return None

    monkeypatch.setattr(
        durable,
        "_CLOSE_UNKNOWN_REGISTRY_LOCK",
        ExplodingRegistryLock(),
    )
    handle = _FakeAdoptedHandle(raw_handle=0x5678, detached_handle=0x5678)
    close_calls: list[int] = []
    _install_preclose_failure_harness(
        approved_parent=approved_parent,
        monkeypatch=monkeypatch,
        handle=handle,
        close_calls=close_calls,
        close_error=OSError("CloseHandle result unknown"),
    )

    with pytest.raises(BaseExceptionGroup) as captured:
        write_durable_bytes(
            parent=approved_parent,
            filename="quarantine-incomplete.bin",
            payload=b"x",
            replace=False,
            max_bytes=64,
        )

    graph = _walk_exception_graph(captured.value)
    observed = [error for error in graph if isinstance(error, _ObservedNativeFailFast)]
    operation_errors = [
        error
        for error in captured.value.exceptions
        if type(error) is DurableWriteError
    ]
    assert len(observed) == 1
    assert observed[0].exit_code == durable._FAILFAST_CLOSE_QUARANTINE
    assert len(operation_errors) == 1
    assert (
        operation_errors[0].stage
        is durable.DurableWriteStage.VERIFY_PARENT_BEFORE_FLUSH
    )
    assert handle.detach_calls == 1
    assert close_calls == [handle.raw_handle]


def _run_native_boundary_child(
    *, script: str, label: str
) -> tuple[subprocess.CompletedProcess[str], list[str], list[dict[str, object]]]:
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    nonce = f"{os.getpid()}-{os.urandom(8).hex()}"
    case = TEST_TMP_ROOT / f"boundary-{label}-{nonce}"
    marker = TEST_TMP_ROOT / f"boundary-{label}-{nonce}.txt"
    child_tmp = TEST_TMP_ROOT / "subprocess-temp"
    case.mkdir()
    child_tmp.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "DURABLE_BOUNDARY_CASE": str(case),
            "DURABLE_BOUNDARY_MARKER": str(marker),
            "PYTHONPATH": str(REPO_ROOT / "backend"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(child_tmp),
            "TMP": str(child_tmp),
        }
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-B", "-c", textwrap.dedent(script)],
            cwd=REPO_ROOT / "backend",
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        marker_lines = (
            marker.read_text(encoding="utf-8").splitlines()
            if marker.exists()
            else []
        )
        candidate_snapshot: list[dict[str, object]] = []
        for candidate in case.iterdir():
            payload = candidate.read_bytes()
            volume_serial, file_index = _identity(candidate)
            candidate_snapshot.append(
                {
                    "name": candidate.name,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "volume_serial": volume_serial,
                    "file_index": file_index,
                    "content_prefix_hex": payload[:32].hex(),
                }
            )
        return completed, marker_lines, candidate_snapshot
    finally:
        if marker.exists():
            marker.unlink()
        for child in list(case.iterdir()):
            child.unlink()
        case.rmdir()


def test_native_boundary_create_return_to_guard_hold_has_no_unprotected_line() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="create-gap",
        script=r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        nonowning_raw = None

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        real_close = durable.win32api.CloseHandle

        def track_close(raw):
            record(f"close:{int(raw)}")
            return real_close(raw)

        def interrupt_after_return(returned):
            global nonowning_raw
            nonowning_raw = int(returned)
            durable.win32api.GetHandleInformation(nonowning_raw)
            record(f"create-gap-live:{nonowning_raw}")
            raise KeyboardInterrupt("interrupt after CreateFile return")

        def failfast(*_args, **_kwargs):
            assert nonowning_raw is not None
            durable.win32api.GetHandleInformation(nonowning_raw)
            record(f"create-gap-failfast-live:{nonowning_raw}")
            os._exit(81)

        durable.win32api.CloseHandle = track_close
        durable._after_create_return_before_attach = interrupt_after_return
        durable._failfast_unadopted_create = failfast
        parent = approve_physical_d_write_root(case)
        durable.write_durable_bytes(
            parent=parent,
            filename="create-gap.bin",
            payload=b"not-written",
            replace=False,
            max_bytes=64,
        )
        record("receipt")
        """,
    )
    create_markers = [line for line in markers if line.startswith("create-gap-live:")]
    failfast_markers = [
        line for line in markers if line.startswith("create-gap-failfast-live:")
    ]
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-800:],
    }
    assert completed.returncode == 81, json.dumps(evidence, sort_keys=True)
    assert len(create_markers) == len(failfast_markers) == 1, evidence
    assert create_markers[0].partition(":")[2] == failfast_markers[0].partition(":")[2]
    assert [line for line in markers if line.startswith("close:")] == []
    assert all(candidate["size"] == 0 for candidate in candidates)
    assert "receipt" not in markers


def test_native_boundary_detach_to_close_has_no_unprotected_line() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="detach-gap",
        script=r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        nonowning_raw = None

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        real_close = durable.win32api.CloseHandle

        def track_close(raw):
            record(f"close:{int(raw)}")
            return real_close(raw)

        def interrupt_after_detach(_owner, raw):
            global nonowning_raw
            nonowning_raw = int(raw)
            durable.win32api.GetHandleInformation(nonowning_raw)
            record(f"detach-gap-live:{nonowning_raw}")
            raise KeyboardInterrupt("interrupt after Detach identity gate")

        def failfast(*_args, **_kwargs):
            assert nonowning_raw is not None
            durable.win32api.GetHandleInformation(nonowning_raw)
            record(f"detach-gap-failfast-live:{nonowning_raw}")
            os._exit(82)

        durable.win32api.CloseHandle = track_close
        durable._after_detach_identity_before_close = interrupt_after_detach
        durable._failfast_close_quarantine = failfast
        parent = approve_physical_d_write_root(case)
        durable.write_durable_bytes(
            parent=parent,
            filename="detach-gap.bin",
            payload=b"written-before-close",
            replace=False,
            max_bytes=64,
        )
        record("receipt")
        """,
    )
    detach_markers = [line for line in markers if line.startswith("detach-gap-live:")]
    failfast_markers = [
        line for line in markers if line.startswith("detach-gap-failfast-live:")
    ]
    close_markers = [line for line in markers if line.startswith("close:")]
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-800:],
    }
    assert completed.returncode == 82, json.dumps(evidence, sort_keys=True)
    assert len(detach_markers) == len(failfast_markers) == 1, evidence
    assert detach_markers[0].partition(":")[2] == failfast_markers[0].partition(":")[2]
    assert close_markers == []
    assert "receipt" not in markers


def test_native_boundary_close_return_to_closed_commit_is_quarantined_or_failfast() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="close-return-gap",
        script=r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        primary = KeyboardInterrupt("interrupt after CloseHandle return")

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        real_close = durable.win32api.CloseHandle

        def track_close(raw):
            record(f"close:{int(raw)}")
            return real_close(raw)

        def interrupt_close_commit(_owner, _raw):
            record("close-return-gap")
            raise primary

        def failfast(*_args, **_kwargs):
            record("close-return-failfast")
            os._exit(83)

        durable.win32api.CloseHandle = track_close
        durable._after_close_return_before_state_commit = interrupt_close_commit
        durable._failfast_close_quarantine = failfast
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="close-return-gap.bin",
                payload=b"closed-once",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            # native-boundary-RED.txt fixed quarantine and close-once; the
            # cancellation contract additionally requires typed + raw direct.
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            close_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.CLOSE
            ]
            assert len(close_errors) == 1
            assert isinstance(
                close_errors[0].evidence_owner, durable.CloseUnknownToken
            )
            assert sum(item is primary for item in direct) == 1
            record("close-return-quarantined")
        else:
            record("receipt")
        """,
    )
    close_markers = [line for line in markers if line.startswith("close:")]
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-800:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert len(close_markers) == 1, evidence
    assert markers.count("close-return-gap") == 1
    assert markers.count("close-return-quarantined") == 1
    assert "close-return-failfast" not in markers
    assert "receipt" not in markers


def test_native_boundary_pending_preempts_after_return_hook_reconcile_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = durable._DurableOwner(
        temp_path=Path(r"D:\pending-temp.tmp"),
        target_path=Path(r"D:\pending-target.tmp"),
        sha256="0" * 64,
        byte_count=0,
        path_state=durable.DurablePathState.TEMP_CONFIRMED,
        handle_state=durable.DurableHandleState.OPEN,
        handle=object(),
        raw_handle=0x1234,
    )
    calls = {"hook": 0, "reconcile": 0, "close": 0}

    class PendingFailFastObserved(BaseException):
        pass

    def return_pending(*_args: object) -> int:
        return durable._STATUS_PENDING

    def forbidden_hook(*_args: object) -> None:
        calls["hook"] += 1
        raise KeyboardInterrupt("pending must preempt this hook")

    def forbidden_reconcile(*_args: object) -> None:
        calls["reconcile"] += 1
        return None

    def forbidden_close(*_args: object) -> None:
        calls["close"] += 1
        return None

    def pending_failfast(*_args: object) -> None:
        raise PendingFailFastObserved()

    monkeypatch.setattr(durable, "_NtSetInformationFile", return_pending)
    monkeypatch.setattr(
        durable, "_after_native_return_before_status_commit", forbidden_hook
    )
    monkeypatch.setattr(durable, "_try_reconcile_owner_location", forbidden_reconcile)
    monkeypatch.setattr(durable.win32api, "CloseHandle", forbidden_close)
    monkeypatch.setattr(
        durable, "_failfast_pending_native_rename", pending_failfast
    )
    monkeypatch.setattr(
        durable.os,
        "_exit",
        lambda _code: (_ for _ in ()).throw(PendingFailFastObserved()),
    )

    with pytest.raises(PendingFailFastObserved):
        durable._native_sibling_rename(
            owner=owner,
            basename="pending-target.tmp",
            name_utf16le="pending-target.tmp".encode("utf-16-le"),
            replace=False,
        )

    assert calls == {"hook": 0, "reconcile": 0, "close": 0}
    assert owner.path_state is durable.DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
    assert owner.handle_state is durable.DurableHandleState.OPEN


def test_native_boundary_close_in_flight_state_to_phase_try_is_protected() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="close-state-gap",
        script=r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        returned_raw = None

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        real_close = durable.win32api.CloseHandle

        def track_close(raw):
            record(f"close:{int(raw)}")
            return real_close(raw)

        def interrupt_before_detach(_owner):
            record("close-state-gap")
            raise KeyboardInterrupt("interrupt after CLOSE_IN_FLIGHT state mutation")

        def failfast(*_args, **_kwargs):
            record("close-state-failfast")
            os._exit(86)

        durable.win32api.CloseHandle = track_close
        durable._before_close_detach_identity = interrupt_before_detach
        durable._failfast_detach_identity = failfast
        parent = approve_physical_d_write_root(case)
        durable.write_durable_bytes(
            parent=parent,
            filename="close-state-gap.bin",
            payload=b"state-gap",
            replace=False,
            max_bytes=64,
        )
        record("receipt")
        """,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-800:],
    }
    assert completed.returncode == 86, json.dumps(evidence, sort_keys=True)
    assert markers.count("close-state-gap") == 1
    assert markers.count("close-state-failfast") == 1
    assert not any(marker.startswith("close:") for marker in markers)
    assert "receipt" not in markers


def test_native_boundary_close_exception_routes_directly_into_quarantine() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="close-except-gap",
        script=r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        primary = KeyboardInterrupt("interrupt before quarantine handoff")

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        real_close = durable.win32api.CloseHandle

        def close_then_unknown(raw):
            record(f"close:{int(raw)}")
            real_close(raw)
            raise OSError("CloseHandle result unknown")

        def interrupt_before_quarantine(*_args, **_kwargs):
            record("close-exception-boundary-hook")
            raise primary

        durable.win32api.CloseHandle = close_then_unknown
        durable._after_close_exception_before_quarantine = interrupt_before_quarantine
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="close-except-gap.bin",
                payload=b"close-unknown",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            # native-boundary-RED.txt fixed quarantine and close-once; the
            # cancellation contract additionally requires typed + raw direct.
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            close_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.CLOSE
            ]
            assert len(close_errors) == 1
            assert isinstance(
                close_errors[0].evidence_owner, durable.CloseUnknownToken
            )
            assert sum(item is primary for item in direct) == 1
            record("close-exception-quarantined")
        else:
            record("receipt")
        """,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-800:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("close-exception-boundary-hook") == 1, evidence
    assert markers.count("close-exception-quarantined") == 1, evidence
    assert len([marker for marker in markers if marker.startswith("close:")]) == 1
    assert "receipt" not in markers


def test_native_boundary_hook_return_to_status_commit_reconciles_on_interrupt(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_reconcile = durable._try_reconcile_owner_location
    reconcile_calls = 0
    primary = KeyboardInterrupt("interrupt after native return")

    def reconcile(owner: object):
        nonlocal reconcile_calls
        reconcile_calls += 1
        return real_reconcile(owner)

    def interrupt_hook(_owner: object, status: int) -> None:
        assert status == durable._STATUS_SUCCESS
        raise primary

    monkeypatch.setattr(durable, "_try_reconcile_owner_location", reconcile)
    monkeypatch.setattr(
        durable, "_after_native_return_before_status_commit", interrupt_hook
    )
    with pytest.raises(BaseExceptionGroup) as captured:
        write_durable_bytes(
            parent=approved_parent,
            filename="native-status-gap.bin",
            payload=b"renamed-before-interrupt",
            replace=False,
            max_bytes=64,
        )

    direct = list(captured.value.exceptions)
    operation_errors = [
        item
        for item in direct
        if type(item) is DurableWriteError
        and item.stage is durable.DurableWriteStage.NATIVE_RENAME
    ]
    assert len(operation_errors) == 1
    error = operation_errors[0]
    assert sum(item is primary for item in direct) == 1
    assert reconcile_calls == 1
    assert error.path_state is durable.DurablePathState.TARGET_CONFIRMED
    assert error.temp_owner is None
    assert error.target is not None
    assert error.target.read_bytes() == b"renamed-before-interrupt"


def test_native_status_exact_success_commits_target_without_reconcile(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_native = durable._NtSetInformationFile
    statuses: list[int] = []
    native_handles: list[int] = []
    reconcile_calls = 0

    def exact_success(
        handle: object,
        iosb: object,
        buffer: object,
        length: int,
        information_class: int,
    ) -> int:
        native_handles.append(int(handle))
        returned = int(real_native(handle, iosb, buffer, length, information_class))
        statuses.append(durable._signed_ntstatus(returned))
        return returned

    def forbidden_reconcile(*_args: object) -> None:
        nonlocal reconcile_calls
        reconcile_calls += 1
        raise AssertionError("exact STATUS_SUCCESS must not reconcile")

    monkeypatch.setattr(durable, "_NtSetInformationFile", exact_success)
    monkeypatch.setattr(
        durable, "_try_reconcile_owner_location", forbidden_reconcile
    )

    receipt = write_durable_bytes(
        parent=approved_parent,
        filename="status-exact-success.bin",
        payload=b"exact-success",
        replace=False,
        max_bytes=64,
    )

    assert statuses == [durable._STATUS_SUCCESS]
    assert len(native_handles) == 1 and native_handles[0] != 0
    assert reconcile_calls == 0
    assert receipt.path.read_bytes() == b"exact-success"
    assert _visible_files(approved_parent) == [
        approved_parent.path / "status-exact-success.bin"
    ]


def test_native_status_generic_final_failure_confirms_temp_and_preserves_raw_status(
    approved_parent: ApprovedPhysicalDirectory, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_u32 = 0xC000000D
    signed_status = ctypes.c_int32(raw_u32).value
    mapped_inputs: list[int] = []
    reconcile_calls = 0

    def final_failure(*_args: object) -> int:
        return signed_status

    def map_status(status: int) -> int:
        mapped_inputs.append(int(status))
        return 87

    def forbidden_reconcile(*_args: object) -> None:
        nonlocal reconcile_calls
        reconcile_calls += 1
        raise AssertionError("final failure must not reconcile")

    monkeypatch.setattr(durable, "_NtSetInformationFile", final_failure)
    monkeypatch.setattr(durable, "_RtlNtStatusToDosError", map_status)
    monkeypatch.setattr(
        durable, "_try_reconcile_owner_location", forbidden_reconcile
    )

    with pytest.raises(DurableWriteError) as captured:
        write_durable_bytes(
            parent=approved_parent,
            filename="status-final-failure.bin",
            payload=b"final-failure",
            replace=False,
            max_bytes=64,
        )

    error = captured.value
    assert error.stage is durable.DurableWriteStage.NATIVE_RENAME
    assert error.path_state is durable.DurablePathState.TEMP_CONFIRMED
    assert error.handle_state is durable.DurableHandleState.CLOSED
    assert isinstance(error.cause, durable.NativeRenameStatusError)
    assert error.cause.signed_status == signed_status
    assert error.cause.u32_status == raw_u32
    assert error.cause.mapped_winerror == 87
    assert mapped_inputs == [signed_status]
    assert reconcile_calls == 0
    assert error.temp_owner is not None
    assert error.temp_owner.path.read_bytes() == b"final-failure"
    assert error.target is not None and not error.target.exists()


@pytest.mark.parametrize(
    "reconciled_location",
    ["target", "temp", "unknown"],
    ids=["target-confirmed", "temp-confirmed", "reconcile-unknown"],
)
def test_native_status_informational_reconciles_same_handle_to_target_temp_or_unknown(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    reconciled_location: str,
) -> None:
    real_native = durable._NtSetInformationFile
    real_reconcile = durable._try_reconcile_owner_location
    real_final_path = durable.win32file.GetFinalPathNameByHandle
    native_returned = False
    native_handles: list[int] = []
    reconcile_handles: list[int] = []
    reconcile_results: list[BaseException | None] = []

    def informational_status(
        handle: object,
        iosb: object,
        buffer: object,
        length: int,
        information_class: int,
    ) -> int:
        nonlocal native_returned
        native_handles.append(int(handle))
        if reconciled_location == "target":
            returned = int(
                real_native(handle, iosb, buffer, length, information_class)
            )
            assert durable._signed_ntstatus(returned) == durable._STATUS_SUCCESS
        native_returned = True
        return 1

    def maybe_fail_final_path(handle: object, flags: int):
        if reconciled_location == "unknown" and native_returned:
            raise OSError("same-handle final-path reconcile failed")
        return real_final_path(handle, flags)

    def track_reconcile(owner: object):
        assert owner.path_state is durable.DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
        assert owner.handle is not None
        assert owner.raw_handle == int(owner.handle)
        reconcile_handles.append(int(owner.handle))
        result = real_reconcile(owner)
        reconcile_results.append(result)
        return result

    monkeypatch.setattr(durable, "_NtSetInformationFile", informational_status)
    monkeypatch.setattr(durable, "_RtlNtStatusToDosError", lambda _status: 317)
    monkeypatch.setattr(
        durable.win32file, "GetFinalPathNameByHandle", maybe_fail_final_path
    )
    monkeypatch.setattr(durable, "_try_reconcile_owner_location", track_reconcile)

    with pytest.raises(DurableWriteError) as captured:
        write_durable_bytes(
            parent=approved_parent,
            filename=f"status-informational-{reconciled_location}.bin",
            payload=b"informational-status",
            replace=False,
            max_bytes=64,
        )

    error = captured.value
    graph = _walk_exception_graph(error)
    native_errors = [
        item for item in graph if isinstance(item, durable.NativeRenameStatusError)
    ]
    assert len(native_handles) == 1
    assert reconcile_handles == native_handles
    assert len(reconcile_results) == 1
    assert len(native_errors) == 1
    assert native_errors[0].signed_status == 1
    assert native_errors[0].u32_status == 1
    assert native_errors[0].mapped_winerror == 317
    assert error.stage is durable.DurableWriteStage.NATIVE_RENAME
    assert error.handle_state is durable.DurableHandleState.CLOSED

    if reconciled_location == "target":
        assert reconcile_results == [None]
        assert error.path_state is durable.DurablePathState.TARGET_CONFIRMED
        assert error.temp_owner is None
        assert error.target is not None
        assert error.target.read_bytes() == b"informational-status"
    elif reconciled_location == "temp":
        assert reconcile_results == [None]
        assert error.path_state is durable.DurablePathState.TEMP_CONFIRMED
        assert error.temp_owner is not None
        assert error.temp_owner.path.read_bytes() == b"informational-status"
        assert error.target is not None and not error.target.exists()
    else:
        assert isinstance(reconcile_results[0], OSError)
        assert error.path_state is durable.DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
        assert error.temp_owner is not None
        assert error.temp_owner.path.read_bytes() == b"informational-status"
        assert error.target is not None and not error.target.exists()


@pytest.mark.parametrize(
    "mapping_mode",
    ["returns-317", "raises-baseexception"],
    ids=["rtl-returns-317", "rtl-raises-baseexception"],
)
def test_native_status_unknown_rtl_mapping_preserves_raw_status(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    mapping_mode: str,
) -> None:
    raw_u32 = 0xC1234567
    signed_status = ctypes.c_int32(raw_u32).value
    mapping_inputs: list[int] = []
    mapping_interruption = KeyboardInterrupt(
        "Rtl mapping result registration interrupted"
    )

    def final_failure(*_args: object) -> int:
        return signed_status

    def unknown_mapping(status: int) -> int:
        mapping_inputs.append(int(status))
        if mapping_mode == "raises-baseexception":
            raise mapping_interruption
        return durable._ERROR_MR_MID_NOT_FOUND

    monkeypatch.setattr(durable, "_NtSetInformationFile", final_failure)
    monkeypatch.setattr(durable, "_RtlNtStatusToDosError", unknown_mapping)

    captured_outward: BaseException | None = None
    try:
        write_durable_bytes(
            parent=approved_parent,
            filename=f"rtl-unknown-{mapping_mode}.bin",
            payload=b"raw-status-must-survive",
            replace=False,
            max_bytes=64,
        )
    except BaseException as outward:
        captured_outward = outward
    else:
        raise AssertionError("mapping failure returned a receipt")
    assert captured_outward is not None

    if mapping_mode == "raises-baseexception":
        assert type(captured_outward) is BaseExceptionGroup
        direct = list(captured_outward.exceptions)
        operation_errors = [
            item for item in direct if type(item) is DurableWriteError
        ]
        status_errors = [
            item for item in direct
            if type(item) is durable.NativeRenameStatusError
        ]
        assert len(operation_errors) == len(status_errors) == 1
        assert sum(item is mapping_interruption for item in direct) == 1
        error = operation_errors[0]
        status_error = status_errors[0]
    else:
        assert type(captured_outward) is DurableWriteError
        error = captured_outward
        assert isinstance(error.cause, durable.NativeRenameStatusError)
        status_error = error.cause
    assert error.stage is durable.DurableWriteStage.NATIVE_RENAME
    assert error.path_state is durable.DurablePathState.TEMP_CONFIRMED
    assert status_error.signed_status == signed_status
    assert status_error.u32_status == raw_u32
    assert status_error.mapped_winerror == durable._ERROR_MR_MID_NOT_FOUND
    assert mapping_inputs == [signed_status]
    assert error.temp_owner is not None
    assert error.temp_owner.path.read_bytes() == b"raw-status-must-survive"


@pytest.mark.parametrize(
    ("interruption_type", "reconcile_outcome"),
    [
        (KeyboardInterrupt, "target"),
        (SystemExit, "target"),
        (KeyboardInterrupt, "unknown"),
    ],
    ids=[
        "keyboardinterrupt-target",
        "systemexit-target",
        "keyboardinterrupt-reconcile-fails-unknown",
    ],
)
def test_native_status_baseexception_after_actual_rename_reconciles_same_handle_or_unknown(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    interruption_type: type[BaseException],
    reconcile_outcome: str,
) -> None:
    real_native = durable._NtSetInformationFile
    real_reconcile = durable._try_reconcile_owner_location
    real_final_path = durable.win32file.GetFinalPathNameByHandle
    real_close = durable.win32api.CloseHandle
    native_completed = False
    native_handles: list[int] = []
    reconcile_handles: list[int] = []
    reconcile_results: list[BaseException | None] = []
    close_calls: list[int] = []
    primary = interruption_type("native return registration interrupted")
    reconcile_error = OSError("second same-handle reconcile failed")

    def rename_then_interrupt(
        handle: object,
        iosb: object,
        buffer: object,
        length: int,
        information_class: int,
    ) -> int:
        nonlocal native_completed
        native_handles.append(int(handle))
        returned = int(real_native(handle, iosb, buffer, length, information_class))
        assert durable._signed_ntstatus(returned) == durable._STATUS_SUCCESS
        native_completed = True
        raise primary

    def maybe_fail_final_path(handle: object, flags: int):
        if reconcile_outcome == "unknown" and native_completed:
            raise reconcile_error
        return real_final_path(handle, flags)

    def track_reconcile(owner: object):
        assert owner.path_state is durable.DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
        assert owner.handle is not None
        assert owner.raw_handle == int(owner.handle)
        reconcile_handles.append(int(owner.handle))
        result = real_reconcile(owner)
        reconcile_results.append(result)
        return result

    def track_close(raw_handle: int) -> None:
        close_calls.append(int(raw_handle))
        return real_close(raw_handle)

    monkeypatch.setattr(durable, "_NtSetInformationFile", rename_then_interrupt)
    monkeypatch.setattr(
        durable.win32file, "GetFinalPathNameByHandle", maybe_fail_final_path
    )
    monkeypatch.setattr(durable, "_try_reconcile_owner_location", track_reconcile)
    monkeypatch.setattr(durable.win32api, "CloseHandle", track_close)

    with pytest.raises(BaseExceptionGroup) as captured:
        write_durable_bytes(
            parent=approved_parent,
            filename=(
                f"native-{interruption_type.__name__.lower()}-{reconcile_outcome}.bin"
            ),
            payload=b"rename-happened-before-baseexception",
            replace=False,
            max_bytes=64,
        )

    direct = list(captured.value.exceptions)
    operation_errors = [
        item
        for item in direct
        if type(item) is DurableWriteError
        and item.stage is durable.DurableWriteStage.NATIVE_RENAME
    ]
    assert len(operation_errors) == 1
    error = operation_errors[0]
    assert sum(item is primary for item in direct) == 1
    assert len(native_handles) == 1
    assert reconcile_handles == native_handles
    assert len(reconcile_results) == 1
    assert close_calls == native_handles
    assert error.stage is durable.DurableWriteStage.NATIVE_RENAME
    assert error.handle_state is durable.DurableHandleState.CLOSED
    assert error.target is not None
    assert error.target.read_bytes() == b"rename-happened-before-baseexception"

    if reconcile_outcome == "target":
        assert reconcile_results == [None]
        assert error.path_state is durable.DurablePathState.TARGET_CONFIRMED
        assert error.temp_owner is None
    else:
        assert reconcile_results == [reconcile_error]
        assert sum(item is reconcile_error for item in direct) == 1
        assert error.path_state is durable.DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
        assert error.temp_owner is not None
        assert not error.temp_owner.path.exists()


@pytest.mark.parametrize(
    "pending_mode",
    ["returned-status", "exception-with-iosb-sentinel"],
    ids=["returned-status-pending", "raised-baseexception-iosb-pending"],
)
def test_native_status_pending_is_nonreturning_with_live_resources(
    pending_mode: str,
) -> None:
    script = r"""
        import json
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        mode = __PENDING_MODE__
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        parent = approve_physical_d_write_root(case)
        real_failfast = durable._failfast_pending_native_rename

        def pending_native(*_args):
            record("native-enter")
            if mode == "returned-status":
                record("native-return-pending")
                return durable._STATUS_PENDING
            record("native-raise-with-iosb-sentinel")
            raise KeyboardInterrupt("native wrapper interrupted before return")

        def forbidden_hook(*_args, **_kwargs):
            record("forbidden-after-return-hook")
            os._exit(90)

        def forbidden_reconcile(*_args, **_kwargs):
            record("forbidden-reconcile")
            os._exit(91)

        def forbidden_close(*_args, **_kwargs):
            record("forbidden-close")
            os._exit(92)

        def observe_then_failfast(owner):
            state = {
                "failfast_started": owner.failfast_started,
                "owner_path_state": owner.path_state.value,
                "owner_handle_state": owner.handle_state.value,
                "owner_handle_live": (
                    owner.handle is not None
                    and owner.raw_handle == int(owner.handle)
                    and int(owner.handle) != 0
                ),
                "native_storage_live": owner.native_storage is not None,
                "native_iosb_live": owner.native_iosb is not None,
                "native_name_live": owner.native_name is not None,
                "iosb_status_u32": int(owner.native_iosb.Status) & 0xFFFFFFFF,
                "parent_pin_live": (not parent._closed and int(parent.handle) != 0),
            }
            record("pending-state:" + json.dumps(state, sort_keys=True))
            record("pending-failfast")
            real_failfast(owner)
            record("forbidden-failfast-return")

        durable._NtSetInformationFile = pending_native
        durable._after_native_return_before_status_commit = forbidden_hook
        durable._try_reconcile_owner_location = forbidden_reconcile
        durable.win32api.CloseHandle = forbidden_close
        durable._failfast_pending_native_rename = observe_then_failfast

        durable.write_durable_bytes(
            parent=parent,
            filename="pending-must-not-return.bin",
            payload=b"pending-live-resources",
            replace=False,
            max_bytes=64,
        )
        record("forbidden-write-return")
    """.replace("__PENDING_MODE__", repr(pending_mode))

    completed, markers, candidates = _run_native_boundary_child(
        label=("pending-ret" if pending_mode == "returned-status" else "pending-exc"),
        script=script,
    )
    state_markers = [
        marker for marker in markers if marker.startswith("pending-state:")
    ]
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }

    assert completed.returncode == durable._FAILFAST_PENDING_RENAME, json.dumps(
        evidence, sort_keys=True
    )
    assert markers.count("native-enter") == 1
    assert markers.count("pending-failfast") == 1
    assert len(state_markers) == 1
    state = json.loads(state_markers[0].split(":", 1)[1])
    assert state == {
        "failfast_started": True,
        "iosb_status_u32": durable._STATUS_PENDING,
        "native_iosb_live": True,
        "native_name_live": True,
        "native_storage_live": True,
        "owner_handle_live": True,
        "owner_handle_state": durable.DurableHandleState.OPEN.value,
        "owner_path_state": durable.DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN.value,
        "parent_pin_live": True,
    }
    assert not any(marker.startswith("forbidden-") for marker in markers)
    assert len(candidates) == 1
    assert candidates[0]["name"] != "pending-must-not-return.bin"
    assert candidates[0]["size"] == len(b"pending-live-resources")
    assert candidates[0]["sha256"] == hashlib.sha256(
        b"pending-live-resources"
    ).hexdigest()


@pytest.mark.parametrize("replace", [False, True], ids=["no-replace", "replace"])
def test_native_abi_rename_storage_has_exact_counted_payload_without_terminator_dependency(
    replace: bool,
) -> None:
    name_utf16le = "缓冲 区😀.bin".encode("utf-16-le", "strict")
    storage, iosb, allocation_length = durable._build_native_rename_storage(
        name_utf16le=name_utf16le,
        replace=replace,
    )
    head_type = durable._FILE_RENAME_INFORMATION_HEAD
    head = head_type.from_buffer(storage)
    file_name_offset = head_type.FileName.offset
    payload_address = ctypes.addressof(storage) + file_name_offset

    assert allocation_length == ctypes.sizeof(head_type) + len(name_utf16le)
    assert ctypes.sizeof(storage) == allocation_length
    assert head.ReplaceIfExists == int(replace)
    assert head.RootDirectory in (None, 0)
    assert head.FileNameLength == len(name_utf16le)
    assert ctypes.string_at(payload_address, len(name_utf16le)) == name_utf16le
    assert int(iosb.Status) & 0xFFFFFFFF == durable._STATUS_PENDING
    assert int(iosb.Information) == 0

    payload_end = file_name_offset + len(name_utf16le)
    assert payload_end < allocation_length
    ctypes.memset(
        ctypes.addressof(storage) + payload_end,
        0xA5,
        allocation_length - payload_end,
    )
    assert head.FileNameLength == len(name_utf16le)
    assert ctypes.string_at(payload_address, head.FileNameLength) == name_utf16le


def test_native_abi_function_signatures_are_exact() -> None:
    assert durable._NtSetInformationFile.argtypes == [
        durable.wintypes.HANDLE,
        ctypes.POINTER(durable._IO_STATUS_BLOCK),
        ctypes.c_void_p,
        durable.wintypes.ULONG,
        ctypes.c_int,
    ]
    assert durable._NtSetInformationFile.restype is durable._NTSTATUS
    assert durable._RtlNtStatusToDosError.argtypes == [durable._NTSTATUS]
    assert durable._RtlNtStatusToDosError.restype is durable.wintypes.ULONG


@pytest.mark.parametrize(
    "filename",
    [
        pytest.param("BMP-é-ß.bin", id="bmp"),
        pytest.param("中文 空格.bin", id="chinese-space"),
        pytest.param("emoji-😀.bin", id="emoji-surrogate-pair"),
    ],
)
def test_native_path_accepts_bmp_chinese_space_and_emoji(
    approved_parent: ApprovedPhysicalDirectory,
    filename: str,
) -> None:
    payload = filename.encode("utf-8", "strict")

    receipt = write_durable_bytes(
        parent=approved_parent,
        filename=filename,
        payload=payload,
        replace=False,
        max_bytes=128,
    )

    assert receipt.path.name == filename
    assert receipt.path.read_bytes() == payload
    assert _visible_files(approved_parent) == [approved_parent.path / filename]


@pytest.mark.parametrize(
    ("filename", "maximum_component_length", "accepted"),
    [
        pytest.param("a" * 8, 8, True, id="bmp-exact-boundary"),
        pytest.param("a" * 9, 8, False, id="bmp-one-over"),
        pytest.param("😀" * 4, 8, True, id="emoji-exact-eight-code-units"),
        pytest.param("😀" * 4 + "a", 8, False, id="emoji-one-code-unit-over"),
    ],
)
def test_native_path_enforces_volume_maximum_in_utf16_code_units_before_create(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    maximum_component_length: int,
    accepted: bool,
) -> None:
    real_create = durable.win32file.CreateFile
    create_calls: list[str] = []

    def bounded_volume(_path: str):
        return (
            "D",
            approved_parent.identity.volume_serial,
            maximum_component_length,
            0,
            "NTFS",
        )

    def track_create(path: str, *args: object, **kwargs: object):
        create_calls.append(path)
        return real_create(path, *args, **kwargs)

    monkeypatch.setattr(durable.win32api, "GetVolumeInformation", bounded_volume)
    monkeypatch.setattr(durable.win32file, "CreateFile", track_create)

    if accepted:
        receipt = write_durable_bytes(
            parent=approved_parent,
            filename=filename,
            payload=b"utf16-boundary",
            replace=False,
            max_bytes=64,
        )
        assert receipt.path.read_bytes() == b"utf16-boundary"
        assert len(create_calls) == 1
    else:
        with pytest.raises(ValueError, match="component limit"):
            write_durable_bytes(
                parent=approved_parent,
                filename=filename,
                payload=b"must-not-create",
                replace=False,
                max_bytes=64,
            )
        assert create_calls == []
        assert _visible_files(approved_parent) == []


@pytest.mark.parametrize(
    "filename",
    [
        pytest.param("bad\ud800name.bin", id="isolated-surrogate"),
        pytest.param("slash/name.bin", id="slash"),
        pytest.param(r"backslash\name.bin", id="backslash"),
        pytest.param("nul\x00name.bin", id="nul"),
        pytest.param("ads:stream", id="ads"),
        pytest.param(".", id="dot"),
        pytest.param("..", id="dot-dot"),
        pytest.param("trailing.", id="trailing-dot"),
        pytest.param("trailing ", id="trailing-space"),
        pytest.param("CON", id="device-con"),
        pytest.param("NUL.txt", id="device-nul"),
        pytest.param("COM1", id="device-com"),
        pytest.param("LPT9.log", id="device-lpt"),
    ],
)
def test_native_path_rejects_ambiguous_or_unencodable_basename_before_volume_or_create(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    volume_calls = 0
    create_calls = 0

    def forbidden_volume(*_args: object, **_kwargs: object):
        nonlocal volume_calls
        volume_calls += 1
        raise AssertionError("invalid basename must fail before volume query")

    def forbidden_create(*_args: object, **_kwargs: object):
        nonlocal create_calls
        create_calls += 1
        raise AssertionError("invalid basename must fail before CreateFile")

    monkeypatch.setattr(durable.win32api, "GetVolumeInformation", forbidden_volume)
    monkeypatch.setattr(durable.win32file, "CreateFile", forbidden_create)

    with pytest.raises(ValueError):
        write_durable_bytes(
            parent=approved_parent,
            filename=filename,
            payload=b"must-not-create",
            replace=False,
            max_bytes=64,
        )

    assert volume_calls == 0
    assert create_calls == 0
    assert _visible_files(approved_parent) == []


def test_native_path_rename_is_independent_of_process_cwd(
    approved_parent: ApprovedPhysicalDirectory,
) -> None:
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    cwd_case = TEST_TMP_ROOT / f"cwd-{os.getpid()}-{os.urandom(6).hex()}"
    cwd_case.mkdir()
    original_cwd = Path.cwd()
    filename = "cwd隔离-😀.bin"
    cwd_files: list[Path] = []
    receipt: DurableWriteReceipt | None = None
    try:
        os.chdir(cwd_case)
        receipt = write_durable_bytes(
            parent=approved_parent,
            filename=filename,
            payload=b"cwd-independent",
            replace=False,
            max_bytes=64,
        )
        cwd_files = list(cwd_case.iterdir())
    finally:
        os.chdir(original_cwd)
        for child in list(cwd_case.iterdir()):
            child.unlink()
        cwd_case.rmdir()

    assert receipt is not None
    assert receipt.path == Path(approved_parent.stable_volume_path) / filename
    assert receipt.path.read_bytes() == b"cwd-independent"
    assert cwd_files == []
    assert _visible_files(approved_parent) == [approved_parent.path / filename]


@pytest.mark.parametrize(
    "gate_case",
    [
        "query-exception",
        "not-a-tuple",
        "short-tuple",
        "serial-mismatch",
        "non-ntfs",
        "max-non-int",
        "max-zero",
        "max-negative",
        "max-over-ulong",
    ],
)
def test_native_volume_gate_rejects_invalid_query_before_create_with_typed_state(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    gate_case: str,
) -> None:
    volume_calls: list[str] = []
    create_calls = 0
    serial = approved_parent.identity.volume_serial

    def invalid_volume(path: str):
        volume_calls.append(path)
        if gate_case == "query-exception":
            raise OSError("volume query failed")
        if gate_case == "not-a-tuple":
            return ["D", serial, 255, 0, "NTFS"]
        if gate_case == "short-tuple":
            return ("D", serial, 255, 0)
        if gate_case == "serial-mismatch":
            return ("D", serial + 1, 255, 0, "NTFS")
        if gate_case == "non-ntfs":
            return ("D", serial, 255, 0, "ReFS")
        invalid_maximums: dict[str, object] = {
            "max-non-int": "255",
            "max-zero": 0,
            "max-negative": -1,
            "max-over-ulong": durable._ULONG_MAX + 1,
        }
        return ("D", serial, invalid_maximums[gate_case], 0, "NTFS")

    def forbidden_create(*_args: object, **_kwargs: object):
        nonlocal create_calls
        create_calls += 1
        raise AssertionError("failed volume gate must not call CreateFile")

    monkeypatch.setattr(durable.win32api, "GetVolumeInformation", invalid_volume)
    monkeypatch.setattr(durable.win32file, "CreateFile", forbidden_create)

    filename = f"volume-{gate_case}.bin"
    with pytest.raises(DurableWriteError) as captured:
        write_durable_bytes(
            parent=approved_parent,
            filename=filename,
            payload=b"must-not-create",
            replace=False,
            max_bytes=64,
        )

    error = captured.value
    assert error.stage is durable.DurableWriteStage.VOLUME_GATE
    assert error.target == Path(approved_parent.stable_volume_path) / filename
    assert error.evidence_owner is None
    assert error.path_state is None
    assert error.handle_state is None
    assert volume_calls == [approved_parent.identity.volume_guid]
    assert create_calls == 0
    assert _visible_files(approved_parent) == []


@pytest.mark.parametrize(
    "detach_mode",
    ["zero", "wrong-raw", "raises", "result-registration-gap"],
)
def test_detach_ambiguous_failfasts_without_close(detach_mode: str) -> None:
    script = r"""
        import json
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        mode = __DETACH_MODE__
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        detach_calls = 0
        frozen_raw = None

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        real_close_owner = durable._close_owner
        real_failfast = durable._failfast_detach_identity

        class RegistrationGap:
            def __int__(self):
                record("detach-result-int-gap")
                raise KeyboardInterrupt("Detach result registration interrupted")

        class AmbiguousDetachHandle:
            def __init__(self, actual):
                self.actual = actual
                self.raw = int(actual)

            def __int__(self):
                return self.raw

            def Detach(self):
                global detach_calls
                detach_calls += 1
                record("detach-call")
                if mode == "zero":
                    return 0
                if mode == "wrong-raw":
                    return self.raw + 1
                if mode == "raises":
                    raise OSError("Detach raised before a trustworthy result")
                return RegistrationGap()

        def inject_ambiguous_detach(owner):
            global frozen_raw
            frozen_raw = owner.raw_handle
            owner.handle = AmbiguousDetachHandle(owner.handle)
            assert owner.handle.raw == frozen_raw
            return real_close_owner(owner)

        def forbidden_close(raw):
            record(f"forbidden-close:{int(raw)}")
            os._exit(99)

        def observe_then_failfast(owner):
            record(
                "detach-failfast-state:"
                + json.dumps(
                    {
                        "detach_calls": detach_calls,
                        "failfast_started": owner.failfast_started,
                        "frozen_raw": frozen_raw,
                        "handle_state": owner.handle_state.value,
                        "owner_raw": owner.raw_handle,
                        "owner_handle_retained": owner.handle is not None,
                        "path_state": owner.path_state.value,
                        "raw_matches_frozen": owner.raw_handle == frozen_raw,
                        "raw_handle_nonzero": bool(owner.raw_handle),
                    },
                    sort_keys=True,
                )
            )
            real_failfast(owner)
            record("forbidden-failfast-return")

        durable._close_owner = inject_ambiguous_detach
        durable.win32api.CloseHandle = forbidden_close
        durable._failfast_detach_identity = observe_then_failfast
        parent = approve_physical_d_write_root(case)
        durable.write_durable_bytes(
            parent=parent,
            filename="detach-ambiguous.bin",
            payload=b"detach-ambiguous",
            replace=False,
            max_bytes=64,
        )
        record("forbidden-write-return")
    """.replace("__DETACH_MODE__", repr(detach_mode))

    short_label = {
        "zero": "d-zero",
        "wrong-raw": "d-wrong",
        "raises": "d-raise",
        "result-registration-gap": "d-gap",
    }[detach_mode]
    completed, markers, candidates = _run_native_boundary_child(
        label=short_label,
        script=script,
    )
    state_markers = [
        marker for marker in markers if marker.startswith("detach-failfast-state:")
    ]
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }

    assert completed.returncode == durable._FAILFAST_DETACH_IDENTITY, json.dumps(
        evidence, sort_keys=True
    )
    assert markers.count("detach-call") == 1
    assert len(state_markers) == 1
    state = json.loads(state_markers[0].split(":", 1)[1])
    assert state == {
        "detach_calls": 1,
        "failfast_started": True,
        "frozen_raw": state["frozen_raw"],
        "handle_state": durable.DurableHandleState.CLOSE_IN_FLIGHT.value,
        "owner_raw": state["frozen_raw"],
        "owner_handle_retained": True,
        "path_state": durable.DurablePathState.TARGET_CONFIRMED.value,
        "raw_matches_frozen": True,
        "raw_handle_nonzero": True,
    }
    assert isinstance(state["frozen_raw"], int) and state["frozen_raw"] != 0
    assert markers.count("detach-result-int-gap") == int(
        detach_mode == "result-registration-gap"
    )
    assert not any(marker.startswith("forbidden-") for marker in markers)
    assert not any(marker.startswith("forbidden-close:") for marker in markers)
    assert len(candidates) == 1
    assert candidates[0]["name"] == "detach-ambiguous.bin"
    assert candidates[0]["sha256"] == hashlib.sha256(
        b"detach-ambiguous"
    ).hexdigest()


@pytest.mark.parametrize(
    "close_result",
    [
        pytest.param(None, id="none-is-success"),
        pytest.param(False, id="false-is-unknown"),
        pytest.param(0, id="zero-is-unknown"),
        pytest.param(True, id="true-is-unknown"),
        pytest.param(1, id="one-is-unknown"),
    ],
)
def test_closehandle_accepts_only_exact_none(
    monkeypatch: pytest.MonkeyPatch,
    close_result: object,
) -> None:
    registry: dict[str, object] = {}
    monkeypatch.setattr(durable, "_CLOSE_UNKNOWN_REGISTRY", registry)
    raw_handle = 0x5100 + len(registry)
    handle = _FakeAdoptedHandle(raw_handle=raw_handle, detached_handle=raw_handle)
    owner = durable._DurableOwner(
        temp_path=TEST_TMP_ROOT / "close-result.tmp",
        target_path=TEST_TMP_ROOT / "close-result.bin",
        sha256="0" * 64,
        byte_count=0,
        path_state=durable.DurablePathState.TARGET_CONFIRMED,
        handle_state=durable.DurableHandleState.OPEN,
        handle=handle,
        raw_handle=raw_handle,
    )
    close_calls: list[int] = []

    def close_with_exact_result(raw: int):
        close_calls.append(int(raw))
        return close_result

    monkeypatch.setattr(durable.win32api, "CloseHandle", close_with_exact_result)

    close_error = durable._close_owner(owner)

    assert handle.detach_calls == 1
    assert close_calls == [raw_handle]
    assert owner.handle is None
    assert owner.raw_handle == raw_handle
    if close_result is None:
        assert close_error is None
        assert owner.handle_state is durable.DurableHandleState.CLOSED
        assert registry == {}
    else:
        assert isinstance(close_error, DurableWriteError)
        assert close_error.stage is durable.DurableWriteStage.CLOSE
        assert close_error.path_state is durable.DurablePathState.TARGET_CONFIRMED
        assert close_error.handle_state is durable.DurableHandleState.CLOSE_UNKNOWN
        assert isinstance(close_error.evidence_owner, durable.CloseUnknownToken)
        token = close_error.evidence_owner
        assert token.path_state is durable.DurablePathState.TARGET_CONFIRMED
        assert token.handle_state is durable.DurableHandleState.CLOSE_UNKNOWN
        assert owner.handle_state is durable.DurableHandleState.CLOSE_UNKNOWN
        assert owner.path_state is durable.DurablePathState.TARGET_CONFIRMED
        assert len(registry) == 1
        assert token.value in registry
        entry = registry[token.value]
        assert entry.token is token
        assert entry.owner is owner
        assert entry.owner.handle is None
        assert entry.owner.raw_handle == raw_handle
        assert entry.owner.path_state is durable.DurablePathState.TARGET_CONFIRMED
        assert entry.owner.handle_state is durable.DurableHandleState.CLOSE_UNKNOWN
        assert entry.raw_handle == raw_handle
        assert isinstance(close_error.cause, RuntimeError)
        assert entry.cause is close_error.cause


@pytest.mark.parametrize(
    ("operation_error_type", "close_error_type", "expected_order"),
    [
        (
            KeyboardInterrupt,
            OSError,
            ("typed:write_temp", "raw:KeyboardInterrupt", "typed:close"),
        ),
        (
            SystemExit,
            OSError,
            ("typed:write_temp", "raw:SystemExit", "typed:close"),
        ),
        (
            OSError,
            KeyboardInterrupt,
            ("typed:write_temp", "typed:close", "raw:KeyboardInterrupt"),
        ),
        (
            OSError,
            SystemExit,
            ("typed:write_temp", "typed:close", "raw:SystemExit"),
        ),
    ],
    ids=[
        "operation-keyboardinterrupt-close-oserror",
        "operation-systemexit-close-oserror",
        "operation-oserror-close-keyboardinterrupt",
        "operation-oserror-close-systemexit",
    ],
)
def test_operation_and_close_baseexceptions_are_grouped_with_direct_order(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    operation_error_type: type[BaseException],
    close_error_type: type[BaseException],
    expected_order: tuple[str, ...],
) -> None:
    registry: dict[str, object] = {}
    monkeypatch.setattr(durable, "_CLOSE_UNKNOWN_REGISTRY", registry)
    real_close = durable.win32api.CloseHandle
    close_calls: list[int] = []
    operation_cause = operation_error_type("operation interrupted")
    close_cause = close_error_type("close result registration interrupted")

    def fail_operation(*_args: object, **_kwargs: object):
        raise operation_cause

    def close_then_interrupt(raw_handle: int):
        close_calls.append(int(raw_handle))
        real_close(raw_handle)
        raise close_cause

    monkeypatch.setattr(durable.win32file, "WriteFile", fail_operation)
    monkeypatch.setattr(durable.win32api, "CloseHandle", close_then_interrupt)

    with pytest.raises(BaseExceptionGroup) as captured:
        write_durable_bytes(
            parent=approved_parent,
            filename=(
                f"group-{operation_error_type.__name__}-{close_error_type.__name__}.bin"
            ),
            payload=b"group-order",
            replace=False,
            max_bytes=64,
        )

    group = captured.value
    descriptors: list[str] = []
    for member in group.exceptions:
        if type(member) is DurableWriteError:
            descriptors.append(f"typed:{member.stage.value}")
        else:
            descriptors.append(f"raw:{type(member).__name__}")

    raw_type = (
        operation_error_type
        if not issubclass(operation_error_type, Exception)
        else close_error_type
    )
    raw_cause = (
        operation_cause
        if not isinstance(operation_cause, Exception)
        else close_cause
    )
    typed_errors = [
        member
        for member in group.exceptions
        if type(member) is DurableWriteError
    ]
    assert len(typed_errors) == 2
    operation_error = next(
        error
        for error in typed_errors
        if error.stage is durable.DurableWriteStage.WRITE_TEMP
    )
    close_error = next(
        error
        for error in typed_errors
        if error.stage is durable.DurableWriteStage.CLOSE
    )
    assert type(group) is BaseExceptionGroup
    assert tuple(descriptors) == expected_order
    assert sum(type(member) is raw_type for member in group.exceptions) == 1
    assert sum(member is raw_cause for member in group.exceptions) == 1
    assert operation_error.cause is operation_cause
    assert close_error.cause is close_cause
    assert operation_error.path_state is durable.DurablePathState.TEMP_CONFIRMED
    assert operation_error.handle_state is durable.DurableHandleState.CLOSE_UNKNOWN
    assert close_error.path_state is durable.DurablePathState.TEMP_CONFIRMED
    assert close_error.handle_state is durable.DurableHandleState.CLOSE_UNKNOWN
    assert isinstance(operation_error.evidence_owner, durable.DurableEvidenceOwner)
    assert (
        operation_error.evidence_owner.path_state
        is durable.DurablePathState.TEMP_CONFIRMED
    )
    assert (
        operation_error.evidence_owner.handle_state
        is durable.DurableHandleState.CLOSE_UNKNOWN
    )
    assert isinstance(close_error.evidence_owner, durable.CloseUnknownToken)
    token = close_error.evidence_owner
    assert token.path_state is durable.DurablePathState.TEMP_CONFIRMED
    assert token.handle_state is durable.DurableHandleState.CLOSE_UNKNOWN
    assert token.value in registry
    entry = registry[token.value]
    assert entry.token is token
    assert entry.owner.path_state is durable.DurablePathState.TEMP_CONFIRMED
    assert entry.owner.handle_state is durable.DurableHandleState.CLOSE_UNKNOWN
    assert entry.owner.handle is None
    assert entry.owner.raw_handle == close_calls[0]
    assert entry.raw_handle == close_calls[0]
    assert entry.cause is close_cause
    assert len(close_calls) == 1


@pytest.mark.parametrize(
    "quarantine_stage",
    [
        "before-lock",
        "after-lock",
        "token-before-insert",
        "after-insert",
        "before-state",
    ],
)
def test_quarantine_stage_failure_is_nonreturning_failfast(
    quarantine_stage: str,
) -> None:
    script = r"""
        import json
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        stage = __QUARANTINE_STAGE__
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        real_close = durable.win32api.CloseHandle
        real_failfast = durable._failfast_close_quarantine
        durable._CLOSE_UNKNOWN_REGISTRY = {}
        detach_raw = None
        close_raw = None

        def track_detach(_owner, raw):
            global detach_raw
            detach_raw = int(raw)
            record(f"detach:{detach_raw}")

        def close_then_unknown(raw):
            global close_raw
            close_raw = int(raw)
            record(f"close:{close_raw}")
            real_close(raw)
            raise OSError("CloseHandle result unknown")

        def inject(_owner):
            record(f"inject:{stage}")
            raise KeyboardInterrupt(f"quarantine interrupted at {stage}")

        class ExplodingRegistry(dict):
            def __setitem__(self, key, value):
                assert key == value.token.value
                assert value.owner.raw_handle == value.raw_handle
                record("inject:token-before-insert")
                raise KeyboardInterrupt("registry insertion interrupted")

        if stage == "before-lock":
            durable._before_close_quarantine_lock = inject
        elif stage == "after-lock":
            durable._after_close_quarantine_lock = inject
        elif stage == "token-before-insert":
            durable._CLOSE_UNKNOWN_REGISTRY = ExplodingRegistry()
        elif stage == "after-insert":
            durable._after_close_quarantine_insert = inject
        else:
            durable._before_close_quarantine_state_commit = inject

        def observe_then_failfast(owner):
            items = list(durable._CLOSE_UNKNOWN_REGISTRY.items())
            key, entry = items[0] if items else (None, None)
            record(
                "quarantine-failfast-state:"
                + json.dumps(
                    {
                        "close_raw": close_raw,
                        "detach_close_owner_same": (
                            detach_raw == close_raw == owner.raw_handle
                        ),
                        "detach_raw": detach_raw,
                        "entry_raw_same": bool(
                            entry and entry.raw_handle == owner.raw_handle
                        ),
                        "failfast_started": owner.failfast_started,
                        "handle_is_none": owner.handle is None,
                        "handle_state": owner.handle_state.value,
                        "owner_raw": owner.raw_handle,
                        "path_state": owner.path_state.value,
                        "raw_handle_nonzero": bool(owner.raw_handle),
                        "registry_owner_same": bool(entry and entry.owner is owner),
                        "registry_raw_same": bool(
                            entry and entry.raw_handle == owner.raw_handle
                        ),
                        "registry_key_matches_token": bool(
                            entry and key == entry.token.value
                        ),
                        "registry_size": len(items),
                    },
                    sort_keys=True,
                )
            )
            real_failfast(owner)
            record("forbidden-failfast-return")

        durable._after_detach_identity_before_close = track_detach
        durable.win32api.CloseHandle = close_then_unknown
        durable._failfast_close_quarantine = observe_then_failfast
        parent = approve_physical_d_write_root(case)
        durable.write_durable_bytes(
            parent=parent,
            filename=f"quarantine-{stage}.bin",
            payload=b"quarantine-stage",
            replace=False,
            max_bytes=64,
        )
        record("forbidden-write-return")
    """.replace("__QUARANTINE_STAGE__", repr(quarantine_stage))

    label = {
        "before-lock": "q-before",
        "after-lock": "q-afterlock",
        "token-before-insert": "q-token",
        "after-insert": "q-insert",
        "before-state": "q-state",
    }[quarantine_stage]
    completed, markers, candidates = _run_native_boundary_child(
        label=label,
        script=script,
    )
    state_markers = [
        marker
        for marker in markers
        if marker.startswith("quarantine-failfast-state:")
    ]
    expected_inserted = quarantine_stage in {"after-insert", "before-state"}
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }

    assert completed.returncode == durable._FAILFAST_CLOSE_QUARANTINE, json.dumps(
        evidence, sort_keys=True
    )
    assert len([marker for marker in markers if marker.startswith("detach:")]) == 1
    assert len([marker for marker in markers if marker.startswith("close:")]) == 1
    assert markers.count(f"inject:{quarantine_stage}") == 1
    assert len(state_markers) == 1
    state = json.loads(state_markers[0].split(":", 1)[1])
    assert state["detach_raw"] == state["close_raw"] == state["owner_raw"]
    assert isinstance(state["owner_raw"], int) and state["owner_raw"] != 0
    assert state == {
        "close_raw": state["owner_raw"],
        "detach_close_owner_same": True,
        "detach_raw": state["owner_raw"],
        "entry_raw_same": expected_inserted,
        "failfast_started": True,
        "handle_is_none": True,
        "handle_state": durable.DurableHandleState.CLOSE_IN_FLIGHT.value,
        "owner_raw": state["owner_raw"],
        "path_state": durable.DurablePathState.TARGET_CONFIRMED.value,
        "raw_handle_nonzero": True,
        "registry_owner_same": expected_inserted,
        "registry_raw_same": expected_inserted,
        "registry_key_matches_token": expected_inserted,
        "registry_size": int(expected_inserted),
    }
    assert not any(marker.startswith("forbidden-") for marker in markers)
    assert len(candidates) == 1
    assert candidates[0]["name"] == f"quarantine-{quarantine_stage}.bin"


def test_close_unknown_error_gc_does_not_close_again(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry: dict[str, object] = {}
    monkeypatch.setattr(durable, "_CLOSE_UNKNOWN_REGISTRY", registry)
    real_close = durable.win32api.CloseHandle
    close_calls: list[int] = []
    close_results: list[object] = []
    close_markers: list[str] = []
    close_cause = OSError("close result unknown")

    def close_then_unknown(raw_handle: int):
        close_calls.append(int(raw_handle))
        result = real_close(raw_handle)
        close_results.append(result)
        close_markers.append("real-close-returned-none")
        raise close_cause

    monkeypatch.setattr(durable.win32api, "CloseHandle", close_then_unknown)

    def produce_nonowning_token() -> durable.CloseUnknownToken:
        try:
            write_durable_bytes(
                parent=approved_parent,
                filename="close-unknown-gc.bin",
                payload=b"gc-must-not-reclose",
                replace=False,
                max_bytes=64,
            )
        except DurableWriteError as error:
            assert isinstance(error.evidence_owner, durable.CloseUnknownToken)
            return error.evidence_owner
        raise AssertionError("close-unknown path returned a receipt")

    token = produce_nonowning_token()
    assert token.value in registry
    assert len(close_calls) == 1
    assert close_results == [None]
    assert close_markers == ["real-close-returned-none"]
    entry = registry[token.value]
    assert entry.token is token
    assert entry.owner.handle is None
    assert entry.owner.raw_handle == close_calls[0]
    assert entry.owner.path_state is durable.DurablePathState.TARGET_CONFIRMED
    assert entry.owner.handle_state is durable.DurableHandleState.CLOSE_UNKNOWN
    assert entry.raw_handle == close_calls[0]
    assert entry.cause is close_cause

    for _ in range(3):
        gc.collect()

    assert token.value in registry
    assert len(close_calls) == 1
    assert close_results == [None]
    assert close_markers == ["real-close-returned-none"]
    assert (approved_parent.path / "close-unknown-gc.bin").read_bytes() == (
        b"gc-must-not-reclose"
    )


def test_direct_close_owner_detach_clears_destructor_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry: dict[str, object] = {}
    monkeypatch.setattr(durable, "_CLOSE_UNKNOWN_REGISTRY", registry)
    explicit_close_calls: list[int] = []
    owning_destructor_closes: list[int] = []
    destructor_states: list[bool] = []
    detach_calls: list[int] = []
    raw_handle = 0x6D51

    class DestructorProbeHandle:
        def __init__(self, raw: int):
            self.raw = raw
            self.owns = True

        def __int__(self) -> int:
            return self.raw

        def Detach(self) -> int:
            detach_calls.append(self.raw)
            assert self.owns
            self.owns = False
            return self.raw

        def __del__(self) -> None:
            destructor_states.append(self.owns)
            if self.owns:
                owning_destructor_closes.append(self.raw)

    def explicit_close_unknown(raw: int) -> bool:
        explicit_close_calls.append(int(raw))
        return False

    monkeypatch.setattr(durable.win32api, "CloseHandle", explicit_close_unknown)
    handle = DestructorProbeHandle(raw_handle)
    owner = durable._DurableOwner(
        temp_path=TEST_TMP_ROOT / "destructor-probe.tmp",
        target_path=TEST_TMP_ROOT / "destructor-probe.bin",
        sha256="1" * 64,
        byte_count=0,
        path_state=durable.DurablePathState.TARGET_CONFIRMED,
        handle_state=durable.DurableHandleState.OPEN,
        handle=handle,
        raw_handle=raw_handle,
    )

    close_error = durable._close_owner(owner)
    assert isinstance(close_error, DurableWriteError)
    assert isinstance(close_error.evidence_owner, durable.CloseUnknownToken)
    token_value = close_error.evidence_owner.value
    entry = registry[token_value]
    assert entry.owner is owner
    assert entry.owner.handle is None
    assert entry.raw_handle == raw_handle
    assert detach_calls == [raw_handle]
    assert explicit_close_calls == [raw_handle]

    del close_error
    del entry
    del owner
    del handle
    for _ in range(3):
        gc.collect()

    assert token_value in registry
    assert destructor_states == [False]
    assert owning_destructor_closes == []
    assert explicit_close_calls == [raw_handle]


def test_close_unknown_registry_33rd_entry_is_nonreturning_failfast() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="registry33",
        script=r"""
        import json
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        detach_count = 0
        close_count = 0
        detach_raws = []
        close_raws = []

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        real_close = durable.win32api.CloseHandle
        real_failfast = durable._failfast_close_quarantine
        durable._CLOSE_UNKNOWN_REGISTRY = {}

        def track_detach(_owner, raw):
            global detach_count
            detach_count += 1
            detach_raws.append(int(raw))
            record(f"detach:{detach_count}:{detach_raws[-1]}")

        def close_then_unknown(raw):
            global close_count
            close_count += 1
            close_raws.append(int(raw))
            record(f"close:{close_count}:{close_raws[-1]}")
            real_close(raw)
            raise OSError("CloseHandle result unknown")

        def observe_then_failfast(owner):
            record(
                "registry-failfast-state:"
                + json.dumps(
                    {
                        "close_count": close_count,
                        "detach_count": detach_count,
                        "detach_raw": detach_raws[-1],
                        "failfast_started": owner.failfast_started,
                        "handle_is_none": owner.handle is None,
                        "handle_state": owner.handle_state.value,
                        "owner_raw": owner.raw_handle,
                        "path_state": owner.path_state.value,
                        "raw_triplet_same": (
                            detach_raws[-1] == close_raws[-1] == owner.raw_handle
                        ),
                        "registry_size": len(durable._CLOSE_UNKNOWN_REGISTRY),
                        "close_raw": close_raws[-1],
                    },
                    sort_keys=True,
                )
            )
            real_failfast(owner)
            record("forbidden-failfast-return")

        durable._after_detach_identity_before_close = track_detach
        durable.win32api.CloseHandle = close_then_unknown
        durable._failfast_close_quarantine = observe_then_failfast
        parent = approve_physical_d_write_root(case)

        for index in range(33):
            ordinal = index + 1
            try:
                durable.write_durable_bytes(
                    parent=parent,
                    filename=f"registry-{ordinal:02d}.bin",
                    payload=f"registry-{ordinal:02d}".encode("ascii"),
                    replace=False,
                    max_bytes=64,
                )
            except durable.DurableWriteError as error:
                assert isinstance(error.evidence_owner, durable.CloseUnknownToken)
                assert len(durable._CLOSE_UNKNOWN_REGISTRY) == ordinal
                token = error.evidence_owner
                assert token.value in durable._CLOSE_UNKNOWN_REGISTRY
                entry = durable._CLOSE_UNKNOWN_REGISTRY[token.value]
                assert entry.token is token
                assert entry.token.value == token.value
                assert entry.owner.path_state is durable.DurablePathState.TARGET_CONFIRMED
                assert entry.owner.handle_state is durable.DurableHandleState.CLOSE_UNKNOWN
                assert entry.owner.handle is None
                assert (
                    entry.raw_handle
                    == entry.owner.raw_handle
                    == detach_raws[-1]
                    == close_raws[-1]
                )
                record(f"verified:{ordinal}:{entry.raw_handle}")
                record(f"returned:{ordinal}")
            else:
                record(f"forbidden-receipt:{ordinal}")

        record("forbidden-returned-33")
        """,
    )
    detach_markers = [marker for marker in markers if marker.startswith("detach:")]
    close_markers = [marker for marker in markers if marker.startswith("close:")]
    returned_markers = [marker for marker in markers if marker.startswith("returned:")]
    verified_markers = [marker for marker in markers if marker.startswith("verified:")]
    state_markers = [
        marker for marker in markers if marker.startswith("registry-failfast-state:")
    ]
    evidence = {
        "returncode": completed.returncode,
        "markers_tail": markers[-20:],
        "candidate_count": len(candidates),
        "stderr_tail": completed.stderr[-1000:],
    }

    assert completed.returncode == durable._FAILFAST_CLOSE_QUARANTINE, json.dumps(
        evidence, sort_keys=True
    )
    assert len(detach_markers) == 33
    assert len(close_markers) == 33
    assert close_markers[-1].startswith("close:33:")
    assert returned_markers == [f"returned:{ordinal}" for ordinal in range(1, 33)]
    assert len(verified_markers) == 32
    assert [int(marker.split(":", 2)[1]) for marker in verified_markers] == list(
        range(1, 33)
    )
    assert len(state_markers) == 1
    state = json.loads(state_markers[0].split(":", 1)[1])
    assert state == {
        "close_count": 33,
        "close_raw": state["owner_raw"],
        "detach_count": 33,
        "detach_raw": state["owner_raw"],
        "failfast_started": True,
        "handle_is_none": True,
        "handle_state": durable.DurableHandleState.CLOSE_IN_FLIGHT.value,
        "owner_raw": state["owner_raw"],
        "path_state": durable.DurablePathState.TARGET_CONFIRMED.value,
        "raw_triplet_same": True,
        "registry_size": durable._CLOSE_UNKNOWN_REGISTRY_CAPACITY,
    }
    assert isinstance(state["owner_raw"], int) and state["owner_raw"] != 0
    assert not any(marker.startswith("forbidden-") for marker in markers)
    assert len(candidates) == 33


def _install_real_native_rename_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, list[object]]:
    real_native = durable._NtSetInformationFile
    real_rtl = durable._RtlNtStatusToDosError
    real_close_owner = durable._close_owner
    observed: dict[str, list[object]] = {
        "native": [],
        "rtl": [],
        "preclose": [],
        "postclose": [],
    }

    def observe_native(
        handle: object,
        iosb: object,
        buffer: object,
        length: int,
        information_class: int,
    ) -> int:
        source_snapshot = durable._snapshot_handle(handle)
        source_path = durable.win32file.GetFinalPathNameByHandle(
            handle, durable._VOLUME_NAME_GUID
        )
        head = ctypes.cast(
            buffer, ctypes.POINTER(durable._FILE_RENAME_INFORMATION_HEAD)
        ).contents
        returned = int(real_native(handle, iosb, buffer, length, information_class))
        observed["native"].append(
            {
                "class": int(information_class),
                "file_name_length": int(head.FileNameLength),
                "length": int(length),
                "replace": int(head.ReplaceIfExists),
                "source_identity": source_snapshot.identity,
                "source_path": str(source_path),
                "status": durable._signed_ntstatus(returned),
            }
        )
        return returned

    def observe_rtl(status: int) -> int:
        mapped = int(real_rtl(status))
        observed["rtl"].append((int(status), mapped))
        return mapped

    def observe_close(owner: object):
        observed["preclose"].append((owner.path_state, owner.handle_state))
        close_error = real_close_owner(owner)
        observed["postclose"].append((owner.path_state, owner.handle_state))
        return close_error

    monkeypatch.setattr(durable, "_NtSetInformationFile", observe_native)
    monkeypatch.setattr(durable, "_RtlNtStatusToDosError", observe_rtl)
    monkeypatch.setattr(durable, "_close_owner", observe_close)
    return observed


def _identity_allow_directory(path: Path) -> tuple[int, int]:
    import win32con
    import win32file

    flags = win32file.FILE_FLAG_OPEN_REPARSE_POINT
    if path.is_dir():
        flags |= win32file.FILE_FLAG_BACKUP_SEMANTICS
    handle = win32file.CreateFile(
        str(path),
        durable._FILE_READ_ATTRIBUTES,
        win32file.FILE_SHARE_READ
        | win32file.FILE_SHARE_WRITE
        | win32file.FILE_SHARE_DELETE,
        None,
        win32con.OPEN_EXISTING,
        flags,
        None,
    )
    try:
        information = win32file.GetFileInformationByHandle(handle)
        return int(information[4]), (int(information[8]) << 32) | int(
            information[9]
        )
    finally:
        handle.Close()


def _target_content_digest(path: Path) -> str:
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256()
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        digest.update(child.name.encode("utf-8", "strict"))
        digest.update(b"\x00")
        digest.update(child.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def test_native_ntfs_no_clobber_case_equivalent_collision_preserves_both_identities(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_name = "CaseTarget.BIN"
    requested_name = "casetarget.bin"
    old_payload = b"case-sensitive-spelling-must-survive"
    new_payload = b"no-clobber-source-evidence"
    original_target = approved_parent.path / original_name
    original_target.write_bytes(old_payload)
    old_identity = _identity(original_target)
    old_hash = hashlib.sha256(old_payload).hexdigest()
    observed = _install_real_native_rename_observer(monkeypatch)

    with pytest.raises(DurableWriteError) as captured:
        write_durable_bytes(
            parent=approved_parent,
            filename=requested_name,
            payload=new_payload,
            replace=False,
            max_bytes=128,
        )

    error = captured.value
    assert len(observed["native"]) == 1
    native = cast(dict[str, object], observed["native"][0])
    source_identity = cast(durable.DurableFileIdentity, native["source_identity"])
    assert native["class"] == durable._FILE_RENAME_INFORMATION_CLASS
    assert native["replace"] == 0
    assert cast(int, native["status"]) & 0xFFFFFFFF == 0xC0000035
    assert observed["rtl"] == [(native["status"], 183)]
    assert observed["preclose"] == [
        (
            durable.DurablePathState.TEMP_CONFIRMED,
            durable.DurableHandleState.OPEN,
        )
    ]
    assert observed["postclose"] == [
        (
            durable.DurablePathState.TEMP_CONFIRMED,
            durable.DurableHandleState.CLOSED,
        )
    ]

    assert error.stage is durable.DurableWriteStage.NATIVE_RENAME
    assert error.path_state is durable.DurablePathState.TEMP_CONFIRMED
    assert error.handle_state is durable.DurableHandleState.CLOSED
    assert isinstance(error.cause, durable.NativeRenameStatusError)
    assert error.cause.u32_status == 0xC0000035
    assert error.cause.mapped_winerror == 183
    assert error.temp_owner is not None
    assert error.temp_owner.identity == source_identity
    assert _identity(error.temp_owner.path) == (
        source_identity.volume_serial,
        source_identity.file_index,
    )
    assert error.temp_owner.sha256 == hashlib.sha256(new_payload).hexdigest()
    assert error.temp_owner.path.read_bytes() == new_payload

    assert original_target.name == original_name
    assert original_target.read_bytes() == old_payload
    assert _identity(original_target) == old_identity
    assert hashlib.sha256(original_target.read_bytes()).hexdigest() == old_hash
    visible_names = {path.name for path in _visible_files(approved_parent)}
    assert original_name in visible_names
    assert requested_name not in visible_names
    assert error.temp_owner.path.name in visible_names


@pytest.mark.parametrize(
    "target_state",
    ["missing", "ordinary-existing"],
    ids=["replace-missing", "replace-ordinary-existing"],
)
def test_native_ntfs_replace_success_preserves_source_identity(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    target_state: str,
) -> None:
    filename = f"replace-success-{target_state}.bin"
    target = approved_parent.path / filename
    old_identity: tuple[int, int] | None = None
    old_hash: str | None = None
    if target_state == "ordinary-existing":
        old_payload = b"ordinary-old-target"
        target.write_bytes(old_payload)
        old_identity = _identity(target)
        old_hash = hashlib.sha256(old_payload).hexdigest()

    new_payload = f"new-source-{target_state}".encode("ascii")
    observed = _install_real_native_rename_observer(monkeypatch)

    receipt = write_durable_bytes(
        parent=approved_parent,
        filename=filename,
        payload=new_payload,
        replace=True,
        max_bytes=128,
    )

    assert len(observed["native"]) == 1
    native = cast(dict[str, object], observed["native"][0])
    source_identity = cast(durable.DurableFileIdentity, native["source_identity"])
    source_path = Path(cast(str, native["source_path"]))
    assert native["class"] == durable._FILE_RENAME_INFORMATION_CLASS
    assert native["replace"] == 1
    assert native["status"] == durable._STATUS_SUCCESS
    assert observed["rtl"] == []
    assert observed["preclose"] == [
        (
            durable.DurablePathState.TARGET_CONFIRMED,
            durable.DurableHandleState.OPEN,
        )
    ]
    assert observed["postclose"] == [
        (
            durable.DurablePathState.TARGET_CONFIRMED,
            durable.DurableHandleState.CLOSED,
        )
    ]

    final_identity = _identity(receipt.path)
    assert final_identity == (
        source_identity.volume_serial,
        source_identity.file_index,
    )
    assert receipt.path.read_bytes() == new_payload
    assert receipt.sha256 == hashlib.sha256(new_payload).hexdigest()
    assert not source_path.exists()
    assert _visible_files(approved_parent) == [target]
    if old_identity is not None:
        assert final_identity != old_identity
        assert hashlib.sha256(receipt.path.read_bytes()).hexdigest() != old_hash


@pytest.mark.parametrize(
    "failure_target",
    ["directory", "read-only", "incompatible-open-handle"],
    ids=["target-directory", "target-read-only", "target-open-without-delete-share"],
)
def test_native_ntfs_replace_failure_preserves_target_and_temp_evidence(
    approved_parent: ApprovedPhysicalDirectory,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
) -> None:
    import win32con
    import win32file

    filename = f"replace-failure-{failure_target}.bin"
    target = approved_parent.path / filename
    directory_child: Path | None = None
    original_attributes: int | None = None
    live_target_handle: object | None = None
    old_payload = f"protected-{failure_target}".encode("ascii")
    new_payload = f"replacement-{failure_target}".encode("ascii")

    try:
        if failure_target == "directory":
            target.mkdir()
            directory_child = target / "keep.txt"
            directory_child.write_bytes(old_payload)
        else:
            target.write_bytes(old_payload)

        if failure_target == "read-only":
            original_attributes = int(
                durable.win32api.GetFileAttributes(str(target))
            )
            durable.win32api.SetFileAttributes(
                str(target), original_attributes | win32con.FILE_ATTRIBUTE_READONLY
            )
        elif failure_target == "incompatible-open-handle":
            live_target_handle = win32file.CreateFile(
                str(target),
                win32con.GENERIC_READ,
                win32file.FILE_SHARE_READ,
                None,
                win32con.OPEN_EXISTING,
                win32con.FILE_ATTRIBUTE_NORMAL,
                None,
            )

        before_is_directory = target.is_dir()
        before_attributes = int(durable.win32api.GetFileAttributes(str(target)))
        before_identity = _identity_allow_directory(target)
        before_digest = _target_content_digest(target)
        observed = _install_real_native_rename_observer(monkeypatch)

        with pytest.raises(DurableWriteError) as captured:
            write_durable_bytes(
                parent=approved_parent,
                filename=filename,
                payload=new_payload,
                replace=True,
                max_bytes=128,
            )

        error = captured.value
        assert len(observed["native"]) == 1
        native = cast(dict[str, object], observed["native"][0])
        signed_status = cast(int, native["status"])
        source_identity = cast(
            durable.DurableFileIdentity, native["source_identity"]
        )
        assert signed_status < 0
        assert native["class"] == durable._FILE_RENAME_INFORMATION_CLASS
        assert native["replace"] == 1
        assert len(observed["rtl"]) == 1
        mapped_input, mapped_result = cast(
            tuple[int, int], observed["rtl"][0]
        )
        assert mapped_input == signed_status
        assert observed["preclose"] == [
            (
                durable.DurablePathState.TEMP_CONFIRMED,
                durable.DurableHandleState.OPEN,
            )
        ]
        assert observed["postclose"] == [
            (
                durable.DurablePathState.TEMP_CONFIRMED,
                durable.DurableHandleState.CLOSED,
            )
        ]

        assert error.stage is durable.DurableWriteStage.NATIVE_RENAME
        assert error.path_state is durable.DurablePathState.TEMP_CONFIRMED
        assert error.handle_state is durable.DurableHandleState.CLOSED
        assert isinstance(error.cause, durable.NativeRenameStatusError)
        assert error.cause.signed_status == signed_status
        assert error.cause.u32_status == signed_status & 0xFFFFFFFF
        assert error.cause.mapped_winerror == mapped_result
        assert error.temp_owner is not None
        assert error.temp_owner.identity == source_identity
        assert _identity(error.temp_owner.path) == (
            source_identity.volume_serial,
            source_identity.file_index,
        )
        assert error.temp_owner.sha256 == hashlib.sha256(new_payload).hexdigest()
        assert error.temp_owner.path.read_bytes() == new_payload

        assert target.is_dir() is before_is_directory
        assert int(durable.win32api.GetFileAttributes(str(target))) == before_attributes
        assert _identity_allow_directory(target) == before_identity
        assert _target_content_digest(target) == before_digest
    finally:
        if live_target_handle is not None:
            live_target_handle.Close()
        if original_attributes is not None and target.exists():
            durable.win32api.SetFileAttributes(str(target), original_attributes)
        if directory_child is not None and directory_child.exists():
            directory_child.unlink()
        if target.exists() and target.is_dir():
            target.rmdir()


# Phase F final-review lifecycle/native RED. These tests deliberately exercise
# asynchronous BaseException windows that ordinary monkeypatch failures cannot
# place between two adjacent Python statements.


def test_native_final_review_create_hook_error_then_handler_interrupt_exits_71() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="final-create-handler",
        script=r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        real_close = durable.win32api.CloseHandle
        real_failfast = durable._failfast_unadopted_create

        def first_hook_failure(handle):
            global returned_raw
            returned_raw = int(handle)
            record(f"create-hook-oserror:{int(handle)}")
            raise OSError("phase-f create hook failure")

        def interrupt_failfast_call(_owner):
            record("create-handler-keyboardinterrupt")
            raise KeyboardInterrupt("phase-f create handler interruption")

        def forbidden_close(raw):
            record(f"forbidden-close:{int(raw)}")
            return real_close(raw)

        def observe_then_failfast(owner):
            assert returned_raw is not None
            durable.win32api.GetHandleInformation(returned_raw)
            record(
                "create-failfast-live:"
                + owner.path_state.value
                + ":"
                + owner.handle_state.value
            )
            real_failfast(owner)

        durable._after_create_return_before_attach = first_hook_failure
        durable._before_unadopted_create_failfast_call = interrupt_failfast_call
        durable.win32api.CloseHandle = forbidden_close
        durable._failfast_unadopted_create = observe_then_failfast
        parent = approve_physical_d_write_root(case)
        durable.write_durable_bytes(
            parent=parent,
            filename="create-double-interrupt.bin",
            payload=b"must-not-write",
            replace=False,
            max_bytes=64,
        )
        record("forbidden-receipt")
        """,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == durable._FAILFAST_UNADOPTED_CREATE, json.dumps(
        evidence, sort_keys=True
    )
    assert len([item for item in markers if item.startswith("create-hook-oserror:")]) == 1
    assert markers.count("create-handler-keyboardinterrupt") == 1
    assert markers.count("create-failfast-live:create_unknown:create_in_flight") == 1
    assert not any(item.startswith("forbidden-") for item in markers)


def test_native_final_review_unadopted_failfast_call_interrupt_still_exits_71() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="final-create-failfast-call",
        script=r"""
        import os
        from pathlib import Path

        import win32con
        import win32file
        from app.core import windows_durable_file as durable

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        temp_path = case / "manual-unadopted.tmp"

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        handle = win32file.CreateFile(
            str(temp_path),
            win32con.GENERIC_READ | win32con.GENERIC_WRITE,
            win32file.FILE_SHARE_READ,
            None,
            win32con.CREATE_NEW,
            win32con.FILE_ATTRIBUTE_NORMAL,
            None,
        )
        raw = int(handle)
        owner = durable._DurableOwner(
            temp_path=temp_path,
            target_path=case / "manual-target.bin",
            sha256="0" * 64,
            byte_count=0,
            handle=handle,
            raw_handle=raw,
            create_phase=durable._CreatePhase.RETURNED_UNCOMMITTED,
        )
        lifecycle = durable._DurableWriteLifecycle(current_owner=owner)
        real_close = durable.win32api.CloseHandle
        real_failfast = durable._failfast_unadopted_create

        def interrupt_call(_owner):
            record("unadopted-failfast-call-secondary")
            raise KeyboardInterrupt("phase-f unadopted failfast call interruption")

        def forbidden_close(candidate):
            record(f"forbidden-close:{int(candidate)}")
            return real_close(candidate)

        def observe_then_failfast(current_owner):
            durable.win32api.GetHandleInformation(raw)
            record(
                "unadopted-failfast-live:"
                + current_owner.create_phase.value
                + ":"
                + current_owner.handle_state.value
            )
            real_failfast(current_owner)

        durable._before_unadopted_create_failfast_call = interrupt_call
        durable.win32api.CloseHandle = forbidden_close
        durable._failfast_unadopted_create = observe_then_failfast
        with lifecycle:
            pass
        record("forbidden-lifecycle-return")
        """,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == durable._FAILFAST_UNADOPTED_CREATE, json.dumps(
        evidence, sort_keys=True
    )
    assert markers.count("unadopted-failfast-call-secondary") == 1
    assert markers.count("unadopted-failfast-live:returned_uncommitted:create_in_flight") == 1
    assert not any(item.startswith("forbidden-") for item in markers)


def test_native_final_review_adopted_owner_before_operation_try_is_explicitly_closed() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="final-adopted-gap",
        script=r"""
        import gc
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        adopted_raw = None
        adopted_raw = None
        primary = KeyboardInterrupt("phase-f adopted owner gap")
        explicit_close_calls = []

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        real_commit = durable.DurableHandleAdoptionGuard.commit
        real_close = durable.win32api.CloseHandle

        def track_commit(self):
            global adopted_raw
            owner = real_commit(self)
            adopted_raw = owner.raw_handle
            record(f"adopted:{adopted_raw}")
            return owner

        def track_explicit_close(raw):
            raw = int(raw)
            if raw == adopted_raw:
                explicit_close_calls.append(raw)
                record(f"explicit-close:{raw}")
            return real_close(raw)

        def interrupt_after_adoption(owner):
            assert owner.raw_handle == adopted_raw
            assert owner.handle_state is durable.DurableHandleState.OPEN
            record("adopted-operation-gap")
            raise primary

        durable.DurableHandleAdoptionGuard.commit = track_commit
        durable.win32api.CloseHandle = track_explicit_close
        durable._after_owner_adopted_before_operations = interrupt_after_adoption
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="adopted-gap.bin",
                payload=b"not-written",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            operation_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.CREATE_TEMP
            ]
            assert len(operation_errors) == 1
            assert operation_errors[0].cause is primary
            assert operation_errors[0].path_state is durable.DurablePathState.TEMP_CONFIRMED
            assert operation_errors[0].handle_state is durable.DurableHandleState.CLOSED
            assert sum(item is primary for item in direct) == 1
            record("adopted-typed-direct")
            record("caught:" + type(error).__name__)
        else:
            record("forbidden-receipt")

        gc.collect()
        record(f"explicit-close-count:{len(explicit_close_calls)}")
        try:
            durable.win32api.GetHandleInformation(adopted_raw)
        except durable.pywintypes.error as error:
            record(f"raw-closed-winerror:{error.winerror}")
        else:
            record("raw-still-live")
        """,
    )
    explicit = [item for item in markers if item.startswith("explicit-close:")]
    adopted = [item for item in markers if item.startswith("adopted:")]
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert len(adopted) == len(explicit) == 1, evidence
    assert adopted[0].partition(":")[2] == explicit[0].partition(":")[2]
    assert markers.count("adopted-operation-gap") == 1
    assert markers.count("adopted-typed-direct") == 1
    assert markers.count("caught:BaseExceptionGroup") == 1
    assert markers.count("explicit-close-count:1") == 1
    assert markers.count("raw-closed-winerror:6") == 1
    assert "raw-still-live" not in markers
    assert "forbidden-receipt" not in markers


def test_native_final_review_lifecycle_exit_entry_interrupt_still_closes_and_preserves_causes() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="final-lifecycle-entry",
        script=r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        adopted_raw = None
        explicit_close_calls = []
        primary = OSError("phase-f lifecycle entry primary")
        secondary = KeyboardInterrupt("phase-f lifecycle entry secondary")

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def walk(error):
            found = []
            pending = [error]
            seen = set()
            while pending:
                current = pending.pop()
                if id(current) in seen:
                    continue
                seen.add(id(current))
                found.append(current)
                if isinstance(current, BaseExceptionGroup):
                    pending.extend(current.exceptions)
                for name in ("cause", "__cause__", "__context__"):
                    nested = getattr(current, name, None)
                    if isinstance(nested, BaseException):
                        pending.append(nested)
            return found

        real_commit = durable.DurableHandleAdoptionGuard.commit
        real_close = durable.win32api.CloseHandle

        def track_commit(self):
            global adopted_raw
            owner = real_commit(self)
            adopted_raw = owner.raw_handle
            return owner

        def fail_operation(*_args, **_kwargs):
            raise primary

        def interrupt_entry(owner, received_primary):
            assert owner.raw_handle == adopted_raw
            assert owner.handle_state is durable.DurableHandleState.OPEN
            assert received_primary is primary
            record("lifecycle-entry-secondary")
            raise secondary

        def track_close(raw):
            raw = int(raw)
            if raw == adopted_raw:
                explicit_close_calls.append(raw)
                record(f"explicit-close:{raw}")
            return real_close(raw)

        durable.DurableHandleAdoptionGuard.commit = track_commit
        durable._write_exact = fail_operation
        durable._after_lifecycle_exit_entry = interrupt_entry
        durable.win32api.CloseHandle = track_close
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="lifecycle-entry.bin",
                payload=b"lifecycle-entry",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            graph = walk(error)
            record("primary-preserved:" + str(any(item is primary for item in graph)).lower())
            record("secondary-preserved:" + str(any(item is secondary for item in graph)).lower())
        else:
            record("forbidden-receipt")
        record(f"explicit-close-count:{len(explicit_close_calls)}")
        """,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("lifecycle-entry-secondary") == 1
    assert markers.count("primary-preserved:true") == 1
    assert markers.count("secondary-preserved:true") == 1
    assert len([item for item in markers if item.startswith("explicit-close:")]) == 1
    assert markers.count("explicit-close-count:1") == 1
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    "barrier_window",
    ["exit-finalize-call", "finalize-force-call", "force-finally-owner-read"],
)
def test_native_final_review_nested_lifecycle_barriers_close_exactly_once(
    barrier_window: str,
) -> None:
    script = r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        mode = __BARRIER_WINDOW__
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        adopted_raw = None
        explicit_close_calls = []
        close_owner_calls = 0
        primary = OSError("phase-f nested lifecycle primary")
        secondary = KeyboardInterrupt("phase-f nested lifecycle secondary")

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def walk(error):
            found = []
            pending = [error]
            seen = set()
            while pending:
                current = pending.pop()
                if id(current) in seen:
                    continue
                seen.add(id(current))
                found.append(current)
                if isinstance(current, BaseExceptionGroup):
                    pending.extend(current.exceptions)
                for name in ("cause", "__cause__", "__context__"):
                    nested = getattr(current, name, None)
                    if isinstance(nested, BaseException):
                        pending.append(nested)
            return found

        real_commit = durable.DurableHandleAdoptionGuard.commit
        real_close = durable.win32api.CloseHandle
        real_close_owner = durable._close_owner

        def track_commit(self):
            global adopted_raw
            owner = real_commit(self)
            adopted_raw = owner.raw_handle
            return owner

        def fail_operation(*_args, **_kwargs):
            raise primary

        def staged_close_owner(owner):
            global close_owner_calls
            close_owner_calls += 1
            fail_count = {
                "exit-finalize-call": 0,
                "finalize-force-call": 1,
                "force-finally-owner-read": 2,
            }[mode]
            if close_owner_calls <= fail_count:
                record(f"preclose-boundary:{close_owner_calls}")
                raise OSError(f"phase-f staged preclose {close_owner_calls}")
            return real_close_owner(owner)

        def track_close(raw):
            raw = int(raw)
            if raw == adopted_raw:
                explicit_close_calls.append(raw)
                record(f"explicit-close:{raw}")
            return real_close(raw)

        def interrupt_exit_finalize(_lifecycle):
            if mode == "exit-finalize-call":
                record("barrier-secondary:" + mode)
                raise secondary

        def interrupt_finalize_force(_lifecycle):
            if mode == "finalize-force-call":
                record("barrier-secondary:" + mode)
                raise secondary

        def interrupt_force_owner_read(_lifecycle):
            if mode == "force-finally-owner-read":
                record("barrier-secondary:" + mode)
                raise secondary

        durable.DurableHandleAdoptionGuard.commit = track_commit
        durable._write_exact = fail_operation
        durable._close_owner = staged_close_owner
        durable.win32api.CloseHandle = track_close
        durable._before_lifecycle_finalize_call = interrupt_exit_finalize
        durable._before_lifecycle_force_call = interrupt_finalize_force
        durable._before_lifecycle_terminal_owner_read = interrupt_force_owner_read
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="nested-" + mode + ".bin",
                payload=b"nested-lifecycle",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            graph = walk(error)
            record("primary-preserved:" + str(any(item is primary for item in graph)).lower())
            record("secondary-preserved:" + str(any(item is secondary for item in graph)).lower())
        else:
            record("forbidden-receipt")
        record(f"explicit-close-count:{len(explicit_close_calls)}")
        """.replace("__BARRIER_WINDOW__", repr(barrier_window))
    completed, markers, candidates = _run_native_boundary_child(
        label="nested-" + barrier_window,
        script=script,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("barrier-secondary:" + barrier_window) == 1
    assert markers.count("primary-preserved:true") == 1
    assert markers.count("secondary-preserved:true") == 1
    assert len([item for item in markers if item.startswith("explicit-close:")]) == 1
    assert markers.count("explicit-close-count:1") == 1
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    "interrupt_window",
    ["operation-handler", "operation-before-close", "success-before-close"],
)
def test_native_final_review_write_handler_to_close_boundary_preserves_causes_and_closes(
    interrupt_window: str,
) -> None:
    script = r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        mode = __INTERRUPT_WINDOW__
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        adopted_raw = None
        explicit_close_calls = []
        primary = OSError("phase-f primary operation cause")
        secondary = KeyboardInterrupt("phase-f secondary close-boundary interrupt")

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def walk(error):
            found = []
            pending = [error]
            seen = set()
            while pending:
                current = pending.pop()
                if id(current) in seen:
                    continue
                seen.add(id(current))
                found.append(current)
                if isinstance(current, BaseExceptionGroup):
                    pending.extend(current.exceptions)
                for name in ("cause", "reconcile_cause", "__cause__", "__context__"):
                    nested = getattr(current, name, None)
                    if isinstance(nested, BaseException):
                        pending.append(nested)
            return found

        real_commit = durable.DurableHandleAdoptionGuard.commit
        real_close = durable.win32api.CloseHandle

        def track_commit(self):
            global adopted_raw
            owner = real_commit(self)
            adopted_raw = owner.raw_handle
            record(f"adopted:{adopted_raw}")
            return owner

        def track_close(raw):
            raw = int(raw)
            if raw == adopted_raw:
                explicit_close_calls.append(raw)
                record(f"explicit-close:{raw}")
            return real_close(raw)

        def fail_operation(*_args, **_kwargs):
            record("primary-operation-raised")
            raise primary

        def interrupt_exit_entry(owner, received_primary):
            assert owner.handle_state is durable.DurableHandleState.OPEN
            if mode == "operation-handler":
                assert received_primary is primary
                record("secondary-injected:" + mode)
                raise secondary

        def interrupt_before_close(owner, received_primary):
            assert owner.handle_state is durable.DurableHandleState.OPEN
            inject = (
                mode == "operation-before-close" and received_primary is primary
            ) or (
                mode == "success-before-close" and received_primary is None
            )
            if inject:
                record("secondary-injected:" + mode)
                raise secondary

        durable.DurableHandleAdoptionGuard.commit = track_commit
        durable.win32api.CloseHandle = track_close
        if mode != "success-before-close":
            durable._write_exact = fail_operation
        durable._after_lifecycle_exit_entry = interrupt_exit_entry
        durable._before_lifecycle_owner_close = interrupt_before_close
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="handler-close-" + mode + ".bin",
                payload=b"phase-f-handler-close",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            graph = walk(error)
            record("caught:" + type(error).__name__)
            record("secondary-preserved:" + str(any(item is secondary for item in graph)).lower())
            record("primary-preserved:" + str(any(item is primary for item in graph)).lower())
        else:
            record("forbidden-receipt")
        record(f"explicit-close-count:{len(explicit_close_calls)}")
        """.replace("__INTERRUPT_WINDOW__", repr(interrupt_window))
    label = {
        "operation-handler": "f-op-handler",
        "operation-before-close": "f-op-close",
        "success-before-close": "f-ok-close",
    }[interrupt_window]
    completed, markers, candidates = _run_native_boundary_child(
        label=label,
        script=script,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count(f"secondary-injected:{interrupt_window}") == 1
    assert markers.count("secondary-preserved:true") == 1
    if interrupt_window.startswith("operation-"):
        assert markers.count("primary-operation-raised") == 1
        assert markers.count("primary-preserved:true") == 1
    assert len([item for item in markers if item.startswith("explicit-close:")]) == 1
    assert markers.count("explicit-close-count:1") == 1
    assert "forbidden-receipt" not in markers


def test_native_final_review_native_exception_then_handler_interrupt_exits_72() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="final-native-handler",
        script=r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        real_commit = durable.DurableHandleAdoptionGuard.commit
        real_close = durable.win32api.CloseHandle
        real_failfast = durable._failfast_pending_native_rename

        def track_commit(self):
            global adopted_raw
            owner = real_commit(self)
            adopted_raw = owner.raw_handle
            return owner

        def native_oserror(*_args):
            record("native-wrapper-oserror-with-pending-iosb")
            raise OSError("phase-f native wrapper failure")

        def interrupt_pending_settle(_owner, _returned_status):
            record("native-handler-keyboardinterrupt")
            raise KeyboardInterrupt("phase-f native handler interruption")

        def forbidden_close(raw):
            record(f"forbidden-close:{int(raw)}")
            return real_close(raw)

        def observe_then_failfast(owner):
            assert adopted_raw is not None
            durable.win32api.GetHandleInformation(adopted_raw)
            record(
                "pending-failfast-live:"
                + str(int(owner.native_iosb.Status) & 0xFFFFFFFF)
                + ":"
                + owner.path_state.value
            )
            real_failfast(owner)

        durable.DurableHandleAdoptionGuard.commit = track_commit
        durable._NtSetInformationFile = native_oserror
        durable._before_native_pending_settle_call = interrupt_pending_settle
        durable.win32api.CloseHandle = forbidden_close
        durable._failfast_pending_native_rename = observe_then_failfast
        parent = approve_physical_d_write_root(case)
        durable.write_durable_bytes(
            parent=parent,
            filename="native-double-interrupt.bin",
            payload=b"native-double-interrupt",
            replace=False,
            max_bytes=64,
        )
        record("forbidden-receipt")
        """,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == durable._FAILFAST_PENDING_RENAME, json.dumps(
        evidence, sort_keys=True
    )
    assert markers.count("native-wrapper-oserror-with-pending-iosb") == 1
    assert markers.count("native-handler-keyboardinterrupt") == 1
    assert markers.count(
        "pending-failfast-live:259:rename_in_flight_or_unknown"
    ) == 1
    assert not any(item.startswith("forbidden-") for item in markers)


def test_native_final_review_pending_decision_interrupt_exits_72_before_close_or_reconcile() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="final-pending-decision",
        script=r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        real_close = durable.win32api.CloseHandle
        real_failfast = durable._failfast_pending_native_rename

        def native_error_with_pending_iosb(*_args):
            record("native-error-pending-iosb")
            raise OSError("phase-f native call interrupted")

        def interrupt_pending_decision(owner, returned_status, iosb):
            assert returned_status is durable._NO_NATIVE_RETURN
            assert (int(iosb.Status) & 0xFFFFFFFF) == durable._STATUS_PENDING
            assert owner.handle_state is durable.DurableHandleState.OPEN
            record("pending-decision-secondary")
            raise KeyboardInterrupt("phase-f pending decision interruption")

        def forbidden_close(raw):
            record(f"forbidden-close:{int(raw)}")
            return real_close(raw)

        def forbidden_reconcile(_owner):
            record("forbidden-reconcile")
            return None

        def observe_then_failfast(owner):
            durable.win32api.GetHandleInformation(owner.raw_handle)
            record(
                "pending-decision-failfast:"
                + owner.native_pending_decision.value
                + ":"
                + owner.handle_state.value
                + ":live"
            )
            real_failfast(owner)

        durable._NtSetInformationFile = native_error_with_pending_iosb
        durable._before_native_pending_decision = interrupt_pending_decision
        durable.win32api.CloseHandle = forbidden_close
        durable._try_reconcile_owner_location = forbidden_reconcile
        durable._failfast_pending_native_rename = observe_then_failfast
        parent = approve_physical_d_write_root(case)
        durable.write_durable_bytes(
            parent=parent,
            filename="pending-decision.bin",
            payload=b"pending-decision",
            replace=False,
            max_bytes=64,
        )
        record("forbidden-receipt")
        """,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == durable._FAILFAST_PENDING_RENAME, json.dumps(
        evidence, sort_keys=True
    )
    assert markers.count("native-error-pending-iosb") == 1
    assert markers.count("pending-decision-secondary") == 1
    assert markers.count("pending-decision-failfast:unknown:open:live") == 1
    assert not any(item.startswith("forbidden-") for item in markers)


def test_native_final_review_pending_settle_call_interrupt_exits_72_before_close() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="final-pending-settle-call",
        script=r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        real_close = durable.win32api.CloseHandle
        real_failfast = durable._failfast_pending_native_rename

        def native_error(*_args):
            record("native-error-before-settle")
            raise OSError("phase-f native call interrupted")

        def interrupt_settle_call(owner, returned_status):
            assert returned_status is durable._NO_NATIVE_RETURN
            assert owner.native_pending_decision is durable._NativePendingDecision.UNKNOWN
            record("pending-settle-call-secondary")
            raise KeyboardInterrupt("phase-f settle call entry interruption")

        def forbidden_close(raw):
            record(f"forbidden-close:{int(raw)}")
            return real_close(raw)

        def forbidden_reconcile(_owner):
            record("forbidden-reconcile")
            return None

        def observe_then_failfast(owner):
            durable.win32api.GetHandleInformation(owner.raw_handle)
            record(
                "pending-settle-failfast:"
                + owner.native_pending_decision.value
                + ":"
                + owner.handle_state.value
                + ":live"
            )
            real_failfast(owner)

        durable._NtSetInformationFile = native_error
        durable._before_native_pending_settle_call = interrupt_settle_call
        durable.win32api.CloseHandle = forbidden_close
        durable._try_reconcile_owner_location = forbidden_reconcile
        durable._failfast_pending_native_rename = observe_then_failfast
        parent = approve_physical_d_write_root(case)
        durable.write_durable_bytes(
            parent=parent,
            filename="pending-settle-call.bin",
            payload=b"pending-settle-call",
            replace=False,
            max_bytes=64,
        )
        record("forbidden-receipt")
        """,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == durable._FAILFAST_PENDING_RENAME, json.dumps(
        evidence, sort_keys=True
    )
    assert markers.count("native-error-before-settle") == 1
    assert markers.count("pending-settle-call-secondary") == 1
    assert markers.count("pending-settle-failfast:unknown:open:live") == 1
    assert not any(item.startswith("forbidden-") for item in markers)


def test_native_final_review_exact_success_with_stale_pending_iosb_reconciles_same_handle() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="final-stale-iosb",
        script=r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        adopted_raw = None
        close_calls = []

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def walk(error):
            found = []
            pending = [error]
            seen = set()
            while pending:
                current = pending.pop()
                if id(current) in seen:
                    continue
                seen.add(id(current))
                found.append(current)
                if isinstance(current, BaseExceptionGroup):
                    pending.extend(current.exceptions)
                for name in ("cause", "reconcile_cause", "__cause__", "__context__"):
                    nested = getattr(current, name, None)
                    if isinstance(nested, BaseException):
                        pending.append(nested)
            return found

        real_commit = durable.DurableHandleAdoptionGuard.commit
        real_reconcile = durable._try_reconcile_owner_location
        real_close = durable.win32api.CloseHandle

        def track_commit(self):
            global adopted_raw
            owner = real_commit(self)
            adopted_raw = owner.raw_handle
            record(f"adopted:{adopted_raw}")
            return owner

        def exact_success_without_iosb_commit(_handle, iosb, *_args):
            current = int(iosb._obj.Status) & 0xFFFFFFFF
            record(f"native-success-stale-iosb:{current}")
            assert current == durable._STATUS_PENDING
            return durable._STATUS_SUCCESS

        def interrupt_hook(owner, status):
            assert status == durable._STATUS_SUCCESS
            assert (int(owner.native_iosb.Status) & 0xFFFFFFFF) == durable._STATUS_PENDING
            record(f"success-hook-interrupt:{owner.raw_handle}")
            raise KeyboardInterrupt("phase-f success hook interruption")

        def forbidden_pending_failfast(_owner):
            record("forbidden-pending-failfast")
            os._exit(92)

        def track_reconcile(owner):
            assert owner.handle is not None
            assert owner.raw_handle == int(owner.handle) == adopted_raw
            record(f"same-handle-reconcile:{owner.raw_handle}")
            return real_reconcile(owner)

        def track_close(raw):
            raw = int(raw)
            if raw == adopted_raw:
                close_calls.append(raw)
                record(f"explicit-close:{raw}")
            return real_close(raw)

        durable.DurableHandleAdoptionGuard.commit = track_commit
        durable._NtSetInformationFile = exact_success_without_iosb_commit
        durable._after_native_return_before_status_commit = interrupt_hook
        durable._failfast_pending_native_rename = forbidden_pending_failfast
        durable._try_reconcile_owner_location = track_reconcile
        durable.win32api.CloseHandle = track_close
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="stale-iosb.bin",
                payload=b"stale-iosb",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            graph = walk(error)
            typed = [
                item
                for item in graph
                if isinstance(item, durable.DurableWriteError)
                and item.stage is durable.DurableWriteStage.NATIVE_RENAME
            ]
            assert len(typed) == 1
            assert any(
                isinstance(item, KeyboardInterrupt)
                and str(item) == "phase-f success hook interruption"
                for item in graph
            )
            assert typed[0].path_state is durable.DurablePathState.TEMP_CONFIRMED
            assert typed[0].handle_state is durable.DurableHandleState.CLOSED
            assert typed[0].temp_owner is not None
            record("stale-iosb-verified")
        else:
            record("forbidden-receipt")
        assert close_calls == [adopted_raw]
        """,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert len([item for item in markers if item.startswith("native-success-stale-iosb:")]) == 1
    assert len([item for item in markers if item.startswith("success-hook-interrupt:")]) == 1
    assert len([item for item in markers if item.startswith("same-handle-reconcile:")]) == 1
    assert len([item for item in markers if item.startswith("explicit-close:")]) == 1
    assert markers.count("stale-iosb-verified") == 1
    assert "forbidden-pending-failfast" not in markers
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize("close_interrupt_window", ["handler-entry", "hook-to-quarantine"])
def test_native_final_review_close_unknown_interrupt_windows_exit_74(
    close_interrupt_window: str,
) -> None:
    script = r"""
        import inspect
        import os
        import sys
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        mode = __CLOSE_INTERRUPT_WINDOW__
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        adopted_raw = None

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        real_commit = durable.DurableHandleAdoptionGuard.commit
        real_close = durable.win32api.CloseHandle
        real_failfast = durable._failfast_close_quarantine

        def track_commit(self):
            global adopted_raw
            owner = real_commit(self)
            adopted_raw = owner.raw_handle
            return owner

        def close_then_unknown(raw):
            raw = int(raw)
            assert raw == adopted_raw
            record(f"close-oserror-after-real-close:{raw}")
            real_close(raw)
            raise OSError("phase-f CloseHandle result unknown")

        def hook_returns(_owner, raw, cause):
            assert int(raw) == adopted_raw
            assert isinstance(cause, OSError)
            record("close-exception-hook-returned")

        def interrupt_handler(_owner, raw, cause):
            if mode == "handler-entry":
                assert int(raw) == adopted_raw
                assert isinstance(cause, OSError)
                record("close-secondary-interrupt:" + mode)
                raise KeyboardInterrupt("phase-f close handler interruption")

        def interrupt_quarantine(_owner, raw, cause):
            if mode == "hook-to-quarantine":
                assert int(raw) == adopted_raw
                assert isinstance(cause, OSError)
                record("close-secondary-interrupt:" + mode)
                raise KeyboardInterrupt("phase-f close quarantine interruption")

        def observe_then_failfast(owner):
            record(
                "close-failfast:"
                + owner.handle_state.value
                + ":"
                + str(len(durable._CLOSE_UNKNOWN_REGISTRY))
            )
            real_failfast(owner)

        durable.DurableHandleAdoptionGuard.commit = track_commit
        durable.win32api.CloseHandle = close_then_unknown
        durable._after_close_exception_before_quarantine = hook_returns
        durable._before_close_cause_capture = interrupt_handler
        durable._before_close_quarantine_handoff = interrupt_quarantine
        durable._failfast_close_quarantine = observe_then_failfast
        parent = approve_physical_d_write_root(case)
        durable.write_durable_bytes(
            parent=parent,
            filename="close-window-" + mode + ".bin",
            payload=b"close-window",
            replace=False,
            max_bytes=64,
        )
        record("forbidden-receipt")
        """.replace("__CLOSE_INTERRUPT_WINDOW__", repr(close_interrupt_window))
    label = {
        "handler-entry": "f-close-handler",
        "hook-to-quarantine": "f-close-quarantine",
    }[close_interrupt_window]
    completed, markers, candidates = _run_native_boundary_child(
        label=label,
        script=script,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == durable._FAILFAST_CLOSE_QUARANTINE, json.dumps(
        evidence, sort_keys=True
    )
    assert len(
        [item for item in markers if item.startswith("close-oserror-after-real-close:")]
    ) == 1
    assert markers.count(f"close-secondary-interrupt:{close_interrupt_window}") == 1
    if close_interrupt_window == "hook-to-quarantine":
        assert markers.count("close-exception-hook-returned") == 1
    assert markers.count("close-failfast:close_in_flight:0") == 1
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    ("callback_name", "expected_exit"),
    [
        ("_failfast_unadopted_create", 71),
        ("_failfast_pending_native_rename", 72),
        ("_failfast_detach_identity", 73),
        ("_failfast_close_quarantine", 74),
    ],
)
@pytest.mark.parametrize("interrupt_window", ["before-flag", "before-callback"])
def test_native_final_review_begin_failfast_callback_boundary_keeps_fixed_exit(
    callback_name: str,
    expected_exit: int,
    interrupt_window: str,
) -> None:
    script = r"""
        import inspect
        import os
        import sys
        from pathlib import Path

        from app.core import windows_durable_file as durable

        callback_name = __CALLBACK_NAME__
        expected_exit = __EXPECTED_EXIT__
        interrupt_window = __INTERRUPT_WINDOW__
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        callback = getattr(durable, callback_name)
        owner = durable._DurableOwner(
            temp_path=case / "temp.bin",
            target_path=case / "target.bin",
            sha256="0" * 64,
            byte_count=0,
        )

        def interrupt_before_flag(current_owner, exit_code):
            if interrupt_window == "before-flag":
                assert exit_code == expected_exit
                record(
                    "begin-failfast-interrupt:"
                    + callback_name
                    + ":"
                    + interrupt_window
                    + ":"
                    + str(current_owner.failfast_started).lower()
                )
                raise KeyboardInterrupt("phase-f failfast callback boundary")

        def interrupt_before_callback(current_owner, current_callback, exit_code):
            if interrupt_window == "before-callback":
                assert current_callback is callback
                assert exit_code == expected_exit
                record(
                    "begin-failfast-interrupt:"
                    + callback_name
                    + ":"
                    + interrupt_window
                    + ":"
                    + str(current_owner.failfast_started).lower()
                )
                raise KeyboardInterrupt("phase-f failfast callback boundary")

        durable._before_failfast_flag = interrupt_before_flag
        durable._before_failfast_callback = interrupt_before_callback
        durable._begin_failfast(owner, callback, expected_exit)
        record("forbidden-failfast-return")
        """
    script = (
        script.replace("__CALLBACK_NAME__", repr(callback_name))
        .replace("__EXPECTED_EXIT__", repr(expected_exit))
        .replace("__INTERRUPT_WINDOW__", repr(interrupt_window))
    )
    completed, markers, candidates = _run_native_boundary_child(
        label=f"f-begin-{expected_exit}-{interrupt_window}",
        script=script,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == expected_exit, json.dumps(evidence, sort_keys=True)
    expected_flag = "false" if interrupt_window == "before-flag" else "true"
    assert markers == [
        f"begin-failfast-interrupt:{callback_name}:{interrupt_window}:{expected_flag}"
    ]


def _native_final_review_exception_graph(error: BaseException) -> list[BaseException]:
    found: list[BaseException] = []
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        for name in ("cause", "reconcile_cause", "__cause__", "__context__"):
            nested = getattr(current, name, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return found


@pytest.mark.parametrize(
    ("status_name", "status_u32"),
    [
        pytest.param("final-failure", 0xC0000035, id="final-failure"),
        pytest.param("informational", 0x00000001, id="informational"),
    ],
)
def test_native_final_review_status_error_throw_boundary_preserves_raw_ntstatus(
    status_name: str,
    status_u32: int,
) -> None:
    script = r"""
        import ctypes
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        status_name = __STATUS_NAME__
        status_u32 = __STATUS_U32__
        signed_status = ctypes.c_int32(status_u32).value
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        prepared_status_error = None
        close_calls = []
        secondary = KeyboardInterrupt(
            "phase-f " + status_name + " status handoff interruption"
        )
        real_native_handoff = durable._native_sibling_rename
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def return_status(*_args):
            return signed_status

        def interrupt_handoff(owner, status_error):
            global prepared_status_error
            assert owner.committed_native_status_error is status_error
            prepared_status_error = status_error
            record("native-status-handoff-hook")
            raise secondary

        def assert_native_outward_group(**kwargs):
            try:
                return real_native_handoff(**kwargs)
            except BaseException as error:
                assert type(error) is BaseExceptionGroup
                direct = list(error.exceptions)
                assert sum(item is prepared_status_error for item in direct) == 1
                assert sum(item is secondary for item in direct) == 1
                assert prepared_status_error.signed_status == signed_status
                assert prepared_status_error.u32_status == status_u32
                record("native-status-direct-members")
                raise

        def track_close(raw):
            close_calls.append(int(raw))
            return real_close(raw)

        durable._NtSetInformationFile = return_status
        durable._before_native_status_handoff = interrupt_handoff
        durable._native_sibling_rename = assert_native_outward_group
        durable.win32api.CloseHandle = track_close
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="status-handoff-" + status_name + ".bin",
                payload=b"status-handoff",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            operation_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.NATIVE_RENAME
            ]
            assert len(operation_errors) == 1
            assert operation_errors[0].cause is not None
            assert sum(item is prepared_status_error for item in direct) == 1
            assert sum(item is secondary for item in direct) == 1
            record("lifecycle-status-direct-members")
            record(f"native-status-close-count:{len(close_calls)}")
        else:
            record("forbidden-receipt")
        """
    script = script.replace("__STATUS_NAME__", repr(status_name)).replace(
        "__STATUS_U32__", repr(status_u32)
    )
    completed, markers, candidates = _run_native_boundary_child(
        label="status-handoff-" + status_name,
        script=script,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("native-status-handoff-hook") == 1
    assert markers.count("native-status-direct-members") == 1
    assert markers.count("lifecycle-status-direct-members") == 1
    assert markers.count("native-status-close-count:1") == 1
    assert "forbidden-receipt" not in markers


def test_native_final_review_quarantine_to_typed_error_interrupt_preserves_handoff() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="final-typed-handoff",
        script=r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        main_cause = OSError("phase-f main operation handoff cause")
        close_cause = OSError("phase-f close handoff cause")
        secondary = KeyboardInterrupt("phase-f quarantine typed-error interruption")
        close_calls = []
        committed_token = None
        committed_close_error = None
        durable._CLOSE_UNKNOWN_REGISTRY = {}
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def fail_main(*_args, **_kwargs):
            raise main_cause

        def close_then_unknown(raw):
            close_calls.append(int(raw))
            assert real_close(raw) is None
            raise close_cause

        def interrupt_handoff(owner, token, close_error):
            global committed_token, committed_close_error
            assert owner.handle_state is durable.DurableHandleState.CLOSE_UNKNOWN
            assert owner.committed_close_token is token
            assert owner.committed_close_cause is close_cause
            assert owner.committed_close_error is close_error
            assert close_error.evidence_owner is token
            assert durable._CLOSE_UNKNOWN_REGISTRY[token.value].owner is owner
            committed_token = token
            committed_close_error = close_error
            record("typed-handoff-committed")
            raise secondary

        durable._write_exact = fail_main
        durable.win32api.CloseHandle = close_then_unknown
        durable._after_close_unknown_commit_before_typed_handoff = interrupt_handoff
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="typed-handoff.bin",
                payload=b"typed-handoff",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            operation_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.WRITE_TEMP
            ]
            close_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.CLOSE
            ]
            assert len(operation_errors) == len(close_errors) == 1
            assert operation_errors[0].cause is main_cause
            assert close_errors[0].cause is close_cause
            assert close_errors[0] is committed_close_error
            assert close_errors[0].evidence_owner is committed_token
            assert sum(item is secondary for item in direct) == 1
            record("typed-handoff-direct-members")
        else:
            record("forbidden-receipt")
        record(f"typed-handoff-close-count:{len(close_calls)}")
        """,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("typed-handoff-committed") == 1
    assert markers.count("typed-handoff-direct-members") == 1
    assert markers.count("typed-handoff-close-count:1") == 1
    assert "forbidden-receipt" not in markers


def test_native_final_review_lifecycle_typed_handoff_interrupt_preserves_direct_members() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="final-lifecycle-typed-handoff",
        script=r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        main_cause = OSError("phase-f lifecycle typed main")
        close_cause = OSError("phase-f lifecycle typed close")
        secondary = KeyboardInterrupt("phase-f lifecycle typed secondary")
        prepared_operation_error = None
        prepared_close_error = None
        close_calls = []
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def fail_main(*_args, **_kwargs):
            raise main_cause

        def close_then_unknown(raw):
            close_calls.append(int(raw))
            assert real_close(raw) is None
            raise close_cause

        def interrupt_handoff(owner, primary, operation_error, close_error):
            global prepared_operation_error, prepared_close_error
            assert primary is main_cause
            assert operation_error.cause is main_cause
            assert close_error is owner.committed_close_error
            assert close_error.cause is close_cause
            prepared_operation_error = operation_error
            prepared_close_error = close_error
            record("lifecycle-typed-handoff-hook")
            raise secondary

        durable._write_exact = fail_main
        durable.win32api.CloseHandle = close_then_unknown
        durable._before_lifecycle_typed_handoff = interrupt_handoff
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="lifecycle-typed-handoff.bin",
                payload=b"lifecycle-typed-handoff",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            assert sum(item is prepared_operation_error for item in direct) == 1
            assert sum(item is prepared_close_error for item in direct) == 1
            assert sum(item is secondary for item in direct) == 1
            record("lifecycle-typed-direct-members")
        else:
            record("forbidden-receipt")
        record(f"lifecycle-typed-close-count:{len(close_calls)}")
        """,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("lifecycle-typed-handoff-hook") == 1
    assert markers.count("lifecycle-typed-direct-members") == 1
    assert markers.count("lifecycle-typed-close-count:1") == 1
    assert "forbidden-receipt" not in markers


def test_native_final_review_duplicate_quarantine_token_preserves_old_entry_and_exits_74() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="final-duplicate-token",
        script=r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        duplicate = "d" * 48

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        old_owner = durable._DurableOwner(
            temp_path=case / "old-temp.bin",
            target_path=case / "old-target.bin",
            sha256="1" * 64,
            byte_count=1,
            path_state=durable.DurablePathState.TARGET_CONFIRMED,
            handle_state=durable.DurableHandleState.CLOSE_UNKNOWN,
            raw_handle=0x1111,
        )
        old_token = durable.CloseUnknownToken(
            value=duplicate,
            path_state=durable.DurablePathState.TARGET_CONFIRMED,
            handle_state=durable.DurableHandleState.CLOSE_UNKNOWN,
        )
        old_entry = durable._CloseUnknownEntry(
            token=old_token,
            owner=old_owner,
            raw_handle=0x1111,
            cause=OSError("old close cause"),
        )
        durable._CLOSE_UNKNOWN_REGISTRY = {duplicate: old_entry}
        new_owner = durable._DurableOwner(
            temp_path=case / "new-temp.bin",
            target_path=case / "new-target.bin",
            sha256="2" * 64,
            byte_count=2,
            path_state=durable.DurablePathState.TARGET_CONFIRMED,
            handle_state=durable.DurableHandleState.CLOSE_IN_FLIGHT,
            raw_handle=0x2222,
        )
        real_failfast = durable._failfast_close_quarantine

        def duplicate_token_hex(_size):
            record("duplicate-token-generated")
            return duplicate

        def observe_then_failfast(owner):
            preserved = durable._CLOSE_UNKNOWN_REGISTRY.get(duplicate) is old_entry
            record("duplicate-old-entry-preserved:" + str(preserved).lower())
            record("duplicate-failfast-owner:" + str(owner is new_owner).lower())
            real_failfast(owner)

        durable.secrets.token_hex = duplicate_token_hex
        durable._failfast_close_quarantine = observe_then_failfast
        returned = durable._quarantine_close_unknown(
            owner=new_owner,
            raw_handle=0x2222,
            cause=OSError("new close cause"),
        )
        record("forbidden-duplicate-return:" + returned.value)
        """,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == durable._FAILFAST_CLOSE_QUARANTINE, json.dumps(
        evidence, sort_keys=True
    )
    assert markers.count("duplicate-token-generated") == 1
    assert markers.count("duplicate-old-entry-preserved:true") == 1
    assert markers.count("duplicate-failfast-owner:true") == 1
    assert not any(item.startswith("forbidden-duplicate-return:") for item in markers)


def test_native_final_review_close_terminal_commit_interrupt_is_visible_without_reclose() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="final-close-terminal-commit",
        script=r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        secondary = KeyboardInterrupt("phase-f close terminal commit interruption")
        close_calls = []
        durable._CLOSE_UNKNOWN_REGISTRY = {}
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def track_close(raw):
            close_calls.append(int(raw))
            result = real_close(raw)
            assert result is None
            return result

        def interrupt_terminal_commit(owner, raw):
            assert owner.handle_state is durable.DurableHandleState.CLOSED
            assert owner.handle is None
            assert owner.raw_handle == int(raw)
            assert close_calls == [int(raw)]
            record("close-terminal-committed")
            raise secondary

        durable.win32api.CloseHandle = track_close
        durable._after_close_terminal_commit_before_return = interrupt_terminal_commit
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="close-terminal-commit.bin",
                payload=b"terminal-commit",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            assert error is secondary
            record("close-terminal-secondary-visible")
        else:
            record("forbidden-receipt")
        record(f"close-terminal-close-count:{len(close_calls)}")
        record(f"close-terminal-quarantine-count:{len(durable._CLOSE_UNKNOWN_REGISTRY)}")
        """,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("close-terminal-committed") == 1
    assert markers.count("close-terminal-secondary-visible") == 1
    assert markers.count("close-terminal-close-count:1") == 1
    assert markers.count("close-terminal-quarantine-count:0") == 1
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    ("status_name", "status_u32"),
    [
        pytest.param("final-failure", 0xC0000035, id="final-failure"),
        pytest.param("informational", 0x00000001, id="informational"),
    ],
)
def test_native_final_review_first_status_throw_interrupt_preserves_direct_members(
    status_name: str,
    status_u32: int,
) -> None:
    script = r"""
        import ctypes
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        status_name = __STATUS_NAME__
        status_u32 = __STATUS_U32__
        signed_status = ctypes.c_int32(status_u32).value
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        secondary = KeyboardInterrupt("phase-f first status throw interruption")
        prepared_status_error = None
        reconcile_handles = []
        close_calls = []
        real_reconcile = durable._try_reconcile_owner_location
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def return_status(*_args):
            return signed_status

        def interrupt_first_throw(owner, status_error):
            global prepared_status_error
            assert owner.committed_native_status == signed_status
            assert owner.committed_native_status_error is status_error
            assert status_error.signed_status == signed_status
            assert status_error.u32_status == status_u32
            prepared_status_error = status_error
            record("first-status-throw-hook:" + status_name)
            raise secondary

        def track_reconcile(owner):
            assert owner.handle is not None
            assert owner.raw_handle == int(owner.handle)
            reconcile_handles.append(owner.raw_handle)
            return real_reconcile(owner)

        def track_close(raw):
            close_calls.append(int(raw))
            return real_close(raw)

        durable._NtSetInformationFile = return_status
        durable._before_native_status_throw = interrupt_first_throw
        durable._try_reconcile_owner_location = track_reconcile
        durable.win32api.CloseHandle = track_close
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="first-status-throw-" + status_name + ".bin",
                payload=b"first-status-throw",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            operation_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.NATIVE_RENAME
            ]
            assert len(operation_errors) == 1
            assert sum(item is prepared_status_error for item in direct) == 1
            assert sum(item is secondary for item in direct) == 1
            expected_reconcile_count = 0 if status_name == "final-failure" else 1
            assert len(reconcile_handles) == expected_reconcile_count
            record("first-status-throw-direct-members:" + status_name)
        else:
            record("forbidden-receipt")
        record(f"first-status-throw-close-count:{len(close_calls)}")
        """
    script = script.replace("__STATUS_NAME__", repr(status_name)).replace(
        "__STATUS_U32__", repr(status_u32)
    )
    completed, markers, candidates = _run_native_boundary_child(
        label="first-status-throw-" + status_name,
        script=script,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("first-status-throw-hook:" + status_name) == 1
    assert markers.count("first-status-throw-direct-members:" + status_name) == 1
    assert markers.count("first-status-throw-close-count:1") == 1
    assert "forbidden-receipt" not in markers


def test_native_final_review_committed_raise_interrupt_preserves_all_direct_members() -> None:
    completed, markers, candidates = _run_native_boundary_child(
        label="final-committed-raise",
        script=r"""
        import ctypes
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        status_u32 = 0xC0000035
        signed_status = ctypes.c_int32(status_u32).value
        close_cause = OSError("phase-f committed raise close cause")
        secondary = KeyboardInterrupt("phase-f committed raise interruption")
        committed_operation_error = None
        committed_close_error = None
        committed_status_error = None
        close_calls = []
        durable._CLOSE_UNKNOWN_REGISTRY = {}
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def return_status(*_args):
            return signed_status

        def close_then_unknown(raw):
            close_calls.append(int(raw))
            assert real_close(raw) is None
            raise close_cause

        def interrupt_committed_raise(lifecycle, handoff_error):
            global committed_operation_error, committed_close_error
            global committed_status_error
            assert lifecycle.committed_handoff_error is handoff_error
            owner = lifecycle.current_owner
            committed_operation_error = lifecycle.committed_operation_error
            committed_close_error = lifecycle.close_error
            committed_status_error = owner.committed_native_status_error
            record("committed-raise-hook")
            record("committed-raise-error-type:" + type(handoff_error).__name__)
            record("committed-raise-owner-state:" + owner.handle_state.value)
            raise secondary

        durable._NtSetInformationFile = return_status
        durable.win32api.CloseHandle = close_then_unknown
        durable._before_lifecycle_committed_raise = interrupt_committed_raise
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="committed-raise.bin",
                payload=b"committed-raise",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            assert sum(item is committed_operation_error for item in direct) == 1
            assert sum(item is committed_close_error for item in direct) == 1
            assert sum(item is committed_status_error for item in direct) == 1
            assert sum(item is secondary for item in direct) == 1
            record("committed-raise-direct-members")
        else:
            record("forbidden-receipt")
        record(f"committed-raise-close-count:{len(close_calls)}")
        record(f"committed-raise-quarantine-count:{len(durable._CLOSE_UNKNOWN_REGISTRY)}")
        """,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("committed-raise-hook") == 1
    assert markers.count("committed-raise-direct-members") == 1
    assert markers.count("committed-raise-close-count:1") == 1
    assert markers.count("committed-raise-quarantine-count:1") == 1
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    ("status_name", "status_u32", "force_reconcile_failure"),
    [
        pytest.param("final-failure", 0xC0000035, False, id="final-failure"),
        pytest.param("informational", 0x00000001, False, id="informational"),
        pytest.param(
            "informational-reconcile-failure",
            0x00000001,
            True,
            id="informational-reconcile-failure",
        ),
    ],
)
def test_native_final_review_committed_status_final_raise_interrupt_preserves_direct_members(
    status_name: str,
    status_u32: int,
    force_reconcile_failure: bool,
) -> None:
    script = r"""
        import ctypes
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        status_name = __STATUS_NAME__
        status_u32 = __STATUS_U32__
        force_reconcile_failure = __FORCE_RECONCILE_FAILURE__
        signed_status = ctypes.c_int32(status_u32).value
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        secondary = KeyboardInterrupt("phase-f committed status final raise")
        reconcile_cause = (
            OSError("phase-f informational reconcile cause")
            if force_reconcile_failure
            else None
        )
        prepared_status_error = None
        close_calls = []
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def return_status(*_args):
            return signed_status

        def fail_reconcile(_owner):
            assert force_reconcile_failure
            record("committed-status-reconcile-failed")
            return reconcile_cause

        def interrupt_final_raise(owner, status_error, observed_reconcile_cause):
            global prepared_status_error
            assert owner.committed_native_status_error is status_error
            assert status_error.signed_status == signed_status
            assert status_error.u32_status == status_u32
            assert observed_reconcile_cause is reconcile_cause
            prepared_status_error = status_error
            record("committed-status-final-raise-hook:" + status_name)
            raise secondary

        def track_close(raw):
            close_calls.append(int(raw))
            return real_close(raw)

        durable._NtSetInformationFile = return_status
        if force_reconcile_failure:
            durable._try_reconcile_owner_location = fail_reconcile
        durable._before_native_status_final_raise = interrupt_final_raise
        durable.win32api.CloseHandle = track_close
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="committed-status-final-" + status_name + ".bin",
                payload=b"committed-status-final",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            operation_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.NATIVE_RENAME
            ]
            assert len(operation_errors) == 1
            assert sum(item is prepared_status_error for item in direct) == 1
            assert sum(item is secondary for item in direct) == 1
            if reconcile_cause is not None:
                assert sum(item is reconcile_cause for item in direct) == 1
            record("committed-status-final-direct-members:" + status_name)
        else:
            record("forbidden-receipt")
        record(f"committed-status-final-close-count:{len(close_calls)}")
        """
    script = (
        script.replace("__STATUS_NAME__", repr(status_name))
        .replace("__STATUS_U32__", repr(status_u32))
        .replace("__FORCE_RECONCILE_FAILURE__", repr(force_reconcile_failure))
    )
    short_label = {
        "final-failure": "f-status-out-f",
        "informational": "f-status-out-i",
        "informational-reconcile-failure": "f-status-out-ir",
    }[status_name]
    completed, markers, candidates = _run_native_boundary_child(
        label=short_label,
        script=script,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("committed-status-final-raise-hook:" + status_name) == 1
    assert markers.count("committed-status-final-direct-members:" + status_name) == 1
    assert markers.count("committed-status-final-close-count:1") == 1
    if force_reconcile_failure:
        assert markers.count("committed-status-reconcile-failed") == 1
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    "handoff_window",
    [
        pytest.param("post-prepare-pre-read", id="post-prepare-pre-read"),
        pytest.param("second-prepare-pre-commit", id="second-prepare-pre-commit"),
    ],
)
def test_native_final_review_committed_lifecycle_pre_raise_windows_preserve_direct_members(
    handoff_window: str,
) -> None:
    script = r"""
        import ctypes
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        handoff_window = __HANDOFF_WINDOW__
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        signed_status = ctypes.c_int32(0xC0000035).value
        close_cause = OSError("phase-f lifecycle pre-raise close cause")
        first_prepare_cause = OSError("phase-f first prepare interruption")
        secondary = KeyboardInterrupt("phase-f lifecycle pre-raise interruption")
        committed_operation_error = None
        committed_close_error = None
        committed_status_error = None
        close_calls = []
        durable._CLOSE_UNKNOWN_REGISTRY = {}
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def return_status(*_args):
            return signed_status

        def close_then_unknown(raw):
            close_calls.append(int(raw))
            assert real_close(raw) is None
            raise close_cause

        def fail_first_prepare(_lifecycle, _prepared_error):
            assert handoff_window == "second-prepare-pre-commit"
            record("lifecycle-first-prepare-interrupted")
            raise first_prepare_cause

        def interrupt_window(lifecycle):
            global committed_operation_error, committed_close_error
            global committed_status_error
            owner = lifecycle.current_owner
            committed_operation_error = lifecycle.committed_operation_error
            committed_close_error = lifecycle.close_error
            committed_status_error = owner.committed_native_status_error
            record("lifecycle-pre-raise-hook:" + handoff_window)
            raise secondary

        def interrupt_post_prepare(lifecycle):
            assert handoff_window == "post-prepare-pre-read"
            interrupt_window(lifecycle)

        def interrupt_second_prepare(lifecycle, _recovered_error):
            assert handoff_window == "second-prepare-pre-commit"
            assert lifecycle.committed_handoff_error is None
            interrupt_window(lifecycle)

        durable._NtSetInformationFile = return_status
        durable.win32api.CloseHandle = close_then_unknown
        if handoff_window == "post-prepare-pre-read":
            durable._after_lifecycle_committed_prepare_before_read = interrupt_post_prepare
        else:
            durable._after_lifecycle_first_prepare_before_commit = fail_first_prepare
            durable._after_lifecycle_second_prepare_before_commit = interrupt_second_prepare
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="lifecycle-pre-raise-" + handoff_window + ".bin",
                payload=b"lifecycle-pre-raise",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            assert sum(item is committed_operation_error for item in direct) == 1
            assert sum(item is committed_close_error for item in direct) == 1
            assert sum(item is committed_status_error for item in direct) == 1
            assert sum(item is secondary for item in direct) == 1
            if handoff_window == "second-prepare-pre-commit":
                assert sum(item is first_prepare_cause for item in direct) == 1
            record("lifecycle-pre-raise-direct-members:" + handoff_window)
        else:
            record("forbidden-receipt")
        record(f"lifecycle-pre-raise-close-count:{len(close_calls)}")
        record(f"lifecycle-pre-raise-quarantine-count:{len(durable._CLOSE_UNKNOWN_REGISTRY)}")
        """.replace("__HANDOFF_WINDOW__", repr(handoff_window))
    short_label = {
        "post-prepare-pre-read": "f-life-read",
        "second-prepare-pre-commit": "f-life-second",
    }[handoff_window]
    completed, markers, candidates = _run_native_boundary_child(
        label=short_label,
        script=script,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("lifecycle-pre-raise-hook:" + handoff_window) == 1
    assert markers.count("lifecycle-pre-raise-direct-members:" + handoff_window) == 1
    assert markers.count("lifecycle-pre-raise-close-count:1") == 1
    assert markers.count("lifecycle-pre-raise-quarantine-count:1") == 1
    if handoff_window == "second-prepare-pre-commit":
        assert markers.count("lifecycle-first-prepare-interrupted") == 1
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    "terminal_window",
    [
        pytest.param("before-finalize", id="before-finalize"),
        pytest.param("after-final-last-resort", id="after-final-last-resort"),
    ],
)
def test_native_final_review_terminalization_secondary_enters_typed_handoff(
    terminal_window: str,
) -> None:
    script = r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        terminal_window = __TERMINAL_WINDOW__
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        main_cause = OSError("phase-f terminalization primary")
        secondary = KeyboardInterrupt("phase-f terminalization secondary")
        close_calls = []
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def fail_main(*_args, **_kwargs):
            raise main_cause

        def track_close(raw):
            close_calls.append(int(raw))
            return real_close(raw)

        def interrupt_before_finalize(lifecycle):
            assert terminal_window == "before-finalize"
            assert lifecycle.current_owner.handle_state is durable.DurableHandleState.OPEN
            record("terminalization-hook:before-finalize")
            raise secondary

        def interrupt_after_last_resort(lifecycle):
            assert terminal_window == "after-final-last-resort"
            assert lifecycle.current_owner.handle_state is durable.DurableHandleState.CLOSED
            assert len(close_calls) == 1
            record("terminalization-hook:after-final-last-resort")
            raise secondary

        durable._write_exact = fail_main
        durable.win32api.CloseHandle = track_close
        if terminal_window == "before-finalize":
            durable._before_lifecycle_finalize_call = interrupt_before_finalize
        else:
            durable._after_lifecycle_final_last_resort_before_handoff = (
                interrupt_after_last_resort
            )
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="terminalization-" + terminal_window + ".bin",
                payload=b"terminalization",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            operation_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.WRITE_TEMP
            ]
            assert len(operation_errors) == 1
            operation_error = operation_errors[0]
            assert operation_error.cause is main_cause
            assert operation_error.evidence_owner.handle_state is durable.DurableHandleState.CLOSED
            assert sum(item is secondary for item in direct) == 1
            graph = []
            pending = [error]
            seen = set()
            while pending:
                current = pending.pop()
                if id(current) in seen:
                    continue
                seen.add(id(current))
                graph.append(current)
                if isinstance(current, BaseExceptionGroup):
                    pending.extend(current.exceptions)
                for name in ("cause", "__cause__", "__context__"):
                    nested = getattr(current, name, None)
                    if isinstance(nested, BaseException):
                        pending.append(nested)
            assert sum(item is main_cause for item in graph) == 1
            record("terminalization-typed-direct:" + terminal_window)
        else:
            record("forbidden-receipt")
        record(f"terminalization-close-count:{len(close_calls)}")
        """.replace("__TERMINAL_WINDOW__", repr(terminal_window))
    short_label = {
        "before-finalize": "f-term-before",
        "after-final-last-resort": "f-term-after",
    }[terminal_window]
    completed, markers, candidates = _run_native_boundary_child(
        label=short_label,
        script=script,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("terminalization-hook:" + terminal_window) == 1
    assert markers.count("terminalization-typed-direct:" + terminal_window) == 1
    assert markers.count("terminalization-close-count:1") == 1
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    "status_window",
    [
        pytest.param("return-registration", id="return-registration"),
        pytest.param("settled-pre-handoff", id="settled-pre-handoff"),
    ],
)
@pytest.mark.parametrize(
    ("status_name", "status_u32"),
    [
        pytest.param("final-failure", 0xC0000035, id="final-failure"),
        pytest.param("informational", 0x00000001, id="informational"),
    ],
)
def test_native_final_review_status_registration_windows_preserve_direct_members(
    status_window: str,
    status_name: str,
    status_u32: int,
) -> None:
    script = r"""
        import ctypes
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        status_window = __STATUS_WINDOW__
        status_name = __STATUS_NAME__
        status_u32 = __STATUS_U32__
        signed_status = ctypes.c_int32(status_u32).value
        expected_mapped_winerror = int(
            durable._RtlNtStatusToDosError(signed_status)
        )
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        secondary = KeyboardInterrupt("phase-f native status registration")
        prepared_status_error = None
        observed_owner_raw = None
        reconcile_handles = []
        close_calls = []
        real_reconcile = durable._try_reconcile_owner_location
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def return_status(*_args):
            return signed_status

        def interrupt_return(owner, status):
            global observed_owner_raw, prepared_status_error
            assert status_window == "return-registration"
            assert status == signed_status
            assert owner.committed_native_status is None
            assert owner.committed_native_status_error is None
            observed_owner_raw = owner.raw_handle
            prepared_status_error = owner.committed_native_status_error
            record("status-registration-hook:return-registration:" + status_name)
            raise secondary

        def interrupt_settled(owner, status_error, _captured_cause):
            global observed_owner_raw, prepared_status_error
            assert status_window == "settled-pre-handoff"
            assert owner.committed_native_status == signed_status
            assert owner.committed_native_status_error is status_error
            assert status_error.signed_status == signed_status
            assert status_error.u32_status == status_u32
            assert status_error.mapped_winerror == expected_mapped_winerror
            observed_owner_raw = owner.raw_handle
            prepared_status_error = status_error
            record("status-registration-hook:settled-pre-handoff:" + status_name)
            raise secondary

        def track_reconcile(owner):
            assert owner.handle is not None
            assert owner.raw_handle == int(owner.handle)
            reconcile_handles.append(owner.raw_handle)
            return real_reconcile(owner)

        def track_close(raw):
            close_calls.append(int(raw))
            return real_close(raw)

        durable._NtSetInformationFile = return_status
        durable._try_reconcile_owner_location = track_reconcile
        if status_window == "return-registration":
            durable._after_native_return_before_status_commit = interrupt_return
        else:
            durable._after_native_pending_settled_before_status_handoff = (
                interrupt_settled
            )
        durable.win32api.CloseHandle = track_close
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="status-registration-" + status_window + "-" + status_name + ".bin",
                payload=b"status-registration",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            operation_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.NATIVE_RENAME
            ]
            assert len(operation_errors) == 1
            operation_error = operation_errors[0]
            assert operation_error.path_state is durable.DurablePathState.TEMP_CONFIRMED
            assert operation_error.handle_state is durable.DurableHandleState.CLOSED
            assert operation_error.temp_owner is not None
            status_errors = [
                item for item in direct
                if type(item) is durable.NativeRenameStatusError
            ]
            assert len(status_errors) == 1
            direct_status_error = status_errors[0]
            assert direct_status_error.signed_status == signed_status
            assert direct_status_error.u32_status == status_u32
            assert direct_status_error.mapped_winerror == expected_mapped_winerror
            if status_window == "settled-pre-handoff":
                assert direct_status_error is prepared_status_error
            else:
                assert prepared_status_error is None
            assert sum(item is secondary for item in direct) == 1
            expected_reconcile_handles = (
                [] if status_name == "final-failure" else [observed_owner_raw]
            )
            assert reconcile_handles == expected_reconcile_handles
            record("status-registration-direct:" + status_window + ":" + status_name)
        else:
            record("forbidden-receipt")
        record(f"status-registration-close-count:{len(close_calls)}")
        """
    script = (
        script.replace("__STATUS_WINDOW__", repr(status_window))
        .replace("__STATUS_NAME__", repr(status_name))
        .replace("__STATUS_U32__", repr(status_u32))
    )
    short_window = "ret" if status_window == "return-registration" else "set"
    short_status = "f" if status_name == "final-failure" else "i"
    completed, markers, candidates = _run_native_boundary_child(
        label=f"f-stat-{short_window}-{short_status}",
        script=script,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    marker_suffix = status_window + ":" + status_name
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("status-registration-hook:" + marker_suffix) == 1
    assert markers.count("status-registration-direct:" + marker_suffix) == 1
    assert markers.count("status-registration-close-count:1") == 1
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    ("status_name", "iosb_status_u32"),
    [
        pytest.param("final-failure", 0xC0000035, id="final-failure"),
        pytest.param("informational", 0x00000001, id="informational"),
    ],
)
def test_native_final_review_no_return_iosb_nonpending_is_not_authoritative(
    status_name: str,
    iosb_status_u32: int,
) -> None:
    script = r"""
        import ctypes
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        status_name = __STATUS_NAME__
        iosb_status_u32 = __IOSB_STATUS_U32__
        signed_iosb_status = ctypes.c_int32(iosb_status_u32).value
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        primary = KeyboardInterrupt("native wrapper raised without a return")
        native_raw = None
        reconcile_handles = []
        close_calls = []
        real_reconcile = durable._try_reconcile_owner_location
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def walk(root):
            pending = [root]
            found = []
            seen = set()
            while pending:
                current = pending.pop()
                if id(current) in seen:
                    continue
                seen.add(id(current))
                found.append(current)
                if isinstance(current, BaseExceptionGroup):
                    pending.extend(current.exceptions)
                for name in ("cause", "reconcile_cause", "__cause__", "__context__"):
                    nested = getattr(current, name, None)
                    if isinstance(nested, BaseException):
                        pending.append(nested)
            return found

        def raise_without_return(handle, iosb, *_args):
            global native_raw
            native_raw = int(handle)
            iosb._obj.Status = signed_iosb_status
            record("no-return-iosb:" + status_name)
            raise primary

        def inspect_settled(owner, status_error, captured_cause):
            assert captured_cause is primary
            assert owner.raw_handle == native_raw
            assert owner.native_pending_decision is durable._NativePendingDecision.NOT_PENDING
            assert owner.committed_native_status is None
            assert owner.committed_native_status_error is None
            assert status_error is None
            assert owner.path_state is durable.DurablePathState.TEMP_CONFIRMED
            assert reconcile_handles == [native_raw]
            record("no-return-settled:" + status_name)

        def track_reconcile(owner):
            assert owner.handle is not None
            assert owner.raw_handle == int(owner.handle) == native_raw
            assert owner.committed_native_status is None
            assert owner.committed_native_status_error is None
            assert owner.path_state is durable.DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
            reconcile_handles.append(owner.raw_handle)
            record("no-return-reconcile:" + status_name)
            return real_reconcile(owner)

        def track_close(raw):
            close_calls.append(int(raw))
            return real_close(raw)

        durable._NtSetInformationFile = raise_without_return
        durable._after_native_pending_settled_before_status_handoff = inspect_settled
        durable._try_reconcile_owner_location = track_reconcile
        durable.win32api.CloseHandle = track_close
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename="no-return-iosb-" + status_name + ".bin",
                payload=b"no-return-iosb",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            graph = walk(error)
            operation_errors = [
                item for item in graph
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.NATIVE_RENAME
            ]
            assert len(operation_errors) == 1
            operation_error = operation_errors[0]
            assert operation_error.path_state is durable.DurablePathState.TEMP_CONFIRMED
            assert operation_error.handle_state is durable.DurableHandleState.CLOSED
            assert operation_error.temp_owner is not None
            assert not any(
                type(item) is durable.NativeRenameStatusError for item in graph
            )
            assert sum(item is primary for item in graph) == 1
            assert reconcile_handles == [native_raw]
            assert close_calls == [native_raw]
            record("no-return-typed:" + status_name)
        else:
            record("forbidden-receipt")
        """
    script = (
        script.replace("__STATUS_NAME__", repr(status_name))
        .replace("__IOSB_STATUS_U32__", repr(iosb_status_u32))
    )
    short_status = "f" if status_name == "final-failure" else "i"
    completed, markers, candidates = _run_native_boundary_child(
        label="f-noret-" + short_status,
        script=script,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("no-return-iosb:" + status_name) == 1
    assert markers.count("no-return-settled:" + status_name) == 1
    assert markers.count("no-return-reconcile:" + status_name) == 1
    assert markers.count("no-return-typed:" + status_name) == 1
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    ("status_name", "status_u32", "force_reconcile_failure"),
    [
        pytest.param("final-failure", 0xC0000035, False, id="final-failure"),
        pytest.param(
            "informational-reconcile-failure",
            0x00000001,
            True,
            id="informational-reconcile-failure",
        ),
    ],
)
def test_native_final_review_exception_secondary_preserves_committed_status_evidence(
    status_name: str,
    status_u32: int,
    force_reconcile_failure: bool,
) -> None:
    script = r"""
        import ctypes
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        status_name = __STATUS_NAME__
        status_u32 = __STATUS_U32__
        force_reconcile_failure = __FORCE_RECONCILE_FAILURE__
        signed_status = ctypes.c_int32(status_u32).value
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        secondary = RuntimeError("ordinary exception after committed status")
        reconcile_cause = (
            OSError("exact informational reconcile failure")
            if force_reconcile_failure
            else None
        )
        prepared_status_error = None
        close_calls = []
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def return_status(*_args):
            return signed_status

        def reconcile(owner):
            assert force_reconcile_failure
            assert owner.handle is not None
            assert owner.raw_handle == int(owner.handle)
            owner.path_state = durable.DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
            record("exception-secondary-reconcile-failed")
            return reconcile_cause

        def interrupt_after_settle(owner, status_error, captured_cause):
            global prepared_status_error
            assert captured_cause is status_error
            assert owner.committed_native_status == signed_status
            assert owner.committed_native_status_error is status_error
            assert status_error.signed_status == signed_status
            assert status_error.u32_status == status_u32
            assert owner.native_status_semantics_committed
            if force_reconcile_failure:
                assert owner.committed_native_reconcile_cause is reconcile_cause
                assert owner.path_state is durable.DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
            else:
                assert owner.committed_native_reconcile_cause is None
                assert owner.path_state is durable.DurablePathState.TEMP_CONFIRMED
            prepared_status_error = status_error
            record("exception-secondary-hook:" + status_name)
            raise secondary

        def track_close(raw):
            close_calls.append(int(raw))
            return real_close(raw)

        durable._NtSetInformationFile = return_status
        durable._try_reconcile_owner_location = reconcile
        durable._after_native_pending_settled_before_status_handoff = (
            interrupt_after_settle
        )
        durable.win32api.CloseHandle = track_close
        parent = approve_physical_d_write_root(case)
        try:
            durable.write_durable_bytes(
                parent=parent,
                filename=(
                    "exc-sec-i.bin" if force_reconcile_failure else "exc-sec-f.bin"
                ),
                payload=b"exception-secondary",
                replace=False,
                max_bytes=64,
            )
        except BaseException as error:
            assert isinstance(error, BaseExceptionGroup)
            direct = list(error.exceptions)
            operation_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.NATIVE_RENAME
            ]
            assert len(operation_errors) == 1
            expected_path_state = (
                durable.DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
                if force_reconcile_failure
                else durable.DurablePathState.TEMP_CONFIRMED
            )
            assert operation_errors[0].path_state is expected_path_state
            assert operation_errors[0].handle_state is durable.DurableHandleState.CLOSED
            assert sum(item is prepared_status_error for item in direct) == 1
            assert sum(item is secondary for item in direct) == 1
            if reconcile_cause is not None:
                assert sum(item is reconcile_cause for item in direct) == 1
            record("exception-secondary-direct:" + status_name)
        else:
            record("forbidden-receipt")
        record(f"exception-secondary-close-count:{len(close_calls)}")
        """
    script = (
        script.replace("__STATUS_NAME__", repr(status_name))
        .replace("__STATUS_U32__", repr(status_u32))
        .replace("__FORCE_RECONCILE_FAILURE__", repr(force_reconcile_failure))
    )
    short_status = "f" if not force_reconcile_failure else "i"
    completed, markers, candidates = _run_native_boundary_child(
        label="f-exc-sec-" + short_status,
        script=script,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("exception-secondary-hook:" + status_name) == 1
    assert markers.count("exception-secondary-direct:" + status_name) == 1
    assert markers.count("exception-secondary-close-count:1") == 1
    if force_reconcile_failure:
        assert markers.count("exception-secondary-reconcile-failed") == 1
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    ("interrupt_mode", "primary_kind"),
    [
        pytest.param(
            "exact-success-return",
            "keyboardinterrupt",
            id="exact-success-keyboardinterrupt",
        ),
        pytest.param(
            "no-return",
            "keyboardinterrupt",
            id="no-return-keyboardinterrupt",
        ),
        pytest.param(
            "no-return",
            "systemexit",
            id="no-return-systemexit",
        ),
    ],
)
def test_native_final_review_nonexception_primary_remains_direct_and_uncatchable(
    interrupt_mode: str,
    primary_kind: str,
) -> None:
    script = r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        interrupt_mode = __INTERRUPT_MODE__
        primary_kind = __PRIMARY_KIND__
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        primary = (
            KeyboardInterrupt("exact non-Exception native primary")
            if primary_kind == "keyboardinterrupt"
            else SystemExit("exact non-Exception native primary")
        )
        native_raw = None
        reconcile_handles = []
        close_calls = []
        real_native = durable._NtSetInformationFile
        real_reconcile = durable._try_reconcile_owner_location
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def native_call(handle, iosb, buffer, length, information_class):
            global native_raw
            native_raw = int(handle)
            if interrupt_mode == "exact-success-return":
                returned = real_native(
                    handle, iosb, buffer, length, information_class
                )
                assert durable._signed_ntstatus(returned) == durable._STATUS_SUCCESS
                return returned
            iosb._obj.Status = durable._STATUS_SUCCESS
            record("nonexception-no-return:" + primary_kind)
            raise primary

        def interrupt_return(owner, status):
            assert interrupt_mode == "exact-success-return"
            assert owner.raw_handle == native_raw
            assert status == durable._STATUS_SUCCESS
            record("nonexception-return-hook:" + primary_kind)
            raise primary

        def track_reconcile(owner):
            assert owner.handle is not None
            assert owner.raw_handle == int(owner.handle) == native_raw
            reconcile_handles.append(owner.raw_handle)
            return real_reconcile(owner)

        def track_close(raw):
            close_calls.append(int(raw))
            return real_close(raw)

        durable._NtSetInformationFile = native_call
        if interrupt_mode == "exact-success-return":
            durable._after_native_return_before_status_commit = interrupt_return
        durable._try_reconcile_owner_location = track_reconcile
        durable.win32api.CloseHandle = track_close
        parent = approve_physical_d_write_root(case)
        try:
            try:
                durable.write_durable_bytes(
                    parent=parent,
                    filename=(
                        "nonexc-ret.bin"
                        if interrupt_mode == "exact-success-return"
                        else "nonexc-noret.bin"
                    ),
                    payload=b"nonexception-primary",
                    replace=False,
                    max_bytes=64,
                )
            except Exception:
                record("forbidden-exception-swallow")
            else:
                record("forbidden-receipt")
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            operation_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.NATIVE_RENAME
            ]
            assert len(operation_errors) == 1
            operation_error = operation_errors[0]
            expected_path_state = (
                durable.DurablePathState.TARGET_CONFIRMED
                if interrupt_mode == "exact-success-return"
                else durable.DurablePathState.TEMP_CONFIRMED
            )
            assert operation_error.path_state is expected_path_state
            assert operation_error.handle_state is durable.DurableHandleState.CLOSED
            assert sum(item is primary for item in direct) == 1
            assert not any(
                type(item) is durable.NativeRenameStatusError for item in direct
            )
            assert reconcile_handles == [native_raw]
            assert close_calls == [native_raw]
            record("nonexception-direct:" + interrupt_mode + ":" + primary_kind)
        """
    script = (
        script.replace("__INTERRUPT_MODE__", repr(interrupt_mode))
        .replace("__PRIMARY_KIND__", repr(primary_kind))
    )
    short_mode = "ret" if interrupt_mode == "exact-success-return" else "noret"
    short_primary = "ki" if primary_kind == "keyboardinterrupt" else "se"
    completed, markers, candidates = _run_native_boundary_child(
        label="f-nonexc-" + short_mode + "-" + short_primary,
        script=script,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    marker_suffix = interrupt_mode + ":" + primary_kind
    assert markers.count("nonexception-direct:" + marker_suffix) == 1
    assert "forbidden-exception-swallow" not in markers
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    "primary_kind",
    ["keyboardinterrupt", "systemexit"],
    ids=["keyboardinterrupt", "systemexit"],
)
def test_native_final_review_mapping_nonexception_preserves_fallback_and_primary(
    primary_kind: str,
) -> None:
    script = r"""
        import ctypes
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        primary_kind = __PRIMARY_KIND__
        raw_status_u32 = 0xC1234567
        signed_status = ctypes.c_int32(raw_status_u32).value
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        primary = (
            KeyboardInterrupt("exact Rtl mapping interruption")
            if primary_kind == "keyboardinterrupt"
            else SystemExit("exact Rtl mapping interruption")
        )
        tracked_owner = None
        mapping_inputs = []
        close_calls = []
        real_commit_status = durable._commit_native_status
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def return_failure(*_args):
            return signed_status

        def track_commit_status(owner, status):
            global tracked_owner
            tracked_owner = owner
            return real_commit_status(owner, status)

        def interrupt_mapping(status):
            assert tracked_owner is not None
            record("mapping-path-before:" + tracked_owner.path_state.value)
            assert tracked_owner.path_state is durable.DurablePathState.TEMP_CONFIRMED
            mapping_inputs.append(int(status))
            record("mapping-nonexception-hook:" + primary_kind)
            raise primary

        def track_close(raw):
            close_calls.append(int(raw))
            return real_close(raw)

        durable._NtSetInformationFile = return_failure
        durable._commit_native_status = track_commit_status
        durable._RtlNtStatusToDosError = interrupt_mapping
        durable.win32api.CloseHandle = track_close
        parent = approve_physical_d_write_root(case)
        try:
            try:
                durable.write_durable_bytes(
                    parent=parent,
                    filename="map-nonexc.bin",
                    payload=b"mapping-nonexception",
                    replace=False,
                    max_bytes=64,
                )
            except Exception:
                record("forbidden-exception-swallow")
            else:
                record("forbidden-receipt")
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            operation_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.NATIVE_RENAME
            ]
            status_errors = [
                item for item in direct
                if type(item) is durable.NativeRenameStatusError
            ]
            assert len(operation_errors) == len(status_errors) == 1
            operation_error = operation_errors[0]
            status_error = status_errors[0]
            assert operation_error.path_state is durable.DurablePathState.TEMP_CONFIRMED
            assert operation_error.handle_state is durable.DurableHandleState.CLOSED
            assert operation_error.temp_owner is not None
            assert status_error.signed_status == signed_status
            assert status_error.u32_status == raw_status_u32
            assert status_error.mapped_winerror == durable._ERROR_MR_MID_NOT_FOUND
            assert sum(item is primary for item in direct) == 1
            assert mapping_inputs == [signed_status]
            assert len(close_calls) == 1
            record("mapping-nonexception-direct:" + primary_kind)
        """
    completed, markers, candidates = _run_native_boundary_child(
        label="f-map-" + ("ki" if primary_kind == "keyboardinterrupt" else "se"),
        script=script.replace("__PRIMARY_KIND__", repr(primary_kind)),
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("mapping-path-before:temp_confirmed") == 1
    assert markers.count("mapping-nonexception-hook:" + primary_kind) == 1
    assert markers.count("mapping-nonexception-direct:" + primary_kind) == 1
    assert "forbidden-exception-swallow" not in markers
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    "primary_kind",
    ["keyboardinterrupt", "systemexit"],
    ids=["keyboardinterrupt", "systemexit"],
)
def test_native_final_review_informational_reconcile_nonexception_remains_direct(
    primary_kind: str,
) -> None:
    script = r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        primary_kind = __PRIMARY_KIND__
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        primary = (
            KeyboardInterrupt("exact informational reconcile interruption")
            if primary_kind == "keyboardinterrupt"
            else SystemExit("exact informational reconcile interruption")
        )
        native_raw = None
        reconcile_handles = []
        close_calls = []
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def return_informational(handle, *_args):
            global native_raw
            native_raw = int(handle)
            return 1

        def interrupt_reconcile(owner):
            assert owner.handle is not None
            assert owner.raw_handle == int(owner.handle) == native_raw
            reconcile_handles.append(owner.raw_handle)
            owner.path_state = durable.DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
            record("info-reconcile-nonexception-hook:" + primary_kind)
            return primary

        def track_close(raw):
            close_calls.append(int(raw))
            return real_close(raw)

        durable._NtSetInformationFile = return_informational
        durable._try_reconcile_owner_location = interrupt_reconcile
        durable.win32api.CloseHandle = track_close
        parent = approve_physical_d_write_root(case)
        try:
            try:
                durable.write_durable_bytes(
                    parent=parent,
                    filename="info-reconcile-nonexc.bin",
                    payload=b"informational-reconcile",
                    replace=False,
                    max_bytes=64,
                )
            except Exception:
                record("forbidden-exception-swallow")
            else:
                record("forbidden-receipt")
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            operation_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.NATIVE_RENAME
            ]
            status_errors = [
                item for item in direct
                if type(item) is durable.NativeRenameStatusError
            ]
            assert len(operation_errors) == len(status_errors) == 1
            assert operation_errors[0].path_state is durable.DurablePathState.RENAME_IN_FLIGHT_OR_UNKNOWN
            assert operation_errors[0].handle_state is durable.DurableHandleState.CLOSED
            assert status_errors[0].signed_status == 1
            assert status_errors[0].u32_status == 1
            assert sum(item is primary for item in direct) == 1
            assert reconcile_handles == [native_raw]
            assert close_calls == [native_raw]
            record("info-reconcile-nonexception-direct:" + primary_kind)
        """
    completed, markers, candidates = _run_native_boundary_child(
        label="f-info-rec-" + ("ki" if primary_kind == "keyboardinterrupt" else "se"),
        script=script.replace("__PRIMARY_KIND__", repr(primary_kind)),
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("info-reconcile-nonexception-hook:" + primary_kind) == 1
    assert markers.count("info-reconcile-nonexception-direct:" + primary_kind) == 1
    assert "forbidden-exception-swallow" not in markers
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    "failure_kind",
    ["noncollision", "collision-before-retry"],
    ids=["noncollision", "collision-before-retry"],
)
@pytest.mark.parametrize(
    "primary_kind",
    ["keyboardinterrupt", "systemexit"],
    ids=["keyboardinterrupt", "systemexit"],
)
def test_native_final_review_known_no_handle_wrapped_nonexception_remains_direct(
    failure_kind: str,
    primary_kind: str,
) -> None:
    script = r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        failure_kind = __FAILURE_KIND__
        primary_kind = __PRIMARY_KIND__
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        nonexception_primary = (
            KeyboardInterrupt("exact known-no-handle CreateFile interruption")
            if primary_kind == "keyboardinterrupt"
            else SystemExit("exact known-no-handle CreateFile interruption")
        )
        create_error = durable.pywintypes.error(
            80 if failure_kind == "collision-before-retry" else 5,
            "CreateFileW",
            "ordinary CreateFile wrapper around interruption",
        )
        create_calls = []
        close_calls = []
        real_create = durable.win32file.CreateFile
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def fail_create(*_args, **_kwargs):
            create_calls.append(True)
            record("create-call:" + str(len(create_calls)))
            if len(create_calls) > 1:
                record("forbidden-second-create")
                return real_create(*_args, **_kwargs)
            raise create_error from nonexception_primary

        def track_close(raw_handle):
            close_calls.append(int(raw_handle))
            record("forbidden-close")
            return real_close(raw_handle)

        parent = approve_physical_d_write_root(case)
        durable.win32file.CreateFile = fail_create
        durable.win32api.CloseHandle = track_close
        try:
            try:
                durable.write_durable_bytes(
                    parent=parent,
                    filename="known-no-handle.bin",
                    payload=b"not-created",
                    replace=False,
                    max_bytes=64,
                )
            except Exception:
                record("forbidden-exception-swallow")
            else:
                record("forbidden-receipt")
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            typed_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.CREATE_TEMP
            ]
            assert len(typed_errors) == 1
            assert typed_errors[0].cause is create_error
            assert typed_errors[0].evidence_owner is None
            assert typed_errors[0].temp_owner is None
            assert sum(item is nonexception_primary for item in direct) == 1
            assert close_calls == []
            assert len(create_calls) == 1
            record("known-no-handle-direct:" + failure_kind)
        """
    completed, markers, candidates = _run_native_boundary_child(
        label="f-known-no-handle-" + failure_kind + "-" + primary_kind,
        script=(
            script.replace("__FAILURE_KIND__", repr(failure_kind))
            .replace("__PRIMARY_KIND__", repr(primary_kind))
        ),
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert len([item for item in markers if item.startswith("create-call:")]) == 1
    assert markers.count("known-no-handle-direct:" + failure_kind) == 1
    assert candidates == []
    assert "forbidden-second-create" not in markers
    assert "forbidden-close" not in markers
    assert "forbidden-exception-swallow" not in markers
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    "primary_kind",
    ["keyboardinterrupt", "systemexit", "group-keyboardinterrupt"],
    ids=["keyboardinterrupt", "systemexit", "group-keyboardinterrupt"],
)
def test_native_final_review_close_only_nonexception_remains_direct_and_quarantined(
    primary_kind: str,
) -> None:
    script = r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        primary_kind = __PRIMARY_KIND__
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        nonexception_primary = (
            KeyboardInterrupt("exact close-only interruption")
            if primary_kind != "systemexit"
            else SystemExit("exact close-only interruption")
        )
        primary = (
            BaseExceptionGroup(
                "composite close-only interruption",
                [OSError("exact close OSError"), nonexception_primary],
            )
            if primary_kind == "group-keyboardinterrupt"
            else nonexception_primary
        )
        close_calls = []
        durable._CLOSE_UNKNOWN_REGISTRY = {}
        real_close = durable.win32api.CloseHandle

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def close_then_interrupt(raw):
            raw = int(raw)
            close_calls.append(raw)
            result = real_close(raw)
            assert result is None
            record("close-only-nonexception-hook:" + primary_kind)
            raise primary

        durable.win32api.CloseHandle = close_then_interrupt
        parent = approve_physical_d_write_root(case)
        try:
            try:
                durable.write_durable_bytes(
                    parent=parent,
                    filename="close-only-nonexc.bin",
                    payload=b"close-only-nonexception",
                    replace=False,
                    max_bytes=64,
                )
            except Exception:
                record("forbidden-exception-swallow")
            else:
                record("forbidden-receipt")
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            close_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.CLOSE
            ]
            assert len(close_errors) == 1
            close_error = close_errors[0]
            assert close_error.path_state is durable.DurablePathState.TARGET_CONFIRMED
            assert close_error.handle_state is durable.DurableHandleState.CLOSE_UNKNOWN
            assert type(close_error.evidence_owner) is durable.CloseUnknownToken
            token = close_error.evidence_owner
            assert token.value in durable._CLOSE_UNKNOWN_REGISTRY
            entry = durable._CLOSE_UNKNOWN_REGISTRY[token.value]
            assert entry.token is token
            assert entry.owner.committed_close_error is close_error
            assert entry.owner.committed_close_cause is primary
            assert entry.owner.handle_state is durable.DurableHandleState.CLOSE_UNKNOWN
            assert sum(item is nonexception_primary for item in direct) == 1
            assert len(close_calls) == 1
            record("close-only-nonexception-direct:" + primary_kind)
        """
    completed, markers, candidates = _run_native_boundary_child(
        label="f-close-only-" + {
            "keyboardinterrupt": "ki",
            "systemexit": "se",
            "group-keyboardinterrupt": "gki",
        }[primary_kind],
        script=script.replace("__PRIMARY_KIND__", repr(primary_kind)),
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("close-only-nonexception-hook:" + primary_kind) == 1
    assert markers.count("close-only-nonexception-direct:" + primary_kind) == 1
    assert "forbidden-exception-swallow" not in markers
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    "boundary",
    ["parent-verify", "volume-gate"],
    ids=["parent-verify", "volume-gate"],
)
@pytest.mark.parametrize(
    "primary_kind",
    [
        "keyboardinterrupt",
        "systemexit",
        "wrapped-keyboardinterrupt",
        "wrapped-group-order",
    ],
    ids=[
        "keyboardinterrupt",
        "systemexit",
        "wrapped-keyboardinterrupt",
        "wrapped-group-order",
    ],
)
def test_native_final_review_preowner_nonexception_remains_direct(
    boundary: str,
    primary_kind: str,
) -> None:
    script = r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import (
            ApprovedPhysicalDirectory,
            approve_physical_d_write_root,
        )

        boundary = __BOUNDARY__
        primary_kind = __PRIMARY_KIND__
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        nonexception_primary = (
            KeyboardInterrupt("exact pre-owner interruption")
            if primary_kind != "systemexit"
            else SystemExit("exact pre-owner interruption")
        )
        ordered_second = SystemExit("ordered second pre-owner interruption")
        ordered_group = BaseExceptionGroup(
            "ordered pre-owner interruptions",
            [nonexception_primary, ordered_second],
        )
        primary = (
            OSError("ordinary wrapper around pre-owner interruption")
            if primary_kind in {
                "wrapped-keyboardinterrupt",
                "wrapped-group-order",
            }
            else nonexception_primary
        )
        create_calls = []
        real_verify = ApprovedPhysicalDirectory.verify

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        def interrupt_verify(self):
            if boundary == "parent-verify":
                record("preowner-hook:parent-verify:" + primary_kind)
                if primary_kind == "wrapped-keyboardinterrupt":
                    raise primary from nonexception_primary
                if primary_kind == "wrapped-group-order":
                    primary.cause = ordered_group
                    raise primary from ordered_group
                raise primary
            return real_verify(self)

        def interrupt_volume(*_args):
            assert boundary == "volume-gate"
            record("preowner-hook:volume-gate:" + primary_kind)
            if primary_kind == "wrapped-keyboardinterrupt":
                raise primary from nonexception_primary
            if primary_kind == "wrapped-group-order":
                primary.cause = ordered_group
                raise primary from ordered_group
            raise primary

        def forbidden_create(*_args, **_kwargs):
            create_calls.append(True)
            raise AssertionError("CreateFile must not run before pre-owner gate")

        parent = approve_physical_d_write_root(case)
        ApprovedPhysicalDirectory.verify = interrupt_verify
        if boundary == "volume-gate":
            durable.win32api.GetVolumeInformation = interrupt_volume
        durable.win32file.CreateFile = forbidden_create
        try:
            try:
                durable.write_durable_bytes(
                    parent=parent,
                    filename="preowner.bin",
                    payload=b"preowner",
                    replace=False,
                    max_bytes=64,
                )
            except Exception:
                record("forbidden-exception-swallow")
            else:
                record("forbidden-receipt")
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            expected_stage = (
                durable.DurableWriteStage.VALIDATE_PARENT
                if boundary == "parent-verify"
                else durable.DurableWriteStage.VOLUME_GATE
            )
            typed_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is expected_stage
            ]
            assert len(typed_errors) == 1
            assert typed_errors[0].cause is primary
            assert typed_errors[0].evidence_owner is None
            assert typed_errors[0].path_state is None
            assert typed_errors[0].handle_state is None
            if primary_kind == "wrapped-group-order":
                assert len(direct) == 3
                assert direct[1] is nonexception_primary
                assert direct[2] is ordered_second
                assert sum(item is nonexception_primary for item in direct) == 1
                assert sum(item is ordered_second for item in direct) == 1
            else:
                assert sum(item is nonexception_primary for item in direct) == 1
            assert create_calls == []
            record("preowner-direct:" + boundary + ":" + primary_kind)
        """
    script = (
        script.replace("__BOUNDARY__", repr(boundary))
        .replace("__PRIMARY_KIND__", repr(primary_kind))
    )
    short_boundary = "pv" if boundary == "parent-verify" else "vg"
    short_primary = {
        "keyboardinterrupt": "ki",
        "systemexit": "se",
        "wrapped-keyboardinterrupt": "wki",
        "wrapped-group-order": "wgroup",
    }[primary_kind]
    completed, markers, candidates = _run_native_boundary_child(
        label="f-pre-" + short_boundary + "-" + short_primary,
        script=script,
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    suffix = boundary + ":" + primary_kind
    assert markers.count("preowner-hook:" + suffix) == 1
    assert markers.count("preowner-direct:" + suffix) == 1
    assert candidates == []
    assert "forbidden-exception-swallow" not in markers
    assert "forbidden-receipt" not in markers


@pytest.mark.parametrize(
    "primary_kind",
    ["keyboardinterrupt", "systemexit"],
    ids=["keyboardinterrupt", "systemexit"],
)
def test_native_final_review_expected_eof_wrapped_nonexception_remains_direct(
    primary_kind: str,
) -> None:
    script = r"""
        import os
        from pathlib import Path

        from app.core import windows_durable_file as durable
        from app.core.windows_storage_policy import approve_physical_d_write_root

        primary_kind = __PRIMARY_KIND__
        case = Path(os.environ["DURABLE_BOUNDARY_CASE"])
        marker = Path(os.environ["DURABLE_BOUNDARY_MARKER"])
        nonexception_primary = (
            KeyboardInterrupt("exact expected-EOF interruption")
            if primary_kind == "keyboardinterrupt"
            else SystemExit("exact expected-EOF interruption")
        )
        eof_error = durable.pywintypes.error(
            durable._ERROR_HANDLE_EOF,
            "ReadFile",
            "ordinary expected EOF wrapper around interruption",
        )
        read_calls = []
        close_calls = []

        def record(value):
            with marker.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value + "\n")
                stream.flush()

        parent = approve_physical_d_write_root(case)
        real_close = durable.win32api.CloseHandle

        def expected_eof(*_args, **_kwargs):
            read_calls.append(True)
            record("expected-eof-read:" + str(len(read_calls)))
            raise eof_error from nonexception_primary

        def track_close(raw_handle):
            close_calls.append(int(raw_handle))
            record("expected-eof-close:" + str(len(close_calls)))
            return real_close(raw_handle)

        durable.win32file.ReadFile = expected_eof
        durable.win32api.CloseHandle = track_close
        try:
            try:
                durable.write_durable_bytes(
                    parent=parent,
                    filename="expected-eof.bin",
                    payload=b"",
                    replace=False,
                    max_bytes=64,
                )
            except Exception:
                record("forbidden-exception-swallow")
            else:
                record("forbidden-receipt")
        except BaseException as error:
            assert type(error) is BaseExceptionGroup
            direct = list(error.exceptions)
            typed_errors = [
                item for item in direct
                if type(item) is durable.DurableWriteError
                and item.stage is durable.DurableWriteStage.VERIFY_TARGET
            ]
            assert len(typed_errors) == 1
            assert typed_errors[0].cause is eof_error
            assert typed_errors[0].path_state is durable.DurablePathState.TARGET_CONFIRMED
            assert typed_errors[0].handle_state is durable.DurableHandleState.CLOSED
            assert sum(item is nonexception_primary for item in direct) == 1
            assert len(read_calls) == len(close_calls) == 1
            record("expected-eof-direct:" + primary_kind)
        """
    completed, markers, candidates = _run_native_boundary_child(
        label="f-expected-eof-" + primary_kind,
        script=script.replace("__PRIMARY_KIND__", repr(primary_kind)),
    )
    evidence = {
        "returncode": completed.returncode,
        "markers": markers,
        "candidates": candidates,
        "stderr_tail": completed.stderr[-1000:],
    }
    assert completed.returncode == 0, json.dumps(evidence, sort_keys=True)
    assert markers.count("expected-eof-read:1") == 1
    assert markers.count("expected-eof-close:1") == 1
    assert markers.count("expected-eof-direct:" + primary_kind) == 1
    assert len(candidates) == 1
    assert candidates[0]["name"] == "expected-eof.bin"
    assert candidates[0]["size"] == 0
    assert "forbidden-exception-swallow" not in markers
    assert "forbidden-receipt" not in markers
