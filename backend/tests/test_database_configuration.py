from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, make_url, text
from sqlalchemy.exc import IntegrityError

from app.core import database as database_module
from app.core.config import Settings, get_settings, resolve_configured_path
from app.core.runtime_identity import resolve_runtime_identity


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_relative_sqlite_url_is_anchored_to_supplied_physical_root(tmp_path: Path):
    physical_root = tmp_path / "physical-repository"
    physical_root.mkdir()

    resolved = database_module.resolve_sqlite_database_url(
        "sqlite:///./trend_video_factory.db",
        physical_root,
    )

    assert Path(make_url(resolved).database) == (
        physical_root / "trend_video_factory.db"
    ).resolve()


def test_sqlite_resolution_preserves_driver_query_and_memory_database(tmp_path: Path):
    raw_url = "sqlite+pysqlite:///./nested/database.db?timeout=20&uri=false"

    resolved = make_url(
        database_module.resolve_sqlite_database_url(raw_url, tmp_path)
    )

    assert resolved.drivername == "sqlite+pysqlite"
    assert resolved.query == make_url(raw_url).query
    assert Path(resolved.database) == (tmp_path / "nested/database.db").resolve()
    assert (
        database_module.resolve_sqlite_database_url("sqlite:///:memory:", tmp_path)
        == "sqlite:///:memory:"
    )


def test_absolute_sqlite_url_remains_unchanged(tmp_path: Path):
    database_path = (tmp_path / "absolute.db").resolve()
    raw_url = _sqlite_url(database_path)

    assert (
        database_module.resolve_sqlite_database_url(raw_url, tmp_path / "other")
        == raw_url
    )


def test_non_sqlite_url_passes_through_unchanged(tmp_path: Path):
    raw_url = "postgresql+psycopg://example.invalid/database?sslmode=require"

    assert database_module.resolve_sqlite_database_url(raw_url, tmp_path) == raw_url


def test_relative_sqlite_file_uri_is_rejected_instead_of_using_cwd(tmp_path: Path):
    raw_url = "sqlite:///file:relative.db?mode=rwc&uri=true"

    with pytest.raises(ValueError, match="相对 file: SQLite URI"):
        database_module.resolve_sqlite_database_url(raw_url, tmp_path)


def test_absolute_and_memory_sqlite_file_uris_remain_unchanged(tmp_path: Path):
    absolute_database = (tmp_path / "absolute-uri.db").resolve().as_posix()
    absolute_url = f"sqlite:///file:{absolute_database}?mode=ro&uri=true"
    memory_url = "sqlite:///file:shared-memory?mode=memory&cache=shared&uri=true"

    assert (
        database_module.resolve_sqlite_database_url(absolute_url, tmp_path)
        == absolute_url
    )
    assert (
        database_module.resolve_sqlite_database_url(memory_url, tmp_path)
        == memory_url
    )


