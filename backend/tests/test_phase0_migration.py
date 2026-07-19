from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import make_url


BACKEND_ROOT = Path(__file__).resolve().parents[1]
FROZEN_LEGACY_SQL = Path(__file__).parent / "fixtures" / "legacy_schema.sql"


def _preflight_module():
    return importlib.import_module("app.core.database_preflight")


def _migration_module():
    return importlib.import_module("app.maintenance.phase0_migration")


def _create_database(path: Path, *, with_orphan: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute(
            "CREATE TABLE children ("
            "id INTEGER PRIMARY KEY, "
            "parent_id INTEGER REFERENCES parents(id), "
            "name TEXT)"
        )
        connection.executemany(
            "INSERT INTO parents (id, name) VALUES (?, ?)",
            [(1, "one"), (2, "two")],
        )
        connection.execute(
            "INSERT INTO children (id, parent_id, name) VALUES (1, 1, 'valid')"
        )
        if with_orphan:
            connection.execute(
                "INSERT INTO children (id, parent_id, name) "
                "VALUES (2, 999, 'orphan')"
            )


def _create_frozen_legacy_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(FROZEN_LEGACY_SQL.read_text(encoding="utf-8"))


def _seed_pipeline_status_matrix(path: Path) -> None:
    rows = (
        (402, "已完成"),
        (403, "失败"),
        (404, "未开始"),
        (405, "需要人工确认"),
    )
    with closing(sqlite3.connect(path)) as connection:
        connection.executemany(
            "INSERT INTO production_pipelines ("
            "id, hotspot_id, topic_idea_id, script_asset_id, project_name, "
            "project_dir, status, provider, model, created_at, updated_at"
            ") VALUES (?, 101, 201, 301, ?, ?, ?, 'fake', 'fake-low-cost', "
            "'2026-07-02 04:05:06', '2026-07-02 05:06:07')",
            (
                (
                    pipeline_id,
                    f"状态项目-{pipeline_id}",
                    f"D:/fixtures/project-{pipeline_id}",
                    status,
                )
                for pipeline_id, status in rows
            ),
        )
        connection.commit()


def _seed_completed_pipeline(path: Path, pipeline_id: int) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "INSERT INTO production_pipelines ("
            "id, hotspot_id, topic_idea_id, script_asset_id, project_name, "
            "project_dir, status, provider, model, created_at, updated_at"
            ") VALUES (?, 101, 201, 301, ?, ?, '已完成', 'fake', "
            "'fake-low-cost', '2026-07-02 04:05:06', "
            "'2026-07-02 05:06:07')",
            (
                pipeline_id,
                f"完成项目-{pipeline_id}",
                f"D:/fixtures/project-{pipeline_id}",
            ),
        )
        connection.commit()


def _stat_identity(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _sidecar_paths(path: Path) -> list[Path]:
    return [Path(f"{path}{suffix}") for suffix in ("-journal", "-wal", "-shm")]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_manifest_next_to_source(
    backup_report,
    destination: Path,
    *,
    backup_path: Path | None = None,
) -> Path:
    payload = json.loads(Path(backup_report.manifest_path).read_text(encoding="utf-8"))
    if backup_path is not None:
        payload["backup_path"] = str(backup_path.resolve())
        payload["backup_sha256"] = _sha256(backup_path)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return destination.resolve()


def _business_counts(path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as connection:
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "AND name != 'alembic_version' ORDER BY name"
            ).fetchall()
        ]
        return {
            name: int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for name in names
        }


def _logical_database_snapshot(path: Path) -> dict[str, object]:
    with closing(sqlite3.connect(path)) as connection:
        schema = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        )
        table_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
        ]
        rows = {
            table_name: tuple(
                tuple(row)
                for row in connection.execute(
                    f'SELECT * FROM "{table_name}" ORDER BY rowid'
                ).fetchall()
            )
            for table_name in table_names
        }
    return {"schema": schema, "rows": rows}


def test_missing_canonical_database_is_allowed_without_creating_any_file(
    tmp_path: Path,
):
    canonical = tmp_path / "missing.db"

    report = _preflight_module().audit_database_identity(
        canonical,
        candidate_paths=[],
    )

    assert report.canonical_exists is False
    assert report.blockers == []
    assert report.inspected_candidate_paths == [str(canonical.resolve())]
    assert not canonical.exists()
    assert all(not path.exists() for path in _sidecar_paths(canonical))


def test_default_candidate_detection_includes_known_backend_relative_path(
    tmp_path: Path,
):
    canonical = tmp_path / "canonical.db"
    legacy = tmp_path / "backend" / canonical.name
    _create_database(canonical)
    legacy.parent.mkdir()
    legacy.write_bytes(b"")

    report = _preflight_module().audit_database_identity(canonical)

    assert str(legacy.resolve()) in report.inspected_candidate_paths
    assert report.blockers == []
    assert report.warnings


def test_zero_byte_duplicate_is_warning_and_is_not_deleted(tmp_path: Path):
    canonical = tmp_path / "canonical.db"
    duplicate = tmp_path / "legacy" / "canonical.db"
    _create_database(canonical)
    duplicate.parent.mkdir()
    duplicate.write_bytes(b"")

    report = _preflight_module().audit_database_identity(
        canonical,
        candidate_paths=[duplicate],
    )

    assert report.blockers == []
    assert any(str(duplicate.resolve()) in warning for warning in report.warnings)
    assert duplicate.exists()
    assert duplicate.stat().st_size == 0


def test_second_nonempty_database_is_a_blocking_conflict(tmp_path: Path):
    canonical = tmp_path / "canonical.db"
    duplicate = tmp_path / "legacy" / "canonical.db"
    _create_database(canonical)
    _create_database(duplicate)

    report = _preflight_module().audit_database_identity(
        canonical,
        candidate_paths=[duplicate],
    )

    assert any(str(duplicate.resolve()) in blocker for blocker in report.blockers)


def test_same_physical_database_candidate_is_deduplicated(tmp_path: Path):
    canonical = tmp_path / "canonical.db"
    alias = tmp_path / "hardlink.db"
    _create_database(canonical)
    try:
        os.link(canonical, alias)
    except OSError as exc:
        pytest.skip(f"Hard links are not available: {exc}")

    report = _preflight_module().audit_database_identity(
        canonical,
        candidate_paths=[alias, canonical],
    )

    assert report.blockers == []


def test_audit_preserves_size_mtime_and_creates_no_sqlite_sidecars(tmp_path: Path):
    canonical = tmp_path / "canonical.db"
    duplicate = tmp_path / "legacy.db"
    _create_database(canonical)
    duplicate.write_bytes(b"")
    before = {
        canonical: _stat_identity(canonical),
        duplicate: _stat_identity(duplicate),
    }
    assert all(not path.exists() for path in _sidecar_paths(canonical))

    _preflight_module().audit_database_identity(
        canonical,
        candidate_paths=[duplicate],
    )

    assert {path: _stat_identity(path) for path in before} == before
    assert all(not path.exists() for path in _sidecar_paths(canonical))
    assert all(not path.exists() for path in _sidecar_paths(duplicate))


def test_clean_wal_database_is_blocked_without_creating_sidecars(tmp_path: Path):
    canonical = tmp_path / "wal-mode.db"
    connection = sqlite3.connect(canonical)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO items VALUES (1)")
        connection.commit()
    finally:
        connection.close()
    before = _stat_identity(canonical)
    assert all(not path.exists() for path in _sidecar_paths(canonical))

    report = _preflight_module().audit_database_identity(
        canonical,
        candidate_paths=[],
    )

    assert any("WAL" in blocker for blocker in report.blockers)
    assert _stat_identity(canonical) == before
    assert all(not path.exists() for path in _sidecar_paths(canonical))


def test_existing_wal_sidecars_are_blocked_without_metadata_changes(tmp_path: Path):
    canonical = tmp_path / "wal-sidecars.db"
    connection = sqlite3.connect(canonical)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO items VALUES (1)")
        connection.commit()
    finally:
        connection.close()
    wal_path = Path(f"{canonical}-wal")
    shm_path = Path(f"{canonical}-shm")
    wal_path.write_bytes(b"")
    shm_path.write_bytes(b"")
    before = {
        canonical: _stat_identity(canonical),
        wal_path: _stat_identity(wal_path),
        shm_path: _stat_identity(shm_path),
    }

    report = _preflight_module().audit_database_identity(
        canonical,
        candidate_paths=[],
    )

    assert any("WAL" in blocker for blocker in report.blockers)
    assert {path: _stat_identity(path) for path in before} == before


def test_header_read_error_becomes_actionable_blocker_without_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    canonical = tmp_path / "locked.db"
    _create_database(canonical)
    preflight = _preflight_module()

    def fail_header_read(_path: Path) -> bool:
        raise PermissionError("simulated Windows file lock")

    monkeypatch.setattr(preflight, "_database_header_uses_wal", fail_header_read)

    report = preflight.audit_database_identity(canonical, candidate_paths=[])

    assert any("数据库头" in blocker for blocker in report.blockers)
    assert any(str(canonical.resolve()) in blocker for blocker in report.blockers)
    assert all(not path.exists() for path in _sidecar_paths(canonical))


def test_integrity_errors_are_blocking_and_actionable(tmp_path: Path):
    canonical = tmp_path / "corrupt.db"
    canonical.write_bytes(b"this is not a sqlite database")

    report = _preflight_module().audit_database_identity(
        canonical,
        candidate_paths=[],
    )

    assert report.blockers
    assert any(str(canonical.resolve()) in blocker for blocker in report.blockers)
    assert report.quick_check != "ok"


