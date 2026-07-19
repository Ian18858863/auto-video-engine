from __future__ import annotations

import importlib
import importlib.util
import os
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command as alembic_command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.database import Base
from app.core.schema_migrations import _config, inspect_schema_status, upgrade_fresh_database
from app.models import HotItem, ProductionPipeline


FROZEN_LEGACY_SQL = Path(__file__).parent / "fixtures" / "legacy_schema.sql"


def _identity_module():
    spec = importlib.util.find_spec("app.pipeline.project_identity")
    assert spec is not None, "Task 4 project identity module is missing"
    return importlib.import_module("app.pipeline.project_identity")


def _pipeline(
    pipeline_id: int,
    title: str,
    *,
    project_key: str | None = None,
    legacy_project_dir: str | None = None,
):
    return SimpleNamespace(
        id=pipeline_id,
        project_name=title,
        project_key=project_key,
        legacy_project_dir=legacy_project_dir,
    )


def test_same_title_different_ids_produce_distinct_portable_keys() -> None:
    identity = _identity_module()

    first = identity.build_project_key(41, "同一个标题")
    second = identity.build_project_key(42, "同一个标题")

    assert first != second
    assert re.fullmatch(r"[a-z0-9-]+", first)
    assert re.fullmatch(r"[a-z0-9-]+", second)
    assert len(first) <= identity.MAX_PROJECT_KEY_LENGTH
    assert len(second) <= identity.MAX_PROJECT_KEY_LENGTH


@pytest.mark.parametrize(
    "title",
    ["", "   ", "中文标题", "CON", "nul.txt", "a" * 1000, "../bad\\name"],
)
def test_edge_titles_produce_bounded_portable_keys(title: str) -> None:
    identity = _identity_module()

    key = identity.build_project_key(7, title)

    assert re.fullmatch(r"[a-z0-9-]+", key)
    assert len(key) <= identity.MAX_PROJECT_KEY_LENGTH
    assert not key.endswith((".", " "))
    assert "/" not in key and "\\" not in key


def test_existing_key_survives_title_rename_but_mismatch_is_rejected() -> None:
    identity = _identity_module()
    pipeline = _pipeline(9, "旧标题")

    assigned = identity.ensure_project_key(pipeline)
    pipeline.project_name = "完全不同的新标题"

    assert identity.ensure_project_key(pipeline) == assigned
    assert pipeline.project_key == assigned

    pipeline.project_key = identity.build_project_key(10, "别的项目")
    with pytest.raises(identity.ProjectIdentityError, match="mismatch|不匹配"):
        identity.ensure_project_key(pipeline)


def test_project_key_assignment_is_deterministic_across_fresh_objects() -> None:
    identity = _identity_module()

    first = _pipeline(88, "Restart Stable Title")
    second = _pipeline(88, "Restart Stable Title")

    assert identity.ensure_project_key(first) == identity.ensure_project_key(second)


def test_new_pipeline_uses_provisional_key_only_until_flush_assigns_id(
    tmp_path: Path,
) -> None:
    identity = _identity_module()
    database = tmp_path / "new-pipeline.db"
    upgrade_fresh_database(database)
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        with Session(engine) as session:
            hot_item = HotItem(
                source_platform="manual",
                source_type="manual",
                title="Flush identity",
                url="https://example.com/flush-identity",
            )
            session.add(hot_item)
            session.flush()
            pipeline = ProductionPipeline(
                hotspot_id=hot_item.id,
                project_name="Flush identity",
                status="进行中",
            )

            key = identity.add_pipeline_with_identity(session, pipeline)

            assert pipeline.id is not None
            assert key == identity.build_project_key(pipeline.id, pipeline.project_name)
            assert not key.startswith("pending-")
            session.commit()
            assert session.get(ProductionPipeline, pipeline.id).project_key == key
    finally:
        engine.dispose()


def test_persisted_project_key_cannot_change_even_to_same_id_slug(tmp_path: Path) -> None:
    identity = _identity_module()
    database = tmp_path / "immutable-key.db"
    upgrade_fresh_database(database)
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        with Session(engine) as session:
            hot_item = HotItem(
                source_platform="manual",
                source_type="manual",
                title="Immutable identity",
                url="https://example.com/immutable-identity",
            )
            session.add(hot_item)
            session.flush()
            pipeline = ProductionPipeline(
                hotspot_id=hot_item.id,
                project_name="Immutable identity",
                status="进行中",
            )
            identity.add_pipeline_with_identity(session, pipeline)
            session.commit()
            original = pipeline.project_key

            with pytest.raises(identity.ProjectIdentityError, match="immutable|不可变"):
                pipeline.project_key = f"p-{pipeline.id}-tampered"
                session.flush()

            session.rollback()
            assert session.get(ProductionPipeline, pipeline.id).project_key == original
    finally:
        engine.dispose()


