from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from app.hotspots.schemas import HotspotCandidate, ScoreResult


SOURCE_WEIGHTS = {
    "百度热搜": 20,
    "百度热搜网页版": 20,
    "微博热搜网页版": 20,
    "今日头条热榜": 19,
    "抖音热点网页版": 18,
    "小红书公开发现页": 17,
    "B站热门": 18,
    "B站全站排行": 18,
    "B站科技区排行": 18,
    "财联社电报网页版": 18,
    "36氪": 14,
    "IT之家": 14,
    "量子位": 18,
    "Reuters": 18,
    "Bloomberg": 18,
    "TechCrunch": 18,
    "Product Hunt": 18,
    "Hacker News": 17,
    "Google News": 15,
    "Reddit": 13,
    "手动导入": 10,
}

TOPIC_KEYWORDS = [
    "ai",
    "人工智能",
    "大模型",
    "agent",
    "工具",
    "科技",
    "商业",
    "business",
    "startup",
    "saas",
    "productivity",
    "developer",
    "新工具",
    "新产品",
    "趋势",
    "消费",
    "财经",
    "价格",
    "就业",
    "产业",
    "增长",
    "平台",
    "出海",
    "国内",
    "中国",
    "上班",
    "通勤",
    "外卖",
    "电商",
    "车企",
    "手机",
    "教育",
]

COLLECT_VALUE_KEYWORDS = [
    "how",
    "why",
    "guide",
    "framework",
    "workflow",
    "清单",
    "避坑",
    "方法",
    "判断",
    "逻辑",
    "框架",
    "对比",
    "趋势",
    "工具",
    "效率",
    "省钱",
    "省时间",
]

RISK_KEYWORDS = [
    "politics",
    "election",
    "war",
    "religion",
    "porn",
    "casino",
    "betting",
    "hack",
    "bypass",
    "leak",
    "政治",
    "选举",
    "战争",
    "宗教",
    "色情",
    "赌博",
    "博彩",
    "暴力",
    "枪击",
    "诈骗",
    "黑产",
    "绕过审核",
]

LOW_VALUE_KEYWORDS = [
    "celebrity",
    "gossip",
    "rumor",
    "scandal",
    "八卦",
    "绯闻",
    "猎奇",
    "低俗",
    "爆料",
    "gta",
    "游戏",
    "acg",
    "艺术图集",
    "展会",
    "谣言",
    "电视剧",
    "电影",
    "动漫",
    "修仙传",
    "官方声明",
]


def clamp_score(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)


def score_hotspot(candidate: HotspotCandidate, now: datetime | None = None) -> ScoreResult:
    now = now or datetime.now(timezone.utc)
    text = f"{candidate.title} {candidate.summary} {' '.join(candidate.tags)}".lower()

    freshness_score = _freshness_score(candidate.published_at, now)
    risk_score = _risk_score(text)
    low_value_penalty = 18 if any(keyword in text for keyword in LOW_VALUE_KEYWORDS) else 0

    hot_score = (
        _source_score(candidate.source_platform)
        + freshness_score
        + _metrics_score(candidate.metrics)
        + _rank_score(candidate.metrics.get("rank"))
    )
    if _has_plain_discussion_point(text):
        hot_score += 8

    topic_score = 25 + _keyword_score(text, TOPIC_KEYWORDS, points_per_hit=8, limit=45)
    if candidate.category in {"AI", "科技", "商业", "财经", "消费", "互联网趋势"}:
        topic_score += 15
    if _has_plain_discussion_point(text):
        topic_score += 10
    topic_score -= risk_score * 0.35 + low_value_penalty

    save_score = 20 + _keyword_score(text, COLLECT_VALUE_KEYWORDS, points_per_hit=8, limit=45)
    if candidate.category in {"AI", "科技", "商业", "财经", "消费"}:
        save_score += 10
    if any(key in text for key in ["framework", "guide", "判断", "逻辑", "清单", "workflow"]):
        save_score += 15
    save_score -= risk_score * 0.2 + low_value_penalty

    return ScoreResult(
        hot_score=clamp_score(hot_score),
        topic_score=clamp_score(topic_score),
        save_score=clamp_score(save_score),
        risk_score=clamp_score(risk_score),
        freshness_score=clamp_score(freshness_score),
    )


def _source_score(source_platform: str) -> float:
    return float(SOURCE_WEIGHTS.get(source_platform, 10))


def _freshness_score(published_at: datetime | None, now: datetime) -> float:
    if published_at is None:
        return 12
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (now - published_at).total_seconds() / 3600)
    if age_hours <= 3:
        return 25
    if age_hours <= 12:
        return 20
    if age_hours <= 24:
        return 16
    if age_hours <= 72:
        return 10
    return 4


def _metrics_score(metrics: dict[str, Any]) -> float:
    score = 0.0
    score += _log_score(metrics.get("upvotes"), 18, scale=80)
    score += _log_score(metrics.get("likes"), 14, scale=120)
    score += _log_score(metrics.get("comments"), 14, scale=40)
    score += _log_score(metrics.get("shares"), 10, scale=40)
    score += _log_score(metrics.get("views"), 18, scale=5000)
    return min(35.0, score)


def _rank_score(rank: Any) -> float:
    try:
        rank_value = int(rank)
    except (TypeError, ValueError):
        return 0.0
    if rank_value <= 1:
        return 20
    if rank_value <= 5:
        return 16
    if rank_value <= 10:
        return 12
    if rank_value <= 30:
        return 7
    return 2


def _log_score(value: Any, max_points: float, scale: float) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if number <= 0:
        return 0.0
    return min(max_points, math.log1p(number) / math.log1p(scale) * max_points)


def _keyword_score(text: str, keywords: list[str], points_per_hit: float, limit: float) -> float:
    hits = sum(1 for keyword in keywords if keyword.lower() in text)
    return min(limit, hits * points_per_hit)


def _has_plain_discussion_point(text: str) -> bool:
    return any(
        keyword in text
        for keyword in [
            "why",
            "how",
            "changes",
            "新",
            "为什么",
            "怎么",
            "背后",
            "趋势",
            "变化",
            "工具",
        ]
    )


def _risk_score(text: str) -> float:
    score = _keyword_score(text, RISK_KEYWORDS, points_per_hit=18, limit=90)
    if any(keyword in text for keyword in LOW_VALUE_KEYWORDS):
        score += 18
    return score
