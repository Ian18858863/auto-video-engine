"""Bounded, provider-scoped HTTP downloads for material acquisition.

The source APIs return third-party CDN URLs.  Those URLs are untrusted input:
redirects can change their destination, response bodies can be unbounded, and
``Content-Type`` can describe an error page instead of media.  This module is
the network admission boundary.  Byte-level media decoding still belongs to
``candidate_media_validation`` after the download finishes.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import os
from pathlib import Path
import socket
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SENSITIVE_REDIRECT_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie"}
)
_FORBIDDEN_REQUEST_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection"}
)
_CONTENT_TYPE_ALIASES = {
    "image/jpg": "image/jpeg",
    "video/x-m4v": "video/mp4",
}


class SafeDownloadError(RuntimeError):
    """A remote response cannot safely cross the local file boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SafeDownloadReceipt:
    final_url: str
    content_type: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_url": self.final_url,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


Resolver = Callable[..., Iterable[tuple[Any, Any, Any, Any, Any]]]


class SafeHttpDownloader:
    """Stream one HTTPS response to an exclusive D:/non-system-drive file."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_redirects: int = 3,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = socket.getaddrinfo,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if type(max_redirects) is not int or max_redirects < 0:
            raise ValueError("max_redirects must be a non-negative integer")
        self.timeout_seconds = float(timeout_seconds)
        self.max_redirects = max_redirects
        self.transport = transport
        self.resolver = resolver

    def download(
        self,
        url: str,
        destination: Path | str,
        *,
        allowed_host_suffixes: set[str] | frozenset[str],
        allowed_mime_types: set[str] | frozenset[str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> SafeDownloadReceipt:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        host_suffixes = _normalise_host_suffixes(allowed_host_suffixes)
        mime_types = _normalise_allowed_mimes(allowed_mime_types)
        target = _validate_destination(destination)
        current_url = _normalise_and_validate_url(url, host_suffixes)
        current_headers = _normalise_headers(headers)

        try:
            with httpx.Client(
                transport=self.transport,
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                for redirect_count in range(self.max_redirects + 1):
                    host, port = _request_host(current_url, host_suffixes)
                    _require_public_dns(host, port, self.resolver)
                    try:
                        response_context = client.stream(
                            "GET",
                            current_url,
                            headers=current_headers,
                        )
                        with response_context as response:
                            if response.status_code in _REDIRECT_STATUSES:
                                if redirect_count >= self.max_redirects:
                                    raise SafeDownloadError(
                                        "redirect_limit_exceeded",
                                        "素材下载重定向次数超过安全上限。",
                                    )
                                location = response.headers.get("Location", "").strip()
                                if not location:
                                    raise SafeDownloadError(
                                        "invalid_redirect",
                                        "素材下载响应缺少重定向地址。",
                                    )
                                next_url = _normalise_and_validate_url(
                                    urljoin(current_url, location),
                                    host_suffixes,
                                )
                                if _origin(next_url) != _origin(current_url):
                                    current_headers = {
                                        name: value
                                        for name, value in current_headers.items()
                                        if name.casefold() not in _SENSITIVE_REDIRECT_HEADERS
                                    }
                                current_url = next_url
                                continue
                            _raise_for_status(response)
                            content_type = _response_content_type(response)
                            if content_type not in mime_types:
                                raise SafeDownloadError(
                                    "content_type_rejected",
                                    f"素材响应类型 {content_type or 'missing'} 不在允许范围。",
                                )
                            _reject_content_encoding(response)
                            declared_length = _declared_content_length(response)
                            if declared_length is not None and declared_length > max_bytes:
                                raise SafeDownloadError(
                                    "size_limit_exceeded",
                                    f"素材响应超过 {max_bytes} 字节上限。",
                                )
                            return _write_response(
                                response,
                                target,
                                final_url=current_url,
                                content_type=content_type,
                                max_bytes=max_bytes,
                                declared_length=declared_length,
                            )
                    except SafeDownloadError:
                        raise
                    except httpx.HTTPError as exc:
                        raise SafeDownloadError(
                            "network_error",
                            f"素材下载网络请求失败：{exc}",
                        ) from exc
        except SafeDownloadError:
            raise
        except OSError as exc:
            raise SafeDownloadError("file_write_failed", "素材下载文件无法安全写入。") from exc

        raise SafeDownloadError("download_failed", "素材下载未返回可用文件。")


def _normalise_host_suffixes(values: Iterable[str]) -> frozenset[str]:
    result: set[str] = set()
    for value in values:
        text = str(value).strip().casefold().strip(".")
        if not text or "/" in text or ":" in text:
            raise ValueError("allowed_host_suffixes contains an invalid host")
        try:
            result.add(text.encode("idna").decode("ascii"))
        except UnicodeError as exc:
            raise ValueError("allowed_host_suffixes contains an invalid host") from exc
    if not result:
        raise ValueError("allowed_host_suffixes must not be empty")
    return frozenset(result)


def _normalise_allowed_mimes(values: Iterable[str]) -> frozenset[str]:
    result = {
        _normalise_content_type(str(value))
        for value in values
        if str(value).strip()
    }
    if not result or "" in result:
        raise ValueError("allowed_mime_types must contain valid MIME types")
    return frozenset(result)


def _normalise_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_name, raw_value in (headers or {}).items():
        name = str(raw_name).strip()
        value = str(raw_value).strip()
        folded = name.casefold()
        if not name or folded in _FORBIDDEN_REQUEST_HEADERS:
            raise ValueError(f"unsafe request header: {name or 'empty'}")
        if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
            raise ValueError("request headers must not contain line breaks")
        if folded == "accept-encoding":
            continue
        result[name] = value
    result["Accept-Encoding"] = "identity"
    return result


def _normalise_and_validate_url(url: str, host_suffixes: frozenset[str]) -> str:
    text = str(url).strip()
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise SafeDownloadError("unsafe_url", "素材下载地址格式不正确。") from exc
    if parsed.scheme.casefold() != "https":
        raise SafeDownloadError("unsafe_scheme", "素材下载只允许 HTTPS 地址。")
    if parsed.username is not None or parsed.password is not None:
        raise SafeDownloadError("unsafe_url", "素材下载地址不能包含账号信息。")
    if parsed.fragment:
        raise SafeDownloadError("unsafe_url", "素材下载地址不能包含片段标识。")
    if port not in (None, 443):
        raise SafeDownloadError("unsafe_port", "素材下载只允许 HTTPS 默认端口。")
    host = _normalise_host(parsed.hostname)
    _require_allowed_host(host, host_suffixes)
    netloc = host if ":" not in host else f"[{host}]"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def _normalise_host(value: str | None) -> str:
    if not value:
        raise SafeDownloadError("unsafe_url", "素材下载地址缺少主机名。")
    text = value.casefold().rstrip(".")
    try:
        return text.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise SafeDownloadError("unsafe_url", "素材下载主机名格式不正确。") from exc


def _require_allowed_host(host: str, suffixes: frozenset[str]) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise SafeDownloadError("unsafe_address", "素材下载地址指向非公网 IP。")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes):
        raise SafeDownloadError("host_not_allowed", "素材下载地址不属于已批准的素材源。")


def _request_host(url: str, suffixes: frozenset[str]) -> tuple[str, int]:
    parsed = urlsplit(url)
    host = _normalise_host(parsed.hostname)
    _require_allowed_host(host, suffixes)
    return host, parsed.port or 443


def _require_public_dns(host: str, port: int, resolver: Resolver) -> None:
    try:
        results = list(resolver(host, port, type=socket.SOCK_STREAM))
    except (OSError, socket.gaierror) as exc:
        raise SafeDownloadError("dns_failed", "素材下载主机无法安全解析。") from exc
    if not results:
        raise SafeDownloadError("dns_failed", "素材下载主机没有可用地址。")
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    try:
        for result in results:
            sockaddr = result[4]
            addresses.append(ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0]))
    except (IndexError, TypeError, ValueError) as exc:
        raise SafeDownloadError("dns_failed", "素材下载主机解析结果不可信。") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise SafeDownloadError("unsafe_address", "素材下载主机解析到非公网 IP。")


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    return parsed.scheme.casefold(), _normalise_host(parsed.hostname), parsed.port or 443


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 200 or response.status_code >= 300:
        raise SafeDownloadError(
            "http_error",
            f"素材下载失败：HTTP {response.status_code}。",
        )


def _normalise_content_type(value: str) -> str:
    text = value.split(";", 1)[0].strip().casefold()
    return _CONTENT_TYPE_ALIASES.get(text, text)


def _response_content_type(response: httpx.Response) -> str:
    return _normalise_content_type(response.headers.get("Content-Type", ""))


def _reject_content_encoding(response: httpx.Response) -> None:
    encoding = response.headers.get("Content-Encoding", "").strip().casefold()
    if encoding and encoding != "identity":
        raise SafeDownloadError(
            "content_encoding_rejected",
            "素材下载响应使用了不允许的压缩编码。",
        )


def _declared_content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("Content-Length")
    if raw is None:
        return None
    text = raw.strip()
    if not text.isdecimal():
        raise SafeDownloadError("invalid_content_length", "素材响应大小字段不可信。")
    return int(text)


def _validate_destination(destination: Path | str) -> Path:
    raw = Path(destination)
    if not raw.is_absolute():
        raise SafeDownloadError("unsafe_path", "素材下载目标必须是绝对路径。")
    if os.name == "nt" and raw.drive.casefold() == "c:":
        raise SafeDownloadError("unsafe_path", "素材下载目标不能位于系统盘。")
    _reject_link_components(raw.parent)
    try:
        parent = raw.parent.resolve(strict=True)
    except OSError as exc:
        raise SafeDownloadError("unsafe_path", "素材下载目标目录不存在或不可用。") from exc
    _reject_link_components(parent)
    if not parent.is_dir():
        raise SafeDownloadError("unsafe_path", "素材下载目标目录不可用。")
    target = parent / raw.name
    if target.exists():
        raise SafeDownloadError("target_exists", "素材下载目标已存在，拒绝覆盖。")
    return target


def _reject_link_components(path: Path) -> None:
    for component in (path, *path.parents):
        try:
            is_junction = bool(
                getattr(os.path, "isjunction", lambda _path: False)(component)
            )
            if component.is_symlink() or is_junction:
                raise SafeDownloadError("unsafe_path", "素材下载路径不能经过链接或联接点。")
        except OSError as exc:
            raise SafeDownloadError("unsafe_path", "素材下载路径无法安全检查。") from exc


def _write_response(
    response: httpx.Response,
    destination: Path,
    *,
    final_url: str,
    content_type: str,
    max_bytes: int,
    declared_length: int | None,
) -> SafeDownloadReceipt:
    digest = hashlib.sha256()
    total = 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= int(getattr(os, "O_BINARY", 0))
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(destination, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            for chunk in response.iter_raw():
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise SafeDownloadError(
                        "size_limit_exceeded",
                        f"素材响应超过 {max_bytes} 字节上限。",
                    )
                handle.write(chunk)
                digest.update(chunk)
            if total <= 0:
                raise SafeDownloadError("empty_response", "素材下载返回空内容。")
            if declared_length is not None and total != declared_length:
                raise SafeDownloadError(
                    "content_length_mismatch",
                    "素材实际大小与响应声明不一致。",
                )
            handle.flush()
            os.fsync(handle.fileno())
        if destination.stat().st_size != total:
            raise SafeDownloadError("file_write_failed", "素材下载文件写入不完整。")
        return SafeDownloadReceipt(
            final_url=final_url,
            content_type=content_type,
            size_bytes=total,
            sha256=digest.hexdigest(),
        )
    except SafeDownloadError:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            destination.unlink(missing_ok=True)
        raise SafeDownloadError("file_write_failed", "素材下载文件无法安全写入。") from exc


__all__ = ["SafeDownloadError", "SafeDownloadReceipt", "SafeHttpDownloader"]