def test_manifest_preparation_is_unique_idempotent_and_preserves_shared_legacy(
    tmp_path: Path,
) -> None:
    identity = _identity_module()
    shared_legacy = tmp_path / "shared-legacy"
    shared_legacy.mkdir()
    sentinel = shared_legacy / "keep.txt"
    sentinel.write_text("legacy", encoding="utf-8")
    project_root = tmp_path / "new-projects"
    project_root.mkdir()
    manifest_path = tmp_path / "project-directory-plan.json"
    pipelines = [
        _pipeline(
            1,
            "同名项目",
            project_key=identity.build_project_key(1, "同名项目"),
            legacy_project_dir=str(shared_legacy),
        ),
        _pipeline(
            2,
            "同名项目",
            project_key=identity.build_project_key(2, "同名项目"),
            legacy_project_dir=str(shared_legacy),
        ),
    ]

    manifest, seal = identity.write_project_directory_manifest(
        pipelines,
        project_root=project_root,
        manifest_path=manifest_path,
        system_drive="C:",
    )

    assert not list(project_root.iterdir())
    assert [entry["legacy_project_dir"] for entry in manifest["projects"]] == [
        str(shared_legacy),
        str(shared_legacy),
    ]
    prepared = identity.prepare_project_directories(
        manifest_path,
        expected_sha256=seal,
        system_drive="C:",
    )
    assert len(set(prepared)) == 2
    assert all(Path(path).is_dir() for path in prepared)
    assert identity.prepare_project_directories(
        manifest_path,
        expected_sha256=seal,
        system_drive="C:",
    ) == prepared
    assert sentinel.read_text(encoding="utf-8") == "legacy"


def test_manifest_collision_or_reference_tampering_fails_closed(tmp_path: Path) -> None:
    identity = _identity_module()
    project_root = tmp_path / "new-projects"
    project_root.mkdir()
    manifest_path = tmp_path / "project-directory-plan.json"
    pipeline = _pipeline(
        3,
        "安全项目",
        project_key=identity.build_project_key(3, "安全项目"),
    )
    _, seal = identity.write_project_directory_manifest(
        [pipeline],
        project_root=project_root,
        manifest_path=manifest_path,
        system_drive="C:",
    )
    [prepared] = identity.prepare_project_directories(
        manifest_path,
        expected_sha256=seal,
        system_drive="C:",
    )
    reference = Path(prepared) / identity.PROJECT_REFERENCE_FILENAME
    reference.write_text(json.dumps({"project_key": "tampered"}), encoding="utf-8")

    with pytest.raises(identity.ProjectIdentityError, match="tamper|collision|篡改|冲突"):
        identity.prepare_project_directories(
            manifest_path,
            expected_sha256=seal,
            system_drive="C:",
        )


def test_system_drive_project_root_is_rejected_before_any_write(tmp_path: Path) -> None:
    identity = _identity_module()
    manifest_path = tmp_path / "project-directory-plan.json"
    pipeline = _pipeline(
        4,
        "系统盘拒绝",
        project_key=identity.build_project_key(4, "系统盘拒绝"),
    )

    with pytest.raises(ValueError, match="系统盘|system"):
        identity.write_project_directory_manifest(
            [pipeline],
            project_root=Path("C:/forbidden-projects"),
            manifest_path=manifest_path,
            system_drive="C:",
        )

    assert not manifest_path.exists()


def test_manifest_preserves_opaque_legacy_directory_values_exactly(tmp_path: Path) -> None:
    identity = _identity_module()
    project_root = tmp_path / "approved-project-root"
    project_root.mkdir()
    manifest_path = tmp_path / "opaque-legacy.json"
    shared = "D:/Shared/Case\\Mixed"
    values = [None, "", "relative\\Case/Path", "E:/Absolute/Case Path", shared, shared]
    pipelines = [
        _pipeline(
            index,
            f"project {index}",
            project_key=identity.build_project_key(index, f"project {index}"),
            legacy_project_dir=value,
        )
        for index, value in enumerate(values, start=1)
    ]

    manifest, _seal = identity.write_project_directory_manifest(
        pipelines,
        project_root=project_root,
        manifest_path=manifest_path,
        system_drive="C:",
    )

    assert [item["legacy_project_dir"] for item in manifest["projects"]] == values


