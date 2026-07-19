from __future__ import annotations

from pathlib import Path

import pytest

from app.core.runtime_identity import (
    require_non_system_drive,
    resolve_runtime_identity,
)


def test_c_alias_and_d_path_resolve_same_physical_root():
    d_root = Path(__file__).resolve().parents[2]
    c_alias = Path(
        r"C:/Users/陈子彦/Documents/Codex/2026-06-14/"
        r"superpowers-plugin-superpowers-openai-curated-trend"
    )
    assert c_alias.exists()

    from_d = resolve_runtime_identity(d_root)
    from_c = resolve_runtime_identity(c_alias)

    assert from_d.physical_repo_root == from_c.physical_repo_root
    assert from_d.physical_repo_root.drive.upper() != "C:"
    assert from_c.is_alias is True


def test_research_workspace_is_not_treated_as_repo_alias():
    research = Path(r"C:/Users/陈子彦/Documents/Auto Video Engine")
    with pytest.raises(RuntimeError, match="Auto Video Engine 根目录"):
        resolve_runtime_identity(research)


def test_runtime_identity_builds_one_absolute_database_path():
    identity = resolve_runtime_identity(Path(__file__).resolve().parents[2])
    assert identity.canonical_database_path.is_absolute()
    assert (
        identity.canonical_database_path
        == identity.physical_repo_root / "trend_video_factory.db"
    )


def test_system_drive_is_rejected_without_hardcoding_d():
    with pytest.raises(ValueError, match="禁止写入系统盘"):
        require_non_system_drive(Path("C:/forbidden/project"))
    assert require_non_system_drive(Path("E:/approved/project")).drive == "E:"