def test_foreign_key_violations_are_reported_and_block_startup(tmp_path: Path):
    canonical = tmp_path / "orphan.db"
    _create_database(canonical, with_orphan=True)

    report = _preflight_module().audit_database_identity(
        canonical,
        candidate_paths=[],
    )

    assert report.quick_check == "ok"
    assert report.foreign_key_violations
    assert report.blockers
    assert any("外键" in blocker for blocker in report.blockers)


def test_verified_online_backup_has_manifest_hash_and_matching_counts(
    tmp_path: Path,
):
    source = tmp_path / "source.db"
    backup_root = tmp_path / "backup-root"
    _create_database(source)

    report = _migration_module().create_verified_backup(
        source,
        backup_root,
        system_drive="C:",
    )

    backup_path = Path(report.backup_path)
    manifest_path = Path(report.manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert backup_path.exists()
    assert report.backup_quick_check == "ok"
    assert report.backup_foreign_key_violations == []
    assert len(report.backup_sha256) == 64
    assert report.source_table_counts == report.backup_table_counts
    assert report.table_counts_match is True
    assert manifest["backup_sha256"] == report.backup_sha256
    assert manifest["backup_path"] == str(backup_path.resolve())


@pytest.mark.parametrize("source_name", ["manifest.json", ".manifest.tmp"])
def test_backup_reserved_source_names_cannot_collide_with_manifest_files(
    tmp_path: Path,
    source_name: str,
):
    source = tmp_path / source_name
    backup_root = tmp_path / "backup-root"
    _create_database(source)

    report = _migration_module().create_verified_backup(
        source,
        backup_root,
        system_drive="C:",
    )

    backup_path = Path(report.backup_path)
    manifest_path = Path(report.manifest_path)
    assert backup_path != manifest_path
    assert backup_path.name == "database.sqlite3"
    assert backup_path.read_bytes()[:16] == b"SQLite format 3\x00"
    assert source.read_bytes()[:16] == b"SQLite format 3\x00"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["backup_path"] == str(
        backup_path.resolve()
    )


def test_backup_rejects_injected_system_drive_before_creating_output(
    tmp_path: Path,
):
    source = tmp_path / "source.db"
    backup_root = tmp_path / "forbidden-backup-root"
    _create_database(source)

    with pytest.raises(ValueError, match="系统盘"):
        _migration_module().create_verified_backup(
            source,
            backup_root,
            system_drive=backup_root.resolve().drive,
        )

    assert not backup_root.exists()


def test_backup_validates_source_before_creating_output(tmp_path: Path):
    missing_source = tmp_path / "missing.db"
    backup_root = tmp_path / "backup-root"

    with pytest.raises(ValueError, match="不存在"):
        _migration_module().create_verified_backup(
            missing_source,
            backup_root,
            system_drive="C:",
        )

    assert not backup_root.exists()


def test_cli_audit_json_uses_only_temporary_paths(tmp_path: Path):
    source = tmp_path / "audit-source.db"
    candidate = tmp_path / "legacy-zero.db"
    _create_database(source)
    candidate.write_bytes(b"")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.maintenance.phase0_migration",
            "audit",
            "--database",
            str(source),
            "--candidate",
            str(candidate),
            "--json",
        ],
        cwd=BACKEND_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["canonical_database_path"] == str(source.resolve())
    assert str(candidate.resolve()) in payload["inspected_candidate_paths"]
    assert str(Path(__file__).resolve().parents[2]) not in result.stdout


def test_cli_backup_json_uses_only_temporary_paths(tmp_path: Path):
    source = tmp_path / "backup-source.db"
    backup_root = tmp_path / "backup-output"
    _create_database(source)
    environment = os.environ.copy()
    environment["SystemDrive"] = "C:"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.maintenance.phase0_migration",
            "backup",
            "--database",
            str(source),
            "--backup-root",
            str(backup_root),
            "--json",
        ],
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
    payload = json.loads(result.stdout)
    assert payload["source_path"] == str(source.resolve())
    assert Path(payload["backup_path"]).is_relative_to(backup_root.resolve())
    assert str(Path(__file__).resolve().parents[2]) not in result.stdout


