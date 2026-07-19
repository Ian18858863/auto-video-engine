from collections.abc import Generator
from pathlib import Path, PureWindowsPath
import sqlite3
from typing import Any
from urllib.parse import unquote

from sqlalchemy import create_engine, event, inspect, make_url, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings
from app.core.runtime_identity import resolve_runtime_identity
from app.pipeline.project_identity import project_slug_v1


class Base(DeclarativeBase):
    pass


def _register_sqlite_project_identity_functions(
    dbapi_connection: Any,
    _connection_record: Any,
) -> None:
    if isinstance(dbapi_connection, sqlite3.Connection):
        dbapi_connection.create_function(
            "project_slug_v1",
            1,
            project_slug_v1,
            deterministic=True,
        )


# Tests and maintenance code sometimes construct SQLAlchemy engines directly.
# The database trigger must have the same deterministic function everywhere,
# while raw sqlite3 connections remain fail-closed unless they opt in.
event.listen(Engine, "connect", _register_sqlite_project_identity_functions)


def resolve_sqlite_database_url(raw_url: str, repo_root: Path) -> str:
    url = make_url(raw_url)
    if url.get_backend_name() != "sqlite":
        return raw_url

    database = url.database
    if not database or database == ":memory:":
        return raw_url
    if database.startswith("file:"):
        file_target = database.removeprefix("file:")
        mode = str(url.query.get("mode", "")).casefold()
        if mode == "memory" or file_target == ":memory:":
            return raw_url
        if Path(file_target).is_absolute() or PureWindowsPath(
            file_target
        ).is_absolute():
            return raw_url
        raise ValueError(
            "相对 file: SQLite URI 会依赖当前工作目录，已拒绝；"
            "请改用 sqlite:///./文件名 让应用锚定物理仓库根，"
            "或提供绝对 file: URI"
        )

    database_path = Path(database)
    if database_path.is_absolute() or PureWindowsPath(database).is_absolute():
        return raw_url

    absolute_database_path = (Path(repo_root) / database_path).resolve()
    return url.set(database=absolute_database_path.as_posix()).render_as_string(
        hide_password=False
    )


def _enable_sqlite_foreign_keys(
    dbapi_connection: Any,
    _connection_record: Any,
) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def build_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    engine_kwargs: dict[str, Any] = {}
    is_sqlite = url.get_backend_name() == "sqlite"
    if is_sqlite:
        engine_kwargs["connect_args"] = {"check_same_thread": False}

    target_engine = create_engine(database_url, **engine_kwargs)
    if is_sqlite:
        event.listen(target_engine, "connect", _enable_sqlite_foreign_keys)
    return target_engine


def get_sqlite_database_path(target_engine: Engine) -> Path:
    url = target_engine.url
    backend_name = url.get_backend_name()
    if backend_name != "sqlite":
        raise ValueError(
            "生产运行数据库只支持磁盘 SQLite 文件；"
            f"当前数据库类型为 {backend_name}"
        )

    database = url.database
    mode = str(url.query.get("mode", "")).casefold()
    if not database or database == ":memory:" or mode == "memory":
        raise ValueError("生产运行数据库必须是磁盘 SQLite 文件，不能使用内存数据库")

    if database.startswith("file:"):
        file_target = unquote(database.removeprefix("file:"))
        if file_target == ":memory:":
            raise ValueError(
                "生产运行数据库必须是磁盘 SQLite 文件，不能使用内存数据库"
            )
        drive_qualified_target = file_target.lstrip("/")
        if (
            len(drive_qualified_target) >= 3
            and drive_qualified_target[1:3] == ":/"
        ):
            file_target = drive_qualified_target
        database_path = Path(file_target)
    else:
        database_path = Path(database)

    if not (
        database_path.is_absolute()
        or PureWindowsPath(str(database_path)).is_absolute()
    ):
        raise ValueError(
            "生产运行数据库路径必须是已解析的绝对路径；"
            f"当前路径为 {database_path}"
        )
    return database_path.resolve(strict=False)


_runtime_identity = resolve_runtime_identity(Path(__file__))
_resolved_database_url = resolve_sqlite_database_url(
    get_settings().database_url,
    _runtime_identity.physical_repo_root,
)
engine = build_engine(_resolved_database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_db_and_tables(target_engine: Engine) -> None:
    """Legacy isolated-test helper; production startup uses Alembic only."""
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=target_engine)
    _upgrade_existing_tables(target_engine)


