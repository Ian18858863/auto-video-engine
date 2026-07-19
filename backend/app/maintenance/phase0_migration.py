from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import stat as stat_module
import struct
import sys
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, NoReturn, Sequence
from uuid import uuid4

from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from app.core.database_preflight import (
    DatabasePreflightReport,
    audit_database_identity,
)
from app.core.runtime_identity import require_non_system_drive, resolve_runtime_identity
from app.core.schema_migrations import (
    ALEMBIC_INI,
    BASELINE_REVISION,
    SchemaStatus,
    inspect_schema_status,
    schema_blockers_for_revision,
)
from app.materials.provenance import audit_legacy_material_reports


MAINTENANCE_SQLITE_TIMEOUT_SECONDS = 5.0
COMPLETION_EVIDENCE_REVISION = "20260711_0003"
MIGRATION_PREPARATION_FORMAT = "phase0-migration-preparation-v3"
LEGACY_COMPLETION_RECLASSIFICATION_REASON = (
    "legacy_completion_without_evidence"
)


@dataclass(frozen=True, slots=True)
class BackupReport:
    manifest_format: str
    source_path: str
    backup_path: str
    manifest_path: str
    source_size_bytes: int
    backup_size_bytes: int
    created_at_utc: str
    backup_sha256: str
    backup_quick_check: str
    backup_foreign_key_violations: list[dict[str, Any]]
    source_table_counts: dict[str, int]
    backup_table_counts: dict[str, int]
    table_counts_match: bool
    source_sha256: str | None
    source_sha256_unavailable_reason: str | None
    snapshot_logical_sha256: str
    snapshot_business_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ManualCutoverRequiredError(RuntimeError):
    """Automatic replacement is unavailable; verified artifacts remain untouched."""

    def __init__(
        self,
        message: str,
        *,
        source_path: Path,
        candidate_path: Path,
        backup_path: Path,
        preparation_path: Path,
    ) -> None:
        super().__init__(message)
        self.source_path = source_path
        self.candidate_path = candidate_path
        self.backup_path = backup_path
        self.preparation_path = preparation_path


@dataclass(frozen=True, slots=True)
class MigrationPreparationReport:
    preparation_format: str
    database_path: str
    manifest_path: str
    backup_path: str
    preparation_path: str
    candidate_path: str
    prepared_at_utc: str
    before_revision: str | None
    head_revision: str
    source_device: int
    source_inode: int
    manifest_sha256: str
    source_snapshot_logical_sha256: str
    source_snapshot_business_sha256: str
    source_business_projection: dict[str, list[str]]
    source_preserved_business_sha256: str
    pipeline_status_reclassifications: list[dict[str, Any]]
    candidate_snapshot_business_sha256: str
    candidate_sha256: str
    candidate_device: int
    candidate_inode: int
    candidate_parent_device: int
    candidate_parent_inode: int
    quick_check: str
    foreign_key_violations: list[dict[str, Any]]
    table_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _ResultReservation:
    final_path: Path
    temporary_path: Path
    protected_paths: tuple[Path, ...]
    descriptor: int
    temporary_identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _OwnedSQLiteArtifact:
    path: Path
    identity: tuple[int, int]
    container_path: Path
    container_identity: tuple[int, int]


def _digest_frame(digest: Any, label: bytes, payload: bytes) -> None:
    digest.update(struct.pack(">Q", len(label)))
    digest.update(label)
    digest.update(struct.pack(">Q", len(payload)))
    digest.update(payload)


def _typed_sqlite_value(value: Any) -> bytes:
    if value is None:
        return b"n"
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii")
    if isinstance(value, float):
        return b"f" + struct.pack(">d", value)
    if isinstance(value, str):
        return b"t" + value.encode("utf-8", errors="surrogatepass")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return b"b" + bytes(value)
    raise TypeError(f"unsupported SQLite value type: {type(value)!r}")


def _typed_sqlite_row(row: Sequence[Any]) -> bytes:
    payload = bytearray()
    for value in row:
        encoded = _typed_sqlite_value(value)
        payload.extend(struct.pack(">Q", len(encoded)))
        payload.extend(encoded)
    return bytes(payload)


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _sqlite_logical_digest(
    connection: sqlite3.Connection,
    *,
    include_schema: bool,
    exclude_tables: Sequence[str] = (),
    projection: dict[str, tuple[str, ...]] | None = None,
) -> str:
    """Hash one pinned SQLite snapshot, preserving value types and all rows."""

    digest = hashlib.sha256()
    excluded = set(exclude_tables)
    if include_schema:
        schema_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name"
        ).fetchall()
        for row in schema_rows:
            _digest_frame(digest, b"schema", _typed_sqlite_row(row))

    available_tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        if str(row[0]) not in excluded
    ]
    table_names = sorted(projection) if projection is not None else available_tables
    if not set(table_names).issubset(available_tables):
        raise ValueError("SQLite projection references a missing table")
    for table_name in table_names:
        _digest_frame(digest, b"table", table_name.encode("utf-8"))
        all_columns = connection.execute(
            f"PRAGMA table_xinfo({_quote_identifier(table_name)})"
        ).fetchall()
        projected_names = projection.get(table_name) if projection is not None else None
        columns = (
            [row for row in all_columns if str(row[1]) in projected_names]
            if projected_names is not None
            else all_columns
        )
        if projected_names is not None and tuple(str(row[1]) for row in columns) != (
            projected_names
        ):
            raise ValueError("SQLite projection columns changed or disappeared")
        for column in columns:
            _digest_frame(digest, b"column", _typed_sqlite_row(column))
        selection = (
            ", ".join(_quote_identifier(name) for name in projected_names)
            if projected_names is not None
            else "*"
        )
        rows = [
            _typed_sqlite_row(row)
            for row in connection.execute(
                f"SELECT {selection} FROM {_quote_identifier(table_name)}"
            ).fetchall()
        ]
        for encoded_row in sorted(rows):
            _digest_frame(digest, b"row", encoded_row)
    return digest.hexdigest()


def _capture_sqlite_projection(
    connection: sqlite3.Connection,
    *,
    exclude_tables: Sequence[str] = (),
) -> dict[str, tuple[str, ...]]:
    excluded = set(exclude_tables)
    table_names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        if str(row[0]) not in excluded
    ]
    return {
        table_name: tuple(
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_xinfo({_quote_identifier(table_name)})"
            ).fetchall()
        )
        for table_name in table_names
    }


def _crosses_completion_evidence_revision(before_revision: str | None) -> bool:
    """Derive whether upgrade-to-head crosses 0003 from the Alembic graph."""

    config = Config(str(ALEMBIC_INI))
    script = ScriptDirectory.from_config(config)
    head_revision = script.get_current_head()
    if head_revision is None:
        raise RuntimeError("Alembic migration graph has no unique head")
    target = script.get_revision(COMPLETION_EVIDENCE_REVISION)
    if target is None:
        raise RuntimeError(
            f"Alembic migration graph is missing {COMPLETION_EVIDENCE_REVISION}"
        )
    if before_revision == COMPLETION_EVIDENCE_REVISION:
        return False
    if before_revision is not None and script.get_revision(before_revision) is None:
        raise ValueError(f"unknown source revision: {before_revision}")
    lower = "base" if before_revision is None else before_revision
    crossed_revisions = tuple(script.iterate_revisions(head_revision, lower))
    crosses = any(
        revision.revision == COMPLETION_EVIDENCE_REVISION
        for revision in crossed_revisions
    )
    if not crosses:
        raise ValueError(
            "source revision cannot be upgraded to head through completion evidence: "
            f"before={before_revision}, head={head_revision}"
        )
    return True


def build_preserved_projection(
    full_projection: dict[str, tuple[str, ...]],
    before_revision: str | None,
) -> dict[str, tuple[str, ...]]:
    """Build the code-owned projection that 0003 is allowed to preserve."""

    projection = {
        str(table_name): tuple(columns)
        for table_name, columns in full_projection.items()
    }
    pipeline_columns = projection.get("production_pipelines")
    if pipeline_columns is None:
        raise ValueError("source projection has no production_pipelines table")
    if len(set(pipeline_columns)) != len(pipeline_columns):
        raise ValueError("source production_pipelines projection has duplicate columns")
    crosses_0003 = _crosses_completion_evidence_revision(before_revision)
    if crosses_0003:
        if "completion_evidence" in pipeline_columns:
            raise ValueError(
                "source older than 0003 unexpectedly has completion_evidence"
            )
        if "status" not in pipeline_columns:
            raise ValueError("source production_pipelines projection has no status")
        projection["production_pipelines"] = tuple(
            column for column in pipeline_columns if column != "status"
        )
    elif "completion_evidence" not in pipeline_columns:
        raise ValueError("source at 0003 has no completion_evidence column")
    return projection


def _database_current_revision(connection: sqlite3.Connection) -> str | None:
    version_table = connection.execute(
        "SELECT 1 FROM sqlite_schema "
        "WHERE type='table' AND name='alembic_version'"
    ).fetchone()
    if version_table is None:
        return None
    rows = connection.execute(
        "SELECT version_num FROM alembic_version ORDER BY version_num"
    ).fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
        raise ValueError("source has no single canonical Alembic revision")
    return str(rows[0][0])


