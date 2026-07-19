from __future__ import annotations

import argparse
import warnings
from pathlib import Path

from app.core.config import get_settings, resolve_configured_path
from app.core.database import SessionLocal, engine, get_sqlite_database_path
from app.core.database_preflight import audit_database_identity
from app.core.schema_migrations import ensure_schema_ready
from app.hotspots.browser_collection import (
    PlaywrightBrowserHotspotCollector,
    run_browser_hotspot_collection,
)
from app.hotspots.config import load_source_config


def main() -> int:
    parser = argparse.ArgumentParser(description="低频采集公开热点并导出 CSV")
    parser.add_argument("--source", action="append", default=[], help="只采集指定来源")
    parser.add_argument("--force", action="store_true", help="忽略今日已采集限制")
    args = parser.parse_args()

    settings = get_settings()
    try:
        database_path = get_sqlite_database_path(engine)
    except ValueError as exc:
        raise RuntimeError(f"生产运行数据库配置无效，采集已停止：{exc}") from exc
    preflight = audit_database_identity(database_path)
    if preflight.blockers:
        raise RuntimeError(
            "数据库启动预检未通过。"
            f"规范路径：{database_path}；"
            "问题：" + "；".join(preflight.blockers)
        )
    for warning in preflight.warnings:
        warnings.warn(f"数据库预检警告：{warning}", RuntimeWarning, stacklevel=2)
    ensure_schema_ready(database_path)
    config = load_source_config(resolve_configured_path(settings.source_config_path))
    names = {name.strip().lower() for name in args.source if name.strip()}
    sources = [
        source
        for source in config.browser_hotspot_sources
        if not names
        or source.name.lower() in names
        or any(token in source.name.lower() for token in names)
    ]
    if not sources:
        print("没有找到可采集的浏览器热点来源，请检查 config/source_config.yaml。")
        return 1

    print("开始低频采集公开热点。")
    print("安全规则：不绕过验证码、不破解登录、不批量下载视频。")
    csv_path = resolve_configured_path(settings.browser_hotspot_csv_path)
    print(f"CSV 输出：{csv_path}")

    collector = PlaywrightBrowserHotspotCollector(
        profile_dir=resolve_configured_path(settings.browser_hotspot_profile_dir),
        headless=settings.browser_hotspot_headless,
        max_items=settings.browser_hotspot_max_items,
    )
    with SessionLocal() as session:
        result = run_browser_hotspot_collection(
            session,
            sources=sources,
            collector=collector,
            csv_path=csv_path,
            force=args.force,
        )

    print(result.message)
    if result.needs_login_sources:
        print("以下来源需要你在 Chrome 里重新登录后再试：")
        for source_name in result.needs_login_sources:
            print(f"- {source_name}")
    if result.failed_sources:
        print("失败来源已记录到 source_runs，不影响其他来源。")
    print(f"CSV 文件：{result.csv_path}")
    return 0 if not result.failed_sources else 2


if __name__ == "__main__":
    raise SystemExit(main())
