from __future__ import annotations

from pathlib import Path

from app.hotspots.config import SourceConfig, load_source_config
from app.hotspots.sources.base import HotspotSource
from app.hotspots.sources.china_public import (
    BaiduHotSearchSource,
    BilibiliRankingSource,
    ToutiaoHotBoardSource,
)
from app.hotspots.sources.google_news import GoogleNewsRssSource
from app.hotspots.sources.hacker_news import HackerNewsSource
from app.hotspots.sources.reddit import RedditSource
from app.hotspots.sources.rss import RssSource


def build_hotspot_sources(
    config_path: str | Path,
    *,
    source_names: list[str] | None = None,
) -> list[HotspotSource]:
    source_config = load_source_config(config_path)
    sources = _build_sources(source_config)
    names = {name.strip().lower() for name in (source_names or []) if name.strip()}
    if not names:
        return sources
    return [
        source
        for source in sources
        if source.name.lower() in names
        or any(token in source.name.lower() for token in names)
    ]


def _build_sources(config: SourceConfig) -> list[HotspotSource]:
    sources: list[HotspotSource] = []
    for rss_source in config.rss_sources:
        if rss_source.enabled and rss_source.url:
            sources.append(
                RssSource(
                    name=rss_source.name,
                    url=rss_source.url,
                    category=rss_source.category,
                    tags=rss_source.tags,
                    limit=rss_source.limit,
                    timeout=rss_source.timeout,
                    retries=rss_source.retries,
                )
            )
    for china_source in config.china_public_sources:
        if not china_source.enabled:
            continue
        if china_source.provider == "baidu_hot":
            sources.append(BaiduHotSearchSource(limit=china_source.limit))
        elif china_source.provider == "toutiao_hot":
            sources.append(ToutiaoHotBoardSource(limit=china_source.limit))
        elif china_source.provider == "bilibili_ranking":
            sources.append(
                BilibiliRankingSource(
                    name=china_source.name,
                    rid=china_source.rid,
                    limit=china_source.limit,
                )
            )
    for keyword in config.google_news_keywords:
        if keyword.strip():
            sources.append(GoogleNewsRssSource(keyword.strip()))
    if config.hacker_news_enabled:
        sources.append(HackerNewsSource(limit=config.hacker_news_limit))
    if config.reddit_enabled:
        for subreddit in config.reddit_subreddits:
            if subreddit.strip():
                sources.append(RedditSource(subreddit.strip(), limit=config.reddit_limit))
    return sources
