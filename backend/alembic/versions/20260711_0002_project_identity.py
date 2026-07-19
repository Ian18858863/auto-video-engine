"""Add stable project identity without touching project directories.

Revision ID: 20260711_0002
Revises: 20260711_0001
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Sequence
from typing import Any

from alembic import op


revision: str = "20260711_0002"
down_revision: str | Sequence[str] | None = "20260711_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_PROJECT_KEY_LENGTH = 80
MAX_PROJECT_SLUG_LENGTH = 48
PROJECT_KEY_INDEX = "ix_production_pipelines_project_key"
PROJECT_KEY_UNIQUE = "uq_production_pipelines_project_key"
INSERT_GUARD_TRIGGER = "trg_production_pipelines_project_identity_insert_guard"
CANONICALIZE_TRIGGER = "trg_production_pipelines_project_identity_canonicalize"
IMMUTABLE_TRIGGER = "trg_production_pipelines_project_identity_immutable"
_TEMP_PIPELINES_TABLE = "_alembic_tmp_production_pipelines_0002"
_PROJECT_KEY_PATTERN = re.compile(r"^[a-z0-9-]+$")
_PROJECT_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_LEGACY_PIPELINE_COLUMNS = (
    "id",
    "hotspot_id",
    "topic_idea_id",
    "script_asset_id",
    "project_name",
    "project_dir",
    "status",
    "provider",
    "model",
    "created_at",
    "updated_at",
)
_LEGACY_PIPELINE_INDEX_SQL = (
    "CREATE INDEX ix_production_pipelines_hotspot_id "
    "ON production_pipelines (hotspot_id)",
    "CREATE INDEX ix_production_pipelines_script_asset_id "
    "ON production_pipelines (script_asset_id)",
    "CREATE INDEX ix_production_pipelines_status "
    "ON production_pipelines (status)",
    "CREATE INDEX ix_production_pipelines_topic_idea_id "
    "ON production_pipelines (topic_idea_id)",
)


def _pipelines_table_sql(table_name: str, *, with_identity: bool) -> str:
    """Frozen 0001 column/FK order plus the additive 0002 identity terms."""

    identity_columns = ""
    identity_constraint = ""
    if with_identity:
        identity_columns = (
            "\n    project_key VARCHAR(80) NOT NULL,"
            "\n    legacy_project_dir VARCHAR(800),"
        )
        identity_constraint = (
            "\n    CONSTRAINT uq_production_pipelines_project_key "
            "UNIQUE (project_key),"
        )
    return f"""CREATE TABLE {table_name} (
    id INTEGER NOT NULL,
    hotspot_id INTEGER NOT NULL,
    topic_idea_id INTEGER,
    script_asset_id INTEGER,
    project_name VARCHAR(200) NOT NULL,
    project_dir VARCHAR(800) NOT NULL,
    status VARCHAR(40) NOT NULL,
    provider VARCHAR(80) NOT NULL,
    model VARCHAR(120) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,{identity_columns}
    PRIMARY KEY (id),{identity_constraint}
    FOREIGN KEY(hotspot_id) REFERENCES hot_items (id),
    FOREIGN KEY(topic_idea_id) REFERENCES topic_ideas (id),
    FOREIGN KEY(script_asset_id) REFERENCES script_assets (id)
)"""


def _pending_predicate(alias: str) -> str:
    slug = f"substr({alias}.project_key, 42)"
    return (
        f"length({alias}.project_key) BETWEEN 42 AND 89 "
        f"AND substr({alias}.project_key, 1, 8) = 'pending-' "
        f"AND length(substr({alias}.project_key, 9, 32)) = 32 "
        f"AND substr({alias}.project_key, 9, 32) "
        "NOT GLOB '*[^0-9a-f]*' "
        f"AND substr({alias}.project_key, 41, 1) = '-' "
        f"AND length({slug}) BETWEEN 1 AND 48 "
        f"AND {slug} NOT GLOB '*[^a-z0-9-]*' "
        f"AND substr({slug}, 1, 1) != '-' "
        f"AND substr({slug}, -1, 1) != '-' "
        f"AND {slug} = project_slug_v1({alias}.project_name)"
    )


_INSERT_GUARD_SQL = f"""CREATE TRIGGER {INSERT_GUARD_TRIGGER}
BEFORE INSERT ON production_pipelines
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM production_pipelines AS existing WHERE existing.id = NEW.id
)
OR NOT ({_pending_predicate("NEW")})
BEGIN
    SELECT CASE
        WHEN EXISTS (
            SELECT 1 FROM production_pipelines AS existing WHERE existing.id = NEW.id
        )
        THEN RAISE(ABORT, 'project identity replacement is forbidden')
    END;
    SELECT RAISE(ABORT, 'invalid pending project identity');
