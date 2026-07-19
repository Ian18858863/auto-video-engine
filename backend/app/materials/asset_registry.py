from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class MaterialUsageRecord:
    file_path: str
    source_type: str
    usage_count: int
    last_used_at: datetime


@dataclass(slots=True)
class MaterialFingerprintRecord:
    fingerprint: str
    file_path: str
    source_type: str
    file_size: int
    first_seen_at: datetime
    last_seen_at: datetime


class MaterialUsageRegistry:
    def __init__(self, path: Path):
        self.path = path

    def record_use(
        self,
        file_path: str,
        *,
        source_type: str,
        used_at: datetime | None = None,
    ) -> MaterialUsageRecord:
        used_at = used_at or datetime.now(timezone.utc)
        records = self._load_raw()
        current = records.get(file_path, {})
        usage_count = int(current.get("usage_count", 0)) + 1
        payload = {
            "file_path": file_path,
            "source_type": source_type,
            "usage_count": usage_count,
            "last_used_at": used_at.isoformat(),
        }
        records[file_path] = payload
        self._save_raw(records)
        return self._to_record(payload)

    def get(self, file_path: str) -> MaterialUsageRecord | None:
        payload = self._load_raw().get(file_path)
        if not isinstance(payload, dict):
            return None
        return self._to_record(payload)

    def is_recently_used(
        self,
        file_path: str,
        *,
        now: datetime | None = None,
        cooldown: timedelta = timedelta(days=2),
    ) -> bool:
        record = self.get(file_path)
        if record is None:
            return False
        now = now or datetime.now(timezone.utc)
        return now - record.last_used_at < cooldown

    def score_multiplier(
        self,
        file_path: str,
        *,
        now: datetime | None = None,
    ) -> float:
        record = self.get(file_path)
        if record is None:
            return 1.0
        if self.is_recently_used(file_path, now=now):
            return 0.0
        return max(0.2, 1.0 - record.usage_count * 0.15)

    def _load_raw(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, dict)
        }

    def _save_raw(self, records: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _to_record(payload: dict[str, Any]) -> MaterialUsageRecord:
        raw_time = str(payload.get("last_used_at", ""))
        try:
            last_used_at = datetime.fromisoformat(raw_time)
        except ValueError:
            last_used_at = datetime.now(timezone.utc)
        if last_used_at.tzinfo is None:
            last_used_at = last_used_at.replace(tzinfo=timezone.utc)
        return MaterialUsageRecord(
            file_path=str(payload.get("file_path", "")),
            source_type=str(payload.get("source_type", "")),
            usage_count=int(payload.get("usage_count", 0)),
            last_used_at=last_used_at,
        )


class MaterialFingerprintRegistry:
    def __init__(self, path: Path):
        self.path = path

    def record_file(
        self,
        file_path: Path,
        *,
        source_type: str,
        seen_at: datetime | None = None,
    ) -> MaterialFingerprintRecord:
        seen_at = seen_at or datetime.now(timezone.utc)
        fingerprint = sha256_file(file_path)
        records = self._load_raw()
        current = records.get(fingerprint, {})
        first_seen_at = str(current.get("first_seen_at") or seen_at.isoformat())
        payload = {
            "fingerprint": fingerprint,
            "file_path": str(file_path),
            "source_type": source_type,
            "file_size": file_path.stat().st_size,
            "first_seen_at": first_seen_at,
            "last_seen_at": seen_at.isoformat(),
        }
        records[fingerprint] = payload
        self._save_raw(records)
        return self._to_record(payload)

    def find_duplicate(self, file_path: Path) -> MaterialFingerprintRecord | None:
        fingerprint = sha256_file(file_path)
        payload = self._load_raw().get(fingerprint)
        if not isinstance(payload, dict):
            return None
        return self._to_record(payload)

    def get(self, fingerprint: str) -> MaterialFingerprintRecord | None:
        payload = self._load_raw().get(fingerprint)
        if not isinstance(payload, dict):
            return None
        return self._to_record(payload)

    def _load_raw(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, dict)
        }

    def _save_raw(self, records: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _to_record(payload: dict[str, Any]) -> MaterialFingerprintRecord:
        first_seen_at = _parse_datetime(str(payload.get("first_seen_at", "")))
        last_seen_at = _parse_datetime(str(payload.get("last_seen_at", "")))
        return MaterialFingerprintRecord(
            fingerprint=str(payload.get("fingerprint", "")),
            file_path=str(payload.get("file_path", "")),
            source_type=str(payload.get("source_type", "")),
            file_size=int(payload.get("file_size", 0)),
            first_seen_at=first_seen_at,
            last_seen_at=last_seen_at,
        )


def sha256_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
