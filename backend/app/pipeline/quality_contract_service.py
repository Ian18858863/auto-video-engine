"""Immutable project-local quality contracts and structured review evidence."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, BinaryIO, Mapping
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_configured_path
from app.models import ProductionPipeline
from app.pipeline.completion_policy import (
    _GuardedFileMissing,
    _GuardedFileTooLarge,
    _GuardedFileUnsafe,
    _is_strict_json,
    _read_with_guard,
)
from app.pipeline.project_identity import (
    ProjectIdentityError,
    begin_prepared_project_child_directory_use,
    begin_prepared_project_directory_use,
    prepared_project_output_path,
)


CONTRACT_FORMAT = "auto-video-engine-quality-contract-v1"
CONTRACT_RELATIVE_PATH = PurePosixPath("质量合同", "quality-contract-v1.json")
_CONTRACT_DIR = Path("质量合同")
_CONTRACT_FILENAME = "quality-contract-v1.json"
_MAX_CONTRACT_BYTES = 256 * 1024
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_ITEM_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

_MUST_PRESERVE = (
    (
        "facts_traceable",
        "事实可追溯",
        "每个关键事实都能追溯到项目内保存的原始证据或来源记录",
    ),
    (
        "rights_auditable",
        "权利可审计",
        "每项正式素材都记录来源、许可状态与本次用途，未知权利不得通过",
    ),
    (
        "visual_matches_narration",
        "画面匹配旁白",
        "每个事实性旁白都有同语义画面，不用气氛素材冒充事实证据",
    ),
    (
        "mobile_readable_audio_clear",
        "移动端可读且声音清楚",
        "在 375×812 竖屏视口播放时字幕可读、关键画面无遮挡且人声清楚",
    ),
)
_ALLOWED_DIFFERENCES = (
    ("audio_first_timing", "允许按实测旁白时长调整镜头秒数"),
    ("equivalent_cleared_material", "允许替换为同语义且已清权的素材"),
    ("nonsemantic_motion_adjustment", "允许在不改变事实含义时调整动画与转场节奏"),
)
_PLANNED_EXCEED = (
    (
        "information_clarity",
        "信息清晰度",
        "核心观点、依据与结论比统一 R1 基线更易在一次播放中理解",
    ),
    (
        "evidence_traceability",
        "证据可追溯性",
        "关键事实的来源线索比统一 R1 基线更完整且更容易复核",
    ),
    (
        "mobile_readability",
        "移动端可读性",
        "375×812 视口下的字幕层级与关键信息辨识度优于统一 R1 基线",
    ),
)


class QualityContractError(ValueError):
    """The quality contract is missing, changed, or reviewed incorrectly."""


@dataclass(frozen=True, slots=True)
class QualityContractSnapshot:
    pipeline_id: int
    relative_path: str
    sha256: str
    contract: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline_id": self.pipeline_id,
            "quality_contract_relative_path": self.relative_path,
            "quality_contract_sha256": self.sha256,
            "contract": deepcopy(self.contract),
        }


def ensure_default_quality_contract(
    session: Session,
    pipeline_id: int,
    *,
    project_root: Path | None = None,
) -> QualityContractSnapshot:
    """Create the unified R1 baseline once, or verify the existing snapshot."""

    pipeline, approved_root = _pipeline_context(
        session,
        pipeline_id,
        project_root=project_root,
    )
    expected_contract = _default_contract(pipeline)
    expected_bytes = _canonical_json(expected_contract)
    try:
        directory_use = begin_prepared_project_child_directory_use(
            pipeline,
            project_root=approved_root,
            relative_path=_CONTRACT_DIR,
            create=True,
        )
        path = prepared_project_output_path(
            directory_use,
            _CONTRACT_FILENAME,
            must_exist=False,
        )
        if not os.path.lexists(path):
            _publish_immutable_bytes(path, expected_bytes)
        directory_use.verify()
    except (OSError, ProjectIdentityError, ValueError) as exc:
        raise QualityContractError("quality contract could not be frozen safely") from exc

    snapshot = get_quality_contract(
        session,
        pipeline_id,
        project_root=approved_root,
    )
    if snapshot.contract != expected_contract:
        raise QualityContractError(
            "quality contract changed; immutable baseline will not be overwritten"
        )
    return snapshot


def get_quality_contract(
    session: Session,
    pipeline_id: int,
    *,
    project_root: Path | None = None,
) -> QualityContractSnapshot:
    pipeline, _approved_root = _pipeline_context(
        session,
        pipeline_id,
        project_root=project_root,
    )
    try:
        content = _read_with_guard(
            pipeline,
            Path(*CONTRACT_RELATIVE_PATH.parts),
            system_drive=None,
            reader=_read_contract,
        )
    except _GuardedFileMissing as exc:
        raise QualityContractError("quality contract is missing") from exc
    except _GuardedFileTooLarge as exc:
        raise QualityContractError("quality contract exceeds the size limit") from exc
    except _GuardedFileUnsafe as exc:
        raise QualityContractError("quality contract cannot be read safely") from exc
    contract = _parse_contract(content)
    if int(contract["pipeline_id"]) != int(pipeline.id):
        raise QualityContractError("quality contract pipeline identity is invalid")
    if contract["project_key"] != pipeline.project_key:
        raise QualityContractError("quality contract project identity is invalid")
    _verify_frozen_references(pipeline, contract)
    return QualityContractSnapshot(
        pipeline_id=int(pipeline.id),
        relative_path=CONTRACT_RELATIVE_PATH.as_posix(),
        sha256=hashlib.sha256(content).hexdigest(),
        contract=contract,
    )


def evaluate_quality_contract_review(
    session: Session,
    pipeline_id: int,
    review: Mapping[str, Any] | None,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Validate a per-item review and return one five-gate-compatible result."""

    if not isinstance(review, Mapping):
        return {
            "status": "failed",
            "evidence": ["quality_contract_review_required"],
        }
    expected_review_keys = {
        "contract_sha256",
        "must_preserve_results",
        "allowed_difference_results",
        "planned_exceed_results",
    }
    if set(review) != expected_review_keys:
        raise QualityContractError("quality contract review schema is invalid")
    snapshot = get_quality_contract(
        session,
        pipeline_id,
        project_root=project_root,
    )
    supplied_hash = review.get("contract_sha256")
    if not isinstance(supplied_hash, str) or not _HEX_64.fullmatch(supplied_hash):
        raise QualityContractError("quality contract review hash is invalid")
    if supplied_hash != snapshot.sha256:
        raise QualityContractError("quality contract review hash does not match frozen contract")

    preserve_items = snapshot.contract["must_preserve"]
    expected_preserve_ids = {item["id"] for item in preserve_items}
    raw_results = review.get("must_preserve_results")
    if not isinstance(raw_results, Mapping) or set(raw_results) != expected_preserve_ids:
        raise QualityContractError(
            "quality contract review must cover every must-preserve item exactly once"
        )

    normalized_preserve: list[dict[str, str]] = []
    for item in preserve_items:
        item_id = item["id"]
        result = raw_results.get(item_id)
        if not isinstance(result, Mapping) or set(result) != {"result", "evidence"}:
            raise QualityContractError(f"quality contract result is invalid: {item_id}")
        status = result.get("result")
        evidence = _review_text(result.get("evidence"), label=f"must preserve {item_id}")
        if status not in {"passed", "failed"}:
            raise QualityContractError(f"quality contract status is invalid: {item_id}")
        normalized_preserve.append(
            {"id": item_id, "result": status, "evidence": evidence}
        )

    allowed_items = snapshot.contract["allowed_differences"]
    raw_allowed = _exact_result_mapping(
        review.get("allowed_difference_results"),
        expected={item["id"] for item in allowed_items},
        label="allowed difference",
    )
    normalized_allowed: list[dict[str, str]] = []
    for item in allowed_items:
        item_id = item["id"]
        result = raw_allowed[item_id]
        if not isinstance(result, Mapping) or set(result) != {
            "result",
            "actual_difference",
            "evidence",
        }:
            raise QualityContractError(f"allowed difference result is invalid: {item_id}")
        outcome = result.get("result")
        if outcome not in {"used", "not_used"}:
            raise QualityContractError(f"allowed difference outcome is invalid: {item_id}")
        actual = _review_text(
            result.get("actual_difference"),
            label=f"allowed difference actual {item_id}",
            allow_empty=outcome == "not_used",
        )
        if outcome == "not_used" and actual:
            raise QualityContractError(
                f"unused allowed difference must not claim an actual difference: {item_id}"
            )
        evidence_text = _review_text(
            result.get("evidence"),
            label=f"allowed difference evidence {item_id}",
        )
        normalized_allowed.append(
            {
                "id": item_id,
                "result": outcome,
                "actual_difference": actual,
                "evidence": evidence_text,
            }
        )

    exceed_items = snapshot.contract["planned_exceed_dimensions"]
    raw_exceed = _exact_result_mapping(
        review.get("planned_exceed_results"),
        expected={item["id"] for item in exceed_items},
        label="planned exceed",
    )
    normalized_exceed: list[dict[str, str]] = []
    for item in exceed_items:
        item_id = item["id"]
        result = raw_exceed[item_id]
        if not isinstance(result, Mapping) or set(result) != {
            "result",
            "difference",
            "evidence",
        }:
            raise QualityContractError(f"planned exceed result is invalid: {item_id}")
        outcome = result.get("result")
        if outcome not in {"exceeded", "matched", "not_met", "not_comparable"}:
            raise QualityContractError(f"planned exceed outcome is invalid: {item_id}")
        normalized_exceed.append(
            {
                "id": item_id,
                "result": outcome,
                "difference": _review_text(
                    result.get("difference"),
                    label=f"planned exceed difference {item_id}",
                ),
                "evidence": _review_text(
                    result.get("evidence"),
                    label=f"planned exceed evidence {item_id}",
                ),
            }
        )
    reference_status = snapshot.contract["reference_pack"]["status"]
    evidence = [
        f"quality_contract_id:{snapshot.contract['contract_id']}",
        f"quality_contract_sha256:{snapshot.sha256}",
        f"quality_contract_path:{snapshot.relative_path}",
        f"reference_pack:{reference_status}",
    ]
    evidence.extend(_review_evidence("must_preserve_review", item) for item in normalized_preserve)
    evidence.extend(
        _review_evidence("allowed_difference_review", item)
        for item in normalized_allowed
    )
    evidence.extend(
        _review_evidence("planned_exceed_review", item)
        for item in normalized_exceed
    )
    return {
        "status": (
            "passed"
            if all(item["result"] == "passed" for item in normalized_preserve)
            and all(
                result["result"] == contract_item["required_result"]
                for result, contract_item in zip(
                    normalized_exceed,
                    exceed_items,
                    strict=True,
                )
            )
            else "failed"
        ),
        "evidence": evidence,
    }