def test_settings_ignore_caller_working_directory_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    foreign_cwd = tmp_path / "caller"
    foreign_cwd.mkdir()
    (foreign_cwd / ".env").write_text(
        "DATABASE_URL=sqlite:///./wrong-working-directory.db\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(foreign_cwd)

    settings = Settings()

    assert settings.database_url != "sqlite:///./wrong-working-directory.db"


def test_c_alias_and_d_root_resolve_one_canonical_database_url():
    physical_root = Path(__file__).resolve().parents[2]
    c_alias = Path(
        r"C:/Users/陈子彦/Documents/Codex/2026-06-14/"
        r"superpowers-plugin-superpowers-openai-curated-trend"
    )
    if not c_alias.exists():
        pytest.skip("The executable C-path alias is not available on this host.")

    from_d = resolve_runtime_identity(physical_root)
    from_c = resolve_runtime_identity(c_alias)
    raw_url = "sqlite:///./trend_video_factory.db"

    assert database_module.resolve_sqlite_database_url(
        raw_url,
        from_d.physical_repo_root,
    ) == database_module.resolve_sqlite_database_url(
        raw_url,
        from_c.physical_repo_root,
    )


def test_global_engine_and_session_use_configured_resolved_database_path():
    identity = resolve_runtime_identity(Path(__file__))
    expected_url = database_module.resolve_sqlite_database_url(
        get_settings().database_url,
        identity.physical_repo_root,
    )

    assert Path(database_module.engine.url.database).is_absolute()
    assert Path(database_module.engine.url.database).resolve() == (
        Path(make_url(expected_url).database).resolve()
    )
    assert database_module.SessionLocal.kw["bind"] is database_module.engine


def test_runtime_setting_paths_anchor_relative_values_to_physical_d_root():
    identity = resolve_runtime_identity(Path(__file__))
    resolved = resolve_configured_path(Path("outputs/browser-profiles/hotspots"))
    assert resolved == (
        identity.physical_repo_root / "outputs/browser-profiles/hotspots"
    ).resolve()
    assert resolved.drive.upper() != "C:"


def test_runtime_setting_paths_reject_system_drive_absolute_values():
    with pytest.raises(ValueError, match="禁止写入系统盘"):
        resolve_configured_path(Path("C:/should-never-be-written"))


def test_engine_database_path_is_the_exact_bound_sqlite_file(tmp_path: Path):
    database_path = (tmp_path / "alternate-runtime.db").resolve()
    test_engine = database_module.build_engine(_sqlite_url(database_path))
    try:
        assert database_module.get_sqlite_database_path(test_engine) == database_path
    finally:
        test_engine.dispose()


def test_build_engine_enables_foreign_keys_on_every_sqlite_connection():
    test_engine = database_module.build_engine("sqlite:///:memory:")
    try:
        with test_engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    finally:
        test_engine.dispose()


def test_real_orphan_insert_is_rejected(tmp_path: Path):
    test_engine = database_module.build_engine(_sqlite_url(tmp_path / "foreign-keys.db"))
    try:
        with test_engine.begin() as connection:
            connection.execute(text("CREATE TABLE parents (id INTEGER PRIMARY KEY)"))
            connection.execute(
                text(
                    "CREATE TABLE children ("
                    "id INTEGER PRIMARY KEY, "
                    "parent_id INTEGER REFERENCES parents(id))"
                )
            )

        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO children (id, parent_id) VALUES (1, 999)")
                )
    finally:
        test_engine.dispose()


def test_build_engine_does_not_apply_sqlite_options_to_non_sqlite_urls(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_create_engine(database_url: str, **kwargs: object) -> object:
        captured["database_url"] = database_url
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(database_module, "create_engine", fake_create_engine)
    raw_url = "postgresql+psycopg://example.invalid/database"

    assert database_module.build_engine(raw_url) is sentinel
    assert captured == {"database_url": raw_url, "kwargs": {}}


def test_create_db_and_tables_uses_only_the_supplied_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target_engine = database_module.build_engine(_sqlite_url(tmp_path / "target.db"))
    monkeypatch.setattr(database_module, "engine", object())
    try:
        database_module.create_db_and_tables(target_engine=target_engine)

        assert "production_pipelines" in inspect(target_engine).get_table_names()
    finally:
        target_engine.dispose()


def test_legacy_create_helper_cannot_default_to_the_production_engine():
    with pytest.raises(TypeError):
        database_module.create_db_and_tables()


def test_compatibility_upgrade_uses_only_the_supplied_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target_engine = database_module.build_engine(_sqlite_url(tmp_path / "upgrade.db"))
    try:
        with target_engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE video_posts (id INTEGER PRIMARY KEY)")
            )
            connection.execute(
                text(
                    "CREATE TABLE hot_items ("
                    "id INTEGER PRIMARY KEY, data_origin TEXT, "
                    "duplicate_group_key TEXT, lifecycle_status TEXT)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE topic_ideas ("
                    "id INTEGER PRIMARY KEY, recommendation_bucket TEXT, "
                    "recommendation_score FLOAT)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE source_runs ("
                    "id INTEGER PRIMARY KEY, is_rate_limited BOOLEAN)"
                )
            )
        monkeypatch.setattr(database_module, "engine", object())

        database_module._upgrade_existing_tables(target_engine)

        columns = {
            column["name"]
            for column in inspect(target_engine).get_columns("video_posts")
        }
        assert {"pipeline_id", "updated_at"} <= columns
    finally:
        target_engine.dispose()
