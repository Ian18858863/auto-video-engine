from __future__ import annotations

import re
from dataclasses import dataclass

from app.hotspots.eligibility import is_today_recommendation_eligible
from app.models import HotItem
from app.topic_ideas.filter_settings import (
    DEFAULT_TOPIC_FILTER_SETTINGS,
    topic_bonus_for_text,
)


PUBLIC_RELEVANCE_KEYWORDS = {
    "价格",
    "消费",
    "用户",
    "普通人",
    "工作",
    "就业",
    "工资",
    "手机",
    "汽车",
    "电商",
    "教育",
    "医疗",
    "房",
    "搜索",
    "工具",
    "AI",
    "芯片",
    "支付",
    "外卖",
    "出行",
    "平台",
}
TREND_KEYWORDS = {
    "AI",
    "人工智能",
    "模型",
    "芯片",
    "机器人",
    "自动化",
    "搜索",
    "电商",
    "新能源",
    "消费",
    "价格战",
    "平台",
    "政策",
    "监管",
}
MAJOR_NEWS_KEYWORDS = {
    "首次",
    "全球",
    "监管",
    "禁令",
    "发布",
    "涨价",
    "降价",
    "裁员",
    "收购",
    "上市",
    "破产",
    "限制",
    "安全",
}
DOMESTIC_SOURCES = {
    "百度热搜",
    "百度热搜网页版",
    "今日头条热榜",
    "B站热门",
    "B站热门网页版",
    "B站全站排行",
    "B站科技区排行",
    "B站科技区排行网页版",
    "微博热搜网页版",
    "抖音热点网页版",
    "小红书公开发现页",
    "财联社电报网页版",
    "36氪",
    "IT之家",
    "量子位",
}
DOMESTIC_RELEVANCE_KEYWORDS = {
    "国内",
    "中国",
    "北京",
    "上海",
    "深圳",
    "广州",
    "杭州",
    "用户",
    "上班",
    "通勤",
    "外卖",
    "电商",
    "车企",
    "手机",
    "就业",
    "教育",
    "消费",
    "房贷",
    "楼市",
    "支付",
}
LOW_EVENT_VALUE_KEYWORDS = {
    "gta",
    "acg",
    "艺术图集",
    "展会",
    "晚报",
    "早报",
    "日报",
    "八卦",
    "谣言",
    "电视剧",
    "动漫",
    "修仙传",
    "官方声明",
}
ACCOUNT_MAINLINE_KEYWORDS = {
    "AI",
    "人工智能",
    "大模型",
    "智能体",
    "agent",
    "科技",
    "芯片",
    "机器人",
    "Robotaxi",
    "自动驾驶",
    "智驾",
    "商业",
    "公司",
    "平台",
    "电商",
    "外卖",
    "消费",
    "财经",
    "价格",
    "涨价",
    "降价",
    "裁员",
    "就业",
    "工具",
    "搜索",
    "流量",
    "广告",
    "商业模式",
}
ENTERTAINMENT_KEYWORDS = {
    "电影",
    "上映",
    "明星",
    "艺人",
    "综艺",
    "电视剧",
    "演唱会",
    "游戏",
    "手游",
    "电竞",
    "体育",
    "比赛",
    "八卦",
}
ENTERTAINMENT_BUSINESS_CONTEXT = {
    "平台",
    "票房",
    "院线",
    "流媒体",
    "商业",
    "广告",
    "AI",
    "人工智能",
    "影视工业",
    "内容产业",
    "商业模式",
    "公司战略",
}


@dataclass(slots=True)
class EventScore:
    total: float
    base_total: float
    bonus_total: float
    bonus_reasons: list[str]
    impact: float
    account_fit: float
    freshness_heat: float
    cross_source_resonance: float
    ordinary_relevance: float
    risk: float
    fits_collection: bool
    can_single_deep_dive: bool