def _pipeline_context(
    session: Session,
    pipeline_id: int,
    *,
    project_root: Path | None,
) -> tuple[ProductionPipeline, Path]:
    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None:
        raise QualityContractError(f"pipeline not found: {pipeline_id}")
    if not str(pipeline.project_dir or "").strip():
        raise QualityContractError("pipeline project directory is missing")
    approved_root = resolve_configured_path(
        project_root or get_settings().project_root_dir
    )
    try:
        project_use = begin_prepared_project_directory_use(
            pipeline,
            project_root=approved_root,
        )
        project_use.verify()
    except (OSError, ProjectIdentityError, ValueError) as exc:
        raise QualityContractError("quality contract project identity check failed") from exc
    return pipeline, approved_root


def _default_contract(pipeline: ProductionPipeline) -> dict[str, Any]:
    created_at = pipeline.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    else:
        created_at = created_at.astimezone(timezone.utc)
    return {
        "format": CONTRACT_FORMAT,
        "contract_id": f"qc-{pipeline.project_key}-r1",
        "pipeline_id": int(pipeline.id),
        "project_key": pipeline.project_key,
        "baseline": "r1-unified",
        "target": {
            "platform": "抖音",
            "aspect_ratio": "9:16",
            "duration_seconds": 60,
        },
        "reference_pack": {
            "status": "none_supplied",
            "items": [],
        },
        "must_preserve": [
            {"id": item_id, "label": label, "criterion": criterion}
            for item_id, label, criterion in _MUST_PRESERVE
        ],
        "allowed_differences": _items(_ALLOWED_DIFFERENCES),
        "planned_exceed_dimensions": [
            {
                "id": item_id,
                "label": label,
                "criterion": criterion,
                "required_result": "exceeded",
            }
            for item_id, label, criterion in _PLANNED_EXCEED
        ],
        "created_at": created_at.isoformat(),
        "source": "master-refactor-phase1-unified-baseline",
    }