def test_precreated_target_without_reference_fails_closed(tmp_path: Path) -> None:
    identity = _identity_module()
    project_root = tmp_path / "approved-project-root"
    project_root.mkdir()
    manifest_path = tmp_path / "collision.json"
    pipeline = _pipeline(
        31,
        "collision",
        project_key=identity.build_project_key(31, "collision"),
    )
    _, seal = identity.write_project_directory_manifest(
        [pipeline],
        project_root=project_root,
        manifest_path=manifest_path,
        system_drive="C:",
    )
    collision = project_root / pipeline.project_key
    collision.mkdir()
    sentinel = collision / "owned-by-someone-else.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(identity.ProjectIdentityError, match="collision|冲突"):
        identity.prepare_project_directories(
            manifest_path,
            expected_sha256=seal,
            system_drive="C:",
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_manifest_recorded_path_tampering_fails_closed(tmp_path: Path) -> None:
    identity = _identity_module()
    project_root = tmp_path / "approved-project-root"
    project_root.mkdir()
    manifest_path = tmp_path / "tampered-path.json"
    pipeline = _pipeline(
        32,
        "tamper",
        project_key=identity.build_project_key(32, "tamper"),
    )
    _, seal = identity.write_project_directory_manifest(
        [pipeline],
        project_root=project_root,
        manifest_path=manifest_path,
        system_drive="C:",
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["manifest_path"] = str(tmp_path / "different.json")
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(identity.ProjectIdentityError, match="tamper|篡改"):
        identity.prepare_project_directories(
            manifest_path,
            expected_sha256=seal,
            system_drive="C:",
        )

    assert not list(project_root.iterdir())


def _create_0001_database(database: Path) -> None:
    with __import__("sqlite3").connect(database) as connection:
        connection.executescript(FROZEN_LEGACY_SQL.read_text(encoding="utf-8"))
    alembic_command.upgrade(_config(database), "20260711_0001")


def test_sqlite_metadata_fixture_installs_frozen_identity_guards_and_default(
    tmp_path: Path,
) -> None:
    identity = _identity_module()
    database = tmp_path / "metadata-identity.db"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    Base.metadata.create_all(engine)
    script = ScriptDirectory.from_config(_config(database))
    migration = script.get_revision("20260711_0002").module
    baseline = script.get_revision("20260711_0001").module
    try:
        with engine.connect() as connection:
            actual = {
                str(row[0]): str(row[1])
                for row in connection.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_schema "
                    "WHERE type='trigger' AND name LIKE "
                    "'trg_production_pipelines_project_identity_%'"
                ).fetchall()
            }
        assert set(actual) == set(migration._TRIGGER_SQL)
        for trigger_name, expected_sql in migration._TRIGGER_SQL.items():
            assert baseline._semantic_sql_tokens(actual[trigger_name]) == (
                baseline._semantic_sql_tokens(expected_sql)
            )

        with Session(engine) as session:
            hot_item = HotItem(
                source_platform="manual",
                source_type="manual",
                title="Metadata direct insert",
                url="https://example.com/metadata-direct-insert",
            )
            session.add(hot_item)
            session.flush()
            pipeline = ProductionPipeline(
                hotspot_id=hot_item.id,
                project_name="Metadata direct insert",
                status="进行中",
            )
            session.add(pipeline)
            session.commit()
            session.refresh(pipeline)
            assert pipeline.project_key == identity.build_project_key(
                pipeline.id,
                pipeline.project_name,
            )
    finally:
        engine.dispose()


def test_0002_backfills_identity_and_round_trips_legacy_rows_without_filesystem_writes(
    tmp_path: Path,
) -> None:
    identity = _identity_module()
    database = tmp_path / "identity-migration.db"
    _create_0001_database(database)
    shared_legacy = tmp_path / "shared-legacy"
    shared_legacy.mkdir()
    sentinel = shared_legacy / "keep.txt"
    sentinel.write_text("legacy", encoding="utf-8")
    sqlite3 = __import__("sqlite3")
    with sqlite3.connect(database) as connection:
        template = connection.execute(
            "SELECT hotspot_id, topic_idea_id, script_asset_id, status, provider, model, "
            "created_at, updated_at FROM production_pipelines WHERE id=401"
        ).fetchone()
        connection.executemany(
            "INSERT INTO production_pipelines "
            "(id, hotspot_id, topic_idea_id, script_asset_id, project_name, project_dir, "
            "status, provider, model, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (402, *template[:3], "同名项目", "relative\\Case", *template[3:]),
                (403, *template[:3], "同名项目", str(shared_legacy), *template[3:]),
                (404, *template[:3], "绝对路径", "E:/Absolute/Case Path", *template[3:]),
                (405, *template[:3], "共享路径", str(shared_legacy), *template[3:]),
            ],
        )
        connection.commit()
        before_rows = connection.execute(
            "SELECT * FROM production_pipelines ORDER BY id"
        ).fetchall()

    alembic_command.upgrade(_config(database), "20260711_0002")

    with sqlite3.connect(database) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        migrated = connection.execute(
            "SELECT id, project_name, project_dir, project_key, legacy_project_dir "
            "FROM production_pipelines ORDER BY id"
        ).fetchall()
    assert revision == "20260711_0002"
    assert len({row[3] for row in migrated}) == len(migrated)
    assert all(row[3] == identity.build_project_key(row[0], row[1]) for row in migrated)
    assert all(row[2] == row[4] for row in migrated)
    assert sentinel.read_text(encoding="utf-8") == "legacy"

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE production_pipelines SET project_dir=? WHERE id=404",
            ("D:/Approved/p-404-absolute",),
        )
        connection.commit()

    alembic_command.downgrade(_config(database), "20260711_0001")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT * FROM production_pipelines ORDER BY id"
        ).fetchall() == before_rows
    assert sentinel.read_text(encoding="utf-8") == "legacy"

    alembic_command.upgrade(_config(database), "20260711_0002")
    with sqlite3.connect(database) as connection:
        reupgraded = connection.execute(
            "SELECT id, project_key FROM production_pipelines ORDER BY id"
        ).fetchall()
    assert reupgraded == [(row[0], identity.build_project_key(row[0], row[1])) for row in migrated]