def event_score(
    hot_item: HotItem,
    *,
    source_count: int = 1,
    settings: dict | None = None,
) -> EventScore:
    settings = settings or DEFAULT_TOPIC_FILTER_SETTINGS
    text = f"{hot_item.title} {hot_item.summary} {' '.join(hot_item.tags or [])}"
    domestic_relevance = _domestic_relevance_score(hot_item, text)
    account_categories = {"社会", "财经", "科技", "消费", "AI", "国际", "生活", "商业"}
    account_fit = _bounded(
        hot_item.topic_score * 0.35
        + _keyword_bonus(text, ACCOUNT_MAINLINE_KEYWORDS, 7)
        + _category_bonus(hot_item.category, account_categories, 18)
    )
    ordinary_relevance = _bounded(
        hot_item.topic_score * 0.34
        + hot_item.save_score * 0.22
        + hot_item.hot_score * 0.08
        + _keyword_bonus(text, PUBLIC_RELEVANCE_KEYWORDS, 6)
        + _category_bonus(hot_item.category, {"社会", "财经", "科技", "消费", "AI", "生活"}, 10)
        + domestic_relevance * 0.22
    )
    impact = _bounded(
        hot_item.hot_score * 0.5
        + hot_item.topic_score * 0.25
        + hot_item.freshness_score * 0.15
        + _keyword_bonus(text, MAJOR_NEWS_KEYWORDS, 5)
    )
    freshness_heat = _bounded(
        hot_item.freshness_score * 0.45
        + hot_item.hot_score * 0.28
        + hot_item.topic_score * 0.12
        + _keyword_bonus(text, TREND_KEYWORDS, 5)
    )
    cross_source_resonance = _cross_source_resonance_score(source_count)
    risk = _bounded(hot_item.risk_score)
    weights = settings.get("weights", DEFAULT_TOPIC_FILTER_SETTINGS["weights"])
    base_total = (
        impact * (float(weights.get("impact", 25)) / 100)
        + account_fit * (float(weights.get("account_fit", 20)) / 100)
        + freshness_heat * (float(weights.get("freshness_heat", 20)) / 100)
        + cross_source_resonance * (float(weights.get("cross_source_resonance", 15)) / 100)
        + ordinary_relevance * (float(weights.get("ordinary_relevance", 20)) / 100)
        - risk * 0.12
    )
    if any(keyword in text.lower() for keyword in LOW_EVENT_VALUE_KEYWORDS):
        base_total -= 18
    if _is_low_value_entertainment(text):
        base_total -= 35
        ordinary_relevance = min(ordinary_relevance, 45)
        account_fit = min(account_fit, 40)
        freshness_heat = min(freshness_heat, 35)
    if not is_today_recommendation_eligible(hot_item):
        base_total -= 12
    bonus_total, bonus_reasons = topic_bonus_for_text(
        category=hot_item.category,
        text=text,
        settings=settings,
    )
    total = base_total + bonus_total
    total = round(_bounded(total), 2)
    base_total = round(_bounded(base_total), 2)
    fits_collection = (
        is_today_recommendation_eligible(hot_item)
        and account_fit >= 42
        and ordinary_relevance >= 45
        and impact >= 35
        and risk < 60
    )
    can_single_deep_dive = (
        total >= 88
        and ordinary_relevance >= 72
        and impact >= 78
        and account_fit >= 72
        and risk < 35
        and _has_keyword(text, MAJOR_NEWS_KEYWORDS)
    )
    return EventScore(
        total=total,
        base_total=base_total,
        bonus_total=round(bonus_total, 2),
        bonus_reasons=bonus_reasons,
        impact=round(impact, 2),
        account_fit=round(account_fit, 2),
        freshness_heat=round(freshness_heat, 2),
        cross_source_resonance=round(cross_source_resonance, 2),
        ordinary_relevance=round(ordinary_relevance, 2),
        risk=round(risk, 2),
        fits_collection=fits_collection,
        can_single_deep_dive=can_single_deep_dive,
    )


