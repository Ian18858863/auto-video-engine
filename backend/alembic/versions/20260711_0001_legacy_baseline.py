"""Freeze the verified twelve-table legacy SQLite schema.

Revision ID: 20260711_0001
Revises: None
"""
from __future__ import annotations

import copy
import re
import sqlite3
from functools import lru_cache
from typing import Any, Sequence

from alembic import op


revision: str = "20260711_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PIPELINE_FK_NAME = "fk_video_posts_pipeline_id_production_pipelines"
PINNED_ALEMBIC_VERSION_SQL = """
CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
)
"""
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

# Frozen verbatim in structure and column order from the verified pre-Phase-0
# backup (SHA-256 A349DD2E...DE0BEF1). It intentionally keeps the eighteen
# ALTER-added nullable differences and does not import current ORM metadata.
FROZEN_SCHEMA_SQL = r"""
CREATE TABLE ai_generations (
    id INTEGER NOT NULL, generation_type VARCHAR(80) NOT NULL,
    input_hash VARCHAR(128) NOT NULL, input_content JSON NOT NULL,
    output_content JSON NOT NULL, provider VARCHAR(80) NOT NULL,
    model VARCHAR(120) NOT NULL, created_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_ai_generations_generation_type ON ai_generations (generation_type);
CREATE UNIQUE INDEX ix_ai_generations_input_hash ON ai_generations (input_hash);

CREATE TABLE app_config (
    id INTEGER NOT NULL, "key" VARCHAR(120) NOT NULL, value JSON NOT NULL,
    updated_at DATETIME NOT NULL, PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_app_config_key ON app_config ("key");

CREATE TABLE copywriting_samples (
    id INTEGER NOT NULL, title VARCHAR(300) NOT NULL,
    sample_type VARCHAR(80) NOT NULL, sample_text TEXT NOT NULL,
    scene VARCHAR(100) NOT NULL, content_direction VARCHAR(120) NOT NULL,
    structure_type VARCHAR(120) NOT NULL, why_reference TEXT NOT NULL,
    reusable_points TEXT NOT NULL, avoid_points TEXT NOT NULL,
    tags JSON NOT NULL, enabled BOOLEAN NOT NULL,
    is_key_reference BOOLEAN NOT NULL, notes TEXT NOT NULL,
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_copywriting_samples_content_direction ON copywriting_samples (content_direction);
CREATE INDEX ix_copywriting_samples_enabled ON copywriting_samples (enabled);
CREATE INDEX ix_copywriting_samples_is_key_reference ON copywriting_samples (is_key_reference);
CREATE INDEX ix_copywriting_samples_sample_type ON copywriting_samples (sample_type);
CREATE INDEX ix_copywriting_samples_scene ON copywriting_samples (scene);
CREATE INDEX ix_copywriting_samples_title ON copywriting_samples (title);

CREATE TABLE hot_items (
    id INTEGER NOT NULL, source_platform VARCHAR(100) NOT NULL,
    source_type VARCHAR(50) NOT NULL, title VARCHAR(300) NOT NULL,
    url VARCHAR(800) NOT NULL, published_at DATETIME,
    fetched_at DATETIME NOT NULL, summary VARCHAR(300) NOT NULL,
    metrics JSON NOT NULL, tags JSON NOT NULL, category VARCHAR(50) NOT NULL,
    is_filtered BOOLEAN NOT NULL, filter_reason VARCHAR(200) NOT NULL,
    hot_score FLOAT NOT NULL, topic_score FLOAT NOT NULL,
    save_score FLOAT NOT NULL, is_favorite BOOLEAN NOT NULL,
    has_topic_ideas BOOLEAN NOT NULL, has_script BOOLEAN NOT NULL,
    data_origin VARCHAR(30) DEFAULT 'real', origin_note VARCHAR(200) DEFAULT '',
    risk_score FLOAT DEFAULT 0, freshness_score FLOAT DEFAULT 0,
    duplicate_reason VARCHAR(300) DEFAULT '',
    duplicate_group_key VARCHAR(160) DEFAULT '',
    lifecycle_status VARCHAR(40) DEFAULT 'active', archived_at DATETIME,
    archive_reason VARCHAR(200) DEFAULT '', PRIMARY KEY (id)
);
CREATE INDEX ix_hot_items_category ON hot_items (category);
CREATE INDEX ix_hot_items_data_origin ON hot_items (data_origin);
CREATE INDEX ix_hot_items_duplicate_group_key ON hot_items (duplicate_group_key);
CREATE INDEX ix_hot_items_has_script ON hot_items (has_script);
CREATE INDEX ix_hot_items_has_topic_ideas ON hot_items (has_topic_ideas);
CREATE INDEX ix_hot_items_is_favorite ON hot_items (is_favorite);
CREATE INDEX ix_hot_items_is_filtered ON hot_items (is_filtered);
CREATE INDEX ix_hot_items_lifecycle_status ON hot_items (lifecycle_status);
CREATE INDEX ix_hot_items_source_platform ON hot_items (source_platform);
CREATE INDEX ix_hot_items_source_type ON hot_items (source_type);
CREATE INDEX ix_hot_items_title ON hot_items (title);
CREATE UNIQUE INDEX ix_hot_items_url ON hot_items (url);

CREATE TABLE source_runs (
    id INTEGER NOT NULL, source_name VARCHAR(120) NOT NULL,
    started_at DATETIME NOT NULL, ended_at DATETIME,
    success BOOLEAN NOT NULL, items_fetched INTEGER NOT NULL,
    error_message TEXT NOT NULL, status VARCHAR(20) DEFAULT '失败',
    is_rate_limited BOOLEAN DEFAULT 0, PRIMARY KEY (id)
);
CREATE INDEX ix_source_runs_is_rate_limited ON source_runs (is_rate_limited);
CREATE INDEX ix_source_runs_source_name ON source_runs (source_name);
CREATE INDEX ix_source_runs_success ON source_runs (success);

CREATE TABLE topic_ideas (
    id INTEGER NOT NULL, hot_item_id INTEGER NOT NULL,
    title VARCHAR(300) NOT NULL, core_view VARCHAR(500) NOT NULL,
    recommended_format VARCHAR(80) NOT NULL, why_watch VARCHAR(500) NOT NULL,
    why_save VARCHAR(500) NOT NULL, comment_trigger VARCHAR(500) NOT NULL,
    risk_note VARCHAR(500) NOT NULL, recommended_duration VARCHAR(80) NOT NULL,
    hook VARCHAR(500) NOT NULL, is_selected BOOLEAN NOT NULL,
    video_type VARCHAR(80) DEFAULT 'single_deep_dive',
    generation_source VARCHAR(80) DEFAULT 'fake_provider',
    recommendation_score FLOAT DEFAULT 0,
    recommendation_bucket VARCHAR(40) DEFAULT 'backup',
    source_hotspot_ids JSON DEFAULT '[]',
    source_hotspot_titles JSON DEFAULT '[]', is_example BOOLEAN DEFAULT 0,
    PRIMARY KEY (id), FOREIGN KEY(hot_item_id) REFERENCES hot_items (id)
);
CREATE INDEX ix_topic_ideas_hot_item_id ON topic_ideas (hot_item_id);
CREATE INDEX ix_topic_ideas_is_selected ON topic_ideas (is_selected);
CREATE INDEX ix_topic_ideas_recommendation_bucket ON topic_ideas (recommendation_bucket);
CREATE INDEX ix_topic_ideas_recommendation_score ON topic_ideas (recommendation_score);

CREATE TABLE script_assets (
    id INTEGER NOT NULL, topic_idea_id INTEGER NOT NULL,
    voiceover TEXT NOT NULL, storyboard JSON NOT NULL,
    subtitles JSON NOT NULL, animation_prompts JSON NOT NULL,
    title_options JSON NOT NULL, cover_copy JSON NOT NULL,
    publish_copy JSON NOT NULL, edit_checklist JSON NOT NULL,
    created_at DATETIME NOT NULL, PRIMARY KEY (id),
    FOREIGN KEY(topic_idea_id) REFERENCES topic_ideas (id)
);
CREATE INDEX ix_script_assets_topic_idea_id ON script_assets (topic_idea_id);

CREATE TABLE production_pipelines (
    id INTEGER NOT NULL, hotspot_id INTEGER NOT NULL, topic_idea_id INTEGER,
    script_asset_id INTEGER, project_name VARCHAR(200) NOT NULL,
    project_dir VARCHAR(800) NOT NULL, status VARCHAR(40) NOT NULL,
    provider VARCHAR(80) NOT NULL, model VARCHAR(120) NOT NULL,
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
    PRIMARY KEY (id), FOREIGN KEY(hotspot_id) REFERENCES hot_items (id),
    FOREIGN KEY(topic_idea_id) REFERENCES topic_ideas (id),
    FOREIGN KEY(script_asset_id) REFERENCES script_assets (id)
);
CREATE INDEX ix_production_pipelines_hotspot_id ON production_pipelines (hotspot_id);
CREATE INDEX ix_production_pipelines_script_asset_id ON production_pipelines (script_asset_id);
CREATE INDEX ix_production_pipelines_status ON production_pipelines (status);
CREATE INDEX ix_production_pipelines_topic_idea_id ON production_pipelines (topic_idea_id);

CREATE TABLE content_quality_checks (
    id INTEGER NOT NULL, pipeline_id INTEGER, script_asset_id INTEGER,
    source_type VARCHAR(80) NOT NULL, source_title VARCHAR(300) NOT NULL,
    checked_text TEXT NOT NULL, result JSON NOT NULL,
    created_at DATETIME NOT NULL, PRIMARY KEY (id),
    FOREIGN KEY(pipeline_id) REFERENCES production_pipelines (id),
    FOREIGN KEY(script_asset_id) REFERENCES script_assets (id)
);
CREATE INDEX ix_content_quality_checks_pipeline_id ON content_quality_checks (pipeline_id);
CREATE INDEX ix_content_quality_checks_script_asset_id ON content_quality_checks (script_asset_id);
CREATE INDEX ix_content_quality_checks_source_type ON content_quality_checks (source_type);

CREATE TABLE pipeline_steps (
    id INTEGER NOT NULL, pipeline_id INTEGER NOT NULL,
    name VARCHAR(80) NOT NULL, step_order INTEGER NOT NULL,
    status VARCHAR(40) NOT NULL, error TEXT NOT NULL,
    output_summary JSON NOT NULL, started_at DATETIME, ended_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(pipeline_id) REFERENCES production_pipelines (id)
);
CREATE INDEX ix_pipeline_steps_pipeline_id ON pipeline_steps (pipeline_id);
CREATE INDEX ix_pipeline_steps_status ON pipeline_steps (status);

CREATE TABLE video_posts (
    id INTEGER NOT NULL, platform VARCHAR(80) NOT NULL,
    video_title VARCHAR(300) NOT NULL, published_at DATETIME,
    video_url VARCHAR(800) NOT NULL, topic_type VARCHAR(80) NOT NULL,
    video_format VARCHAR(80) NOT NULL, hook_type VARCHAR(80) NOT NULL,
    duration_seconds INTEGER NOT NULL, views INTEGER NOT NULL,
    likes INTEGER NOT NULL, comments INTEGER NOT NULL,
    favorites INTEGER NOT NULL, shares INTEGER NOT NULL,
    followers_gained INTEGER NOT NULL, completion_rate FLOAT NOT NULL,
    average_watch_seconds FLOAT NOT NULL, profile_visits INTEGER NOT NULL,
    current_view_velocity FLOAT NOT NULL, notes TEXT NOT NULL,
    pipeline_id INTEGER, updated_at DATETIME, PRIMARY KEY (id)
);
CREATE INDEX ix_video_posts_pipeline_id ON video_posts (pipeline_id);
CREATE INDEX ix_video_posts_platform ON video_posts (platform);

CREATE TABLE video_post_snapshots (
    id INTEGER NOT NULL, video_post_id INTEGER NOT NULL,
    captured_at DATETIME NOT NULL, elapsed_minutes FLOAT NOT NULL,
    views INTEGER NOT NULL, likes INTEGER NOT NULL, comments INTEGER NOT NULL,
    favorites INTEGER NOT NULL, shares INTEGER NOT NULL,
    followers_gained INTEGER NOT NULL, completion_rate FLOAT NOT NULL,
    average_watch_seconds FLOAT NOT NULL, notes TEXT NOT NULL,
    PRIMARY KEY (id), FOREIGN KEY(video_post_id) REFERENCES video_posts (id)
);
CREATE INDEX ix_video_post_snapshots_captured_at ON video_post_snapshots (captured_at);
CREATE INDEX ix_video_post_snapshots_video_post_id ON video_post_snapshots (video_post_id);
"""


