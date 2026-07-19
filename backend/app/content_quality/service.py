from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.account_style.service import get_account_style_context
from app.models import ContentQualityCheck, ProductionPipeline, ScriptAsset, TopicIdea


class PipelineScriptNotFoundError(ValueError):
    pass


PLAIN_LANGUAGE_MARKERS = [
    "说白了",
    "简单说",
    "换成人话",
    "也就是",
    "意思是",
    "可以理解成",
    "大白话",
    "打个比方",
]

MARKETING_PATTERNS = [
    "爆火",
    "疯传",
    "全网都在",
    "必须",
    "赶快",
    "立刻",
    "逆袭",
    "翻身",
    "错过",
]

INVESTMENT_RISK_PATTERNS = [
    "现在可以买",
    "马上上车",
    "买入",
    "卖出",
    "荐股",
    "荐币",
    "推荐基金",
    "股票代码",
    "稳赚",
    "必涨",
    "低风险高收益",
]

HARD_FINANCE_PATTERNS = [
    "宏观",
    "流动性",
    "货币政策",
    "资产负债表",
    "同比",
    "环比",
    "基点",
    "估值",
    "现金流折现",
    "GDP",
    "CPI",
    "研报",
]

JUDGEMENT_MARKERS = [
    "判断",
    "标准",
    "关键",
    "真正",
    "核心",
    "所以",
    "更合理",
    "我更倾向",
    "普通人该看",
    "可以看",
]

COLLECT_MARKERS = [
    "清单",
    "3 个",
    "三点",
    "判断标准",
    "注意",
    "避坑",
    "框架",
    "复用",
    "以后",
    "长期",
    "保存",
    "收藏",
]

POSITIONING_MARKERS = [
    "趋势",
    "环境",
    "风口",
    "工具",
    "商业",
    "逻辑",
    "普通人",
    "变化",
    "底层",
]


@dataclass(slots=True)
class QualityCheckPayload:
    checked_text: str
    source_title: str = "手动粘贴内容"
    source_type: str = "手动粘贴"
    pipeline_id: int | None = None
    script_asset_id: int | None = None