def _domestic_relevance_score(hot_item: HotItem, text: str) -> float:
    score = 0.0
    if hot_item.source_platform in DOMESTIC_SOURCES:
        score += 45
    if re.search(r"[\u4e00-\u9fff]", text):
        score += 15
    score += _keyword_bonus(text, DOMESTIC_RELEVANCE_KEYWORDS, 5)
    return _bounded(score)


def _is_low_value_entertainment(text: str) -> bool:
    if not _has_keyword(text, ENTERTAINMENT_KEYWORDS):
        return False
    return not _has_keyword(text, ENTERTAINMENT_BUSINESS_CONTEXT)


def is_low_value_entertainment_item(hot_item: HotItem) -> bool:
    text = f"{hot_item.category} {hot_item.title} {hot_item.summary} {' '.join(hot_item.tags or [])}"
    return _is_low_value_entertainment(text)


def event_title(title: str) -> str:
    cleaned = _normalize_title(title)
    if not cleaned:
        return "今日事件"
    return _human_event_title(cleaned)


def event_title_for_hotspot(hot_item: HotItem) -> str:
    text = _normalize_title(
        f"{hot_item.title} {hot_item.summary} {' '.join(hot_item.tags or [])}",
    )
    if not text:
        return "今日事件"
    return _human_event_title(text)


def _human_event_title(text: str) -> str:
    cleaned = _normalize_title(text)
    if _is_low_value_entertainment(cleaned):
        return "娱乐消息"
    lowered = cleaned.lower()
    for keywords, label in HUMAN_EVENT_RULES:
        if all(keyword.lower() in lowered for keyword in keywords):
            return label
    for keywords, label in HUMAN_EVENT_ANY_RULES:
        if any(keyword.lower() in lowered for keyword in keywords):
            return label
    structured = _structured_event_title(cleaned)
    if structured:
        return structured
    phrase = _best_phrase(cleaned)
    if len(phrase) <= 14 and not _is_weak_phrase(phrase):
        return phrase
    compact = _compact_phrase(phrase)
    return compact if compact else phrase


def score_breakdown_dict(score: EventScore) -> dict[str, float]:
    return {
        "impact": score.impact,
        "account_fit": score.account_fit,
        "freshness_heat": score.freshness_heat,
        "cross_source_resonance": score.cross_source_resonance,
        "ordinary_relevance": score.ordinary_relevance,
        "risk": score.risk,
        "base_total": score.base_total,
        "bonus_total": score.bonus_total,
        "total": score.total,
    }


def _cross_source_resonance_score(source_count: int) -> float:
    if source_count >= 4:
        return 92.0
    if source_count == 3:
        return 84.0
    if source_count == 2:
        return 76.0
    return 38.0


def _keyword_bonus(text: str, keywords: set[str], unit: float) -> float:
    matches = sum(1 for keyword in keywords if keyword and keyword in text)
    return min(matches * unit, 28)


