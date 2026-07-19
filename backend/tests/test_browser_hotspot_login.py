from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.core.database import Base, get_db
from app.hotspots.browser_login import login_sources_from_config
from app.hotspots.config import load_source_config
from app.main import app


def test_default_browser_hotspot_sources_include_bilibili(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    sources = login_sources_from_config(load_source_config("config/source_config.yaml"))

    bilibili_sources = [source for source in sources if "B站" in source["name"]]

    assert bilibili_sources == [
        {
            "name": "B站热门网页版",
            "url": "https://www.bilibili.com/v/popular/all",
        },
        {
            "name": "B站科技区排行网页版",
            "url": "https://www.bilibili.com/v/popular/rank/tech",
        },
    ]


def test_login_sources_use_browser_hotspot_urls(tmp_path: Path):
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(
        """
browser_hotspot_sources:
  - provider: weibo_web
    name: 微博热搜网页版
    url: https://s.weibo.com/top/summary
    enabled: true
  - provider: douyin_web
    name: 抖音热点网页版
    url: https://www.douyin.com/hot
    enabled: true
  - provider: disabled_web
    name: 已停用来源
    url: https://example.com/disabled
    enabled: false
""".strip(),
        encoding="utf-8",
    )

    sources = login_sources_from_config(load_source_config(config_path))

    assert sources == [
        {"name": "微博热搜网页版", "url": "https://s.weibo.com/top/summary"},
        {"name": "抖音热点网页版", "url": "https://www.douyin.com/hot"},
    ]


def test_login_browser_api_starts_process_without_collecting(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine)
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(
        """
browser_hotspot_sources:
  - provider: xiaohongshu_web
    name: 小红书公开发现页
    url: https://www.xiaohongshu.com/explore
    enabled: true
""".strip(),
        encoding="utf-8",
    )
    launched: dict[str, object] = {}

    def fake_launch(*, profile_dir, sources):
        launched["profile_dir"] = str(profile_dir)
        launched["sources"] = sources
        return {"pid": 1234, "message": "登录浏览器已打开。"}

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setenv("SOURCE_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("BROWSER_HOTSPOT_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr("app.main.open_browser_hotspot_login_browser_process", fake_launch)
    get_settings.cache_clear()
    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.post("/api/automation/browser-hotspots/login-browser")
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert response.status_code == 200
    data = response.json()
    assert data["pid"] == 1234
    assert data["real_collection_started"] is False
    assert data["sources"] == [
        {"name": "小红书公开发现页", "url": "https://www.xiaohongshu.com/explore"}
    ]
    assert "profile" in str(launched["profile_dir"])
