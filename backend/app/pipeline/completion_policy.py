from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, Literal, TypeVar

from app.models import ProductionPipeline
from app.pipeline.models import PIPELINE_STATUSES, PIPELINE_STEP_NAMES
from app.pipeline.project_identity import (
    ProjectIdentityError,
    begin_prepared_project_child_directory_use,
    begin_prepared_project_directory_use,
    prepared_project_output_path,
)


_EVIDENCE_KEYS = {
    "evidence_version",
    "pipeline_state_sha256",
    "artifact_relative_path",
    "artifact_sha256",
    "quality_report_relative_path",
    "quality_report_sha256",
    "approved_at",
    "approved_by",
}
_REPORT_KEYS = {
    "report_format",
    "pipeline_id",
    "pipeline_state_sha256",
    "artifact_sha256",
    "generated_at",
    "gates",
    "release_blockers",
}
_GATE_NAMES = {
    "structure",
    "technical",
    "visual_narration",
    "facts_rights",
    "quality_contract",
}
_MAX_REPORT_BYTES = 2 * 1024 * 1024
_MAX_QUALITY_CONTRACT_BYTES = 256 * 1024
_QUALITY_CONTRACT_FORMAT = "auto-video-engine-quality-contract-v1"
_QUALITY_CONTRACT_PATH = "质量合同/quality-contract-v1.json"
_QUALITY_CONTRACT_ITEM_ID = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789_"
)
_MAX_FUTURE_SKEW = timedelta(minutes=5)
_MAX_JSON_DEPTH = 128
_MAX_JSON_NODES = 100_000
_HEX_DIGITS = frozenset("0123456789abcdef")
_FINAL_ACTION = "打开成片页检查当前文件和验收状态"
_T = TypeVar("_T")


class _GuardedFileMissing(Exception):
    pass


class _GuardedFileUnsafe(Exception):
    pass


class _GuardedFileTooLarge(Exception):
    pass


class _StrictJsonError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PipelineCompletionDecision:
    is_complete: bool
    status: str
    reason_code: str
    message: str
    next_action: str
    next_surface: Literal["project", "finished", "none"]
    current_step: str

    @property
    def needs_confirmation(self) -> bool:
        return self.status in {"失败", "需要人工确认"}


def step_output_id(step: object) -> int | None:
    summary = getattr(step, "output_summary", None)
    if not isinstance(summary, dict):
        return None
    for key in ("topic_idea_id", "script_asset_id"):
        value = summary.get(key)
        if type(value) is int and value > 0:
            return value
    return None


def _summary_id(step: object, key: str) -> int | None:
    summary = getattr(step, "output_summary", None)
    if not isinstance(summary, dict):
        return None
    value = summary.get(key)
    return value if type(value) is int and value > 0 else None


def _is_strict_json(value: Any) -> bool:
    pending: list[tuple[Any, int]] = [(value, 0)]
    visited = 0
    while pending:
        current, depth = pending.pop()
        visited += 1
        if depth > _MAX_JSON_DEPTH or visited > _MAX_JSON_NODES:
            return False
        if current is None or isinstance(current, (bool, str)):
            continue
        if type(current) is int:
            continue
        if type(current) is float:
            if not math.isfinite(current):
                return False
            continue
        if isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            if not all(isinstance(key, str) for key in current):
                return False
            pending.extend((item, depth + 1) for item in current.values())
            continue
        return False
    return True


def _pipeline_state_payload(pipeline: ProductionPipeline) -> dict[str, Any]:
    script = getattr(pipeline, "script_asset", None)
    steps = sorted(
        getattr(pipeline, "steps", ()),
        key=lambda item: getattr(item, "step_order", 0),
    )
    return {
        "pipeline_id": pipeline.id,
        "hotspot_id": pipeline.hotspot_id,
        "project_key": pipeline.project_key,
        "topic_idea_id": pipeline.topic_idea_id,
        "script_asset_id": pipeline.script_asset_id,
        "provider": pipeline.provider,
        "model": pipeline.model,
        "script_asset": None
        if script is None
        else {
            "id": script.id,
            "topic_idea_id": script.topic_idea_id,
            "voiceover": script.voiceover,
            "storyboard": script.storyboard,
            "subtitles": script.subtitles,
            "animation_prompts": script.animation_prompts,
            "title_options": script.title_options,
            "cover_copy": script.cover_copy,
            "publish_copy": script.publish_copy,
            "edit_checklist": script.edit_checklist,
        },
        "steps": [
            {
                "name": step.name,
                "step_order": step.step_order,
                "status": step.status,
                "output_id": step_output_id(step),
                "output_summary": step.output_summary,
            }
            for step in steps
        ],
    }


