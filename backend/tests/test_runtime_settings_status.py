from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app import main as main_module


def test_runtime_settings_status_reports_resolved_non_system_paths(
    monkeypatch,
):
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            ai_provider="fake",
            data_dir=Path("outputs/data"),
            export_dir=Path("D:/runtime/exports"),
            breakdown_dir=Path("outputs/breakdowns"),
            source_config_path=Path("config/source_config.yaml"),
            material_root_dir=Path("D:/runtime/materials"),
            project_root_dir=Path("D:/runtime/projects"),
            jianying_download_dir=Path("D:/runtime/downloads"),
            candidate_screenshot_dir=Path("outputs/screenshots"),
        ),
    )

    result = main_module.runtime_settings_status()

    assert result["provider"]["status"] == "test_only"
    assert result["provider"]["production_allowed"] is False
    assert result["storage_policy"]["runtime_paths_are_resolved"] is True
    assert all(item["drive"] != "C:" for item in result["paths"] if item["path"])
    assert all("C:\\" not in item["path"] for item in result["paths"])


def test_runtime_settings_status_marks_system_drive_configuration_blocked(
    monkeypatch,
):
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(project_root_dir=Path("C:/project-payload")),
    )

    result = main_module.runtime_settings_status()
    project_path = next(item for item in result["paths"] if item["key"] == "PROJECT_ROOT_DIR")

    assert project_path["status"] == "blocked"
    assert project_path["path"] == ""


def test_runtime_settings_status_does_not_treat_configured_provider_as_verified(
    monkeypatch,
):
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            ai_provider="openai-compatible",
            ai_base_url="https://provider.example/v1",
            ai_api_key="secret-is-not-returned",
            ai_model="model-a",
        ),
    )

    result = main_module.runtime_settings_status()

    assert result["provider"]["status"] == "configured_unverified"
    assert result["provider"]["verified"] is False
    assert result["provider"]["production_allowed"] is False
    assert result["provider"]["secret_configured"] is True


def test_runtime_settings_status_separates_skill_and_provider_truth(
    tmp_path: Path,
    monkeypatch,
):
    skills_root = tmp_path / "skills"
    hyperframes = skills_root / "hyperframes" / "SKILL.md"
    hyperframes.parent.mkdir(parents=True)
    hyperframes.write_text(
        "---\nname: hyperframes\nversion: 1.2.3\ndescription: test\n---\n",
        encoding="utf-8",
    )
    material_root = tmp_path / "materials"
    pexels_key = material_root / "自动下载" / "Pexels" / "API Key"
    pexels_key.parent.mkdir(parents=True)
    pexels_key.write_text("pexels-secret-value", encoding="utf-8")
    decoy_pexels_key = tmp_path / "legacy-pexels-decoy.key"
    decoy_pixabay_key = tmp_path / "legacy-pixabay-decoy.key"
    decoy_pexels_key.write_text("legacy-pexels-canary", encoding="utf-8")
    decoy_pixabay_key.write_text("legacy-pixabay-canary", encoding="utf-8")

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            ai_provider="openai-compatible",
            ai_base_url="https://provider.example/v1",
            ai_api_key="ai-secret-value",
            ai_model="model-a",
            auto_material_root_dir=material_root,
            pexels_api_key_path=decoy_pexels_key,
            pixabay_api_key_path=decoy_pixabay_key,
            codex_skills_root_dir=skills_root,
        ),
    )

    result = main_module.runtime_settings_status()
    providers = {item["id"]: item for item in result["providers"]}
    skills = {item["id"]: item for item in result["skills"]}

    assert providers["script_ai"]["installed"] is True
    assert providers["script_ai"]["configured"] is True
    assert providers["script_ai"]["verified"] is False
    assert providers["script_ai"]["status"] == "configured_unverified"
    assert providers["script_ai"]["production_allowed"] is False
    assert providers["pexels"]["configured"] is True
    assert providers["pexels"]["verified"] is False
    assert providers["pixabay"]["status"] == "blocked_missing_config"

    assert skills["hyperframes"]["installed"] is True
    assert skills["hyperframes"]["configured"] is False
    assert skills["hyperframes"]["verified"] is False
    assert skills["hyperframes"]["status"] == "installed_not_integrated"
    assert skills["hyperframes"]["declared_version"] == "1.2.3"
    assert skills["video-use"]["installed"] is False
    assert skills["video-use"]["status"] == "not_installed"
    assert skills["hyperframes"]["last_verified_at"] is None

    serialized = json.dumps(
        {"providers": result["providers"], "skills": result["skills"]},
        ensure_ascii=False,
    )
    assert "ai-secret-value" not in serialized
    assert "pexels-secret-value" not in serialized
    assert "legacy-pexels-canary" not in serialized
    assert "legacy-pixabay-canary" not in serialized
    assert str(skills_root) not in serialized
    assert str(pexels_key) not in serialized
    assert str(decoy_pexels_key) not in serialized
    assert str(decoy_pixabay_key) not in serialized