def check_manual_content(
    session: Session,
    payload: QualityCheckPayload,
) -> ContentQualityCheck:
    result = run_local_quality_rules(session, payload.checked_text)
    record = ContentQualityCheck(
        pipeline_id=payload.pipeline_id,
        script_asset_id=payload.script_asset_id,
        source_type=payload.source_type,
        source_title=payload.source_title,
        checked_text=payload.checked_text,
        result=result,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def check_pipeline_script(session: Session, pipeline_id: int) -> ContentQualityCheck:
    pipeline = session.get(ProductionPipeline, pipeline_id)
    if pipeline is None:
        raise PipelineScriptNotFoundError(f"流程不存在：{pipeline_id}")
    if pipeline.script_asset_id is None:
        raise PipelineScriptNotFoundError(f"该流程还没有脚本素材：{pipeline_id}")
    script = session.get(ScriptAsset, pipeline.script_asset_id)
    if script is None:
        raise PipelineScriptNotFoundError(f"脚本素材不存在：{pipeline.script_asset_id}")
    topic = session.get(TopicIdea, pipeline.topic_idea_id) if pipeline.topic_idea_id else None
    checked_text = build_script_check_text(topic, script)
    return check_manual_content(
        session,
        QualityCheckPayload(
            checked_text=checked_text,
            source_title=pipeline.project_name or (topic.title if topic else "脚本工作台内容"),
            source_type="脚本工作台",
            pipeline_id=pipeline.id,
            script_asset_id=script.id,
        ),
    )


def run_local_quality_rules(session: Session, text: str) -> dict[str, Any]:
    style = get_account_style_context(session)
    normalized_text = _normalize_text(text)
    banned = _match_enabled_banned(style, normalized_text)
    default_common = _matches(
        style["validation_rules"].get("default_common_knowledge_phrases", []),
        normalized_text,
    )
    unexplained_terms = _unexplained_terms(style, normalized_text)
    marketing_hits = _matches(MARKETING_PATTERNS, normalized_text)
    investment_hits = _matches(INVESTMENT_RISK_PATTERNS, normalized_text)
    hard_finance_hits = _matches(HARD_FINANCE_PATTERNS, normalized_text)
    has_plain_language = _has_plain_language(normalized_text, style)
    has_judgement = _contains_any(normalized_text, JUDGEMENT_MARKERS)
    collect_hits = _matches(COLLECT_MARKERS, normalized_text)
    fits_positioning = _contains_any(normalized_text, POSITIONING_MARKERS)
    style_rules_used = _style_rules_used(style)

    issues: list[str] = []
    suggestions: list[str] = []
    score = 100

    if banned:
        issues.append(f"命中禁用表达：{'、'.join(banned[:6])}")
        suggestions.append("删掉夸张、焦虑或强营销表达，改成冷静判断。")
        score -= min(30, 6 * len(banned))
    if default_common:
        issues.append(f"出现默认常识句式：{'、'.join(default_common[:6])}")
        suggestions.append("不要用“大家都知道”这类话，直接把背景讲给小白听。")
        score -= min(18, 6 * len(default_common))
    if unexplained_terms:
        issues.append(f"专业词需要解释：{'、'.join(unexplained_terms[:6])}")
        suggestions.append("专业财经词后面补一句“说白了/也就是”的大白话解释。")
        score -= min(30, 10 * len(unexplained_terms))
    if marketing_hits:
        issues.append("表达有营销号风险。")
        suggestions.append("降低煽动语气，用事实、原因和判断标准替代情绪词。")
        score -= 10
    if investment_hits:
        issues.append(f"存在投资建议风险：{'、'.join(investment_hits[:5])}")
        suggestions.append("删除买卖暗示，明确只做商业逻辑拆解，不做投资建议。")
        score -= 25
    if len(hard_finance_hits) >= 3 or (hard_finance_hits and not has_plain_language):
        issues.append("内容偏硬财经，普通小白可能听不懂。")
        suggestions.append("把术语改成生活化说法，再给一个普通人判断标准。")
        score -= 10
    if not has_plain_language and (unexplained_terms or hard_finance_hits):
        issues.append("复杂概念缺少大白话解释。")
        suggestions.append("加一句“换成人话就是……”，降低理解门槛。")
        score -= 10
    if not has_judgement:
        issues.append("缺少明确判断，容易变成复述新闻。")
        suggestions.append("补一个可复用判断框架，例如“普通人主要看这三点”。")
        score -= 10
    if not collect_hits:
        issues.append("收藏价值偏弱。")
        suggestions.append("加入清单、判断标准、避坑提醒或长期可复用信息。")
        score -= 8
    if not fits_positioning:
        issues.append("和账号定位的连接不够明显。")
        suggestions.append("补充新趋势、新工具或商业逻辑背后的普通人判断点。")
        score -= 8

    risk_level = _risk_level(banned, unexplained_terms, marketing_hits, investment_hits)
    collect_value = "高" if len(collect_hits) >= 3 else "中" if collect_hits else "低"
    score = max(0, min(100, score))
    publish_suggestion = _publish_suggestion(score, risk_level)
    max_issue = issues[0] if issues else "整体符合账号风格，发布前只需核对事实和来源。"

    return {
        "total_score": score,
        "publish_suggestion": publish_suggestion,
        "max_issue": max_issue,
        "issues": issues,
        "suggestions": _dedupe(suggestions)[:6],
        "matched_banned_expressions": _dedupe(banned + default_common),
        "professional_terms_need_explanation": unexplained_terms,
        "risk_level": risk_level,
        "collect_value": collect_value,
        "series_worthy": score >= 75 and risk_level != "高" and collect_value in {"中", "高"},
        "style_source": "账号风格库",
        "style_rules_used": style_rules_used,
        "checked_dimensions": [
            "禁用表达",
            "默认常识句式",
            "专业术语解释",
            "内容结构偏好",
            "开头偏好",
            "结尾偏好",
            "营销号风险",
            "投资建议风险",
            "目标观众理解门槛",
            "明确判断",
            "收藏价值",
            "账号定位",
        ],
        "local_rules_only": True,
        "real_ai_called": False,
    }


def build_script_check_text(topic: TopicIdea | None, script: ScriptAsset) -> str:
    parts: list[str] = []
    if topic is not None:
        parts.extend(
            [
                f"选题标题：{topic.title}",
                f"核心观点：{topic.core_view}",
                f"开头钩子：{topic.hook}",
            ]
        )
    parts.append(f"口播稿：{script.voiceover}")
    parts.append("分镜脚本：" + _jsonish_text(script.storyboard))
    parts.append("字幕内容：" + _jsonish_text(script.subtitles))
    parts.append("标题：" + _jsonish_text(script.title_options))
    parts.append("封面文案：" + _jsonish_text(script.cover_copy))
    parts.append("发布文案：" + _jsonish_text(script.publish_copy))
    return "\n".join(part for part in parts if part.strip())


def serialize_quality_check(record: ContentQualityCheck) -> dict[str, Any]:
    return {
        "id": record.id,
        "pipeline_id": record.pipeline_id,
        "script_asset_id": record.script_asset_id,
        "source_type": record.source_type,
        "source_title": record.source_title,
        "checked_text": record.checked_text,
        "created_at": record.created_at.isoformat(),
        **record.result,
    }


def _match_enabled_banned(style: dict[str, Any], text: str) -> list[str]:
    labels = [
        str(item)
        for item in style.get("banned_expressions", [])
        if item and str(item) != "未解释的专业财经术语"
    ]
    return _matches(labels, text)


def _matches(patterns: list[str], text: str) -> list[str]:
    return _dedupe([pattern for pattern in patterns if pattern and pattern in text])


def _unexplained_terms(style: dict[str, Any], text: str) -> list[str]:
    terms = style["validation_rules"].get("professional_terms", [])
    if not style["validation_rules"].get("professional_terms_require_explanation", True):
        return []
    has_generic_explanation = _contains_any(text, PLAIN_LANGUAGE_MARKERS)
    examples = style["validation_rules"].get("plain_language_examples", [])
    unexplained: list[str] = []
    for term in terms:
        if not term or term not in text:
            continue
        example_explained = any(
            isinstance(item, dict)
            and item.get("term") == term
            and str(item.get("plain_explanation", ""))[:8] in text
            for item in examples
        )
        if not has_generic_explanation and not example_explained:
            unexplained.append(term)
    return _dedupe(unexplained)


def _has_plain_language(text: str, style: dict[str, Any]) -> bool:
    if _contains_any(text, PLAIN_LANGUAGE_MARKERS):
        return True
    for item in style["validation_rules"].get("plain_language_examples", []):
        if isinstance(item, dict) and str(item.get("plain_explanation", ""))[:8] in text:
            return True
    return False


def _risk_level(
    banned: list[str],
    terms: list[str],
    marketing_hits: list[str],
    investment_hits: list[str],
) -> str:
    if investment_hits or len(banned) >= 4:
        return "高"
    if banned or terms or marketing_hits:
        return "中"
    return "低"


def _publish_suggestion(score: int, risk_level: str) -> str:
    if risk_level == "高" or score < 60:
        return "不建议发布"
    if score >= 80 and risk_level == "低":
        return "可以发布"
    return "建议修改"


def _contains_any(text: str, patterns: list[str]) -> bool:
    return any(pattern in text for pattern in patterns)


def _normalize_text(text: str) -> str:
    return str(text or "").strip()


def _jsonish_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key}：{_jsonish_text(item)}" for key, item in value.items())
    if isinstance(value, list):
        return " ".join(_jsonish_text(item) for item in value)
    return str(value or "")


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _style_rules_used(style: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_positioning": style.get("account_positioning", ""),
        "structure_preferences": style.get("structure_preferences", []),
        "opening_templates": style.get("opening_templates", []),
        "ending_templates": style.get("ending_templates", []),
        "banned_expressions": style.get("banned_expressions", []),
    }
