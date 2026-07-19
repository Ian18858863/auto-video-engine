from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.materials.asset_registry import MaterialFingerprintRegistry, sha256_file
from app.materials.asset_paths import AutoMaterialPaths
from app.materials.safe_http_download import SafeDownloadReceipt, SafeHttpDownloader
from app.materials.stock_rights_evidence import (
    StockRightsEvidenceError,
    build_stock_rights_evidence,
)


MIN_VERTICAL_EXPORT_DIMENSION = 1080
MAX_PEXELS_VIDEO_BYTES = 512 * 1024 * 1024
MAX_PEXELS_PHOTO_BYTES = 64 * 1024 * 1024


@dataclass(slots=True)
class PexelsAssetCandidate:
    pexels_id: str
    asset_type: str
    title: str
    keyword: str
    download_url: str
    width: int = 0
    height: int = 0
    duration_seconds: float = 0.0
    photographer: str = ""
    source_url: str = ""
    contributor_id: str = ""
    contributor_profile_url: str = ""
    rights_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PexelsDownloadItem:
    asset_type: str
    status: str
    save_path: str = ""
    source_url: str = ""
    fingerprint: str = ""
    reason: str = ""
    rights_evidence: dict[str, Any] = field(default_factory=dict)

    def to_chinese_dict(self) -> dict[str, Any]:
        return {
            "类型": "视频" if self.asset_type == "video" else "图片",
            "状态": self.status,
            "保存路径": self.save_path,
            "来源链接": self.source_url,
            "文件指纹": self.fingerprint,
            "失败原因": self.reason,
            "权利证据": deepcopy(self.rights_evidence),
        }


@dataclass(slots=True)
class PexelsSampleDownloadResult:
    keyword: str
    success: bool
    video: PexelsDownloadItem
    photo: PexelsDownloadItem
    generated_at: datetime

    def to_chinese_dict(self) -> dict[str, Any]:
        failure_reasons = [
            item.reason
            for item in [self.video, self.photo]
            if item.status != "已下载" and item.reason
        ]
        return {
            "成功": self.success,
            "关键词": self.keyword,
            "生成时间": self.generated_at.isoformat(timespec="seconds"),
            "视频": self.video.to_chinese_dict(),
            "图片": self.photo.to_chinese_dict(),
            "失败原因": failure_reasons,
        }


class PexelsDownloadError(RuntimeError):
    pass


class PexelsHttpClient(Protocol):
    def get_json(self, url: str, *, headers: dict[str, str], params: dict[str, Any]) -> dict[str, Any]:
        ...

    def get_bytes(self, url: str, *, headers: dict[str, str]) -> bytes:
        ...


