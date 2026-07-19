from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True, slots=True)
class DatabasePreflightReport:
    canonical_database_path: str
    canonical_exists: bool
    canonical_size_bytes: int | None
    quick_check: str | None
    foreign_key_violations: list[dict[str, Any]]
    table_counts: dict[str, int]
    warnings: list[str]
    blockers: list[str]
    inspected_candidate_paths: list[str]

    @property
    def is_blocked(self) -> bool:
        return bool(self.blockers)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def audit_database_identity(
    canonical_database_path: Path,
    *,
    candidate_paths: Sequence[Path] | None = None,
) -> DatabasePreflightReport:
    canonical = _absolute_path(canonical_database_path)
    candidates = (
        [canonical.parent / "backend" / canonical.name]
        if candidate_paths is None
        else list(candidate_paths)
    )
    warnings: list[str] = []
    blockers: list[str] = []
    inspected_paths = [str(canonical)]

    canonical_exists = canonical.exists()
    canonical_size: int | None = None
    quick_check: str | None = None
    foreign_key_violations: list[dict[str, Any]] = []
    table_counts: dict[str, int] = {}

    if canonical_exists:
        if not canonical.is_file():
            blockers.append(f"规范数据库路径不是普通文件：{canonical}")
        else:
            before_stat = canonical.stat()
            canonical_size = before_stat.st_size
            sidecars = [
                Path(f"{canonical}{suffix}")
                for suffix in ("-journal", "-wal", "-shm")
            ]
            sidecars_before = {path: path.exists() for path in sidecars}
            header_uses_wal = False
            header_read_failed = False
            if canonical_size > 0:
                try:
                    header_uses_wal = _database_header_uses_wal(canonical)
                except OSError as exc:
                    header_read_failed = True
                    blockers.append(
                        f"无法读取 SQLite 数据库头：{canonical}；"
                        f"请检查文件锁定和读取权限后重试（{type(exc).__name__}）"
                    )
            if canonical_size == 0:
                blockers.append(f"规范数据库文件为零字节，需先确认或恢复：{canonical}")
            elif header_read_failed:
                pass
            elif header_uses_wal:
                blockers.append(
                    "数据库处于 WAL 模式；SQLite 即使以 mode=ro 打开也可能改写 "
                    "SHM sidecar。为保证严格只读，已在连接前停止检查。"
                    f"请通过已批准的维护流程切回 DELETE 模式后重试：{canonical}"
                )
            elif any(sidecars_before.values()):
                existing_sidecars = [
                    str(path) for path, exists in sidecars_before.items() if exists
                ]
                blockers.append(
                    "检测到既有 SQLite sidecar；为避免只读连接清理或改写它们，"
                    "已在连接前停止检查。请先通过已批准的维护流程恢复一致状态："
                    + ", ".join(existing_sidecars)
                )
            else:
                try:
                    quick_check, foreign_key_violations, table_counts = (
                        _inspect_sqlite_read_only(canonical)
                    )
                except sqlite3.Error as exc:
                    blockers.append(
                        f"SQLite 只读完整性检查失败：{canonical}；"
                        f"请从已验证备份恢复后重试（{exc}）"
                    )
                else:
                    if quick_check != "ok":
                        blockers.append(
                            f"SQLite 完整性检查未通过：{canonical}；"
                            f"quick_check={quick_check}"
                        )
                    if foreign_key_violations:
                        blockers.append(
                            f"数据库存在 {len(foreign_key_violations)} 条外键违规："
                            f"{canonical}；请先修复或恢复备份"
                        )

                after_stat = canonical.stat()
                if (
                    after_stat.st_size != before_stat.st_size
                    or after_stat.st_mtime_ns != before_stat.st_mtime_ns
                ):
                    blockers.append(
                        f"只读预检期间数据库发生外部变化，请停止启动并重试：{canonical}"
                    )
                created_sidecars = [
                    str(path)
                    for path, existed in sidecars_before.items()
                    if not existed and path.exists()
                ]
                if created_sidecars:
                    blockers.append(
                        "只读预检意外创建了 SQLite sidecar，请停止启动并检查权限："
                        + ", ".join(created_sidecars)
                    )

    seen_candidates: list[Path] = [canonical]
    for raw_candidate in candidates:
        candidate = _absolute_path(raw_candidate)
        if any(_same_physical_path(candidate, seen) for seen in seen_candidates):
            continue
        seen_candidates.append(candidate)
        inspected_paths.append(str(candidate))
        if not candidate.exists():
            continue
        if not candidate.is_file():
            blockers.append(f"候选数据库路径不是普通文件：{candidate}")
            continue

        candidate_size = candidate.stat().st_size
        if candidate_size == 0:
            warnings.append(
                f"发现零字节旧数据库文件，已保留且不阻断启动：{candidate}"
            )
            continue
        blockers.append(
            f"发现非空重复数据库，禁止自动选择或合并：{candidate} "
            f"({candidate_size} 字节)"
        )

    return DatabasePreflightReport(
        canonical_database_path=str(canonical),
        canonical_exists=canonical_exists,
        canonical_size_bytes=canonical_size,
        quick_check=quick_check,
        foreign_key_violations=foreign_key_violations,
        table_counts=table_counts,
        warnings=warnings,
        blockers=blockers,
        inspected_candidate_paths=inspected_paths,
    )


def _inspect_sqlite_read_only(
    database_path: Path,
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    uri = f"{database_path.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
        connection.execute("PRAGMA query_only=ON")
        quick_check_rows = connection.execute("PRAGMA quick_check").fetchall()
        quick_check_values = [str(row[0]) for row in quick_check_rows]
        quick_check = (
            "ok"
            if quick_check_values == ["ok"]
            else "; ".join(quick_check_values) or "no result"
        )
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
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
        ]
        table_counts = {
            table_name: int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table_name.replace(chr(34), chr(34) * 2)}"'
                ).fetchone()[0]
            )
            for table_name in table_names
        }
    return quick_check, foreign_key_violations, table_counts


def _absolute_path(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _database_header_uses_wal(database_path: Path) -> bool:
    with database_path.open("rb") as database_file:
        header = database_file.read(20)
    return (
        len(header) >= 20
        and header[:16] == b"SQLite format 3\x00"
        and (header[18] == 2 or header[19] == 2)
    )


def _same_physical_path(first: Path, second: Path) -> bool:
    if first.exists() and second.exists():
        try:
            return os.path.samefile(first, second)
        except OSError:
            pass
    return os.path.normcase(os.path.normpath(str(first))) == os.path.normcase(
        os.path.normpath(str(second))
    )