def _pipeline_status_rows(
    connection: sqlite3.Connection,
) -> list[tuple[int, str]]:
    rows = connection.execute(
        "SELECT id, status FROM production_pipelines ORDER BY id"
    ).fetchall()
    result: list[tuple[int, str]] = []
    for pipeline_id, status in rows:
        if not isinstance(pipeline_id, int) or not isinstance(status, str):
            raise ValueError("pipeline status proof contains a malformed row")
        result.append((pipeline_id, status))
    return result


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_completion_evidence(raw_value: Any) -> str:
    if isinstance(raw_value, bytes):
        raw_value = raw_value.decode("utf-8", errors="strict")
    if not isinstance(raw_value, str):
        raise ValueError("completion evidence is not JSON text")
    try:
        parsed = json.loads(
            raw_value,
            parse_constant=_reject_nonstandard_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        OverflowError,
    ) as exc:
        raise ValueError("completion evidence is not canonicalizable JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("completion evidence must be a JSON object")
    return json.dumps(
        parsed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _pipeline_evidence_rows(
    connection: sqlite3.Connection,
) -> list[tuple[int, str]]:
    rows = connection.execute(
        "SELECT id, completion_evidence FROM production_pipelines ORDER BY id"
    ).fetchall()
    result: list[tuple[int, str]] = []
    for pipeline_id, raw_evidence in rows:
        if not isinstance(pipeline_id, int):
            raise ValueError("pipeline evidence proof contains a malformed ID")
        result.append(
            (pipeline_id, _canonical_completion_evidence(raw_evidence))
        )
    return result


def _rebuild_pipeline_status_reclassifications(
    source_rows: Sequence[tuple[int, str]],
    candidate_rows: Sequence[tuple[int, str]],
    *,
    crosses_0003: bool,
) -> list[dict[str, Any]]:
    if [row[0] for row in source_rows] != [row[0] for row in candidate_rows]:
        raise RuntimeError(
            "pipeline reclassification proof changed, inserted, or deleted pipeline IDs"
        )
    transitions: list[dict[str, Any]] = []
    for (pipeline_id, old_status), (_, new_status) in zip(
        source_rows,
        candidate_rows,
        strict=True,
    ):
        if not crosses_0003:
            if new_status != old_status:
                raise RuntimeError(
                    "pipeline reclassification proof changed a status at 0003: "
                    f"pipeline_id={pipeline_id}, old={old_status}, new={new_status}"
                )
            continue
        expected_status = "需要人工确认" if old_status == "已完成" else old_status
        if new_status != expected_status:
            raise RuntimeError(
                "pipeline reclassification proof contains an unauthorized status change: "
                f"pipeline_id={pipeline_id}, old={old_status}, new={new_status}"
            )
        if old_status == "已完成":
            transitions.append(
                {
                    "pipeline_id": pipeline_id,
                    "old_status": "已完成",
                    "new_status": "需要人工确认",
                    "reason_code": LEGACY_COMPLETION_RECLASSIFICATION_REASON,
                }
            )
    return transitions


def _verify_pipeline_row_proof(
    source_connection: sqlite3.Connection,
    candidate_connection: sqlite3.Connection,
    *,
    before_revision: str | None,
) -> list[dict[str, Any]]:
    crosses_0003 = _crosses_completion_evidence_revision(before_revision)
    source_status_rows = _pipeline_status_rows(source_connection)
    candidate_status_rows = _pipeline_status_rows(candidate_connection)
    transitions = _rebuild_pipeline_status_reclassifications(
        source_status_rows,
        candidate_status_rows,
        crosses_0003=crosses_0003,
    )
    candidate_evidence_rows = _pipeline_evidence_rows(candidate_connection)
    if [row[0] for row in candidate_evidence_rows] != [
        row[0] for row in source_status_rows
    ]:
        raise RuntimeError("pipeline evidence proof changed pipeline IDs")
    if crosses_0003:
        nonempty = [
            pipeline_id
            for pipeline_id, evidence in candidate_evidence_rows
            if evidence != "{}"
        ]
        if nonempty:
            raise RuntimeError(
                "pipeline evidence proof added legacy completion evidence: "
                f"pipeline_ids={nonempty}"
            )
    else:
        source_evidence_rows = _pipeline_evidence_rows(source_connection)
        if candidate_evidence_rows != source_evidence_rows:
            raise RuntimeError(
                "pipeline evidence proof changed existing completion evidence"
            )
    return transitions


def _validated_prepared_reclassifications(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_rows = payload.get("pipeline_status_reclassifications")
    if not isinstance(raw_rows, list):
        raise ValueError("invalid reclassification proof: expected a list")
    expected_keys = {
        "pipeline_id",
        "old_status",
        "new_status",
        "reason_code",
    }
    validated: list[dict[str, Any]] = []
    seen_pipeline_ids: set[int] = set()
    for index, row in enumerate(raw_rows):
        if type(row) is not dict:
            raise ValueError(
                "invalid reclassification proof: "
                f"row {index} must be an object"
            )
        if set(row) != expected_keys:
            raise ValueError(
                "invalid reclassification proof: "
                f"row {index} keys must be exactly {sorted(expected_keys)}"
            )
        pipeline_id = row["pipeline_id"]
        if type(pipeline_id) is not int or pipeline_id <= 0:
            raise ValueError(
                "invalid reclassification proof: "
                f"row {index} pipeline_id must be a positive exact integer"
            )
        if pipeline_id in seen_pipeline_ids:
            raise ValueError(
                "invalid reclassification proof: "
                f"duplicate pipeline_id {pipeline_id}"
            )
        seen_pipeline_ids.add(pipeline_id)
        if type(row["old_status"]) is not str or row["old_status"] != "已完成":
            raise ValueError(
                "invalid reclassification proof: "
                f"row {index} old_status must be exactly 已完成"
            )
        if (
            type(row["new_status"]) is not str
            or row["new_status"] != "需要人工确认"
        ):
            raise ValueError(
                "invalid reclassification proof: "
                f"row {index} new_status must be exactly 需要人工确认"
            )
        if (
            type(row["reason_code"]) is not str
            or row["reason_code"]
            != LEGACY_COMPLETION_RECLASSIFICATION_REASON
        ):
            raise ValueError(
                "invalid reclassification proof: "
                f"row {index} reason_code is not canonical"
            )
        validated.append(row)
    return validated


def _sqlite_snapshot_verification(
    connection: sqlite3.Connection,
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    quick_values = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
    quick_check = "ok" if quick_values == ["ok"] else "; ".join(quick_values)
    foreign_key_violations = [
        {
            "table": row[0],
            "rowid": row[1],
            "parent": row[2],
            "foreign_key_index": row[3],
        }
        for row in connection.execute("PRAGMA foreign_key_check").fetchall()
    ]
    table_names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    table_counts = {
        name: int(
            connection.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(name)}"
            ).fetchone()[0]
        )
        for name in table_names
    }
    return quick_check, foreign_key_violations, table_counts


def _sqlite_sidecars(database_path: Path) -> tuple[Path, Path, Path]:
    return tuple(
        Path(f"{database_path}{suffix}")
        for suffix in ("-journal", "-wal", "-shm")
    )


def _require_no_sqlite_sidecars(database_path: Path) -> None:
    existing = [path for path in _sqlite_sidecars(database_path) if os.path.lexists(path)]
    if existing:
        raise ValueError(
            "SQLite candidate/cutover path still has journal, WAL, or SHM sidecars: "
            + ", ".join(str(path) for path in existing)
        )


def _database_header_uses_wal(database_path: Path) -> bool:
    with database_path.open("rb") as database_file:
        header = database_file.read(20)
    if len(header) < 20 or not header.startswith(b"SQLite format 3\x00"):
        raise ValueError(f"invalid SQLite header: {database_path}")
    return header[18:20] == b"\x02\x02"


def _normalize_sqlite_connection_to_delete(
    connection: sqlite3.Connection,
    database_path: Path,
) -> None:
    mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
    if mode == "wal":
        checkpoint = tuple(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        if checkpoint[0] != 0:
            raise RuntimeError(
                f"candidate WAL checkpoint failed: {database_path}, result={checkpoint}"
            )
    final_mode = str(
        connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    ).casefold()
    if final_mode != "delete":
        raise RuntimeError(
            f"candidate did not enter DELETE journal mode: {database_path}, mode={final_mode}"
        )
    _require_no_sqlite_sidecars(database_path)
    if _database_header_uses_wal(database_path):
        raise RuntimeError(f"candidate SQLite header still advertises WAL: {database_path}")


def _normalize_sqlite_to_delete(database_path: Path) -> None:
    with closing(
        sqlite3.connect(
            database_path,
            isolation_level=None,
            timeout=MAINTENANCE_SQLITE_TIMEOUT_SECONDS,
        )
    ) as connection:
        _normalize_sqlite_connection_to_delete(connection, database_path)


@contextmanager
def _maintenance_lock(source_path: Path):
    """Serialize maintenance tools; cutover still requires an explicit offline assertion."""

    lock_path = source_path.with_name(f".{source_path.name}.phase0-maintenance.lock")
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        opened_stat = os.fstat(descriptor)
        path_stat = lock_path.lstat()
        if (
            not stat_module.S_ISREG(path_stat.st_mode)
            or int(opened_stat.st_dev) != int(path_stat.st_dev)
            or int(opened_stat.st_ino) != int(path_stat.st_ino)
            or int(opened_stat.st_nlink) != 1
        ):
            raise RuntimeError(
                f"maintenance lock path is not one unaliased regular file: {lock_path}"
            )
    except Exception:
        os.close(descriptor)
        raise
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise RuntimeError(f"another Phase 0 maintenance operation is active: {lock_path}") from exc
    try:
        yield lock_path
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def create_verified_backup(
    source: Path,
    backup_root: Path,
    *,
    system_drive: str | None = None,
) -> BackupReport:
    source_path = Path(source).expanduser().resolve(strict=False)
    backup_root_path = Path(backup_root).expanduser().resolve(strict=False)

    if not source_path.exists():
        raise ValueError(f"源数据库不存在：{source_path}")
    if not source_path.is_file():
        raise ValueError(f"源数据库不是普通文件：{source_path}")
    if source_path.stat().st_size <= 0:
        raise ValueError(f"源数据库为空，拒绝备份：{source_path}")

    if system_drive is None:
        require_non_system_drive(backup_root_path)
    else:
        require_non_system_drive(backup_root_path, system_drive=system_drive)
    if backup_root_path.exists() and not backup_root_path.is_dir():
        raise ValueError(f"备份根路径不是目录：{backup_root_path}")

    created_at = datetime.now(timezone.utc)
    backup_directory = backup_root_path / (
        f"phase0-{created_at.strftime('%Y%m%dT%H%M%S.%fZ')}-{secrets.token_hex(4)}"
    )
    backup_path = backup_directory / "database.sqlite3"
    manifest_path = backup_directory / "manifest.json"

    source_uri = f"{source_path.as_uri()}?mode=rw"
    reader_uri = f"{source_path.as_uri()}?mode=ro"
    with _maintenance_lock(source_path):
        with closing(
            sqlite3.connect(
                source_uri,
                uri=True,
                timeout=MAINTENANCE_SQLITE_TIMEOUT_SECONDS,
                isolation_level=None,
            )
        ) as writer_gate:
            writer_gate.execute("BEGIN IMMEDIATE")
            try:
                with closing(
                    sqlite3.connect(
                        reader_uri,
                        uri=True,
                        timeout=MAINTENANCE_SQLITE_TIMEOUT_SECONDS,
                        isolation_level=None,
                    )
                ) as snapshot_reader:
                    snapshot_reader.execute("PRAGMA query_only=ON")
                    snapshot_reader.execute("BEGIN")
                    try:
                        quick_check, foreign_key_violations, source_counts = (
                            _sqlite_snapshot_verification(snapshot_reader)
                        )
                        if quick_check != "ok" or foreign_key_violations:
                            raise ValueError(
                                "source snapshot verification failed: "
                                f"quick_check={quick_check}, fk={foreign_key_violations}"
                            )
                        snapshot_logical_sha256 = _sqlite_logical_digest(
                            snapshot_reader,
                            include_schema=True,
                        )
                        snapshot_business_sha256 = _sqlite_logical_digest(
                            snapshot_reader,
                            include_schema=False,
                            exclude_tables=("alembic_version",),
                        )
                        source_size = source_path.stat().st_size
                        try:
                            source_sha256 = _sha256_file(source_path)
                            source_hash_unavailable_reason = None
                        except OSError as exc:
                            source_sha256 = None
                            source_hash_unavailable_reason = (
                                f"informational main-file hash unavailable: {exc}"
                            )

                        backup_root_path.mkdir(parents=True, exist_ok=True)
                        backup_directory.mkdir(exist_ok=False)
                        with closing(sqlite3.connect(backup_path)) as backup_connection:
                            snapshot_reader.backup(backup_connection)
                    finally:
                        snapshot_reader.execute("ROLLBACK")
            finally:
                writer_gate.execute("ROLLBACK")

    _normalize_sqlite_to_delete(backup_path)
    backup_audit = audit_database_identity(backup_path, candidate_paths=[])
    _require_verified_backup(backup_audit)
    backup_size = backup_path.stat().st_size
    if backup_size <= 0:
        raise RuntimeError(f"备份验证失败：输出文件为空：{backup_path}")
    backup_sha256 = _sha256_file(backup_path)
    if not backup_sha256:
        raise RuntimeError(f"备份验证失败：无法计算 SHA-256：{backup_path}")
    with closing(sqlite3.connect(backup_path)) as backup_connection:
        backup_logical_sha256 = _sqlite_logical_digest(
            backup_connection,
            include_schema=True,
        )
        backup_business_sha256 = _sqlite_logical_digest(
            backup_connection,
            include_schema=False,
            exclude_tables=("alembic_version",),
        )
    if backup_logical_sha256 != snapshot_logical_sha256:
        raise RuntimeError("backup logical digest does not match the locked source snapshot")
    if backup_business_sha256 != snapshot_business_sha256:
        raise RuntimeError("backup business digest does not match the locked source snapshot")
    table_counts_match = source_counts == backup_audit.table_counts
    if not table_counts_match:
        raise RuntimeError(
            "备份验证失败：源数据库与备份的表行数不一致；"
            f"source={source_counts}, backup={backup_audit.table_counts}"
        )

    report = BackupReport(
        manifest_format="phase0-backup-v2",
        source_path=str(source_path),
        backup_path=str(backup_path.resolve()),
        manifest_path=str(manifest_path.resolve()),
        source_size_bytes=source_size,
        backup_size_bytes=backup_size,
        created_at_utc=created_at.isoformat().replace("+00:00", "Z"),
        backup_sha256=backup_sha256,
        backup_quick_check=backup_audit.quick_check or "",
        backup_foreign_key_violations=backup_audit.foreign_key_violations,
        source_table_counts=source_counts,
        backup_table_counts=backup_audit.table_counts,
        table_counts_match=table_counts_match,
        source_sha256=source_sha256,
        source_sha256_unavailable_reason=source_hash_unavailable_reason,
        snapshot_logical_sha256=snapshot_logical_sha256,
        snapshot_business_sha256=snapshot_business_sha256,
    )
    _write_json_atomically(manifest_path, report.to_dict())
    return report


def _require_verified_backup(report: DatabasePreflightReport) -> None:
    if report.blockers:
        raise RuntimeError("备份验证失败：" + "；".join(report.blockers))
    if report.quick_check != "ok":
        raise RuntimeError(
            f"备份验证失败：quick_check={report.quick_check or '无结果'}"
        )
    if report.foreign_key_violations:
        raise RuntimeError(
            f"备份验证失败：存在 {len(report.foreign_key_violations)} 条外键违规"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve(strict=False))))


def _absolute_lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _path_exists_including_symlink(path: Path) -> bool:
    return os.path.lexists(path)


def _require_unaliased_evidence_path(
    candidate: Path,
    protected_paths: Sequence[Path],
    *,
    must_not_exist: bool,
) -> None:
    absolute_candidate = _absolute_lexical_path(candidate)
    candidate_key = os.path.normcase(os.path.normpath(str(absolute_candidate)))
    for protected in protected_paths:
        absolute_protected = protected.resolve(strict=False)
        protected_key = os.path.normcase(os.path.normpath(str(absolute_protected)))
        if candidate_key == protected_key:
            raise ValueError(
                f"迁移证据路径不得与源数据库、备份或 manifest 相同：{absolute_candidate}"
            )
        if _path_exists_including_symlink(absolute_candidate):
            try:
                if os.path.samefile(absolute_candidate, absolute_protected):
                    raise ValueError(
                        "迁移证据路径不得通过硬链接或符号链接指向受保护文件："
                        f"{absolute_candidate}"
                    )
            except OSError as exc:
                raise ValueError(
                    "无法证明迁移证据路径与受保护文件物理分离，安全失败关闭："
                    f"candidate={absolute_candidate}, protected={absolute_protected}"
                ) from exc
    if must_not_exist and _path_exists_including_symlink(absolute_candidate):
        raise FileExistsError(f"迁移证据文件已存在，拒绝覆盖：{absolute_candidate}")


def _publish_file_exclusively(temporary_path: Path, final_path: Path) -> None:
    # A same-directory hard link publishes the fully fsynced inode atomically
    # and, unlike os.replace(), fails if the final evidence name already exists.
    try:
        os.link(temporary_path, final_path, follow_symlinks=False)
    except TypeError:
        os.link(temporary_path, final_path)


def _unlink_if_owned(path: Path, identity: tuple[int, int]) -> None:
    try:
        stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if (int(stat.st_dev), int(stat.st_ino)) == identity:
        path.unlink()


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    final_path = _absolute_lexical_path(Path(path))
    temporary_path = final_path.with_name(
        f".json-{secrets.token_hex(6)}.tmp"
    )
    _require_unaliased_evidence_path(
        temporary_path,
        (final_path,),
        must_not_exist=True,
    )
    descriptor = os.open(
        temporary_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    stat = os.fstat(descriptor)
    temporary_identity = (int(stat.st_dev), int(stat.st_ino))
    completed = False
    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
            closefd=False,
        ) as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        current_stat = temporary_path.stat(follow_symlinks=False)
        if (int(current_stat.st_dev), int(current_stat.st_ino)) != temporary_identity:
            raise RuntimeError("JSON 临时文件在发布前被替换，拒绝发布")
        _require_unaliased_evidence_path(
            final_path,
            (),
            must_not_exist=True,
        )
        _publish_file_exclusively(temporary_path, final_path)
        try:
            published_path_stat = final_path.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "published JSON final path lost its recorded inode"
            ) from exc
        if (
            not stat_module.S_ISREG(published_path_stat.st_mode)
            or (
                int(published_path_stat.st_dev),
                int(published_path_stat.st_ino),
            )
            != temporary_identity
        ):
            raise RuntimeError(
                "published JSON final path is not the recorded regular inode"
            )
        published_descriptor = os.open(
            final_path,
            os.O_RDWR
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened_published_stat = os.fstat(published_descriptor)
            if (
                not stat_module.S_ISREG(opened_published_stat.st_mode)
                or (
                    int(opened_published_stat.st_dev),
                    int(opened_published_stat.st_ino),
                )
                != temporary_identity
            ):
                raise RuntimeError(
                    "published JSON descriptor is not the recorded regular inode"
                )
            os.fsync(published_descriptor)
            post_fsync_stat = os.fstat(published_descriptor)
            if (
                not stat_module.S_ISREG(post_fsync_stat.st_mode)
                or (
                    int(post_fsync_stat.st_dev),
                    int(post_fsync_stat.st_ino),
                )
                != temporary_identity
            ):
                raise RuntimeError(
                    "published JSON descriptor identity changed during fsync"
                )
            try:
                post_fsync_path_stat = final_path.stat(follow_symlinks=False)
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "published JSON final path disappeared during fsync"
                ) from exc
            if (
                not stat_module.S_ISREG(post_fsync_path_stat.st_mode)
                or (
                    int(post_fsync_path_stat.st_dev),
                    int(post_fsync_path_stat.st_ino),
                )
                != temporary_identity
            ):
                raise RuntimeError(
                    "published JSON final path identity changed during fsync"
                )
        finally:
            os.close(published_descriptor)
        os.close(descriptor)
        descriptor = -1
        completed = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not completed:
            _unlink_if_owned(final_path, temporary_identity)
        _unlink_if_owned(temporary_path, temporary_identity)


