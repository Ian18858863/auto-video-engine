"""Public, bounded rights evidence for stock-media candidates.

This is deliberately not a blanket statement that a file is publishable.  It
records the provider page and licence snapshot separately from the per-use
review of people, brands, property and third-party audio.  CDN URLs, API keys
and raw API responses are intentionally absent because candidate payloads are
returned to the browser.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_REVIEW_FLAGS = (
    "identifiable_people",
    "brands_or_logos",
    "property_or_architecture",
    "music_or_third_party_audio",
)
_FLAG_VALUES = frozenset({"unknown", "yes", "no"})
_CLEARANCE_VALUES = frozenset(
    {"unknown", "required", "not_required", "obtained"}
)
_REVIEW_STATUSES = frozenset(
    {"unreviewed", "needs_review", "cleared_for_intended_use", "rejected"}
)
_MEDIA_TYPES = {
    "Pexels": frozenset({"video", "photo"}),
    "Pixabay": frozenset({"video", "photo", "illustration", "vector"}),
}
_PROVIDER_CONFIG = {
    "Pexels": {
        "host": "pexels.com",
        "license_name": "Pexels License",
        "license_url": "https://www.pexels.com/legal-pages/license/",
        "terms_url": (
            "https://help.pexels.com/hc/en-us/articles/"
            "900005880463-What-are-the-Terms-and-Conditions"
        ),
        "api_display_requirement": (
            "在使用 API 结果的界面显著链接至 Pexels，并尽可能标注创作者。"
        ),
    },
    "Pixabay": {
        "host": "pixabay.com",
        "license_name": "Pixabay Content License",
        "license_url": "https://pixabay.com/service/terms/",
        "terms_url": "https://pixabay.com/service/terms/",
        "api_display_requirement": (
            "API 搜索结果展示时标明素材来自 Pixabay；API 响应缓存 24 小时。"
        ),
    },
}


class StockRightsEvidenceError(ValueError):
    """Provider or review evidence is incomplete, unsafe or contradictory."""


def build_stock_rights_evidence(
    *,
    provider: str,
    provider_asset_id: str | int,
    media_type: str,
    asset_page_url: str,
    contributor_name: str,
    search_query: str,
    contributor_id: str | int | None = None,
    contributor_profile_url: str = "",
    observed_at: datetime | str | None = None,
) -> dict[str, Any]:
    provider_name = _provider(provider)
    config = _PROVIDER_CONFIG[provider_name]
    timestamp = _timestamp(observed_at or datetime.now(timezone.utc), "observed_at")
    contributor = _text(contributor_name, "contributor.name", required=True, limit=500)
    kind = _text(media_type, "media_type", required=True, limit=40).casefold()
    if kind not in _MEDIA_TYPES[provider_name]:
        raise StockRightsEvidenceError("media_type is not supported by provider")
    page_url = _public_provider_url(
        asset_page_url,
        provider_host=str(config["host"]),
        field="asset_page_url",
        required=True,
    )
    profile_url = _public_provider_url(
        contributor_profile_url,
        provider_host=str(config["host"]),
        field="contributor.profile_url",
        required=False,
    )
    credit_kind = "Video" if kind == "video" else "Photo"
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "provider": provider_name,
        "provider_asset_id": _text(
            provider_asset_id,
            "provider_asset_id",
            required=True,
            limit=200,
        ),
        "media_type": kind,
        "asset_page_url": page_url,
        "contributor": {
            "provider_user_id": _text(
                contributor_id,
                "contributor.provider_user_id",
                required=False,
                limit=200,
            ),
            "name": contributor,
            "profile_url": profile_url,
        },
        "license_snapshot": {
            "license_name": config["license_name"],
            "license_url": config["license_url"],
            "terms_url": config["terms_url"],
            "observed_at": timestamp,
            "terms_version_or_last_updated": "",
        },
        "attribution": {
            "license_required": False,
            "api_display_requirement": config["api_display_requirement"],
            "recommended_credit": f"{credit_kind} by {contributor} on {provider_name}",
        },
        "rights_review": {
            "status": "unreviewed",
            "intended_use": "short-video",
            "identifiable_people": "unknown",
            "brands_or_logos": "unknown",
            "property_or_architecture": "unknown",
            "music_or_third_party_audio": "unknown",
            "third_party_clearance": "unknown",
            "restriction_notes": [],
            "reviewed_by": "",
            "reviewed_at": "",
        },
        "source_record": {
            "search_query": _text(
                search_query,
                "source_record.search_query",
                required=True,
                limit=2_048,
            ),
            "record_checked_at": timestamp,
        },
    }
    return normalise_stock_rights_evidence(evidence)


def normalise_stock_rights_evidence(value: Any) -> dict[str, Any]:
    root = _object(
        value,
        "rights_evidence",
        {
            "schema_version",
            "provider",
            "provider_asset_id",
            "media_type",
            "asset_page_url",
            "contributor",
            "license_snapshot",
            "attribution",
            "rights_review",
            "source_record",
        },
    )
    if root["schema_version"] != 1:
        raise StockRightsEvidenceError("rights_evidence schema_version must be 1")
    provider = _provider(root["provider"])
    config = _PROVIDER_CONFIG[provider]
    media_type = _text(root["media_type"], "media_type", required=True, limit=40).casefold()
    if media_type not in _MEDIA_TYPES[provider]:
        raise StockRightsEvidenceError("media_type is not supported by provider")
    contributor = _object(
        root["contributor"],
        "contributor",
        {"provider_user_id", "name", "profile_url"},
    )
    licence = _object(
        root["license_snapshot"],
        "license_snapshot",
        {
            "license_name",
            "license_url",
            "terms_url",
            "observed_at",
            "terms_version_or_last_updated",
        },
    )
    attribution = _object(
        root["attribution"],
        "attribution",
        {"license_required", "api_display_requirement", "recommended_credit"},
    )
    review = _object(
        root["rights_review"],
        "rights_review",
        {
            "status",
            "intended_use",
            *_REVIEW_FLAGS,
            "third_party_clearance",
            "restriction_notes",
            "reviewed_by",
            "reviewed_at",
        },
    )
    source_record = _object(
        root["source_record"],
        "source_record",
        {"search_query", "record_checked_at"},
    )

    if licence["license_name"] != config["license_name"]:
        raise StockRightsEvidenceError("license_snapshot license_name is not canonical")
    if licence["license_url"] != config["license_url"]:
        raise StockRightsEvidenceError("license_snapshot license_url is not canonical")
    if licence["terms_url"] != config["terms_url"]:
        raise StockRightsEvidenceError("license_snapshot terms_url is not canonical")
    if attribution["license_required"] is not False:
        raise StockRightsEvidenceError("attribution license_required must be false")
    if attribution["api_display_requirement"] != config["api_display_requirement"]:
        raise StockRightsEvidenceError("attribution API requirement is not canonical")

    status = _text(review["status"], "rights_review.status", required=True, limit=80)
    if status not in _REVIEW_STATUSES:
        raise StockRightsEvidenceError("rights_review status is invalid")
    flags: dict[str, str] = {}
    for field in _REVIEW_FLAGS:
        flag = _text(review[field], f"rights_review.{field}", required=True, limit=20)
        if flag not in _FLAG_VALUES:
            raise StockRightsEvidenceError(f"rights_review {field} is invalid")
        flags[field] = flag
    clearance = _text(
        review["third_party_clearance"],
        "rights_review.third_party_clearance",
        required=True,
        limit=40,
    )
    if clearance not in _CLEARANCE_VALUES:
        raise StockRightsEvidenceError("rights_review third_party_clearance is invalid")
    reviewed_by = _text(
        review["reviewed_by"], "rights_review.reviewed_by", required=False, limit=200
    )
    raw_reviewed_at = _text(
        review["reviewed_at"], "rights_review.reviewed_at", required=False, limit=128
    )
    reviewed_at = _timestamp(raw_reviewed_at, "rights_review.reviewed_at") if raw_reviewed_at else ""
    if status == "cleared_for_intended_use":
        if any(value == "unknown" for value in flags.values()) or clearance == "unknown":
            raise StockRightsEvidenceError(
                "cleared rights_review cannot contain unknown review fields"
            )
        if clearance == "required":
            raise StockRightsEvidenceError(
                "cleared rights_review cannot have required third-party clearance"
            )
        if any(value == "yes" for value in flags.values()) and clearance != "obtained":
            raise StockRightsEvidenceError(
                "cleared rights_review with third-party subjects requires obtained clearance"
            )
        if not reviewed_by or not reviewed_at:
            raise StockRightsEvidenceError(
                "cleared rights_review requires reviewed_by and reviewed_at"
            )
    elif status == "unreviewed" and (reviewed_by or reviewed_at):
        raise StockRightsEvidenceError(
            "unreviewed rights_review cannot claim reviewer metadata"
        )
    elif status in {"needs_review", "rejected"} and (
        not reviewed_by or not reviewed_at
    ):
        raise StockRightsEvidenceError(
            f"{status} rights_review requires reviewed_by and reviewed_at"
        )

    notes = _string_list(review["restriction_notes"], "rights_review.restriction_notes")
    observed_at = _timestamp(licence["observed_at"], "license_snapshot.observed_at")
    record_checked_at = _timestamp(
        source_record["record_checked_at"], "source_record.record_checked_at"
    )
    canonical = {
        "schema_version": 1,
        "provider": provider,
        "provider_asset_id": _text(
            root["provider_asset_id"], "provider_asset_id", required=True, limit=200
        ),
        "media_type": media_type,
        "asset_page_url": _public_provider_url(
            root["asset_page_url"],
            provider_host=str(config["host"]),
            field="asset_page_url",
            required=True,
        ),
        "contributor": {
            "provider_user_id": _text(
                contributor["provider_user_id"],
                "contributor.provider_user_id",
                required=False,
                limit=200,
            ),
            "name": _text(
                contributor["name"], "contributor.name", required=True, limit=500
            ),
            "profile_url": _public_provider_url(
                contributor["profile_url"],
                provider_host=str(config["host"]),
                field="contributor.profile_url",
                required=False,
            ),
        },
        "license_snapshot": {
            "license_name": str(config["license_name"]),
            "license_url": str(config["license_url"]),
            "terms_url": str(config["terms_url"]),
            "observed_at": observed_at,
            "terms_version_or_last_updated": _text(
                licence["terms_version_or_last_updated"],
                "license_snapshot.terms_version_or_last_updated",
                required=False,
                limit=500,
            ),
        },
        "attribution": {
            "license_required": False,
            "api_display_requirement": str(config["api_display_requirement"]),
            "recommended_credit": _text(
                attribution["recommended_credit"],
                "attribution.recommended_credit",
                required=True,
                limit=1_000,
            ),
        },
        "rights_review": {
            "status": status,
            "intended_use": _text(
                review["intended_use"],
                "rights_review.intended_use",
                required=True,
                limit=500,
            ),
            **flags,
            "third_party_clearance": clearance,
            "restriction_notes": notes,
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
        },
        "source_record": {
            "search_query": _text(
                source_record["search_query"],
                "source_record.search_query",
                required=True,
                limit=2_048,
            ),
            "record_checked_at": record_checked_at,
        },
    }
    return deepcopy(canonical)


def review_stock_rights_evidence(
    value: Any,
    *,
    status: str,
    intended_use: str,
    identifiable_people: str,
    brands_or_logos: str,
    property_or_architecture: str,
    music_or_third_party_audio: str,
    third_party_clearance: str,
    restriction_notes: Sequence[str],
    reviewed_by: str,
    reviewed_at: datetime | str,
) -> dict[str, Any]:
    """Return a new evidence snapshot for one explicit intended-use review.

    Provider/license facts remain immutable.  Callers may update only the
    human review section, and may not reset an audited row to ``unreviewed``.
    The normalizer performs the final contradiction checks.
    """

    canonical = normalise_stock_rights_evidence(value)
    review_status = _text(
        status,
        "rights_review.status",
        required=True,
        limit=80,
    )
    if review_status not in {
        "needs_review",
        "cleared_for_intended_use",
        "rejected",
    }:
        raise StockRightsEvidenceError(
            "review status must be needs_review, cleared_for_intended_use, or rejected"
        )
    canonical["rights_review"] = {
        "status": review_status,
        "intended_use": _text(
            intended_use,
            "rights_review.intended_use",
            required=True,
            limit=500,
        ),
        "identifiable_people": identifiable_people,
        "brands_or_logos": brands_or_logos,
        "property_or_architecture": property_or_architecture,
        "music_or_third_party_audio": music_or_third_party_audio,
        "third_party_clearance": third_party_clearance,
        "restriction_notes": _string_list(
            restriction_notes,
            "rights_review.restriction_notes",
        ),
        "reviewed_by": _text(
            reviewed_by,
            "rights_review.reviewed_by",
            required=True,
            limit=200,
        ),
        "reviewed_at": _timestamp(
            reviewed_at,
            "rights_review.reviewed_at",
        ),
    }
    return normalise_stock_rights_evidence(canonical)


def _provider(value: Any) -> str:
    text = _text(value, "provider", required=True, limit=40).casefold()
    aliases = {"pexels": "Pexels", "pixabay": "Pixabay"}
    try:
        return aliases[text]
    except KeyError as exc:
        raise StockRightsEvidenceError("provider must be Pexels or Pixabay") from exc


def _object(value: Any, field: str, expected: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StockRightsEvidenceError(f"{field} must be an object")
    if set(value) != expected:
        raise StockRightsEvidenceError(f"{field} fields are invalid")
    return value


def _text(
    value: Any,
    field: str,
    *,
    required: bool,
    limit: int,
) -> str:
    if value is None and not required:
        return ""
    if isinstance(value, int) and not isinstance(value, bool):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise StockRightsEvidenceError(f"{field} must be a string")
    if required and not text:
        raise StockRightsEvidenceError(f"{field} is required")
    if len(text) > limit or _CONTROL_CHARS.search(text):
        raise StockRightsEvidenceError(f"{field} is invalid")
    return text


def _timestamp(value: datetime | str, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = _text(value, field, required=True, limit=128)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StockRightsEvidenceError(f"{field} must be an ISO-8601 timestamp") from exc
    else:
        raise StockRightsEvidenceError(f"{field} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StockRightsEvidenceError(f"{field} must include a timezone offset")
    return parsed.isoformat()


def _public_provider_url(
    value: Any,
    *,
    provider_host: str,
    field: str,
    required: bool,
) -> str:
    text = _text(value, field, required=required, limit=2_000)
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise StockRightsEvidenceError(f"{field} is not a valid provider URL") from exc
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not parsed.hostname
    ):
        raise StockRightsEvidenceError(f"{field} is not a valid provider URL")
    host = parsed.hostname.casefold().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
        address = ipaddress.ip_address(host)
    except UnicodeError as exc:
        raise StockRightsEvidenceError(f"{field} is not a valid provider URL") from exc
    except ValueError:
        address = None
    if address is not None or not (
        host == provider_host or host.endswith(f".{provider_host}")
    ):
        raise StockRightsEvidenceError(f"{field} is not a valid provider URL")
    return urlunsplit(("https", host, parsed.path or "/", "", ""))


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise StockRightsEvidenceError(f"{field} must be a list")
    if len(value) > 16:
        raise StockRightsEvidenceError(f"{field} exceeds its item limit")
    return [
        _text(item, f"{field}[]", required=True, limit=1_000)
        for item in value
    ]


__all__ = [
    "StockRightsEvidenceError",
    "build_stock_rights_evidence",
    "normalise_stock_rights_evidence",
    "review_stock_rights_evidence",
]
