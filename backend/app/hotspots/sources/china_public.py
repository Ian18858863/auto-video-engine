from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from app.hotspots.cleaning import clean_text
from app.hotspots.schemas import HotspotCandidate
from app.hotspots.sources.errors import HotspotSourceError
from app.hotspots.sources.http_client import public_get


BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html,application/xhtml+xml,*/*",
}


class BaiduHotSearchSource:
    name = "百度热搜"

    def __init__(self, limit: int = 30, timeout: float = 10.0):
        self.limit = limit
        self.timeout = timeout

    def fetch(self) -> list[HotspotCandidate]:
        response = public_get(
            "https://top.baidu.com/board?tab=realtime",
            label="百度热搜",
            timeout=self.timeout,
            headers=BROWSER_HEADERS,
            retries=1,
        )
        return _parse_baidu_hot_search(response.text, limit=self.limit)


class ToutiaoHotBoardSource:
    name = "今日头条热榜"

    def __init__(self, limit: int = 30, timeout: float = 10.0):
        self.limit = limit
        self.timeout = timeout

    def fetch(self) -> list[HotspotCandidate]:
        response = public_get(
            "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
            label="今日头条热榜",
            timeout=self.timeout,
            headers=BROWSER_HEADERS,
            retries=1,
        )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise HotspotSourceError("今日头条热榜返回内容不可解析") from exc
        return _parse_toutiao_hot_board(payload, limit=self.limit)


class BilibiliRankingSource:
    def __init__(
        self,
        name: str = "B站全站排行",
        rid: int = 0,
        limit: int = 30,
        timeout: float = 10.0,
    ):
        self.name = name
        self.rid = rid
        self.limit = limit
        self.timeout = timeout

    def fetch(self) -> list[HotspotCandidate]:
        headers = {
            **BROWSER_HEADERS,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        }
        response = public_get(
            f"https://api.bilibili.com/x/web-interface/ranking/v2?rid={self.rid}&type=all",
            label=self.name,
            timeout=self.timeout,
            headers=headers,
            retries=1,
        )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise HotspotSourceError(f"{self.name} 返回内容不可解析") from exc
        if int(payload.get("code") or 0) != 0:
            raise HotspotSourceError(f"{self.name} 返回错误：{payload.get('message') or '未知错误'}")
        return _parse_bilibili_ranking(payload, source_name=self.name, limit=self.limit)


def _parse_baidu_hot_search(html: str, *, limit: int) -> list[HotspotCandidate]:
    match = re.search(r"<!--s-data:(.*?)-->", html, flags=re.S)
    if not match:
        raise HotspotSourceError("百度热搜页面缺少公开数据块")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise HotspotSourceError("百度热搜数据不可解析") from exc
    cards = payload.get("data", {}).get("cards", [])
    content = cards[0].get("content", []) if cards else []
    candidates: list[HotspotCandidate] = []
    for rank, item in enumerate(content[:limit], start=1):
        title = clean_text(str(item.get("word") or item.get("query") or ""))
        url = str(item.get("rawUrl") or item.get("appUrl") or item.get("url") or "")
        if not title or not url:
            continue
        candidates.append(
            HotspotCandidate(
                source_platform="百度热搜",
                source_type="hotlist",
                title=title,
                url=url,
                published_at=None,
                summary=clean_text(str(item.get("desc") or ""), 160),
                metrics={
                    "rank": rank,
                    "hot_score_raw": _safe_int(item.get("hotScore")),
                },
                tags=["国内热点", "百度热搜"],
                category="热点",
                data_origin="real",
                origin_note="百度热搜公开页面",
            )
        )
    return candidates


def _parse_toutiao_hot_board(
    payload: dict[str, Any],
    *,
    limit: int,
) -> list[HotspotCandidate]:
    rows = payload.get("data", [])
    candidates: list[HotspotCandidate] = []
    for rank, item in enumerate(rows[:limit], start=1):
        title = clean_text(str(item.get("Title") or item.get("QueryWord") or ""))
        url = str(item.get("Url") or "")
        if not title or not url:
            continue
        candidates.append(
            HotspotCandidate(
                source_platform="今日头条热榜",
                source_type="hotlist",
                title=title,
                url=url,
                published_at=None,
                summary=clean_text(str(item.get("Abstract") or item.get("LabelDesc") or ""), 160),
                metrics={
                    "rank": rank,
                    "hot_score_raw": _safe_int(item.get("HotValue")),
                    "cluster_id": str(item.get("ClusterIdStr") or item.get("ClusterId") or ""),
                },
                tags=["国内热点", "今日头条"],
                category="热点",
                data_origin="real",
                origin_note="今日头条公开热榜接口",
            )
        )
    return candidates


def _parse_bilibili_ranking(
    payload: dict[str, Any],
    *,
    source_name: str,
    limit: int,
) -> list[HotspotCandidate]:
    rows = payload.get("data", {}).get("list", [])
    candidates: list[HotspotCandidate] = []
    for rank, item in enumerate(rows[:limit], start=1):
        title = clean_text(str(item.get("title") or ""))
        bvid = str(item.get("bvid") or "")
        url = str(item.get("short_link_v2") or f"https://www.bilibili.com/video/{bvid}")
        if not title or not bvid:
            continue
        stat = item.get("stat", {}) if isinstance(item.get("stat"), dict) else {}
        published_at = None
        if item.get("pubdate"):
            published_at = datetime.fromtimestamp(int(item["pubdate"]), tz=timezone.utc)
        candidates.append(
            HotspotCandidate(
                source_platform=source_name,
                source_type="video_hotlist",
                title=title,
                url=url,
                published_at=published_at,
                summary=clean_text(str(item.get("desc") or ""), 160),
                metrics={
                    "rank": rank,
                    "views": _safe_int(stat.get("view") or stat.get("vv")),
                    "likes": _safe_int(stat.get("like")),
                    "comments": _safe_int(stat.get("reply")),
                    "shares": _safe_int(stat.get("share")),
                    "favorites": _safe_int(stat.get("favorite")),
                },
                tags=["国内热点", "B站", clean_text(str(item.get("tname") or ""), 20)],
                category=_bilibili_category(source_name, item),
                data_origin="real",
                origin_note=f"{source_name}公开排行榜 API",
            )
        )
    return candidates


def _bilibili_category(source_name: str, item: dict[str, Any]) -> str:
    if "科技" in source_name:
        return "科技"
    tname = str(item.get("tname") or "")
    if any(keyword in tname for keyword in ["科技", "数码"]):
        return "科技"
    if any(keyword in tname for keyword in ["财经", "商业"]):
        return "商业"
    if any(keyword in tname for keyword in ["知识", "社科", "教育"]):
        return "知识"
    return "国内热点"

def _safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0
