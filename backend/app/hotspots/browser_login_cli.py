from __future__ import annotations

from app.core.config import get_settings, resolve_configured_path
from app.hotspots.browser_login import login_sources_from_config, open_login_browser
from app.hotspots.config import load_source_config


def main() -> int:
    settings = get_settings()
    sources = login_sources_from_config(
        load_source_config(resolve_configured_path(settings.source_config_path))
    )
    if not sources:
        print("没有可打开的浏览器采集来源，请检查 config/source_config.yaml。")
        return 1
    print("将打开热点采集专用 Chrome。")
    print("请在打开的窗口里登录微博、抖音、小红书等账号。")
    print("登录完成后，直接关闭这个 Chrome 窗口。")
    open_login_browser(
        profile_dir=resolve_configured_path(settings.browser_hotspot_profile_dir),
        sources=sources,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
