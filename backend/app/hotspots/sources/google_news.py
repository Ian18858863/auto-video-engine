from __future__ import annotations

from urllib.parse import quote_plus

from app.hotspots.sources.rss import RssSource


class GoogleNewsRssSource(RssSource):
    def __init__(self, keyword: str, limit: int = 30):
        self.keyword = keyword
        query = quote_plus(keyword)
        super().__init__(
            name="Google News",
            url=f"https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
            category=_category_for_keyword(keyword),
            tags=[keyword],
            limit=limit,
        )


def _category_for_keyword(keyword: str) -> str:
    lowered = keyword.lower()
    if "ai" in lowered or "人工智能" in keyword:
        return "AI"
    if "科技" in keyword:
        return "科技"
    if "商业" in keyword:
        return "商业"
    if "消费" in keyword:
        return "消费"
    if "财经" in keyword:
        return "财经"
    return "其他"