def _iter_schema_statements() -> list[str]:
    statements: list[str] = []
    buffer = ""
    for line in FROZEN_SCHEMA_SQL.splitlines():
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip().removesuffix(";").strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise RuntimeError("冻结 schema SQL 存在不完整语句")
    return statements


def _business_tables(connection: Any) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND name != 'alembic_version'"
        ).fetchall()
    }


def _normalize_sql_fragment(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.casefold().strip()
    normalized = re.sub(r'"([a-z_][a-z0-9_]*)"', r"\1", normalized)
    normalized = re.sub(r'`([a-z_][a-z0-9_]*)`', r"\1", normalized)
    normalized = re.sub(r'\[([a-z_][a-z0-9_]*)\]', r"\1", normalized)
    normalized = " ".join(normalized.split())
    return re.sub(r"\s*([(),])\s*", r"\1", normalized)


def _index_definition_parts(index_sql: str | None) -> tuple[str | None, str | None]:
    if index_sql is None:
        return None, None
    on_match = re.search(r"\bon\b", index_sql, flags=re.IGNORECASE)
    if on_match is None:
        return _normalize_sql_fragment(index_sql), None
    opening = index_sql.find("(", on_match.end())
    if opening < 0:
        return _normalize_sql_fragment(index_sql), None
    depth = 0
    closing = -1
    quote: str | None = None
    for position in range(opening, len(index_sql)):
        char = index_sql[position]
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                closing = position
                break
    if closing < 0:
        return _normalize_sql_fragment(index_sql), None
    key_expression = _normalize_sql_fragment(index_sql[opening + 1 : closing])
    tail = index_sql[closing + 1 :]
    predicate_match = re.search(
        r"\bwhere\b(?P<predicate>.+)$",
        tail,
        flags=re.IGNORECASE | re.DOTALL,
    )
    predicate = (
        None
        if predicate_match is None
        else _normalize_sql_fragment(predicate_match.group("predicate"))
    )
    return key_expression, predicate


_IDENTIFIER = r'(?:"(?:[^"]|"")*"|`[^`]*`|\[[^\]]*\]|[A-Za-z_][A-Za-z0-9_$]*)'
_FK_CLAUSE = re.compile(
    rf"(?:CONSTRAINT\s+(?P<name>{_IDENTIFIER})\s+)?"
    rf"FOREIGN\s+KEY\s*\((?P<local>[^)]*)\)\s*"
    rf"REFERENCES\s+(?P<table>{_IDENTIFIER})\s*\((?P<remote>[^)]*)\)"
    r"(?P<tail>(?:\s+ON\s+(?:UPDATE|DELETE)\s+"
    r"(?:NO\s+ACTION|RESTRICT|SET\s+NULL|SET\s+DEFAULT|CASCADE)"
    r"|\s+MATCH\s+[A-Za-z_][A-Za-z0-9_$]*)*)",
    flags=re.IGNORECASE | re.DOTALL,
)


def _unquote_identifier(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and (
        (value[0] == value[-1] and value[0] in {'"', "'", "`"})
        or (value[0] == "[" and value[-1] == "]")
    ):
        value = value[1:-1]
    return value.replace('""', '"')


def _identifier_list(value: str) -> tuple[str, ...]:
    return tuple(_unquote_identifier(item) for item in value.split(","))


def _action_from_tail(tail: str, action: str) -> str:
    match = re.search(
        rf"\bON\s+{action}\s+"
        r"(NO\s+ACTION|RESTRICT|SET\s+NULL|SET\s+DEFAULT|CASCADE)",
        tail,
        flags=re.IGNORECASE,
    )
    return "NO ACTION" if match is None else " ".join(match.group(1).upper().split())


def _match_from_tail(tail: str) -> str:
    match = re.search(
        r"\bMATCH\s+([A-Za-z_][A-Za-z0-9_$]*)",
        tail,
        flags=re.IGNORECASE,
    )
    return "NONE" if match is None else match.group(1).upper()


def _sql_structure_without_comments_or_strings(sql: str) -> str:
    """Keep SQL structure while blanking comments and single-quoted values."""
    output: list[str] = []
    position = 0
    state = "normal"
    while position < len(sql):
        char = sql[position]
        following = sql[position + 1] if position + 1 < len(sql) else ""
        if state == "normal":
            if char == "'":
                output.append(" ")
                state = "string"
            elif char == "-" and following == "-":
                output.extend((" ", " "))
                position += 1
                state = "line_comment"
            elif char == "/" and following == "*":
                output.extend((" ", " "))
                position += 1
                state = "block_comment"
            elif char == '"':
                output.append(char)
                state = "double_identifier"
            elif char == "`":
                output.append(char)
                state = "backtick_identifier"
            elif char == "[":
                output.append(char)
                state = "bracket_identifier"
            else:
                output.append(char)
        elif state == "string":
            if char == "'" and following == "'":
                output.extend((" ", " "))
                position += 1
            elif char == "'":
                output.append(" ")
                state = "normal"
            else:
                output.append("\n" if char == "\n" else " ")
        elif state == "line_comment":
            if char == "\n":
                output.append("\n")
                state = "normal"
            else:
                output.append(" ")
        elif state == "block_comment":
            if char == "*" and following == "/":
                output.extend((" ", " "))
                position += 1
                state = "normal"
            else:
                output.append("\n" if char == "\n" else " ")
        elif state == "double_identifier":
            output.append(char)
            if char == '"' and following == '"':
                output.append(following)
                position += 1
            elif char == '"':
                state = "normal"
        elif state == "backtick_identifier":
            output.append(char)
            if char == "`" and following == "`":
                output.append(following)
                position += 1
            elif char == "`":
                state = "normal"
        else:
            output.append(char)
            if char == "]":
                state = "normal"
        position += 1
    return "".join(output)


def _sql_semantics_without_comments(sql: str) -> str:
    """Remove comments while preserving literals and quoted identifiers."""
    output: list[str] = []
    position = 0
    state = "normal"
    while position < len(sql):
        char = sql[position]
        following = sql[position + 1] if position + 1 < len(sql) else ""
        if state == "normal":
            if char == "-" and following == "-":
                output.extend((" ", " "))
                position += 1
                state = "line_comment"
            elif char == "/" and following == "*":
                output.extend((" ", " "))
                position += 1
                state = "block_comment"
            else:
                output.append(char)
                if char == "'":
                    state = "string"
                elif char == '"':
                    state = "double_identifier"
                elif char == "`":
                    state = "backtick_identifier"
                elif char == "[":
                    state = "bracket_identifier"
        elif state == "line_comment":
            if char == "\n":
                output.append("\n")
                state = "normal"
            else:
                output.append(" ")
        elif state == "block_comment":
            if char == "*" and following == "/":
                output.extend((" ", " "))
                position += 1
                state = "normal"
            else:
                output.append("\n" if char == "\n" else " ")
        else:
            output.append(char)
            if state == "string" and char == "'":
                if following == "'":
                    output.append(following)
                    position += 1
                else:
                    state = "normal"
            elif state == "double_identifier" and char == '"':
                if following == '"':
                    output.append(following)
                    position += 1
                else:
                    state = "normal"
            elif state == "backtick_identifier" and char == "`":
                if following == "`":
                    output.append(following)
                    position += 1
                else:
                    state = "normal"
            elif state == "bracket_identifier" and char == "]":
                state = "normal"
        position += 1
    return "".join(output)


def _top_level_table_terms(structural_sql: str) -> list[str]:
    opening = structural_sql.find("(")
    if opening < 0:
        return []
    terms: list[str] = []
    start = opening + 1
    depth = 1
    state = "normal"
    position = opening + 1
    while position < len(structural_sql):
        char = structural_sql[position]
        following = (
            structural_sql[position + 1]
            if position + 1 < len(structural_sql)
            else ""
        )
        if state == "normal":
            if char == "'":
                state = "string"
            elif char == '"':
                state = "double_identifier"
            elif char == "`":
                state = "backtick_identifier"
            elif char == "[":
                state = "bracket_identifier"
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    terms.append(structural_sql[start:position].strip())
                    return [term for term in terms if term]
            elif char == "," and depth == 1:
                terms.append(structural_sql[start:position].strip())
                start = position + 1
        elif state == "string":
            if char == "'" and following == "'":
                position += 1
            elif char == "'":
                state = "normal"
        elif state == "double_identifier":
            if char == '"' and following == '"':
                position += 1
            elif char == '"':
                state = "normal"
        elif state == "backtick_identifier":
            if char == "`" and following == "`":
                position += 1
            elif char == "`":
                state = "normal"
        elif char == "]":
            state = "normal"
        position += 1
    return []


def _semantic_sql_tokens(sql: str) -> tuple[str, ...]:
    tokens: list[str] = []
    position = 0
    while position < len(sql):
        char = sql[position]
        if char.isspace():
            position += 1
            continue
        if char == "'":
            position += 1
            value: list[str] = []
            while position < len(sql):
                if sql[position] == "'":
                    if position + 1 < len(sql) and sql[position + 1] == "'":
                        value.append("'")
                        position += 2
                        continue
                    position += 1
                    break
                value.append(sql[position])
                position += 1
            tokens.append("s:" + "".join(value))
            continue
        if char in {'"', "`", "["}:
            closing = "]" if char == "[" else char
            position += 1
            value = []
            while position < len(sql):
                if sql[position] == closing:
                    if (
                        closing != "]"
                        and position + 1 < len(sql)
                        and sql[position + 1] == closing
                    ):
                        value.append(closing)
                        position += 2
                        continue
                    position += 1
                    break
                value.append(sql[position])
                position += 1
            tokens.append("i:" + "".join(value).casefold())
            continue
        if char.isalnum() or char in {"_", "$"}:
            start = position
            position += 1
            while position < len(sql) and (
                sql[position].isalnum() or sql[position] in {"_", "$"}
            ):
                position += 1
            tokens.append("i:" + sql[start:position].casefold())
            continue
        tokens.append("p:" + char)
        position += 1
    return tuple(tokens)


def _fk_signature_from_match(match: re.Match[str]) -> tuple[object, ...]:
    tail = match.group("tail") or ""
    name = match.group("name")
    return (
        None if name is None else _unquote_identifier(name),
        _identifier_list(match.group("local")),
        _unquote_identifier(match.group("table")),
        _identifier_list(match.group("remote")),
        _action_from_tail(tail, "UPDATE"),
        _action_from_tail(tail, "DELETE"),
        _match_from_tail(tail),
    )


def _table_ddl_semantics(
    table_sql: str,
) -> tuple[tuple[tuple[tuple[str, ...], tuple[object, ...] | None], ...], tuple[str, ...]]:
    structural_sql = _sql_structure_without_comments_or_strings(table_sql)
    semantic_sql = _sql_semantics_without_comments(table_sql)
    structural_terms = _top_level_table_terms(structural_sql)
    semantic_terms = _top_level_table_terms(semantic_sql)
    if len(structural_terms) != len(semantic_terms):
        raise RuntimeError("无法稳定解析 SQLite CREATE TABLE 顶层语义")
    terms: list[tuple[tuple[str, ...], tuple[object, ...] | None]] = []
    for structural_term, semantic_term in zip(
        structural_terms,
        semantic_terms,
        strict=True,
    ):
        match = _FK_CLAUSE.fullmatch(structural_term)
        signature = None if match is None else _fk_signature_from_match(match)
        terms.append((_semantic_sql_tokens(semantic_term), signature))
    closing = semantic_sql.rfind(")")
    table_options = (
        () if closing < 0 else _semantic_sql_tokens(semantic_sql[closing + 1 :])
    )
    return tuple(terms), table_options


def _named_fk_clauses(table_sql: str) -> list[tuple[object, ...]]:
    clauses: list[tuple[object, ...]] = []
    structural_sql = _sql_structure_without_comments_or_strings(table_sql)
    for term in _top_level_table_terms(structural_sql):
        match = _FK_CLAUSE.fullmatch(term)
        if match is None:
            continue
        clauses.append(_fk_signature_from_match(match))
    return clauses


def _schema_snapshot(connection: Any) -> dict[str, dict[str, object]]:
    snapshot: dict[str, dict[str, object]] = {}
    for table_name in sorted(_business_tables(connection)):
        escaped_table = table_name.replace('"', '""')
        columns = tuple(
            (
                str(row[1]),
                str(row[2]),
                int(row[3]),
                None if row[4] is None else str(row[4]),
                int(row[5]),
                int(row[6]),
            )
            for row in connection.execute(
                f'PRAGMA table_xinfo("{escaped_table}")'
            ).fetchall()
        )
        indexes = []
        for row in connection.execute(
            f'PRAGMA index_list("{escaped_table}")'
        ).fetchall():
            index_name = str(row[1])
            escaped_index = index_name.replace('"', '""')
            key_terms = tuple(
                (
                    int(index_row[0]),
                    int(index_row[1]),
                    None if index_row[2] is None else str(index_row[2]),
                    int(index_row[3]),
                    None if index_row[4] is None else str(index_row[4]).casefold(),
                )
                for index_row in connection.execute(
                    f'PRAGMA index_xinfo("{escaped_index}")'
                ).fetchall()
                if int(index_row[5]) == 1
            )
            index_sql_row = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='index' AND name=?",
                (index_name,),
            ).fetchone()
            index_sql = (
                None
                if index_sql_row is None or index_sql_row[0] is None
                else str(index_sql_row[0])
            )
            key_expression, predicate = _index_definition_parts(index_sql)
            indexes.append(
                (
                    None if index_name.startswith("sqlite_autoindex_") else index_name,
                    int(row[2]),
                    str(row[3]),
                    int(row[4]),
                    key_terms,
                    key_expression,
                    predicate,
                )
            )

        sql_row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        table_sql = "" if sql_row is None or sql_row[0] is None else str(sql_row[0])
        table_terms, table_options = _table_ddl_semantics(table_sql)
        parsed_clauses = _named_fk_clauses(table_sql)
        grouped: dict[int, list[tuple[object, ...]]] = {}
        for row in connection.execute(
            f'PRAGMA foreign_key_list("{escaped_table}")'
        ).fetchall():
            grouped.setdefault(int(row[0]), []).append(row)
        used_clause_indexes: set[int] = set()
        foreign_keys = []
        for rows in grouped.values():
            ordered = sorted(rows, key=lambda item: int(item[1]))
            pragma_semantics = (
                tuple(str(item[3]) for item in ordered),
                str(ordered[0][2]),
                tuple(str(item[4]) for item in ordered),
                str(ordered[0][5]).upper(),
                str(ordered[0][6]).upper(),
                str(ordered[0][7]).upper(),
            )
            matched_name: str | None = None
            for clause_index, clause in enumerate(parsed_clauses):
                if clause_index in used_clause_indexes:
                    continue
                if clause[1:] == pragma_semantics:
                    matched_name = None if clause[0] is None else str(clause[0])
                    used_clause_indexes.add(clause_index)
                    break
            foreign_keys.append((matched_name, *pragma_semantics))

        snapshot[table_name] = {
            "columns": columns,
            "indexes": tuple(sorted(indexes, key=repr)),
            "foreign_keys": tuple(sorted(foreign_keys, key=repr)),
            "table_terms": table_terms,
            "table_options": table_options,
        }
    return snapshot


@lru_cache(maxsize=1)
def _expected_legacy_snapshot() -> dict[str, dict[str, object]]:
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(FROZEN_SCHEMA_SQL)
        return _schema_snapshot(connection)


EXACT_PIPELINE_FK = (
    PIPELINE_FK_NAME,
    ("pipeline_id",),
    "production_pipelines",
    ("id",),
    "NO ACTION",
    "NO ACTION",
    "NONE",
)


def _is_pipeline_fk_candidate(foreign_key: tuple[object, ...]) -> bool:
    return foreign_key[1] == ("pipeline_id",)


def alembic_version_blockers(connection: Any) -> list[str]:
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='alembic_version'"
    ).fetchone()
    if table_exists is None:
        return []
    actual = tuple(
        (
            int(row[0]),
            str(row[1]),
            str(row[2]).upper(),
            int(row[3]),
            None if row[4] is None else str(row[4]),
            int(row[5]),
            int(row[6]),
        )
        for row in connection.execute("PRAGMA table_xinfo(alembic_version)").fetchall()
    )
    expected = ((0, "version_num", "VARCHAR(32)", 1, None, 1, 0),)
    if actual != expected:
        return [
            "alembic_version 表结构无效；必须仅包含 "
            "Alembic 1.18.5 固定的 version_num VARCHAR(32) NOT NULL 主键结构"
        ]
    sql_row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='alembic_version'"
    ).fetchone()
    actual_sql = "" if sql_row is None or sql_row[0] is None else str(sql_row[0])
    if _table_ddl_semantics(actual_sql) != _table_ddl_semantics(
        PINNED_ALEMBIC_VERSION_SQL
    ):
        return [
            "alembic_version DDL 语义不是 Alembic 1.18.5 固定结构；"
            "拒绝 COLLATE、ON CONFLICT、WITHOUT ROWID 或其他额外语义"
        ]
    indexes = []
    for row in connection.execute("PRAGMA index_list(alembic_version)").fetchall():
        index_name = str(row[1])
        escaped_index = index_name.replace('"', '""')
        key_terms = tuple(
            (
                int(index_row[0]),
                int(index_row[1]),
                None if index_row[2] is None else str(index_row[2]),
                int(index_row[3]),
                None if index_row[4] is None else str(index_row[4]).casefold(),
            )
            for index_row in connection.execute(
                f'PRAGMA index_xinfo("{escaped_index}")'
            ).fetchall()
            if int(index_row[5]) == 1
        )
        indexes.append(
            (
                None if index_name.startswith("sqlite_autoindex_") else index_name,
                int(row[2]),
                str(row[3]),
                int(row[4]),
                key_terms,
            )
        )
    expected_indexes = (
        (None, 1, "pk", 0, ((0, 0, "version_num", 0, "binary"),)),
    )
    if tuple(sorted(indexes, key=repr)) != expected_indexes:
        return ["alembic_version 索引结构不是固定的唯一主键 autoindex"]
    return []


