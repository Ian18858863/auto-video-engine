from __future__ import annotations

from datetime import datetime, timezone
from time import struct_time

import feedparser

from app.hotspots.cleaning import clean_text
from app.hotspots.schemas import HotspotCandidate
from app.hotspots.sources.errors import HotspotSourceError
from app.hotspots.sources.http_client import DEFAULT_PUBLIC_HEADERS, public_get


DEFAULT_HEADERS = {
    **DEFAULT_PUBLIC_HEADERS,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}


class RssSource:
    def __init__(
        self,
        name: str,
        url: str,
        category: str = "其他",
        tags: list[str] | None = None,
        limit: int = 30,
        timeout: float = 10.0,
        retries: int = 1,
    ):
        self.name = name
        self.url = url
        self.category = category
        self.tags = tags or []
        self.limit = limit
        self.timeout = timeout
        self.retries = retries

    def fetch(self) -> list[HotspotCandidate]:
        if not self.url:
            raise HotspotSourceError(f"RSS 源未配置 URL：{self.name}")
        response = public_get(
            self.url,
            label=f"RSS 源 {self.name}",
            timeout=self.timeout,
            headers=DEFAULT_HEADERS,
            retries=self.retries,
        )

        feed = feedparser.parse(response.content)
        if getattr(feed, "bozo", False) and not feed.entries:
            raise HotspotSourceError(f"RSS 源不可解析：{self.url}")

        candidates: list[HotspotCandidate] = []
        for rank, entry in enumerate(feed.entries[: self.limit], start=1):
            link = getattr(entry, "link", "")
            title = clean_text(getattr(entry, "title", ""))
            if not title or not link:
                continue
            candidates.append(
                HotspotCandidate(
                    source_platform=self.name,
                    source_type="news",
                    title=title,
                    url=link,
                    published_at=_entry_datetime(entry),
                    summary=clean_text(getattr(entry, "summary", ""), 100),
                    metrics={"rank": rank},
                    tags=self.tags,
                    category=self.category,
                    data_origin="real",
                    origin_note=f"公开 RSS：{self.url}",
                )
            )
        return candidates


def _entry_datetime(entry) -> datetime | None:
    published = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if isinstance(published, struct_time):
        return datetime(*published[:6], tzinfo=timezone.utc)
    return None
