from __future__ import annotations

from copy import deepcopy
from datetime import timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import AppConfig
from app.models.entities import utc_now


ACCOUNT_STYLE_CONFIG_KEY = "account_style_library"


DEFAULT_ACCOUNT_STYLE: dict[str, Any] = {
    "account_positioning": "用大白话讲清楚新趋势、新环境、新风口、新工具背后的底层商业逻辑。",
    "target_audiences": [
        {"id": "students", "label": "小白学生", "enabled": True},
        {"id": "young_workers", "label": "刚毕业或准备职场的年轻人", "enabled": True},
        {"id": "office_workers", "label": "普通上班族", "enabled": True},
        {"id": "mothers", "label": "宝妈", "enabled": True},
        {"id": "side_hustle", "label": "想做副业、自媒体、AI 工具应用的人", "enabled": True},
        {
            "id": "trend_beginners",
            "label": "对财经、商业、科技趋势感兴趣但没有基础的人",
            "enabled": True,
        },
    ],
    "content_directions": [
        {"id": "ai_tools", "label": "AI 工具", "enabled": True},
        {"id": "business_hotspots", "label": "商业热点", "enabled": True},
        {"id": "tech_trends", "label": "科技趋势", "enabled": True},
        {"id": "consumer_changes", "label": "消费变化", "enabled": True},
        {"id": "finance_translation", "label": "财经新闻普通人转译", "enabled": True},
        {"id": "new_economy", "label": "新经济现象", "enabled": True},
        {"id": "trend_judgement", "label": "风口判断", "enabled": True},
        {"id": "checklists", "label": "收藏清单", "enabled": True},
        {"id": "business_logic", "label": "热点背后的商业逻辑", "enabled": True},
    ],
    "expression_styles": [
        {"id": "plain_language", "label": "大白话", "enabled": True},
        {"id": "dense", "label": "信息密度高", "enabled": True},
        {"id": "calm_direct", "label": "冷静直接", "enabled": True},
        {"id": "clear_judgement", "label": "有明确判断", "enabled": True},
        {"id": "not_expert", "label": "不装专家", "enabled": True},
        {"id": "no_anxiety", "label": "不制造焦虑", "enabled": True},
        {"id": "not_marketing", "label": "不像营销号", "enabled": True},
        {"id": "not_hard_finance", "label": "不像硬财经", "enabled": True},
        {"id": "not_news_anchor", "label": "不像新闻播报", "enabled": True},
        {"id": "easy_to_understand", "label": "普通人能听懂", "enabled": True},
        {"id": "not_too_shallow", "label": "专业人不觉得太蠢", "enabled": True},
    ],
    "banned_expressions": [
        {"id": "shock", "label": "震惊", "category": "营销号", "enabled": True},
        {"id": "explosive", "label": "炸裂", "category": "营销号", "enabled": True},
        {"id": "viral", "label": "全网疯传", "category": "营销号", "enabled": True},
        {"id": "save_now", "label": "赶紧收藏", "category": "强营销", "enabled": True},
        {"id": "must_grab", "label": "普通人必须抓住", "category": "焦虑话术", "enabled": True},
        {"id": "too_late", "label": "错过就晚了", "category": "焦虑话术", "enabled": True},
        {"id": "wealth_code", "label": "财富密码", "category": "收益暗示", "enabled": True},
        {"id": "guaranteed_profit", "label": "稳赚", "category": "投资风险", "enabled": True},
        {"id": "must_rise", "label": "必涨", "category": "投资风险", "enabled": True},
        {"id": "get_on_now", "label": "马上上车", "category": "投资风险", "enabled": True},
        {"id": "turnaround", "label": "普通人翻身机会", "category": "焦虑话术", "enabled": True},
        {"id": "change_destiny", "label": "彻底改变命运", "category": "夸大表达", "enabled": True},
        {
            "id": "unexplained_finance_terms",
            "label": "未解释的专业财经术语",
            "category": "理解门槛",
            "reason": "专业词必须配生活化类比或大白话解释。",
            "enabled": True,
        },
        {"id": "everyone_knows", "label": "大家都知道", "category": "默认常识句式", "enabled": True},
        {"id": "obvious", "label": "显而易见", "category": "默认常识句式", "enabled": True},
        {"id": "clearly", "label": "很明显", "category": "默认常识句式", "enabled": True},
        {"id": "no_need_to_say", "label": "这不用多说", "category": "默认常识句式", "enabled": True},
        {"id": "as_we_all_know", "label": "众所周知", "category": "默认常识句式", "enabled": True},
    ],
    "structure_preferences": [
        {
            "id": "beginner_translation",
            "name": "小白转译型",
            "framework": "事件影响 → 通俗解释 → 背后逻辑 → 注意提醒",
            "enabled": True,
        },
        {
            "id": "behind_hotspot",
            "name": "热点背后逻辑型",
            "framework": "表面事件 → 谁受益 → 为什么现在发生 → 普通人该看什么",
            "enabled": True,
        },
        {
            "id": "checklist",
            "name": "收藏清单型",
            "framework": "使用场景 → 3 个判断点 → 避坑提醒 → 总结保存价值",
            "enabled": True,
        },
        {
            "id": "avoid_trap",
            "name": "风口避坑型",
            "framework": "为什么火 → 真正有价值的部分 → 容易误解的部分 → 适合谁 / 不适合谁",
            "enabled": True,
        },
        {
            "id": "trend_explain",
            "name": "趋势解释型",
            "framework": "发生了什么 → 变化原因 → 影响谁 → 后续观察点",
            "enabled": True,
        },
        {
            "id": "business_breakdown",
            "name": "商业逻辑拆解型",
            "framework": "表面现象 → 赚钱逻辑 → 成本变化 → 谁受益 / 谁被影响 → 普通人判断标准",
            "enabled": True,
        },
    ],
    "opening_templates": [
        {
            "id": "hotspot_opening",
            "scene": "热点类开头",
            "template": "这件事表面上是……，但背后真正值得看的其实是……",
            "enabled": True,
        },
        {
            "id": "checklist_opening",
            "scene": "清单类开头",
            "template": "不用记所有细节，记住这几点就够了。",
            "enabled": True,
        },
        {
            "id": "avoid_trap_opening",
            "scene": "避坑类开头",
            "template": "这个风口先别急着跟，真正要看的是……",
            "enabled": True,
        },
        {
            "id": "trend_opening",
            "scene": "趋势类开头",
            "template": "这个变化不一定马上影响你，但它说明了一个趋势。",
            "enabled": True,
        },
        {
            "id": "beginner_opening",
            "scene": "小白转译类开头",
            "template": "这条新闻普通人不用全看，关键是看懂这个变化。",
            "enabled": True,
        },
    ],
    "ending_templates": [
        {
            "id": "hotspot_ending",
            "scene": "热点类结尾",
            "template": "总结普通人该看懂的逻辑。",
            "enabled": True,
        },
        {
            "id": "checklist_ending",
            "scene": "清单类结尾",
            "template": "强调这套判断标准以后还能用。",
            "enabled": True,
        },
        {
            "id": "avoid_trap_ending",
            "scene": "避坑类结尾",
            "template": "提醒适合谁、不适合谁。",
            "enabled": True,
        },
        {
            "id": "trend_ending",
            "scene": "趋势类结尾",
            "template": "给出后续观察点，不做绝对预测。",
            "enabled": True,
        },
        {
            "id": "beginner_ending",
            "scene": "小白转译类结尾",
            "template": "用一句话总结普通人真正该关注什么。",
            "enabled": True,
        },
    ],
    "validation_rules": {
        "professional_terms_require_explanation": True,
        "professional_terms": [
            "流动性收紧",
            "宏观环境变化",
            "商业模式",
            "用户增长",
            "降本增效",
            "平台生态",
            "技术突破",
            "风口",
        ],
        "plain_language_examples": [
            {
                "term": "流动性收紧",
                "plain_explanation": "市场上的钱变少了，大家借钱和投资都更谨慎了。",
            },
            {
                "term": "商业模式",
                "plain_explanation": "它到底靠什么赚钱。",
            },
        ],
        "default_common_knowledge_phrases": [
            "大家都知道",
            "显而易见",
            "很明显",
            "这不用多说",
            "众所周知",
        ],
        "checks": [
            "是否用了禁用表达",
            "是否出现未解释的专业财经术语",
            "是否出现默认常识句式",
            "是否符合目标受众",
            "是否太硬财经",
            "是否太营销号",
            "是否缺少大白话解释",
            "是否制造焦虑",
            "是否有投资建议风险",
        ],
    },
}


