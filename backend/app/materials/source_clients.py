from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.materials.asset_paths import AutoMaterialPaths
from app.materials.asset_registry import MaterialFingerprintRegistry, sha256_file
from app.materials.safe_http_download import SafeDownloadReceipt, SafeHttpDownloader
from app.materials.stock_rights_evidence import (
    StockRightsEvidenceError,
    build_stock_rights_evidence,
)


MIN_VIDEO_DIMENSION = 1080
MAX_SOURCE_VIDEO_BYTES = 512 * 1024 * 1024
MAX_SOURCE_PHOTO_BYTES = 64 * 1024 * 1024
_SOURCE_HOST_SUFFIXES = {
    "Pixabay": frozenset({"pixabay.com"}),
    "Coverr": frozenset({"coverr.co"}),
}


@dataclass(slots=True)
class MaterialSourceItem:
    source_name: str
    asset_type: str
    status: str
    save_path: str = ""
    source_url: str = ""
    fingerprint: str = ""
    reason: str = ""
    rights_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MaterialCandidate:
    source_name: str
    source_id: str
    asset_type: str
    title: str
    keyword: str
    download_url: str
    source_url: str = ""
    width: int = 0
    height: int = 0
    duration_seconds: float = 0.0
    contributor_id: str = ""
    contributor_name: str = ""
    contributor_profile_url: str = ""
    rights_evidence: dict[str, Any] = field(default_factory=dict)


class MaterialHttpClient(Protocol):
    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...

    def get_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        ...