def _items(rows: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
    return [{"id": item_id, "label": label} for item_id, label in rows]


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def _publish_immutable_bytes(path: Path, payload: bytes) -> None:
    """Publish complete bytes without ever exposing a half-written contract."""

    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    published = False
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            if os.name == "nt":
                # Windows rename is a native no-replace publication when the
                # destination already exists.
                os.rename(temporary, path)
            else:  # pragma: no cover - production is Windows.
                # link() supplies the same no-replace boundary on POSIX.
                os.link(temporary, path)
                temporary.unlink()
            published = True
        except FileExistsError:
            return
    finally:
        if not published and os.path.lexists(temporary):
            try:
                temporary.unlink()
            except OSError:
                pass


def _read_contract(stream: BinaryIO) -> bytes:
    stream.seek(0)
    content = stream.read(_MAX_CONTRACT_BYTES + 1)
    if len(content) > _MAX_CONTRACT_BYTES:
        raise _GuardedFileTooLarge
    if not content:
        raise ValueError("quality contract is empty")
    return content


def _parse_contract(content: bytes) -> dict[str, Any]:
    if content.startswith(b"\xef\xbb\xbf"):
        raise QualityContractError("quality contract JSON is invalid")

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        contract = json.loads(
            content.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise QualityContractError("quality contract JSON is invalid") from exc
    if not isinstance(contract, dict) or not _is_strict_json(contract):
        raise QualityContractError("quality contract JSON is invalid")
    _validate_contract_shape(contract)
    return contract


def _validate_contract_shape(contract: dict[str, Any]) -> None:
    expected_keys = {
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
    }
    if set(contract) != expected_keys or contract.get("format") != CONTRACT_FORMAT:
        raise QualityContractError("quality contract schema is invalid")
    if type(contract.get("pipeline_id")) is not int or contract["pipeline_id"] <= 0:
        raise QualityContractError("quality contract pipeline id is invalid")
    for key in ("contract_id", "project_key", "baseline", "created_at", "source"):
        if not isinstance(contract.get(key), str) or not contract[key].strip():
            raise QualityContractError(f"quality contract {key} is invalid")
    target = contract.get("target")
    if (
        not isinstance(target, dict)
        or set(target) != {"platform", "aspect_ratio", "duration_seconds"}
        or not isinstance(target.get("platform"), str)
        or target.get("aspect_ratio") != "9:16"
        or type(target.get("duration_seconds")) is not int
        or not 1 <= target["duration_seconds"] <= 600
    ):
        raise QualityContractError("quality contract target is invalid")
    reference = contract.get("reference_pack")
    if (
        not isinstance(reference, dict)
        or set(reference) != {"status", "items"}
        or reference.get("status") not in {"none_supplied", "frozen"}
        or not isinstance(reference.get("items"), list)
        or (reference["status"] == "none_supplied" and reference["items"])
        or (reference["status"] == "frozen" and not reference["items"])
    ):
        raise QualityContractError("quality contract reference pack is invalid")
    _validate_reference_items(reference["items"])
    _validate_items(
        contract.get("must_preserve"),
        "must preserve",
        exact_keys={"id", "label", "criterion"},
    )
    _validate_items(contract.get("allowed_differences"), "allowed differences")
    _validate_items(
        contract.get("planned_exceed_dimensions"),
        "planned exceed dimensions",
        exact_keys={"id", "label", "criterion", "required_result"},
        required_result="exceeded",
    )
    all_ids = [
        item["id"]
        for key in ("must_preserve", "allowed_differences", "planned_exceed_dimensions")
        for item in contract[key]
    ]
    if len(all_ids) != len(set(all_ids)):
        raise QualityContractError("quality contract item ids must be globally unique")


def _validate_items(
    value: Any,
    label: str,
    *,
    exact_keys: set[str] | None = None,
    required_result: str | None = None,
) -> None:
    if not isinstance(value, list) or not value or len(value) > 16:
        raise QualityContractError(f"quality contract {label} are invalid")
    keys = exact_keys or {"id", "label"}
    ids: set[str] = set()
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != keys
            or not isinstance(item.get("id"), str)
            or not _ITEM_ID.fullmatch(item["id"])
            or not isinstance(item.get("label"), str)
            or not item["label"].strip()
            or len(item["label"].strip()) > 200
            or item["id"] in ids
        ):
            raise QualityContractError(f"quality contract {label} are invalid")
        if "criterion" in keys and (
            not isinstance(item.get("criterion"), str)
            or not item["criterion"].strip()
            or len(item["criterion"].strip()) > 500
        ):
            raise QualityContractError(f"quality contract {label} are invalid")
        if required_result is not None and item.get("required_result") != required_result:
            raise QualityContractError(f"quality contract {label} are invalid")
        ids.add(item["id"])


def _validate_reference_items(value: list[Any]) -> None:
    if len(value) > 16:
        raise QualityContractError("quality contract reference pack is invalid")
    ids: set[str] = set()
    paths: set[str] = set()
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "project_relative_path", "sha256"}
            or not isinstance(item.get("id"), str)
            or not _ITEM_ID.fullmatch(item["id"])
            or item["id"] in ids
            or not _HEX_64.fullmatch(str(item.get("sha256") or ""))
        ):
            raise QualityContractError("quality contract reference pack is invalid")
        relative = _validated_reference_path(item.get("project_relative_path"))
        if relative is None or relative in paths:
            raise QualityContractError("quality contract reference pack is invalid")
        ids.add(item["id"])
        paths.add(relative)