def test_cli_backup_rejects_injected_system_drive_with_json_only_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    source = tmp_path / "source.db"
    backup_root = tmp_path / "backup-root"
    _create_database(source)

    exit_code = _migration_module().main(
        [
            "backup",
            "--database",
            str(source),
            "--backup-root",
            str(backup_root),
            "--json",
        ],
        system_drive=backup_root.resolve().drive,
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    assert json.loads(captured.out)["ok"] is False
    assert captured.err == ""
    assert not backup_root.exists()


def test_schema_status_cli_is_read_only_for_missing_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    database_path = (tmp_path / "missing-schema-status.db").resolve()

    exit_code = _migration_module().main(
        ["schema-status", "--database", str(database_path), "--json"]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["state"] == "missing"
    assert captured.err == ""
    assert not database_path.exists()
    assert all(not path.exists() for path in _sidecar_paths(database_path))


@pytest.mark.parametrize(
    "manifest_defect",
    [
        "wrong_source",
        "missing_snapshot_digest",
        "tampered_backup",
        "changed_source",
        "old_manifest",
    ],
)
def test_guarded_apply_rejects_invalid_manifest_before_alembic_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_defect: str,
):
    migration = _migration_module()
    source = (tmp_path / "legacy-source.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    manifest_path = Path(backup.manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest_defect == "wrong_source":
        manifest["source_path"] = str((tmp_path / "other.db").resolve())
    elif manifest_defect == "missing_snapshot_digest":
        manifest["snapshot_logical_sha256"] = None
    elif manifest_defect == "tampered_backup":
        manifest["backup_sha256"] = "0" * 64
    elif manifest_defect == "old_manifest":
        manifest.pop("manifest_format")
        manifest.pop("snapshot_logical_sha256")
        manifest.pop("snapshot_business_sha256")
    else:
        with sqlite3.connect(source) as connection:
            connection.execute(
                "UPDATE app_config SET value = '{\"changed\":true}' WHERE id = 1201"
            )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    upgrade_calls: list[str] = []
    monkeypatch.setattr(
        migration,
        "_upgrade_with_connection",
        lambda *_args, **_kwargs: upgrade_calls.append("upgrade"),
        raising=False,
    )

    with pytest.raises((RuntimeError, ValueError), match="manifest|清单|SHA|源数据库|备份"):
        migration.apply_verified_schema_migration(source, manifest_path)

    assert upgrade_calls == []
    assert not list(manifest_path.parent.glob("migration-e-*.json"))


@pytest.mark.parametrize("alias_kind", ["same_path", "hardlink"])
def test_guarded_apply_rejects_backup_aliasing_the_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alias_kind: str,
):
    migration = _migration_module()
    source = (tmp_path / "legacy-source.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    manifest_path = Path(backup.manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if alias_kind == "same_path":
        alias = source
    else:
        alias = tmp_path / "source-hardlink.db"
        try:
            os.link(source, alias)
        except OSError as exc:
            pytest.skip(f"Hard links are not available: {exc}")
    manifest["backup_path"] = str(alias.resolve())
    manifest["backup_sha256"] = manifest["source_sha256"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    upgrade_calls: list[str] = []
    monkeypatch.setattr(
        migration,
        "_upgrade_with_connection",
        lambda *_args, **_kwargs: upgrade_calls.append("upgrade"),
        raising=False,
    )

    with pytest.raises((RuntimeError, ValueError), match="同一|硬链接|备份"):
        migration.apply_verified_schema_migration(source, manifest_path)

    assert upgrade_calls == []


def test_identity_check_error_fails_closed_for_source_backup_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "identity-source.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    manifest_path = Path(backup.manifest_path)
    hardlink = tmp_path / "source-backup-hardlink.db"
    try:
        os.link(source, hardlink)
    except OSError as exc:
        pytest.skip(f"Hard links are not available: {exc}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["backup_path"] = str(hardlink.resolve())
    manifest["backup_sha256"] = manifest["source_sha256"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    upgrade_calls: list[str] = []
    monkeypatch.setattr(
        migration.os.path,
        "samefile",
        lambda *_args: (_ for _ in ()).throw(OSError("identity unavailable")),
    )
    monkeypatch.setattr(
        migration,
        "_upgrade_with_connection",
        lambda *_args, **_kwargs: upgrade_calls.append("upgrade"),
    )

    with pytest.raises(ValueError, match="身份|identity|无法证明|同一"):
        migration.apply_verified_schema_migration(source, manifest_path)

    assert upgrade_calls == []
    assert source.stat().st_ino == hardlink.stat().st_ino


def test_guarded_apply_rechecks_source_after_initial_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    migration = _migration_module()
    source = (tmp_path / "toctou-source.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    manifest_path = Path(backup.manifest_path)
    real_authorize = migration._require_manifest_authorization
    authorization_calls = 0

    def mutate_after_first_authorization(source_path: Path, manifest: Path):
        nonlocal authorization_calls
        result = real_authorize(source_path, manifest)
        authorization_calls += 1
        if authorization_calls == 1:
            with sqlite3.connect(source) as connection:
                connection.execute(
                    "UPDATE app_config SET value = '{\"raced\":true}' WHERE id = 1201"
                )
        return result

    upgrade_calls: list[str] = []
    monkeypatch.setattr(
        migration,
        "_require_manifest_authorization",
        mutate_after_first_authorization,
    )
    monkeypatch.setattr(
        migration,
        "_upgrade_with_connection",
        lambda *_args, **_kwargs: upgrade_calls.append("upgrade"),
    )

    with pytest.raises(ValueError, match="SHA-256|过期|变化|logical|stale"):
        migration.apply_verified_schema_migration(source, manifest_path)

    with sqlite3.connect(source) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name = 'video_posts'"
        ).fetchone()[0]
    assert authorization_calls == 1
    assert upgrade_calls == []
    assert "alembic_version" not in tables
    assert not any(name.startswith("_alembic_tmp_") for name in tables)
    assert "fk_video_posts_pipeline_id_production_pipelines" not in table_sql
    assert not list(manifest_path.parent.glob("migration-e-*.json"))


@pytest.mark.parametrize(
    "protected_collision",
    ["source", "backup", "manifest"],
)
def test_result_publication_never_overwrites_or_aliases_protected_files(
    tmp_path: Path,
    protected_collision: str,
) -> None:
    migration = _migration_module()
    source_name = "migration-result.json" if protected_collision == "source" else "source.db"
    source = (tmp_path / source_name).resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    original_backup = Path(backup.backup_path)
    local_backup = original_backup
    manifest_name = (
        "migration-result.json" if protected_collision == "manifest" else "manifest.json"
    )
    if protected_collision == "backup":
        local_backup = tmp_path / "migration-result.json"
        local_backup.write_bytes(original_backup.read_bytes())
    manifest_path = _copy_manifest_next_to_source(
        backup,
        tmp_path / manifest_name,
        backup_path=local_backup,
    )
    protected = {
        "source": source,
        "backup": local_backup,
        "manifest": manifest_path,
    }
    protected_hashes = {name: _sha256(path) for name, path in protected.items()}

    reservation = migration._reserve_migration_result(
        source,
        local_backup,
        manifest_path,
    )
    migration._publish_reserved_result(
        reservation,
        {"result_path": str(reservation.final_path)},
    )

    result_path = reservation.final_path
    assert result_path not in protected.values()
    assert result_path.is_file()
    assert json.loads(result_path.read_text(encoding="utf-8"))["result_path"] == str(
        result_path
    )
    assert {name: _sha256(path) for name, path in protected.items()} == protected_hashes
    assert source.read_bytes()[:16] == b"SQLite format 3\x00"
    assert _sha256(local_backup) == protected_hashes["backup"]
    assert _sha256(manifest_path) == protected_hashes["manifest"]
    assert len(list(manifest_path.parent.glob("migration-e-*.json"))) == 1


def test_unique_result_writer_ignores_fixed_temp_and_never_overwrites_existing_evidence(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "source.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    manifest_path = Path(backup.manifest_path)
    fixed_temp = manifest_path.parent / ".manifest.tmp"
    old_target = manifest_path.parent / "migration-result.json"
    fixed_temp.write_text("owned by another operation", encoding="utf-8")
    old_target.write_text('{"old":true}\n', encoding="utf-8")
    fixed_hash = _sha256(fixed_temp)
    old_hash = _sha256(old_target)

    reservation = migration._reserve_migration_result(
        source,
        Path(backup.backup_path),
        manifest_path,
    )
    migration._publish_reserved_result(reservation, {"ok": True})

    assert reservation.final_path not in {fixed_temp, old_target}
    assert _sha256(fixed_temp) == fixed_hash
    assert _sha256(old_target) == old_hash
    assert len(list(manifest_path.parent.glob("migration-e-*.json"))) == 1


@pytest.mark.parametrize("alias_kind", ["hardlink", "symlink"])
def test_preexisting_result_alias_is_never_replaced(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "source.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    backup_path = Path(backup.backup_path)
    manifest_path = Path(backup.manifest_path)
    old_target = manifest_path.parent / "migration-result.json"
    try:
        if alias_kind == "hardlink":
            os.link(backup_path, old_target)
        else:
            old_target.symlink_to(backup_path)
    except OSError as exc:
        pytest.skip(f"{alias_kind} unavailable: {exc}")
    assert os.path.samefile(old_target, backup_path)

    reservation = migration._reserve_migration_result(
        source,
        backup_path,
        manifest_path,
    )
    migration._publish_reserved_result(reservation, {"ok": True})

    assert reservation.final_path != old_target
    assert os.path.samefile(old_target, backup_path)
    assert backup_path.read_bytes()[:16] == b"SQLite format 3\x00"


@pytest.mark.parametrize("protected_name", ["source", "backup", "manifest"])
def test_reserved_final_path_is_rejected_when_it_is_a_protected_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protected_name: str,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "source.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    protected = {
        "source": source,
        "backup": Path(backup.backup_path),
        "manifest": Path(backup.manifest_path),
    }
    before = {name: _sha256(path) for name, path in protected.items()}
    monkeypatch.setattr(
        migration,
        "_migration_result_path",
        lambda *_args: protected[protected_name],
    )

    with pytest.raises((ValueError, FileExistsError), match="证据|覆盖|相同"):
        migration._reserve_migration_result(
            source,
            Path(backup.backup_path),
            Path(backup.manifest_path),
        )

    assert {name: _sha256(path) for name, path in protected.items()} == before


def test_reserved_temp_path_rejects_a_hardlink_alias_before_begin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "source.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    manifest_path = Path(backup.manifest_path)
    temp_token = "b" * 32
    aliased_temp = manifest_path.parent / f".mr-{temp_token[:12]}.tmp"
    try:
        os.link(source, aliased_temp)
    except OSError as exc:
        pytest.skip(f"Hard links are not available: {exc}")
    tokens = iter([SimpleNamespace(hex="a" * 32), SimpleNamespace(hex=temp_token)])
    monkeypatch.setattr(migration, "uuid4", lambda: next(tokens))
    source_hash = _sha256(source)

    with pytest.raises((ValueError, FileExistsError), match="链接|覆盖|证据"):
        migration._reserve_migration_result(
            source,
            Path(backup.backup_path),
            manifest_path,
        )

    assert _sha256(source) == source_hash
    assert os.path.samefile(source, aliased_temp)


def test_cleanup_never_deletes_a_replacement_at_the_reserved_temp_path(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "source.db").resolve()
    backup_path = (tmp_path / "backup.db").resolve()
    manifest_path = (tmp_path / "manifest.json").resolve()
    _create_frozen_legacy_database(source)
    backup_path.write_bytes(source.read_bytes())
    manifest_path.write_text("{}\n", encoding="utf-8")
    reservation = migration._reserve_migration_result(
        source,
        backup_path,
        manifest_path,
    )
    os.close(reservation.descriptor)
    reservation.descriptor = -1
    reservation.temporary_path.unlink()
    replacement = b"replacement owned by another actor"
    attacker_path = tmp_path / "attacker-replacement.tmp"
    attacker_path.write_bytes(replacement)
    attacker_stat = attacker_path.stat()
    assert (attacker_stat.st_dev, attacker_stat.st_ino) != (
        reservation.temporary_identity
    )
    os.replace(attacker_path, reservation.temporary_path)

    migration._cleanup_result_reservation(reservation)

    assert reservation.temporary_path.read_bytes() == replacement


def test_guarded_apply_rejects_non_exact_fk_before_version_write(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "wrong-fk.db").resolve()
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
        "    CONSTRAINT fk_video_posts_pipeline_id_production_pipelines_suffix "
        "FOREIGN KEY(pipeline_id) REFERENCES production_pipelines(id)\n"
        ");\n"
        "CREATE INDEX ix_video_posts_pipeline_id"
    )
    with sqlite3.connect(source) as connection:
        connection.executescript(sql.replace(marker, replacement, 1))
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )

    with pytest.raises((RuntimeError, ValueError), match="pipeline_id|外键|schema|结构"):
        migration.apply_verified_schema_migration(source, Path(backup.manifest_path))

    with sqlite3.connect(source) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='alembic_version'"
        ).fetchone() is None


def test_prepare_rejects_same_count_update_committed_only_to_wal(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "wal-stale-source.db").resolve()
    _create_frozen_legacy_database(source)
    writer: sqlite3.Connection | None = sqlite3.connect(source, isolation_level=None)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        backup = migration.create_verified_backup(
            source,
            tmp_path / "backup-root",
            system_drive="C:",
        )
        manifest = json.loads(Path(backup.manifest_path).read_text(encoding="utf-8"))
        assert manifest["manifest_format"] == "phase0-backup-v2"
        assert len(manifest["snapshot_logical_sha256"]) == 64
        main_file_hash_before = _sha256(source)
        counts_before = _business_counts(source)

        writer.execute(
            "UPDATE app_config SET value = '{\"wal_only\":true}' WHERE id = 1201"
        )

        assert _sha256(source) == main_file_hash_before
        assert _business_counts(source) == counts_before
        with pytest.raises(ValueError, match="logical|digest|snapshot|manifest|逻辑|快照|过期"):
            migration.prepare_verified_schema_migration(
                source,
                Path(backup.manifest_path),
            )
        assert not list(tmp_path.glob(".phase0-candidate-*.sqlite3"))
        with closing(sqlite3.connect(source)) as check:
            assert check.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='alembic_version'"
            ).fetchone() is None
    finally:
        writer.close()


def test_two_phase_prepare_migrates_isolated_wal_snapshot_before_cutover(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "wal-two-phase-source.db").resolve()
    _create_frozen_legacy_database(source)
    writer = sqlite3.connect(source, isolation_level=None)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        backup = migration.create_verified_backup(
            source,
            tmp_path / "backup-root",
            system_drive="C:",
        )
        source_before = _logical_database_snapshot(source)

        preparation = migration.prepare_verified_schema_migration(
            source,
            Path(backup.manifest_path),
        )

        candidate = Path(preparation.candidate_path)
        assert candidate.is_file()
        assert _logical_database_snapshot(source) == source_before
        with closing(sqlite3.connect(source)) as check:
            assert check.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='alembic_version'"
            ).fetchone() is None
        with closing(sqlite3.connect(candidate)) as check:
            assert check.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0] == "20260711_0003"
            assert check.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert all(not path.exists() for path in _sidecar_paths(candidate))

        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.close()
        writer = None
        normalizer = sqlite3.connect(source, isolation_level=None)
        try:
            assert normalizer.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        finally:
            normalizer.close()
    finally:
        if writer is not None:
            writer.close()

    with pytest.raises(migration.ManualCutoverRequiredError):
        migration.cutover_prepared_schema_migration(
            Path(preparation.preparation_path),
            confirm_services_stopped=True,
        )

    assert _logical_database_snapshot(source) == source_before
    with closing(sqlite3.connect(source)) as check:
        assert check.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='alembic_version'"
        ).fetchone() is None
        assert check.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert all(not path.exists() for path in _sidecar_paths(source))
    assert candidate.is_file()
    assert not list(tmp_path.glob(".phase0-rollback-*.sqlite3"))
    assert not list(Path(backup.manifest_path).parent.glob("migration-e-*.json"))