def test_runtime_settings_status_keeps_fake_provider_test_only(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(ai_provider="fake"),
    )

    result = main_module.runtime_settings_status()
    script_ai = next(item for item in result["providers"] if item["id"] == "script_ai")

    assert script_ai["status"] == "test_only"
    assert script_ai["verified"] is False
    assert script_ai["production_allowed"] is False
    assert script_ai["fallback"] == "无生产级 fallback"


def test_runtime_settings_status_does_not_claim_partial_ai_config_is_configured(
    monkeypatch,
):
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            ai_provider="openai-compatible",
            ai_api_key="key-only-canary",
            ai_base_url="",
            ai_model="",
        ),
    )

    result = main_module.runtime_settings_status()
    script_ai = next(item for item in result["providers"] if item["id"] == "script_ai")

    assert script_ai["status"] == "blocked_missing_config"
    assert script_ai["secret_configured"] is True
    assert script_ai["configured"] is False
    assert script_ai["verified"] is False
    assert script_ai["production_allowed"] is False


def test_runtime_settings_status_keeps_blocked_skill_installation_unknown(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(codex_skills_root_dir=Path("C:/blocked-skills")),
    )

    result = main_module.runtime_settings_status()

    assert result["skills"]
    assert all(item["status"] == "blocked_path_policy" for item in result["skills"])
    assert all(item["installed"] is None for item in result["skills"])
    assert all(item["configured"] is None for item in result["skills"])


def test_runtime_settings_status_http_contract_hides_all_secret_material(
    tmp_path: Path,
    monkeypatch,
):
    material_root = tmp_path / "materials"
    pexels_key = material_root / "自动下载" / "Pexels" / "API Key"
    pixabay_key = material_root / "自动下载" / "Pixabay" / "API Key.env"
    pexels_key.parent.mkdir(parents=True)
    pixabay_key.parent.mkdir(parents=True)
    pexels_key.write_text("pexels-http-canary", encoding="utf-8")
    pixabay_key.write_text("PIXABAY_API_KEY=pixabay-http-canary", encoding="utf-8")
    skills_root = tmp_path / "skills"

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            ai_provider="openai-compatible",
            ai_base_url="https://provider.example/v1",
            ai_api_key="ai-http-canary",
            ai_model="model-http-canary",
            auto_material_root_dir=material_root,
            codex_skills_root_dir=skills_root,
        ),
    )

    response = TestClient(main_module.app).get("/api/settings/runtime-status")

    assert response.status_code == 200
    body = response.text
    assert "ai-http-canary" not in body
    assert "pexels-http-canary" not in body
    assert "pixabay-http-canary" not in body
    assert str(pexels_key) not in body
    assert str(pixabay_key) not in body
    payload = response.json()
    assert payload["providers"]
    assert payload["skills"]
    for capability in [*payload["providers"], *payload["skills"]]:
        if capability["production_allowed"]:
            assert capability["installed"] is True
            assert capability["configured"] is True
            assert capability["verified"] is True
