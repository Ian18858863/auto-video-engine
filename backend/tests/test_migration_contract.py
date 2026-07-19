from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
ALEMBIC_ENV = BACKEND_ROOT / "alembic" / "env.py"
BASELINE_REVISION = (
    BACKEND_ROOT
    / "alembic"
    / "versions"
    / "20260711_0001_legacy_baseline.py"
)
FROZEN_LEGACY_SQL = Path(__file__).parent / "fixtures" / "legacy_schema.sql"
FROZEN_LEGACY_ORACLE = (
    Path(__file__).parent / "fixtures" / "legacy_schema_oracle.json"
)
HEAD_REVISION = "20260711_0003"
BASELINE_REVISION_ID = "20260711_0001"
IDENTITY_REVISION_ID = "20260711_0002"
PIPELINE_FK_NAME = "fk_video_posts_pipeline_id_production_pipelines"
CANONICAL_VERSION_TABLE_SQL = (
    "CREATE TABLE alembic_version ("
    "version_num VARCHAR(32) NOT NULL, "
    "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
)
LEGACY_TABLES = {
    "ai_generations",
    "app_config",
    "content_quality_checks",
    "copywriting_samples",
    "hot_items",
    "pipeline_steps",
    "production_pipelines",
    "script_assets",
    "source_runs",
    "topic_ideas",
    "video_post_snapshots",
    "video_posts",
}
VERIFIED_BACKUP_SHA256 = (
    "a349dd2ebd58fc5f65eea97e38aafa05c26f209b86a8cf64ee98db624de0bef1"
)
VERIFIED_LEGACY_COLUMN_ORDER = {
    "hot_items": (
        "id",
        "source_platform",
        "source_type",
        "title",
        "url",
        "published_at",
        "fetched_at",
        "summary",
        "metrics",
        "tags",
        "category",
        "is_filtered",
        "filter_reason",
        "hot_score",
        "topic_score",
        "save_score",
        "is_favorite",
        "has_topic_ideas",
        "has_script",
        "data_origin",
        "origin_note",
        "risk_score",
        "freshness_score",
        "duplicate_reason",
        "duplicate_group_key",
        "lifecycle_status",
        "archived_at",
        "archive_reason",
    ),
    "topic_ideas": (
        "id",
        "hot_item_id",
        "title",
        "core_view",
        "recommended_format",
        "why_watch",
        "why_save",
        "comment_trigger",
        "risk_note",
        "recommended_duration",
        "hook",
        "is_selected",
        "video_type",
        "generation_source",
        "recommendation_score",
        "recommendation_bucket",
        "source_hotspot_ids",
        "source_hotspot_titles",
        "is_example",
    ),
    "source_runs": (
        "id",
        "source_name",
        "started_at",
        "ended_at",
        "success",
        "items_fetched",
        "error_message",
        "status",
        "is_rate_limited",
    ),
    "video_posts": (
        "id",
        "platform",
        "video_title",
        "published_at",
        "video_url",
        "topic_type",
        "video_format",
        "hook_type",
        "duration_seconds",
        "views",
        "likes",
        "comments",
        "favorites",
        "shares",
        "followers_gained",
        "completion_rate",
        "average_watch_seconds",
        "profile_visits",
        "current_view_velocity",
        "notes",
        "pipeline_id",
        "updated_at",
    ),
}


def _sqlite_url(database_path: Path) -> str:
    absolute_path = database_path.resolve(strict=False)
    assert absolute_path.is_absolute()
    return f"sqlite:///{absolute_path.as_posix()}"


def _alembic_config(database_path: Path):
    config_module = importlib.import_module("alembic.config")
    config = config_module.Config(str(ALEMBIC_INI))
    database_url = _sqlite_url(database_path)
    config.attributes["database_url"] = database_url
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _upgrade_to_revision(database_path: Path, revision: str) -> None:
    command = importlib.import_module("alembic.command")
    command.upgrade(_alembic_config(database_path), revision)


def _upgrade_to_baseline(database_path: Path) -> None:
    _upgrade_to_revision(database_path, BASELINE_REVISION_ID)


def _upgrade_to_head(database_path: Path) -> None:
    _upgrade_to_revision(database_path, "head")


def _downgrade_to_base(database_path: Path) -> None:
    command = importlib.import_module("alembic.command")
    command.downgrade(_alembic_config(database_path), "base")


def _create_frozen_legacy_database(database_path: Path) -> None:
    sql = FROZEN_LEGACY_SQL.read_text(encoding="utf-8")
    with sqlite3.connect(database_path) as connection:
        connection.executescript(sql)


def _create_0002_database_with_pipeline_statuses(
    database_path: Path,
) -> dict[int, str]:
    _create_frozen_legacy_database(database_path)
    _upgrade_to_baseline(database_path)
    statuses = {
        401: "已完成",
        402: "失败",
        403: "进行中",
        404: "未开始",
        405: "需要人工确认",
    }
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE production_pipelines SET status=? WHERE id=401",
            (statuses[401],),
        )
        connection.executemany(
            "INSERT INTO production_pipelines "
            "(id, hotspot_id, topic_idea_id, script_asset_id, project_name, "
            "project_dir, status, provider, model, created_at, updated_at) "
            "SELECT ?, hotspot_id, topic_idea_id, script_asset_id, ?, "
            "project_dir, ?, provider, model, created_at, updated_at "
            "FROM production_pipelines WHERE id=401",
            [
                (pipeline_id, f"Task 2 status {pipeline_id}", status)
                for pipeline_id, status in statuses.items()
                if pipeline_id != 401
            ],
        )
    _upgrade_to_revision(database_path, IDENTITY_REVISION_ID)
    return statuses


def _completion_evidence_column(database_path: Path) -> dict[str, object] | None:
    snapshot = _independent_rich_schema_snapshot(database_path)
    pipelines = snapshot.get("production_pipelines")
    if not isinstance(pipelines, dict):
        return None
    return next(
        (
            column
            for column in pipelines["columns"]
            if column["name"] == "completion_evidence"
        ),
        None,
    )


def _rewrite_production_pipeline_table_sql(
    database_path: Path,
    original: str,
    replacement: str,
) -> None:
    with sqlite3.connect(database_path) as connection:
        table_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_schema "
                "WHERE type='table' AND name='production_pipelines'"
            ).fetchone()[0]
        )
        assert original in table_sql
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_schema SET sql=? WHERE type='table' AND name=?",
            (
                table_sql.replace(original, replacement, 1),
                "production_pipelines",
            ),
        )
        schema_version = int(
            connection.execute("PRAGMA schema_version").fetchone()[0]
        )
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.execute("PRAGMA writable_schema=OFF")


def _rebuild_0003_with_evidence_before_legacy_directory(
    database_path: Path,
) -> None:
    script_module = importlib.import_module("alembic.script")
    script = script_module.ScriptDirectory.from_config(_alembic_config(database_path))
    identity = script.get_revision(IDENTITY_REVISION_ID).module
    temporary_table = "_task6_reordered_production_pipelines"
    table_sql = identity._pipelines_table_sql(
        temporary_table,
        with_identity=True,
    )
    marker = "    legacy_project_dir VARCHAR(800),"
    assert marker in table_sql
    reordered_sql = table_sql.replace(
        marker,
        "    completion_evidence JSON DEFAULT '{}' NOT NULL,\n"
        "    legacy_project_dir VARCHAR(800),",
        1,
    )
    columns = (
        *identity._LEGACY_PIPELINE_COLUMNS,
        "project_key",
        "completion_evidence",
        "legacy_project_dir",
    )
    column_list = ", ".join(columns)
    with sqlite3.connect(database_path) as connection:
        for trigger_name in identity._TRIGGER_SQL:
            connection.execute(f"DROP TRIGGER {trigger_name}")
        connection.execute(reordered_sql)
        connection.execute(
            f"INSERT INTO {temporary_table} ({column_list}) "
            f"SELECT {column_list} FROM production_pipelines ORDER BY id"
        )
        connection.execute("DROP TABLE production_pipelines")
        connection.execute(
            f"ALTER TABLE {temporary_table} RENAME TO production_pipelines"
        )
        for statement in identity._LEGACY_PIPELINE_INDEX_SQL:
            connection.execute(statement)
        connection.execute(
            f"CREATE INDEX {identity.PROJECT_KEY_INDEX} "
            "ON production_pipelines (project_key)"
        )
        for statement in identity._TRIGGER_SQL.values():
            connection.execute(statement)