def test_backup_holds_writer_gate_across_digest_and_online_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "locked-backup-source.db").resolve()
    _create_frozen_legacy_database(source)
    real_digest = migration._sqlite_logical_digest
    attempted_updates = 0

    def attack_between_digest_and_copy(connection, **kwargs):
        nonlocal attempted_updates
        result = real_digest(connection, **kwargs)
        if kwargs.get("include_schema") is True and attempted_updates == 0:
            attempted_updates += 1
            attacker = sqlite3.connect(source, timeout=0.05, isolation_level=None)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    attacker.execute(
                        "UPDATE app_config SET value = '{\"raced\":true}' WHERE id = 1201"
                    )
            finally:
                attacker.close()
        return result

    monkeypatch.setattr(migration, "_sqlite_logical_digest", attack_between_digest_and_copy)

    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )

    assert attempted_updates == 1
    manifest = json.loads(Path(backup.manifest_path).read_text(encoding="utf-8"))
    with closing(sqlite3.connect(Path(backup.backup_path))) as connection:
        assert real_digest(connection, include_schema=True) == manifest[
            "snapshot_logical_sha256"
        ]


def test_original_business_projection_digest_survives_additive_schema_change(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    database = tmp_path / "projection.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO items (id, value) VALUES (1, 'preserved')")
        connection.commit()
        original_digest = migration._sqlite_logical_digest(
            connection,
            include_schema=False,
        )
        projection = migration._capture_sqlite_projection(
            connection,
            exclude_tables=("alembic_version",),
        )
        connection.execute("ALTER TABLE items ADD COLUMN project_key TEXT")
        connection.execute("UPDATE items SET project_key = 'p-1-preserved'")
        connection.commit()

        assert migration._sqlite_logical_digest(
            connection,
            include_schema=False,
            projection=projection,
        ) == original_digest


def test_preparation_v3_seed_read_failure_cleans_owned_candidate_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "seed-read-failure.db").resolve()
    _create_frozen_legacy_database(source)
    real_open = Path.open

    def fail_seed_read(path: Path, mode="r", *args, **kwargs):
        if path.resolve(strict=False) == source and mode == "rb":
            raise OSError("simulated seed read failure")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_seed_read)

    with pytest.raises(OSError, match="simulated seed read failure"):
        migration._reserve_owned_sqlite_artifact(
            source,
            role="candidate",
            protected_paths=(source,),
            seed_path=source,
        )

    assert not list(tmp_path.glob(".p0c-*"))


def test_preparation_v3_seed_cleanup_never_deletes_replacement_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "seed-replacement.db").resolve()
    _create_frozen_legacy_database(source)
    real_open = Path.open
    real_close = os.close
    replacement_path: Path | None = None
    replacement_payload = b"externally-replaced-candidate"

    def fail_seed_read(path: Path, mode="r", *args, **kwargs):
        if path.resolve(strict=False) == source and mode == "rb":
            raise OSError("simulated seed failure before cleanup")
        return real_open(path, mode, *args, **kwargs)

    def close_then_replace(descriptor: int) -> None:
        nonlocal replacement_path
        opened_stat = os.fstat(descriptor)
        real_close(descriptor)
        if replacement_path is None:
            container = next(tmp_path.glob(".p0c-*"))
            candidate = container / "c.db"
            candidate_stat = candidate.stat(follow_symlinks=False)
            if (
                int(candidate_stat.st_dev),
                int(candidate_stat.st_ino),
            ) == (int(opened_stat.st_dev), int(opened_stat.st_ino)):
                candidate.unlink()
                candidate.write_bytes(replacement_payload)
                replacement_path = candidate

    monkeypatch.setattr(Path, "open", fail_seed_read)
    monkeypatch.setattr(migration.os, "close", close_then_replace)

    with pytest.raises(OSError, match="before cleanup"):
        migration._reserve_owned_sqlite_artifact(
            source,
            role="candidate",
            protected_paths=(source,),
            seed_path=source,
        )

    assert replacement_path is not None
    assert replacement_path.read_bytes() == replacement_payload


def test_preparation_v3_json_publish_fsync_open_failure_cleans_owned_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    evidence_path = (tmp_path / "preparation.json").resolve()
    real_open = migration.os.open

    def fail_published_open(path, flags, *args, **kwargs):
        if Path(path).resolve(strict=False) == evidence_path:
            raise OSError("simulated final fsync open failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(migration.os, "open", fail_published_open)

    with pytest.raises(OSError, match="final fsync open failure"):
        migration._write_json_atomically(evidence_path, {"complete": True})

    assert not evidence_path.exists()
    assert not list(tmp_path.glob(".json-*.tmp"))


def test_preparation_v3_json_publish_cleanup_preserves_replacement_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    evidence_path = (tmp_path / "replacement.json").resolve()
    replacement_payload = b"external-json-replacement"
    real_open = migration.os.open

    def replace_published_file(path, flags, *args, **kwargs):
        if Path(path).resolve(strict=False) == evidence_path:
            evidence_path.unlink()
            evidence_path.write_bytes(replacement_payload)
            raise OSError("simulated replacement before final fsync")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(migration.os, "open", replace_published_file)

    with pytest.raises(OSError, match="replacement before final fsync"):
        migration._write_json_atomically(evidence_path, {"complete": True})

    assert evidence_path.read_bytes() == replacement_payload
    assert not list(tmp_path.glob(".json-*.tmp"))


def test_preparation_v3_json_publish_rejects_final_path_exchange_before_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    evidence_path = (tmp_path / "exchanged.json").resolve()
    replacement_payload = b"foreign-after-hardlink-publish"
    real_publish = migration._publish_file_exclusively

    def publish_then_exchange(temporary_path: Path, final_path: Path) -> None:
        real_publish(temporary_path, final_path)
        final_path.unlink()
        final_path.write_bytes(replacement_payload)

    monkeypatch.setattr(
        migration,
        "_publish_file_exclusively",
        publish_then_exchange,
    )

    with pytest.raises(RuntimeError, match="published JSON.*inode|identity"):
        migration._write_json_atomically(evidence_path, {"complete": True})

    assert evidence_path.read_bytes() == replacement_payload
    assert not list(tmp_path.glob(".json-*.tmp"))


def test_preparation_v3_json_publish_exchange_cleans_callers_owned_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "prepare-json-exchange.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    replacement_payload = b"foreign-preparation-evidence"
    real_publish = migration._publish_file_exclusively

    def publish_then_exchange(temporary_path: Path, final_path: Path) -> None:
        real_publish(temporary_path, final_path)
        final_path.unlink()
        final_path.write_bytes(replacement_payload)

    monkeypatch.setattr(
        migration,
        "_publish_file_exclusively",
        publish_then_exchange,
    )

    with pytest.raises(RuntimeError, match="published JSON.*inode|identity"):
        migration.prepare_verified_schema_migration(
            source,
            Path(backup.manifest_path),
        )

    assert not list(tmp_path.glob(".p0c-*"))
    evidence_paths = list(
        Path(backup.manifest_path).parent.glob("migration-p-*.json")
    )
    assert len(evidence_paths) == 1
    assert evidence_paths[0].read_bytes() == replacement_payload
    assert not list(Path(backup.manifest_path).parent.glob(".json-*.tmp"))


def test_preparation_v3_json_publish_normal_success_is_complete_and_parseable(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    evidence_path = (tmp_path / "normal.json").resolve()

    migration._write_json_atomically(
        evidence_path,
        {"complete": True, "label": "正常"},
    )

    assert json.loads(evidence_path.read_text(encoding="utf-8")) == {
        "complete": True,
        "label": "正常",
    }
    assert not list(tmp_path.glob(".json-*.tmp"))


def test_preparation_v3_reclassification_proves_only_legacy_completed_rows(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "v3-reclassification-source.db").resolve()
    _create_frozen_legacy_database(source)
    _seed_pipeline_status_matrix(source)
    with closing(sqlite3.connect(source)) as connection:
        full_logical_before = migration._sqlite_logical_digest(
            connection,
            include_schema=True,
        )
        full_business_before = migration._sqlite_logical_digest(
            connection,
            include_schema=False,
            exclude_tables=("alembic_version",),
        )
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )

    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )

    assert preparation.preparation_format == "phase0-migration-preparation-v3"
    assert preparation.pipeline_status_reclassifications == [
        {
            "pipeline_id": 402,
            "old_status": "已完成",
            "new_status": "需要人工确认",
            "reason_code": "legacy_completion_without_evidence",
        }
    ]
    candidate = Path(preparation.candidate_path)
    assert (candidate.parent.stat().st_dev, candidate.parent.stat().st_ino) == (
        preparation.candidate_parent_device,
        preparation.candidate_parent_inode,
    )
    with closing(sqlite3.connect(source)) as source_connection, closing(
        sqlite3.connect(candidate)
    ) as candidate_connection:
        assert migration._sqlite_logical_digest(
            source_connection,
            include_schema=True,
        ) == full_logical_before
        assert migration._sqlite_logical_digest(
            source_connection,
            include_schema=False,
            exclude_tables=("alembic_version",),
        ) == full_business_before
        full_projection = migration._capture_sqlite_projection(
            source_connection,
            exclude_tables=("alembic_version",),
        )
        preserved_projection = migration.build_preserved_projection(
            full_projection,
            preparation.before_revision,
        )
        assert migration._sqlite_logical_digest(
            source_connection,
            include_schema=False,
            projection=preserved_projection,
        ) == preparation.source_preserved_business_sha256
        assert migration._sqlite_logical_digest(
            candidate_connection,
            include_schema=False,
            projection=preserved_projection,
        ) == preparation.source_preserved_business_sha256
        assert candidate_connection.execute(
            "SELECT id, status FROM production_pipelines ORDER BY id"
        ).fetchall() == [
            (401, "进行中"),
            (402, "需要人工确认"),
            (403, "失败"),
            (404, "未开始"),
            (405, "需要人工确认"),
        ]
        assert candidate_connection.execute(
            "SELECT id, completion_evidence FROM production_pipelines ORDER BY id"
        ).fetchall() == [(pipeline_id, "{}") for pipeline_id in range(401, 406)]