def test_0002_downgrade_keeps_current_directory_for_new_row_without_legacy_value(
    tmp_path: Path,
) -> None:
    database = tmp_path / "new-row-downgrade.db"
    _create_0001_database(database)
    alembic_command.upgrade(_config(database), "head")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        with Session(engine) as session:
            hot_item = HotItem(
                source_platform="manual",
                source_type="manual",
                title="Post migration row",
                url="https://example.com/post-migration-row",
            )
            session.add(hot_item)
            session.flush()
            pipeline = ProductionPipeline(
                hotspot_id=hot_item.id,
                project_name="Post migration row",
                project_dir="D:/Approved/p-new-row",
                status="进行中",
            )
            session.add(pipeline)
            session.commit()
            pipeline_id = pipeline.id
            assert pipeline.legacy_project_dir is None
    finally:
        engine.dispose()

    alembic_command.downgrade(_config(database), "20260711_0001")

    sqlite3 = __import__("sqlite3")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT project_dir FROM production_pipelines WHERE id=?",
            (pipeline_id,),
        ).fetchone() == ("D:/Approved/p-new-row",)


def test_schema_status_is_revision_aware_for_0001_0003_and_fresh_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "status-chain.db"
    _create_0001_database(database)

    behind = inspect_schema_status(database)
    assert behind.state == "behind"
    assert behind.current_revision == "20260711_0001"
    assert behind.head_revision == "20260711_0003"

    alembic_command.upgrade(_config(database), "head")
    ready = inspect_schema_status(database)
    assert ready.state == "ready"
    assert ready.current_revision == "20260711_0003"

    fresh = tmp_path / "fresh.db"
    upgrade_fresh_database(fresh)
    assert inspect_schema_status(fresh).current_revision == "20260711_0003"


def test_stamped_0002_without_identity_contract_is_invalid(tmp_path: Path) -> None:
    database = tmp_path / "stamped-without-identity.db"
    _create_0001_database(database)
    sqlite3 = __import__("sqlite3")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE alembic_version SET version_num='20260711_0002'"
        )
        connection.commit()

    status = inspect_schema_status(database)
    assert status.state == "invalid"
    assert status.current_revision == "20260711_0002"
    assert any("identity" in blocker or "project_key" in blocker for blocker in status.blockers)


def test_0002_postvalidation_rolls_back_if_required_trigger_is_not_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "postvalidation-trigger-fault.db"
    _create_0001_database(database)
    sqlite3 = __import__("sqlite3")
    with sqlite3.connect(database) as connection:
        before_schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name"
        ).fetchall()
        before_rows = connection.execute(
            "SELECT * FROM production_pipelines ORDER BY id"
        ).fetchall()

    sqlalchemy = __import__("sqlalchemy")
    real_create_engine = sqlalchemy.create_engine
    intercepted = False

    def instrumented_create_engine(*args, **kwargs):
        engine = real_create_engine(*args, **kwargs)

        @sqlalchemy.event.listens_for(engine, "before_cursor_execute", retval=True)
        def skip_canonicalize_trigger(
            _connection,
            _cursor,
            statement,
            parameters,
            _context,
            _executemany,
        ):
            nonlocal intercepted
            normalized = " ".join(str(statement).casefold().split())
            if normalized.startswith(
                "create trigger "
                "trg_production_pipelines_project_identity_canonicalize"
            ):
                intercepted = True
                return "SELECT 1", parameters
            return statement, parameters

        return engine

    monkeypatch.setattr(sqlalchemy, "create_engine", instrumented_create_engine)
    with pytest.raises(RuntimeError, match="post-migration|trigger|schema"):
        alembic_command.upgrade(_config(database), "head")

    assert intercepted is True
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == "20260711_0001"
        assert connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name"
        ).fetchall() == before_schema
        assert connection.execute(
            "SELECT * FROM production_pipelines ORDER BY id"
        ).fetchall() == before_rows
    assert inspect_schema_status(database).state == "behind"


