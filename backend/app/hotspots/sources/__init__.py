from app.hotspots.config import SourceConfig
from app.hotspots.sources.base import HotspotSource
from app.hotspots.sources.google_news import GoogleNewsRssSource
from app.hotspots.sources.hacker_news import HackerNewsSource
from app.hotspots.sources.reddit import RedditSource
from app.hotspots.sources.rss import RssSource


def build_sources_from_config(config: SourceConfig) -> list[HotspotSource]:
    sources: list[HotspotSource] = []
    for rss_source in config.rss_sources:
        if rss_source.enabled and rss_source.url:
            sources.append(
                RssSource(
                    name=rss_source.name,
                    url=rss_source.url,
                    category=rss_source.category,
                    tags=rss_source.tags,
                )
            )
    for keyword in config.google_news_keywords:
        sources.append(GoogleNewsRssSource(keyword=keyword))
    if config.hacker_news_enabled:
        sources.append(HackerNewsSource(limit=config.hacker_news_limit))
    if config.reddit_enabled:
        for subreddit in config.reddit_subreddits:
            sources.append(RedditSource(subreddit=subreddit, limit=config.reddit_limit))
    return sources


__all__ = [
    "GoogleNewsRssSource",
    "HackerNewsSource",
    "HotspotSource",
    "RedditSource",
    "RssSource",
    "build_sources_from_config",
]