def get_account_style_library(session: Session) -> dict[str, Any]:
    item = _get_config_item(session)
    if item is None:
        now = utc_now()
        item = AppConfig(
            key=ACCOUNT_STYLE_CONFIG_KEY,
            value=deepcopy(DEFAULT_ACCOUNT_STYLE),
            updated_at=now,
        )
        session.add(item)
        session.commit()
        session.refresh(item)
    normalized = _normalize_style(item.value)
    return _with_runtime_meta(
        normalized,
        item.updated_at,
        using_default=normalized == _normalize_style(DEFAULT_ACCOUNT_STYLE),
    )


def save_account_style_library(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    value = _normalize_style(payload)
    item = _get_config_item(session)
    now = utc_now()
    if item is None:
        item = AppConfig(key=ACCOUNT_STYLE_CONFIG_KEY, value=value, updated_at=now)
    else:
        item.value = value
        item.updated_at = now
    session.add(item)
    session.commit()
    session.refresh(item)
    return _with_runtime_meta(_normalize_style(item.value), item.updated_at, using_default=False)


def reset_account_style_library(session: Session) -> dict[str, Any]:
    return save_account_style_library(session, deepcopy(DEFAULT_ACCOUNT_STYLE))


def get_account_style_context(session: Session) -> dict[str, Any]:
    style = get_account_style_library(session)
    return {
        "source": "account_style_library",
        "account_positioning": style["account_positioning"],
        "target_audiences": _enabled_labels(style["target_audiences"]),
        "content_directions": _enabled_labels(style["content_directions"]),
        "expression_styles": _enabled_labels(style["expression_styles"]),
        "banned_expressions": _enabled_labels(style["banned_expressions"]),
        "structure_preferences": [
            {
                "name": item["name"],
                "framework": item["framework"],
            }
            for item in style["structure_preferences"]
            if item.get("enabled", True)
        ],
        "opening_templates": [
            {
                "scene": item["scene"],
                "template": item["template"],
            }
            for item in style["opening_templates"]
            if item.get("enabled", True)
        ],
        "ending_templates": [
            {
                "scene": item["scene"],
                "template": item["template"],
            }
            for item in style["ending_templates"]
            if item.get("enabled", True)
        ],
        "validation_rules": style["validation_rules"],
    }


def _get_config_item(session: Session) -> AppConfig | None:
    return (
        session.query(AppConfig)
        .filter(AppConfig.key == ACCOUNT_STYLE_CONFIG_KEY)
        .one_or_none()
    )


def _normalize_style(payload: dict[str, Any]) -> dict[str, Any]:
    base = deepcopy(DEFAULT_ACCOUNT_STYLE)
    raw = payload if isinstance(payload, dict) else {}
    return {
        "account_positioning": _text(
            raw.get("account_positioning"),
            base["account_positioning"],
        ),
        "target_audiences": _normalize_label_items(
            raw.get("target_audiences"),
            base["target_audiences"],
        ),
        "content_directions": _normalize_label_items(
            raw.get("content_directions"),
            base["content_directions"],
        ),
        "expression_styles": _normalize_label_items(
            raw.get("expression_styles"),
            base["expression_styles"],
        ),
        "banned_expressions": _normalize_label_items(
            raw.get("banned_expressions"),
            base["banned_expressions"],
            keep_extra=("category", "reason"),
        ),
        "structure_preferences": _normalize_structure_items(
            raw.get("structure_preferences"),
            base["structure_preferences"],
        ),
        "opening_templates": _normalize_template_items(
            raw.get("opening_templates"),
            base["opening_templates"],
        ),
        "ending_templates": _normalize_template_items(
            raw.get("ending_templates"),
            base["ending_templates"],
        ),
        "validation_rules": _normalize_validation_rules(raw.get("validation_rules")),
    }


def _normalize_label_items(
    value: Any,
    defaults: list[dict[str, Any]],
    keep_extra: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    source = value if isinstance(value, list) else defaults
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(source):
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label"), "")
        if not label:
            continue
        normalized_item = {
            "id": _text(item.get("id"), f"item_{index + 1}"),
            "label": label,
            "enabled": bool(item.get("enabled", True)),
        }
        for key in keep_extra:
            normalized_item[key] = _text(item.get(key), "")
        normalized.append(normalized_item)
    return normalized or deepcopy(defaults)


def _normalize_structure_items(
    value: Any,
    defaults: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source = value if isinstance(value, list) else defaults
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(source):
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), "")
        framework = _text(item.get("framework"), "")
        if not name or not framework:
            continue
        normalized.append(
            {
                "id": _text(item.get("id"), f"structure_{index + 1}"),
                "name": name,
                "framework": framework,
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return normalized or deepcopy(defaults)


def _normalize_template_items(
    value: Any,
    defaults: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source = value if isinstance(value, list) else defaults
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(source):
        if not isinstance(item, dict):
            continue
        scene = _text(item.get("scene"), "")
        template = _text(item.get("template"), "")
        if not scene or not template:
            continue
        normalized.append(
            {
                "id": _text(item.get("id"), f"template_{index + 1}"),
                "scene": scene,
                "template": template,
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return normalized or deepcopy(defaults)


def _normalize_validation_rules(value: Any) -> dict[str, Any]:
    defaults = deepcopy(DEFAULT_ACCOUNT_STYLE["validation_rules"])
    if not isinstance(value, dict):
        return defaults
    return {
        "professional_terms_require_explanation": bool(
            value.get(
                "professional_terms_require_explanation",
                defaults["professional_terms_require_explanation"],
            )
        ),
        "professional_terms": _string_list(
            value.get("professional_terms"),
            defaults["professional_terms"],
        ),
        "plain_language_examples": _plain_language_examples(
            value.get("plain_language_examples"),
            defaults["plain_language_examples"],
        ),
        "default_common_knowledge_phrases": _string_list(
            value.get("default_common_knowledge_phrases"),
            defaults["default_common_knowledge_phrases"],
        ),
        "checks": _string_list(value.get("checks"), defaults["checks"]),
    }


def _plain_language_examples(value: Any, defaults: list[dict[str, str]]) -> list[dict[str, str]]:
    source = value if isinstance(value, list) else defaults
    examples: list[dict[str, str]] = []
    for item in source:
        if not isinstance(item, dict):
            continue
        term = _text(item.get("term"), "")
        explanation = _text(item.get("plain_explanation"), "")
        if term and explanation:
            examples.append({"term": term, "plain_explanation": explanation})
    return examples or deepcopy(defaults)


def _string_list(value: Any, defaults: list[str]) -> list[str]:
    if not isinstance(value, list):
        return defaults
    cleaned = [_text(item, "") for item in value]
    return [item for item in cleaned if item] or defaults


def _with_runtime_meta(
    style: dict[str, Any],
    updated_at: Any,
    *,
    using_default: bool,
) -> dict[str, Any]:
    if updated_at is not None and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    style["last_saved_at"] = updated_at.isoformat() if updated_at else ""
    style["is_active"] = True
    style["using_default"] = using_default
    return style


def _enabled_labels(items: list[dict[str, Any]]) -> list[str]:
    return [item["label"] for item in items if item.get("enabled", True)]


def _text(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text if text else fallback