class HttpxPexelsHttpClient:
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

    def get_json(self, url: str, *, headers: dict[str, str], params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = httpx.get(
                url,
                headers=headers,
                params=params,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise PexelsDownloadError(f"Pexels 请求失败：{exc}") from exc
        self._raise_for_status(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise PexelsDownloadError("Pexels 返回内容格式不正确。")
        return payload

    def get_bytes(self, url: str, *, headers: dict[str, str]) -> bytes:
        try:
            response = httpx.get(url, headers=headers, timeout=self.timeout_seconds)
        except httpx.HTTPError as exc:
            raise PexelsDownloadError(f"素材下载失败：{exc}") from exc
        self._raise_for_status(response)
        if not response.content:
            raise PexelsDownloadError("素材下载失败：返回空内容。")
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
        if source_name != "Pexels":
            raise PexelsDownloadError(f"未批准的 Pexels 下载源：{source_name}。")
        return self.downloader.download(
            url,
            destination,
            headers=headers,
            allowed_host_suffixes={"pexels.com"},
            allowed_mime_types=_allowed_mimes(asset_type),
            max_bytes=_max_download_bytes(asset_type),
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 401:
            raise PexelsDownloadError("Pexels 鉴权失败，请检查 API Key 文件。")
        if response.status_code == 429:
            raise PexelsDownloadError("Pexels 请求过多，请稍后再试。")
        if response.status_code >= 400:
            raise PexelsDownloadError(f"Pexels 请求失败：HTTP {response.status_code}。")


class PexelsAssetClient:
    def __init__(self, paths: AutoMaterialPaths | None = None):
        self.paths = paths or AutoMaterialPaths()

    def api_key_status(self) -> str:
        self.paths.read_pexels_api_key()
        return "已读取 Pexels API Key"

    def target_path(
        self,
        candidate: PexelsAssetCandidate,
        *,
        fetched_at: datetime | None = None,
    ) -> Path:
        fetched_at = fetched_at or datetime.now()
        directory = (
            self.paths.pexels_video_dir
            if candidate.asset_type == "video"
            else self.paths.pexels_photo_dir
        )
        suffix = _suffix_from_url(candidate.download_url)
        if candidate.asset_type == "video" and suffix not in {".mp4", ".mov", ".webm"}:
            suffix = ".mp4"
        if candidate.asset_type == "photo" and suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            suffix = ".jpg"
        filename = (
            f"{_safe_filename(candidate.title or candidate.keyword)}_"
            f"{fetched_at:%Y%m%d_%H%M%S}_"
            f"{_safe_filename(candidate.pexels_id)}{suffix}"
        )
        return directory / filename

    def is_duplicate(self, candidate: PexelsAssetCandidate) -> bool:
        target_dirs = [self.paths.pexels_video_dir, self.paths.pexels_photo_dir]
        marker = _safe_filename(candidate.pexels_id)
        return any(directory.exists() and any(marker in item.name for item in directory.iterdir()) for directory in target_dirs)


class PexelsDownloadService:
    def __init__(
        self,
        paths: AutoMaterialPaths | None = None,
        *,
        api_key: str | None = None,
        http_client: PexelsHttpClient | None = None,
        fingerprint_registry: MaterialFingerprintRegistry | None = None,
    ):
        self.paths = paths or AutoMaterialPaths()
        self.api_key = api_key.strip() if isinstance(api_key, str) else None
        self.asset_client = PexelsAssetClient(paths=self.paths)
        self.http_client = http_client or HttpxPexelsHttpClient()
        self.fingerprint_registry = fingerprint_registry or MaterialFingerprintRegistry(
            self.paths.fingerprint_registry_path
        )

    def download_one_video_and_one_photo(
        self,
        keyword: str,
        *,
        per_page: int = 10,
        api_key: str | None = None,
    ) -> PexelsSampleDownloadResult:
        per_page = max(1, min(per_page, 10))
        generated_at = datetime.now()
        try:
            self.paths.ensure_directories()
            resolved_api_key = (
                api_key.strip()
                if isinstance(api_key, str) and api_key.strip()
                else self.api_key
            )
            if not resolved_api_key:
                resolved_api_key = self.paths.read_pexels_api_key()
        except Exception as exc:
            reason = str(exc)
            return PexelsSampleDownloadResult(
                keyword=keyword,
                success=False,
                video=PexelsDownloadItem(asset_type="video", status="失败", reason=reason),
                photo=PexelsDownloadItem(asset_type="photo", status="失败", reason=reason),
                generated_at=generated_at,
            )
        headers = {"Authorization": resolved_api_key}

        video = self._download_first_video(keyword, headers=headers, per_page=per_page)
        photo = self._download_first_photo(keyword, headers=headers, per_page=per_page)

        return PexelsSampleDownloadResult(
            keyword=keyword,
            success=video.status == "已下载" and photo.status == "已下载",
            video=video,
            photo=photo,
            generated_at=generated_at,
        )

    def _download_first_video(
        self,
        keyword: str,
        *,
        headers: dict[str, str],
        per_page: int,
    ) -> PexelsDownloadItem:
        try:
            payload = self.http_client.get_json(
                "https://api.pexels.com/videos/search",
                headers=headers,
                params={"query": keyword, "per_page": per_page},
            )
            videos = payload.get("videos")
            if not isinstance(videos, list) or not videos:
                return PexelsDownloadItem(
                    asset_type="video",
                    status="失败",
                    reason="Pexels 没有找到视频结果。",
                )
            saw_low_resolution_video = False
            for raw_video in videos:
                if _has_low_resolution_mp4(raw_video):
                    saw_low_resolution_video = True
                candidate = _candidate_from_video(raw_video, keyword)
                if candidate is None:
                    continue
                item = self._download_candidate(candidate)
                if item.status == "已下载":
                    return item
            if saw_low_resolution_video:
                return PexelsDownloadItem(
                    asset_type="video",
                    status="失败",
                    reason="Pexels 视频结果低于 1080p，竖屏导出会变糊，已拒绝下载。",
                )
            return PexelsDownloadItem(
                asset_type="video",
                status="失败",
                reason="Pexels 视频结果没有可下载的 mp4 文件。",
            )
        except Exception as exc:
            return PexelsDownloadItem(
                asset_type="video",
                status="失败",
                reason=str(exc),
            )

    def _download_first_photo(
        self,
        keyword: str,
        *,
        headers: dict[str, str],
        per_page: int,
    ) -> PexelsDownloadItem:
        try:
            payload = self.http_client.get_json(
                "https://api.pexels.com/v1/search",
                headers=headers,
                params={"query": keyword, "per_page": per_page},
            )
            photos = payload.get("photos")
            if not isinstance(photos, list) or not photos:
                return PexelsDownloadItem(
                    asset_type="photo",
                    status="失败",
                    reason="Pexels 没有找到图片结果。",
                )
            for raw_photo in photos:
                candidate = _candidate_from_photo(raw_photo, keyword)
                if candidate is None:
                    continue
                item = self._download_candidate(candidate)
                if item.status == "已下载":
                    return item
            return PexelsDownloadItem(
                asset_type="photo",
                status="失败",
                reason="Pexels 图片结果没有可下载文件。",
            )
        except Exception as exc:
            return PexelsDownloadItem(
                asset_type="photo",
                status="失败",
                reason=str(exc),
            )

    def _download_candidate(self, candidate: PexelsAssetCandidate) -> PexelsDownloadItem:
        if self.asset_client.is_duplicate(candidate):
            return PexelsDownloadItem(
                asset_type=candidate.asset_type,
                status="跳过",
                source_url=candidate.source_url,
                reason="已存在相同 Pexels ID 的素材。",
                rights_evidence=deepcopy(candidate.rights_evidence),
            )
        target_path = self.asset_client.target_path(candidate)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target_path.with_name(f"{target_path.name}.part")
        download_completed = False
        try:
            _download_client_to(
                self.http_client,
                candidate.download_url,
                temp_path,
                asset_type=candidate.asset_type,
            )
            download_completed = True
            duplicate = self.fingerprint_registry.find_duplicate(temp_path)
            if duplicate is not None:
                temp_path.unlink(missing_ok=True)
                return PexelsDownloadItem(
                    asset_type=candidate.asset_type,
                    status="跳过",
                    source_url=candidate.source_url,
                    fingerprint=duplicate.fingerprint,
                    reason=f"文件指纹重复，已存在：{duplicate.file_path}",
                    rights_evidence=deepcopy(candidate.rights_evidence),
                )
            temp_path.replace(target_path)
            fingerprint = sha256_file(target_path)
            self.fingerprint_registry.record_file(
                target_path,
                source_type="Pexels视频" if candidate.asset_type == "video" else "Pexels图片",
            )
            return PexelsDownloadItem(
                asset_type=candidate.asset_type,
                status="已下载",
                save_path=str(target_path),
                source_url=candidate.source_url,
                fingerprint=fingerprint,
                rights_evidence=deepcopy(candidate.rights_evidence),
            )
        except Exception as exc:
            if download_completed:
                temp_path.unlink(missing_ok=True)
            return PexelsDownloadItem(
                asset_type=candidate.asset_type,
                status="失败",
                source_url=candidate.source_url,
                reason=f"素材下载失败：{exc}",
                rights_evidence=deepcopy(candidate.rights_evidence),
            )


def _suffix_from_url(url: str) -> str:
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    return suffix if suffix else ""


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\r\n\t]+', "_", value).strip(" ._")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:80] or "未命名素材"


def _download_client_to(
    client: PexelsHttpClient,
    url: str,
    destination: Path,
    *,
    asset_type: str,
) -> None:
    streaming_download = getattr(client, "download_to", None)
    if callable(streaming_download):
        streaming_download(
            url,
            destination,
            source_name="Pexels",
            asset_type=asset_type,
            headers={},
        )
        return
    content = client.get_bytes(url, headers={})
    max_bytes = _max_download_bytes(asset_type)
    if not content:
        raise PexelsDownloadError("素材下载失败：返回空内容。")
    if len(content) > max_bytes:
        raise PexelsDownloadError(f"素材下载失败：超过 {max_bytes} 字节上限。")
    with destination.open("xb") as handle:
        handle.write(content)
        handle.flush()


def _max_download_bytes(asset_type: str) -> int:
    if asset_type == "video":
        return MAX_PEXELS_VIDEO_BYTES
    if asset_type == "photo":
        return MAX_PEXELS_PHOTO_BYTES
    raise PexelsDownloadError(f"不支持的 Pexels 素材类型：{asset_type}。")


def _allowed_mimes(asset_type: str) -> frozenset[str]:
    if asset_type == "video":
        return frozenset({"video/mp4", "video/quicktime", "video/webm"})
    if asset_type == "photo":
        return frozenset({"image/jpeg", "image/png", "image/webp"})
    raise PexelsDownloadError(f"不支持的 Pexels 素材类型：{asset_type}。")


def _candidate_from_video(raw_video: Any, keyword: str) -> PexelsAssetCandidate | None:
    if not isinstance(raw_video, dict):
        return None
    files = raw_video.get("video_files")
    if not isinstance(files, list):
        return None
    mp4_files = [
        item
        for item in files
        if isinstance(item, dict)
        and str(item.get("file_type", "")).lower().startswith("video/mp4")
        and item.get("link")
    ]
    if not mp4_files:
        return None
    qualified_files = [
        item
        for item in mp4_files
        if _is_export_quality_video_file(item)
    ]
    if not qualified_files:
        return None
    best_file = max(qualified_files, key=_video_quality_score)
    user = raw_video.get("user") if isinstance(raw_video.get("user"), dict) else {}
    pexels_id = str(raw_video.get("id") or best_file.get("id") or "")
    if not pexels_id:
        return None
    source_url = str(raw_video.get("url") or "")
    contributor_name = str(user.get("name") or "")
    try:
        rights_evidence = build_stock_rights_evidence(
            provider="Pexels",
            provider_asset_id=pexels_id,
            media_type="video",
            asset_page_url=source_url,
            contributor_id=user.get("id"),
            contributor_name=contributor_name,
            contributor_profile_url=str(user.get("url") or ""),
            search_query=keyword,
        )
    except StockRightsEvidenceError:
        return None
    return PexelsAssetCandidate(
        pexels_id=f"pexels-video-{pexels_id}",
        asset_type="video",
        title=f"{keyword} Pexels视频 {pexels_id}",
        keyword=keyword,
        download_url=str(best_file.get("link")),
        width=int(best_file.get("width") or raw_video.get("width") or 0),
        height=int(best_file.get("height") or raw_video.get("height") or 0),
        duration_seconds=float(raw_video.get("duration") or 0),
        photographer=contributor_name,
        source_url=source_url,
        contributor_id=str(user.get("id") or ""),
        contributor_profile_url=str(user.get("url") or ""),
        rights_evidence=rights_evidence,
    )


def _has_low_resolution_mp4(raw_video: Any) -> bool:
    if not isinstance(raw_video, dict):
        return False
    files = raw_video.get("video_files")
    if not isinstance(files, list):
        return False
    mp4_files = [
        item
        for item in files
        if isinstance(item, dict)
        and str(item.get("file_type", "")).lower().startswith("video/mp4")
        and item.get("link")
    ]
    return bool(mp4_files) and not any(_is_export_quality_video_file(item) for item in mp4_files)


def _is_export_quality_video_file(item: dict[str, Any]) -> bool:
    width = int(item.get("width") or 0)
    height = int(item.get("height") or 0)
    return width >= MIN_VERTICAL_EXPORT_DIMENSION and height >= MIN_VERTICAL_EXPORT_DIMENSION


def _video_quality_score(item: dict[str, Any]) -> tuple[int, int, int]:
    width = int(item.get("width") or 0)
    height = int(item.get("height") or 0)
    is_portrait = 1 if height >= width else 0
    min_side = min(width, height)
    area = width * height
    return (is_portrait, min_side, area)


def _candidate_from_photo(raw_photo: Any, keyword: str) -> PexelsAssetCandidate | None:
    if not isinstance(raw_photo, dict):
        return None
    src = raw_photo.get("src")
    if not isinstance(src, dict):
        return None
    download_url = src.get("large2x") or src.get("large") or src.get("original")
    pexels_id = str(raw_photo.get("id") or "")
    if not download_url or not pexels_id:
        return None
    source_url = str(raw_photo.get("url") or "")
    contributor_name = str(raw_photo.get("photographer") or "")
    try:
        rights_evidence = build_stock_rights_evidence(
            provider="Pexels",
            provider_asset_id=pexels_id,
            media_type="photo",
            asset_page_url=source_url,
            contributor_id=raw_photo.get("photographer_id"),
            contributor_name=contributor_name,
            contributor_profile_url=str(raw_photo.get("photographer_url") or ""),
            search_query=keyword,
        )
    except StockRightsEvidenceError:
        return None
    return PexelsAssetCandidate(
        pexels_id=f"pexels-photo-{pexels_id}",
        asset_type="photo",
        title=str(raw_photo.get("alt") or f"{keyword} Pexels图片 {pexels_id}"),
        keyword=keyword,
        download_url=str(download_url),
        width=int(raw_photo.get("width") or 0),
        height=int(raw_photo.get("height") or 0),
        photographer=contributor_name,
        source_url=source_url,
        contributor_id=str(raw_photo.get("photographer_id") or ""),
        contributor_profile_url=str(raw_photo.get("photographer_url") or ""),
        rights_evidence=rights_evidence,
    )
