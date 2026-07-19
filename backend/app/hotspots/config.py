from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.core.runtime_identity import resolve_runtime_identity


@dataclass(slots=True)
class RssSourceConfig:
    name: str
    url: str
    enabled: bool = True
    category: str = "其他"
    tags: list[str] = field(default_factory=list)
    limit: int = 30
    timeout: float = 10.0
    retries: int = 1


@dataclass(slots=True)
class ChinaPublicSourceConfig:
    provider: str
    name: str
    enabled: bool = True
    limit: int = 30
    rid: int = 0


@dataclass(slots=True)
class BrowserHotspotSourceConfig:
    provider: str
    name: str
    url: str
    enabled: bool = True
    category: str = "其他"
    tags: list[str] = field(default_factory=list)
    limit: int = 10
    timeout: float = 45.0


@dataclass(slots=True)
class SourceConfig:
    rss_sources: list[RssSourceConfig] = field(default_factory=list)
    china_public_sources: list[ChinaPublicSourceConfig] = field(default_factory=list)
    browser_hotspot_sources: list[BrowserHotspotSourceConfig] = field(
        default_factory=list
    )
    google_news_keywords: list[str] = field(default_factory=list)
    hacker_news_enabled: bool = True
    hacker_news_limit: int = 30
    reddit_enabled: bool = True
    reddit_subreddits: list[str] = field(default_factory=list)
    reddit_limit: int = 20


DEFAULT_REDDIT_SUBREDDITS = [
    "technology",
    "artificialintelligence",
    "Futurology",
    "Entrepreneur",
    "SaaS",
    "Productivity",
    "ChatGPT",
]


def load_source_config(path: str | Path) -> SourceConfig:
    config_path = Path(path)
    if not config_path.is_absolute():
        # Source configuration belongs to the physical repository.  Resolving
        # it from process cwd makes the same launcher behave differently when
        # started from ``backend/``, a desktop shortcut, or a C: alias.
        config_path = (
            resolve_runtime_identity(Path(__file__)).physical_repo_root
            / config_path
        ).resolve(strict=False)
    if not config_path.exists():
        return SourceConfig(reddit_subreddits=DEFAULT_REDDIT_SUBREDDITS.copy())

    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return SourceConfig(
        rss_sources=_load_rss_sources(data.get("rss_sources", [])),
        china_public_sources=_load_china_public_sources(
            data.get("china_public_sources", [])
        ),
        browser_hotspot_sources=_load_browser_hotspot_sources(
            data.get("browser_hotspot_sources", [])
        ),
        google_news_keywords=list(data.get("google_news_keywords", [])),
        hacker_news_enabled=bool(data.get("hacker_news", {}).get("enabled", True)),
        hacker_news_limit=int(data.get("hacker_news", {}).get("limit", 30)),
        reddit_enabled=bool(data.get("reddit", {}).get("enabled", True)),
        reddit_subreddits=list(data.get("reddit_subreddits", DEFAULT_REDDIT_SUBREDDITS)),
        reddit_limit=int(data.get("reddit", {}).get("limit", 20)),
    )


def _load_rss_sources(raw_sources: list[dict[str, Any]]) -> list[RssSourceConfig]:
    sources: list[RssSourceConfig] = []
    for raw_source in raw_sources:
        name = str(raw_source.get("name", "")).strip()
        url = str(raw_source.get("url", "")).strip()
        enabled = bool(raw_source.get("enabled", True))
        if not name:
            continue
        sources.append(
            RssSourceConfig(
                name=name,
                url=url,
                enabled=enabled,
                category=str(raw_source.get("category", "其他")),
                tags=list(raw_source.get("tags", [])),
                limit=int(raw_source.get("limit", 30)),
                timeout=float(raw_source.get("timeout", 10.0)),
                retries=int(raw_source.get("retries", 1)),
            )
        )
    return sources


def _load_browser_hotspot_sources(
    raw_sources: list[dict[str, Any]],
) -> list[BrowserHotspotSourceConfig]:
    sources: list[BrowserHotspotSourceConfig] = []
    for raw_source in raw_sources:
        provider = str(raw_source.get("provider", "")).strip()
        name = str(raw_source.get("name", "")).strip()
        url = str(raw_source.get("url", "")).strip()
        if not provider or not name or not url:
            continue
        sources.append(
            BrowserHotspotSourceConfig(
                provider=provider,
                name=name,
                url=url,
                enabled=bool(raw_source.get("enabled", True)),
                category=str(raw_source.get("category", "其他")),
                tags=list(raw_source.get("tags", [])),
                limit=int(raw_source.get("limit", 10)),
                timeout=float(raw_source.get("timeout", 45.0)),
            )
        )
    return sources


def _load_china_public_sources(
    raw_sources: list[dict[str, Any]],
) -> list[ChinaPublicSourceConfig]:
    sources: list[ChinaPublicSourceConfig] = []
    for raw_source in raw_sources:
        provider = str(raw_source.get("provider", "")).strip()
        name = str(raw_source.get("name", "")).strip()
        if not provider or not name:
            continue
        sources.append(
            ChinaPublicSourceConfig(
                provider=provider,
                name=name,
                enabled=bool(raw_source.get("enabled", True)),
                limit=int(raw_source.get("limit", 30)),
                rid=int(raw_source.get("rid", 0)),
            )
        )
    return sources
