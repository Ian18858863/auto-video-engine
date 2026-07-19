from __future__ import annotations

from datetime import datetime, timezone
from time import struct_time
from typing import Any

import feedparser

from app.hotspots.schemas import HotspotCandidate
from app.hotspots.sources.errors import HotspotSourceError, RateLimitedSourceError
from app.hotspots.sources.http_client import DEFAULT_PUBLIC_HEADERS, public_get


class RedditSource:
    def __init__(
        self,
        subreddit: str,
        limit: int = 20,
        timeout: float = 6.0,
        retries: int = 1,
    ):
        self.subreddit = subreddit
        self.limit = limit
        self.timeout = timeout
        self.retries = retries
        self.name = f"Reddit/r/{subreddit}"

    def fetch(self) -> list[HotspotCandidate]:
        try:
            return self._fetch_json()
        except RateLimitedSourceError as json_exc:
            json_rate_limited = True
            json_error: Exception = json_exc
        except Exception as json_exc:
            json_rate_limited = False
            json_error = json_exc

        try:
            return self._fetch_rss()
        except RateLimitedSourceError as rss_exc:
            raise RateLimitedSourceError(
                f"Reddit/r/{self.subreddit} 被限流：JSON 失败：{json_error}；RSS 失败：{rss_exc}"
            ) from rss_exc
        except Exception as rss_exc:
            if json_rate_limited:
                raise RateLimitedSourceError(
                    f"Reddit/r/{self.subreddit} 被限流：JSON 失败：{json_error}；RSS 失败：{rss_exc}"
                ) from rss_exc
            raise HotspotSourceError(
                f"Reddit 源不可用：JSON 失败：{json_error}；RSS 失败：{rss_exc}"
            ) from rss_exc

    def _fetch_json(self) -> list[HotspotCandidate]:
        url = f"https://www.reddit.com/r/{self.subreddit}/hot.json"
        response = public_get(
            url,
            label=f"Reddit/r/{self.subreddit} JSON",
            timeout=self.timeout,
            headers=DEFAULT_PUBLIC_HEADERS,
            params={"limit": self.limit},
            retries=self.retries,
        )
        data = response.json()

        posts = data.get("data", {}).get("children", [])
        candidates: list[HotspotCandidate] = []
        for rank, post in enumerate(posts, start=1):
            candidate = _to_candidate(self.subreddit, post.get("data", {}), rank)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _fetch_rss(self) -> list[HotspotCandidate]:
        url = f"https://www.reddit.com/r/{self.subreddit}/.rss"
        response = public_get(
            url,
            label=f"Reddit/r/{self.subreddit} RSS",
            timeout=self.timeout,
            headers=DEFAULT_PUBLIC_HEADERS,
            retries=self.retries,
        )
        feed = feedparser.parse(response.text)

        candidates: list[HotspotCandidate] = []
        for rank, entry in enumerate(feed.entries[: self.limit], start=1):
            title = str(getattr(entry, "title", "")).strip()
            link = str(getattr(entry, "link", "")).strip()
            if not title or not link:
                continue
            candidates.append(
                HotspotCandidate(
                    source_platform="Reddit",
                    source_type="community",
                    title=title,
                    url=link,
                    published_at=_entry_datetime(entry),
                    summary=str(getattr(entry, "summary", ""))[:100],
                    metrics={"rank": rank},
                    tags=[self.subreddit],
                    category=_category_for_subreddit(self.subreddit),
                    data_origin="real",
                    origin_note=f"Reddit public RSS r/{self.subreddit}",
                )
            )
        return candidates


def _to_candidate(subreddit: str, data: dict[str, Any], rank: int) -> HotspotCandidate | None:
    title = str(data.get("title", "")).strip()
    permalink = str(data.get("permalink", "")).strip()
    url = str(data.get("url") or f"https://www.reddit.com{permalink}")
    if not title or not url:
        return None
    published_at = None
    if data.get("created_utc"):
        published_at = datetime.fromtimestamp(float(data["created_utc"]), tz=timezone.utc)
    return HotspotCandidate(
        source_platform="Reddit",
        source_type="community",
        title=title,
        url=url,
        published_at=published_at,
        summary=str(data.get("selftext", ""))[:100],
        metrics={
            "rank": rank,
            "upvotes": int(data.get("ups") or 0),
            "comments": int(data.get("num_comments") or 0),
        },
        tags=[subreddit],
        category=_category_for_subreddit(subreddit),
        data_origin="real",
        origin_note=f"Reddit public JSON r/{subreddit}",
    )


def _entry_datetime(entry) -> datetime | None:
    published = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if isinstance(published, struct_time):
        return datetime(*published[:6], tzinfo=timezone.utc)
    return None


def _category_for_subreddit(subreddit: str) -> str:
    lowered = subreddit.lower()
    if lowered in {"artificialintelligence", "chatgpt"}:
        return "AI"
    if lowered in {"entrepreneur", "saas"}:
        return "商业"
    if lowered in {"productivity"}:
        return "生活方式"
    return "科技"
