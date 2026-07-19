from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session

from app.hotspots.config import BrowserHotspotSourceConfig
from app.hotspots.repository import save_hotspot
from app.hotspots.schemas import HotspotCandidate
from app.models import SourceRun
from app.models.entities import utc_now


CSV_HEADERS = [
    "来源平台",
    "标题",
    "链接",
    "作者",
    "发布时间",
    "点赞数",
    "评论数",
    "采集时间",
]
LOGIN_KEYWORDS = [
    "请先登录",
    "登录后查看",
    "立即登录",
    "登录已过期",
    "验证码",
    "安全验证",
    "访问频繁",
    "滑块",
    "verify",
    "captcha",
]


class BrowserHotspotError(RuntimeError):
    pass


class BrowserHotspotLoginRequired(BrowserHotspotError):
    pass


@dataclass(slots=True)
class BrowserHotspotRecord:
    source_platform: str
    title: str
    link: str
    author: str = ""
    published_at: str = ""
    likes: int | None = None
    comments: int | None = None
    collected_at: datetime = field(default_factory=utc_now)


class BrowserHotspotCollector(Protocol):
    def collect(
        self,
        source: BrowserHotspotSourceConfig,
    ) -> list[BrowserHotspotRecord]:
        raise NotImplementedError


@dataclass(slots=True)
class BrowserHotspotCollectionResult:
    source_count: int = 0
    fetched_count: int = 0
    saved_count: int = 0
    duplicate_count: int = 0
    csv_written_count: int = 0
    failed_sources: list[str] = field(default_factory=list)
    needs_login_sources: list[str] = field(default_factory=list)
    skipped_sources: list[str] = field(default_factory=list)
    csv_path: str = ""
    message: str = ""


class PlaywrightBrowserHotspotCollector:
    def __init__(
        self,
        *,
        profile_dir: str | Path,
        headless: bool = False,
        max_items: int = 10,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.max_items = max_items

    def collect(
        self,
        source: BrowserHotspotSourceConfig,
    ) -> list[BrowserHotspotRecord]:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise BrowserHotspotError(
                "缺少 Playwright。请先运行：backend\\.venv\\Scripts\\python.exe -m pip install playwright"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        limit = min(source.limit, self.max_items)
        try:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    channel="chrome",
                    headless=self.headless,
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 1000},
                )
                try:
                    page = context.new_page()
                    page.goto(
                        source.url,
                        wait_until="domcontentloaded",
                        timeout=int(source.timeout * 1000),
                    )
                    page.wait_for_timeout(2500)
                    body_text = page.locator("body").inner_text(timeout=5000)
                    if _looks_like_login_or_verification(body_text):
                        raise BrowserHotspotLoginRequired(
                            f"{source.name} 需要重新登录或遇到验证页，请在 Chrome 里登录后再采集。"
                        )
                    anchors = page.locator("a").evaluate_all(
                        """
                        els => els.map((el) => ({
                            title: (el.innerText || el.textContent || '').trim(),
                            link: el.href || ''
                        }))
                        """
                    )
                finally:
                    context.close()
        except BrowserHotspotLoginRequired:
            raise
        except PlaywrightTimeoutError as exc:
            raise BrowserHotspotError(f"{source.name} 页面加载超时。") from exc
        except Exception as exc:
            raise BrowserHotspotError(f"{source.name} 浏览器采集失败：{exc}") from exc

        now = utc_now()
        records: list[BrowserHotspotRecord] = []
        seen_links: set[str] = set()
        for raw in anchors:
            title = _clean_text(str(raw.get("title") or ""))
            link = str(raw.get("link") or "").strip()
            if not _is_candidate_link(title, link):
                continue
            if link in seen_links:
                continue
            seen_links.add(link)
            records.append(
                BrowserHotspotRecord(
                    source_platform=source.name,
                    title=title,
                    link=link,
                    collected_at=now,
                )
            )
            if len(records) >= limit:
                break
        return records


