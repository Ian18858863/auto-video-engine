from __future__ import annotations

from copy import deepcopy
from datetime import timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AppConfig
from app.models.entities import utc_now


TOPIC_FILTER_SETTINGS_KEY = "topic_filter_settings"

WEIGHT_KEYS = (
    "impact",
    "account_fit",
    "freshness_heat",
    "cross_source_resonance",
    "ordinary_relevance",
)

DEFAULT_TOPIC_FILTER_SETTINGS: dict[str, Any] = {
    "weights": {
        "impact": 25,
        "account_fit": 20,
        "freshness_heat": 20,
        "cross_source_resonance": 15,
        "ordinary_relevance": 20,
    },
    "current_period_limit": 5,
    "backup_pool_limit": 10,
    "source_limit": 3,
    "minimum_score": 40,
    "freshness_window_hours": 36,
    "bonus_rules": {
        "categories": [
            {"id": "category_ai", "label": "AI 加分", "category": "AI", "score": 3, "enabled": True},
            {"id": "category_tech", "label": "科技加分", "category": "科技", "score": 2, "enabled": True},
            {"id": "category_finance", "label": "财经加分", "category": "财经", "score": 2, "enabled": True},
            {"id": "category_consumer", "label": "消费加分", "category": "消费", "score": 2, "enabled": True},
        ],
        "keywords": [
            {"id": "keyword_ai", "label": "AI 关键词", "keyword": "AI", "score": 2, "enabled": True},
            {"id": "keyword_tool", "label": "工具关键词", "keyword": "工具", "score": 2, "enabled": True},
            {"id": "keyword_ordinary", "label": "普通人关键词", "keyword": "普通人", "score": 2, "enabled": True},
        ],
    },
    "last_saved_at": "",
    "is_active": True,
    "using_default": True,
}