def _create_legacy_database_with_pipeline_fk(
    database_path: Path,
    pipeline_fk_sql: str,
) -> None:
    sql = FROZEN_LEGACY_SQL.read_text(encoding="utf-8")
    marker = (
        "    pipeline_id INTEGER,\n"
        "    updated_at DATETIME,\n"
        "    PRIMARY KEY (id)\n"
        ");\n"
        "CREATE INDEX ix_video_posts_pipeline_id"
    )
    replacement = (
        "    pipeline_id INTEGER,\n"
        "    updated_at DATETIME,\n"
        "    PRIMARY KEY (id),\n"
        f"    {pipeline_fk_sql}\n"
        ");\n"
        "CREATE INDEX ix_video_posts_pipeline_id"
    )
    assert marker in sql
    with sqlite3.connect(database_path) as connection:
        connection.executescript(sql.replace(marker, replacement, 1))


def _replace_version_table(
    database_path: Path,
    create_sql: str,
    *,
    revision: str | None,
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE IF EXISTS alembic_version")
        connection.execute(create_sql)
        if revision is not None:
            column_name = str(
                connection.execute("PRAGMA table_info(alembic_version)").fetchone()[1]
            )
            connection.execute(
                f'INSERT INTO alembic_version ("{column_name}") VALUES (?)',
                (revision,),
            )


def _normalize_sql_fragment(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.casefold().strip().split())


def _independent_rich_schema_snapshot(database_path: Path) -> dict[str, object]:
    """Independent archive oracle; deliberately imports no production helper."""
    snapshot: dict[str, object] = {}
    with sqlite3.connect(database_path) as connection:
        table_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "AND name != 'alembic_version' ORDER BY name"
            ).fetchall()
        ]
        for table_name in table_names:
            quoted_table = table_name.replace('"', '""')
            table_sql_row = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            table_sql = (
                None
                if table_sql_row is None or table_sql_row[0] is None
                else str(table_sql_row[0])
            )
            columns = [
                {
                    "position": int(row[0]),
                    "name": str(row[1]),
                    "type": str(row[2]).upper(),
                    "not_null": bool(row[3]),
                    "default": None if row[4] is None else str(row[4]),
                    "primary_key_position": int(row[5]),
                    "hidden": int(row[6]),
                }
                for row in connection.execute(
                    f'PRAGMA table_xinfo("{quoted_table}")'
                ).fetchall()
            ]
            indexes: list[dict[str, object]] = []
            for index_row in connection.execute(
                f'PRAGMA index_list("{quoted_table}")'
            ).fetchall():
                index_name = str(index_row[1])
                quoted_index = index_name.replace('"', '""')
                sql_row = connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE type='index' AND name=?",
                    (index_name,),
                ).fetchone()
                index_sql = None if sql_row is None else str(sql_row[0])
                where_match = re.search(
                    r"\bwhere\b(?P<predicate>.+)$",
                    index_sql or "",
                    flags=re.IGNORECASE | re.DOTALL,
                )
                indexes.append(
                    {
                        "name": (
                            None
                            if index_name.startswith("sqlite_autoindex_")
                            else index_name
                        ),
                        "unique": bool(index_row[2]),
                        "origin": str(index_row[3]),
                        "partial": bool(index_row[4]),
                        "key_terms": [
                            {
                                "position": int(term[0]),
                                "column_id": int(term[1]),
                                "column_name": (
                                    None if term[2] is None else str(term[2])
                                ),
                                "descending": bool(term[3]),
                                "collation": (
                                    None if term[4] is None else str(term[4]).casefold()
                                ),
                            }
                            for term in connection.execute(
                                f'PRAGMA index_xinfo("{quoted_index}")'
                            ).fetchall()
                            if bool(term[5])
                        ],
                        "sql": _normalize_sql_fragment(index_sql),
                        "predicate": _normalize_sql_fragment(
                            None if where_match is None else where_match.group("predicate")
                        ),
                    }
                )
            foreign_keys = [
                {
                    "id": int(row[0]),
                    "sequence": int(row[1]),
                    "target_table": str(row[2]),
                    "local_column": str(row[3]),
                    "target_column": str(row[4]),
                    "on_update": str(row[5]).upper(),
                    "on_delete": str(row[6]).upper(),
                    "match": str(row[7]).upper(),
                }
                for row in connection.execute(
                    f'PRAGMA foreign_key_list("{quoted_table}")'
                ).fetchall()
            ]
            snapshot[table_name] = {
                "columns": columns,
                "indexes": sorted(indexes, key=lambda item: json.dumps(item, sort_keys=True)),
                "foreign_keys": foreign_keys,
                "normalized_table_sql": _normalize_sql_fragment(table_sql),
            }
    return snapshot


def _rich_snapshot_digest(database_path: Path) -> str:
    payload = json.dumps(
        _independent_rich_schema_snapshot(database_path),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _business_tables(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "AND name != 'alembic_version'"
            )
        }


def _has_version_table(database_path: Path) -> bool:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM sqlite_schema "
            "WHERE type = 'table' AND name = 'alembic_version'"
        ).fetchone()
    return row is not None