def pipeline_state_sha256(pipeline: ProductionPipeline) -> str:
    payload = _pipeline_state_payload(pipeline)
    if not _is_strict_json(payload):
        raise ValueError("pipeline state is not strict JSON")
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise ValueError("pipeline state is not strict JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _decision(
    *,
    status: str,
    reason_code: str,
    message: str,
    current_step: str,
    next_surface: Literal["project", "finished", "none"],
    next_action: str,
    is_complete: bool = False,
) -> PipelineCompletionDecision:
    return PipelineCompletionDecision(
        is_complete=is_complete,
        status=status,
        reason_code=reason_code,
        message=message,
        next_action=next_action,
        next_surface=next_surface,
        current_step=current_step,
    )


def _invalid_preparation() -> PipelineCompletionDecision:
    return _decision(
        status="需要人工确认",
        reason_code="preparation_invalid",
        message="生产步骤数据不完整或不一致。",
        current_step="项目检查",
        next_surface="project",
        next_action="打开项目页检查生产步骤",
    )


def _valid_preparation_structure(pipeline: ProductionPipeline) -> bool:
    raw_steps = getattr(pipeline, "steps", None)
    if not isinstance(raw_steps, (list, tuple)):
        return False
    try:
        steps = sorted(raw_steps, key=lambda item: item.step_order)
    except (AttributeError, TypeError, ValueError):
        return False
    if len(steps) != len(PIPELINE_STEP_NAMES):
        return False
    for expected_order, (expected_name, step) in enumerate(
        zip(PIPELINE_STEP_NAMES, steps, strict=True),
        start=1,
    ):
        if (
            getattr(step, "step_order", None) != expected_order
            or getattr(step, "name", None) != expected_name
            or getattr(step, "status", None) not in PIPELINE_STATUSES
            or not isinstance(getattr(step, "output_summary", None), dict)
            or not _is_strict_json(step.output_summary)
        ):
            return False

    step_map = {step.name: step for step in steps}
    selected_topic = step_map["选择候选选题"]
    generated_script = step_map["生成脚本素材"]
    topic_id = getattr(pipeline, "topic_idea_id", None)
    if selected_topic.status == "已完成":
        if (
            type(topic_id) is not int
            or topic_id <= 0
            or _summary_id(selected_topic, "topic_idea_id") != topic_id
        ):
            return False
    script_id = getattr(pipeline, "script_asset_id", None)
    if generated_script.status == "已完成":
        if (
            type(script_id) is not int
            or script_id <= 0
            or _summary_id(generated_script, "script_asset_id") != script_id
        ):
            return False

    if script_id is not None:
        script = getattr(pipeline, "script_asset", None)
        if (
            script is None
            or getattr(script, "id", None) != script_id
            or getattr(script, "topic_idea_id", None)
            != getattr(pipeline, "topic_idea_id", None)
        ):
            return False
    try:
        return _is_strict_json(_pipeline_state_payload(pipeline))
    except (AttributeError, TypeError, ValueError):
        return False


def _final_decision(
    reason_code: str,
    message: str,
) -> PipelineCompletionDecision:
    return _decision(
        status="需要人工确认",
        reason_code=reason_code,
        message=message,
        current_step="成片验收",
        next_surface="finished",
        next_action=_FINAL_ACTION,
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX_DIGITS for character in value)
    )