def get_topic_filter_settings(session: Session) -> dict[str, Any]:
    row = (
        session.query(AppConfig)
        .filter(AppConfig.key == TOPIC_FILTER_SETTINGS_KEY)
        .one_or_none()
    )
    if row is None:
        settings = _normalize_settings(DEFAULT_TOPIC_FILTER_SETTINGS, using_default=True)
        row = AppConfig(
            key=TOPIC_FILTER_SETTINGS_KEY,
            value=settings,
            updated_at=utc_now(),
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            row = (
                session.query(AppConfig)
                .filter(AppConfig.key == TOPIC_FILTER_SETTINGS_KEY)
                .one_or_none()
            )
            if row is None:
                raise
        else:
            session.refresh(row)
    else:
        settings = _normalize_settings(row.value or {}, using_default=False)
        if settings != row.value:
            row.value = settings
            row.updated_at = utc_now()
            session.add(row)
            session.commit()
            session.refresh(row)

    result = deepcopy(row.value or DEFAULT_TOPIC_FILTER_SETTINGS)
    result["last_saved_at"] = _format_time(row.updated_at)
    result["is_active"] = True
    result.setdefault("using_default", False)
    return result


def save_topic_filter_settings(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    settings = _normalize_settings(payload, using_default=False)
    settings["using_default"] = False
    row = (
        session.query(AppConfig)
        .filter(AppConfig.key == TOPIC_FILTER_SETTINGS_KEY)
        .one_or_none()
    )
    if row is None:
        row = AppConfig(key=TOPIC_FILTER_SETTINGS_KEY, value=settings, updated_at=utc_now())
    else:
        row.value = settings
        row.updated_at = utc_now()
    session.add(row)
    session.commit()
    session.refresh(row)

    result = deepcopy(row.value)
    result["last_saved_at"] = _format_time(row.updated_at)
    result["is_active"] = True
    return result


def active_bonus_rules(settings: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bonus_rules = settings.get("bonus_rules") or {}
    categories = [
        rule
        for rule in bonus_rules.get("categories", [])
        if rule.get("enabled") and str(rule.get("category", "")).strip()
    ]
    keywords = [
        rule
        for rule in bonus_rules.get("keywords", [])
        if rule.get("enabled") and str(rule.get("keyword", "")).strip()
    ]
    return categories, keywords


def topic_bonus_for_text(
    *,
    category: str,
    text: str,
    settings: dict[str, Any],
) -> tuple[float, list[str]]:
    categories, keywords = active_bonus_rules(settings)
    bonus = 0.0
    reasons: list[str] = []
    normalized_text = (text or "").lower()
    normalized_category = (category or "").strip().lower()

    for rule in categories:
        if str(rule.get("category", "")).strip().lower() == normalized_category:
            score = _as_float(rule.get("score"), 0.0)
            bonus += score
            reasons.append(str(rule.get("label") or rule.get("category") or "分类加分"))

    for rule in keywords:
        keyword = str(rule.get("keyword", "")).strip()
        if keyword and keyword.lower() in normalized_text:
            score = _as_float(rule.get("score"), 0.0)
            bonus += score
            reasons.append(str(rule.get("label") or keyword or "关键词加分"))

    return round(max(0.0, bonus), 2), reasons


def _normalize_settings(payload: dict[str, Any], *, using_default: bool) -> dict[str, Any]:
    merged = deepcopy(DEFAULT_TOPIC_FILTER_SETTINGS)
    if isinstance(payload, dict):
        merged.update({key: value for key, value in payload.items() if key != "weights"})
        merged["weights"].update(payload.get("weights") or {})
        merged["bonus_rules"] = _normalize_bonus_rules(payload.get("bonus_rules") or merged["bonus_rules"])
    else:
        merged["bonus_rules"] = _normalize_bonus_rules(merged["bonus_rules"])

    weights = {
        key: _as_float(merged.get("weights", {}).get(key), DEFAULT_TOPIC_FILTER_SETTINGS["weights"][key])
        for key in WEIGHT_KEYS
    }
    total_weight = sum(weights.values())
    if abs(total_weight - 100) > 0.01:
        raise ValueError(f"五个评分权重总和必须等于 100，当前是 {round(total_weight, 2)}。")

    merged["weights"] = {
        key: int(value) if float(value).is_integer() else round(value, 2)
        for key, value in weights.items()
    }
    merged["current_period_limit"] = _bounded_int(merged.get("current_period_limit"), 1, 10, 5)
    merged["backup_pool_limit"] = _bounded_int(merged.get("backup_pool_limit"), 0, 30, 10)
    merged["source_limit"] = _bounded_int(merged.get("source_limit"), 1, 10, 3)
    merged["minimum_score"] = _bounded_int(merged.get("minimum_score"), 0, 100, 40)
    merged["freshness_window_hours"] = _bounded_int(merged.get("freshness_window_hours"), 1, 168, 36)
    merged["is_active"] = True
    merged["using_default"] = using_default
    merged["last_saved_at"] = str(merged.get("last_saved_at") or "")
    return merged


def _normalize_bonus_rules(raw: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "categories": [
            _normalize_rule(rule, rule_type="category", index=index)
            for index, rule in enumerate(raw.get("categories", []) if isinstance(raw, dict) else [])
        ],
        "keywords": [
            _normalize_rule(rule, rule_type="keyword", index=index)
            for index, rule in enumerate(raw.get("keywords", []) if isinstance(raw, dict) else [])
        ],
    }


def _normalize_rule(raw: dict[str, Any], *, rule_type: str, index: int) -> dict[str, Any]:
    field = "category" if rule_type == "category" else "keyword"
    value = str((raw or {}).get(field, "")).strip()
    return {
        "id": str((raw or {}).get("id") or f"{rule_type}_{index + 1}"),
        "label": str((raw or {}).get("label") or value or ("分类加分" if field == "category" else "关键词加分")),
        field: value,
        "score": _bounded_int((raw or {}).get("score"), 0, 30, 0),
        "enabled": bool((raw or {}).get("enabled", True)),
    }


def _bounded_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _format_time(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "tzinfo") and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