def _schema_snapshot(database_path: Path) -> dict[str, dict[str, object]]:
    snapshot: dict[str, dict[str, object]] = {}
    with sqlite3.connect(database_path) as connection:
        for table_name in sorted(_business_tables(database_path)):
            escaped_table = table_name.replace('"', '""')
            columns = tuple(
                (
                    str(row[1]),
                    str(row[2]),
                    int(row[3]),
                    None if row[4] is None else str(row[4]),
                    int(row[5]),
                )
                for row in connection.execute(
                    f'PRAGMA table_info("{escaped_table}")'
                ).fetchall()
            )
            indexes = []
            for row in connection.execute(
                f'PRAGMA index_list("{escaped_table}")'
            ).fetchall():
                index_name = str(row[1])
                if index_name.startswith("sqlite_autoindex_"):
                    continue
                escaped_index = index_name.replace('"', '""')
                index_columns = tuple(
                    str(index_row[2])
                    for index_row in connection.execute(
                        f'PRAGMA index_info("{escaped_index}")'
                    ).fetchall()
                )
                indexes.append((index_name, int(row[2]), index_columns))

            foreign_key_rows = connection.execute(
                f'PRAGMA foreign_key_list("{escaped_table}")'
            ).fetchall()
            grouped_foreign_keys: dict[int, list[tuple[object, ...]]] = {}
            for row in foreign_key_rows:
                grouped_foreign_keys.setdefault(int(row[0]), []).append(row)
            foreign_keys = []
            for rows in grouped_foreign_keys.values():
                ordered = sorted(rows, key=lambda item: int(item[1]))
                foreign_keys.append(
                    (
                        tuple(str(item[3]) for item in ordered),
                        str(ordered[0][2]),
                        tuple(str(item[4]) for item in ordered),
                        str(ordered[0][5]),
                        str(ordered[0][6]),
                    )
                )

            table_sql_row = connection.execute(
                "SELECT sql FROM sqlite_schema "
                "WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            table_sql = "" if table_sql_row is None else str(table_sql_row[0])
            snapshot[table_name] = {
                "columns": columns,
                "indexes": tuple(sorted(indexes)),
                "foreign_keys": tuple(sorted(foreign_keys)),
                "has_named_pipeline_fk": (
                    PIPELINE_FK_NAME.casefold() in table_sql.casefold()
                ),
            }
    return snapshot


def _row_snapshot(database_path: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    rows: dict[str, tuple[tuple[object, ...], ...]] = {}
    with sqlite3.connect(database_path) as connection:
        for table_name in sorted(_business_tables(database_path)):
            escaped_table = table_name.replace('"', '""')
            rows[table_name] = tuple(
                tuple(row)
                for row in connection.execute(
                    f'SELECT * FROM "{escaped_table}" ORDER BY id'
                ).fetchall()
            )
    return rows


def _without_pipeline_fk(
    snapshot: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    normalized = copy.deepcopy(snapshot)
    video_posts = normalized["video_posts"]
    video_posts["foreign_keys"] = tuple(
        foreign_key
        for foreign_key in video_posts["foreign_keys"]
        if not (
            foreign_key[0] == ("pipeline_id",)
            and foreign_key[1] == "production_pipelines"
            and foreign_key[2] == ("id",)
        )
    )
    video_posts["has_named_pipeline_fk"] = False
    return normalized


def _current_revision(database_path: Path) -> str | None:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
    return None if row is None else str(row[0])


def _column_order(database_path: Path, table_name: str) -> tuple[str, ...]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return tuple(str(row[1]) for row in rows)


def _file_metadata(path: Path) -> tuple[bool, int | None, int | None]:
    if not path.exists():
        return False, None, None
    stat = path.stat()
    return True, stat.st_size, stat.st_mtime_ns


def test_alembic_environment_and_frozen_baseline_are_versioned() -> None:
    assert ALEMBIC_INI.is_file()
    assert ALEMBIC_ENV.is_file()
    assert BASELINE_REVISION.is_file()
    assert FROZEN_LEGACY_SQL.is_file()


def test_0003_is_head_and_orm_completion_evidence_contract_is_exact() -> None:
    sqlalchemy = importlib.import_module("sqlalchemy")
    models = importlib.import_module("app.models")
    script_module = importlib.import_module("alembic.script")
    script = script_module.ScriptDirectory.from_config(
        _alembic_config(Path("D:/task6-not-opened.db"))
    )

    assert script.get_current_head() == HEAD_REVISION
    revision = script.get_revision(HEAD_REVISION)
    assert revision is not None
    assert revision.down_revision == IDENTITY_REVISION_ID
    column = models.ProductionPipeline.__table__.c.completion_evidence
    assert isinstance(column.type, sqlalchemy.JSON)
    assert column.nullable is False
    assert column.default is not None
    assert callable(column.default.arg)
    assert column.default.arg(None) == {}
    assert column.server_default is not None
    assert str(column.server_default.arg) == "'{}'"


def test_0003_upgrade_reclassifies_only_completed_and_preserves_identity_guards(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "completion-upgrade.db").resolve()
    original_statuses = _create_0002_database_with_pipeline_statuses(database_path)
    script_module = importlib.import_module("alembic.script")
    script = script_module.ScriptDirectory.from_config(_alembic_config(database_path))
    identity_migration = script.get_revision(IDENTITY_REVISION_ID).module
    before_triggers: dict[str, str]
    with sqlite3.connect(database_path) as connection:
        before_triggers = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_schema WHERE type='trigger' ORDER BY name"
            ).fetchall()
        }

    _upgrade_to_revision(database_path, HEAD_REVISION)

    column = _completion_evidence_column(database_path)
    assert column is not None
    assert column["type"] == "JSON"
    assert column["not_null"] is True
    assert column["default"] == "'{}'"
    with sqlite3.connect(database_path) as connection:
        project_identity = importlib.import_module("app.pipeline.project_identity")
        connection.create_function(
            "project_slug_v1",
            1,
            project_identity.project_slug_v1,
        )
        rows = connection.execute(
            "SELECT id, status, completion_evidence "
            "FROM production_pipelines ORDER BY id"
        ).fetchall()
        after_triggers = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_schema WHERE type='trigger' ORDER BY name"
            ).fetchall()
        }
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE production_pipelines SET project_key='p-401-tampered' "
                "WHERE id=401"
            )
    assert {row[0]: row[1] for row in rows} == {
        pipeline_id: (
            "需要人工确认" if status == "已完成" else status
        )
        for pipeline_id, status in original_statuses.items()
    }
    assert all(json.loads(str(row[2])) == {} for row in rows)
    assert set(after_triggers) == set(identity_migration._TRIGGER_SQL)
    assert after_triggers == before_triggers


def test_0003_validator_rejects_completion_evidence_inserted_before_old_column(
    tmp_path: Path,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / "completion-column-position.db").resolve()
    _create_0002_database_with_pipeline_statuses(database_path)
    _upgrade_to_revision(database_path, HEAD_REVISION)
    _rebuild_0003_with_evidence_before_legacy_directory(database_path)
    with sqlite3.connect(database_path) as connection:
        column_names = tuple(
            str(row[1])
            for row in connection.execute(
                'PRAGMA table_xinfo("production_pipelines")'
            ).fetchall()
        )
    assert column_names[-3:] == (
        "project_key",
        "completion_evidence",
        "legacy_project_dir",
    )

    status = schema_migrations.inspect_schema_status(database_path)

    assert status.current_revision == HEAD_REVISION
    assert status.state == "invalid"
    assert any(
        "completion_evidence" in blocker and ("position" in blocker or "order" in blocker)
        for blocker in status.blockers
    )


def test_0003_downgrade_reclassifies_post_approval_before_drop_and_repeats(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "completion-round-trip.db").resolve()
    _create_0002_database_with_pipeline_statuses(database_path)

    for cycle in range(2):
        _upgrade_to_revision(database_path, HEAD_REVISION)
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "UPDATE production_pipelines "
                "SET status='已完成', completion_evidence=? WHERE id=402",
                (json.dumps({"cycle": cycle}, ensure_ascii=False),),
            )
        command = importlib.import_module("alembic.command")
        command.downgrade(_alembic_config(database_path), IDENTITY_REVISION_ID)
        with sqlite3.connect(database_path) as connection:
            columns = {
                str(row[1])
                for row in connection.execute(
                    'PRAGMA table_xinfo("production_pipelines")'
                ).fetchall()
            }
            assert "completion_evidence" not in columns
            assert connection.execute(
                "SELECT status FROM production_pipelines WHERE id=402"
            ).fetchone() == ("需要人工确认",)
            assert connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone() == (IDENTITY_REVISION_ID,)


@pytest.mark.parametrize(
    "tamper",
    [
        "wrong_type",
        "wrong_default",
        "nullable",
        "invalid_json",
        "non_object_json",
        "extra_column",
        "extra_index",
        "extra_view",
        "extra_trigger",
        "mutated_old_column",
    ],
)
def test_0003_validator_rejects_schema_and_evidence_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / f"completion-tamper-{tamper}.db").resolve()
    _create_0002_database_with_pipeline_statuses(database_path)
    _upgrade_to_revision(database_path, HEAD_REVISION)
    evidence_fragment = "completion_evidence JSON DEFAULT '{}' NOT NULL"
    if tamper == "wrong_type":
        _rewrite_production_pipeline_table_sql(
            database_path,
            evidence_fragment,
            "completion_evidence TEXT DEFAULT '{}' NOT NULL",
        )
    elif tamper == "wrong_default":
        _rewrite_production_pipeline_table_sql(
            database_path,
            evidence_fragment,
            "completion_evidence JSON DEFAULT '[]' NOT NULL",
        )
    elif tamper == "nullable":
        _rewrite_production_pipeline_table_sql(
            database_path,
            evidence_fragment,
            "completion_evidence JSON DEFAULT '{}'",
        )
    elif tamper == "mutated_old_column":
        _rewrite_production_pipeline_table_sql(
            database_path,
            "status VARCHAR(40) NOT NULL",
            "status VARCHAR(41) NOT NULL",
        )
    else:
        with sqlite3.connect(database_path) as connection:
            if tamper in {"invalid_json", "non_object_json"}:
                connection.execute(
                    "UPDATE production_pipelines "
                    "SET completion_evidence=? WHERE id=401",
                    ("not-json" if tamper == "invalid_json" else "[]",),
                )
            elif tamper == "extra_column":
                connection.execute(
                    "ALTER TABLE production_pipelines "
                    "ADD COLUMN unexpected_task6 TEXT"
                )
            elif tamper == "extra_index":
                connection.execute(
                    "CREATE INDEX unexpected_task6_index "
                    "ON production_pipelines(status)"
                )
            elif tamper == "extra_view":
                connection.execute(
                    "CREATE VIEW unexpected_task6_view AS "
                    "SELECT id FROM production_pipelines"
                )
            else:
                connection.execute(
                    "CREATE TRIGGER unexpected_task6_trigger "
                    "AFTER UPDATE OF status ON production_pipelines "
                    "BEGIN SELECT 1; END"
                )

    status = schema_migrations.inspect_schema_status(database_path)

    assert status.current_revision == HEAD_REVISION
    assert status.state == "invalid"
    assert status.blockers