def test_0002_downgrade_postvalidation_failure_rolls_back_to_exact_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "downgrade-postvalidation-fault.db"
    upgrade_fresh_database(database)
    sqlite3 = __import__("sqlite3")
    with sqlite3.connect(database) as connection:
        before_schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name"
        ).fetchall()
        before_rows = connection.execute(
            "SELECT * FROM production_pipelines ORDER BY id"
        ).fetchall()

    sqlalchemy = __import__("sqlalchemy")
    real_create_engine = sqlalchemy.create_engine
    intercepted = False

    def instrumented_create_engine(*args, **kwargs):
        engine = real_create_engine(*args, **kwargs)

        @sqlalchemy.event.listens_for(engine, "before_cursor_execute", retval=True)
        def skip_required_legacy_index(
            _connection,
            _cursor,
            statement,
            parameters,
            _context,
            _executemany,
        ):
            nonlocal intercepted
            normalized = " ".join(str(statement).casefold().split())
            if normalized.startswith(
                "create index ix_production_pipelines_status "
            ):
                intercepted = True
                return "SELECT 1", parameters
            return statement, parameters

        return engine

    monkeypatch.setattr(sqlalchemy, "create_engine", instrumented_create_engine)
    with pytest.raises(RuntimeError, match="post-migration|schema|index"):
        alembic_command.downgrade(_config(database), "20260711_0001")

    assert intercepted is True
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == "20260711_0003"
        assert connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name"
        ).fetchall() == before_schema
        assert connection.execute(
            "SELECT * FROM production_pipelines ORDER BY id"
        ).fetchall() == before_rows
    assert inspect_schema_status(database).state == "ready"


def test_migrated_database_canonicalizes_pending_identity_before_commit_and_blocks_raw_mutation(
    tmp_path: Path,
) -> None:
    identity = _identity_module()
    database = tmp_path / "database-identity-guard.db"
    upgrade_fresh_database(database)
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        with Session(engine) as session:
            hot_item = HotItem(
                source_platform="manual",
                source_type="manual",
                title="Database identity guard",
                url="https://example.com/database-identity-guard",
            )
            session.add(hot_item)
            session.flush()
            pipeline = ProductionPipeline(
                hotspot_id=hot_item.id,
                project_name="Database identity guard",
                project_key="pending-" + "a" * 32 + "-database-identity-guard",
                status="进行中",
            )
            session.add(pipeline)
            session.commit()
            session.refresh(pipeline)

            assert pipeline.project_key == identity.build_project_key(
                pipeline.id,
                pipeline.project_name,
            )
            assert not pipeline.project_key.startswith("pending-")

            with pytest.raises(DBAPIError, match="immutable"):
                session.execute(
                    text(
                        "UPDATE production_pipelines SET project_key=:project_key "
                        "WHERE id=:pipeline_id"
                    ),
                    {
                        "project_key": f"p-{pipeline.id}-raw-alternate",
                        "pipeline_id": pipeline.id,
                    },
                )
            session.rollback()

            with pytest.raises(DBAPIError, match="immutable"):
                session.execute(
                    text(
                        "UPDATE production_pipelines SET legacy_project_dir='tampered' "
                        "WHERE id=:pipeline_id"
                    ),
                    {"pipeline_id": pipeline.id},
                )
            session.rollback()

            with pytest.raises(DBAPIError, match="replacement|immutable|identity"):
                session.execute(
                    text(
                        "INSERT OR REPLACE INTO production_pipelines "
                        "(id, hotspot_id, topic_idea_id, script_asset_id, project_name, "
                        "project_dir, status, provider, model, created_at, updated_at, "
                        "project_key, legacy_project_dir) "
                        "SELECT id, hotspot_id, topic_idea_id, script_asset_id, "
                        "'Replacement title', project_dir, status, provider, model, "
                        "created_at, updated_at, :pending_key, legacy_project_dir "
                        "FROM production_pipelines WHERE id=:pipeline_id"
                    ),
                    {
                        "pending_key": "pending-" + "c" * 32 + "-replacement-title",
                        "pipeline_id": pipeline.id,
                    },
                )
            session.rollback()
            session.refresh(pipeline)
            assert pipeline.project_key == identity.build_project_key(
                pipeline.id,
                "Database identity guard",
            )

    finally:
        engine.dispose()


def test_schema_validator_rejects_noncanonical_oversized_project_slug(
    tmp_path: Path,
) -> None:
    database = tmp_path / "oversized-project-key.db"
    _create_0001_database(database)
    alembic_command.upgrade(_config(database), "head")
    script = ScriptDirectory.from_config(_config(database))
    migration = script.get_revision("20260711_0002").module
    sqlite3 = __import__("sqlite3")
    with sqlite3.connect(database) as connection:
        for trigger_name in migration._TRIGGER_SQL:
            connection.execute(f"DROP TRIGGER {trigger_name}")
        connection.execute(
            "UPDATE production_pipelines SET project_key=? WHERE id=401",
            ("p-401-" + "a" * 60,),
        )
        for statement in migration._TRIGGER_SQL.values():
            connection.execute(statement)
        connection.commit()

    status = inspect_schema_status(database)

    assert status.state == "invalid"
    assert any("project_key" in blocker or "identity" in blocker for blocker in status.blockers)


