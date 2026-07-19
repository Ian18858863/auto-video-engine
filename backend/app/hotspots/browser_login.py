from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.hotspots.config import SourceConfig
from app.core.runtime_identity import resolve_runtime_identity


def login_sources_from_config(config: SourceConfig) -> list[dict[str, str]]:
    return [
        {"name": source.name, "url": source.url}
        for source in config.browser_hotspot_sources
        if source.enabled and source.url
    ]


def open_browser_hotspot_login_browser_process(
    *,
    profile_dir: str | Path,
    sources: list[dict[str, str]],
) -> dict[str, Any]:
    if importlib.util.find_spec("playwright") is None:
        raise RuntimeError(
            "缺少 Playwright。请先运行：backend\\.venv\\Scripts\\python.exe -m pip install playwright"
        )
    if not sources:
        raise RuntimeError("没有可登录的浏览器采集来源，请检查 config/source_config.yaml。")

    # The API may be launched through the C: metadata/junction path.  The
    # child process must still import and run from the physical D: checkout.
    root_dir = resolve_runtime_identity(Path(__file__)).physical_repo_root
    env = os.environ.copy()
    backend_path = str(root_dir / "backend")
    env["PYTHONPATH"] = (
        backend_path
        if not env.get("PYTHONPATH")
        else f"{backend_path}{os.pathsep}{env['PYTHONPATH']}"
    )
    env["BROWSER_HOTSPOT_PROFILE_DIR"] = str(profile_dir)
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        [sys.executable, "-m", "app.hotspots.browser_login_cli"],
        cwd=str(root_dir),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    return {"pid": process.pid, "message": "登录浏览器已打开。"}


def open_login_browser(
    *,
    profile_dir: str | Path,
    sources: list[dict[str, str]],
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "缺少 Playwright。请先运行：backend\\.venv\\Scripts\\python.exe -m pip install playwright"
        ) from exc

    profile_path = Path(profile_dir)
    profile_path.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            channel="chrome",
            headless=False,
            locale="zh-CN",
            viewport={"width": 1440, "height": 1000},
        )
        try:
            first_page = context.pages[0] if context.pages else context.new_page()
            for index, source in enumerate(sources):
                page = first_page if index == 0 else context.new_page()
                try:
                    page.goto(source["url"], wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    # 登录窗口的目的只是保留账号状态，单页失败不关闭整个窗口。
                    continue
            print("热点采集登录浏览器已打开。登录完成后，请直接关闭这个 Chrome 窗口。")
            while context.pages:
                context.pages[0].wait_for_timeout(1000)
        finally:
            context.close()
