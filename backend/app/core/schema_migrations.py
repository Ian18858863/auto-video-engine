from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError

from app.core.database_preflight import audit_database_identity


BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
BASELINE_REVISION = "20260711_0001"


@dataclass(frozen=True, slots=True)
class SchemaStatus:
    database_path: str
    state: str
    current_revision: str | None
    head_revision: str
    blockers: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _config(
    database_path: Path | None = None,
    *,
    fresh_only: bool = False,
) -> Config:
    config = Config(str(ALEMBIC_INI))
    if database_path is not None:
        absolute_path = Path(database_path).expanduser().resolve(strict=False)
        if not absolute_path.is_absolute():
            raise ValueError("schema migration 数据库路径必须是绝对路径")
        database_url = f"sqlite:///{absolute_path.as_posix()}"
        config.attributes["database_url"] = database_url
        config.set_main_option("sqlalchemy.url", database_url)
    if fresh_only:
        config.attributes["fresh_only"] = True
    return config


def _script_state() -> tuple[ScriptDirectory, str]:
    script = ScriptDirectory.from_config(_config())
    head = script.get_current_head()
    if head is None:
        raise RuntimeError("Alembic migration head 不存在")
    return script, str(head)


def _status(
    database_path: Path,
    state: str,
    head_revision: str,
    *,
    current_revision: str | None = None,
    blockers: list[str] | None = None,
) -> SchemaStatus:
    return SchemaStatus(
        database_path=str(database_path),
        state=state,
        current_revision=current_revision,
        head_revision=head_revision,
        blockers=[] if blockers is None else blockers,
    )


def schema_blockers_for_revision(
    script: ScriptDirectory,
    connection: sqlite3.Connection,
    revision: str,
) -> list[str]:
    baseline = script.get_revision(BASELINE_REVISION)
    target = script.get_revision(revision)
    if baseline is None:
        return [f"missing frozen baseline revision: {BASELINE_REVISION}"]
    if target is None:
        return [f"unknown schema revision: {revision}"]
    if revision == BASELINE_REVISION:
        return list(
            baseline.module.legacy_schema_blockers(
                connection,
                require_pipeline_fk=True,
            )
        )
    validator = getattr(target.module, "schema_blockers", None)
    if not callable(validator):
        return [f"revision {revision} has no exact schema validator"]
    return list(
        validator(
            connection,
            script=script,
            revision=revision,
        )
    )