def _migration_result_path(source_path: Path, manifest_path: Path) -> Path:
    target_id = hashlib.sha256(
        _path_key(source_path).encode("utf-8")
    ).hexdigest()[:8]
    return manifest_path.with_name(
        f"migration-e-{target_id}-{uuid4().hex[:8]}.json"
    )


def _reserve_migration_result(
    source_path: Path,
    backup_path: Path,
    manifest_path: Path,
    additional_protected: Sequence[Path] = (),
) -> _ResultReservation:
    protected = tuple(
        path.resolve(strict=False)
        for path in (
            source_path,
            backup_path,
            manifest_path,
            *additional_protected,
        )
    )
    final_path = _migration_result_path(source_path, manifest_path)
    temporary_path = final_path.with_name(
        f".mr-{uuid4().hex[:12]}.tmp"
    )
    _require_unaliased_evidence_path(
        final_path,
        protected,
        must_not_exist=True,
    )
    _require_unaliased_evidence_path(
        temporary_path,
        protected,
        must_not_exist=True,
    )
    descriptor = os.open(
        temporary_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    opened_stat = os.fstat(descriptor)
    temporary_identity = (int(opened_stat.st_dev), int(opened_stat.st_ino))
    try:
        _require_unaliased_evidence_path(
            temporary_path,
            protected,
            must_not_exist=False,
        )
        return _ResultReservation(
            final_path=final_path,
            temporary_path=temporary_path,
            protected_paths=protected,
            descriptor=descriptor,
            temporary_identity=temporary_identity,
        )
    except Exception:
        os.close(descriptor)
        _unlink_if_owned(temporary_path, temporary_identity)
        raise


def _cleanup_result_reservation(reservation: _ResultReservation) -> None:
    if reservation.descriptor >= 0:
        try:
            os.close(reservation.descriptor)
        finally:
            reservation.descriptor = -1
    _unlink_if_owned(
        reservation.temporary_path,
        reservation.temporary_identity,
    )


def _try_cleanup_result_reservation(
    reservation: _ResultReservation,
) -> Exception | None:
    try:
        _cleanup_result_reservation(reservation)
    except Exception as exc:
        return exc
    return None


def _require_reserved_temp_identity(reservation: _ResultReservation) -> None:
    try:
        stat = reservation.temporary_path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise RuntimeError("迁移结果临时文件在发布前消失") from exc
    actual_identity = (int(stat.st_dev), int(stat.st_ino))
    if actual_identity != reservation.temporary_identity:
        raise RuntimeError("迁移结果临时文件在发布前被替换，拒绝发布")


def _publish_reserved_result(
    reservation: _ResultReservation,
    payload: dict[str, Any],
) -> None:
    if reservation.descriptor < 0:
        raise RuntimeError("迁移结果临时文件预约已失效")
    with os.fdopen(
        reservation.descriptor,
        "w",
        encoding="utf-8",
        newline="\n",
        closefd=False,
    ) as output:
        json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    _require_reserved_temp_identity(reservation)
    _require_unaliased_evidence_path(
        reservation.final_path,
        reservation.protected_paths,
        must_not_exist=True,
    )
    _publish_file_exclusively(
        reservation.temporary_path,
        reservation.final_path,
    )
    final_stat = reservation.final_path.stat(follow_symlinks=False)
    final_identity = (int(final_stat.st_dev), int(final_stat.st_ino))
    if final_identity != reservation.temporary_identity:
        raise RuntimeError("迁移结果 final 文件不是已预约临时 inode，拒绝确认发布")
    os.close(reservation.descriptor)
    reservation.descriptor = -1
    _unlink_if_owned(
        reservation.temporary_path,
        reservation.temporary_identity,
    )


def _reserve_owned_sqlite_artifact(
    source_path: Path,
    *,
    role: str,
    protected_paths: Sequence[Path],
    seed_path: Path | None = None,
    artifact_root: Path | None = None,
) -> _OwnedSQLiteArtifact:
    target_id = hashlib.sha256(_path_key(source_path).encode("utf-8")).hexdigest()[:8]
    root = (
        source_path.parent
        if artifact_root is None
        else Path(artifact_root).expanduser().resolve(strict=False)
    )
    if not root.is_dir():
        raise ValueError(f"SQLite artifact root is missing or not a directory: {root}")
    container_path = root / f".p0{role[:1]}-{target_id[:4]}-{secrets.token_hex(4)}"
    container_path.mkdir(mode=0o700, exist_ok=False)
    container_stat = container_path.stat(follow_symlinks=False)
    if not stat_module.S_ISDIR(container_stat.st_mode):
        raise RuntimeError(f"SQLite artifact container is not a directory: {container_path}")
    container_identity = (int(container_stat.st_dev), int(container_stat.st_ino))
    path = container_path / "c.db"
    _require_unaliased_evidence_path(path, protected_paths, must_not_exist=True)
    identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            opened_stat = os.fstat(descriptor)
            identity = (int(opened_stat.st_dev), int(opened_stat.st_ino))
            if seed_path is not None:
                seed = Path(seed_path).expanduser().resolve(strict=False)
                if not seed.is_file():
                    raise ValueError(f"SQLite seed is missing or not a file: {seed}")
                with seed.open("rb") as seed_file, os.fdopen(
                    os.dup(descriptor),
                    "wb",
                ) as output:
                    for chunk in iter(lambda: seed_file.read(1024 * 1024), b""):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                final_stat = os.fstat(descriptor)
                if (
                    not stat_module.S_ISREG(final_stat.st_mode)
                    or int(final_stat.st_nlink) != 1
                    or (int(final_stat.st_dev), int(final_stat.st_ino)) != identity
                ):
                    raise RuntimeError(
                        f"owned SQLite artifact changed while seeding: {path}"
                    )
        finally:
            os.close(descriptor)
    except Exception:
        if identity is not None:
            try:
                _unlink_if_owned(path, identity)
            except OSError:
                pass
        try:
            current_container_stat = container_path.stat(follow_symlinks=False)
            if (
                stat_module.S_ISDIR(current_container_stat.st_mode)
                and (
                    int(current_container_stat.st_dev),
                    int(current_container_stat.st_ino),
                )
                == container_identity
            ):
                container_path.rmdir()
        except (FileNotFoundError, OSError):
            pass
        raise
    if identity is None:
        raise RuntimeError("owned SQLite artifact identity was not recorded")
    return _OwnedSQLiteArtifact(
        path=path,
        identity=identity,
        container_path=container_path,
        container_identity=container_identity,
    )


def _require_owned_sqlite_artifact(artifact: _OwnedSQLiteArtifact) -> None:
    container_stat = artifact.container_path.stat(follow_symlinks=False)
    if (
        not stat_module.S_ISDIR(container_stat.st_mode)
        or (int(container_stat.st_dev), int(container_stat.st_ino))
        != artifact.container_identity
    ):
        raise RuntimeError(
            f"owned SQLite artifact container was replaced: {artifact.container_path}"
        )
    stat = artifact.path.stat(follow_symlinks=False)
    if (
        not stat_module.S_ISREG(stat.st_mode)
        or int(stat.st_nlink) != 1
        or (int(stat.st_dev), int(stat.st_ino)) != artifact.identity
    ):
        raise RuntimeError(
            f"owned SQLite artifact is not the reserved unaliased inode: {artifact.path}"
        )


def _cleanup_owned_sqlite_artifact(artifact: _OwnedSQLiteArtifact) -> None:
    try:
        _require_owned_sqlite_artifact(artifact)
    except FileNotFoundError:
        return
    _unlink_if_owned(artifact.path, artifact.identity)
    try:
        container_stat = artifact.container_path.stat(follow_symlinks=False)
        if (
            stat_module.S_ISDIR(container_stat.st_mode)
            and (int(container_stat.st_dev), int(container_stat.st_ino))
            == artifact.container_identity
        ):
            artifact.container_path.rmdir()
    except (FileNotFoundError, OSError):
        pass


def _preparation_path(source_path: Path, manifest_path: Path) -> Path:
    target_id = hashlib.sha256(_path_key(source_path).encode("utf-8")).hexdigest()[:8]
    return manifest_path.with_name(
        f"migration-p-{target_id}-{secrets.token_hex(6)}.json"
    )


def _load_preparation(preparation_path: Path) -> dict[str, Any]:
    path = Path(preparation_path).expanduser().resolve(strict=False)
    payload = _load_manifest(path)
    preparation_format = payload.get("preparation_format")
    if preparation_format != MIGRATION_PREPARATION_FORMAT:
        if preparation_format == "phase0-migration-preparation-v2":
            raise ValueError(
                "phase0-migration-preparation-v2 is retired; re-run prepare "
                "to produce phase0-migration-preparation-v3"
            )
        raise ValueError(
            "unknown migration preparation; re-run prepare to produce "
            "phase0-migration-preparation-v3"
        )
    recorded_path = payload.get("preparation_path")
    if not isinstance(recorded_path, str) or Path(recorded_path).resolve(
        strict=False
    ) != path:
        raise ValueError("migration preparation 路径证据不匹配")
    return payload


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    path = Path(manifest_path).expanduser().resolve(strict=False)
    if not path.is_file():
        raise ValueError(f"备份 manifest 不存在或不是文件：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"备份 manifest 无法读取：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ValueError("备份 manifest 顶层必须是 JSON 对象")
    return payload


def _same_physical_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError as exc:
        if _path_exists_including_symlink(first) and _path_exists_including_symlink(
            second
        ):
            raise ValueError(
                "无法验证源数据库与备份的物理身份是否分离，安全失败关闭："
                f"source={first}, backup={second}"
            ) from exc
        return os.path.normcase(os.path.normpath(str(first))) == os.path.normcase(
            os.path.normpath(str(second))
        )


def _require_manifest_authorization(
    source_path: Path,
    manifest_path: Path,
) -> tuple[dict[str, Any], Path, str]:
    source = Path(source_path).expanduser().resolve(strict=False)
    manifest = _load_manifest(manifest_path)
    if manifest.get("manifest_format") != "phase0-backup-v2":
        raise ValueError(
            "旧版或未知备份 manifest 不能授权迁移；请重新执行 backup 生成 phase0-backup-v2"
        )
    manifest_source_value = manifest.get("source_path")
    if not isinstance(manifest_source_value, str) or not manifest_source_value:
        raise ValueError("备份 manifest 缺少 source_path")
    manifest_source = Path(manifest_source_value).expanduser().resolve(strict=False)
    if manifest_source != source:
        raise ValueError(
            f"备份 manifest 的源数据库不匹配：manifest={manifest_source}，current={source}"
        )
    if not source.is_file():
        raise ValueError(f"源数据库不存在或不是文件：{source}")

    informational_source_sha256 = manifest.get("source_sha256")
    if informational_source_sha256 is not None and (
        not isinstance(informational_source_sha256, str)
        or len(informational_source_sha256) != 64
    ):
        raise ValueError("备份 manifest 的信息性源主文件 SHA-256 格式无效")
    snapshot_logical_sha256 = manifest.get("snapshot_logical_sha256")
    if not isinstance(snapshot_logical_sha256, str) or len(
        snapshot_logical_sha256
    ) != 64:
        raise ValueError("备份 manifest 缺少锁定快照的完整逻辑 SHA-256")
    snapshot_logical_sha256 = snapshot_logical_sha256.casefold()
    snapshot_business_sha256 = manifest.get("snapshot_business_sha256")
    if not isinstance(snapshot_business_sha256, str) or len(
        snapshot_business_sha256
    ) != 64:
        raise ValueError("备份 manifest 缺少锁定快照的业务数据 SHA-256")

    backup_value = manifest.get("backup_path")
    if not isinstance(backup_value, str) or not backup_value:
        raise ValueError("备份 manifest 缺少 backup_path")
    backup_path = Path(backup_value).expanduser().resolve(strict=False)
    if not backup_path.is_file():
        raise ValueError(f"已验证备份不存在或不是文件：{backup_path}")
    if _same_physical_file(source, backup_path):
        raise ValueError("备份与源数据库是同一文件、符号链接或硬链接，拒绝 apply")

    backup_sha256 = manifest.get("backup_sha256")
    if not isinstance(backup_sha256, str) or len(backup_sha256) != 64:
        raise ValueError("备份 manifest 缺少 backup SHA-256")
    if _sha256_file(backup_path).casefold() != backup_sha256.casefold():
        raise ValueError("备份 SHA-256 与 manifest 不匹配，备份可能已被篡改")

    if manifest.get("backup_quick_check") != "ok":
        raise ValueError("备份 manifest 未证明 quick_check=ok")
    if manifest.get("backup_foreign_key_violations") != []:
        raise ValueError("备份 manifest 未明确证明外键检查零违规")
    if manifest.get("table_counts_match") is not True:
        raise ValueError("备份 manifest 未证明表行数一致")
    source_counts = manifest.get("source_table_counts")
    backup_counts = manifest.get("backup_table_counts")
    if not isinstance(source_counts, dict) or source_counts != backup_counts:
        raise ValueError("备份 manifest 的源/备份表行数证据不一致")

    backup_audit = audit_database_identity(backup_path, candidate_paths=[])
    _require_verified_backup(backup_audit)
    if backup_audit.table_counts != backup_counts:
        raise ValueError("备份当前表行数与 manifest 不匹配")
    with closing(sqlite3.connect(backup_path)) as backup_connection:
        backup_logical_sha256 = _sqlite_logical_digest(
            backup_connection,
            include_schema=True,
        )
        backup_business_sha256 = _sqlite_logical_digest(
            backup_connection,
            include_schema=False,
            exclude_tables=("alembic_version",),
        )
    if backup_logical_sha256.casefold() != snapshot_logical_sha256:
        raise ValueError("备份完整逻辑摘要与 manifest 不匹配")
    if backup_business_sha256.casefold() != snapshot_business_sha256.casefold():
        raise ValueError("备份业务数据摘要与 manifest 不匹配")
    return manifest, backup_path, snapshot_logical_sha256


def _require_manifest_and_backup_unchanged(
    manifest_path: Path,
    expected_manifest: dict[str, Any],
    expected_manifest_sha256: str,
    backup_path: Path,
) -> None:
    if _sha256_file(manifest_path) != expected_manifest_sha256:
        raise ValueError("manifest changed during the locked operation")
    if _load_manifest(manifest_path) != expected_manifest:
        raise ValueError("manifest content changed during the locked operation")
    expected_backup_sha256 = expected_manifest.get("backup_sha256")
    if (
        not isinstance(expected_backup_sha256, str)
        or _sha256_file(backup_path).casefold()
        != expected_backup_sha256.casefold()
    ):
        raise ValueError("verified backup changed during the locked operation")


def _connection_verification(
    connection: Connection,
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    quick_rows = connection.exec_driver_sql("PRAGMA quick_check").fetchall()
    quick_values = [str(row[0]) for row in quick_rows]
    quick_check = (
        "ok"
        if quick_values == ["ok"]
        else "; ".join(quick_values) or "no result"
    )
    foreign_key_violations = [
        {
            "table": row[0],
            "rowid": row[1],
            "parent": row[2],
            "foreign_key_index": row[3],
        }
        for row in connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    ]
    table_names = [
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_schema "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND name != 'alembic_version' ORDER BY name"
        ).fetchall()
    ]
    table_counts = {
        table_name: int(
            connection.exec_driver_sql(
                f'SELECT COUNT(*) FROM "{table_name.replace(chr(34), chr(34) * 2)}"'
            ).scalar_one()
        )
        for table_name in table_names
    }
    return quick_check, foreign_key_violations, table_counts


def _manifest_business_counts(manifest: dict[str, Any]) -> dict[str, int]:
    counts = manifest.get("source_table_counts")
    if not isinstance(counts, dict):
        raise ValueError("备份 manifest 缺少 source_table_counts")
    business_counts: dict[str, int] = {}
    for table_name, count in counts.items():
        if not isinstance(table_name, str) or not isinstance(count, int) or count < 0:
            raise ValueError("备份 manifest 表行数格式无效")
        if table_name != "alembic_version":
            business_counts[table_name] = count
    return business_counts


def _raw_driver_connection(connection: Connection):
    proxied = connection.connection
    return getattr(proxied, "driver_connection", proxied)


def _post_migration_schema_verification(
    connection: Connection,
    *,
    expected_head: str,
) -> str:
    config = Config(str(ALEMBIC_INI))
    script = ScriptDirectory.from_config(config)
    baseline = script.get_revision(BASELINE_REVISION)
    if baseline is None:
        raise RuntimeError(f"缺少目标 revision：{expected_head}")
    raw_connection = _raw_driver_connection(connection)
    version_blockers = baseline.module.alembic_version_blockers(raw_connection)
    if version_blockers:
        raise RuntimeError("迁移后版本表校验失败：" + "；".join(version_blockers))
    revision_rows = [
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).fetchall()
    ]
    if revision_rows != [expected_head]:
        raise RuntimeError(
            f"迁移未到唯一 head：after={revision_rows}, head={expected_head}"
        )
    schema_blockers = schema_blockers_for_revision(
        script,
        raw_connection,
        expected_head,
    )
    if schema_blockers:
        raise RuntimeError("迁移后结构校验失败：" + "；".join(schema_blockers))
    return revision_rows[0]


def _upgrade_with_connection(
    database_path: Path,
    connection: Connection,
) -> None:
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config(str(ALEMBIC_INI))
    config.attributes["database_url"] = database_url
    config.attributes["connection"] = connection
    config.set_main_option("sqlalchemy.url", database_url)
    alembic_command.upgrade(config, "head")


def _migrate_verified_candidate(
    candidate_path: Path,
    candidate_connection: sqlite3.Connection,
    *,
    source_connection: sqlite3.Connection,
    expected_before_revision: str | None,
    expected_business_counts: dict[str, int],
    expected_business_digest: str,
    expected_business_projection: dict[str, tuple[str, ...]],
    expected_preserved_digest: str,
    expected_preserved_projection: dict[str, tuple[str, ...]],
) -> tuple[
    SchemaStatus,
    str,
    str,
    list[dict[str, Any]],
    dict[str, int],
    str,
    list[dict[str, Any]],
]:
    before_status = inspect_schema_status(candidate_path)
    if before_status.state not in {"unversioned", "behind", "ready"}:
        raise ValueError(
            "candidate schema state does not allow migration: "
            f"{before_status.state}; " + "; ".join(before_status.blockers)
        )
    if before_status.current_revision != expected_before_revision:
        raise ValueError(
            "candidate revision differs from the locked source snapshot: "
            f"candidate={before_status.current_revision}, "
            f"source={expected_before_revision}"
        )
    engine = create_engine(
        "sqlite://",
        creator=lambda: candidate_connection,
        poolclass=StaticPool,
        pool_reset_on_return=None,
        isolation_level="AUTOCOMMIT",
    )
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            committed = False
            try:
                raw_connection = _raw_driver_connection(connection)
                candidate_source_projection_digest = _sqlite_logical_digest(
                    raw_connection,
                    include_schema=False,
                    projection=expected_business_projection,
                )
                if candidate_source_projection_digest != expected_business_digest:
                    raise RuntimeError(
                        "candidate source projection differs from the locked snapshot"
                    )
                _upgrade_with_connection(candidate_path, connection)
                quick_check, foreign_key_violations, table_counts = (
                    _connection_verification(connection)
                )
                after_revision = _post_migration_schema_verification(
                    connection,
                    expected_head=before_status.head_revision,
                )
                candidate_business_digest = _sqlite_logical_digest(
                    raw_connection,
                    include_schema=False,
                    exclude_tables=("alembic_version",),
                )
                candidate_projection_digest = _sqlite_logical_digest(
                    raw_connection,
                    include_schema=False,
                    projection=expected_preserved_projection,
                )
                if quick_check != "ok" or foreign_key_violations:
                    raise RuntimeError(
                        "candidate post-migration verification failed: "
                        f"quick_check={quick_check}, fk={foreign_key_violations}"
                    )
                changed_original_counts = {
                    table_name: (
                        expected_count,
                        table_counts.get(table_name),
                    )
                    for table_name, expected_count in expected_business_counts.items()
                    if table_counts.get(table_name) != expected_count
                }
                if changed_original_counts:
                    raise RuntimeError(
                        "candidate original table counts differ from the locked snapshot: "
                        f"{changed_original_counts}"
                    )
                if candidate_projection_digest != expected_preserved_digest:
                    raise RuntimeError(
                        "candidate original projection digest differs from the locked "
                        "preserved snapshot"
                    )
                pipeline_status_reclassifications = _verify_pipeline_row_proof(
                    source_connection,
                    raw_connection,
                    before_revision=expected_before_revision,
                )
                connection.exec_driver_sql("COMMIT")
                committed = True
            except Exception:
                if not committed:
                    try:
                        connection.exec_driver_sql("ROLLBACK")
                    except Exception:
                        pass
                raise
    finally:
        engine.dispose(close=False)
    _require_no_sqlite_sidecars(candidate_path)
    if _database_header_uses_wal(candidate_path):
        raise RuntimeError("verified candidate unexpectedly remains in WAL mode")
    return (
        before_status,
        after_revision,
        quick_check,
        foreign_key_violations,
        table_counts,
        candidate_business_digest,
        pipeline_status_reclassifications,
    )


def prepare_verified_schema_migration(
    source: Path,
    manifest_path: Path,
) -> MigrationPreparationReport:
    """Create and fully verify a migrated copy without changing production."""

    source_path = Path(source).expanduser().resolve(strict=False)
    manifest_path = Path(manifest_path).expanduser().resolve(strict=False)
    manifest, backup_path, snapshot_logical_sha256 = (
        _require_manifest_authorization(source_path, manifest_path)
    )
    manifest_sha256 = _sha256_file(manifest_path)
    expected_business_counts = _manifest_business_counts(manifest)
    expected_source_counts = manifest.get("source_table_counts")
    expected_business_digest = str(manifest["snapshot_business_sha256"]).casefold()
    evidence_path = _preparation_path(source_path, manifest_path)
    _require_unaliased_evidence_path(
        evidence_path,
        (source_path, backup_path, manifest_path),
        must_not_exist=True,
    )
    candidate = _reserve_owned_sqlite_artifact(
        source_path,
        role="candidate",
        protected_paths=(source_path, backup_path, manifest_path, evidence_path),
        seed_path=backup_path,
    )
    preparation_persisted = False
    candidate_connection: sqlite3.Connection | None = None
    try:
        initial_source_stat = source_path.stat(follow_symlinks=False)
        if (
            not stat_module.S_ISREG(initial_source_stat.st_mode)
            or int(initial_source_stat.st_nlink) != 1
        ):
            raise RuntimeError(
                f"source database is not one unaliased regular file: {source_path}"
            )
        initial_source_identity = (
            int(initial_source_stat.st_dev),
            int(initial_source_stat.st_ino),
        )
        _require_owned_sqlite_artifact(candidate)
        _require_no_sqlite_sidecars(candidate.path)
        candidate_connection = sqlite3.connect(
            candidate.path,
            isolation_level=None,
            timeout=MAINTENANCE_SQLITE_TIMEOUT_SECONDS,
            check_same_thread=False,
        )
        _require_owned_sqlite_artifact(candidate)
        _require_no_sqlite_sidecars(candidate.path)
        journal_mode = str(
            candidate_connection.execute("PRAGMA journal_mode=MEMORY").fetchone()[0]
        ).casefold()
        if journal_mode != "memory":
            raise RuntimeError(
                f"candidate did not enter MEMORY journal mode: {candidate.path}"
            )
        _require_no_sqlite_sidecars(candidate.path)
        source_uri = f"{source_path.as_uri()}?mode=rw"
        reader_uri = f"{source_path.as_uri()}?mode=ro"
        with _maintenance_lock(source_path):
            with closing(
                sqlite3.connect(
                    source_uri,
                    uri=True,
                    timeout=MAINTENANCE_SQLITE_TIMEOUT_SECONDS,
                    isolation_level=None,
                )
            ) as writer_gate:
                writer_gate.execute("BEGIN IMMEDIATE")
                try:
                    with closing(
                        sqlite3.connect(
                            reader_uri,
                            uri=True,
                            timeout=MAINTENANCE_SQLITE_TIMEOUT_SECONDS,
                            isolation_level=None,
                        )
                    ) as snapshot_reader:
                        snapshot_reader.execute("PRAGMA query_only=ON")
                        snapshot_reader.execute("BEGIN")
                        try:
                            quick_check, foreign_key_violations, source_counts = (
                                _sqlite_snapshot_verification(snapshot_reader)
                            )
                            source_snapshot_logical_sha256 = _sqlite_logical_digest(
                                snapshot_reader,
                                include_schema=True,
                            )
                            source_business_projection = _capture_sqlite_projection(
                                snapshot_reader,
                                exclude_tables=("alembic_version",),
                            )
                            source_before_revision = _database_current_revision(
                                snapshot_reader
                            )
                            source_preserved_projection = build_preserved_projection(
                                source_business_projection,
                                source_before_revision,
                            )
                            source_snapshot_business_sha256 = _sqlite_logical_digest(
                                snapshot_reader,
                                include_schema=False,
                                projection=source_business_projection,
                            )
                            source_preserved_business_sha256 = _sqlite_logical_digest(
                                snapshot_reader,
                                include_schema=False,
                                projection=source_preserved_projection,
                            )
                            if quick_check != "ok" or foreign_key_violations:
                                raise ValueError(
                                    "locked source snapshot verification failed: "
                                    f"quick_check={quick_check}, fk={foreign_key_violations}"
                                )
                            if source_counts != expected_source_counts:
                                raise ValueError(
                                    "locked source snapshot counts differ from manifest"
                                )
                            if (
                                source_snapshot_logical_sha256.casefold()
                                != snapshot_logical_sha256.casefold()
                            ):
                                raise ValueError(
                                    "locked source logical snapshot differs from manifest; backup is stale"
                                )
                            if (
                                source_snapshot_business_sha256.casefold()
                                != expected_business_digest
                            ):
                                raise ValueError(
                                    "locked source business digest differs from manifest; backup is stale"
                                )
                            _require_manifest_and_backup_unchanged(
                                manifest_path,
                                manifest,
                                manifest_sha256,
                                backup_path,
                            )
                            _require_owned_sqlite_artifact(candidate)
                            (
                                before_status,
                                after_revision,
                                candidate_quick_check,
                                candidate_foreign_keys,
                                candidate_counts,
                                candidate_snapshot_business_sha256,
                                pipeline_status_reclassifications,
                            ) = _migrate_verified_candidate(
                                candidate.path,
                                candidate_connection,
                                source_connection=snapshot_reader,
                                expected_before_revision=source_before_revision,
                                expected_business_counts=expected_business_counts,
                                expected_business_digest=expected_business_digest,
                                expected_business_projection=source_business_projection,
                                expected_preserved_digest=(
                                    source_preserved_business_sha256
                                ),
                                expected_preserved_projection=(
                                    source_preserved_projection
                                ),
                            )
                            final_source_digest = _sqlite_logical_digest(
                                snapshot_reader,
                                include_schema=True,
                            )
                            if final_source_digest != source_snapshot_logical_sha256:
                                raise RuntimeError(
                                    "source snapshot changed while writer gate was held"
                                )
                            current_source_stat = source_path.stat(follow_symlinks=False)
                            if (
                                int(current_source_stat.st_dev),
                                int(current_source_stat.st_ino),
                            ) != initial_source_identity:
                                raise RuntimeError(
                                    "source path was replaced while writer gate was held"
                                )
                        finally:
                            snapshot_reader.execute("ROLLBACK")
                finally:
                    writer_gate.execute("ROLLBACK")

        _require_owned_sqlite_artifact(candidate)
        _require_no_sqlite_sidecars(candidate.path)
        candidate_sha256 = _sha256_file(candidate.path)
        candidate_stat = candidate.path.stat(follow_symlinks=False)
        prepared_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        report = MigrationPreparationReport(
            preparation_format=MIGRATION_PREPARATION_FORMAT,
            database_path=str(source_path),
            manifest_path=str(manifest_path),
            backup_path=str(backup_path),
            preparation_path=str(evidence_path),
            candidate_path=str(candidate.path),
            prepared_at_utc=prepared_at,
            before_revision=source_before_revision,
            head_revision=before_status.head_revision,
            source_device=initial_source_identity[0],
            source_inode=initial_source_identity[1],
            manifest_sha256=manifest_sha256,
            source_snapshot_logical_sha256=source_snapshot_logical_sha256,
            source_snapshot_business_sha256=source_snapshot_business_sha256,
            source_business_projection={
                table_name: list(columns)
                for table_name, columns in source_business_projection.items()
            },
            source_preserved_business_sha256=(
                source_preserved_business_sha256
            ),
            pipeline_status_reclassifications=(
                pipeline_status_reclassifications
            ),
            candidate_snapshot_business_sha256=(
                candidate_snapshot_business_sha256
            ),
            candidate_sha256=candidate_sha256,
            candidate_device=int(candidate_stat.st_dev),
            candidate_inode=int(candidate_stat.st_ino),
            candidate_parent_device=candidate.container_identity[0],
            candidate_parent_inode=candidate.container_identity[1],
            quick_check=candidate_quick_check,
            foreign_key_violations=candidate_foreign_keys,
            table_counts=candidate_counts,
        )
        _require_unaliased_evidence_path(
            evidence_path,
            (source_path, backup_path, manifest_path, candidate.path),
            must_not_exist=True,
        )
        _write_json_atomically(evidence_path, report.to_dict())
        preparation_persisted = True
        return report
    finally:
        if candidate_connection is not None:
            candidate_connection.close()
        if not preparation_persisted:
            _cleanup_owned_sqlite_artifact(candidate)


def _verified_preparation_paths(
    preparation_path: Path,
    payload: dict[str, Any],
) -> tuple[Path, Path, Path, Path]:
    values: list[Path] = []
    for field in ("database_path", "manifest_path", "backup_path", "candidate_path"):
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"migration preparation missing {field}")
        values.append(_absolute_lexical_path(Path(value)))
    source_path, manifest_path, backup_path, candidate_path = values
    try:
        candidate_stat = candidate_path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ValueError(f"verified candidate is missing: {candidate_path}") from exc
    if not stat_module.S_ISREG(candidate_stat.st_mode):
        raise ValueError(f"verified candidate is not a regular file: {candidate_path}")
    protected = (source_path, manifest_path, backup_path, preparation_path)
    _require_unaliased_evidence_path(
        candidate_path,
        protected,
        must_not_exist=False,
    )
    return source_path, manifest_path, backup_path, candidate_path


