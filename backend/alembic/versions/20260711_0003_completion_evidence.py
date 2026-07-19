"""Add exact completion evidence and retire unproven completed status.

Revision ID: 20260711_0003
Revises: 20260711_0002
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op


revision: str = "20260711_0003"
down_revision: str | Sequence[str] | None = "20260711_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMPLETION_EVIDENCE_COLUMN = "completion_evidence"
_EXPECTED_COLUMN = ("JSON", 1, "'{}'", 0, 0)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _completion_term(baseline_module: Any) -> tuple[object, ...]:
    terms, options = baseline_module._table_ddl_semantics(
        "CREATE TABLE completion_evidence_contract ("
        "completion_evidence JSON DEFAULT '{}' NOT NULL)"
    )
    if options or len(terms) != 1:
        raise RuntimeError("cannot construct completion evidence column authority")
    return terms[0]


def _identity_blockers_from_normalized_snapshot(
    connection: Any,
    *,
    normalized: dict[str, dict[str, object]],
    parent_module: Any,
    baseline_module: Any,
) -> list[str]:
    blockers: list[str] = []
    pipelines = normalized["production_pipelines"]
    columns = {
        str(column[0]): tuple(column[1:])
        for column in pipelines["columns"]
    }
    if columns.get("project_key") != ("VARCHAR(80)", 1, None, 0, 0):
        blockers.append("production_pipelines.project_key column is not canonical")
    if columns.get("legacy_project_dir") != ("VARCHAR(800)", 0, None, 0, 0):
        blockers.append(
            "production_pipelines.legacy_project_dir column is not canonical"
        )

    expected_terms, expected_options = baseline_module._table_ddl_semantics(
        parent_module._pipelines_table_sql(
            "production_pipelines",
            with_identity=True,
        )
    )
    if (
        pipelines["table_terms"] != expected_terms
        or pipelines["table_options"] != expected_options
    ):
        blockers.append(
            "production_pipelines 0002 table semantics changed outside completion evidence"
        )

    identity_indexes = tuple(
        index
        for index in pipelines["indexes"]
        if index[0] == parent_module.PROJECT_KEY_INDEX
        or any(term[2] == "project_key" for term in index[4])
    )
    expected_indexes = tuple(
        sorted(
            (
                (
                    parent_module.PROJECT_KEY_INDEX,
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
        blockers.append("project_key indexes are not the exact 0002 authority")

    table_sql_row = connection.execute(
        "SELECT sql FROM sqlite_schema "
        "WHERE type='table' AND name='production_pipelines'"
    ).fetchone()
    table_sql = "" if table_sql_row is None else str(table_sql_row[0] or "")
    normalized_sql = " ".join(table_sql.casefold().replace('"', "").split())
    if not re.search(
        r"constraint\s+uq_production_pipelines_project_key\s+"
        r"unique\s*\(\s*project_key\s*\)",
        normalized_sql,
    ):
        blockers.append("named project_key unique constraint is missing")

    trigger_rows = connection.execute(
        "SELECT name, sql FROM sqlite_schema "
        "WHERE type='trigger' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    actual_triggers = {str(row[0]): str(row[1] or "") for row in trigger_rows}
    if set(actual_triggers) != set(parent_module._TRIGGER_SQL):
        blockers.append("project identity trigger set is not canonical")
    else:
        for trigger_name, expected_sql in parent_module._TRIGGER_SQL.items():
            if baseline_module._semantic_sql_tokens(
                actual_triggers[trigger_name]
            ) != baseline_module._semantic_sql_tokens(expected_sql):
                blockers.append(
                    f"project identity trigger semantics changed: {trigger_name}"
                )
                break

    unexpected_views = connection.execute(
        "SELECT name FROM sqlite_schema "
        "WHERE type='view' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    if unexpected_views:
        blockers.append("0003 does not allow extra views")

    try:
        rows = connection.execute(
            "SELECT id, project_key FROM production_pipelines ORDER BY id"
        ).fetchall()
    except Exception as exc:
        return blockers + [f"cannot read project identity values: {exc}"]
    seen: set[str] = set()
    for pipeline_id, project_key in rows:
        if not parent_module._valid_project_key_for_id(
            project_key,
            int(pipeline_id),
        ):
            blockers.append(
                f"pipeline {pipeline_id} has a noncanonical project_key"
            )
            break
        if str(project_key) in seen:
            blockers.append(f"duplicate project_key: {project_key}")
            break
        seen.add(str(project_key))
    return blockers


def _parent_projection_blockers(
    normalized: dict[str, dict[str, object]],
    *,
    parent_module: Any,
    baseline_module: Any,
) -> list[str]:
    blockers: list[str] = []
    projection = copy.deepcopy(normalized)
    pipelines = projection["production_pipelines"]
    pipelines["columns"] = tuple(
        column
        for column in pipelines["columns"]
        if column[0] not in {"project_key", "legacy_project_dir"}
    )
    pipelines["indexes"] = tuple(
        index
        for index in pipelines["indexes"]
        if not (
            index[0] == parent_module.PROJECT_KEY_INDEX
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

    video_posts = projection["video_posts"]
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
        blockers.append("0003 requires the exact 0002 video_posts pipeline FK")
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
        if projection[table_name] != expected[table_name]:
            blockers.append(
                f"{table_name} schema changed outside the 0002 + evidence contract"
            )
    return blockers


def schema_blockers(
    connection: Any,
    *,
    script: Any,
    revision: str,
) -> list[str]:
    if revision != globals()["revision"]:
        return [f"0003 validator received unexpected revision: {revision}"]
    parent = script.get_revision(str(down_revision))
    if parent is None:
        return [f"0003 validator cannot load parent revision: {down_revision}"]
    baseline = script.get_revision(str(parent.down_revision))
    if baseline is None:
        return [f"0003 validator cannot load baseline revision: {parent.down_revision}"]
    parent_module = parent.module
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

    column_rows = connection.execute(
        'PRAGMA table_xinfo("production_pipelines")'
    ).fetchall()
    completion_rows = [
        row for row in column_rows if str(row[1]) == COMPLETION_EVIDENCE_COLUMN
    ]
    if len(completion_rows) != 1:
        blockers.append("production_pipelines must have one completion_evidence column")
    else:
        row = completion_rows[0]
        actual_column = (
            str(row[2]).upper(),
            int(row[3]),
            None if row[4] is None else str(row[4]),
            int(row[5]),
            int(row[6]),
        )
        if actual_column != _EXPECTED_COLUMN:
            blockers.append("completion_evidence column is not exact JSON NOT NULL DEFAULT '{}'")
        if int(row[0]) != max(int(column[0]) for column in column_rows):
            blockers.append(
                "completion_evidence physical column position must be last"
            )

    try:
        actual_snapshot = baseline_module._schema_snapshot(connection)
        normalized = copy.deepcopy(actual_snapshot)
        pipelines = normalized["production_pipelines"]
        completion_terms = tuple(
            term
            for term in pipelines["table_terms"]
            if term[0] and term[0][0] == "i:completion_evidence"
        )
        if completion_terms != (_completion_term(baseline_module),):
            blockers.append("completion_evidence table-term semantics are not exact")
        table_terms = tuple(pipelines["table_terms"])
        evidence_term_indexes = tuple(
            index
            for index, term in enumerate(table_terms)
            if term[0] and term[0][0] == "i:completion_evidence"
        )
        old_column_heads = {
            f"i:{str(column[1]).casefold()}"
            for column in column_rows
            if str(column[1]) != COMPLETION_EVIDENCE_COLUMN
        }
        old_column_indexes = tuple(
            index
            for index, term in enumerate(table_terms)
            if term[0] and term[0][0] in old_column_heads
        )
        constraint_indexes = tuple(
            index
            for index, term in enumerate(table_terms)
            if term[0]
            and term[0][0] not in old_column_heads
            and term[0][0] != "i:completion_evidence"
        )
        if (
            len(evidence_term_indexes) != 1
            or not old_column_indexes
            or evidence_term_indexes[0] != max(old_column_indexes) + 1
            or (
                constraint_indexes
                and evidence_term_indexes[0] >= min(constraint_indexes)
            )
        ):
            blockers.append(
                "completion_evidence table-term order must follow all old columns "
                "and precede table constraints"
            )
        pipelines["columns"] = tuple(
            column
            for column in pipelines["columns"]
            if column[0] != COMPLETION_EVIDENCE_COLUMN
        )
        pipelines["table_terms"] = tuple(
            term
            for term in pipelines["table_terms"]
            if not (term[0] and term[0][0] == "i:completion_evidence")
        )
    except Exception as exc:
        return blockers + [f"cannot normalize 0003 rich schema snapshot: {exc}"]

    blockers.extend(
        _identity_blockers_from_normalized_snapshot(
            connection,
            normalized=normalized,
            parent_module=parent_module,
            baseline_module=baseline_module,
        )
    )
    blockers.extend(
        _parent_projection_blockers(
            normalized,
            parent_module=parent_module,
            baseline_module=baseline_module,
        )
    )

    try:
        evidence_rows = connection.execute(
            "SELECT id, completion_evidence "
            "FROM production_pipelines ORDER BY id"
        ).fetchall()
    except Exception as exc:
        return blockers + [f"cannot read completion evidence values: {exc}"]
    for pipeline_id, raw_evidence in evidence_rows:
        try:
            parsed = json.loads(
                raw_evidence,
                parse_constant=_reject_json_constant,
            )
        except (
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            OverflowError,
        ):
            blockers.append(f"pipeline {pipeline_id} completion_evidence is invalid JSON")
            break
        if not isinstance(parsed, dict):
            blockers.append(f"pipeline {pipeline_id} completion_evidence is not an object")
            break

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
    op.add_column(
        "production_pipelines",
        sa.Column(
            COMPLETION_EVIDENCE_COLUMN,
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.execute(
        "UPDATE production_pipelines "
        "SET status = '需要人工确认' "
        "WHERE status = '已完成'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE production_pipelines "
        "SET status = '需要人工确认' "
        "WHERE status = '已完成'"
    )
    op.drop_column("production_pipelines", COMPLETION_EVIDENCE_COLUMN)