def legacy_schema_blockers(
    connection: Any,
    *,
    require_pipeline_fk: bool | None,
) -> list[str]:
    actual_tables = _business_tables(connection)
    missing = sorted(LEGACY_TABLES - actual_tables)
    extra = sorted(actual_tables - LEGACY_TABLES)
    blockers: list[str] = []
    if missing:
        blockers.append("残缺 legacy schema，缺少表：" + ", ".join(missing))
    if extra:
        blockers.append("未知 business schema，额外表：" + ", ".join(extra))
    unexpected_objects = [
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT type, name FROM sqlite_schema "
            "WHERE type IN ('view', 'trigger') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
    ]
    if unexpected_objects:
        blockers.append(
            "冻结 legacy schema 不允许额外 view/trigger 对象："
            + ", ".join(f"{object_type}:{name}" for object_type, name in unexpected_objects)
        )
    if blockers:
        return blockers

    actual = _schema_snapshot(connection)
    normalized = copy.deepcopy(actual)
    video_posts = normalized["video_posts"]
    pipeline_fks = tuple(
        foreign_key
        for foreign_key in video_posts["foreign_keys"]
        if _is_pipeline_fk_candidate(foreign_key)
    )
    exact_pipeline_fks = tuple(
        foreign_key for foreign_key in pipeline_fks if foreign_key == EXACT_PIPELINE_FK
    )
    if pipeline_fks and (
        len(pipeline_fks) != 1 or len(exact_pipeline_fks) != 1
    ):
        blockers.append(
            "video_posts.pipeline_id 外键必须且只能是精确签名："
            f"{PIPELINE_FK_NAME}, NO ACTION/NO ACTION, MATCH NONE"
        )
    has_pipeline_fk = len(pipeline_fks) == 1 and len(exact_pipeline_fks) == 1
    if require_pipeline_fk is True and not has_pipeline_fk:
        blockers.append("head schema 缺少精确命名的 video_posts.pipeline_id 外键")
    if require_pipeline_fk is False and pipeline_fks:
        blockers.append("base schema 不应包含 video_posts.pipeline_id 外键")

    video_posts["foreign_keys"] = tuple(
        foreign_key
        for foreign_key in video_posts["foreign_keys"]
        if foreign_key != EXACT_PIPELINE_FK
    )
    normalized_terms = list(video_posts["table_terms"])
    for term_index, (_tokens, signature) in enumerate(normalized_terms):
        if signature == EXACT_PIPELINE_FK:
            normalized_terms.pop(term_index)
            break
    video_posts["table_terms"] = tuple(normalized_terms)
    expected = _expected_legacy_snapshot()
    for table_name in sorted(LEGACY_TABLES):
        if normalized[table_name] != expected[table_name]:
            blockers.append(f"{table_name} 的列、索引或既有外键不符合冻结 legacy schema")

    if not blockers:
        orphan = connection.execute(
            "SELECT vp.id, vp.pipeline_id FROM video_posts AS vp "
            "LEFT JOIN production_pipelines AS pp ON pp.id = vp.pipeline_id "
            "WHERE vp.pipeline_id IS NOT NULL AND pp.id IS NULL LIMIT 1"
        ).fetchone()
        if orphan is not None:
            blockers.append(
                "发现孤儿 video_posts.pipeline_id："
                f"video_post_id={orphan[0]}, pipeline_id={orphan[1]}"
            )
    return blockers


