from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from app.hotspots.sources.errors import HotspotSourceError, RateLimitedSourceError


DEFAULT_PUBLIC_HEADERS = {
    "User-Agent": "TrendVideoFactory/0.1 (+public-sources; no-login)",
    "Accept": "application/json, application/rss+xml, application/atom+xml, application/xml, text/xml, text/html, */*",
}

RETRYABLE_STATUS_CODES = {408, 425, 500, 502, 503, 504}


def public_get(
    url: str,
    *,
    label: str,
    timeout: float = 10.0,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    retries: int = 1,
) -> httpx.Response:
    merged_headers = {**DEFAULT_PUBLIC_HEADERS, **dict(headers or {})}
    last_error: Exception | None = None
    attempts = max(0, retries) + 1
    with httpx.Client(
        timeout=timeout,
        headers=merged_headers,
        follow_redirects=True,
    ) as client:
        for attempt in range(1, attempts + 1):
            try:
                response = client.get(url, params=params)
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt < attempts:
                    continue
                raise HotspotSourceError(f"{label} 请求超时") from exc
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < attempts:
                    continue
                raise HotspotSourceError(f"{label} 请求失败：{exc}") from exc
            if response.status_code == 429:
                raise RateLimitedSourceError(f"{label} 返回 HTTP 429")
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < attempts:
                last_error = HotspotSourceError(f"{label} 返回 HTTP {response.status_code}")
                continue
            if response.status_code >= 400:
                raise HotspotSourceError(f"{label} 返回 HTTP {response.status_code}")
            return response
    raise HotspotSourceError(f"{label} 请求失败：{last_error or '未知错误'}")