def _parse_aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _effective_now(value: object | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value


def _validated_relative_path(raw: object, *, suffix: str) -> Path | None:
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        return None
    if (
        "\\" in raw
        or ":" in raw
        or "//" in raw
        or raw.startswith("/")
        or raw.endswith("/")
    ):
        return None
    parts = raw.split("/")
    if len(parts) < 2 or parts[0] != "成片":
        return None
    if any(part in {"", ".", ".."} for part in parts):
        return None
    if "/".join(parts) != raw or not parts[-1].endswith(suffix):
        return None
    if Path(parts[-1]).suffix != suffix:
        return None
    return Path(*parts)


def _stat_snapshot(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_mode,
        value.st_nlink,
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _cross_handle_snapshot(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    # Windows may expose creation time through path stat but mirror mtime through
    # fstat's ctime field. Compare ctime within each observation channel and all
    # other identity/mutation fields across path and handle observations.
    return (
        value.st_mode,
        value.st_nlink,
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
    )


def _safe_regular_snapshot(value: os.stat_result) -> bool:
    return stat.S_ISREG(value.st_mode) and value.st_nlink == 1


@contextmanager
def _open_guarded_read_handle(path: Path) -> Iterator[BinaryIO]:
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        generic_read = 0x80000000
        file_share_read = 0x00000001
        open_existing = 3
        file_attribute_normal = 0x00000080
        handle = create_file(
            str(path),
            generic_read,
            file_share_read,
            None,
            open_existing,
            file_attribute_normal,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            error = ctypes.get_last_error()
            raise OSError(error, "cannot open guarded file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        try:
            descriptor = msvcrt.open_osfhandle(handle, flags)
        except BaseException:
            close_handle(handle)
            raise
    else:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            yield stream
    except BaseException:
        raise


def _read_with_guard(
    pipeline: ProductionPipeline,
    relative: Path,
    *,
    system_drive: str | None,
    reader: Callable[[BinaryIO], _T],
) -> _T:
    try:
        project_use = begin_prepared_project_directory_use(
            pipeline,
            project_root=Path(pipeline.project_dir).parent,
            system_drive=system_drive,
        )
    except (OSError, ProjectIdentityError, ValueError) as exc:
        raise _GuardedFileUnsafe from exc
    try:
        directory_use = begin_prepared_project_child_directory_use(
            pipeline,
            project_root=project_use.project_root,
            relative_path=relative.parent,
            create=False,
            system_drive=system_drive,
        )
    except (OSError, ProjectIdentityError, ValueError) as exc:
        raise _GuardedFileUnsafe from exc
    expected = directory_use.path / relative.name
    existed_before = os.path.lexists(str(expected))
    try:
        path = prepared_project_output_path(
            directory_use,
            relative.name,
            must_exist=True,
        )
        before = path.lstat()
        if not _safe_regular_snapshot(before):
            raise _GuardedFileUnsafe
        with _open_guarded_read_handle(path) as stream:
            immediate = os.fstat(stream.fileno())
            if (
                not _safe_regular_snapshot(immediate)
                or _cross_handle_snapshot(immediate) != _cross_handle_snapshot(before)
            ):
                raise _GuardedFileUnsafe
            try:
                result = reader(stream)
            finally:
                after_handle = os.fstat(stream.fileno())
                after_path = path.lstat()
                if (
                    not _safe_regular_snapshot(after_handle)
                    or not _safe_regular_snapshot(after_path)
                    or _stat_snapshot(after_handle) != _stat_snapshot(immediate)
                    or _stat_snapshot(after_path) != _stat_snapshot(before)
                    or _cross_handle_snapshot(after_path)
                    != _cross_handle_snapshot(after_handle)
                ):
                    raise _GuardedFileUnsafe
                directory_use.verify()
        directory_use.verify()
        return result
    except _GuardedFileTooLarge:
        raise
    except _GuardedFileUnsafe:
        raise
    except (OSError, ProjectIdentityError, ValueError) as exc:
        if not existed_before:
            raise _GuardedFileMissing from exc
        raise _GuardedFileUnsafe from exc


def _stream_sha256(stream: BinaryIO) -> str:
    stream.seek(0)
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _is_bounded_iso_bmff(stream: BinaryIO) -> bool:
    file_size = os.fstat(stream.fileno()).st_size
    stream.seek(0)
    offset = 0
    found: set[bytes] = set()
    while offset < file_size:
        parsed = _read_bounded_box_header(stream, offset=offset, limit=file_size)
        if parsed is None:
            return False
        box_size, box_type, header_size = parsed
        payload_start = offset + header_size
        payload_size = box_size - header_size
        if box_type == b"ftyp":
            if payload_size < 8:
                return False
            found.add(box_type)
        elif box_type == b"moov":
            if not _has_bounded_child_box(
                stream,
                start=payload_start,
                limit=offset + box_size,
            ):
                return False
            found.add(box_type)
        elif box_type == b"mdat":
            if payload_size < 1:
                return False
            found.add(box_type)
        offset += box_size
    return offset == file_size and found == {b"ftyp", b"moov", b"mdat"}


def _read_bounded_box_header(
    stream: BinaryIO,
    *,
    offset: int,
    limit: int,
) -> tuple[int, bytes, int] | None:
    if limit - offset < 8:
        return None
    stream.seek(offset)
    header = stream.read(8)
    if len(header) != 8:
        return None
    box_size, box_type = struct.unpack(">I4s", header)
    header_size = 8
    if box_size == 0:
        return None
    if box_size == 1:
        extended = stream.read(8)
        if len(extended) != 8:
            return None
        box_size = struct.unpack(">Q", extended)[0]
        header_size = 16
    if box_size < header_size or box_size > limit - offset:
        return None
    return box_size, box_type, header_size


def _has_bounded_child_box(stream: BinaryIO, *, start: int, limit: int) -> bool:
    offset = start
    child_count = 0
    while offset < limit:
        parsed = _read_bounded_box_header(stream, offset=offset, limit=limit)
        if parsed is None:
            return False
        box_size, _box_type, _header_size = parsed
        child_count += 1
        offset += box_size
    return child_count > 0 and offset == limit


def _read_artifact(stream: BinaryIO) -> tuple[str, bool]:
    digest = _stream_sha256(stream)
    return digest, _is_bounded_iso_bmff(stream)


def _read_report(stream: BinaryIO) -> bytes:
    stream.seek(0)
    content = stream.read(_MAX_REPORT_BYTES + 1)
    if len(content) > _MAX_REPORT_BYTES:
        raise _GuardedFileTooLarge
    return content


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJsonError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise _StrictJsonError("non-finite number")


def _parse_strict_report(content: bytes) -> dict[str, Any] | None:
    if content.startswith(b"\xef\xbb\xbf"):
        return None
    try:
        text = content.decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _StrictJsonError,
        TypeError,
        RecursionError,
        OverflowError,
    ):
        return None
    if not isinstance(payload, dict) or not _is_strict_json(payload):
        return None
    return payload


def _compact_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _parse_quality_contract_gate_evidence(
    evidence: object,
) -> dict[str, Any] | None:
    """Parse the v2 fifth-gate evidence without trusting its pass flag."""

    if not _trimmed_nonempty_strings(evidence, allow_empty=False):
        return None
    bindings: dict[str, str] = {}
    reviews: dict[str, list[dict[str, Any]]] = {
        "must_preserve_review": [],
        "allowed_difference_review": [],
        "planned_exceed_review": [],
    }
    binding_names = {
        "quality_contract_id",
        "quality_contract_sha256",
        "quality_contract_path",
        "reference_pack",
    }
    for line in evidence:
        prefix, separator, payload = line.partition(":")
        if not separator:
            return None
        if prefix in binding_names:
            if prefix in bindings or not payload or payload != payload.strip():
                return None
            bindings[prefix] = payload
            continue
        if prefix not in reviews or not payload:
            return None
        try:
            parsed = json.loads(
                payload,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_json_constant,
            )
        except (
            json.JSONDecodeError,
            _StrictJsonError,
            TypeError,
            RecursionError,
            OverflowError,
        ):
            return None
        if (
            not isinstance(parsed, dict)
            or not _is_strict_json(parsed)
            or _compact_json(parsed) != payload
            or not _valid_quality_review_item(prefix, parsed)
        ):
            return None
        reviews[prefix].append(parsed)
    if set(bindings) != binding_names:
        return None
    if (
        not bindings["quality_contract_id"]
        or len(bindings["quality_contract_id"]) > 256
        or not _is_sha256(bindings["quality_contract_sha256"])
        or bindings["quality_contract_path"] != _QUALITY_CONTRACT_PATH
        or bindings["reference_pack"] not in {"none_supplied", "frozen"}
    ):
        return None
    if any(not items for items in reviews.values()):
        return None
    for items in reviews.values():
        ids = [item["id"] for item in items]
        if len(ids) != len(set(ids)):
            return None
    return {**bindings, **reviews}


def _valid_quality_review_item(prefix: str, item: dict[str, Any]) -> bool:
    item_id = item.get("id")
    if not _valid_quality_item_id(item_id):
        return False
    if prefix == "must_preserve_review":
        return (
            set(item) == {"id", "result", "evidence"}
            and item.get("result") in {"passed", "failed"}
            and _valid_review_text(item.get("evidence"))
        )
    if prefix == "allowed_difference_review":
        if set(item) != {"id", "result", "actual_difference", "evidence"}:
            return False
        outcome = item.get("result")
        actual = item.get("actual_difference")
        return (
            outcome in {"used", "not_used"}
            and _valid_review_text(actual, allow_empty=outcome == "not_used")
            and (outcome != "not_used" or actual == "")
            and _valid_review_text(item.get("evidence"))
        )
    if prefix == "planned_exceed_review":
        return (
            set(item) == {"id", "result", "difference", "evidence"}
            and item.get("result")
            in {"exceeded", "matched", "not_met", "not_comparable"}
            and _valid_review_text(item.get("difference"))
            and _valid_review_text(item.get("evidence"))
        )
    return False


def _valid_review_text(value: object, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and len(value) <= 1_000
        and (allow_empty or bool(value))
    )


def _valid_quality_item_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 2 <= len(value) <= 64
        and value[0].isalpha()
        and value[0].isascii()
        and all(character in _QUALITY_CONTRACT_ITEM_ID for character in value)
    )


def _read_quality_contract(stream: BinaryIO) -> bytes:
    stream.seek(0)
    content = stream.read(_MAX_QUALITY_CONTRACT_BYTES + 1)
    if len(content) > _MAX_QUALITY_CONTRACT_BYTES:
        raise _GuardedFileTooLarge
    return content


def _valid_quality_contract_document(contract: object) -> bool:
    if not isinstance(contract, dict) or set(contract) != {
        "format",
        "contract_id",
        "pipeline_id",
        "project_key",
        "baseline",
        "target",
        "reference_pack",
        "must_preserve",
        "allowed_differences",
        "planned_exceed_dimensions",
        "created_at",
        "source",
    }:
        return False
    if contract.get("format") != _QUALITY_CONTRACT_FORMAT:
        return False
    if type(contract.get("pipeline_id")) is not int or contract["pipeline_id"] <= 0:
        return False
    for key in ("contract_id", "project_key", "baseline", "source"):
        if not _valid_contract_text(contract.get(key), maximum=256):
            return False
    created_at = _parse_aware_datetime(contract.get("created_at"))
    if created_at is None:
        return False
    target = contract.get("target")
    if (
        not isinstance(target, dict)
        or set(target) != {"platform", "aspect_ratio", "duration_seconds"}
        or not _valid_contract_text(target.get("platform"), maximum=100)
        or target.get("aspect_ratio") != "9:16"
        or type(target.get("duration_seconds")) is not int
        or not 1 <= target["duration_seconds"] <= 600
    ):
        return False
    reference = contract.get("reference_pack")
    if not _valid_reference_pack(reference):
        return False
    if not _valid_contract_items(
        contract.get("must_preserve"),
        keys={"id", "label", "criterion"},
    ):
        return False
    if not _valid_contract_items(
        contract.get("allowed_differences"),
        keys={"id", "label"},
    ):
        return False
    if not _valid_contract_items(
        contract.get("planned_exceed_dimensions"),
        keys={"id", "label", "criterion", "required_result"},
        required_result="exceeded",
    ):
        return False
    all_ids = [
        item["id"]
        for key in ("must_preserve", "allowed_differences", "planned_exceed_dimensions")
        for item in contract[key]
    ]
    return len(all_ids) == len(set(all_ids))


def _valid_contract_text(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= maximum
    )


def _valid_contract_items(
    value: object,
    *,
    keys: set[str],
    required_result: str | None = None,
) -> bool:
    if not isinstance(value, list) or not value or len(value) > 16:
        return False
    ids: set[str] = set()
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != keys
            or not _valid_quality_item_id(item.get("id"))
            or item["id"] in ids
            or not _valid_contract_text(item.get("label"), maximum=200)
        ):
            return False
        if "criterion" in keys and not _valid_contract_text(
            item.get("criterion"), maximum=500
        ):
            return False
        if required_result is not None and item.get("required_result") != required_result:
            return False
        ids.add(item["id"])
    return True


def _valid_reference_pack(value: object) -> bool:
    if (
        not isinstance(value, dict)
        or set(value) != {"status", "items"}
        or value.get("status") not in {"none_supplied", "frozen"}
        or not isinstance(value.get("items"), list)
        or len(value["items"]) > 16
        or (value["status"] == "none_supplied" and value["items"])
        or (value["status"] == "frozen" and not value["items"])
    ):
        return False
    ids: set[str] = set()
    paths: set[str] = set()
    for item in value["items"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "project_relative_path", "sha256"}
            or not _valid_quality_item_id(item.get("id"))
            or item["id"] in ids
            or not _is_sha256(item.get("sha256"))
        ):
            return False
        relative = _validated_reference_path(item.get("project_relative_path"))
        if relative is None or relative.as_posix() in paths:
            return False
        ids.add(item["id"])
        paths.add(relative.as_posix())
    return True


def _validated_reference_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if (
        "\\" in value
        or ":" in value
        or "//" in value
        or value.startswith("/")
        or value.endswith("/")
    ):
        return None
    parts = value.split("/")
    if (
        len(parts) < 3
        or parts[:2] != ["质量合同", "references"]
        or any(part in {"", ".", ".."} for part in parts)
        or "/".join(parts) != value
    ):
        return None
    return Path(*parts)


def _quality_contract_v2_decision(
    pipeline: ProductionPipeline,
    gate: dict[str, Any],
    *,
    system_drive: str | None,
) -> PipelineCompletionDecision | None:
    parsed = _parse_quality_contract_gate_evidence(gate.get("evidence"))
    if parsed is None:
        return _final_decision(
            "quality_contract_review_invalid",
            "质量合同缺少完整的逐项复核证据。",
        )
    contract_relative = Path("质量合同") / "quality-contract-v1.json"
    try:
        contract_content = _read_with_guard(
            pipeline,
            contract_relative,
            system_drive=system_drive,
            reader=_read_quality_contract,
        )
    except _GuardedFileTooLarge:
        return _final_decision("quality_contract_invalid", "质量合同超过大小限制。")
    except _GuardedFileMissing:
        return _final_decision("quality_contract_missing", "质量合同不存在。")
    except _GuardedFileUnsafe:
        return _final_decision("quality_contract_unsafe", "质量合同无法安全读取。")
    actual_hash = hashlib.sha256(contract_content).hexdigest()
    if actual_hash != parsed["quality_contract_sha256"]:
        return _final_decision(
            "quality_contract_hash_mismatch",
            "质量合同已在复核后发生变化。",
        )
    contract = _parse_strict_report(contract_content)
    if contract is None or not _valid_quality_contract_document(contract):
        return _final_decision("quality_contract_invalid", "质量合同格式无效。")
    if (
        contract["pipeline_id"] != pipeline.id
        or contract["project_key"] != pipeline.project_key
        or contract["contract_id"] != parsed["quality_contract_id"]
        or contract["reference_pack"]["status"] != parsed["reference_pack"]
    ):
        return _final_decision(
            "quality_contract_mismatch",
            "质量合同与当前项目或复核证据不匹配。",
        )
    reference_decision = _verify_completion_references(
        pipeline,
        contract["reference_pack"],
        system_drive=system_drive,
    )
    if reference_decision is not None:
        return reference_decision
    if not _quality_review_matches_contract(contract, parsed):
        return _final_decision(
            "quality_contract_review_invalid",
            "质量合同逐项复核不完整或未达到约定结果。",
        )
    return None


def _verify_completion_references(
    pipeline: ProductionPipeline,
    reference_pack: dict[str, Any],
    *,
    system_drive: str | None,
) -> PipelineCompletionDecision | None:
    if reference_pack["status"] != "frozen":
        return None
    for item in reference_pack["items"]:
        relative = _validated_reference_path(item["project_relative_path"])
        if relative is None:
            return _final_decision("quality_contract_invalid", "冻结参考路径无效。")
        try:
            actual_hash = _read_with_guard(
                pipeline,
                relative,
                system_drive=system_drive,
                reader=_stream_sha256,
            )
        except _GuardedFileMissing:
            return _final_decision("reference_missing", "冻结参考文件不存在。")
        except _GuardedFileUnsafe:
            return _final_decision("reference_unsafe", "冻结参考文件无法安全读取。")
        if actual_hash != item["sha256"]:
            return _final_decision("reference_hash_mismatch", "冻结参考文件已发生变化。")
    return None


def _quality_review_matches_contract(
    contract: dict[str, Any],
    parsed: dict[str, Any],
) -> bool:
    groups = (
        ("must_preserve", "must_preserve_review"),
        ("allowed_differences", "allowed_difference_review"),
        ("planned_exceed_dimensions", "planned_exceed_review"),
    )
    for contract_key, review_key in groups:
        if {item["id"] for item in contract[contract_key]} != {
            item["id"] for item in parsed[review_key]
        }:
            return False
    if any(item["result"] != "passed" for item in parsed["must_preserve_review"]):
        return False
    required = {
        item["id"]: item["required_result"]
        for item in contract["planned_exceed_dimensions"]
    }
    return all(
        item["result"] == required[item["id"]]
        for item in parsed["planned_exceed_review"]
    )


def _trimmed_nonempty_strings(value: object, *, allow_empty: bool) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(
            isinstance(item, str) and bool(item) and item == item.strip()
            for item in value
        )
    )