def _upgrade_existing_tables(target_engine: Engine) -> None:
    """Legacy isolated-test helper; never reachable from production startup."""
    inspector = inspect(target_engine)
    table_names = set(inspector.get_table_names())
    statements: list[str] = []

    if "video_posts" in table_names:
        columns = _columns(inspector, "video_posts")
        _add_column_if_missing(statements, columns, "video_posts", "pipeline_id", "INTEGER")
        _add_column_if_missing(statements, columns, "video_posts", "updated_at", "DATETIME")

    if "hot_items" in table_names:
        columns = _columns(inspector, "hot_items")
        _add_column_if_missing(
            statements,
            columns,
            "hot_items",
            "data_origin",
            "VARCHAR(30) DEFAULT 'real'",
        )
        _add_column_if_missing(
            statements,
            columns,
            "hot_items",
            "origin_note",
            "VARCHAR(200) DEFAULT ''",
        )
        _add_column_if_missing(
            statements,
            columns,
            "hot_items",
            "risk_score",
            "FLOAT DEFAULT 0",
        )
        _add_column_if_missing(
            statements,
            columns,
            "hot_items",
            "freshness_score",
            "FLOAT DEFAULT 0",
        )
        _add_column_if_missing(
            statements,
            columns,
            "hot_items",
            "duplicate_reason",
            "VARCHAR(300) DEFAULT ''",
        )
        _add_column_if_missing(
            statements,
            columns,
            "hot_items",
            "duplicate_group_key",
            "VARCHAR(160) DEFAULT ''",
        )
        _add_column_if_missing(
            statements,
            columns,
            "hot_items",
            "lifecycle_status",
            "VARCHAR(40) DEFAULT 'active'",
        )
        _add_column_if_missing(
            statements,
            columns,
            "hot_items",
            "archived_at",
            "DATETIME",
        )
        _add_column_if_missing(
            statements,
            columns,
            "hot_items",
            "archive_reason",
            "VARCHAR(200) DEFAULT ''",
        )

    if "topic_ideas" in table_names:
        columns = _columns(inspector, "topic_ideas")
        _add_column_if_missing(
            statements,
            columns,
            "topic_ideas",
            "video_type",
            "VARCHAR(80) DEFAULT 'single_deep_dive'",
        )
        _add_column_if_missing(
            statements,
            columns,
            "topic_ideas",
            "generation_source",
            "VARCHAR(80) DEFAULT 'fake_provider'",
        )
        _add_column_if_missing(
            statements,
            columns,
            "topic_ideas",
            "recommendation_score",
            "FLOAT DEFAULT 0",
        )
        _add_column_if_missing(
            statements,
            columns,
            "topic_ideas",
            "recommendation_bucket",
            "VARCHAR(40) DEFAULT 'backup'",
        )
        _add_column_if_missing(
            statements,
            columns,
            "topic_ideas",
            "source_hotspot_ids",
            "JSON DEFAULT '[]'",
        )
        _add_column_if_missing(
            statements,
            columns,
            "topic_ideas",
            "source_hotspot_titles",
            "JSON DEFAULT '[]'",
        )
        _add_column_if_missing(
            statements,
            columns,
            "topic_ideas",
            "is_example",
            "BOOLEAN DEFAULT 0",
        )

    if "source_runs" in table_names:
        columns = _columns(inspector, "source_runs")
        _add_column_if_missing(
            statements,
            columns,
            "source_runs",
            "status",
            "VARCHAR(20) DEFAULT '失败'",
        )
        _add_column_if_missing(
            statements,
            columns,
            "source_runs",
            "is_rate_limited",
            "BOOLEAN DEFAULT 0",
        )

    if not statements:
        return

    with target_engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_video_posts_pipeline_id "
                "ON video_posts (pipeline_id)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_hot_items_data_origin "
                "ON hot_items (data_origin)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_hot_items_duplicate_group_key "
                "ON hot_items (duplicate_group_key)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_hot_items_lifecycle_status "
                "ON hot_items (lifecycle_status)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_topic_ideas_recommendation_bucket "
                "ON topic_ideas (recommendation_bucket)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_topic_ideas_recommendation_score "
                "ON topic_ideas (recommendation_score)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_source_runs_is_rate_limited "
                "ON source_runs (is_rate_limited)"
            )
        )


def _columns(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _add_column_if_missing(
    statements: list[str],
    columns: set[str],
    table_name: str,
    column_name: str,
    column_sql: str,
) -> None:
    if column_name not in columns:
        statements.append(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
