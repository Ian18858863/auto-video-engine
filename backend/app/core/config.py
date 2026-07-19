from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.runtime_identity import require_non_system_drive, resolve_runtime_identity


_REPOSITORY_ENV_FILE = (
    resolve_runtime_identity(Path(__file__)).physical_repo_root / ".env"
)


def resolve_configured_path(value: Path | str) -> Path:
    """Resolve a settings path against the physical D: repository root.

    Settings intentionally keep a few relative defaults for backwards
    compatibility with isolated tests.  Runtime writers/readers must call
    this helper so launching through a C: junction or another working
    directory cannot create project payload beside the invoked alias.
    Absolute paths are still checked and system-drive paths fail closed.
    """

    # Anchor from this physical module rather than the process cwd.  A
    # launcher may have been started through a C: alias or an arbitrary shell
    # directory; neither may decide where project payload is written.
    identity = resolve_runtime_identity(Path(__file__))
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = identity.physical_repo_root / candidate
    candidate = candidate.resolve(strict=False)
    return require_non_system_drive(candidate)


class Settings(BaseSettings):
    app_name: str = "Auto Video Engine"
    database_url: str = "sqlite:///./trend_video_factory.db"
    data_dir: Path = Path("./data")
    export_dir: Path = Path("D:/短视频工厂/exports")
    breakdown_dir: Path = Path("./breakdowns")
    source_config_path: Path = Path("./config/source_config.yaml")
    material_root_dir: Path = Path("D:/短视频工厂")
    project_root_dir: Path = Path("D:/短视频工厂/项目")
    auto_material_root_dir: Path = Path("D:/素材")
    # Compatibility-only fields: keep accepting existing .env files, but the
    # production material executor and capability registry both derive their
    # canonical Key locations from auto_material_root_dir.
    pexels_api_key_path: Path = Path("D:/素材/自动下载/Pexels/API Key")
    pixabay_api_key_path: Path = Path("D:/素材/自动下载/Pixabay/API Key")
    pexels_max_candidates_per_keyword: int = 10
    pixabay_max_candidates_per_keyword: int = 10
    coverr_max_candidates_per_keyword: int = 10
    tts_provider: str = "windows_sapi"
    tts_voice_name: str = ""
    tts_rate: int = 0
    tts_volume: int = 90
    jianying_adapter_type: str = "desktop"
    jianying_desktop_app_path: Path = Path("")
    jianying_download_dir: Path = Path("D:/短视频工厂/备选素材/旧剪映下载")
    jianying_desktop_ui_config_path: Path = Path(
        "./config/jianying_ui_profile.yaml"
    )
    max_candidates_per_search: int = 5
    min_material_score: float = 60.0
    max_downloads_per_task: int = 1
    candidate_screenshot_dir: Path = Path("素材信息/候选截图")
    jianying_launch_timeout_seconds: float = 30.0
    jianying_download_timeout_seconds: float = 120.0
    admin_token: str = "dev-admin-token"
    ai_provider: str = "fake"
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = "fake-low-cost"
    ai_timeout: float = 60.0
    ai_max_retries: int = 2
    product_hunt_token: str = ""
    auto_hotspot_on_startup: bool = False
    auto_hotspot_interval_hours: int = 24
    auto_hotspot_max_topics: int = 70
    browser_hotspot_profile_dir: Path = Path("./outputs/browser-profiles/hotspots")
    browser_hotspot_csv_path: Path = Path("D:/短视频工厂/exports/热点采集/browser_hotspots.csv")
    browser_hotspot_headless: bool = False
    browser_hotspot_max_items: int = 10
    codex_skills_root_dir: Path = Path("D:/CodexData/.codex/skills")

    model_config = SettingsConfigDict(
        env_file=_REPOSITORY_ENV_FILE,
        env_file_encoding="utf-8",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