def _has_keyword(text: str, keywords: set[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _category_bonus(category: str, categories: set[str], bonus: float) -> float:
    return bonus if category in categories else 0


def _bounded(value: float) -> float:
    return max(0.0, min(100.0, float(value or 0)))


HUMAN_EVENT_RULES: list[tuple[tuple[str, ...], str]] = [
    (("ai", "搜索", "价格"), "AI 搜索变贵"),
    (("ai", "搜索", "收费"), "AI 搜索变贵"),
    (("ai", "涨价"), "AI 工具变贵"),
    (("ai", "收费"), "AI 工具变贵"),
    (("ai", "采购"), "公司开始买AI"),
    (("ai", "员工"), "打工人用AI被盯上"),
    (("ai", "搜索"), "AI 搜索变天"),
    (("人工智能", "搜索"), "AI 搜索变天"),
    (("robotaxi",), "Robotaxi 加速"),
    (("自动驾驶",), "自动驾驶加速"),
    (("无人驾驶",), "自动驾驶加速"),
    (("曹操出行",), "Robotaxi 加速"),
    (("萝卜快跑",), "Robotaxi 加速"),
    (("暗物质",), "暗物质新发现"),
    (("dark matter",), "暗物质新发现"),
    (("芯片", "限制"), "芯片限制收紧"),
    (("芯片", "出口"), "芯片出口收紧"),
    (("显卡", "上架"), "国产显卡上新"),
    (("显卡", "上市"), "国产显卡上新"),
    (("gpu", "上架"), "国产显卡上新"),
    (("电商", "变化"), "电商平台变了"),
    (("电商", "规则"), "电商调整规则"),
    (("车企", "价格"), "车企继续降价"),
    (("汽车", "降价"), "车企继续降价"),
    (("价格战",), "价格战继续"),
    (("宝马", "纯电"), "新能源洗牌"),
    (("新能源", "车型"), "新能源洗牌"),
    (("智驾", "价格"), "智驾价格战"),
    (("智驾",), "智驾调整功能"),
    (("华为", "智驾"), "智驾调整功能"),
    (("机器人", "降价"), "机器人降价"),
    (("大厂", "裁员"), "大厂裁员潮"),
    (("裁员",), "裁员潮再起"),
    (("就业",), "就业新变化"),
    (("教育", "ai"), "AI 进入课堂"),
    (("消费", "降级"), "消费降级反弹"),
    (("端午",), "假期消费变化"),
    (("出行",), "出行方式变化"),
    (("外卖",), "外卖平台变了"),
    (("平台", "规则"), "平台调整规则"),
    (("b站", "广告"), "B站广告加码"),
    (("哔哩哔哩", "广告"), "B站广告加码"),
    (("小红书",), "小红书变了"),
    (("抖音",), "抖音流量变了"),
    (("微信",), "微信生态变了"),
]

HUMAN_EVENT_ANY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("大模型", "模型发布", "openai", "claude", "deepseek", "豆包"), "AI 模型升级"),
    (("ai工具", "ai 工具", "智能体", "agent"), "AI 工具加速落地"),
    (("短剧", "直播", "带货"), "内容生意变化"),
    (("房贷", "楼市", "买房"), "楼市新变化"),
    (("手机", "苹果", "华为", "小米"), "手机市场变化"),
]


def _normalize_title(title: str) -> str:
    text = re.sub(r"https?://\S+", "", title or "")
    text = re.sub(r"[《》「」\"'“”]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(媒体|专家|官方|外媒|记者|快讯|重磅|突发)[:：]\s*", "", text)
    text = re.sub(r"(最新|新闻|消息|报道|分析|解读)$", "", text).strip()
    return text


def _best_phrase(title: str) -> str:
    parts = [
        part.strip()
        for part in re.split(r"[，。！？；:：|｜\-—_、]", title)
        if part.strip()
    ]
    if not parts:
        return title
    useful_parts = [part for part in parts if not _is_weak_phrase(part)]
    if not useful_parts:
        useful_parts = parts
    useful_parts.sort(key=lambda item: (len(item) > 16, len(item)))
    return useful_parts[0]


def _structured_event_title(text: str) -> str:
    subject = _title_subject(text)
    action = _title_action(text)
    if not subject:
        return ""
    return f"{subject}{action}"


def _title_subject(text: str) -> str:
    lowered = text.lower()
    subject_rules = [
        (("ai搜索", "ai 搜索", "人工智能搜索"), "AI搜索"),
        (("ai工具", "ai 工具", "智能体", "agent"), "AI工具"),
        (("大模型", "模型"), "AI模型"),
        (("openai", "deepseek", "claude", "豆包"), "AI模型"),
        (("robotaxi", "自动驾驶出租车"), "Robotaxi"),
        (("自动驾驶", "无人驾驶"), "自动驾驶"),
        (("智驾",), "智驾"),
        (("芯片", "gpu", "显卡"), "芯片"),
        (("机器人",), "机器人"),
        (("车企", "汽车", "新能源车"), "车企"),
        (("新能源",), "新能源"),
        (("电商", "淘宝", "京东", "拼多多"), "电商"),
        (("外卖", "美团", "饿了么"), "外卖平台"),
        (("b站", "哔哩哔哩"), "B站"),
        (("小红书",), "小红书"),
        (("抖音",), "抖音"),
        (("微信",), "微信"),
        (("平台",), "平台"),
        (("楼市", "房贷", "买房"), "楼市"),
        (("就业", "招聘", "裁员"), "就业市场"),
        (("消费", "消费者"), "消费趋势"),
        (("手机", "苹果", "华为", "小米"), "手机市场"),
        (("教育", "学校"), "教育"),
        (("医疗", "医保", "医院"), "医疗"),
        (("通勤", "出行", "交通"), "出行"),
        (("国际", "美国", "欧洲", "日本", "韩国", "海外", "全球"), "国际市场"),
    ]
    return next(
        (
            label
            for keywords, label in subject_rules
            if any(keyword.lower() in lowered for keyword in keywords)
        ),
        "",
    )


