from __future__ import annotations

from dataclasses import dataclass, field

from app.materials.planner import MaterialTask


RISK_KEYWORDS = ["政治", "宗教", "赌博", "色情", "暴力", "低俗", "猎奇", "荐股", "币圈"]
STYLE_BAD_KEYWORDS = ["土味", "营销", "娱乐搞笑", "花哨", "低质"]


@dataclass(slots=True)
class CandidateMaterial:
    material_id: str
    title: str
    description: str
    keywords: list[str]
    source: str
    license_type: str
    risk_flags: list[str] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)
    local_path: str = ""
    orientation: str = "unknown"
    subject_centered: bool = False
    key_info_safe: bool = False
    background_suitable: bool = False
    overlay_safe: bool = False


@dataclass(slots=True)
class CandidateScore:
    material_id: str
    topic_score: float
    quality_score: float
    style_score: float
    info_value_score: float
    risk_score: float
    duplicate_score: float
    portrait_fit_score: float
    total_score: float
    allowed_license: bool
    recommended_download: bool
    reason: str


def score_candidate(
    task: MaterialTask,
    candidate: CandidateMaterial,
    existing_keywords: list[str],
) -> CandidateScore:
    text = f"{candidate.title} {candidate.description} {' '.join(candidate.keywords)}"
    topic_score = _topic_score(task, text)
    quality_score = _quality_score(candidate)
    style_score = _style_score(text)
    info_value_score = 85 if topic_score >= 70 else 55
    risk_score = _risk_score(candidate, text)
    duplicate_score = 55 if any(keyword in existing_keywords for keyword in candidate.keywords) else 90
    portrait_fit_score = _portrait_fit_score(candidate)
    allowed_license = candidate.license_type in {"free", "member"}
    total = round(
        topic_score * 0.24
        + quality_score * 0.17
        + style_score * 0.14
        + info_value_score * 0.13
        + portrait_fit_score * 0.14
        + risk_score * 0.12
        + duplicate_score * 0.06,
        2,
    )
    recommended = (
        allowed_license
        and total >= 60
        and risk_score >= 80
        and topic_score >= 60
        and quality_score >= 65
        and portrait_fit_score >= 60
    )
    return CandidateScore(
        material_id=candidate.material_id,
        topic_score=topic_score,
        quality_score=quality_score,
        style_score=style_score,
        info_value_score=info_value_score,
        risk_score=risk_score,
        duplicate_score=duplicate_score,
        portrait_fit_score=portrait_fit_score,
        total_score=total,
        allowed_license=allowed_license,
        recommended_download=recommended,
        reason=_reason(
            recommended,
            allowed_license,
            topic_score,
            quality_score,
            risk_score,
            portrait_fit_score,
            total,
        ),
    )


def is_download_allowed(score: CandidateScore, min_score: float = 60.0) -> bool:
    return (
        score.allowed_license
        and score.total_score >= min_score
        and score.risk_score >= 80
        and score.topic_score >= 60
        and score.quality_score >= 65
        and score.portrait_fit_score >= 60
    )


def _topic_score(task: MaterialTask, text: str) -> float:
    hits = sum(1 for keyword in task.jianying_keywords if keyword and keyword in text)
    if hits:
        return min(95.0, 55.0 + hits * 14)
    fallback_hits = sum(1 for keyword in ["AI", "科技", "办公", "商业", "财经", "数据"] if keyword in text)
    return min(80.0, 35.0 + fallback_hits * 10)


def _quality_score(candidate: CandidateMaterial) -> float:
    score = 70.0
    if "sharp" in candidate.quality_flags:
        score += 10
    if "clean" in candidate.quality_flags:
        score += 10
    if "stable" in candidate.quality_flags:
        score += 5
    if "low_quality" in candidate.quality_flags:
        score -= 35
    if "watermark" in candidate.quality_flags:
        score -= 25
    return max(0.0, min(100.0, score))


def _style_score(text: str) -> float:
    score = 82.0
    if any(keyword in text for keyword in ["科技", "财经", "商业", "办公", "数据"]):
        score += 10
    if any(keyword in text for keyword in STYLE_BAD_KEYWORDS):
        score -= 35
    return max(0.0, min(100.0, score))


def _risk_score(candidate: CandidateMaterial, text: str) -> float:
    if candidate.risk_flags:
        return 0.0
    if any(keyword in text for keyword in RISK_KEYWORDS):
        return 0.0
    return 95.0


def _portrait_fit_score(candidate: CandidateMaterial) -> float:
    if candidate.orientation == "portrait":
        return 100.0
    if candidate.orientation != "landscape":
        return 0.0
    if all(
        (
            candidate.subject_centered,
            candidate.key_info_safe,
            candidate.background_suitable,
            candidate.overlay_safe,
        )
    ):
        return 75.0
    return 0.0


def _reason(
    recommended: bool,
    allowed_license: bool,
    topic_score: float,
    quality_score: float,
    risk_score: float,
    portrait_fit_score: float,
    total_score: float,
) -> str:
    if recommended:
        return "筛选通过，可下载。"
    if not allowed_license:
        return "非免费或会员可用素材，不下载。"
    if risk_score < 80:
        return "风险分异常，不下载。"
    if topic_score < 60:
        return "主题不匹配，不下载。"
    if quality_score < 65:
        return "画面质量不足，不下载。"
    if portrait_fit_score < 60:
        return "不适合竖屏 9:16，或横屏素材无法安全裁切，不下载。"
    return f"综合分 {total_score} 低于下载阈值，不下载。"