def test_frozen_fixture_keeps_verified_alter_appended_column_order(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "verified-order.db").resolve()
    _create_frozen_legacy_database(database_path)

    assert VERIFIED_BACKUP_SHA256 in FROZEN_LEGACY_SQL.read_text(
        encoding="utf-8"
    ).casefold()
    assert {
        table_name: _column_order(database_path, table_name)
        for table_name in VERIFIED_LEGACY_COLUMN_ORDER
    } == VERIFIED_LEGACY_COLUMN_ORDER


def test_frozen_fixture_matches_independent_full_archive_semantic_oracle(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "frozen-oracle.db").resolve()
    head_path = (tmp_path / "head-oracle.db").resolve()
    _create_frozen_legacy_database(database_path)
    _upgrade_to_baseline(head_path)

    oracle = json.loads(FROZEN_LEGACY_ORACLE.read_text(encoding="utf-8"))

    assert oracle == {
        "archive_sha256": VERIFIED_BACKUP_SHA256,
        "format": "sqlite-rich-schema-v1",
        "head_schema_sha256": _rich_snapshot_digest(head_path),
        "schema_sha256": _rich_snapshot_digest(database_path),
        "table_count": len(LEGACY_TABLES),
    }


@pytest.mark.parametrize(
    "pipeline_fk_sql",
    [
        (
            "CONSTRAINT fk_video_posts_pipeline_id_production_pipelines_suffix "
            "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id)"
        ),
        "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id)",
        (
            "CONSTRAINT fk_video_posts_pipeline_id_production_pipelines "
            "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id) "
            "ON UPDATE CASCADE ON DELETE CASCADE"
        ),
        (
            "CONSTRAINT fk_video_posts_pipeline_id_production_pipelines "
            "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id) "
            "ON DELETE SET NULL"
        ),
        (
            "CONSTRAINT fk_video_posts_pipeline_id_production_pipelines "
            "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id) "
            "MATCH FULL"
        ),
        (
            "CONSTRAINT fk_video_posts_pipeline_id_production_pipelines "
            "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id) "
            "DEFERRABLE INITIALLY DEFERRED"
        ),
        (
            "CONSTRAINT fk_video_posts_pipeline_id_production_pipelines "
            "FOREIGN KEY(pipeline_id) REFERENCES hot_items(id)"
        ),
        (
            "CONSTRAINT fk_video_posts_pipeline_id_production_pipelines "
            "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id), "
            "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id)"
        ),
        (
            "-- CONSTRAINT fk_video_posts_pipeline_id_production_pipelines "
            "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id)\n"
            "    FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id)"
        ),
        (
            "/* CONSTRAINT fk_video_posts_pipeline_id_production_pipelines "
            "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id) */ "
            "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id)"
        ),
        (
            "CHECK('CONSTRAINT fk_video_posts_pipeline_id_production_pipelines "
            "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id)' <> ''), "
            "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id)"
        ),
    ],
)
def test_unversioned_upgrade_rejects_every_non_exact_pipeline_fk(
    tmp_path: Path,
    pipeline_fk_sql: str,
) -> None:
    database_path = (tmp_path / "wrong-pipeline-fk.db").resolve()
    _create_legacy_database_with_pipeline_fk(database_path, pipeline_fk_sql)
    before = _row_snapshot(database_path)

    with pytest.raises(Exception, match="pipeline_id|foreign|外键|schema|结构"):
        _upgrade_to_baseline(database_path)

    assert _row_snapshot(database_path) == before
    assert not _has_version_table(database_path)


@pytest.mark.parametrize(
    "pipeline_fk_sql",
    [
        (
            "CONSTRAINT fk_video_posts_pipeline_id_production_pipelines_suffix "
            "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id)"
        ),
        (
            "CONSTRAINT fk_video_posts_pipeline_id_production_pipelines "
            "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id) "
            "ON UPDATE CASCADE ON DELETE CASCADE"
        ),
    ],
)
def test_stamped_head_status_rejects_non_exact_pipeline_fk(
    tmp_path: Path,
    pipeline_fk_sql: str,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / "stamped-wrong-fk.db").resolve()
    _create_legacy_database_with_pipeline_fk(database_path, pipeline_fk_sql)
    _replace_version_table(
        database_path,
        CANONICAL_VERSION_TABLE_SQL,
        revision=HEAD_REVISION,
    )

    status = schema_migrations.inspect_schema_status(database_path)

    assert status.state == "invalid"
    assert status.blockers


@pytest.mark.parametrize("index_defect", ["partial", "nocase_desc", "auto_unique"])
def test_rich_index_semantics_reject_lossy_same_name_substitutions(
    tmp_path: Path,
    index_defect: str,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / f"index-{index_defect}.db").resolve()
    _create_frozen_legacy_database(database_path)
    with sqlite3.connect(database_path) as connection:
        if index_defect == "partial":
            connection.execute("DROP INDEX ix_video_posts_platform")
            connection.execute(
                "CREATE INDEX ix_video_posts_platform ON video_posts(platform) "
                "WHERE platform <> ''"
            )
        elif index_defect == "nocase_desc":
            connection.execute("DROP INDEX ix_video_posts_platform")
            connection.execute(
                "CREATE INDEX ix_video_posts_platform "
                "ON video_posts(platform COLLATE NOCASE DESC)"
            )
        else:
            connection.execute("DROP INDEX ix_app_config_key")
            connection.execute("ALTER TABLE app_config RENAME TO app_config_old")
            connection.execute(
                "CREATE TABLE app_config ("
                "id INTEGER NOT NULL, \"key\" VARCHAR(120) NOT NULL, "
                "value JSON NOT NULL, updated_at DATETIME NOT NULL, "
                "PRIMARY KEY (id), UNIQUE(value))"
            )
            connection.execute(
                "INSERT INTO app_config SELECT * FROM app_config_old"
            )
            connection.execute("DROP TABLE app_config_old")
            connection.execute(
                "CREATE UNIQUE INDEX ix_app_config_key ON app_config(\"key\")"
            )
        connection.execute(CANONICAL_VERSION_TABLE_SQL)
        connection.execute(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (HEAD_REVISION,),
        )

    status = schema_migrations.inspect_schema_status(database_path)

    assert status.state == "invalid"
    assert status.blockers


def test_generated_business_column_cannot_hide_from_frozen_schema_validation(
    tmp_path: Path,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / "generated-business-column.db").resolve()
    _create_frozen_legacy_database(database_path)
    _upgrade_to_baseline(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "ALTER TABLE app_config ADD COLUMN stealth TEXT "
            "GENERATED ALWAYS AS (\"key\") VIRTUAL"
        )

    status = schema_migrations.inspect_schema_status(database_path)

    assert status.state == "invalid"
    assert status.blockers


def test_column_collation_difference_blocks_before_destructive_batch_rewrite(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "nocase-column.db").resolve()
    sql = FROZEN_LEGACY_SQL.read_text(encoding="utf-8")
    marker = "    video_title VARCHAR(300) NOT NULL,"
    replacement = "    video_title VARCHAR(300) COLLATE NOCASE NOT NULL,"
    assert sql.count(marker) == 1
    with sqlite3.connect(database_path) as connection:
        connection.executescript(sql.replace(marker, replacement, 1))
    before = _row_snapshot(database_path)

    with pytest.raises(Exception, match="video_posts|COLLATE|schema|结构"):
        _upgrade_to_baseline(database_path)

    assert _row_snapshot(database_path) == before
    assert not _has_version_table(database_path)