def test_runtime_helper_uses_database_canonicalization_on_migrated_schema(
    tmp_path: Path,
) -> None:
    identity = _identity_module()
    database = tmp_path / "helper-database-identity.db"
    upgrade_fresh_database(database)
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        with Session(engine) as session:
            hot_item = HotItem(
                source_platform="manual",
                source_type="manual",
                title="Helper database identity",
                url="https://example.com/helper-database-identity",
            )
            session.add(hot_item)
            session.flush()
            pipeline = ProductionPipeline(
                hotspot_id=hot_item.id,
                project_name="Helper database identity",
                status="进行中",
            )

            key = identity.add_pipeline_with_identity(session, pipeline)

            assert key == identity.build_project_key(pipeline.id, pipeline.project_name)
            assert pipeline.project_key == key
            session.commit()
    finally:
        engine.dispose()


def test_pending_slug_must_match_frozen_title_slug_and_raw_connections_fail_closed(
    tmp_path: Path,
) -> None:
    identity = _identity_module()
    database = tmp_path / "pending-slug-guard.db"
    upgrade_fresh_database(database)
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        with Session(engine) as session:
            hot_item = HotItem(
                source_platform="manual",
                source_type="manual",
                title="Expected title slug",
                url="https://example.com/expected-title-slug",
            )
            session.add(hot_item)
            session.flush()
            attacker = ProductionPipeline(
                hotspot_id=hot_item.id,
                project_name="Expected title slug",
                project_key="pending-" + "b" * 32 + "-attacker-controlled",
                status="进行中",
            )
            session.add(attacker)
            with pytest.raises(DBAPIError, match="pending project identity"):
                session.flush()
            session.rollback()

        sqlite3 = __import__("sqlite3")
        with sqlite3.connect(database) as connection:
            with pytest.raises(sqlite3.OperationalError, match="project_slug_v1"):
                connection.execute(
                    "INSERT INTO production_pipelines "
                    "(hotspot_id, project_name, project_dir, project_key, status, "
                    "provider, model, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        1,
                        "Expected title slug",
                        "",
                        "pending-" + "c" * 32 + "-expected-title-slug",
                        "进行中",
                        "fake",
                        "fake-low-cost",
                        "2026-07-12 00:00:00",
                        "2026-07-12 00:00:00",
                    ),
                )
    finally:
        engine.dispose()
    assert inspect_schema_status(database).state == "ready"


def test_missing_identity_table_is_invalid_instead_of_crashing_validator(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing-identity-table.db"
    upgrade_fresh_database(database)
    sqlite3 = __import__("sqlite3")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE production_pipelines")
        connection.commit()

    status = inspect_schema_status(database)
    assert status.state == "invalid"
    assert status.blockers


@pytest.mark.parametrize("tamper", ["trigger", "rich-index"])
def test_0002_validator_rejects_trigger_and_rich_index_semantic_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    database = tmp_path / f"identity-schema-{tamper}.db"
    upgrade_fresh_database(database)
    sqlite3 = __import__("sqlite3")
    with sqlite3.connect(database) as connection:
        if tamper == "trigger":
            connection.execute(
                "DROP TRIGGER trg_production_pipelines_project_identity_canonicalize"
            )
            connection.execute(
                "CREATE TRIGGER trg_production_pipelines_project_identity_canonicalize "
                "AFTER INSERT ON production_pipelines BEGIN SELECT 1; END"
            )
        else:
            connection.execute("DROP INDEX ix_production_pipelines_project_key")
            connection.execute(
                "CREATE INDEX ix_production_pipelines_project_key "
                "ON production_pipelines(project_key COLLATE NOCASE DESC) "
                "WHERE project_key <> ''"
            )
        connection.commit()

    assert inspect_schema_status(database).state == "invalid"


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        (
            "project_key VARCHAR(80) NOT NULL",
            "project_key VARCHAR(80) COLLATE NOCASE NOT NULL",
        ),
        (
            "UNIQUE (project_key)",
            "UNIQUE (project_key) ON CONFLICT IGNORE",
        ),
    ],
)
def test_0002_validator_rejects_added_identity_table_term_semantics(
    tmp_path: Path,
    original: str,
    replacement: str,
) -> None:
    database = tmp_path / "identity-table-term-tamper.db"
    upgrade_fresh_database(database)
    sqlite3 = __import__("sqlite3")
    with sqlite3.connect(database) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type='table' AND name='production_pipelines'"
        ).fetchone()[0]
        assert original in table_sql
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_schema SET sql=? WHERE type='table' AND name=?",
            (table_sql.replace(original, replacement), "production_pipelines"),
        )
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version={schema_version + 1}")
        connection.commit()

    assert inspect_schema_status(database).state == "invalid"