@pytest.mark.parametrize(
    "mutation",
    (
        "failed_status",
        "running_status",
        "not_started_status",
        "confirmation_status",
        "insert_pipeline",
        "delete_pipeline",
        "change_step",
        "change_non_status_field",
    ),
)
def test_preparation_v3_reclassification_rejects_unauthorized_mutation_and_cleans_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    migration = _migration_module()
    source = (tmp_path / f"reject-{mutation}.db").resolve()
    _create_frozen_legacy_database(source)
    _seed_pipeline_status_matrix(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    real_upgrade = migration._upgrade_with_connection

    def upgrade_then_mutate(database_path, connection) -> None:
        real_upgrade(database_path, connection)
        if mutation == "failed_status":
            connection.exec_driver_sql(
                "UPDATE production_pipelines SET status='进行中' WHERE id=403"
            )
        elif mutation == "running_status":
            connection.exec_driver_sql(
                "UPDATE production_pipelines SET status='失败' WHERE id=401"
            )
        elif mutation == "not_started_status":
            connection.exec_driver_sql(
                "UPDATE production_pipelines SET status='进行中' WHERE id=404"
            )
        elif mutation == "confirmation_status":
            connection.exec_driver_sql(
                "UPDATE production_pipelines SET status='进行中' WHERE id=405"
            )
        elif mutation == "insert_pipeline":
            connection.exec_driver_sql(
                "INSERT INTO production_pipelines ("
                "id, hotspot_id, topic_idea_id, script_asset_id, project_name, "
                "project_dir, status, provider, model, created_at, updated_at, "
                "project_key, legacy_project_dir, completion_evidence"
                ") VALUES (999, 101, 201, 301, 'inserted', "
                "'D:/fixtures/project-999', '未开始', 'fake', 'fake-low-cost', "
                "'2026-07-02 04:05:06', '2026-07-02 05:06:07', "
                "'pending-00000000000000000000000000000000-inserted', "
                "'D:/fixtures/project-999', '{}')"
            )
        elif mutation == "delete_pipeline":
            connection.exec_driver_sql(
                "DELETE FROM production_pipelines WHERE id=405"
            )
        elif mutation == "change_step":
            connection.exec_driver_sql(
                "UPDATE pipeline_steps SET output_summary='{}' WHERE id=501"
            )
        elif mutation == "change_non_status_field":
            connection.exec_driver_sql(
                "UPDATE production_pipelines SET provider='tampered' WHERE id=401"
            )
        else:  # pragma: no cover - the parametrization is the authority
            raise AssertionError(mutation)

    monkeypatch.setattr(migration, "_upgrade_with_connection", upgrade_then_mutate)

    with pytest.raises(RuntimeError, match="projection|reclassification|pipeline"):
        migration.prepare_verified_schema_migration(
            source,
            Path(backup.manifest_path),
        )

    assert not list(tmp_path.glob(".p0c-*"))
    assert not list(Path(backup.manifest_path).parent.glob("migration-p-*.json"))


def test_preparation_v3_at_0003_preserves_status_and_evidence_exactly(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "already-v3-source.db").resolve()
    _create_frozen_legacy_database(source)
    command = importlib.import_module("alembic.command")
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    command.upgrade(schema_migrations._config(source), "head")
    raw_evidence = '{"approved_by":"Ian","nested":{"ok":true}}'
    with closing(sqlite3.connect(source)) as connection:
        connection.execute(
            "UPDATE production_pipelines SET status='已完成', completion_evidence=? "
            "WHERE id=401",
            (raw_evidence,),
        )
        connection.commit()
    source_before = _logical_database_snapshot(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )

    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )

    assert preparation.before_revision == "20260711_0003"
    assert preparation.pipeline_status_reclassifications == []
    with closing(sqlite3.connect(Path(preparation.candidate_path))) as connection:
        assert connection.execute(
            "SELECT status, completion_evidence FROM production_pipelines WHERE id=401"
        ).fetchone() == ("已完成", raw_evidence)
    assert _logical_database_snapshot(source) == source_before


def test_cutover_reclassification_is_recomputed_instead_of_trusting_v3_json(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "cutover-proof-source.db").resolve()
    _create_frozen_legacy_database(source)
    _seed_pipeline_status_matrix(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )
    preparation_path = Path(preparation.preparation_path)
    payload = json.loads(preparation_path.read_text(encoding="utf-8"))
    payload["pipeline_status_reclassifications"] = []
    preparation_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="reclassification|transition|proof"):
        migration.cutover_prepared_schema_migration(
            preparation_path,
            confirm_services_stopped=True,
        )

    assert Path(preparation.candidate_path).is_file()
    assert preparation_path.is_file()


@pytest.mark.parametrize(
    "tamper",
    (
        "float_id",
        "bool_id",
        "string_id",
        "extra_key",
        "missing_key",
        "wrong_old_status",
        "wrong_new_status",
        "wrong_reason",
        "wrong_status_type",
        "wrong_reason_type",
        "row_not_object",
    ),
)
def test_cutover_reclassification_rejects_non_exact_prepared_transition_rows(
    tmp_path: Path,
    tamper: str,
) -> None:
    migration = _migration_module()
    source = (tmp_path / f"strict-transition-{tamper}.db").resolve()
    _create_frozen_legacy_database(source)
    expected_pipeline_id = 1 if tamper == "bool_id" else 402
    _seed_completed_pipeline(source, expected_pipeline_id)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )
    preparation_path = Path(preparation.preparation_path)
    payload = json.loads(preparation_path.read_text(encoding="utf-8"))
    transition = payload["pipeline_status_reclassifications"][0]
    if tamper == "float_id":
        transition["pipeline_id"] = float(expected_pipeline_id)
    elif tamper == "bool_id":
        transition["pipeline_id"] = True
    elif tamper == "string_id":
        transition["pipeline_id"] = str(expected_pipeline_id)
    elif tamper == "extra_key":
        transition["unexpected"] = "value"
    elif tamper == "missing_key":
        transition.pop("reason_code")
    elif tamper == "wrong_old_status":
        transition["old_status"] = "进行中"
    elif tamper == "wrong_new_status":
        transition["new_status"] = "已完成"
    elif tamper == "wrong_reason":
        transition["reason_code"] = "trusted_payload"
    elif tamper == "wrong_status_type":
        transition["old_status"] = ["已完成"]
    elif tamper == "wrong_reason_type":
        transition["reason_code"] = 1
    elif tamper == "row_not_object":
        payload["pipeline_status_reclassifications"][0] = [
            expected_pipeline_id,
            "已完成",
            "需要人工确认",
            "legacy_completion_without_evidence",
        ]
    else:  # pragma: no cover - parametrization is the authority
        raise AssertionError(tamper)
    preparation_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid reclassification proof"):
        migration.cutover_prepared_schema_migration(
            preparation_path,
            confirm_services_stopped=True,
        )

    assert Path(preparation.candidate_path).is_file()
    assert preparation_path.is_file()


def test_cutover_rejects_preparation_v2_with_rerun_prepare_instruction(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "reject-v2-source.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )
    preparation_path = Path(preparation.preparation_path)
    payload = json.loads(preparation_path.read_text(encoding="utf-8"))
    payload["preparation_format"] = "phase0-migration-preparation-v2"
    preparation_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="v2|re-run prepare|重新.*prepare"):
        migration.cutover_prepared_schema_migration(
            preparation_path,
            confirm_services_stopped=True,
        )

    assert Path(preparation.candidate_path).is_file()
    assert preparation_path.is_file()


def test_cutover_reclassification_recomputes_candidate_rows_after_payload_rehash(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "cutover-row-recompute.db").resolve()
    _create_frozen_legacy_database(source)
    _seed_pipeline_status_matrix(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )
    candidate = Path(preparation.candidate_path)
    with closing(sqlite3.connect(candidate)) as connection:
        connection.execute(
            "UPDATE production_pipelines SET status='进行中' WHERE id=403"
        )
        connection.commit()
        candidate_business_digest = migration._sqlite_logical_digest(
            connection,
            include_schema=False,
            exclude_tables=("alembic_version",),
        )
    preparation_path = Path(preparation.preparation_path)
    payload = json.loads(preparation_path.read_text(encoding="utf-8"))
    payload["candidate_sha256"] = migration._sha256_file(candidate)
    payload["candidate_snapshot_business_sha256"] = candidate_business_digest
    preparation_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="reclassification.*unauthorized"):
        migration.cutover_prepared_schema_migration(
            preparation_path,
            confirm_services_stopped=True,
        )

    assert candidate.is_file()
    assert preparation_path.is_file()


