from __future__ import annotations

from pathlib import Path, PureWindowsPath

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, make_url
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from app.core.database import Base
from app.core.schema_migrations import (
    BASELINE_REVISION,
    schema_blockers_for_revision,
)


config = context.config
target_metadata = Base.metadata


def _explicit_database_url() -> str:
    candidates = [config.attributes.get("database_url")]
    try:
        candidates.append(context.get_x_argument(as_dictionary=True).get("database_url"))
    except (AttributeError, TypeError):
        pass
    candidates.append(config.get_main_option("sqlalchemy.url"))
    database_url = next(
        (str(candidate).strip() for candidate in candidates if candidate),
        "",
    )
    if not database_url:
        raise RuntimeError(
            "迁移命令必须显式提供临时或已批准数据库的绝对 SQLite URL；"
            "禁止默认连接生产数据库"
        )

    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise RuntimeError("本产品的 schema migration 只支持 SQLite 文件数据库")
    database = url.database
    if not database or database == ":memory:":
        raise RuntimeError("schema migration 必须使用绝对 SQLite 文件路径")
    database_path = Path(database)
    if not (
        database_path.is_absolute()
        or PureWindowsPath(database).is_absolute()
    ):
        raise RuntimeError(
            f"schema migration 数据库路径必须是绝对路径：{database}"
        )
    return database_url


def _raw_sqlite_connection(connection: Connection):
    proxied = connection.connection
    return getattr(proxied, "driver_connection", proxied)


def _preflight_before_alembic_writes(connection: Connection) -> None:
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    business_tables = table_names - {"alembic_version"}
    user_schema_objects = [
        (str(row[0]), str(row[1]))
        for row in connection.exec_driver_sql(
            "SELECT type, name FROM sqlite_schema "
            "WHERE type IN ('table', 'view', 'trigger', 'index') "
            "AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
    ]
    if config.attributes.get("fresh_only") and user_schema_objects:
        object_summary = ", ".join(
            f"{object_type}:{name}"
            for object_type, name in user_schema_objects
        )
        raise RuntimeError(
            "fresh bootstrap 竞态检查失败：数据库包含非内部 schema 对象，"
            f"拒绝自动升级：{object_summary}"
        )
    script = ScriptDirectory.from_config(config)
    baseline = script.get_revision(BASELINE_REVISION)
    if baseline is None:
        raise RuntimeError(f"缺少冻结基线 revision：{BASELINE_REVISION}")

    if "alembic_version" in table_names:
        version_shape_blockers = baseline.module.alembic_version_blockers(
            _raw_sqlite_connection(connection)
        )
        if version_shape_blockers:
            raise RuntimeError("迁移前版本表校验失败：" + "；".join(version_shape_blockers))

    version_rows: list[str] = []
    if "alembic_version" in table_names:
        version_rows = [
            str(row[0])
            for row in connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).fetchall()
        ]
        if len(version_rows) > 1:
            raise RuntimeError("alembic_version 存在多行，数据库版本状态无效，拒绝迁移")

    if bool(business_tables) and not version_rows:
        blockers = baseline.module.legacy_schema_blockers(
            _raw_sqlite_connection(connection),
            require_pipeline_fk=None,
        )
        if blockers:
            raise RuntimeError("迁移前结构校验失败：" + "；".join(blockers))
    elif version_rows:
        blockers = schema_blockers_for_revision(
            script,
            _raw_sqlite_connection(connection),
            version_rows[0],
        )
        if blockers:
            raise RuntimeError(
                "pre-migration schema validation failed: " + "; ".join(blockers)
            )


def run_migrations_offline() -> None:
    raise RuntimeError("离线 SQL 迁移被禁用；请对显式临时数据库执行在线验证")


def _postvalidate_migration_state(
    connection: Connection,
    *,
    require_head: bool,
) -> None:
    script = ScriptDirectory.from_config(config)
    head_revision = script.get_current_head()
    if head_revision is None:
        raise RuntimeError("fresh bootstrap migration head is missing")
    baseline = script.get_revision(BASELINE_REVISION)
    if baseline is None:
        raise RuntimeError(f"缺少冻结基线 revision：{BASELINE_REVISION}")
    raw_connection = _raw_sqlite_connection(connection)
    table_names = set(inspect(connection).get_table_names())
    business_tables = table_names - {"alembic_version"}
    revision_rows: list[str] = []
    if "alembic_version" in table_names:
        version_blockers = baseline.module.alembic_version_blockers(raw_connection)
        if version_blockers:
            raise RuntimeError(
                "post-migration version table validation failed: "
                + "; ".join(version_blockers)
            )
        revision_rows = [
            str(row[0])
            for row in connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).fetchall()
        ]
    if len(revision_rows) > 1:
        raise RuntimeError("post-migration alembic_version has multiple rows")
    if require_head and revision_rows != [str(head_revision)]:
        raise RuntimeError(
            "fresh bootstrap 未到唯一 head revision："
            f"{revision_rows}"
        )
    if revision_rows:
        blockers = schema_blockers_for_revision(
            script,
            raw_connection,
            revision_rows[0],
        )
    elif business_tables:
        blockers = baseline.module.legacy_schema_blockers(
            raw_connection,
            require_pipeline_fk=False,
        )
    else:
        blockers = []
    if blockers:
        raise RuntimeError(
            "post-migration schema validation failed: " + "; ".join(blockers)
        )


def _run_with_connection(connection: Connection, *, owns_connection: bool) -> None:
    manual_transaction = owns_connection
    fresh_only = bool(config.attributes.get("fresh_only"))
    if manual_transaction:
        # Every owned SQLite write uses one explicit transaction. Its write
        # reservation precedes validation and remains through revision-level
        # postvalidation, preventing both TOCTOU and partial DDL artifacts.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        _preflight_before_alembic_writes(connection)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        _postvalidate_migration_state(connection, require_head=fresh_only)
        if manual_transaction:
            connection.exec_driver_sql("COMMIT")
    except Exception:
        if manual_transaction:
            try:
                connection.exec_driver_sql("ROLLBACK")
            except Exception:
                pass
        raise


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        if config.attributes.get("fresh_only"):
            raise RuntimeError(
                "fresh_only 不接受外部 supplied connection；"
                "必须由 Alembic 环境拥有 BEGIN IMMEDIATE 锁和完整事务"
            )
        _run_with_connection(supplied_connection, owns_connection=False)
        return

    engine_options = {
        "poolclass": NullPool,
        "connect_args": {"check_same_thread": False},
        "isolation_level": "AUTOCOMMIT",
    }
    connectable = create_engine(_explicit_database_url(), **engine_options)
    try:
        with connectable.connect() as connection:
            _run_with_connection(connection, owns_connection=True)
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
