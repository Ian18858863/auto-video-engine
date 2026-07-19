from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from app.models import HotItem


LOW_VALUE_PAGE_TITLE_MARKERS = (
    "用户反馈",
    "意见反馈",
    "反馈中心",
    "用户协议",
    "服务协议",
    "隐私政策",
    "隐私协议",
    "回复我的",
    "消息中心",
    "我的消息",
)
DOCUMENT_TITLE_MARKERS = (
    ".docx",
    ".doc",
    ".pdf",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    "下载",
    "报告文件",
    "文件名",
)
DOCUMENT_URL_EXTENSIONS = (
    ".docx",
    ".doc",
    ".pdf",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".zip",
    ".rar",
)
NON_REAL_ORIGINS = {"sample", "fake", "mock"}
DEFAULT_RECOMMENDATION_WINDOW_HOURS = 36
LOW_VALUE_LIFESTYLE_MARKERS = (
    "备孕",
    "孕期",
    "怀孕",
    "母婴",
    "夫妻日常",
    "新婚小夫妻",
    "育儿",
    "不用愁",
)
EVENT_CONTEXT_MARKERS = (
    "发布",
    "上线",
    "推出",
    "调整",
    "变化",
    "改变",
    "影响",
    "回应",
    "监管",
    "规则",
    "融资",
    "收购",
    "并购",
    "上市",
    "破产",
    "关闭",
    "停运",
    "禁止",
    "禁用",
    "更新",
    "升级",
    "首次",
    "超过",
    "实现",
    "完成",
    "报告",
    "增长",
    "下滑",
    "涨价",
    "降价",
    "裁员",
    "搜索",
    "工具",
    "平台",
)
BUSINESS_CONTEXT_MARKERS = (
    "商业",
    "平台",
    "消费",
    "用户",
    "价格",
    "流量",
    "广告",
    "工具",
    "公司",
    "行业",
    "规则",
    "趋势",
)
SHORT_LABEL_NOISY_SOURCES = (
    "小红书",
    "抖音",
    "B站",
    "哔哩哔哩",
)


def has_original_url(url: str | None) -> bool:
    value = (url or "").strip()
    if not value:
        return False
    parts = urlsplit(value)
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


def is_non_real_origin(value: str | None) -> bool:
    return (value or "").strip().lower() in NON_REAL_ORIGINS


def title_has_document_marker(title: str) -> bool:
    lowered = (title or "").lower()
    return any(marker in lowered for marker in DOCUMENT_TITLE_MARKERS)


def url_points_to_document(url: str | None) -> bool:
    path = urlsplit((url or "").strip()).path.lower()
    return any(path.endswith(extension) for extension in DOCUMENT_URL_EXTENSIONS)


def title_is_low_value_page(title: str | None) -> bool:
    text = (title or "").strip()
    if not text:
        return False
    return any(marker in text for marker in LOW_VALUE_PAGE_TITLE_MARKERS)


def title_is_single_stock_movement(title: str | None) -> bool:
    text = re.sub(r"\s+", "", (title or "").strip())
    if not text:
        return False
    return bool(
        re.fullmatch(
            r"[\u4e00-\u9fffA-Za-z0-9（）()·]{2,16}[+-]\d+(\.\d+)?%",
            text,
        )
    )


def title_is_contextless_short_label(
    title: str | None,
    source_platform: str | None = None,
) -> bool:
    source = source_platform or ""
    if not any(marker in source for marker in SHORT_LABEL_NOISY_SOURCES):
        return False
    text = re.sub(r"\s+", " ", (title or "").strip())
    if not text:
        return False
    normalized = re.sub(r"[\s·\-_｜|:：/]+", "", text)
    if len(normalized) > 10:
        return False
    if any(marker in text for marker in EVENT_CONTEXT_MARKERS):
        return False
    # 只有产品名、账号名或短词，没有“发生了什么”，不能作为选题事件。
    return bool(re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9.]+", normalized))


def title_is_low_value_lifestyle(title: str | None, summary: str | None = None) -> bool:
    text = f"{title or ''} {summary or ''}"
    if not any(marker in text for marker in LOW_VALUE_LIFESTYLE_MARKERS):
        return False
    return not any(marker in text for marker in BUSINESS_CONTEXT_MARKERS)


def url_is_low_value_page(url: str | None) -> bool:
    parts = urlsplit((url or "").strip())
    host = parts.netloc.lower()
    path = parts.path.lower().rstrip("/")
    query = parts.query.lower()
    if not host:
        return False
    if "feedback" in query or "feedback" in path:
        return True
    if host == "message.bilibili.com":
        return True
    if host.endswith("bilibili.com") and (
        path.startswith("/protocal")
        or path.startswith("/protocol")
        or path.startswith("/blackboard/activity")
    ):
        return True
    if host == "space.bilibili.com":
        # B站用户空间是账号主页，不是具体视频/热点内容页。
        return True
    if host.endswith("cls.cn") and path == "/our":
        return True
    return False


def is_low_value_page(item: HotItem) -> bool:
    return (
        title_is_low_value_page(item.title)
        or title_is_single_stock_movement(item.title)
        or title_is_contextless_short_label(item.title, item.source_platform)
        or title_is_low_value_lifestyle(item.title, item.summary)
        or url_is_low_value_page(item.url)
    )


def is_recent_enough_for_recommendation(
    item: HotItem,
    *,
    now: datetime | None = None,
    active_window_hours: int = DEFAULT_RECOMMENDATION_WINDOW_HOURS,
) -> bool:
    current_time = _aware(now or datetime.now(timezone.utc))
    item_time = item.published_at or item.fetched_at
    if item_time is None:
        return False
    return _aware(item_time) >= current_time - timedelta(hours=max(active_window_hours, 1))


def has_news_body_link(item: HotItem) -> bool:
    return (
        has_original_url(item.url)
        and not url_points_to_document(item.url)
        and item.source_type in {"news", "community", "product"}
        and bool((item.summary or "").strip())
    )


def is_document_title_without_news_body_link(item: HotItem) -> bool:
    return title_has_document_marker(item.title) and not has_news_body_link(item)


def is_today_recommendation_eligible(
    item: HotItem,
    *,
    now: datetime | None = None,
) -> bool:
    return (
        not item.is_filtered
        and (item.lifecycle_status or "active") != "archived"
        and (item.data_origin or "real") == "real"
        and has_original_url(item.url)
        and not url_points_to_document(item.url)
        and not is_non_real_origin(item.data_origin)
        and is_recent_enough_for_recommendation(item, now=now)
        and not is_document_title_without_news_body_link(item)
        and not is_low_value_page(item)
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def source_credibility(
    *,
    url: str | None,
    published_at: datetime | None,
    summary: str | None,
) -> tuple[str, str]:
    has_url = has_original_url(url)
    has_time = published_at is not None
    has_summary = bool((summary or "").strip())
    if has_url and has_time and has_summary:
        return "高", "有原文 URL、发布时间和正文摘要。"
    if has_url and (has_time or has_summary):
        return "中", "有原文 URL，但发布时间或正文摘要不完整。"
    return "低", "只有标题和来源名，缺少可验证的原文信息。"