def test_cutover_preparation_v3_rejects_payload_controlled_projection(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "cutover-projection-payload.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )
    preparation_path = Path(preparation.preparation_path)
    payload = json.loads(preparation_path.read_text(encoding="utf-8"))
    payload["source_business_projection"]["app_config"].remove("value")
    preparation_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="full business projection"):
        migration.cutover_prepared_schema_migration(
            preparation_path,
            confirm_services_stopped=True,
        )

    assert Path(preparation.candidate_path).is_file()
    assert preparation_path.is_file()


def test_cutover_preparation_v3_rejects_hardlinked_candidate(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "cutover-hardlink.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )
    candidate = Path(preparation.candidate_path)
    alias = candidate.with_name("candidate-alias.db")
    os.link(candidate, alias)

    with pytest.raises(ValueError, match="single-link"):
        migration.cutover_prepared_schema_migration(
            Path(preparation.preparation_path),
            confirm_services_stopped=True,
        )

    assert candidate.is_file()
    assert alias.is_file()


def test_cutover_preparation_v3_rejects_replaced_candidate_parent_after_link_swap(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "cutover-parent-swap.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )
    candidate = Path(preparation.candidate_path)
    original_parent = candidate.parent
    renamed_parent = original_parent.with_name(original_parent.name + "-renamed")
    original_parent.rename(renamed_parent)
    original_inode_path = renamed_parent / candidate.name
    original_parent.mkdir()
    os.link(original_inode_path, candidate)
    original_inode_path.unlink()
    assert candidate.stat().st_nlink == 1
    assert candidate.stat().st_ino == preparation.candidate_inode

    with pytest.raises(ValueError, match="parent physical identity"):
        migration.cutover_prepared_schema_migration(
            Path(preparation.preparation_path),
            confirm_services_stopped=True,
        )

    assert candidate.is_file()
    assert Path(preparation.preparation_path).is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows deny-write verification handle")
def test_cutover_preparation_v3_blocks_candidate_write_during_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "cutover-write-attempt.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )
    candidate = Path(preparation.candidate_path).resolve()
    real_verification = migration._sqlite_snapshot_verification
    write_attempt_blocked = False

    def verification_with_write_attempt(connection):
        nonlocal write_attempt_blocked
        database_path = Path(
            connection.execute("PRAGMA database_list").fetchone()[2]
        ).resolve()
        if database_path == candidate:
            attacker: sqlite3.Connection | None = None
            try:
                attacker = sqlite3.connect(
                    candidate,
                    timeout=0.05,
                    isolation_level=None,
                )
                attacker.execute(
                    "UPDATE app_config SET value='{}' WHERE id=1201"
                )
            except sqlite3.OperationalError:
                write_attempt_blocked = True
            finally:
                if attacker is not None:
                    attacker.close()
            if not write_attempt_blocked:
                raise AssertionError("candidate write unexpectedly succeeded")
        return real_verification(connection)

    monkeypatch.setattr(
        migration,
        "_sqlite_snapshot_verification",
        verification_with_write_attempt,
    )

    with pytest.raises(migration.ManualCutoverRequiredError):
        migration.cutover_prepared_schema_migration(
            Path(preparation.preparation_path),
            confirm_services_stopped=True,
        )

    assert write_attempt_blocked is True
    assert candidate.is_file()


def test_cutover_preparation_v3_fails_if_candidate_connection_is_not_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "cutover-rw-connection.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )
    candidate = Path(preparation.candidate_path).resolve()
    real_connect = sqlite3.connect

    class ReadWriteConnectionProxy:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def execute(self, statement, *args, **kwargs):
            if str(statement).strip().casefold() == "pragma query_only":
                return SimpleNamespace(fetchone=lambda: (0,))
            return self.connection.execute(statement, *args, **kwargs)

        def close(self) -> None:
            self.connection.close()

        def __getattr__(self, name):
            return getattr(self.connection, name)

    def substitute_read_write_connection(database, *args, **kwargs):
        database_text = str(database)
        if database_text.startswith(candidate.as_uri()) and "mode=ro" in database_text:
            return ReadWriteConnectionProxy(real_connect(database, *args, **kwargs))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(migration.sqlite3, "connect", substitute_read_write_connection)

    with pytest.raises(ValueError, match="connection is not read-only"):
        migration.cutover_prepared_schema_migration(
            Path(preparation.preparation_path),
            confirm_services_stopped=True,
        )

    assert candidate.is_file()
    assert Path(preparation.preparation_path).is_file()


def test_prepare_binds_reserved_candidate_before_snapshot_can_write_backup_victim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "candidate-binding-source.db").resolve()
    _create_frozen_legacy_database(source)
    with closing(sqlite3.connect(source, isolation_level=None)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    victim = Path(backup.backup_path)
    victim_hash_before = _sha256(victim)
    real_reserve = migration._reserve_owned_sqlite_artifact

    def replace_candidate_with_backup_hardlink(*args, **kwargs):
        artifact = real_reserve(*args, **kwargs)
        artifact.path.unlink()
        os.link(victim, artifact.path)
        return artifact

    monkeypatch.setattr(
        migration,
        "_reserve_owned_sqlite_artifact",
        replace_candidate_with_backup_hardlink,
    )

    with pytest.raises(RuntimeError, match="owned SQLite artifact|candidate"):
        migration.prepare_verified_schema_migration(
            source,
            Path(backup.manifest_path),
        )

    assert _sha256(victim) == victim_hash_before


def test_failed_prepare_preserves_unowned_candidate_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "external-sidecar-source.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    external_payloads: dict[Path, bytes] = {}

    def fail_after_external_sidecars(candidate_path: Path, *args, **kwargs):
        del args, kwargs
        for index, sidecar in enumerate(_sidecar_paths(Path(candidate_path))):
            payload = f"external-sidecar-{index}".encode()
            sidecar.write_bytes(payload)
            external_payloads[sidecar] = payload
        raise RuntimeError("simulated candidate migration failure")

    monkeypatch.setattr(
        migration,
        "_migrate_verified_candidate",
        fail_after_external_sidecars,
    )

    with pytest.raises(RuntimeError, match="simulated candidate migration failure"):
        migration.prepare_verified_schema_migration(
            source,
            Path(backup.manifest_path),
        )

    assert external_payloads
    assert {
        sidecar: sidecar.read_bytes()
        for sidecar in external_payloads
    } == external_payloads


def test_prepare_rejects_preexisting_candidate_sidecar_before_sqlite_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "preexisting-sidecar-source.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    real_reserve = migration._reserve_owned_sqlite_artifact
    external_payload = b"externally-owned-journal"
    external_sidecar: Path | None = None

    def reserve_then_create_external_sidecar(*args, **kwargs):
        nonlocal external_sidecar
        artifact = real_reserve(*args, **kwargs)
        external_sidecar = Path(f"{artifact.path}-journal")
        external_sidecar.write_bytes(external_payload)
        return artifact

    monkeypatch.setattr(
        migration,
        "_reserve_owned_sqlite_artifact",
        reserve_then_create_external_sidecar,
    )

    with pytest.raises(ValueError, match="sidecars"):
        migration.prepare_verified_schema_migration(
            source,
            Path(backup.manifest_path),
        )

    assert external_sidecar is not None
    assert external_sidecar.read_bytes() == external_payload


def test_cutover_rejects_tampered_candidate_without_changing_source(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "tampered-candidate-source.db").resolve()
    _create_frozen_legacy_database(source)
    source_before = _logical_database_snapshot(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )
    candidate = Path(preparation.candidate_path)
    with closing(sqlite3.connect(candidate)) as connection:
        connection.execute(
            "UPDATE app_config SET value = '{\"tampered\":true}' WHERE id = 1201"
        )
        connection.commit()

    with pytest.raises(ValueError, match="candidate.*SHA|candidate.*changed"):
        migration.cutover_prepared_schema_migration(
            Path(preparation.preparation_path),
            confirm_services_stopped=True,
        )

    assert _logical_database_snapshot(source) == source_before
    assert candidate.is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-exclusion proof")
def test_cutover_open_source_handle_fails_closed_and_keeps_candidate(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "open-handle-source.db").resolve()
    _create_frozen_legacy_database(source)
    source_before = _logical_database_snapshot(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )
    candidate = Path(preparation.candidate_path)
    blocker = sqlite3.connect(source)
    blocker.execute("SELECT COUNT(*) FROM app_config").fetchone()
    try:
        with pytest.raises(ValueError, match="open.*handle|sharing"):
            migration.cutover_prepared_schema_migration(
                Path(preparation.preparation_path),
                confirm_services_stopped=True,
            )
    finally:
        blocker.close()

    assert _logical_database_snapshot(source) == source_before
    assert candidate.is_file()
    assert not list(tmp_path.glob(".phase0-rollback-*.sqlite3"))
    assert not list(Path(backup.manifest_path).parent.glob("migration-e-*.json"))


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-exclusion proof")
def test_cutover_open_candidate_handle_fails_before_manual_handoff(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "open-candidate-source.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )
    candidate = Path(preparation.candidate_path)
    blocker = sqlite3.connect(candidate)
    blocker.execute("SELECT COUNT(*) FROM app_config").fetchone()
    try:
        with pytest.raises(ValueError, match="open.*handle|sharing"):
            migration.cutover_prepared_schema_migration(
                Path(preparation.preparation_path),
                confirm_services_stopped=True,
            )
    finally:
        blocker.close()

    assert source.is_file()
    assert candidate.is_file()


def test_cutover_stale_source_fails_before_manual_handoff(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migration = _migration_module()
    source = (tmp_path / "gate-race-source.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )
    with closing(sqlite3.connect(source)) as writer:
        writer.execute(
            "UPDATE app_config SET value = '{\"won_race\":true}' WHERE id = 1201"
        )
        writer.commit()

    exit_code = migration.main(
        [
            "cutover",
            "--preparation",
            preparation.preparation_path,
            "--confirm-services-stopped",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code != 0
    assert payload["ok"] is False
    assert payload["production_changed"] is False
    assert "manual_cutover_required" not in payload
    assert "stale" in payload["error"] or "differs" in payload["error"]
    assert captured.err == ""
    with closing(sqlite3.connect(source)) as connection:
        assert json.loads(
            connection.execute("SELECT value FROM app_config WHERE id=1201").fetchone()[0]
        ) == {"won_race": True}
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='alembic_version'"
        ).fetchone() is None
    assert Path(preparation.candidate_path).is_file()


def test_cutover_cli_reports_manual_requirement_and_verified_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migration = _migration_module()
    source = (tmp_path / "manual-cutover-source.db").resolve()
    _create_frozen_legacy_database(source)
    source_before = _logical_database_snapshot(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )

    exit_code = migration.main(
        [
            "cutover",
            "--preparation",
            preparation.preparation_path,
            "--confirm-services-stopped",
            "--json",
        ]
    )

    output = capsys.readouterr()
    result = json.loads(output.out)
    assert exit_code != 0
    assert result["production_changed"] is False
    assert result["manual_cutover_required"] is True
    assert result["candidate_path"] == preparation.candidate_path
    assert result["backup_path"] == backup.backup_path
    assert result["preparation_path"] == preparation.preparation_path
    assert output.err == ""
    assert _logical_database_snapshot(source) == source_before


def test_cutover_fail_closed_never_calls_replace_or_creates_recovery_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "no-replace-source.db").resolve()
    _create_frozen_legacy_database(source)
    source_before = _logical_database_snapshot(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )
    replace_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        migration.os,
        "replace",
        lambda *args: replace_calls.append(args),
    )

    with pytest.raises(migration.ManualCutoverRequiredError):
        migration.cutover_prepared_schema_migration(
            Path(preparation.preparation_path),
            confirm_services_stopped=True,
        )

    assert replace_calls == []
    assert _logical_database_snapshot(source) == source_before
    assert Path(preparation.candidate_path).is_file()
    assert not list(tmp_path.glob(".phase0-rollback-*.sqlite3"))
    assert not list(Path(backup.manifest_path).parent.glob("cutover-i-*.json"))
    assert not list(Path(backup.manifest_path).parent.glob("migration-e-*.json"))


def test_guarded_apply_prepares_migrated_candidate_without_changing_source(
    tmp_path: Path,
):
    migration = _migration_module()
    source = (tmp_path / "legacy-source.db").resolve()
    _create_frozen_legacy_database(source)
    with closing(sqlite3.connect(source)) as connection:
        before_posts = connection.execute(
            "SELECT * FROM video_posts ORDER BY id"
        ).fetchall()
        before_snapshots = connection.execute(
            "SELECT * FROM video_post_snapshots ORDER BY id"
        ).fetchall()
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    manifest_path = Path(backup.manifest_path)

    source_before = _logical_database_snapshot(source)
    preparation = migration.prepare_verified_schema_migration(source, manifest_path)

    candidate = Path(preparation.candidate_path)
    with sqlite3.connect(candidate) as connection:
        after_posts = connection.execute(
            "SELECT * FROM video_posts ORDER BY id"
        ).fetchall()
        after_snapshots = connection.execute(
            "SELECT * FROM video_post_snapshots ORDER BY id"
        ).fetchall()
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(video_posts)"
        ).fetchall()
    assert before_posts == after_posts
    assert before_snapshots == after_snapshots
    assert revision == "20260711_0003"
    assert any(row[2] == "production_pipelines" and row[3] == "pipeline_id" for row in foreign_keys)
    assert preparation.before_revision is None
    assert preparation.head_revision == "20260711_0003"
    assert "project_key" not in preparation.source_business_projection[
        "production_pipelines"
    ]
    with closing(sqlite3.connect(candidate)) as connection:
        projection = {
            table_name: tuple(columns)
            for table_name, columns in preparation.source_business_projection.items()
        }
        assert migration._sqlite_logical_digest(
            connection,
            include_schema=False,
            projection=projection,
        ) == preparation.source_snapshot_business_sha256
        assert migration._sqlite_logical_digest(
            connection,
            include_schema=False,
            exclude_tables=("alembic_version",),
        ) == preparation.candidate_snapshot_business_sha256
    assert (
        preparation.candidate_snapshot_business_sha256
        != preparation.source_snapshot_business_sha256
    )
    assert preparation.quick_check == "ok"
    assert preparation.foreign_key_violations == []
    assert _logical_database_snapshot(source) == source_before
    with closing(sqlite3.connect(source)) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='alembic_version'"
        ).fetchone() is None
    assert not list(manifest_path.parent.glob("migration-e-*.json"))
    assert not list(manifest_path.parent.glob(".mr-*.tmp"))


@pytest.mark.parametrize(
    ("starting_revision", "expected_state"),
    [("20260711_0001", "behind"), ("head", "ready")],
)
def test_guarded_apply_accepts_versioned_states_with_business_only_count_check(
    tmp_path: Path,
    starting_revision: str,
    expected_state: str,
) -> None:
    migration = _migration_module()
    source = (tmp_path / f"{expected_state}.db").resolve()
    _create_frozen_legacy_database(source)
    command = importlib.import_module("alembic.command")
    schema_migrations = importlib.import_module("app.core.schema_migrations")
    config = schema_migrations._config(source)
    command.upgrade(config, "head")
    if starting_revision != "head":
        command.downgrade(config, starting_revision)
    before_counts = _business_counts(source)
    before_status = schema_migrations.inspect_schema_status(source)
    assert before_status.state == expected_state
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    manifest = json.loads(Path(backup.manifest_path).read_text(encoding="utf-8"))
    assert "alembic_version" in manifest["source_table_counts"]

    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )

    candidate = Path(preparation.candidate_path)
    assert preparation.before_revision == before_status.current_revision
    assert preparation.head_revision == "20260711_0003"
    assert preparation.table_counts == before_counts
    assert _business_counts(candidate) == before_counts
    assert schema_migrations.inspect_schema_status(candidate).state == "ready"
    assert _business_counts(source) == before_counts
    assert schema_migrations.inspect_schema_status(source).state == expected_state


def test_guarded_apply_rejects_migration_that_changes_original_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "projection-tamper-source.db").resolve()
    _create_frozen_legacy_database(source)
    source_before = _logical_database_snapshot(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    real_upgrade = migration._upgrade_with_connection

    def upgrade_then_tamper(database_path, connection) -> None:
        real_upgrade(database_path, connection)
        connection.exec_driver_sql(
            "UPDATE app_config SET value='{\"migration_tamper\":true}' WHERE id=1201"
        )

    monkeypatch.setattr(migration, "_upgrade_with_connection", upgrade_then_tamper)
    with pytest.raises(RuntimeError, match="original projection digest"):
        migration.prepare_verified_schema_migration(
            source,
            Path(backup.manifest_path),
        )

    assert _logical_database_snapshot(source) == source_before


def test_cutover_candidate_business_check_uses_full_migrated_digest(
    tmp_path: Path,
) -> None:
    migration = _migration_module()
    source = (tmp_path / "candidate-full-digest-source.db").resolve()
    _create_frozen_legacy_database(source)
    source_before = _logical_database_snapshot(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    preparation = migration.prepare_verified_schema_migration(
        source,
        Path(backup.manifest_path),
    )
    candidate = Path(preparation.candidate_path)
    with closing(sqlite3.connect(candidate)) as connection:
        connection.execute(
            "UPDATE app_config SET value='{\"candidate_tamper\":true}' WHERE id=1201"
        )
        connection.commit()

    preparation_path = Path(preparation.preparation_path)
    payload = json.loads(preparation_path.read_text(encoding="utf-8"))
    payload["candidate_sha256"] = migration._sha256_file(candidate)
    preparation_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="candidate business digest changed"):
        migration.cutover_prepared_schema_migration(
            preparation_path,
            confirm_services_stopped=True,
        )

    assert _logical_database_snapshot(source) == source_before


def test_guarded_apply_rolls_back_schema_when_post_migration_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    migration = _migration_module()
    source = (tmp_path / "rollback-source.db").resolve()
    _create_frozen_legacy_database(source)
    before = _logical_database_snapshot(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )

    def fail_verification(connection):
        raise RuntimeError("simulated post-migration verification failure")

    monkeypatch.setattr(migration, "_connection_verification", fail_verification)

    with pytest.raises(RuntimeError, match="simulated post-migration"):
        migration.apply_verified_schema_migration(
            source,
            Path(backup.manifest_path),
        )

    assert _logical_database_snapshot(source) == before
    assert all(not path.exists() for path in _sidecar_paths(source))
    assert not list(
        Path(backup.manifest_path).parent.glob("migration-e-*.json")
    )
    assert not list(Path(backup.manifest_path).parent.glob(".mr-*.tmp"))


def test_guarded_apply_cli_emits_json_only_for_temporary_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    migration = _migration_module()
    source = (tmp_path / "cli-legacy-source.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )

    exit_code = migration.main(
        [
            "apply",
            "--database",
            str(source),
            "--manifest",
            backup.manifest_path,
            "--json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["database_path"] == str(source)
    assert payload["head_revision"] == "20260711_0003"
    assert payload["production_changed"] is False
    assert Path(payload["candidate_path"]).is_file()
    with closing(sqlite3.connect(source)) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='alembic_version'"
        ).fetchone() is None
    assert captured.err == ""


def test_apply_lock_failure_is_one_json_object_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migration = _migration_module()
    source = (tmp_path / "locked-apply.db").resolve()
    _create_frozen_legacy_database(source)
    backup = migration.create_verified_backup(
        source,
        tmp_path / "backup-root",
        system_drive="C:",
    )
    monkeypatch.setattr(migration, "MAINTENANCE_SQLITE_TIMEOUT_SECONDS", 0.05)
    locker = sqlite3.connect(source, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    try:
        exit_code = migration.main(
            [
                "apply",
                "--database",
                str(source),
                "--manifest",
                backup.manifest_path,
                "--json",
            ]
        )
    finally:
        locker.execute("ROLLBACK")
        locker.close()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code != 0
    assert payload["ok"] is False
    assert payload.get("migration_applied") is not True
    assert captured.err == ""
    assert "Traceback" not in captured.out


def test_lifespan_blocker_prevents_table_creation_and_hotspot_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    main_module = importlib.import_module("app.main")
    database_module = importlib.import_module("app.core.database")
    canonical = (tmp_path / "canonical.db").resolve()
    test_engine = database_module.build_engine(f"sqlite:///{canonical.as_posix()}")
    events: list[str] = []
    monkeypatch.setattr(main_module, "engine", test_engine, raising=False)
    monkeypatch.setattr(
        main_module,
        "audit_database_identity",
        lambda path: SimpleNamespace(
            blockers=["发现非空重复数据库：temporary-duplicate.db"],
            warnings=[],
        ),
        raising=False,
    )
    monkeypatch.setattr(
        main_module,
        "ensure_schema_ready",
        lambda _path: events.append("schema"),
        raising=False,
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
        with pytest.raises(RuntimeError) as exc_info:
            asyncio.run(run_lifespan())
    finally:
        test_engine.dispose()

    assert str(canonical) in str(exc_info.value)
    assert "temporary-duplicate.db" in str(exc_info.value)
    assert events == []


def test_lifespan_warning_continues_in_preflight_schema_hotspot_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    main_module = importlib.import_module("app.main")
    database_module = importlib.import_module("app.core.database")
    canonical = (tmp_path / "canonical.db").resolve()
    test_engine = database_module.build_engine(f"sqlite:///{canonical.as_posix()}")
    events: list[str] = []
    monkeypatch.setattr(main_module, "engine", test_engine, raising=False)

    def audit(path: Path):
        events.append("preflight")
        return SimpleNamespace(blockers=[], warnings=["保留零字节旧数据库"])

    monkeypatch.setattr(main_module, "audit_database_identity", audit, raising=False)
    monkeypatch.setattr(
        main_module,
        "ensure_schema_ready",
        lambda _path: events.append("schema"),
        raising=False,
    )
    monkeypatch.setattr(
        main_module,
        "_run_startup_hotspot_check_if_enabled",
        lambda: events.append("hotspot"),
    )

    class _RuntimeContext:
        async def __aenter__(self):
            events.append("material-runtime")
            return SimpleNamespace(startup_recovery_report=())

        async def __aexit__(self, *_exc_info):
            events.append("material-runtime-close")

    monkeypatch.setattr(
        main_module,
        "material_execution_runtime_lifespan",
        lambda _factory: _RuntimeContext(),
    )

    async def run_lifespan() -> None:
        async with main_module.lifespan(main_module.app):
            events.append("yield")

    try:
        with pytest.warns(RuntimeWarning, match="零字节"):
            asyncio.run(run_lifespan())
    finally:
        test_engine.dispose()

    assert events == [
        "preflight",
        "schema",
        "hotspot",
        "material-runtime",
        "yield",
        "material-runtime-close",
    ]


def test_lifespan_audits_and_checks_schema_on_the_exact_alternate_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    main_module = importlib.import_module("app.main")
    database_module = importlib.import_module("app.core.database")
    alternate_path = (tmp_path / "alternate-runtime.db").resolve()
    alternate_url = database_module.resolve_sqlite_database_url(
        "sqlite:///./alternate-runtime.db",
        tmp_path,
    )
    alternate_engine = database_module.build_engine(alternate_url)
    audited_paths: list[Path] = []
    schema_paths: list[Path] = []
    events: list[str] = []
    monkeypatch.setattr(main_module, "engine", alternate_engine, raising=False)
    monkeypatch.setattr(
        main_module,
        "resolve_runtime_identity",
        lambda *_args, **_kwargs: SimpleNamespace(
            canonical_database_path=tmp_path / "wrong-canonical.db"
        ),
        raising=False,
    )

    def audit(path: Path):
        audited_paths.append(Path(path))
        events.append("preflight")
        return SimpleNamespace(blockers=[], warnings=[])

    def ensure(path: Path) -> None:
        schema_paths.append(Path(path))
        events.append("schema")

    monkeypatch.setattr(main_module, "audit_database_identity", audit)
    monkeypatch.setattr(main_module, "ensure_schema_ready", ensure, raising=False)
    monkeypatch.setattr(
        main_module,
        "_run_startup_hotspot_check_if_enabled",
        lambda: events.append("hotspot"),
    )

    class _RuntimeContext:
        async def __aenter__(self):
            events.append("material-runtime")
            return SimpleNamespace(startup_recovery_report=())

        async def __aexit__(self, *_exc_info):
            events.append("material-runtime-close")

    monkeypatch.setattr(
        main_module,
        "material_execution_runtime_lifespan",
        lambda _factory: _RuntimeContext(),
    )

    async def run_lifespan() -> None:
        async with main_module.lifespan(main_module.app):
            events.append("yield")

    try:
        asyncio.run(run_lifespan())
    finally:
        alternate_engine.dispose()

    assert audited_paths == [alternate_path]
    assert schema_paths == [alternate_path]
    assert events == [
        "preflight",
        "schema",
        "hotspot",
        "material-runtime",
        "yield",
        "material-runtime-close",
    ]


@pytest.mark.parametrize(
    "invalid_url",
    ["sqlite:///:memory:", "postgresql://example.invalid/runtime"],
)
def test_lifespan_rejects_non_file_runtime_before_any_startup_action(
    invalid_url: str,
    monkeypatch: pytest.MonkeyPatch,
):
    main_module = importlib.import_module("app.main")
    database_module = importlib.import_module("app.core.database")
    if invalid_url.startswith("sqlite"):
        invalid_engine = database_module.build_engine(invalid_url)
    else:
        invalid_engine = SimpleNamespace(url=make_url(invalid_url))
    events: list[str] = []
    monkeypatch.setattr(main_module, "engine", invalid_engine, raising=False)
    monkeypatch.setattr(
        main_module,
        "audit_database_identity",
        lambda _path: events.append("preflight")
        or SimpleNamespace(blockers=[], warnings=[]),
    )
    monkeypatch.setattr(
        main_module,
        "ensure_schema_ready",
        lambda _path: events.append("schema"),
        raising=False,
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
        with pytest.raises(RuntimeError, match="生产运行数据库"):
            asyncio.run(run_lifespan())
    finally:
        if invalid_url.startswith("sqlite"):
            invalid_engine.dispose()

    assert events == []


def test_browser_cli_uses_preflight_then_shared_schema_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    browser_cli = importlib.import_module("app.hotspots.browser_cli")
    database_module = importlib.import_module("app.core.database")
    database_path = (tmp_path / "browser-cli.db").resolve()
    test_engine = database_module.build_engine(f"sqlite:///{database_path.as_posix()}")
    events: list[str] = []
    monkeypatch.setattr(browser_cli, "engine", test_engine, raising=False)
    monkeypatch.setattr(
        browser_cli,
        "audit_database_identity",
        lambda path: events.append("preflight")
        or SimpleNamespace(blockers=[], warnings=[]),
        raising=False,
    )
    monkeypatch.setattr(
        browser_cli,
        "ensure_schema_ready",
        lambda path: events.append("schema"),
        raising=False,
    )
    monkeypatch.setattr(
        browser_cli,
        "create_db_and_tables",
        lambda: events.append("legacy-create"),
        raising=False,
    )
    monkeypatch.setattr(
        browser_cli,
        "load_source_config",
        lambda path: events.append("config")
        or SimpleNamespace(browser_hotspot_sources=[]),
    )
    monkeypatch.setattr(sys, "argv", ["browser_cli"])

    try:
        exit_code = browser_cli.main()
    finally:
        test_engine.dispose()

    assert exit_code == 1
    assert events == ["preflight", "schema", "config"]


def test_browser_cli_preflight_blocker_prevents_schema_and_browser_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    browser_cli = importlib.import_module("app.hotspots.browser_cli")
    database_module = importlib.import_module("app.core.database")
    database_path = (tmp_path / "browser-blocked.db").resolve()
    test_engine = database_module.build_engine(f"sqlite:///{database_path.as_posix()}")
    events: list[str] = []
    monkeypatch.setattr(browser_cli, "engine", test_engine, raising=False)
    monkeypatch.setattr(
        browser_cli,
        "audit_database_identity",
        lambda path: events.append("preflight")
        or SimpleNamespace(blockers=["临时阻断"], warnings=[]),
        raising=False,
    )
    monkeypatch.setattr(
        browser_cli,
        "ensure_schema_ready",
        lambda path: events.append("schema"),
        raising=False,
    )
    monkeypatch.setattr(
        browser_cli,
        "create_db_and_tables",
        lambda: events.append("legacy-create"),
        raising=False,
    )
    monkeypatch.setattr(
        browser_cli,
        "load_source_config",
        lambda path: events.append("config")
        or SimpleNamespace(browser_hotspot_sources=[]),
    )
    monkeypatch.setattr(sys, "argv", ["browser_cli"])

    try:
        with pytest.raises(RuntimeError, match="临时阻断"):
            browser_cli.main()
    finally:
        test_engine.dispose()

    assert events == ["preflight"]