def _require_no_reparse_ancestors(path: Path) -> None:
    absolute_path = _absolute_lexical_path(path)
    for ancestor in (absolute_path.parent, *absolute_path.parent.parents):
        try:
            ancestor_stat = ancestor.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ValueError(
                f"verified candidate ancestor is missing: {ancestor}"
            ) from exc
        file_attributes = int(getattr(ancestor_stat, "st_file_attributes", 0))
        if stat_module.S_ISLNK(ancestor_stat.st_mode) or file_attributes & 0x400:
            raise ValueError(
                f"verified candidate has a symlink or reparse ancestor: {ancestor}"
            )


def _require_candidate_parent_and_file_identity(
    candidate_path: Path,
    payload: dict[str, Any],
) -> None:
    expected_parent_device = payload.get("candidate_parent_device")
    expected_parent_inode = payload.get("candidate_parent_inode")
    expected_device = payload.get("candidate_device")
    expected_inode = payload.get("candidate_inode")
    if not all(
        isinstance(value, int)
        for value in (
            expected_parent_device,
            expected_parent_inode,
            expected_device,
            expected_inode,
        )
    ):
        raise ValueError(
            "migration preparation has no candidate parent/file physical identity"
        )
    _require_no_reparse_ancestors(candidate_path)
    parent_stat = candidate_path.parent.stat(follow_symlinks=False)
    if (
        not stat_module.S_ISDIR(parent_stat.st_mode)
        or (int(parent_stat.st_dev), int(parent_stat.st_ino))
        != (expected_parent_device, expected_parent_inode)
    ):
        raise ValueError("verified candidate parent physical identity changed")
    candidate_stat = candidate_path.stat(follow_symlinks=False)
    if (
        not stat_module.S_ISREG(candidate_stat.st_mode)
        or int(candidate_stat.st_nlink) != 1
        or (int(candidate_stat.st_dev), int(candidate_stat.st_ino))
        != (expected_device, expected_inode)
    ):
        raise ValueError(
            "verified candidate is not the prepared regular single-link file"
        )


