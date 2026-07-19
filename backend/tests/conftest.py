from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[2]


def pytest_configure(config: Any) -> None:
    # Keep every generated test artifact on the dedicated D: data volume.
    # The project checkout may be read-only or junction-backed during the
    # migration, so a relative checkout temp directory is not a safe default.
    temp_root = Path(os.environ.get("CODEX_TEST_TEMP_ROOT", r"D:\CodexData\Temp"))
    temp_root.mkdir(exist_ok=True)
    config.option.basetemp = str(temp_root / f"pytest-{os.getpid()}-{uuid4().hex[:8]}")


@pytest.fixture(autouse=True)
def isolate_default_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Never let omitted runtime roots send a test into the real D: factory."""
    material_root = tmp_path / "default-material-root"
    project_root = tmp_path / "default-project-root"
    auto_material_root = tmp_path / "default-auto-material-root"
    project_root.mkdir(parents=True)
    monkeypatch.setenv("MATERIAL_ROOT_DIR", str(material_root))
    monkeypatch.setenv("PROJECT_ROOT_DIR", str(project_root))
    monkeypatch.setenv("AUTO_MATERIAL_ROOT_DIR", str(auto_material_root))

    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        yield project_root
    finally:
        get_settings.cache_clear()