def test_hidden_table_check_constraint_blocks_before_batch_rewrite(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "hidden-check.db").resolve()
    sql = FROZEN_LEGACY_SQL.read_text(encoding="utf-8")
    marker = (
        "    updated_at DATETIME,\n"
        "    PRIMARY KEY (id)\n"
        ");\n"
        "CREATE INDEX ix_video_posts_pipeline_id"
    )
    replacement = (
        "    updated_at DATETIME,\n"
        "    PRIMARY KEY (id),\n"
        "    CHECK(video_title <> 'FORBIDDEN')\n"
        ");\n"
        "CREATE INDEX ix_video_posts_pipeline_id"
    )
    assert marker in sql
    with sqlite3.connect(database_path) as connection:
        connection.executescript(sql.replace(marker, replacement, 1))

    with pytest.raises(Exception, match="video_posts|CHECK|schema|结构"):
        _upgrade_to_baseline(database_path)

    assert not _has_version_table(database_path)


def test_autoincrement_table_semantics_cannot_masquerade_as_frozen_schema(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "autoincrement.db").resolve()
    sql = FROZEN_LEGACY_SQL.read_text(encoding="utf-8")
    frozen_block = (
        "CREATE TABLE app_config (\n"
        "    id INTEGER NOT NULL,\n"
        "    \"key\" VARCHAR(120) NOT NULL,\n"
        "    value JSON NOT NULL,\n"
        "    updated_at DATETIME NOT NULL,\n"
        "    PRIMARY KEY (id)\n"
        ");"
    )
    changed_block = (
        "CREATE TABLE app_config (\n"
        "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    \"key\" VARCHAR(120) NOT NULL,\n"
        "    value JSON NOT NULL,\n"
        "    updated_at DATETIME NOT NULL\n"
        ");"
    )
    assert frozen_block in sql
    with sqlite3.connect(database_path) as connection:
        connection.executescript(sql.replace(frozen_block, changed_block, 1))

    with pytest.raises(Exception, match="app_config|AUTOINCREMENT|schema|结构"):
        _upgrade_to_baseline(database_path)

    assert not _has_version_table(database_path)


@pytest.mark.parametrize("extra_object", ["view", "trigger"])
def test_unexpected_view_or_trigger_blocks_before_any_migration_artifact(
    tmp_path: Path,
    extra_object: str,
) -> None:
    database_path = (tmp_path / f"extra-{extra_object}.db").resolve()
    _create_frozen_legacy_database(database_path)
    with sqlite3.connect(database_path) as connection:
        if extra_object == "view":
            connection.execute(
                "CREATE VIEW video_post_titles AS "
                "SELECT id, video_title FROM video_posts"
            )
        else:
            connection.execute(
                "CREATE TRIGGER video_posts_guard AFTER UPDATE ON video_posts "
                "BEGIN SELECT 1; END"
            )

    with pytest.raises(Exception, match="view|trigger|对象|schema|未知"):
        _upgrade_to_baseline(database_path)

    with sqlite3.connect(database_path) as connection:
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema"
            ).fetchall()
        }
    assert "alembic_version" not in names
    assert not any(name.startswith("_alembic_tmp_") for name in names)


