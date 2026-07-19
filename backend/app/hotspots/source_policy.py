from __future__ import annotations

from app.models import HotItem


BROWSER_HOTSPOT_SOURCES = {
    "百度热搜网页版",
    "微博热搜网页版",
    "抖音热点网页版",
    "小红书公开发现页",
    "B站热门网页版",
    "B站科技区排行网页版",
    "财联社电报网页版",
}

PUBLIC_HOTLIST_SOURCES = {
    "百度热搜",
    "今日头条热榜",
    "B站热门",
    "B站全站排行",
    "B站科技区排行",
    *BROWSER_HOTSPOT_SOURCES,
}

INTERNAL_TEST_SOURCES = {
    "手动跑通测试",
}


def is_browser_hotspot_source(source_platform: str) -> bool:
    return source_platform in BROWSER_HOTSPOT_SOURCES


def is_public_hotlist_source(source_platform: str) -> bool:
    return source_platform in PUBLIC_HOTLIST_SOURCES


def is_internal_test_hotspot(item: HotItem) -> bool:
    source = (item.source_platform or "").strip()
    return source in INTERNAL_TEST_SOURCES or _is_mojibake_source(source) or item.data_origin in {
        "sample",
        "test",
        "manual_v1_test",
    }


def _is_mojibake_source(source_platform: str) -> bool:
    return bool(source_platform) and set(source_platform) <= {"?"}