def inspect_schema_status(database_path: Path) -> SchemaStatus:
    path = Path(database_path).expanduser().resolve(strict=False)
    script, head_revision = _script_state()
    if not path.exists():
        return _status(path, "missing", head_revision)
    if not path.is_file():
        return _status(
            path,
            "invalid",
            head_revision,
            blockers=[f"数据库路径不是普通文件：{path}"],
        )

    audit = audit_database_identity(path, candidate_paths=[])
    if audit.blockers:
        return _status(
            path,
            "invalid",
            head_revision,
            blockers=list(audit.blockers),
        )

    uri = f"{path.as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
            connection.execute("PRAGMA query_only=ON")
            user_schema_objects = [
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT type, name FROM sqlite_schema "
                    "WHERE type IN ('table', 'view', 'trigger', 'index') "
                    "AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY type, name"
                ).fetchall()
            ]
            table_names = {
                name
                for object_type, name in user_schema_objects
                if object_type == "table"
            }
            business_tables = table_names - {"alembic_version"}
            baseline = script.get_revision(BASELINE_REVISION)
            if baseline is None:
                return _status(
                    path,
                    "invalid",
                    head_revision,
                    blockers=[f"缺少冻结基线 revision：{BASELINE_REVISION}"],
                )
            if "alembic_version" in table_names:
                version_shape_blockers = baseline.module.alembic_version_blockers(
                    connection
                )
                if version_shape_blockers:
                    return _status(
                        path,
                        "invalid",
                        head_revision,
                        blockers=list(version_shape_blockers),
                    )
                version_rows = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT version_num FROM alembic_version"
                    ).fetchall()
                ]
            else:
                version_rows = []

            if len(version_rows) > 1:
                return _status(
                    path,
                    "invalid",
                    head_revision,
                    blockers=["alembic_version 存在多行，版本状态无效"],
                )
            current_revision = version_rows[0] if version_rows else None
            try:
                known_revision = (
                    script.get_revision(current_revision)
                    if current_revision
                    else None
                )
            except CommandError:
                known_revision = None
            if current_revision and known_revision is None:
                return _status(
                    path,
                    "ahead",
                    head_revision,
                    current_revision=current_revision,
                    blockers=[
                        f"数据库 revision {current_revision} 不属于当前 migration 历史，禁止启动"
                    ],
                )

            if not business_tables:
                if user_schema_objects:
                    object_summary = ", ".join(
                        f"{object_type}:{name}"
                        for object_type, name in user_schema_objects
                    )
                    return _status(
                        path,
                        "invalid",
                        head_revision,
                        current_revision=current_revision,
                        blockers=[
                            "数据库包含非内部 schema 对象，但没有冻结业务 schema；"
                            "这不是可自动初始化的空白数据库："
                            f"{object_summary}"
                        ],
                    )
                return _status(path, "empty", head_revision)

            try:
                shape_blockers = (
                    baseline.module.legacy_schema_blockers(
                        connection,
                        require_pipeline_fk=None,
                    )
                    if current_revision is None
                    else schema_blockers_for_revision(
                        script,
                        connection,
                        current_revision,
                    )
                )
            except Exception as exc:
                return _status(
                    path,
                    "invalid",
                    head_revision,
                    current_revision=current_revision,
                    blockers=[
                        "schema validator failed closed: "
                        f"{type(exc).__name__}: {exc}"
                    ],
                )
            if shape_blockers:
                return _status(
                    path,
                    "invalid",
                    head_revision,
                    current_revision=current_revision,
                    blockers=list(shape_blockers),
                )

            if current_revision is None:
                state = "behind" if "alembic_version" in table_names else "unversioned"
                return _status(
                    path,
                    state,
                    head_revision,
                    blockers=[
                        "现有数据库未到 migration head；请先创建并核验备份，"
                        "再运行 phase0_migration apply"
                    ],
                )
            if current_revision != head_revision:
                return _status(
                    path,
                    "behind",
                    head_revision,
                    current_revision=current_revision,
                    blockers=[
                        f"数据库 revision={current_revision}，head={head_revision}；"
                        "请先核验备份再执行维护迁移"
                    ],
                )
            return _status(
                path,
                "ready",
                head_revision,
                current_revision=current_revision,
            )
    except sqlite3.Error as exc:
        return _status(
            path,
            "invalid",
            head_revision,
            blockers=[f"读取 schema 状态失败：{exc}"],
        )


def upgrade_fresh_database(database_path: Path) -> None:
    path = Path(database_path).expanduser().resolve(strict=False)
    status = inspect_schema_status(path)
    if status.state not in {"missing", "empty"}:
        raise RuntimeError(
            "只允许通过启动路径初始化缺失或空白数据库；"
            f"当前状态={status.state}。现有数据库必须先备份并通过维护 apply。"
        )
    if not path.parent.is_dir():
        raise RuntimeError(f"数据库父目录不存在：{path.parent}")
    command.upgrade(_config(path, fresh_only=True), "head")


def ensure_schema_ready(database_path: Path) -> None:
    path = Path(database_path).expanduser().resolve(strict=False)
    status = inspect_schema_status(path)
    if status.state in {"missing", "empty"}:
        upgrade_fresh_database(path)
        status = inspect_schema_status(path)
    if status.state == "ready":
        return
    detail = "；".join(status.blockers) or status.state
    raise RuntimeError(
        "数据库 schema 未就绪，启动已停止。"
        f"数据库：{path}；状态：{status.state}；问题：{detail}"
    )