class HttpxMaterialHttpClient:
    def __init__(
        self,
        timeout_seconds: float = 30.0,
        *,
        downloader: SafeHttpDownloader | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.downloader = downloader or SafeHttpDownloader(
            timeout_seconds=timeout_seconds
        )

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = httpx.get(
                url,
                headers=headers or {},
                params=params or {},
                timeout=self.timeout_seconds,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"素材源请求失败：{exc}") from exc
        if response.status_code == 401:
            raise RuntimeError("素材源鉴权失败，请检查 API Key。")
        if response.status_code == 429:
            raise RuntimeError("素材源请求过多，请稍后再试。")
        if response.status_code >= 400:
            raise RuntimeError(f"素材源请求失败：HTTP {response.status_code}。")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("素材源返回格式不正确。")
        return payload

    def get_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        try:
            response = httpx.get(
                url,
                headers=headers or {},
                timeout=self.timeout_seconds,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"素材文件下载失败：{exc}") from exc
        if response.status_code >= 400:
            raise RuntimeError(f"素材文件下载失败：HTTP {response.status_code}。")
        if not response.content:
            raise RuntimeError("素材文件下载失败：返回空内容。")
        return response.content

    def download_to(
        self,
        url: str,
        destination: Path,
        *,
        source_name: str,
        asset_type: str,
        headers: dict[str, str] | None = None,
    ) -> SafeDownloadReceipt:
        host_suffixes = _SOURCE_HOST_SUFFIXES.get(source_name)
        if host_suffixes is None:
            raise RuntimeError(f"未批准的素材下载源：{source_name}。")
        return self.downloader.download(
            url,
            destination,
            headers=headers,
            allowed_host_suffixes=host_suffixes,
            allowed_mime_types=_allowed_mimes(asset_type),
            max_bytes=_max_download_bytes(asset_type),
        )


class _BaseDownloadService:
    def __init__(
        self,
        *,
        paths: AutoMaterialPaths | None = None,
        http_client: MaterialHttpClient | None = None,
        fingerprint_registry: MaterialFingerprintRegistry | None = None,
    ):
        self.paths = paths or AutoMaterialPaths()
        self.http_client = http_client or HttpxMaterialHttpClient()
        self.fingerprint_registry = fingerprint_registry or MaterialFingerprintRegistry(
            self.paths.fingerprint_registry_path
        )

    def _download_candidate(self, candidate: MaterialCandidate, target_dir: Path) -> MaterialSourceItem:
        if self._id_exists(candidate):
            return MaterialSourceItem(
                source_name=candidate.source_name,
                asset_type=candidate.asset_type,
                status="跳过",
                source_url=candidate.source_url or candidate.download_url,
                reason=f"已存在相同 {candidate.source_name} 素材 ID。",
                rights_evidence=deepcopy(candidate.rights_evidence),
            )
        target_dir.mkdir(parents=True, exist_ok=True)
        fetched_at = datetime.now()
        suffix = _suffix_from_url(candidate.download_url)
        if candidate.asset_type == "video" and suffix not in {".mp4", ".mov", ".webm"}:
            suffix = ".mp4"
        if candidate.asset_type == "photo" and suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            suffix = ".jpg"
        target_path = target_dir / (
            f"{_safe_filename(candidate.title or candidate.keyword)}_"
            f"{fetched_at:%Y%m%d_%H%M%S}_"
            f"{_safe_filename(candidate.source_id)}{suffix}"
        )
        temp_path = target_path.with_name(f"{target_path.name}.part")
        download_completed = False
        try:
            _download_client_to(
                self.http_client,
                candidate.download_url,
                temp_path,
                source_name=candidate.source_name,
                asset_type=candidate.asset_type,
            )
            download_completed = True
            duplicate = self.fingerprint_registry.find_duplicate(temp_path)
            if duplicate is not None:
                temp_path.unlink(missing_ok=True)
                return MaterialSourceItem(
                    source_name=candidate.source_name,
                    asset_type=candidate.asset_type,
                    status="跳过",
                    source_url=candidate.source_url or candidate.download_url,
                    fingerprint=duplicate.fingerprint,
                    reason=f"文件指纹重复，已存在：{duplicate.file_path}",
                    rights_evidence=deepcopy(candidate.rights_evidence),
                )
            temp_path.replace(target_path)
            fingerprint = sha256_file(target_path)
            self.fingerprint_registry.record_file(
                target_path,
                source_type=f"{candidate.source_name}{'视频' if candidate.asset_type == 'video' else '图片'}",
            )
            return MaterialSourceItem(
                source_name=candidate.source_name,
                asset_type=candidate.asset_type,
                status="已下载",
                save_path=str(target_path),
                source_url=candidate.source_url or candidate.download_url,
                fingerprint=fingerprint,
                rights_evidence=deepcopy(candidate.rights_evidence),
            )
        except Exception as exc:
            if download_completed:
                temp_path.unlink(missing_ok=True)
            return MaterialSourceItem(
                source_name=candidate.source_name,
                asset_type=candidate.asset_type,
                status="失败",
                source_url=candidate.source_url or candidate.download_url,
                reason=f"{candidate.source_name}素材下载失败：{exc}",
                rights_evidence=deepcopy(candidate.rights_evidence),
            )

    def _id_exists(self, candidate: MaterialCandidate) -> bool:
        marker = _safe_filename(candidate.source_id)
        dirs = [
            self.paths.pexels_video_dir,
            self.paths.pexels_photo_dir,
            self.paths.pixabay_video_dir,
            self.paths.pixabay_photo_dir,
            self.paths.coverr_video_dir,
        ]
        return any(directory.exists() and any(marker in item.name for item in directory.iterdir()) for directory in dirs)


class PixabayDownloadService(_BaseDownloadService):
    def __init__(
        self,
        *,
        paths: AutoMaterialPaths | None = None,
        http_client: MaterialHttpClient | None = None,
        fingerprint_registry: MaterialFingerprintRegistry | None = None,
        api_key: str | None = None,
    ):
        super().__init__(
            paths=paths,
            http_client=http_client,
            fingerprint_registry=fingerprint_registry,
        )
        self.api_key = api_key.strip() if isinstance(api_key, str) else None

    def download_best(
        self,
        keyword: str,
        *,
        per_page: int = 10,
        api_key: str | None = None,
    ) -> MaterialSourceItem:
        per_page = max(1, min(per_page, 10))
        try:
            self.paths.ensure_directories()
            resolved_api_key = (
                api_key.strip()
                if isinstance(api_key, str) and api_key.strip()
                else self.api_key
            )
            if not resolved_api_key:
                resolved_api_key = self.paths.read_pixabay_api_key()
        except Exception as exc:
            return MaterialSourceItem("Pixabay", "video", "失败", reason=str(exc))

        video = self._download_first_video(keyword, api_key=resolved_api_key, per_page=per_page)
        if video.status == "已下载":
            return video
        photo = self._download_first_photo(
            keyword,
            api_key=resolved_api_key,
            per_page=per_page,
        )
        if photo.status == "已下载":
            return photo
        reasons = [item.reason for item in [video, photo] if item.reason]
        return MaterialSourceItem(
            "Pixabay",
            "video",
            "失败",
            reason="；".join(reasons) or "Pixabay 没有找到合适素材。",
        )

    def _download_first_video(self, keyword: str, *, api_key: str, per_page: int) -> MaterialSourceItem:
        try:
            payload = self.http_client.get_json(
                "https://pixabay.com/api/videos/",
                params={"key": api_key, "q": keyword, "per_page": per_page, "safesearch": "true"},
            )
            hits = payload.get("hits")
            if not isinstance(hits, list) or not hits:
                return MaterialSourceItem("Pixabay", "video", "失败", reason="Pixabay 没有找到视频结果。")
            for raw in hits:
                candidate = _pixabay_video_candidate(raw, keyword)
                if candidate is None:
                    continue
                item = self._download_candidate(candidate, self.paths.pixabay_video_dir)
                if item.status == "已下载":
                    return item
            return MaterialSourceItem("Pixabay", "video", "失败", reason="Pixabay 视频结果没有 1080p 以上 MP4。")
        except Exception as exc:
            return MaterialSourceItem("Pixabay", "video", "失败", reason=str(exc))

    def _download_first_photo(self, keyword: str, *, api_key: str, per_page: int) -> MaterialSourceItem:
        try:
            payload = self.http_client.get_json(
                "https://pixabay.com/api/",
                params={"key": api_key, "q": keyword, "per_page": per_page, "safesearch": "true", "image_type": "photo"},
            )
            hits = payload.get("hits")
            if not isinstance(hits, list) or not hits:
                return MaterialSourceItem("Pixabay", "photo", "失败", reason="Pixabay 没有找到图片结果。")
            for raw in hits:
                candidate = _pixabay_photo_candidate(raw, keyword)
                if candidate is None:
                    continue
                item = self._download_candidate(candidate, self.paths.pixabay_photo_dir)
                if item.status == "已下载":
                    return item
            return MaterialSourceItem("Pixabay", "photo", "失败", reason="Pixabay 图片结果没有可下载文件。")
        except Exception as exc:
            return MaterialSourceItem("Pixabay", "photo", "失败", reason=str(exc))


class CoverrDownloadService(_BaseDownloadService):
    def download_best(self, keyword: str, *, per_page: int = 10) -> MaterialSourceItem:
        per_page = max(1, min(per_page, 10))
        try:
            self.paths.ensure_directories()
            payload = self.http_client.get_json(
                "https://api.coverr.co/videos",
                params={"query": keyword, "page_size": per_page},
            )
            candidates = _coverr_candidates(payload, keyword)
            if not candidates:
                return MaterialSourceItem("Coverr", "video", "失败", reason="Coverr 没有返回可下载 MP4。")
            for candidate in candidates:
                item = self._download_candidate(candidate, self.paths.coverr_video_dir)
                if item.status == "已下载":
                    return item
            return MaterialSourceItem("Coverr", "video", "失败", reason="Coverr 结果全部重复或下载失败。")
        except Exception as exc:
            return MaterialSourceItem("Coverr", "video", "失败", reason=f"Coverr 暂时不可用：{exc}")


def _pixabay_video_candidate(raw: Any, keyword: str) -> MaterialCandidate | None:
    if not isinstance(raw, dict):
        return None
    videos = raw.get("videos")
    if not isinstance(videos, dict):
        return None
    files = [item for item in videos.values() if isinstance(item, dict) and item.get("url")]
    qualified = [
        item
        for item in files
        if int(item.get("width") or 0) >= MIN_VIDEO_DIMENSION
        and int(item.get("height") or 0) >= MIN_VIDEO_DIMENSION
    ]
    if not qualified:
        return None
    best = max(qualified, key=lambda item: (int(item.get("height") or 0) >= int(item.get("width") or 0), int(item.get("width") or 0) * int(item.get("height") or 0)))
    source_id = str(raw.get("id") or best.get("url") or "")
    if not source_id:
        return None
    source_url = str(raw.get("pageURL") or "")
    contributor_name = str(raw.get("user") or "")
    try:
        rights_evidence = build_stock_rights_evidence(
            provider="Pixabay",
            provider_asset_id=source_id,
            media_type="video",
            asset_page_url=source_url,
            contributor_id=raw.get("user_id"),
            contributor_name=contributor_name,
            search_query=keyword,
        )
    except StockRightsEvidenceError:
        return None
    return MaterialCandidate(
        source_name="Pixabay",
        source_id=f"pixabay-video-{source_id}",
        asset_type="video",
        title=f"{keyword} Pixabay视频 {source_id}",
        keyword=keyword,
        download_url=str(best.get("url")),
        source_url=source_url,
        width=int(best.get("width") or 0),
        height=int(best.get("height") or 0),
        duration_seconds=float(raw.get("duration") or 0),
        contributor_id=str(raw.get("user_id") or ""),
        contributor_name=contributor_name,
        rights_evidence=rights_evidence,
    )


def _pixabay_photo_candidate(raw: Any, keyword: str) -> MaterialCandidate | None:
    if not isinstance(raw, dict):
        return None
    download_url = raw.get("largeImageURL") or raw.get("webformatURL")
    source_id = str(raw.get("id") or "")
    if not download_url or not source_id:
        return None
    source_url = str(raw.get("pageURL") or "")
    contributor_name = str(raw.get("user") or "")
    try:
        rights_evidence = build_stock_rights_evidence(
            provider="Pixabay",
            provider_asset_id=source_id,
            media_type="photo",
            asset_page_url=source_url,
            contributor_id=raw.get("user_id"),
            contributor_name=contributor_name,
            search_query=keyword,
        )
    except StockRightsEvidenceError:
        return None
    return MaterialCandidate(
        source_name="Pixabay",
        source_id=f"pixabay-photo-{source_id}",
        asset_type="photo",
        title=f"{keyword} Pixabay图片 {source_id}",
        keyword=keyword,
        download_url=str(download_url),
        source_url=source_url,
        width=int(raw.get("imageWidth") or raw.get("webformatWidth") or 0),
        height=int(raw.get("imageHeight") or raw.get("webformatHeight") or 0),
        contributor_id=str(raw.get("user_id") or ""),
        contributor_name=contributor_name,
        rights_evidence=rights_evidence,
    )


def _coverr_candidates(payload: dict[str, Any], keyword: str) -> list[MaterialCandidate]:
    raw_items = payload.get("videos") or payload.get("items") or payload.get("data") or payload.get("results")
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("data") or raw_items.get("items") or []
    if not isinstance(raw_items, list):
        return []
    candidates: list[MaterialCandidate] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        download_url = _find_coverr_mp4(raw)
        if not download_url:
            continue
        source_id = str(raw.get("id") or raw.get("uuid") or raw.get("slug") or download_url)
        title = str(raw.get("title") or raw.get("name") or keyword)
        candidates.append(
            MaterialCandidate(
                source_name="Coverr",
                source_id=f"coverr-video-{source_id}",
                asset_type="video",
                title=f"{title} Coverr视频",
                keyword=keyword,
                download_url=download_url,
                source_url=str(raw.get("url") or raw.get("page_url") or ""),
                width=int(raw.get("width") or 0),
                height=int(raw.get("height") or 0),
            )
        )
    return candidates


def _find_coverr_mp4(raw: dict[str, Any]) -> str:
    queue: list[Any] = [raw]
    seen = 0
    while queue and seen < 80:
        seen += 1
        item = queue.pop(0)
        if isinstance(item, str) and ".mp4" in item.lower():
            return item
        if isinstance(item, dict):
            queue.extend(item.values())
        elif isinstance(item, list):
            queue.extend(item)
    return ""


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\r\n\t]+', "_", value).strip(" ._")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:80] or "未命名素材"