END"""

_CANONICALIZE_SQL = f"""CREATE TRIGGER {CANONICALIZE_TRIGGER}
AFTER INSERT ON production_pipelines
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.id < 1 OR NEW.id > 9223372036854775807
        THEN RAISE(ABORT, 'invalid project identity id')
    END;
    UPDATE production_pipelines
    SET project_key = 'p-' || NEW.id || '-' || project_slug_v1(NEW.project_name)
    WHERE id = NEW.id;
END"""

_IMMUTABLE_SQL = f"""CREATE TRIGGER {IMMUTABLE_TRIGGER}
BEFORE UPDATE OF id, project_key, legacy_project_dir ON production_pipelines
FOR EACH ROW
WHEN (
    NEW.id IS NOT OLD.id
    OR NEW.project_key IS NOT OLD.project_key
    OR NEW.legacy_project_dir IS NOT OLD.legacy_project_dir
)
AND NOT (
    NEW.id = OLD.id
    AND NEW.legacy_project_dir IS OLD.legacy_project_dir
    AND ({_pending_predicate("OLD")})
    AND NEW.project_key = 'p-' || OLD.id || '-' || project_slug_v1(OLD.project_name)
)
BEGIN
    SELECT RAISE(ABORT, 'project identity is immutable');
END"""

_TRIGGER_SQL = {
    INSERT_GUARD_TRIGGER: _INSERT_GUARD_SQL,
    CANONICALIZE_TRIGGER: _CANONICALIZE_SQL,
    IMMUTABLE_TRIGGER: _IMMUTABLE_SQL,
}


def _build_project_key(pipeline_id: int, display_title: str) -> str:
    """Frozen v1 algorithm; do not replace with a runtime import."""

    if (
        isinstance(pipeline_id, bool)
        or not isinstance(pipeline_id, int)
        or pipeline_id <= 0
        or pipeline_id > 2**63 - 1
    ):
        raise RuntimeError(f"invalid production_pipelines.id: {pipeline_id!r}")
    prefix = f"p-{pipeline_id}-"
    available = min(MAX_PROJECT_SLUG_LENGTH, MAX_PROJECT_KEY_LENGTH - len(prefix))
    ascii_title = unicodedata.normalize("NFKD", str(display_title)).encode(
        "ascii", errors="ignore"
    ).decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.casefold()).strip("-")
    slug = slug[:available].rstrip("-") or "project"
    return prefix + slug


def _valid_project_key_for_id(project_key: object, pipeline_id: int) -> bool:
    if (
        isinstance(pipeline_id, bool)
        or not isinstance(pipeline_id, int)
        or pipeline_id <= 0
        or pipeline_id > 2**63 - 1
        or not isinstance(project_key, str)
    ):
        return False
    prefix = f"p-{pipeline_id}-"
    if not project_key.startswith(prefix):
        return False
    slug = project_key[len(prefix) :]
    return (
        len(project_key) <= MAX_PROJECT_KEY_LENGTH
        and _PROJECT_KEY_PATTERN.fullmatch(project_key) is not None
        and 1 <= len(slug) <= MAX_PROJECT_SLUG_LENGTH
        and _PROJECT_SLUG_PATTERN.fullmatch(slug) is not None
    )


def project_identity_schema_blockers(
    connection: Any,
    *,
    baseline_module: Any,
) -> list[str]:
    blockers: list[str] = []
    if "production_pipelines" not in baseline_module._business_tables(connection):
        return ["missing production_pipelines identity table"]
    columns = {
        str(row[1]): (
            str(row[2]).upper(),
            int(row[3]),
            None if row[4] is None else str(row[4]),
            int(row[5]),
            int(row[6]),
        )
        for row in connection.execute(
            'PRAGMA table_xinfo("production_pipelines")'
        ).fetchall()
    }
    if columns.get("project_key") != ("VARCHAR(80)", 1, None, 0, 0):
        blockers.append("production_pipelines.project_key column is not canonical")
    if columns.get("legacy_project_dir") != ("VARCHAR(800)", 0, None, 0, 0):
        blockers.append(
            "production_pipelines.legacy_project_dir column is not canonical"
        )

    snapshot = baseline_module._schema_snapshot(connection)["production_pipelines"]
    actual_identity_terms = tuple(
        term
        for term in snapshot["table_terms"]
        if term[0]
        and (
            term[0][0] in {"i:project_key", "i:legacy_project_dir"}
            or "i:uq_production_pipelines_project_key" in term[0]
        )
    )
    expected_table_terms, _expected_table_options = (
        baseline_module._table_ddl_semantics(
            _pipelines_table_sql("production_pipelines", with_identity=True)
        )
    )
    if (
        snapshot["table_terms"] != expected_table_terms
        or snapshot["table_options"] != _expected_table_options
    ):
        blockers.append("production_pipelines table-term order/semantics are not canonical")
    expected_identity_terms = tuple(
        term
        for term in expected_table_terms
        if term[0]
        and (
            term[0][0] in {"i:project_key", "i:legacy_project_dir"}
            or "i:uq_production_pipelines_project_key" in term[0]
        )
    )
    if actual_identity_terms != expected_identity_terms:
        blockers.append(
            "project identity columns/constraint have noncanonical table-term semantics"
        )

    identity_indexes = tuple(
        index
        for index in snapshot["indexes"]
        if index[0] == PROJECT_KEY_INDEX
        or any(term[2] == "project_key" for term in index[4])
    )
    expected_indexes = tuple(
        sorted(
            (
                (
                    PROJECT_KEY_INDEX,
                    0,
                    "c",
                    0,
                    ((0, 11, "project_key", 0, "binary"),),
                    "project_key",
                    None,
                ),
                (
                    None,
                    1,
                    "u",
                    0,
                    ((0, 11, "project_key", 0, "binary"),),
                    None,
                    None,
                ),
            ),
            key=repr,
        )
    )
    if tuple(sorted(identity_indexes, key=repr)) != expected_indexes:
        blockers.append(
            "project_key indexes have noncanonical xinfo, order, collation, "
            "uniqueness, origin, or predicate semantics"
        )

    table_sql_row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='production_pipelines'"
    ).fetchone()
    table_sql = "" if table_sql_row is None else str(table_sql_row[0] or "")
    normalized_sql = " ".join(table_sql.casefold().replace('"', "").split())
    if not re.search(
        r"constraint\s+uq_production_pipelines_project_key\s+unique\s*\(\s*project_key\s*\)",
        normalized_sql,
    ):
        blockers.append("named project_key unique constraint is missing")

    trigger_rows = connection.execute(
        "SELECT name, sql FROM sqlite_schema "
        "WHERE type='trigger' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    actual_triggers = {str(row[0]): str(row[1] or "") for row in trigger_rows}
    if set(actual_triggers) != set(_TRIGGER_SQL):
        blockers.append("project identity trigger set is not canonical")
    else:
        for trigger_name, expected_sql in _TRIGGER_SQL.items():
            if baseline_module._semantic_sql_tokens(
                actual_triggers[trigger_name]
            ) != baseline_module._semantic_sql_tokens(expected_sql):
                blockers.append(
                    f"project identity trigger semantics changed: {trigger_name}"
                )
                break

    try:
        rows = connection.execute(
            "SELECT id, project_key FROM production_pipelines ORDER BY id"
        ).fetchall()
    except Exception as exc:
        blockers.append(f"cannot read project identity values: {exc}")
        return blockers
    seen: set[str] = set()
    for pipeline_id, project_key in rows:
        if not _valid_project_key_for_id(project_key, int(pipeline_id)):
            blockers.append(
                f"pipeline {pipeline_id} has null, pending, empty, or noncanonical project_key"
            )
            break
        if str(project_key) in seen:
            blockers.append(f"duplicate project_key: {project_key}")
            break
        seen.add(str(project_key))
    return blockers


def schema_blockers(
    connection: Any,
    *,
    script: Any,
    revision: str,
) -> list[str]:
    if revision != globals()["revision"]:
        return [f"0002 validator received unexpected revision: {revision}"]
    baseline = script.get_revision(str(down_revision))
    if baseline is None:
        return [f"0002 validator cannot load parent revision: {down_revision}"]
    baseline_module = baseline.module
    actual_tables = baseline_module._business_tables(connection)
    missing = sorted(baseline_module.LEGACY_TABLES - actual_tables)
    extra = sorted(actual_tables - baseline_module.LEGACY_TABLES)
    blockers: list[str] = []
    if missing:
        blockers.append("missing legacy tables: " + ", ".join(missing))
    if extra:
        blockers.append("unexpected business tables: " + ", ".join(extra))
    if blockers:
        return blockers

    blockers.extend(
        project_identity_schema_blockers(
            connection,
            baseline_module=baseline_module,
        )
    )
    unexpected_objects = [
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT type, name FROM sqlite_schema "
            "WHERE type IN ('view', 'trigger') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
        if str(row[0]) != "trigger" or str(row[1]) not in _TRIGGER_SQL
    ]
    if unexpected_objects:
        blockers.append("0002 does not allow extra views or triggers")
    if blockers:
        return blockers

    normalized = copy.deepcopy(baseline_module._schema_snapshot(connection))
    pipelines = normalized["production_pipelines"]
    pipelines["columns"] = tuple(
        column
        for column in pipelines["columns"]
        if column[0] not in {"project_key", "legacy_project_dir"}
    )
    pipelines["indexes"] = tuple(
        index
        for index in pipelines["indexes"]
        if not (
            index[0] == PROJECT_KEY_INDEX
            or any(term[2] == "project_key" for term in index[4])
        )
    )
    pipelines["table_terms"] = tuple(
        term
        for term in pipelines["table_terms"]
        if not (
            term[0]
            and (
                term[0][0] in {"i:project_key", "i:legacy_project_dir"}
                or "i:uq_production_pipelines_project_key" in term[0]
            )
        )
    )

    video_posts = normalized["video_posts"]
    pipeline_fks = tuple(
        foreign_key
        for foreign_key in video_posts["foreign_keys"]
        if baseline_module._is_pipeline_fk_candidate(foreign_key)
    )
    exact_pipeline_fks = tuple(
        foreign_key
        for foreign_key in pipeline_fks
        if foreign_key == baseline_module.EXACT_PIPELINE_FK
    )
    if len(pipeline_fks) != 1 or len(exact_pipeline_fks) != 1:
        blockers.append("0002 requires the exact 0001 video_posts pipeline FK")
    video_posts["foreign_keys"] = tuple(
        foreign_key
        for foreign_key in video_posts["foreign_keys"]
        if foreign_key != baseline_module.EXACT_PIPELINE_FK
    )
    terms = list(video_posts["table_terms"])
    for term_index, (_tokens, signature) in enumerate(terms):
        if signature == baseline_module.EXACT_PIPELINE_FK:
            terms.pop(term_index)
            break
    video_posts["table_terms"] = tuple(terms)

    expected = baseline_module._expected_legacy_snapshot()
    for table_name in sorted(baseline_module.LEGACY_TABLES):
        if normalized[table_name] != expected[table_name]:
            blockers.append(
                f"{table_name} legacy schema changed outside the 0002 identity contract"
            )
    if not blockers:
        orphan = connection.execute(
            "SELECT vp.id, vp.pipeline_id FROM video_posts AS vp "
            "LEFT JOIN production_pipelines AS pp ON pp.id = vp.pipeline_id "
            "WHERE vp.pipeline_id IS NOT NULL AND pp.id IS NULL LIMIT 1"
        ).fetchone()
        if orphan is not None:
            blockers.append("orphan video_posts.pipeline_id remains")
    return blockers


def upgrade() -> None:
    connection = op.get_bind()
    existing_columns = {
        str(row[1])
        for row in connection.exec_driver_sql(
            'PRAGMA table_xinfo("production_pipelines")'
        ).fetchall()
    }
    if {"project_key", "legacy_project_dir"} & existing_columns:
        raise RuntimeError("0002 identity columns already exist")
    rows = connection.exec_driver_sql(
        "SELECT " + ", ".join(_LEGACY_PIPELINE_COLUMNS) +
        " FROM production_pipelines ORDER BY id"
    ).fetchall()
    seen_ids: set[int] = set()
    backfill: list[tuple[object, ...]] = []
    for row in rows:
        pipeline_id = int(row[0])
        if pipeline_id in seen_ids:
            raise RuntimeError(f"duplicate production_pipelines.id: {pipeline_id}")
        seen_ids.add(pipeline_id)
        project_key = _build_project_key(pipeline_id, str(row[4]))
        backfill.append((*tuple(row), project_key, row[5]))

    connection.exec_driver_sql(
        _pipelines_table_sql(_TEMP_PIPELINES_TABLE, with_identity=True)
    )
    if backfill:
        destination_columns = (*_LEGACY_PIPELINE_COLUMNS, "project_key", "legacy_project_dir")
        placeholders = ", ".join("?" for _ in destination_columns)
        connection.exec_driver_sql(
            f"INSERT INTO {_TEMP_PIPELINES_TABLE} "
            f"({', '.join(destination_columns)}) VALUES ({placeholders})",
            backfill,
        )
    connection.exec_driver_sql("DROP TABLE production_pipelines")
    connection.exec_driver_sql(
        f"ALTER TABLE {_TEMP_PIPELINES_TABLE} RENAME TO production_pipelines"
    )
    for statement in _LEGACY_PIPELINE_INDEX_SQL:
        connection.exec_driver_sql(statement)
    connection.exec_driver_sql(
        f"CREATE INDEX {PROJECT_KEY_INDEX} "
        "ON production_pipelines (project_key)"
    )
    # Create the immutable guard first so the canonicalizing trigger's UPDATE
    # must pass the same database rule that protects later raw SQL writes.
    connection.exec_driver_sql(_IMMUTABLE_SQL)
    connection.exec_driver_sql(_INSERT_GUARD_SQL)
    connection.exec_driver_sql(_CANONICALIZE_SQL)


def downgrade() -> None:
    connection = op.get_bind()
    for trigger_name in _TRIGGER_SQL:
        connection.exec_driver_sql(f"DROP TRIGGER {trigger_name}")
    connection.exec_driver_sql(
        _pipelines_table_sql(_TEMP_PIPELINES_TABLE, with_identity=False)
    )
    columns = ", ".join(_LEGACY_PIPELINE_COLUMNS)
    source_columns = ", ".join(
        "COALESCE(legacy_project_dir, project_dir)"
        if column == "project_dir"
        else column
        for column in _LEGACY_PIPELINE_COLUMNS
    )
    connection.exec_driver_sql(
        f"INSERT INTO {_TEMP_PIPELINES_TABLE} ({columns}) "
        f"SELECT {source_columns} FROM production_pipelines ORDER BY id"
    )
    connection.exec_driver_sql("DROP TABLE production_pipelines")
    connection.exec_driver_sql(
        f"ALTER TABLE {_TEMP_PIPELINES_TABLE} RENAME TO production_pipelines"
    )
    for statement in _LEGACY_PIPELINE_INDEX_SQL:
        connection.exec_driver_sql(statement)