def test_revision_postvalidates_exact_head_before_version_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations_module = importlib.import_module("alembic.operations")
    database_path = (tmp_path / "post-add-validation.db").resolve()
    _create_frozen_legacy_database(database_path)
    monkeypatch.setattr(
        operations_module.BatchOperations,
        "create_foreign_key",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(Exception, match="pipeline_id|外键|head|schema|结构"):
        _upgrade_to_baseline(database_path)

    assert not _has_version_table(database_path)
    assert _schema_snapshot(database_path)["video_posts"][
        "has_named_pipeline_fk"
    ] is False


def test_revision_postvalidates_exact_base_after_fk_drop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations_module = importlib.import_module("alembic.operations")
    database_path = (tmp_path / "post-drop-validation.db").resolve()
    _create_frozen_legacy_database(database_path)
    _upgrade_to_baseline(database_path)
    monkeypatch.setattr(
        operations_module.BatchOperations,
        "drop_constraint",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(Exception, match="pipeline_id|外键|base|schema|结构"):
        _downgrade_to_base(database_path)

    assert _current_revision(database_path) == BASELINE_REVISION_ID
    assert _schema_snapshot(database_path)["video_posts"][
        "has_named_pipeline_fk"
    ] is True


def test_fresh_database_upgrades_to_frozen_twelve_table_head(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "fresh.db").resolve()
    assert not database_path.exists()
    legacy_path = (tmp_path / "expected-legacy.db").resolve()
    _create_frozen_legacy_database(legacy_path)

    _upgrade_to_baseline(database_path)

    assert _business_tables(database_path) == LEGACY_TABLES
    assert _current_revision(database_path) == BASELINE_REVISION_ID
    fresh_schema = _schema_snapshot(database_path)
    assert fresh_schema["video_posts"]["has_named_pipeline_fk"] is True
    assert _without_pipeline_fk(fresh_schema) == _schema_snapshot(legacy_path)


def test_existing_frozen_legacy_shape_upgrades_without_losing_rows(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "legacy.db").resolve()
    _create_frozen_legacy_database(database_path)
    before_schema = _schema_snapshot(database_path)
    before_rows = _row_snapshot(database_path)

    _upgrade_to_baseline(database_path)

    after_schema = _schema_snapshot(database_path)
    assert _row_snapshot(database_path) == before_rows
    assert before_rows["video_post_snapshots"]
    assert _without_pipeline_fk(after_schema) == before_schema
    assert after_schema["video_posts"]["has_named_pipeline_fk"] is True
    assert _business_tables(database_path) == LEGACY_TABLES
    assert _current_revision(database_path) == BASELINE_REVISION_ID


def test_unversioned_legacy_with_named_pipeline_fk_is_idempotent(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "already-corrected.db").resolve()
    _create_frozen_legacy_database(database_path)
    _upgrade_to_baseline(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE alembic_version")
    before_schema = _schema_snapshot(database_path)
    before_rows = _row_snapshot(database_path)

    _upgrade_to_baseline(database_path)

    assert _schema_snapshot(database_path) == before_schema
    assert _row_snapshot(database_path) == before_rows
    pipeline_fks = [
        foreign_key
        for foreign_key in _schema_snapshot(database_path)["video_posts"][
            "foreign_keys"
        ]
        if foreign_key[0] == ("pipeline_id",)
        and foreign_key[1] == "production_pipelines"
    ]
    assert len(pipeline_fks) == 1
    assert _current_revision(database_path) == BASELINE_REVISION_ID


def test_orphaned_pipeline_id_blocks_before_any_schema_write(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "orphan.db").resolve()
    _create_frozen_legacy_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE video_posts SET pipeline_id = 999999 WHERE id = 901"
        )
    before_schema = _schema_snapshot(database_path)
    before_rows = _row_snapshot(database_path)

    with pytest.raises(Exception, match="pipeline_id|孤儿"):
        _upgrade_to_baseline(database_path)

    assert _schema_snapshot(database_path) == before_schema
    assert _row_snapshot(database_path) == before_rows
    assert not _has_version_table(database_path)
    assert not any(
        table_name.startswith("_alembic_tmp_")
        for table_name in _business_tables(database_path)
    )


@pytest.mark.parametrize("unknown_extra", [False, True])
def test_partial_or_unknown_business_schema_blocks_before_version_write(
    tmp_path: Path,
    unknown_extra: bool,
) -> None:
    database_path = (tmp_path / "invalid.db").resolve()
    if unknown_extra:
        _create_frozen_legacy_database(database_path)
        with sqlite3.connect(database_path) as connection:
            connection.execute("CREATE TABLE mystery_business_table (id INTEGER)")
    else:
        with sqlite3.connect(database_path) as connection:
            connection.execute("CREATE TABLE hot_items (id INTEGER PRIMARY KEY)")
    before_schema = _schema_snapshot(database_path)
    before_rows = _row_snapshot(database_path)

    with pytest.raises(Exception, match="残缺|未知|结构|schema"):
        _upgrade_to_baseline(database_path)

    assert _schema_snapshot(database_path) == before_schema
    assert _row_snapshot(database_path) == before_rows
    assert not _has_version_table(database_path)


def test_upgrade_downgrade_and_reupgrade_preserve_all_legacy_data(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "round-trip.db").resolve()
    _create_frozen_legacy_database(database_path)
    legacy_schema = _schema_snapshot(database_path)
    legacy_rows = _row_snapshot(database_path)

    _upgrade_to_baseline(database_path)
    head_schema = _schema_snapshot(database_path)
    assert _current_revision(database_path) == BASELINE_REVISION_ID
    assert head_schema["video_posts"]["has_named_pipeline_fk"] is True

    _downgrade_to_base(database_path)
    assert _current_revision(database_path) is None
    assert _schema_snapshot(database_path) == legacy_schema
    assert _row_snapshot(database_path) == legacy_rows

    _upgrade_to_baseline(database_path)
    assert _current_revision(database_path) == BASELINE_REVISION_ID
    assert _schema_snapshot(database_path) == head_schema
    assert _row_snapshot(database_path) == legacy_rows


def test_schema_status_is_read_only_for_missing_and_unversioned_databases(
    tmp_path: Path,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    missing_path = (tmp_path / "missing-status.db").resolve()

    missing_status = schema_migrations.inspect_schema_status(missing_path)

    assert missing_status.state == "missing"
    assert not missing_path.exists()
    assert not any(
        Path(f"{missing_path}{suffix}").exists()
        for suffix in ("-journal", "-wal", "-shm")
    )

    legacy_path = (tmp_path / "unversioned-status.db").resolve()
    _create_frozen_legacy_database(legacy_path)
    before_stat = (legacy_path.stat().st_size, legacy_path.stat().st_mtime_ns)

    legacy_status = schema_migrations.inspect_schema_status(legacy_path)

    assert legacy_status.state == "unversioned"
    assert legacy_status.current_revision is None
    assert legacy_status.head_revision == HEAD_REVISION
    assert legacy_status.blockers
    assert not _has_version_table(legacy_path)
    assert (legacy_path.stat().st_size, legacy_path.stat().st_mtime_ns) == before_stat


@pytest.mark.parametrize("corruption", ["stamp_only", "missing_fk", "missing_column"])
def test_head_revision_never_substitutes_for_validated_head_schema(
    tmp_path: Path,
    corruption: str,
) -> None:
    database_path = (tmp_path / f"invalid-head-{corruption}.db").resolve()
    if corruption != "stamp_only":
        _create_frozen_legacy_database(database_path)
    with sqlite3.connect(database_path) as connection:
        if corruption == "missing_column":
            connection.execute("ALTER TABLE video_posts DROP COLUMN updated_at")
        connection.execute(CANONICAL_VERSION_TABLE_SQL)
        connection.execute(
            "INSERT INTO alembic_version (version_num) VALUES (?)",
            (HEAD_REVISION,),
        )
    before_schema = _schema_snapshot(database_path)
    before_rows = _row_snapshot(database_path)

    with pytest.raises(Exception, match="head|结构|schema|外键"):
        _upgrade_to_head(database_path)

    assert _schema_snapshot(database_path) == before_schema
    assert _row_snapshot(database_path) == before_rows
    assert _current_revision(database_path) == HEAD_REVISION


@pytest.mark.parametrize(
    ("revision_state", "expected_state"),
    [
        ("multirow", "invalid"),
        ("unknown", "ahead"),
        ("head_missing_fk", "invalid"),
    ],
)
def test_schema_status_rejects_malformed_or_unverified_revision_state(
    tmp_path: Path,
    revision_state: str,
    expected_state: str,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / f"status-{revision_state}.db").resolve()
    _create_frozen_legacy_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(CANONICAL_VERSION_TABLE_SQL)
        if revision_state == "multirow":
            connection.executemany(
                "INSERT INTO alembic_version (version_num) VALUES (?)",
                [(HEAD_REVISION,), ("20260711_unknown",)],
            )
        elif revision_state == "unknown":
            connection.execute(
                "INSERT INTO alembic_version (version_num) VALUES (?)",
                ("20260711_unknown",),
            )
        else:
            connection.execute(
                "INSERT INTO alembic_version (version_num) VALUES (?)",
                (HEAD_REVISION,),
            )

    status = schema_migrations.inspect_schema_status(database_path)

    assert status.state == expected_state
    assert status.blockers


@pytest.mark.parametrize(
    "create_sql",
    [
        (
            "CREATE TABLE alembic_version ("
            "version_num VARCHAR(32) NOT NULL PRIMARY KEY, extra TEXT)"
        ),
        "CREATE TABLE alembic_version (revision VARCHAR(32) NOT NULL PRIMARY KEY)",
        "CREATE TABLE alembic_version (version_num TEXT NOT NULL PRIMARY KEY)",
        "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)",
        "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)",
        (
            "CREATE TABLE alembic_version ("
            "version_num VARCHAR(32) NOT NULL DEFAULT 'x' PRIMARY KEY)"
        ),
        (
            "CREATE TABLE alembic_version ("
            "version_num VARCHAR(32) NOT NULL PRIMARY KEY, "
            "extra TEXT GENERATED ALWAYS AS (version_num) VIRTUAL)"
        ),
        (
            "CREATE TABLE alembic_version ("
            "version_num VARCHAR(32) COLLATE NOCASE NOT NULL "
            "PRIMARY KEY ON CONFLICT IGNORE) WITHOUT ROWID"
        ),
    ],
)
def test_schema_status_and_write_preflight_reject_noncanonical_version_table(
    tmp_path: Path,
    create_sql: str,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / "malformed-version-shape.db").resolve()
    _create_frozen_legacy_database(database_path)
    _upgrade_to_baseline(database_path)
    _replace_version_table(
        database_path,
        create_sql,
        revision=HEAD_REVISION,
    )
    before = _row_snapshot(database_path)

    status = schema_migrations.inspect_schema_status(database_path)

    assert status.state == "invalid"
    assert status.blockers
    with pytest.raises(Exception, match="alembic_version|version_num|版本"):
        _upgrade_to_head(database_path)
    assert _row_snapshot(database_path) == before


def test_canonical_version_table_accepts_empty_base_and_single_head_row(
    tmp_path: Path,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / "canonical-version.db").resolve()
    _create_frozen_legacy_database(database_path)
    _replace_version_table(
        database_path,
        CANONICAL_VERSION_TABLE_SQL,
        revision=None,
    )

    empty_status = schema_migrations.inspect_schema_status(database_path)
    assert empty_status.state == "behind"

    _upgrade_to_head(database_path)
    head_status = schema_migrations.inspect_schema_status(database_path)
    assert head_status.state == "ready"
    assert head_status.current_revision == HEAD_REVISION


def test_version_table_without_business_schema_is_invalid_not_empty(
    tmp_path: Path,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / "version-only.db").resolve()
    with sqlite3.connect(database_path) as connection:
        connection.execute(CANONICAL_VERSION_TABLE_SQL)

    status = schema_migrations.inspect_schema_status(database_path)

    assert status.state == "invalid"
    assert status.blockers
    with pytest.raises(RuntimeError, match="schema|业务|版本|invalid"):
        schema_migrations.ensure_schema_ready(database_path)
    assert _business_tables(database_path) == set()
    assert _current_revision(database_path) is None


def test_view_only_database_is_invalid_and_startup_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / "view-only.db").resolve()
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE VIEW stray_view AS SELECT 1 AS value")
    before_bytes = database_path.read_bytes()
    upgrade_called = False

    def unexpected_upgrade(*_args, **_kwargs) -> None:
        nonlocal upgrade_called
        upgrade_called = True
        raise AssertionError("view-only database must not enter bootstrap")

    monkeypatch.setattr(schema_migrations.command, "upgrade", unexpected_upgrade)

    status = schema_migrations.inspect_schema_status(database_path)

    assert status.state == "invalid"
    assert any("view:stray_view" in blocker for blocker in status.blockers)
    with pytest.raises(RuntimeError, match="schema|invalid|空白"):
        schema_migrations.ensure_schema_ready(database_path)
    assert upgrade_called is False
    assert database_path.read_bytes() == before_bytes
    with sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro",
        uri=True,
    ) as connection:
        objects = connection.execute(
            "SELECT type, name FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    assert objects == [("view", "stray_view")]
    assert not any(
        Path(f"{database_path}{suffix}").exists()
        for suffix in ("-journal", "-wal", "-shm")
    )


def test_fresh_only_preflight_rejects_view_only_database_without_artifacts(
    tmp_path: Path,
) -> None:
    command = importlib.import_module("alembic.command")
    database_path = (tmp_path / "fresh-view-only.db").resolve()
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE VIEW stray_view AS SELECT 1 AS value")
    before_bytes = database_path.read_bytes()
    config = _alembic_config(database_path)
    config.attributes["fresh_only"] = True

    with pytest.raises(Exception, match="fresh bootstrap|schema|stray_view"):
        command.upgrade(config, "head")

    assert database_path.read_bytes() == before_bytes
    with sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro",
        uri=True,
    ) as connection:
        objects = connection.execute(
            "SELECT type, name FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    assert objects == [("view", "stray_view")]
    assert not any(
        Path(f"{database_path}{suffix}").exists()
        for suffix in ("-journal", "-wal", "-shm")
    )


def test_version_table_rejects_unexpected_explicit_index(
    tmp_path: Path,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / "version-extra-index.db").resolve()
    _create_frozen_legacy_database(database_path)
    _upgrade_to_baseline(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE INDEX unexpected_version_index "
            "ON alembic_version(version_num)"
        )

    status = schema_migrations.inspect_schema_status(database_path)

    assert status.state == "invalid"
    assert status.blockers
    with pytest.raises(Exception, match="alembic_version|index|索引"):
        _upgrade_to_head(database_path)


def test_schema_seam_bootstraps_only_a_missing_clean_database(
    tmp_path: Path,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / "bootstrap.db").resolve()

    schema_migrations.ensure_schema_ready(database_path)

    status = schema_migrations.inspect_schema_status(database_path)
    assert status.state == "ready"
    assert status.current_revision == HEAD_REVISION
    assert _business_tables(database_path) == LEGACY_TABLES


def test_fresh_bootstrap_rechecks_after_missing_status_before_any_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / "fresh-race.db").resolve()
    real_upgrade = importlib.import_module("alembic.command").upgrade

    def create_legacy_then_upgrade(config, revision: str) -> None:
        _create_frozen_legacy_database(database_path)
        real_upgrade(config, revision)

    monkeypatch.setattr(
        schema_migrations.command,
        "upgrade",
        create_legacy_then_upgrade,
    )

    with pytest.raises(RuntimeError, match="fresh|空白|竞态|新建"):
        schema_migrations.upgrade_fresh_database(database_path)

    assert _business_tables(database_path) == LEGACY_TABLES
    assert not _has_version_table(database_path)
    assert _schema_snapshot(database_path)["video_posts"][
        "has_named_pipeline_fk"
    ] is False


def test_fresh_bootstrap_holds_write_lock_from_validation_through_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce the old validate/commit/migrate window with a real competitor."""
    import sqlalchemy

    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / "fresh-lock-race.db").resolve()
    real_inspect = sqlalchemy.inspect
    competitor_started = threading.Event()
    competitor_finished = threading.Event()
    competitor_outcome: list[str] = []
    competitor_thread: list[threading.Thread] = []
    inspected_empty_once = False

    def install_populated_legacy() -> None:
        competitor_started.set()
        try:
            with sqlite3.connect(database_path, timeout=5.0) as connection:
                connection.executescript(
                    FROZEN_LEGACY_SQL.read_text(encoding="utf-8")
                )
            competitor_outcome.append("installed")
        except sqlite3.Error as exc:
            competitor_outcome.append(f"blocked:{exc}")
        finally:
            competitor_finished.set()

    def inspect_with_competitor(subject):
        nonlocal inspected_empty_once
        inspector = real_inspect(subject)
        real_get_table_names = inspector.get_table_names

        def get_table_names(*args, **kwargs):
            nonlocal inspected_empty_once
            names = real_get_table_names(*args, **kwargs)
            if not names and not inspected_empty_once:
                inspected_empty_once = True
                thread = threading.Thread(target=install_populated_legacy, daemon=True)
                competitor_thread.append(thread)
                thread.start()
                assert competitor_started.wait(2.0)
                time.sleep(0.15)
            return names

        inspector.get_table_names = get_table_names
        return inspector

    monkeypatch.setattr(sqlalchemy, "inspect", inspect_with_competitor)

    schema_migrations.upgrade_fresh_database(database_path)

    assert competitor_thread
    competitor_thread[0].join(timeout=6.0)
    assert competitor_finished.is_set()
    assert competitor_outcome and competitor_outcome[0].startswith("blocked:")
    status = schema_migrations.inspect_schema_status(database_path)
    assert status.state == "ready"
    assert _current_revision(database_path) == HEAD_REVISION


def test_failed_fresh_bootstrap_rolls_back_every_schema_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / "fresh-failure.db").resolve()
    operations = importlib.import_module("alembic.operations.base")
    real_execute = operations.Operations.execute
    execute_calls = 0

    def fail_during_schema_creation(self, sqltext, *args, **kwargs):
        nonlocal execute_calls
        execute_calls += 1
        if execute_calls == 4:
            raise RuntimeError("simulated fresh bootstrap failure")
        return real_execute(self, sqltext, *args, **kwargs)

    monkeypatch.setattr(operations.Operations, "execute", fail_during_schema_creation)

    with pytest.raises(RuntimeError, match="simulated fresh bootstrap failure"):
        schema_migrations.upgrade_fresh_database(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
    assert not (tables - {"sqlite_sequence"})
    assert not any(name.startswith("_alembic_tmp_") for name in tables)


def test_owned_direct_upgrade_rolls_back_partial_ddl_and_version_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = (tmp_path / "owned-upgrade-failure.db").resolve()
    operations = importlib.import_module("alembic.operations.base")
    real_execute = operations.Operations.execute
    execute_calls = 0

    def fail_after_some_ddl(self, sqltext, *args, **kwargs):
        nonlocal execute_calls
        execute_calls += 1
        if execute_calls == 4:
            raise RuntimeError("simulated owned direct upgrade failure")
        return real_execute(self, sqltext, *args, **kwargs)

    monkeypatch.setattr(operations.Operations, "execute", fail_after_some_ddl)

    with pytest.raises(RuntimeError, match="owned direct upgrade failure"):
        _upgrade_to_baseline(database_path)

    with sqlite3.connect(database_path) as connection:
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema"
            ).fetchall()
        }
    assert not (names - {"sqlite_sequence"})
    assert "alembic_version" not in names
    assert not any(name.startswith("_alembic_tmp_") for name in names)


def test_owned_direct_downgrade_rolls_back_after_original_table_drop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlalchemy = importlib.import_module("sqlalchemy")
    database_path = (tmp_path / "owned-downgrade-failure.db").resolve()
    _create_frozen_legacy_database(database_path)
    _upgrade_to_baseline(database_path)
    before_schema = _schema_snapshot(database_path)
    before_rows = _row_snapshot(database_path)
    real_create_engine = sqlalchemy.create_engine
    injected = False

    def instrumented_create_engine(*args, **kwargs):
        engine = real_create_engine(*args, **kwargs)

        @sqlalchemy.event.listens_for(engine, "after_cursor_execute")
        def fail_after_drop(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            nonlocal injected
            normalized = " ".join(str(statement).casefold().replace('"', "").split())
            if not injected and "drop table video_posts" in normalized:
                injected = True
                raise RuntimeError("simulated failure after original table drop")

        return engine

    monkeypatch.setattr(sqlalchemy, "create_engine", instrumented_create_engine)

    with pytest.raises(RuntimeError, match="after original table drop"):
        _downgrade_to_base(database_path)

    assert injected is True
    assert _current_revision(database_path) == BASELINE_REVISION_ID
    assert _schema_snapshot(database_path) == before_schema
    assert _row_snapshot(database_path) == before_rows
    assert not any(
        name.startswith("_alembic_tmp_")
        for name in _business_tables(database_path)
    )


def test_fresh_only_rejects_a_supplied_connection_without_lock_ownership(
    tmp_path: Path,
) -> None:
    sqlalchemy = importlib.import_module("sqlalchemy")
    command = importlib.import_module("alembic.command")
    database_path = (tmp_path / "supplied-fresh-connection.db").resolve()
    engine = sqlalchemy.create_engine(
        _sqlite_url(database_path),
        isolation_level="AUTOCOMMIT",
    )
    config = _alembic_config(database_path)
    config.attributes["fresh_only"] = True
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            with pytest.raises(Exception, match="fresh_only|connection|锁|所有权"):
                command.upgrade(config, "head")
    finally:
        engine.dispose()

    assert _business_tables(database_path) == set()
    assert not _has_version_table(database_path)


def test_downgraded_database_is_behind_and_normal_startup_cannot_upgrade_it(
    tmp_path: Path,
) -> None:
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    database_path = (tmp_path / "behind.db").resolve()
    _create_frozen_legacy_database(database_path)
    _upgrade_to_baseline(database_path)
    _downgrade_to_base(database_path)
    before_schema = _schema_snapshot(database_path)
    before_rows = _row_snapshot(database_path)

    status = schema_migrations.inspect_schema_status(database_path)

    assert status.state == "behind"
    with pytest.raises(RuntimeError, match="schema|备份|apply"):
        schema_migrations.ensure_schema_ready(database_path)
    assert _schema_snapshot(database_path) == before_schema
    assert _row_snapshot(database_path) == before_rows


def test_normal_startup_bootstraps_missing_database_through_alembic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = importlib.import_module("app.main")
    database_module = importlib.import_module("app.core.database")
    database_path = (tmp_path / "startup-fresh.db").resolve()
    production_paths = [
        BACKEND_ROOT.parent / "trend_video_factory.db",
        BACKEND_ROOT / "trend_video_factory.db",
    ]
    protected_paths = [
        path
        for database in production_paths
        for path in (
            database,
            Path(f"{database}-journal"),
            Path(f"{database}-wal"),
            Path(f"{database}-shm"),
        )
    ]
    production_before = {
        path: _file_metadata(path)
        for path in protected_paths
    }
    test_engine = database_module.build_engine(_sqlite_url(database_path))
    events: list[str] = []
    monkeypatch.setattr(main_module, "engine", test_engine)
    monkeypatch.setattr(
        main_module,
        "audit_database_identity",
        lambda path: SimpleNamespace(blockers=[], warnings=[]),
    )
    monkeypatch.setattr(
        main_module,
        "_run_startup_hotspot_check_if_enabled",
        lambda: events.append("hotspot"),
    )

    async def run_lifespan() -> None:
        async with main_module.lifespan(main_module.app):
            events.append("yield")

    try:
        asyncio.run(run_lifespan())
    finally:
        test_engine.dispose()

    status = importlib.import_module(
        "app.core.schema_migrations"
    ).inspect_schema_status(database_path)
    assert status.state == "ready"
    assert events == ["hotspot", "yield"]
    assert {
        path: _file_metadata(path)
        for path in protected_paths
    } == production_before


def test_normal_startup_at_head_proceeds_without_schema_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = importlib.import_module("app.main")
    database_module = importlib.import_module("app.core.database")
    database_path = (tmp_path / "startup-head.db").resolve()
    _create_frozen_legacy_database(database_path)
    _upgrade_to_head(database_path)
    before_schema = _schema_snapshot(database_path)
    before_rows = _row_snapshot(database_path)
    test_engine = database_module.build_engine(_sqlite_url(database_path))
    events: list[str] = []
    monkeypatch.setattr(main_module, "engine", test_engine)
    monkeypatch.setattr(
        main_module,
        "audit_database_identity",
        lambda path: SimpleNamespace(blockers=[], warnings=[]),
    )
    monkeypatch.setattr(
        main_module,
        "_run_startup_hotspot_check_if_enabled",
        lambda: events.append("hotspot"),
    )

    async def run_lifespan() -> None:
        async with main_module.lifespan(main_module.app):
            events.append("yield")

    try:
        asyncio.run(run_lifespan())
    finally:
        test_engine.dispose()

    assert events == ["hotspot", "yield"]
    assert _schema_snapshot(database_path) == before_schema
    assert _row_snapshot(database_path) == before_rows


def test_explicit_temporary_database_url_bootstraps_only_that_database(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "environment-override.db").resolve()
    production_paths = [
        BACKEND_ROOT.parent / "trend_video_factory.db",
        BACKEND_ROOT / "trend_video_factory.db",
    ]
    protected_paths = [
        path
        for database in production_paths
        for path in (
            database,
            Path(f"{database}-journal"),
            Path(f"{database}-wal"),
            Path(f"{database}-shm"),
        )
    ]
    before = {path: _file_metadata(path) for path in protected_paths}
    environment = os.environ.copy()
    environment["DATABASE_URL"] = _sqlite_url(database_path)
    environment["PYTHONUTF8"] = "1"
    code = (
        "import asyncio\n"
        "from app import main\n"
        "async def run():\n"
        "    async with main.lifespan(main.app):\n"
        "        print(main.engine.url.database)\n"
        "asyncio.run(run())\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert database_path.as_posix() in result.stdout
    status = importlib.import_module(
        "app.core.schema_migrations"
    ).inspect_schema_status(database_path)
    assert status.state == "ready"
    assert {path: _file_metadata(path) for path in protected_paths} == before


def test_normal_startup_blocks_unversioned_database_before_hotspot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = importlib.import_module("app.main")
    database_module = importlib.import_module("app.core.database")
    database_path = (tmp_path / "startup-unversioned.db").resolve()
    _create_frozen_legacy_database(database_path)
    test_engine = database_module.build_engine(_sqlite_url(database_path))
    events: list[str] = []
    monkeypatch.setattr(main_module, "engine", test_engine)
    monkeypatch.setattr(
        main_module,
        "audit_database_identity",
        lambda path: SimpleNamespace(blockers=[], warnings=[]),
    )
    monkeypatch.setattr(
        main_module,
        "_run_startup_hotspot_check_if_enabled",
        lambda: events.append("hotspot"),
    )

    async def run_lifespan() -> None:
        async with main_module.lifespan(main_module.app):
            events.append("yield")

    try:
        with pytest.raises(RuntimeError, match="schema|migration|备份"):
            asyncio.run(run_lifespan())
    finally:
        test_engine.dispose()

    assert events == []
    assert not _has_version_table(database_path)


def test_alembic_history_from_unrelated_cwd_creates_no_database(
    tmp_path: Path,
) -> None:
    unrelated_cwd = tmp_path / "unrelated"
    unrelated_cwd.mkdir()
    before = set(unrelated_cwd.iterdir())

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ALEMBIC_INI),
            "history",
        ],
        cwd=unrelated_cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert HEAD_REVISION in result.stdout
    assert set(unrelated_cwd.iterdir()) == before


def test_offline_upgrade_is_rejected_even_with_explicit_temporary_url(
    tmp_path: Path,
) -> None:
    unrelated_cwd = tmp_path / "offline"
    unrelated_cwd.mkdir()
    database_path = (tmp_path / "offline-target.db").resolve()
    database_url = _sqlite_url(database_path)
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ALEMBIC_INI),
            "-x",
            f"database_url={database_url}",
            "upgrade",
            HEAD_REVISION,
            "--sql",
        ],
        cwd=unrelated_cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    assert result.returncode != 0
    assert "离线" in (result.stdout + result.stderr)
    assert not database_path.exists()
    assert list(unrelated_cwd.iterdir()) == []