def _suffix_from_url(url: str) -> str:
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    return suffix if suffix else ""


def _download_client_to(
    client: MaterialHttpClient,
    url: str,
    destination: Path,
    *,
    source_name: str,
    asset_type: str,
) -> None:
    streaming_download = getattr(client, "download_to", None)
    if callable(streaming_download):
        streaming_download(
            url,
            destination,
            source_name=source_name,
            asset_type=asset_type,
            headers=None,
        )
        return
    content = client.get_bytes(url)
    max_bytes = _max_download_bytes(asset_type)
    if not content:
        raise RuntimeError("素材文件下载失败：返回空内容。")
    if len(content) > max_bytes:
        raise RuntimeError(f"素材文件下载失败：超过 {max_bytes} 字节上限。")
    with destination.open("xb") as handle:
        handle.write(content)
        handle.flush()


def _max_download_bytes(asset_type: str) -> int:
    if asset_type == "video":
        return MAX_SOURCE_VIDEO_BYTES
    if asset_type == "photo":
        return MAX_SOURCE_PHOTO_BYTES
    raise RuntimeError(f"不支持的素材类型：{asset_type}。")


def _allowed_mimes(asset_type: str) -> frozenset[str]:
    if asset_type == "video":
        return frozenset({"video/mp4", "video/quicktime", "video/webm"})
    if asset_type == "photo":
        return frozenset({"image/jpeg", "image/png", "image/webp"})
    raise RuntimeError(f"不支持的素材类型：{asset_type}。")