def _title_action(text: str) -> str:
    lowered = text.lower()
    action_rules = [
        (("涨价", "变贵", "收费", "价格上涨"), "变贵"),
        (("降价", "价格战", "补贴", "打折"), "降价"),
        (("规则", "调整", "新规", "改版"), "调整规则"),
        (("加速", "落地", "试点", "扩大"), "加速落地"),
        (("发布", "推出", "上线", "上新"), "上新"),
        (("升级", "更新"), "升级"),
        (("限制", "禁令", "监管", "收紧", "审查"), "被收紧"),
        (("裁员", "缩编"), "裁员"),
        (("广告", "商业化"), "加码广告"),
        (("采购", "购买", "接入"), "开始采购"),
        (("出海", "海外"), "加速出海"),
        (("变化", "改变", "影响", "变了"), "变了"),
        (("增长", "上涨"), "增长"),
        (("下滑", "下降"), "下滑"),
    ]
    return next(
        (
            label
            for keywords, label in action_rules
            if any(keyword.lower() in lowered for keyword in keywords)
        ),
        "有新变化",
    )


def _is_weak_phrase(phrase: str) -> bool:
    text = phrase.strip()
    if not text:
        return True
    if re.fullmatch(r"[\d\s年月日:/.\-]+", text):
        return True
    if re.search(r"\d+\s*月\s*\d+\s*日", text) and len(text) <= 12:
        return True
    if text in {"新变化", "变化", "调整", "发布公告", "最新消息"}:
        return True
    return False


def _compact_phrase(phrase: str) -> str:
    text = re.sub(r"\d+(\.\d+)?[%万亿千百]*", "", phrase)
    text = re.sub(r"(宣布|发布|回应|称|表示|指出|预计|认为|将|已|正在|开始|进入)", "", text)
    text = re.sub(r"(有限公司|集团|股份|官方)", "", text)
    text = re.sub(r"\s+", "", text)
    if len(text) <= 14:
        return text
    topic_words = re.findall(
        r"(AI|Robotaxi|芯片|电商|车企|汽车|消费|就业|教育|出行|平台|机器人|手机|楼市|大模型|搜索)",
        text,
        flags=re.I,
    )
    if topic_words:
        return _topic_fallback(topic_words[0])
    return "今日新变化"


def _topic_fallback(topic: str) -> str:
    normalized = topic.upper() if topic.lower() == "ai" else topic
    labels = {
        "AI": "AI 工具加速落地",
        "Robotaxi": "Robotaxi 加速",
        "芯片": "芯片新限制",
        "电商": "电商调整规则",
        "车企": "车企继续降价",
        "汽车": "车企继续降价",
        "消费": "消费趋势变了",
        "就业": "就业新变化",
        "教育": "AI 进入课堂",
        "出行": "出行方式变化",
        "平台": "平台调整规则",
        "机器人": "机器人开始降价",
        "手机": "手机市场变了",
        "楼市": "楼市新变化",
        "大模型": "AI 模型升级",
        "搜索": "AI 搜索变天",
    }
    return labels.get(normalized, f"{normalized}有新变化")