def run_browser_hotspot_collection(
    session: Session,
    *,
    sources: list[BrowserHotspotSourceConfig],
    collector: BrowserHotspotCollector,
    csv_path: str | Path,
    force: bool = False,
    now: datetime | None = None,
) -> BrowserHotspotCollectionResult:
    current_time = now or utc_now()
    enabled_sources = [source for source in sources if source.enabled]
    result = BrowserHotspotCollectionResult(
        source_count=len(enabled_sources),
        csv_path=str(Path(csv_path)),
    )
    all_records: list[BrowserHotspotRecord] = []
    for source in enabled_sources:
        if not force and _successful_today(session, source.name, current_time):
            result.skipped_sources.append(source.name)
            continue

        source_run = SourceRun(source_name=source.name, started_at=utc_now())
        try:
            records = collector.collect(source)
            result.fetched_count += len(records)
            all_records.extend(records)
            saved, duplicates = _save_records_as_hotspots(session, source, records)
            result.saved_count += saved
            result.duplicate_count += duplicates
            source_run.success = True
            source_run.status = "成功"
            source_run.items_fetched = len(records)
        except BrowserHotspotLoginRequired as exc:
            source_run.success = False
            source_run.status = "需要重新登录"
            source_run.error_message = str(exc)
            result.failed_sources.append(source.name)
            result.needs_login_sources.append(source.name)
        except BrowserHotspotError as exc:
            source_run.success = False
            source_run.status = "失败"
            source_run.error_message = str(exc)
            result.failed_sources.append(source.name)
        except Exception as exc:
            source_run.success = False
            source_run.status = "失败"
            source_run.error_message = str(exc)
            result.failed_sources.append(source.name)
        finally:
            source_run.ended_at = utc_now()
            session.add(source_run)
            session.commit()

    result.csv_written_count = append_records_to_csv(csv_path, all_records)
    result.message = _result_message(result)
    return result


def append_records_to_csv(
    csv_path: str | Path,
    records: list[BrowserHotspotRecord],
) -> int:
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_rows = _read_existing_csv_rows(path)
    existing_links = {row.get("链接", "").strip() for row in existing_rows if row.get("链接")}
    new_rows: list[dict[str, str]] = []
    for record in records:
        link = record.link.strip()
        if link and link in existing_links:
            continue
        row = _record_to_csv_row(record)
        new_rows.append(row)
        if link:
            existing_links.add(link)

    rows = existing_rows + new_rows
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)
    return len(new_rows)


def _save_records_as_hotspots(
    session: Session,
    source: BrowserHotspotSourceConfig,
    records: list[BrowserHotspotRecord],
) -> tuple[int, int]:
    saved = 0
    duplicates = 0
    for index, record in enumerate(records, start=1):
        candidate = HotspotCandidate(
            source_platform=record.source_platform or source.name,
            source_type="browser_hotlist",
            title=record.title,
            url=record.link,
            published_at=None,
            summary="低频采集的公开热点，后续请点原链接核对。",
            metrics={
                "rank": index,
                "likes": record.likes or 0,
                "comments": record.comments or 0,
                "author": record.author,
                "published_at_text": record.published_at,
            },
            tags=source.tags[:5],
            category=source.category,
            data_origin="real",
            origin_note="浏览器低频公开采集",
        )
        _, created = save_hotspot(session, candidate)
        if created:
            saved += 1
        else:
            duplicates += 1
    return saved, duplicates


def _read_existing_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [
            {header: row.get(header, "") for header in CSV_HEADERS}
            for row in reader
        ]


def _record_to_csv_row(record: BrowserHotspotRecord) -> dict[str, str]:
    return {
        "来源平台": record.source_platform,
        "标题": record.title,
        "链接": record.link,
        "作者": record.author,
        "发布时间": record.published_at,
        "点赞数": _metric_to_text(record.likes),
        "评论数": _metric_to_text(record.comments),
        "采集时间": record.collected_at.isoformat(),
    }


def _metric_to_text(value: int | None) -> str:
    return "" if value is None else str(value)


def _looks_like_login_or_verification(text: str) -> bool:
    normalized = text.lower()
    if re.search(r"请.{0,8}登录|登录.{0,8}过期|登录后", text):
        return True
    return any(keyword.lower() in normalized for keyword in LOGIN_KEYWORDS)


def _is_candidate_link(title: str, link: str) -> bool:
    if len(title) < 4 or len(title) > 120:
        return False
    if not link.startswith(("http://", "https://")):
        return False
    lowered = title.lower()
    if lowered in {"登录", "注册", "广告", "更多", "首页", "下载app"}:
        return False
    return True


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _successful_today(session: Session, source_name: str, now: datetime) -> bool:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    start_of_day = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    return (
        session.query(SourceRun.id)
        .filter(SourceRun.source_name == source_name)
        .filter(SourceRun.success.is_(True))
        .filter(SourceRun.ended_at >= start_of_day)
        .first()
        is not None
    )


def _result_message(result: BrowserHotspotCollectionResult) -> str:
    parts = [
        f"浏览器来源 {result.source_count} 个",
        f"采集 {result.fetched_count} 条",
        f"新增热点 {result.saved_count} 条",
        f"CSV 新增 {result.csv_written_count} 条",
    ]
    if result.skipped_sources:
        parts.append(f"跳过 {len(result.skipped_sources)} 个今日已采集来源")
    if result.needs_login_sources:
        parts.append("有来源需要重新登录 Chrome")
    if result.failed_sources:
        parts.append(f"失败 {len(result.failed_sources)} 个来源")
    return "；".join(parts) + "。"
