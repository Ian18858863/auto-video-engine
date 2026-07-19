from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.hotspots.schemas import HotspotCandidate
from app.hotspots.sources.errors import HotspotSourceError
from app.hotspots.sources.http_client import public_get


class HackerNewsSource:
    name = "Hacker News"

    def __init__(self, limit: int = 30, timeout: float = 10.0, retries: int = 1):
        self.limit = limit
        self.timeout = timeout
        self.retries = retries

    def fetch(self) -> list[HotspotCandidate]:
        response = public_get(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            label="Hacker News topstories",
            timeout=self.timeout,
            retries=self.retries,
        )
        story_ids = response.json()[: self.limit]
        candidates: list[HotspotCandidate] = []
        for rank, story_id in enumerate(story_ids, start=1):
            try:
                item_response = public_get(
                    f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json",
                    label=f"Hacker News item {story_id}",
                    timeout=self.timeout,
                    retries=0,
                )
            except HotspotSourceError:
                continue
            item = item_response.json() or {}
            candidate = _to_candidate(item, rank)
            if candidate is not None:
                candidates.append(candidate)
        return candidates


def _to_candidate(item: dict[str, Any], rank: int) -> HotspotCandidate | None:
    title = str(item.get("title", "")).strip()
    url = str(item.get("url") or f"https://news.ycombinator.com/item?id={item.get('id', '')}")
    if not title or not url:
        return None
    published_at = None
    if item.get("time"):
        published_at = datetime.fromtimestamp(int(item["time"]), tz=timezone.utc)
    return HotspotCandidate(
        source_platform="Hacker News",
        source_type="community",
        title=title,
        url=url,
        published_at=published_at,
        summary="",
        metrics={
            "rank": rank,
            "upvotes": int(item.get("score") or 0),
            "comments": int(item.get("descendants") or 0),
        },
        tags=["科技", "产品", "开发者"],
        category="科技",
        data_origin="real",
        origin_note="Hacker News public API",
    )