def _valid_report_contract(report: dict[str, Any]) -> bool:
    if set(report) != _REPORT_KEYS:
        return False
    if report.get("report_format") != "auto-video-engine-quality-report-v1":
        return False
    if type(report.get("pipeline_id")) is not int or report["pipeline_id"] <= 0:
        return False
    if not _is_sha256(report.get("pipeline_state_sha256")) or not _is_sha256(
        report.get("artifact_sha256")
    ):
        return False
    if not isinstance(report.get("generated_at"), str):
        return False
    gates = report.get("gates")
    if not isinstance(gates, dict) or set(gates) != _GATE_NAMES:
        return False
    for gate in gates.values():
        if not isinstance(gate, dict) or set(gate) != {"status", "evidence"}:
            return False
        if gate.get("status") not in {"passed", "failed"}:
            return False
        if not _trimmed_nonempty_strings(gate.get("evidence"), allow_empty=False):
            return False
    return _trimmed_nonempty_strings(report.get("release_blockers"), allow_empty=True)


def _evaluate_evidence(
    pipeline: ProductionPipeline,
    evidence: object,
    *,
    now: object | None,
    system_drive: str | None,
) -> PipelineCompletionDecision:
    if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_KEYS:
        return _final_decision("evidence_invalid", "成片验收证据格式无效。")
    if not _is_strict_json(evidence):
        return _final_decision("evidence_invalid", "成片验收证据格式无效。")
    if (
        type(evidence.get("evidence_version")) is not int
        or evidence["evidence_version"] not in {1, 2}
    ):
        return _final_decision("evidence_invalid", "成片验收证据版本无效。")
    if not all(
        _is_sha256(evidence.get(key))
        for key in (
            "pipeline_state_sha256",
            "artifact_sha256",
            "quality_report_sha256",
        )
    ):
        return _final_decision("evidence_invalid", "成片验收摘要无效。")
    if not isinstance(evidence.get("artifact_relative_path"), str) or not isinstance(
        evidence.get("quality_report_relative_path"), str
    ):
        return _final_decision("evidence_invalid", "成片验收路径无效。")

    evaluation_time = _effective_now(now)
    approved_at = _parse_aware_datetime(evidence.get("approved_at"))
    if (
        evidence.get("approved_by") != "Ian"
        or approved_at is None
        or approved_at > evaluation_time + _MAX_FUTURE_SKEW
    ):
        return _final_decision("approval_invalid", "成片验收人或验收时间无效。")

    current_state_hash = pipeline_state_sha256(pipeline)
    if evidence["pipeline_state_sha256"] != current_state_hash:
        return _final_decision("pipeline_state_mismatch", "上游生产内容已在验收后改变。")

    artifact_relative = _validated_relative_path(
        evidence["artifact_relative_path"],
        suffix=".mp4",
    )
    if artifact_relative is None:
        return _final_decision("artifact_path_invalid", "成片文件路径不合法。")
    try:
        artifact_hash, artifact_is_valid = _read_with_guard(
            pipeline,
            artifact_relative,
            system_drive=system_drive,
            reader=_read_artifact,
        )
    except _GuardedFileMissing:
        return _final_decision("artifact_missing", "当前成片文件不存在。")
    except _GuardedFileUnsafe:
        return _final_decision("artifact_unsafe", "当前成片文件无法安全验收。")
    if not artifact_is_valid:
        return _final_decision("artifact_format_invalid", "当前文件不是可验收的 MP4 成片。")
    if artifact_hash != evidence["artifact_sha256"]:
        return _final_decision("artifact_hash_mismatch", "当前成片文件已发生变化。")

    report_relative = _validated_relative_path(
        evidence["quality_report_relative_path"],
        suffix=".json",
    )
    if report_relative is None:
        return _final_decision("quality_report_path_invalid", "质量报告路径不合法。")
    try:
        report_content = _read_with_guard(
            pipeline,
            report_relative,
            system_drive=system_drive,
            reader=_read_report,
        )
    except _GuardedFileTooLarge:
        return _final_decision("quality_report_too_large", "质量报告超过大小限制。")
    except _GuardedFileMissing:
        return _final_decision("quality_report_missing", "质量报告不存在。")
    except _GuardedFileUnsafe:
        return _final_decision("quality_report_unsafe", "质量报告无法安全验收。")

    if hashlib.sha256(report_content).hexdigest() != evidence["quality_report_sha256"]:
        return _final_decision("quality_report_hash_mismatch", "质量报告已发生变化。")
    report = _parse_strict_report(report_content)
    if report is None or not _valid_report_contract(report):
        return _final_decision("quality_report_invalid", "质量报告格式无效。")
    generated_at = _parse_aware_datetime(report["generated_at"])
    if (
        generated_at is None
        or generated_at > evaluation_time + _MAX_FUTURE_SKEW
        or generated_at > approved_at
    ):
        return _final_decision("quality_report_invalid", "质量报告时间无效。")
    if (
        report["pipeline_id"] != pipeline.id
        or report["pipeline_state_sha256"] != current_state_hash
        or report["artifact_sha256"] != artifact_hash
    ):
        return _final_decision("quality_report_mismatch", "质量报告与当前项目或成片不匹配。")
    if evidence["evidence_version"] == 2:
        contract_decision = _quality_contract_v2_decision(
            pipeline,
            report["gates"]["quality_contract"],
            system_drive=system_drive,
        )
        if contract_decision is not None:
            return contract_decision
    if any(gate["status"] == "failed" for gate in report["gates"].values()) or report[
        "release_blockers"
    ]:
        return _final_decision("quality_gate_failed", "成片仍有未通过的质量门。")
    return _decision(
        is_complete=True,
        status="已完成",
        reason_code="approved_artifact_verified",
        message="当前成片与五项质量门均已通过验收。",
        current_step="已完成",
        next_surface="none",
        next_action="",
    )