def test_manifest_requires_external_sha256_seal_and_preexisting_root(
    tmp_path: Path,
) -> None:
    identity = _identity_module()
    missing_root = tmp_path / "missing-approved-root"
    manifest_path = tmp_path / "sealed-plan.json"
    pipeline = _pipeline(
        51,
        "sealed plan",
        project_key=identity.build_project_key(51, "sealed plan"),
    )

    with pytest.raises((ValueError, identity.ProjectIdentityError), match="root|存在"):
        identity.write_project_directory_manifest(
            [pipeline],
            project_root=missing_root,
            manifest_path=manifest_path,
            system_drive="C:",
        )
    assert not missing_root.exists()
    assert not manifest_path.exists()

    project_root = tmp_path / "approved-root"
    project_root.mkdir()
    payload, seal = identity.write_project_directory_manifest(
        [pipeline],
        project_root=project_root,
        manifest_path=manifest_path,
        system_drive="C:",
    )
    assert re.fullmatch(r"[0-9a-f]{64}", seal)
    assert "sha256" not in payload

    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
    with pytest.raises(identity.ProjectIdentityError, match="seal|SHA|tamper"):
        identity.prepare_project_directories(
            manifest_path,
            expected_sha256=seal,
            system_drive="C:",
        )
    assert not list(project_root.iterdir())