def _require_candidate_identity_and_hash(
    candidate_path: Path,
    payload: dict[str, Any],
) -> None:
    expected_hash = payload.get("candidate_sha256")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ValueError("migration preparation has no candidate SHA-256")
    _require_candidate_parent_and_file_identity(candidate_path, payload)
    _require_no_sqlite_sidecars(candidate_path)
    if _database_header_uses_wal(candidate_path):
        raise ValueError("verified candidate unexpectedly advertises WAL mode")
    if _sha256_file(candidate_path).casefold() != expected_hash.casefold():
        raise ValueError("verified candidate SHA-256 changed")


@contextmanager
def _candidate_read_only_snapshot(
    candidate_path: Path,
    payload: dict[str, Any],
) -> Iterator[sqlite3.Connection]:
    """Bind verification to a read-only candidate while denying Windows writes."""

    _require_candidate_identity_and_hash(candidate_path, payload)
    windows_handle: Any | None = None
    descriptor = -1
    close_windows_handle: Any | None = None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        close_windows_handle = kernel32.CloseHandle
        close_windows_handle.argtypes = (wintypes.HANDLE,)
        close_windows_handle.restype = wintypes.BOOL
        windows_handle = create_file(
            str(candidate_path),
            0x80000000,  # GENERIC_READ
            0x00000001,  # FILE_SHARE_READ: deny writes and deletes
            None,
            3,  # OPEN_EXISTING
            0x00000080 | 0x00200000,  # NORMAL | OPEN_REPARSE_POINT
            None,
        )
        if windows_handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            raise ValueError(
                "open candidate handle or sharing conflict prevents the "
                "deny-write/delete verification handle: "
                f"{candidate_path} (winerror={error})"
            )
    else:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(
            os,
            "O_NOFOLLOW",
            0,
        )
        descriptor = os.open(candidate_path, flags)
        opened_stat = os.fstat(descriptor)
        if (
            not stat_module.S_ISREG(opened_stat.st_mode)
            or int(opened_stat.st_nlink) != 1
            or (int(opened_stat.st_dev), int(opened_stat.st_ino))
            != (payload.get("candidate_device"), payload.get("candidate_inode"))
        ):
            os.close(descriptor)
            descriptor = -1
            raise ValueError("candidate no-follow descriptor identity changed")

    connection: sqlite3.Connection | None = None
    try:
        _require_candidate_identity_and_hash(candidate_path, payload)
        candidate_uri = f"{candidate_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(
            candidate_uri,
            uri=True,
            timeout=MAINTENANCE_SQLITE_TIMEOUT_SECONDS,
            isolation_level=None,
        )
        connection.execute("PRAGMA query_only=ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only != (1,):
            raise ValueError("candidate verification connection is not read-only")
        connection.execute("BEGIN")
        try:
            yield connection
        finally:
            connection.execute("ROLLBACK")
        _require_candidate_identity_and_hash(candidate_path, payload)
    finally:
        if connection is not None:
            connection.close()
        if descriptor >= 0:
            os.close(descriptor)
        if windows_handle is not None and close_windows_handle is not None:
            close_windows_handle(windows_handle)
        _require_candidate_parent_and_file_identity(candidate_path, payload)


def _verify_candidate_against_preparation(
    candidate_path: Path,
    payload: dict[str, Any],
    *,
    source_connection: sqlite3.Connection,
    source_full_projection: dict[str, tuple[str, ...]],
    before_revision: str | None,
    source_preserved_digest: str,
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    expected_preserved_digest = payload.get("source_preserved_business_sha256")
    if (
        not isinstance(expected_preserved_digest, str)
        or len(expected_preserved_digest) != 64
        or expected_preserved_digest.casefold() != source_preserved_digest.casefold()
    ):
        raise ValueError("source preserved business digest differs from preparation")
    expected_reclassifications = _validated_prepared_reclassifications(payload)
    with _candidate_read_only_snapshot(candidate_path, payload) as connection:
        status = inspect_schema_status(candidate_path)
        if (
            status.state != "ready"
            or status.current_revision != payload.get("head_revision")
        ):
            raise ValueError(
                "verified candidate is no longer at head: "
                f"{status.state}, {status.blockers}"
            )
        if _database_current_revision(connection) != payload.get("head_revision"):
            raise ValueError("candidate revision changed during read-only verification")
        quick_check, foreign_key_violations, table_counts = (
            _sqlite_snapshot_verification(connection)
        )
        table_counts = {
            name: count
            for name, count in table_counts.items()
            if name != "alembic_version"
        }
        business_digest = _sqlite_logical_digest(
            connection,
            include_schema=False,
            exclude_tables=("alembic_version",),
        )
        if business_digest != payload.get("candidate_snapshot_business_sha256"):
            raise ValueError("verified candidate business digest changed")
        preserved_projection = build_preserved_projection(
            source_full_projection,
            before_revision,
        )
        candidate_preserved_digest = _sqlite_logical_digest(
            connection,
            include_schema=False,
            projection=preserved_projection,
        )
        if candidate_preserved_digest.casefold() != source_preserved_digest.casefold():
            raise ValueError(
                "verified candidate preserved business digest changed"
            )
        actual_reclassifications = _verify_pipeline_row_proof(
            source_connection,
            connection,
            before_revision=before_revision,
        )
    if quick_check != "ok" or foreign_key_violations:
        raise ValueError(
            "verified candidate failed final integrity checks: "
            f"quick_check={quick_check}, fk={foreign_key_violations}"
        )
    if table_counts != payload.get("table_counts"):
        raise ValueError("verified candidate table counts changed")
    if actual_reclassifications != expected_reclassifications:
        raise ValueError(
            "candidate reclassification proof differs from preparation"
        )
    return quick_check, foreign_key_violations, table_counts


def _preparation_business_projection(
    payload: dict[str, Any],
) -> dict[str, tuple[str, ...]]:
    raw_projection = payload.get("source_business_projection")
    if not isinstance(raw_projection, dict) or not raw_projection:
        raise ValueError("migration preparation has no source business projection")
    projection: dict[str, tuple[str, ...]] = {}
    for table_name, raw_columns in raw_projection.items():
        if (
            not isinstance(table_name, str)
            or not table_name
            or not isinstance(raw_columns, list)
            or not raw_columns
            or any(not isinstance(column, str) or not column for column in raw_columns)
            or len(set(raw_columns)) != len(raw_columns)
        ):
            raise ValueError("migration preparation business projection is malformed")
        projection[table_name] = tuple(raw_columns)
    return projection


def _require_prepared_source_identity(
    source_path: Path,
    payload: dict[str, Any],
) -> tuple[int, int]:
    expected_device = payload.get("source_device")
    expected_inode = payload.get("source_inode")
    if not isinstance(expected_device, int) or not isinstance(expected_inode, int):
        raise ValueError("migration preparation has no source physical identity")
    stat = source_path.stat(follow_symlinks=False)
    identity = (int(stat.st_dev), int(stat.st_ino))
    if (
        not stat_module.S_ISREG(stat.st_mode)
        or int(stat.st_nlink) != 1
        or identity != (expected_device, expected_inode)
    ):
        raise ValueError(
            "source physical identity differs from preparation; preparation is stale"
        )
    return identity


def _prove_no_open_database_handles(paths: Sequence[Path]) -> None:
    """Prove a Windows offline-handoff moment without mutating any database."""

    if os.name != "nt":
        raise ValueError(
            "manual cutover handoff requires Windows open-handle exclusion proof"
        )

    import ctypes
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    generic_read = 0x80000000
    file_share_delete = 0x00000004
    open_existing = 3
    file_attribute_normal = 0x00000080
    invalid_handle = ctypes.c_void_p(-1).value
    handles: list[Any] = []
    try:
        for path in paths:
            handle = create_file(
                str(path),
                generic_read,
                file_share_delete,
                None,
                open_existing,
                file_attribute_normal,
                None,
            )
            if handle == invalid_handle:
                error = ctypes.get_last_error()
                raise ValueError(
                    "open database handle or sharing conflict prevents manual "
                    f"cutover handoff: {path} (winerror={error})"
                )
            handles.append(handle)
    finally:
        for handle in reversed(handles):
            close_handle(handle)


def cutover_prepared_schema_migration(
    preparation_path: Path,
    *,
    confirm_services_stopped: bool = False,
) -> NoReturn:
    """Validate the candidate, then fail closed with an explicit manual handoff."""

    if not confirm_services_stopped:
        raise ValueError(
            "cutover requires --confirm-services-stopped; prepare alone never changes production"
        )
    preparation_path = Path(preparation_path).expanduser().resolve(strict=False)
    payload = _load_preparation(preparation_path)
    source_path, manifest_path, backup_path, candidate_path = (
        _verified_preparation_paths(preparation_path, payload)
    )
    expected_manifest_sha256 = payload.get("manifest_sha256")
    if not isinstance(expected_manifest_sha256, str) or len(
        expected_manifest_sha256
    ) != 64:
        raise ValueError("migration preparation has no manifest SHA-256")
    expected_logical_digest = payload.get("source_snapshot_logical_sha256")
    expected_business_digest = payload.get("source_snapshot_business_sha256")
    expected_candidate_business_digest = payload.get(
        "candidate_snapshot_business_sha256"
    )
    if not isinstance(expected_logical_digest, str) or not isinstance(
        expected_business_digest, str
    ) or not isinstance(expected_candidate_business_digest, str):
        raise ValueError("migration preparation has no locked source digests")
    source_business_projection = _preparation_business_projection(payload)
    prepared_before_revision = payload.get("before_revision")
    if prepared_before_revision is not None and not isinstance(
        prepared_before_revision,
        str,
    ):
        raise ValueError("migration preparation has an invalid before_revision")

    source_uri = f"{source_path.as_uri()}?mode=rw"
    reader_uri = f"{source_path.as_uri()}?mode=ro"
    with _maintenance_lock(source_path):
        _require_prepared_source_identity(source_path, payload)
        with closing(
            sqlite3.connect(
                source_uri,
                uri=True,
                timeout=MAINTENANCE_SQLITE_TIMEOUT_SECONDS,
                isolation_level=None,
            )
        ) as writer_gate:
            writer_gate.execute("BEGIN IMMEDIATE")
            try:
                _require_prepared_source_identity(source_path, payload)
                manifest, authorized_backup, snapshot_digest = (
                    _require_manifest_authorization(source_path, manifest_path)
                )
                if authorized_backup != backup_path:
                    raise ValueError("preparation backup path differs from manifest")
                if _sha256_file(manifest_path) != expected_manifest_sha256:
                    raise ValueError("manifest changed after candidate preparation")
                if snapshot_digest.casefold() != expected_logical_digest.casefold():
                    raise ValueError("manifest snapshot digest differs from preparation")
                manifest_business_digest = manifest.get("snapshot_business_sha256")
                if (
                    not isinstance(manifest_business_digest, str)
                    or manifest_business_digest.casefold()
                    != expected_business_digest.casefold()
                ):
                    raise ValueError(
                        "manifest business snapshot digest differs from preparation"
                    )

                with closing(
                    sqlite3.connect(
                        reader_uri,
                        uri=True,
                        timeout=MAINTENANCE_SQLITE_TIMEOUT_SECONDS,
                        isolation_level=None,
                    )
                ) as snapshot_reader:
                    snapshot_reader.execute("PRAGMA query_only=ON")
                    snapshot_reader.execute("BEGIN")
                    try:
                        quick_check, foreign_key_violations, source_counts = (
                            _sqlite_snapshot_verification(snapshot_reader)
                        )
                        live_logical_digest = _sqlite_logical_digest(
                            snapshot_reader,
                            include_schema=True,
                        )
                        live_before_revision = _database_current_revision(
                            snapshot_reader
                        )
                        if live_before_revision != prepared_before_revision:
                            raise ValueError(
                                "live source revision differs from preparation: "
                                f"live={live_before_revision}, "
                                f"prepared={prepared_before_revision}"
                            )
                        live_full_projection = _capture_sqlite_projection(
                            snapshot_reader,
                            exclude_tables=("alembic_version",),
                        )
                        if live_full_projection != source_business_projection:
                            raise ValueError(
                                "live source full business projection differs from preparation"
                            )
                        live_business_digest = _sqlite_logical_digest(
                            snapshot_reader,
                            include_schema=False,
                            projection=live_full_projection,
                        )
                        live_preserved_projection = build_preserved_projection(
                            live_full_projection,
                            live_before_revision,
                        )
                        live_preserved_digest = _sqlite_logical_digest(
                            snapshot_reader,
                            include_schema=False,
                            projection=live_preserved_projection,
                        )
                        if quick_check != "ok" or foreign_key_violations:
                            raise ValueError(
                                "live source failed cutover verification: "
                                f"quick_check={quick_check}, fk={foreign_key_violations}"
                            )
                        if source_counts != manifest.get("source_table_counts"):
                            raise ValueError(
                                "live source counts differ from manifest; preparation is stale"
                            )
                        if (
                            live_logical_digest.casefold()
                            != expected_logical_digest.casefold()
                        ):
                            raise ValueError(
                                "live source logical digest differs from preparation; "
                                "preparation is stale"
                            )
                        if (
                            live_business_digest.casefold()
                            != expected_business_digest.casefold()
                        ):
                            raise ValueError(
                                "live source business digest differs from preparation; "
                                "preparation is stale"
                            )

                        _verify_candidate_against_preparation(
                            candidate_path,
                            payload,
                            source_connection=snapshot_reader,
                            source_full_projection=live_full_projection,
                            before_revision=live_before_revision,
                            source_preserved_digest=live_preserved_digest,
                        )
                        _require_manifest_and_backup_unchanged(
                            manifest_path,
                            manifest,
                            expected_manifest_sha256,
                            backup_path,
                        )
                        _require_prepared_source_identity(source_path, payload)
                        if (
                            _sqlite_logical_digest(
                                snapshot_reader,
                                include_schema=True,
                            )
                            != live_logical_digest
                        ):
                            raise RuntimeError(
                                "live source snapshot changed while writer gate was held"
                            )
                    finally:
                        snapshot_reader.execute("ROLLBACK")
            finally:
                writer_gate.execute("ROLLBACK")

    _require_prepared_source_identity(source_path, payload)
    _require_candidate_identity_and_hash(candidate_path, payload)
    _prove_no_open_database_handles((source_path, candidate_path))

    raise ManualCutoverRequiredError(
        "automatic production cutover is disabled: Windows ReplaceFileW cannot be "
        "proven to replace the database while a deny-write handle remains held, and "
        "releasing that handle would reopen a data-loss race. Production was not changed. "
        f"Keep verified candidate={candidate_path}, backup={backup_path}, "
        f"preparation={preparation_path}; perform the final replacement only through an "
        "approved fully-offline manual procedure with independent rollback evidence.",
        source_path=source_path,
        candidate_path=candidate_path,
        backup_path=backup_path,
        preparation_path=preparation_path,
    )


def apply_verified_schema_migration(
    source: Path,
    manifest_path: Path,
) -> MigrationPreparationReport:
    """Compatibility alias: apply now prepares only and never changes production."""

    return prepare_verified_schema_migration(source, manifest_path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SQLite 身份审计与在线备份")
    commands = parser.add_subparsers(dest="command", required=True)

    audit_parser = commands.add_parser("audit", help="只读审计数据库身份")
    audit_parser.add_argument("--database", type=Path)
    audit_parser.add_argument("--candidate", type=Path, action="append", default=None)
    audit_parser.add_argument("--report-root", type=Path, action="append", default=None)
    audit_parser.add_argument("--json", action="store_true", dest="as_json")

    backup_parser = commands.add_parser("backup", help="创建并验证在线备份")
    backup_parser.add_argument("--database", type=Path, required=True)
    backup_parser.add_argument("--backup-root", type=Path, required=True)
    backup_parser.add_argument("--json", action="store_true", dest="as_json")

    status_parser = commands.add_parser(
        "schema-status",
        help="只读检查 Alembic schema 状态",
    )
    status_parser.add_argument("--database", type=Path, required=True)
    status_parser.add_argument("--json", action="store_true", dest="as_json")

    apply_parser = commands.add_parser(
        "apply",
        help="凭 v2 备份生成并验证迁移候选；绝不修改生产数据库",
    )
    apply_parser.add_argument("--database", type=Path, required=True)
    apply_parser.add_argument("--manifest", type=Path, required=True)
    apply_parser.add_argument("--json", action="store_true", dest="as_json")

    cutover_parser = commands.add_parser(
        "cutover",
        help="复核候选并输出人工停机切换要求；不会自动替换生产库",
    )
    cutover_parser.add_argument("--preparation", type=Path, required=True)
    cutover_parser.add_argument(
        "--confirm-services-stopped",
        action="store_true",
        required=True,
    )
    cutover_parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    system_drive: str | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "audit":
        database_path = args.database
        if database_path is None:
            database_path = resolve_runtime_identity(
                Path(__file__)
            ).canonical_database_path
        report = audit_database_identity(
            database_path,
            candidate_paths=args.candidate,
        )
        legacy_findings = (
            audit_legacy_material_reports(report_roots=args.report_root)
            if args.report_root is not None
            else None
        )
        if args.as_json:
            payload = report.to_dict()
            if legacy_findings is not None:
                payload["legacy_report_findings"] = [
                    asdict(finding) for finding in legacy_findings
                ]
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            _print_human_audit(report)
            if legacy_findings is not None:
                for finding in legacy_findings:
                    print(f"[{finding.status}] {finding.path}: {finding.reason}")
        report_audit_failed = bool(
            legacy_findings
            and any(
                finding.status in {"missing", "unreadable", "malformed"}
                for finding in legacy_findings
            )
        )
        return 2 if report.blockers or report_audit_failed else 0

    if args.command == "schema-status":
        report = inspect_schema_status(args.database)
        if args.as_json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
        else:
            _print_human_schema_status(report)
        return 2 if report.blockers else 0

    if args.command == "apply":
        try:
            report = prepare_verified_schema_migration(
                args.database,
                args.manifest,
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            sqlite3.Error,
            SQLAlchemyError,
        ) as exc:
            if args.as_json:
                print(
                    json.dumps(
                        {"ok": False, "error": str(exc)},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            else:
                print(f"迁移失败：{exc}", file=sys.stderr)
            return 2
        payload = {
            **report.to_dict(),
            "production_changed": False,
            "manual_cutover_required": True,
            "next_step": "等待完整 Phase 0 验收后，由 root 执行人工受控离线切换",
        }
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(f"候选验证完成，生产未改变：{report.candidate_path}")
            print(f"准备证据：{report.preparation_path}")
            print(f"下一步：{payload['next_step']}")
        return 0

    if args.command == "cutover":
        try:
            cutover_prepared_schema_migration(
                args.preparation,
                confirm_services_stopped=args.confirm_services_stopped,
            )
        except ManualCutoverRequiredError as exc:
            payload = {
                "ok": False,
                "production_changed": False,
                "manual_cutover_required": True,
                "source_path": str(exc.source_path),
                "candidate_path": str(exc.candidate_path),
                "backup_path": str(exc.backup_path),
                "preparation_path": str(exc.preparation_path),
                "instruction": (
                    "停止全部服务，复核 source/candidate/backup 后，由 root 按批准的人工离线流程切换"
                ),
                "error": str(exc),
            }
            if args.as_json:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            else:
                print(str(exc), file=sys.stderr)
                print(f"候选：{exc.candidate_path}", file=sys.stderr)
                print(f"备份：{exc.backup_path}", file=sys.stderr)
            return 2
        except (
            OSError,
            RuntimeError,
            ValueError,
            sqlite3.Error,
            SQLAlchemyError,
        ) as exc:
            payload = {
                "ok": False,
                "production_changed": False,
                "error": str(exc),
            }
            if args.as_json:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            else:
                print(f"切换失败：{exc}", file=sys.stderr)
            return 2
        raise AssertionError("cutover unexpectedly returned instead of failing closed")

    try:
        report = create_verified_backup(
            args.database,
            args.backup_root,
            system_drive=system_drive,
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        if args.as_json:
            print(
                json.dumps(
                    {"ok": False, "error": str(exc)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print(f"备份失败：{exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(f"备份完成：{report.backup_path}")
        print(f"校验：quick_check=ok，SHA-256={report.backup_sha256}")
    return 0


def _print_human_audit(report: DatabasePreflightReport) -> None:
    state = "通过" if not report.blockers else "阻断"
    print(f"数据库预检：{state}")
    print(f"规范路径：{report.canonical_database_path}")
    for warning in report.warnings:
        print(f"警告：{warning}")
    for blocker in report.blockers:
        print(f"阻断：{blocker}")


def _print_human_schema_status(report: SchemaStatus) -> None:
    print(f"Schema 状态：{report.state}")
    print(f"数据库：{report.database_path}")
    print(f"当前 revision：{report.current_revision or 'base/unversioned'}")
    print(f"Head revision：{report.head_revision}")
    for blocker in report.blockers:
        print(f"阻断：{blocker}")


if __name__ == "__main__":
    raise SystemExit(main())