def evaluate_pipeline_completion(
    pipeline: ProductionPipeline,
    *,
    now: object | None = None,
    system_drive: str | None = None,
) -> PipelineCompletionDecision:
    if not _valid_preparation_structure(pipeline):
        return _invalid_preparation()

    steps = sorted(pipeline.steps, key=lambda item: item.step_order)
    failed = next((step for step in steps if step.status == "失败"), None)
    if failed is not None:
        return _decision(
            status="失败",
            reason_code="step_failed",
            message="生产步骤执行失败。",
            current_step=failed.name,
            next_surface="project",
            next_action="打开项目页修复失败步骤",
        )
    running = next((step for step in steps if step.status == "进行中"), None)
    if running is not None:
        return _decision(
            status="进行中",
            reason_code="step_running",
            message="生产步骤仍在执行。",
            current_step=running.name,
            next_surface="project",
            next_action="打开项目页查看进度",
        )
    waiting = next((step for step in steps if step.status == "需要人工确认"), None)
    if waiting is not None:
        return _decision(
            status="需要人工确认",
            reason_code="step_confirmation_required",
            message="生产步骤等待人工确认。",
            current_step=waiting.name,
            next_surface="project",
            next_action="打开项目页完成确认",
        )
    if all(step.status == "未开始" for step in steps):
        return _decision(
            status="未开始",
            reason_code="preparation_not_started",
            message="生产准备尚未开始。",
            current_step=PIPELINE_STEP_NAMES[0],
            next_surface="project",
            next_action="打开项目页开始生产",
        )
    unstarted = next((step for step in steps if step.status == "未开始"), None)
    if unstarted is not None:
        return _decision(
            status="进行中",
            reason_code="preparation_incomplete",
            message="生产准备尚未完成。",
            current_step=unstarted.name,
            next_surface="project",
            next_action="打开项目页继续生产",
        )

    evidence = getattr(pipeline, "completion_evidence", None)
    if evidence is None or evidence == {}:
        return _decision(
            status="需要人工确认",
            reason_code="approval_missing",
            message="生产准备已完成，但尚未验收成片。",
            current_step="成片验收",
            next_surface="finished",
            next_action=_FINAL_ACTION,
        )
    try:
        return _evaluate_evidence(
            pipeline,
            evidence,
            now=now,
            system_drive=system_drive,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        json.JSONDecodeError,
        RecursionError,
        OverflowError,
    ):
        return _final_decision("evidence_invalid", "成片验收证据无法安全验证。")