def test_directory_preparation_cleans_only_current_call_after_marker_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity_module()
    project_root = tmp_path / "approved-root"
    project_root.mkdir()
    manifest_path = tmp_path / "interrupted-plan.json"
    pipelines = [
        _pipeline(
            pipeline_id,
            f"project {pipeline_id}",
            project_key=identity.build_project_key(
                pipeline_id,
                f"project {pipeline_id}",
            ),
        )
        for pipeline_id in (61, 62)
    ]
    _, seal = identity.write_project_directory_manifest(
        pipelines,
        project_root=project_root,
        manifest_path=manifest_path,
        system_drive="C:",
    )
    original_writer = identity._write_exclusive_file
    writes = 0

    def interrupted_writer(path: Path, content: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected marker interruption")
        original_writer(path, content)

    monkeypatch.setattr(identity, "_write_exclusive_file", interrupted_writer)
    with pytest.raises(identity.ProjectIdentityError, match="prepare|marker|directory"):
        identity.prepare_project_directories(
            manifest_path,
            expected_sha256=seal,
            system_drive="C:",
        )

    assert not list(project_root.iterdir())


def test_project_root_target_and_reference_reparse_points_are_rejected(
    tmp_path: Path,
) -> None:
    identity = _identity_module()
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    try:
        os.symlink(real_root, linked_root, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    pipeline = _pipeline(
        71,
        "reparse",
        project_key=identity.build_project_key(71, "reparse"),
    )

    with pytest.raises(identity.ProjectIdentityError, match="reparse|symlink|junction"):
        identity.write_project_directory_manifest(
            [pipeline],
            project_root=linked_root,
            manifest_path=tmp_path / "reparse-plan.json",
            system_drive="C:",
        )


def test_project_root_rejects_a_reparse_ancestor_without_symlink_privileges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity_module()
    project_root = tmp_path / "plain-child"
    project_root.mkdir()
    original_probe = identity._is_reparse

    def simulated_reparse(path: Path) -> bool:
        return Path(path) == tmp_path or original_probe(path)

    monkeypatch.setattr(identity, "_is_reparse", simulated_reparse)
    pipeline = _pipeline(
        72,
        "ancestor reparse",
        project_key=identity.build_project_key(72, "ancestor reparse"),
    )
    with pytest.raises(identity.ProjectIdentityError, match="ancestor|reparse|junction"):
        identity.write_project_directory_manifest(
            [pipeline],
            project_root=project_root,
            manifest_path=tmp_path / "ancestor-plan.json",
            system_drive="C:",
        )


def test_reference_marker_is_bound_to_manifest_seal_root_and_target(
    tmp_path: Path,
) -> None:
    identity = _identity_module()
    project_root = tmp_path / "approved-root"
    project_root.mkdir()
    pipeline = _pipeline(
        73,
        "bound marker",
        project_key=identity.build_project_key(73, "bound marker"),
    )
    first_manifest = tmp_path / "first-plan.json"
    _, first_seal = identity.write_project_directory_manifest(
        [pipeline],
        project_root=project_root,
        manifest_path=first_manifest,
        system_drive="C:",
    )
    [prepared] = identity.prepare_project_directories(
        first_manifest,
        expected_sha256=first_seal,
        system_drive="C:",
    )
    reference_path = Path(prepared) / identity.PROJECT_REFERENCE_FILENAME
    original_reference = reference_path.read_bytes()

    second_manifest = tmp_path / "second-plan.json"
    _, second_seal = identity.write_project_directory_manifest(
        [pipeline],
        project_root=project_root,
        manifest_path=second_manifest,
        system_drive="C:",
    )
    assert second_seal != first_seal
    with pytest.raises(identity.ProjectIdentityError, match="reference tampering"):
        identity.prepare_project_directories(
            second_manifest,
            expected_sha256=second_seal,
            system_drive="C:",
        )
    assert reference_path.read_bytes() == original_reference


def test_runtime_resolver_uses_only_approved_root_key_and_bound_marker(
    tmp_path: Path,
) -> None:
    identity = _identity_module()
    project_root = tmp_path / "approved-root"
    project_root.mkdir()
    pipeline = _pipeline(
        74,
        "Display title",
        project_key=identity.build_project_key(74, "Display title"),
        legacy_project_dir="E:/shared/legacy-title-directory",
    )
    prepared = identity.prepare_pipeline_project_directory(
        pipeline,
        project_root=project_root,
        system_drive="C:",
    )
    pipeline.project_dir = "E:/attacker-controlled/old-path"

    with pytest.raises(identity.ProjectIdentityError, match="project_dir|canonical"):
        identity.resolve_prepared_project_directory(
            pipeline,
            project_root=project_root,
            system_drive="C:",
        )

    pipeline.project_dir = str(prepared)
    pipeline.project_name = "Renamed display title"
    assert identity.resolve_prepared_project_directory(
        pipeline,
        project_root=project_root,
        system_drive="C:",
    ) == Path(prepared)
    assert Path(prepared).name == pipeline.project_key
    assert "attacker-controlled" not in str(prepared)

    reference = Path(prepared) / identity.PROJECT_REFERENCE_FILENAME
    payload = json.loads(reference.read_text(encoding="utf-8"))
    payload["target_directory"] = str(project_root / "different")
    reference.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(identity.ProjectIdentityError, match="reference|marker|tamper"):
        identity.resolve_prepared_project_directory(
            pipeline,
            project_root=project_root,
            system_drive="C:",
        )


def test_runtime_resolver_rejects_marker_when_authoritative_manifest_is_missing(
    tmp_path: Path,
) -> None:
    identity = _identity_module()
    project_root = tmp_path / "approved-root"
    project_root.mkdir()
    pipeline = _pipeline(
        75,
        "Manifest authority",
        project_key=identity.build_project_key(75, "Manifest authority"),
    )
    prepared = identity.prepare_pipeline_project_directory(
        pipeline,
        project_root=project_root,
        system_drive="C:",
    )
    manifest_path = project_root / ".project-directory-plan-75.json"
    manifest_path.unlink()

    with pytest.raises(identity.ProjectIdentityError, match="manifest|seal|authority"):
        identity.resolve_prepared_project_directory(
            pipeline,
            project_root=project_root,
            system_drive="C:",
        )

    assert prepared.is_dir()


def test_prepared_child_directory_rejects_existing_reparse_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity_module()
    project_root = tmp_path / "approved-root"
    project_root.mkdir()
    pipeline = _pipeline(
        76,
        "Child guard",
        project_key=identity.build_project_key(76, "Child guard"),
    )
    prepared = identity.prepare_pipeline_project_directory(
        pipeline,
        project_root=project_root,
        system_drive="C:",
    )
    child = prepared / "素材信息"
    child.mkdir()
    original_probe = identity._is_reparse

    def simulated_reparse(path: Path) -> bool:
        return Path(path) == child or original_probe(path)

    monkeypatch.setattr(identity, "_is_reparse", simulated_reparse)

    with pytest.raises(identity.ProjectIdentityError, match="child|reparse|junction"):
        identity.begin_prepared_project_child_directory_use(
            pipeline,
            project_root=project_root,
            relative_path=Path("素材信息"),
            create=False,
            system_drive="C:",
        )


def test_prepared_output_rejects_hard_link_before_write(tmp_path: Path) -> None:
    identity = _identity_module()
    project_root = tmp_path / "approved-root"
    project_root.mkdir()
    pipeline = _pipeline(
        77,
        "Hard link guard",
        project_key=identity.build_project_key(77, "Hard link guard"),
    )
    identity.prepare_pipeline_project_directory(
        pipeline,
        project_root=project_root,
        system_drive="C:",
    )
    child_use = identity.begin_prepared_project_child_directory_use(
        pipeline,
        project_root=project_root,
        relative_path="素材信息",
        create=True,
        system_drive="C:",
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    os.link(outside, child_use.path / "timeline.json")

    with pytest.raises(identity.ProjectIdentityError, match="hard link"):
        identity.prepared_project_output_path(child_use, "timeline.json")

    assert outside.read_text(encoding="utf-8") == "keep"


def test_prepared_output_rejects_reparse_leaf_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity_module()
    project_root = tmp_path / "approved-root"
    project_root.mkdir()
    pipeline = _pipeline(
        78,
        "Output reparse guard",
        project_key=identity.build_project_key(78, "Output reparse guard"),
    )
    identity.prepare_pipeline_project_directory(
        pipeline,
        project_root=project_root,
        system_drive="C:",
    )
    child_use = identity.begin_prepared_project_child_directory_use(
        pipeline,
        project_root=project_root,
        relative_path="素材信息",
        create=True,
        system_drive="C:",
    )
    output = child_use.path / "timeline.json"
    output.write_text("keep", encoding="utf-8")
    original_probe = identity._is_reparse

    def simulated_reparse(path: Path) -> bool:
        return Path(path) == output or original_probe(path)

    monkeypatch.setattr(identity, "_is_reparse", simulated_reparse)

    with pytest.raises(identity.ProjectIdentityError, match="reparse|symlink|junction"):
        identity.prepared_project_output_path(child_use, "timeline.json")

    assert output.read_text(encoding="utf-8") == "keep"