def _raw_connection():
    proxied = op.get_bind().connection
    return getattr(proxied, "driver_connection", proxied)


def _add_pipeline_fk() -> None:
    with op.batch_alter_table("video_posts", recreate="always") as batch_op:
        batch_op.create_foreign_key(
            PIPELINE_FK_NAME,
            "production_pipelines",
            ["pipeline_id"],
            ["id"],
        )


def _drop_pipeline_fk() -> None:
    with op.batch_alter_table("video_posts", recreate="always") as batch_op:
        batch_op.drop_constraint(PIPELINE_FK_NAME, type_="foreignkey")


def upgrade() -> None:
    connection = _raw_connection()
    if not _business_tables(connection):
        for statement in _iter_schema_statements():
            op.execute(statement)
    blockers = legacy_schema_blockers(
        connection,
        require_pipeline_fk=None,
    )
    if blockers:
        raise RuntimeError("legacy baseline 迁移已阻断：" + "；".join(blockers))

    current = _schema_snapshot(connection)["video_posts"]
    has_pipeline_fk = EXACT_PIPELINE_FK in current["foreign_keys"]
    if not has_pipeline_fk:
        _add_pipeline_fk()
    head_blockers = legacy_schema_blockers(
        connection,
        require_pipeline_fk=True,
    )
    if head_blockers:
        raise RuntimeError(
            "legacy baseline 添加外键后的 head 校验失败："
            + "；".join(head_blockers)
        )


def downgrade() -> None:
    connection = _raw_connection()
    blockers = legacy_schema_blockers(
        connection,
        require_pipeline_fk=True,
    )
    if blockers:
        raise RuntimeError("legacy baseline 降级已阻断：" + "；".join(blockers))
    _drop_pipeline_fk()
    base_blockers = legacy_schema_blockers(
        connection,
        require_pipeline_fk=False,
    )
    if base_blockers:
        raise RuntimeError(
            "legacy baseline 删除外键后的 base 校验失败："
            + "；".join(base_blockers)
        )
