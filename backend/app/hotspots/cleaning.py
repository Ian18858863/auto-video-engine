from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from app.hotspots.schemas import HotspotCandidate


INTERNAL_MANUAL_URL_PREFIX = "internal://manual-hotspot/"
TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"fbclid", "gclid", "yclid", "mc_cid", "mc_eid"}
TITLE_STOPWORDS = {
    "the",
    "a",
    "an",
    "to",
    "of",
    "for",
    "and",
    "or",
    "in",
    "on",
    "with",
    "new",
    "latest",
    "breaking",
    "how",
    "why",
    "what",
    "is",
    "are",
    "了",
    "的",
    "是",
    "在",
    "和",
    "与",
    "最新",
    "突发",
}


def clean_text(value: str, max_length: int | None = None) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if max_length is not None and len(text) > max_length:
        return text[: max_length - 1].rstrip() + "…"
    return text


def normalize_url(url: str) -> str:
    raw_url = clean_text(url)
    parts = urlsplit(raw_url)
    query_items = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in TRACKING_KEYS and not key.startswith(TRACKING_PREFIXES)
    ]
    normalized_query = urlencode(query_items, doseq=True)
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/") or parts.path,
            normalized_query,
            "",
        )
    )


def make_internal_manual_url() -> str:
    return f"{INTERNAL_MANUAL_URL_PREFIX}{uuid4().hex}"


def is_internal_manual_url(url: str | None) -> bool:
    return (url or "").startswith(INTERNAL_MANUAL_URL_PREFIX)


def public_url(url: str | None) -> str:
    value = url or ""
    return "" if is_internal_manual_url(value) else value


def clean_candidate(candidate: HotspotCandidate) -> HotspotCandidate:
    return HotspotCandidate(
        source_platform=clean_text(candidate.source_platform, 100),
        source_type=clean_text(candidate.source_type, 50),
        title=clean_text(candidate.title, 300),
        url=normalize_url(candidate.url),
        published_at=candidate.published_at,
        summary=clean_text(candidate.summary, 100),
        metrics=candidate.metrics,
        tags=[clean_text(tag, 40) for tag in candidate.tags[:5] if clean_text(tag)],
        category=clean_text(candidate.category or "其他", 50),
        data_origin=clean_text(candidate.data_origin or "real", 30),
        origin_note=clean_text(candidate.origin_note, 200),
    )


def title_fingerprint(title: str) -> str:
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", " ", clean_text(title).lower())
    tokens = [token for token in normalized.split() if token not in TITLE_STOPWORDS]
    if not tokens:
        return normalized.replace(" ", "")[:160]
    return " ".join(tokens)[:160]