def _validated_reference_path(value: Any) -> str | None:
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
    parts = PurePosixPath(value).parts
    if (
        len(parts) < 3
        or parts[:2] != ("质量合同", "references")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return None
    return PurePosixPath(*parts).as_posix()


def _verify_frozen_references(
    pipeline: ProductionPipeline,
    contract: Mapping[str, Any],
) -> None:
    reference_pack = contract["reference_pack"]
    if reference_pack["status"] != "frozen":
        return
    for item in reference_pack["items"]:
        relative = _validated_reference_path(item["project_relative_path"])
        if relative is None:
            raise QualityContractError("quality contract reference path is invalid")
        try:
            actual = _read_with_guard(
                pipeline,
                Path(*PurePosixPath(relative).parts),
                system_drive=None,
                reader=_stream_sha256,
            )
        except _GuardedFileMissing as exc:
            raise QualityContractError("quality contract reference is missing") from exc
        except _GuardedFileUnsafe as exc:
            raise QualityContractError("quality contract reference is unsafe") from exc
        if actual != item["sha256"]:
            raise QualityContractError("quality contract reference hash does not match")


def _stream_sha256(stream: BinaryIO) -> str:
    stream.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _exact_result_mapping(
    value: Any,
    *,
    expected: set[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise QualityContractError(
            f"quality contract {label} review must cover every item exactly once"
        )
    return value


def _review_text(
    value: Any,
    *,
    label: str,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise QualityContractError(f"quality contract {label} is invalid")
    normalized = value.strip()
    if len(normalized) > 1_000 or (not allow_empty and not normalized):
        raise QualityContractError(f"quality contract {label} is invalid")
    return normalized


def _review_evidence(prefix: str, payload: Mapping[str, str]) -> str:
    return prefix + ":" + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "CONTRACT_FORMAT",
    "QualityContractError",
    "QualityContractSnapshot",
    "ensure_default_quality_contract",
    "evaluate_quality_contract_review",
    "get_quality_contract",
]
