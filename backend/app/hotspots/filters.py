from __future__ import annotations

from app.hotspots.schemas import FilterResult, HotspotCandidate


SENSITIVE_KEYWORDS: dict[str, list[str]] = {
    "政治": [
        "选举",
        "政党",
        "总统",
        "国会",
        "政治人物",
        "战争立场",
        "政府冲突",
        "election",
        "political party",
    ],
    "宗教": [
        "宗教冲突",
        "宗教争议",
        "邪教",
        "传教",
    ],
    "色情": [
        "色情",
        "成人内容",
        "成人视频",
        "裸聊",
        "porn",
        "nsfw",
    ],
    "赌博": [
        "赌博",
        "博彩",
        "赌场",
        "下注",
        "casino",
        "betting",
    ],
    "暴力": [
        "血腥",
        "暴力",
        "枪击",
        "虐杀",
        "shooting",
        "graphic violence",
    ],
    "违法教学": [
        "诈骗教程",
        "洗钱教程",
        "黑产",
        "盗号",
        "绕过审核",
        "phishing kit",
        "credential theft",
        "bypass moderation",
    ],
    "低俗猎奇": [
        "低俗",
        "猎奇",
        "八卦绯闻",
    ],
    "低信息汇总": [
        "晚报｜",
        "早报｜",
        "日报｜",
        "氪星晚报",
        "科技早报",
    ],
}


def evaluate_filter(candidate: HotspotCandidate) -> FilterResult:
    text = f"{candidate.title} {candidate.summary} {' '.join(candidate.tags)}".lower()
    matched: list[str] = []

    for reason, keywords in SENSITIVE_KEYWORDS.items():
        for keyword in keywords:
            if keyword.lower() in text:
                matched.append(f"{reason}:{keyword}")
                break

    if matched:
        return FilterResult(is_filtered=True, reason="；".join(matched))
    return FilterResult(is_filtered=False)
