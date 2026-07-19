"""Append-only ScriptVersion storage for the first golden-project slice.

The current legacy schema is intentionally frozen.  Until its next approved
Alembic revision, script versions live as immutable, identity-guarded JSON
snapshots inside the D: project directory.  Approval and invalidation are
append-only event files; no approved snapshot is edited in place.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from app.core.windows_named_mutex import (
    WindowsMutexBusy,
    WindowsNamedMutex,
    build_mutex_name,
)
from app.pipeline.project_identity import (
    PreparedProjectChildDirectoryUse,
    ProjectIdentityError,
    begin_prepared_project_child_directory_use,
    begin_prepared_project_directory_use,
    prepared_project_output_path,
)
from app.pipeline.shot_director import build_shot_plan


SCRIPT_VERSION_ROOT = "脚本版本"
LEGACY_SCRIPT_VERSION_SCHEMA = 1
SCRIPT_VERSION_SCHEMA = 2
SCRIPT_EVENT_SCHEMA = 2
SCRIPT_OPERATION_SCHEMA = 1
_VERSION_FILENAME_PATTERN = "sv-*.json"
_EVENT_FILENAME_PATTERN = "event-*.json"
_OPERATION_INTENT_FILENAME_PATTERN = "operation-*-intent.json"
_OPERATION_OUTCOME_FILENAME_PATTERN = "operation-*-outcome-*.json"
_OPERATION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_OPERATION_KINDS = frozenset({"import", "approve", "draft", "restore"})
_OPERATION_OUTCOMES = frozenset({"completed", "compensated", "abandoned"})


class ScriptVersionError(ValueError):
    """Raised when a ScriptVersion cannot be safely read or written."""


@dataclass(frozen=True, slots=True)
class ScriptVersionSnapshot:
    pipeline_id: int
    project_key: str
    version_id: str
    version_number: int
    input_hash: str
    content_hash: str
    payload: dict[str, Any]
    fact_sources: tuple[dict[str, Any], ...]
    shot_plan: tuple[dict[str, Any], ...]
    source: str
    created_at: str
    schema_version: int = SCRIPT_VERSION_SCHEMA
    input_evidence: dict[str, Any] | None = None
    shot_plan_hash: str = ""
    snapshot_hash: str = ""
    integrity_status: str = "full_envelope_verified"
    status: str = "draft"
    approved_at: str = ""
    invalidated_at: str = ""
    invalidated_reason: str = ""
    approval_integrity_status: str = "none"
    approval_event_hash: str = ""
    approval_evidence_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "pipeline_id": self.pipeline_id,
            "project_key": self.project_key,
            "version_id": self.version_id,
            "version_number": self.version_number,
            "input_hash": self.input_hash,
            "content_hash": self.content_hash,
            "payload": self.payload,
            "fact_sources": list(self.fact_sources),
            "shot_plan": list(self.shot_plan),
            "source": self.source,
            "created_at": self.created_at,
        }
        if self.schema_version >= SCRIPT_VERSION_SCHEMA:
            result.update(
                {
                    "input_evidence": dict(self.input_evidence or {}),
                    "shot_plan_hash": self.shot_plan_hash,
                    "snapshot_hash": self.snapshot_hash,
                }
            )
        return result

    def public_dict(self) -> dict[str, Any]:
        result = self.to_dict()
        result.update(
            {
                "status": self.status,
                "approved_at": self.approved_at,
                "invalidated_at": self.invalidated_at,
                "invalidated_reason": self.invalidated_reason,
                "integrity_status": self.integrity_status,
                "approval_integrity_status": self.approval_integrity_status,
                "approval_event_hash": self.approval_event_hash,
                "approval_evidence_hash": self.approval_evidence_hash,
            }
        )
        return result


@dataclass(frozen=True, slots=True)
class ScriptVersionOperation:
    """One durable file/DB reconciliation intent and its terminal outcome."""

    operation_id: str
    kind: str
    version_id: str
    context: dict[str, Any]
    created_at: str
    intent_hash: str
    status: str = "pending"
    outcome_at: str = ""
    result: dict[str, Any] | None = None
    outcome_hash: str = ""


class ScriptVersionLedger:
    """Read and append immutable versions for one pipeline identity."""

    def __init__(self, pipeline: Any, *, project_root: Path):
        try:
            pipeline_id = int(getattr(pipeline, "id"))
        except (TypeError, ValueError) as exc:
            raise ScriptVersionError("脚本版本缺少有效 pipeline_id") from exc
        if pipeline_id < 1:
            raise ScriptVersionError("脚本版本缺少有效 pipeline_id")
        self.pipeline = pipeline
        self.pipeline_id = pipeline_id
        self.project_root = Path(project_root)
        if not self.project_root.is_absolute():
            raise ScriptVersionError("脚本版本 project_root 必须是绝对路径")
        self.project_key = str(getattr(pipeline, "project_key", "") or "")
        if not self.project_key:
            raise ScriptVersionError("脚本版本缺少 project_key")

    def create(
        self,
        payload: Mapping[str, Any],
        *,
        fact_sources: Sequence[Mapping[str, Any]] | None = None,
        source: str = "script_factory",
        input_evidence: Mapping[str, Any] | None = None,
    ) -> ScriptVersionSnapshot:
        normalized_payload = _normalize_payload(payload)
        normalized_sources = _normalize_fact_sources(fact_sources or [])
        normalized_evidence = _normalize_input_evidence(input_evidence or {})
        content_hash = _sha256(normalized_payload)
        input_hash = _sha256(
            {
                "payload": normalized_payload,
                "fact_sources": normalized_sources,
                "input_evidence": normalized_evidence,
            }
        )
        shot_plan = tuple(build_shot_plan(normalized_payload))
        shot_plan_hash = _sha256(list(shot_plan))
        source_label = _safe_label(source, "script_factory")
        created_at = _now_iso()

        with self._acquire_write_mutex():
            versions = self.list()
            existing = (
                versions[-1]
                if versions and versions[-1].input_hash == input_hash
                else None
            )
            if existing is not None:
                snapshot = existing
            else:
                versions_use = self._directory_use("versions", create=True)
                version_number = max(
                    (item.version_number for item in versions), default=0
                ) + 1
                version_id = (
                    f"sv-{self.pipeline_id}-{version_number:04d}-{content_hash[:12]}"
                )
                snapshot = ScriptVersionSnapshot(
                    pipeline_id=self.pipeline_id,
                    project_key=self.project_key,
                    version_id=version_id,
                    version_number=version_number,
                    input_hash=input_hash,
                    content_hash=content_hash,
                    payload=normalized_payload,
                    fact_sources=tuple(normalized_sources),
                    shot_plan=shot_plan,
                    source=source_label,
                    created_at=created_at,
                    input_evidence=normalized_evidence,
                    shot_plan_hash=shot_plan_hash,
                )
                snapshot = _replace(
                    snapshot,
                    snapshot_hash=_snapshot_hash(snapshot),
                )
                _write_exclusive_snapshot(versions_use, snapshot)

            return snapshot

    def list(self) -> list[ScriptVersionSnapshot]:
        if not self._child_exists("versions"):
            return []
        versions_use = self._directory_use("versions", create=False)
        if not versions_use.path.exists():
            return []
        snapshots: list[ScriptVersionSnapshot] = []
        for path in sorted(versions_use.path.glob(_VERSION_FILENAME_PATTERN)):
            if not path.is_file() or path.is_symlink():
                raise ScriptVersionError("脚本版本文件不是普通文件")
            versions_use.verify()
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ScriptVersionError("脚本版本文件无法读取") from exc
            snapshot = _snapshot_from_dict(raw)
            if (
                snapshot.pipeline_id != self.pipeline_id
                or snapshot.project_key != self.project_key
            ):
                raise ScriptVersionError("脚本版本项目身份与当前项目不一致")
            snapshots.append(snapshot)
        version_numbers = [item.version_number for item in snapshots]
        if len(version_numbers) != len(set(version_numbers)):
            raise ScriptVersionError("脚本版本号重复")
        return _apply_events(
            sorted(snapshots, key=lambda item: item.version_number),
            self._read_events(),
        )

    def get(self, version_id: str) -> ScriptVersionSnapshot:
        for item in self.list():
            if item.version_id == version_id:
                return item
        raise ScriptVersionError("脚本版本不存在")

    def approve(
        self,
        version_id: str,
        *,
        approval_evidence: Mapping[str, Any] | None = None,
        operation_id: str = "",
    ) -> ScriptVersionSnapshot:
        evidence_hash = (
            _sha256(_json_value(dict(approval_evidence)))
            if approval_evidence is not None
            else ""
        )
        with self._acquire_write_mutex():
            snapshot = self.get(version_id)
            if snapshot.status == "invalidated":
                raise ScriptVersionError("失效脚本版本不能批准")
            if snapshot.status == "approved":
                if evidence_hash and snapshot.approval_evidence_hash != evidence_hash:
                    raise ScriptVersionError("脚本版本已绑定另一份批准证据，不能覆盖")
                return snapshot
            self._append_event(
                {
                    "event": "approved",
                    "version_id": snapshot.version_id,
                    "at": _now_iso(),
                    "operation_id": operation_id,
                    "approval_evidence_hash": evidence_hash,
                }
            )
            return self.get(version_id)

    def invalidate(
        self,
        version_id: str,
        *,
        reason: str,
        operation_id: str = "",
    ) -> ScriptVersionSnapshot:
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ScriptVersionError("脚本版本失效必须说明原因")
        with self._acquire_write_mutex():
            snapshot = self.get(version_id)
            if snapshot.status == "invalidated":
                return snapshot
            self._append_event(
                {
                    "event": "invalidated",
                    "version_id": snapshot.version_id,
                    "reason": clean_reason[:500],
                    "at": _now_iso(),
                    "operation_id": operation_id,
                }
            )
            return self.get(version_id)

    def latest_approved(self) -> ScriptVersionSnapshot | None:
        approved = [item for item in self.list() if item.status == "approved"]
        return approved[-1] if approved else None

    def begin_operation(
        self,
        *,
        operation_id: str,
        kind: str,
        version_id: str,
        context: Mapping[str, Any] | None = None,
    ) -> ScriptVersionOperation:
        """Persist an idempotent intent before crossing the file/DB boundary."""

        clean_operation_id = _require_operation_id(operation_id)
        clean_kind = str(kind or "").strip()
        if clean_kind not in _OPERATION_KINDS:
            raise ScriptVersionError("脚本版本 operation 类型无效")
        snapshot = self.get(version_id)
        normalized_context = _normalize_operation_mapping(context or {})
        if normalized_context.get("version_snapshot_hash") not in {
            None,
            "",
            snapshot.snapshot_hash,
        }:
            raise ScriptVersionError("脚本版本 operation 与目标快照哈希不一致")

        with self._acquire_write_mutex():
            existing = self._operation_by_id(clean_operation_id)
            if existing is not None:
                if (
                    existing.kind != clean_kind
                    or existing.version_id != snapshot.version_id
                    or existing.context != normalized_context
                ):
                    raise ScriptVersionError(
                        "同一 operation_id 已绑定另一份脚本版本操作"
                    )
                return existing

            created_at = _now_iso()
            record = {
                "schema_version": SCRIPT_OPERATION_SCHEMA,
                "record_type": "intent",
                "pipeline_id": self.pipeline_id,
                "project_key": self.project_key,
                "operation_id": clean_operation_id,
                "kind": clean_kind,
                "version_id": snapshot.version_id,
                "context": normalized_context,
                "created_at": created_at,
            }
            record["intent_hash"] = _sha256(record)
            operations_use = self._directory_use("operations", create=True)
            path = prepared_project_output_path(
                operations_use,
                f"operation-{clean_operation_id}-intent.json",
            )
            _write_exclusive_record(
                path,
                record,
                collision_message="脚本版本 operation intent 发生碰撞",
            )
            operations_use.verify()
            return _operation_from_records(record, None)

    def finish_operation(
        self,
        operation_id: str,
        *,
        outcome: str,
        result: Mapping[str, Any] | None = None,
    ) -> ScriptVersionOperation:
        """Append one terminal outcome; retries return the exact existing record."""

        clean_operation_id = _require_operation_id(operation_id)
        clean_outcome = str(outcome or "").strip()
        if clean_outcome not in _OPERATION_OUTCOMES:
            raise ScriptVersionError("脚本版本 operation 结果无效")
        normalized_result = _normalize_operation_mapping(result or {})

        with self._acquire_write_mutex():
            existing = self._operation_by_id(clean_operation_id)
            if existing is None:
                raise ScriptVersionError("脚本版本 operation intent 不存在")
            if existing.status != "pending":
                if (
                    existing.status != clean_outcome
                    or dict(existing.result or {}) != normalized_result
                ):
                    raise ScriptVersionError(
                        "脚本版本 operation 已有不同的终态，不能覆盖"
                    )
                return existing

            outcome_id = uuid4().hex
            record = {
                "schema_version": SCRIPT_OPERATION_SCHEMA,
                "record_type": "outcome",
                "pipeline_id": self.pipeline_id,
                "project_key": self.project_key,
                "operation_id": clean_operation_id,
                "intent_hash": existing.intent_hash,
                "outcome_id": outcome_id,
                "outcome": clean_outcome,
                "result": normalized_result,
                "at": _now_iso(),
            }
            record["outcome_hash"] = _sha256(record)
            operations_use = self._directory_use("operations", create=True)
            path = prepared_project_output_path(
                operations_use,
                f"operation-{clean_operation_id}-outcome-{outcome_id}.json",
            )
            _write_exclusive_record(
                path,
                record,
                collision_message="脚本版本 operation outcome 发生碰撞",
            )
            operations_use.verify()
            return _operation_from_records(
                {
                    "operation_id": existing.operation_id,
                    "kind": existing.kind,
                    "version_id": existing.version_id,
                    "context": existing.context,
                    "created_at": existing.created_at,
                    "intent_hash": existing.intent_hash,
                },
                record,
            )

    def list_operations(self) -> list[ScriptVersionOperation]:
        if not self._child_exists("operations"):
            return []
        operations_use = self._directory_use("operations", create=False)
        if not operations_use.path.exists():
            return []
        operations_use.verify()
        intents: dict[str, dict[str, Any]] = {}
        outcomes: dict[str, dict[str, Any]] = {}
        for path in sorted(
            operations_use.path.glob(_OPERATION_INTENT_FILENAME_PATTERN)
        ):
            raw = _read_operation_record(path)
            _validate_operation_identity(
                raw,
                pipeline_id=self.pipeline_id,
                project_key=self.project_key,
                record_type="intent",
            )
            operation_id = str(raw["operation_id"])
            if operation_id in intents:
                raise ScriptVersionError("脚本版本 operation intent 重复")
            intents[operation_id] = raw
        for path in sorted(
            operations_use.path.glob(_OPERATION_OUTCOME_FILENAME_PATTERN)
        ):
            raw = _read_operation_record(path)
            _validate_operation_identity(
                raw,
                pipeline_id=self.pipeline_id,
                project_key=self.project_key,
                record_type="outcome",
            )
            operation_id = str(raw["operation_id"])
            if operation_id in outcomes:
                raise ScriptVersionError("脚本版本 operation 存在多个终态")
            outcomes[operation_id] = raw
        orphan_outcomes = set(outcomes) - set(intents)
        if orphan_outcomes:
            raise ScriptVersionError("脚本版本 operation outcome 缺少 intent")
        result = [
            _operation_from_records(intent, outcomes.get(operation_id))
            for operation_id, intent in intents.items()
        ]
        return sorted(result, key=lambda item: (item.created_at, item.operation_id))

    def pending_operations(self) -> list[ScriptVersionOperation]:
        return [item for item in self.list_operations() if item.status == "pending"]

    def events_for_operation(self, operation_id: str) -> list[dict[str, Any]]:
        clean_operation_id = _require_operation_id(operation_id)
        return [
            dict(event)
            for event in self._read_events()
            if str(event.get("operation_id") or "") == clean_operation_id
        ]

    def _operation_by_id(self, operation_id: str) -> ScriptVersionOperation | None:
        for item in self.list_operations():
            if item.operation_id == operation_id:
                return item
        return None

    def _acquire_write_mutex(self) -> WindowsNamedMutex:
        key = f"{self.project_root.resolve(strict=False)}|{self.pipeline_id}|{self.project_key}"
        try:
            return WindowsNamedMutex.try_acquire(
                build_mutex_name("script-version", key),
                timeout_ms=5_000,
            )
        except WindowsMutexBusy as exc:
            raise ScriptVersionError(
                "脚本版本正在由另一个操作提交，请刷新后重试。"
            ) from exc

    def _directory_use(
        self,
        child: str,
        *,
        create: bool,
    ) -> PreparedProjectChildDirectoryUse:
        try:
            return begin_prepared_project_child_directory_use(
                self.pipeline,
                project_root=self.project_root,
                relative_path=Path(SCRIPT_VERSION_ROOT) / child,
                create=create,
            )
        except (ProjectIdentityError, OSError, ValueError) as exc:
            raise ScriptVersionError("脚本版本目录未通过项目身份校验") from exc

    def _read_events(self) -> list[dict[str, Any]]:
        if not self._child_exists("events"):
            return []
        events_use = self._directory_use("events", create=False)
        if not events_use.path.exists():
            return []
        events: list[dict[str, Any]] = []
        for path in sorted(events_use.path.glob(_EVENT_FILENAME_PATTERN)):
            if not path.is_file() or path.is_symlink():
                raise ScriptVersionError("脚本版本事件不是普通文件")
            events_use.verify()
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ScriptVersionError("脚本版本事件无法读取") from exc
            if not isinstance(value, dict):
                raise ScriptVersionError("脚本版本事件必须是对象")
            schema_version = value.get("schema_version")
            if schema_version is None:
                events.append(value)
                continue
            if schema_version != SCRIPT_EVENT_SCHEMA:
                raise ScriptVersionError("脚本版本事件 schema 无效")
            event_hash = str(value.get("event_hash") or "")
            unsigned = dict(value)
            unsigned.pop("event_hash", None)
            if len(event_hash) != 64 or event_hash != _sha256(unsigned):
                raise ScriptVersionError("脚本版本事件哈希不匹配，疑似篡改")
            if not str(value.get("event_id") or "").strip():
                raise ScriptVersionError("脚本版本事件缺少 event_id")
            if not str(value.get("operation_id") or "").strip():
                raise ScriptVersionError("脚本版本事件缺少 operation_id")
            approval_evidence_hash = str(value.get("approval_evidence_hash") or "")
            if approval_evidence_hash and len(approval_evidence_hash) != 64:
                raise ScriptVersionError("脚本批准证据哈希无效")
            events.append(value)
        return events

    def _child_exists(self, child: str) -> bool:
        try:
            project_use = begin_prepared_project_directory_use(
                self.pipeline,
                project_root=self.project_root,
            )
            target = project_use.path / SCRIPT_VERSION_ROOT / child
            if not target.exists():
                return False
            if target.is_symlink() or not target.is_dir():
                raise ScriptVersionError("脚本版本目录不是普通目录")
            project_use.verify()
            return True
        except (ProjectIdentityError, OSError, ValueError) as exc:
            raise ScriptVersionError("脚本版本目录未通过项目身份校验") from exc

    def _append_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("event") or "")
        if event_type not in {"approved", "invalidated"}:
            raise ScriptVersionError("未知脚本版本事件")
        version_id = str(event.get("version_id") or "")
        snapshot = self.get(version_id)
        event_id = uuid4().hex
        record = {
            "schema_version": SCRIPT_EVENT_SCHEMA,
            "event_id": event_id,
            "operation_id": str(event.get("operation_id") or event_id),
            "event": event_type,
            "version_id": version_id,
            "version_content_hash": snapshot.content_hash,
            "version_snapshot_hash": snapshot.snapshot_hash,
            "at": str(event.get("at") or _now_iso()),
        }
        if event_type == "invalidated":
            record["reason"] = str(event.get("reason") or "")[:500]
        elif str(event.get("approval_evidence_hash") or ""):
            evidence_hash = str(event["approval_evidence_hash"])
            if len(evidence_hash) != 64:
                raise ScriptVersionError("脚本批准证据哈希无效")
            record["approval_evidence_hash"] = evidence_hash
        record["event_hash"] = _sha256(record)
        events_use = self._directory_use("events", create=True)
        filename = f"event-{event_type}-{event_id}.json"
        path = prepared_project_output_path(events_use, filename)
        encoded = _canonical_bytes(record)
        try:
            with path.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise ScriptVersionError("脚本版本事件文件发生碰撞") from exc
        except OSError as exc:
            raise ScriptVersionError("脚本版本事件写入失败") from exc
        events_use.verify()


def _require_operation_id(value: Any) -> str:
    operation_id = str(value or "").strip()
    if _OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
        raise ScriptVersionError("脚本版本 operation_id 必须是 32 位小写十六进制")
    return operation_id


def _normalize_operation_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _json_value(dict(value))
    if not isinstance(normalized, dict):
        raise ScriptVersionError("脚本版本 operation 内容必须是对象")
    return normalized


def _read_operation_record(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ScriptVersionError("脚本版本 operation 记录不是普通文件")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScriptVersionError("脚本版本 operation 记录无法读取") from exc
    if not isinstance(raw, dict):
        raise ScriptVersionError("脚本版本 operation 记录必须是对象")
    return raw


def _validate_operation_identity(
    raw: dict[str, Any],
    *,
    pipeline_id: int,
    project_key: str,
    record_type: str,
) -> None:
    if raw.get("schema_version") != SCRIPT_OPERATION_SCHEMA:
        raise ScriptVersionError("脚本版本 operation schema 无效")
    if raw.get("record_type") != record_type:
        raise ScriptVersionError("脚本版本 operation 记录类型无效")
    if raw.get("pipeline_id") != pipeline_id or raw.get("project_key") != project_key:
        raise ScriptVersionError("脚本版本 operation 项目身份不一致")
    _require_operation_id(raw.get("operation_id"))

    if record_type == "intent":
        if str(raw.get("kind") or "") not in _OPERATION_KINDS:
            raise ScriptVersionError("脚本版本 operation 类型无效")
        if not str(raw.get("version_id") or "").strip():
            raise ScriptVersionError("脚本版本 operation 缺少 version_id")
        if not isinstance(raw.get("context"), dict):
            raise ScriptVersionError("脚本版本 operation context 无效")
        if not str(raw.get("created_at") or "").strip():
            raise ScriptVersionError("脚本版本 operation 缺少创建时间")
        stored_hash = str(raw.get("intent_hash") or "")
        unsigned = dict(raw)
        unsigned.pop("intent_hash", None)
        if len(stored_hash) != 64 or stored_hash != _sha256(unsigned):
            raise ScriptVersionError("脚本版本 operation intent 哈希不匹配")
        return

    if str(raw.get("outcome") or "") not in _OPERATION_OUTCOMES:
        raise ScriptVersionError("脚本版本 operation 结果无效")
    if _OPERATION_ID_PATTERN.fullmatch(str(raw.get("outcome_id") or "")) is None:
        raise ScriptVersionError("脚本版本 operation outcome_id 无效")
    if len(str(raw.get("intent_hash") or "")) != 64:
        raise ScriptVersionError("脚本版本 operation intent 引用哈希无效")
    if not isinstance(raw.get("result"), dict):
        raise ScriptVersionError("脚本版本 operation result 无效")
    if not str(raw.get("at") or "").strip():
        raise ScriptVersionError("脚本版本 operation outcome 缺少时间")
    stored_hash = str(raw.get("outcome_hash") or "")
    unsigned = dict(raw)
    unsigned.pop("outcome_hash", None)
    if len(stored_hash) != 64 or stored_hash != _sha256(unsigned):
        raise ScriptVersionError("脚本版本 operation outcome 哈希不匹配")


def _operation_from_records(
    intent: Mapping[str, Any],
    outcome: Mapping[str, Any] | None,
) -> ScriptVersionOperation:
    if outcome is not None and outcome.get("intent_hash") != intent.get("intent_hash"):
        raise ScriptVersionError("脚本版本 operation outcome 未绑定原 intent")
    status = str(outcome.get("outcome") or "") if outcome is not None else "pending"
    return ScriptVersionOperation(
        operation_id=str(intent.get("operation_id") or ""),
        kind=str(intent.get("kind") or ""),
        version_id=str(intent.get("version_id") or ""),
        context=dict(intent.get("context") or {}),
        created_at=str(intent.get("created_at") or ""),
        intent_hash=str(intent.get("intent_hash") or ""),
        status=status,
        outcome_at=str(outcome.get("at") or "") if outcome is not None else "",
        result=dict(outcome.get("result") or {}) if outcome is not None else {},
        outcome_hash=(
            str(outcome.get("outcome_hash") or "") if outcome is not None else ""
        ),
    )


def _write_exclusive_record(
    path: Path,
    record: Mapping[str, Any],
    *,
    collision_message: str,
) -> None:
    encoded = _canonical_bytes(record)
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        try:
            if path.read_bytes() != encoded:
                raise ScriptVersionError(collision_message)
        except OSError as exc:
            raise ScriptVersionError(f"{collision_message}且无法验证") from exc
    except OSError as exc:
        raise ScriptVersionError("脚本版本 operation 记录写入失败") from exc


def _write_exclusive_snapshot(
    directory_use: PreparedProjectChildDirectoryUse,
    snapshot: ScriptVersionSnapshot,
) -> None:
    path = prepared_project_output_path(directory_use, f"{snapshot.version_id}.json")
    encoded = _canonical_bytes(snapshot.to_dict())
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        try:
            if path.read_bytes() != encoded:
                raise ScriptVersionError("脚本版本文件碰撞或被篡改")
        except OSError as exc:
            raise ScriptVersionError("脚本版本文件碰撞且无法验证") from exc
    except OSError as exc:
        raise ScriptVersionError("脚本版本写入失败") from exc
    directory_use.verify()


def _snapshot_from_dict(raw: Any) -> ScriptVersionSnapshot:
    if not isinstance(raw, Mapping):
        raise ScriptVersionError("脚本版本 schema 无效")
    schema_version = raw.get("schema_version")
    if schema_version not in {LEGACY_SCRIPT_VERSION_SCHEMA, SCRIPT_VERSION_SCHEMA}:
        raise ScriptVersionError("脚本版本 schema 无效")
    payload = _normalize_payload(raw.get("payload"))
    sources = _normalize_fact_sources(raw.get("fact_sources") or [])
    raw_shot_plan = raw.get("shot_plan")
    if not isinstance(raw_shot_plan, Sequence) or isinstance(
        raw_shot_plan, (str, bytes, bytearray)
    ):
        raise ScriptVersionError("脚本版本缺少 ShotPlan")
    if any(not isinstance(item, Mapping) for item in raw_shot_plan):
        raise ScriptVersionError("脚本版本 ShotPlan 包含无效条目")
    shot_plan = [
        {str(key): _json_value(value) for key, value in item.items()}
        for item in raw_shot_plan
    ]
    input_evidence = (
        _normalize_input_evidence(raw.get("input_evidence") or {})
        if schema_version == SCRIPT_VERSION_SCHEMA
        else {}
    )
    expected_content = _sha256(payload)
    input_envelope = {"payload": payload, "fact_sources": sources}
    if schema_version == SCRIPT_VERSION_SCHEMA:
        input_envelope["input_evidence"] = input_evidence
    expected_input = _sha256(input_envelope)
    if raw.get("content_hash") != expected_content or raw.get("input_hash") != expected_input:
        raise ScriptVersionError("脚本版本哈希不匹配，疑似篡改")
    pipeline_id = _positive_int(raw.get("pipeline_id"), "pipeline_id")
    version_number = _positive_int(raw.get("version_number"), "version_number")
    version_id = str(raw.get("version_id") or "")
    if not version_id.startswith(f"sv-{pipeline_id}-{version_number:04d}-"):
        raise ScriptVersionError("脚本版本 ID 不匹配")
    project_key = str(raw.get("project_key") or "").strip()
    if not project_key:
        raise ScriptVersionError("脚本版本缺少 project_key")
    for label in ("input_hash", "content_hash"):
        value = raw.get(label)
        if not isinstance(value, str) or len(value) != 64:
            raise ScriptVersionError(f"脚本版本 {label} 无效")
    source = _safe_label(raw.get("source"), "unknown")
    created_at = str(raw.get("created_at") or "")
    shot_plan_hash = ""
    snapshot_hash = ""
    integrity_status = "legacy_payload_only"
    if schema_version == SCRIPT_VERSION_SCHEMA:
        shot_plan_hash = str(raw.get("shot_plan_hash") or "")
        snapshot_hash = str(raw.get("snapshot_hash") or "")
        if len(shot_plan_hash) != 64 or shot_plan_hash != _sha256(shot_plan):
            raise ScriptVersionError("脚本版本 ShotPlan 哈希不匹配，疑似篡改")
        if len(snapshot_hash) != 64:
            raise ScriptVersionError("脚本版本快照哈希无效")
        provisional = ScriptVersionSnapshot(
            pipeline_id=pipeline_id,
            project_key=project_key,
            version_id=version_id,
            version_number=version_number,
            input_hash=str(raw["input_hash"]),
            content_hash=str(raw["content_hash"]),
            payload=payload,
            fact_sources=tuple(sources),
            shot_plan=tuple(shot_plan),
            source=source,
            created_at=created_at,
            schema_version=schema_version,
            input_evidence=input_evidence,
            shot_plan_hash=shot_plan_hash,
            snapshot_hash=snapshot_hash,
        )
        if snapshot_hash != _snapshot_hash(provisional):
            raise ScriptVersionError("脚本版本快照哈希不匹配，疑似篡改")
        integrity_status = "full_envelope_verified"
    return ScriptVersionSnapshot(
        pipeline_id=pipeline_id,
        project_key=project_key,
        version_id=version_id,
        version_number=version_number,
        input_hash=str(raw["input_hash"]),
        content_hash=str(raw["content_hash"]),
        payload=payload,
        fact_sources=tuple(sources),
        shot_plan=tuple(shot_plan),
        source=source,
        created_at=created_at,
        schema_version=schema_version,
        input_evidence=input_evidence,
        shot_plan_hash=shot_plan_hash,
        snapshot_hash=snapshot_hash,
        integrity_status=integrity_status,
    )


def _apply_events(
    snapshots: list[ScriptVersionSnapshot],
    events: list[dict[str, Any]],
) -> list[ScriptVersionSnapshot]:
    by_id = {item.version_id: item for item in snapshots}
    for event in events:
        version_id = str(event.get("version_id") or "")
        current = by_id.get(version_id)
        if current is None:
            raise ScriptVersionError("脚本版本事件指向不存在的版本")
        approval_integrity_status = "legacy_version_id_only"
        approval_event_hash = ""
        approval_evidence_hash = ""
        if event.get("schema_version") == SCRIPT_EVENT_SCHEMA:
            if (
                event.get("version_content_hash") != current.content_hash
                or event.get("version_snapshot_hash") != current.snapshot_hash
            ):
                raise ScriptVersionError("脚本版本事件绑定的快照哈希不匹配")
            approval_integrity_status = (
                "snapshot_hash_bound"
                if current.snapshot_hash
                else "content_hash_bound"
            )
            approval_event_hash = str(event.get("event_hash") or "")
            approval_evidence_hash = str(event.get("approval_evidence_hash") or "")
        event_type = event.get("event")
        if event_type == "approved" and current.status != "invalidated":
            by_id[version_id] = _replace(
                current,
                status="approved",
                approved_at=str(event.get("at") or ""),
                approval_integrity_status=approval_integrity_status,
                approval_event_hash=approval_event_hash,
                approval_evidence_hash=approval_evidence_hash,
            )
        elif event_type == "invalidated":
            by_id[version_id] = _replace(
                current,
                status="invalidated",
                invalidated_at=str(event.get("at") or ""),
                invalidated_reason=str(event.get("reason") or ""),
            )
        elif event_type not in {"approved", "invalidated"}:
            raise ScriptVersionError("未知脚本版本事件")
    return [by_id[item.version_id] for item in snapshots]


def _replace(snapshot: ScriptVersionSnapshot, **changes: Any) -> ScriptVersionSnapshot:
    values = snapshot.__dict__ if hasattr(snapshot, "__dict__") else {
        field: getattr(snapshot, field)
        for field in snapshot.__dataclass_fields__
    }
    values.update(changes)
    return ScriptVersionSnapshot(**values)


def _normalize_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ScriptVersionError("脚本版本内容不能为空")
    result = {str(key): _json_value(value) for key, value in payload.items()}
    if not str(result.get("voiceover") or "").strip() and not result.get("storyboard"):
        raise ScriptVersionError("脚本版本内容不能为空")
    return result


def _normalize_fact_sources(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ScriptVersionError("事实来源必须是列表")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ScriptVersionError("事实来源必须是对象")
        result.append({str(key): _json_value(val) for key, val in item.items()})
    return result


def _normalize_input_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ScriptVersionError("脚本版本输入证据必须是对象")
    return {str(key): _json_value(item) for key, item in value.items()}


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ScriptVersionError("脚本版本包含不可序列化字段")


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ScriptVersionError("脚本版本无法生成稳定 JSON") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _snapshot_hash(snapshot: ScriptVersionSnapshot) -> str:
    return _sha256(
        {
            "schema_version": snapshot.schema_version,
            "pipeline_id": snapshot.pipeline_id,
            "project_key": snapshot.project_key,
            "version_id": snapshot.version_id,
            "version_number": snapshot.version_number,
            "input_hash": snapshot.input_hash,
            "content_hash": snapshot.content_hash,
            "payload": snapshot.payload,
            "fact_sources": list(snapshot.fact_sources),
            "input_evidence": dict(snapshot.input_evidence or {}),
            "shot_plan": list(snapshot.shot_plan),
            "shot_plan_hash": snapshot.shot_plan_hash,
            "source": snapshot.source,
            "created_at": snapshot.created_at,
        }
    )


def _positive_int(value: Any, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ScriptVersionError(f"脚本版本 {label} 无效") from exc
    if result < 1:
        raise ScriptVersionError(f"脚本版本 {label} 无效")
    return result


def _safe_label(value: Any, default: str) -> str:
    text = str(value or default).strip()
    if not text or len(text) > 120 or any(char in text for char in "\\/\x00"):
        return default
    return text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


__all__ = [
    "SCRIPT_VERSION_ROOT",
    "SCRIPT_VERSION_SCHEMA",
    "ScriptVersionError",
    "ScriptVersionLedger",
    "ScriptVersionSnapshot",
]
