from __future__ import annotations

import hashlib
import socket
from pathlib import Path

import httpx
import pytest

from app.materials.safe_http_download import SafeDownloadError, SafeHttpDownloader


PUBLIC_IP = "93.184.216.34"


class _ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.iterated = False

    def __iter__(self):
        self.iterated = True
        yield from self.chunks


def _public_resolver(host: str, port: int, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]


def _downloader(handler) -> SafeHttpDownloader:
    return SafeHttpDownloader(
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )


def test_streams_bounded_https_payload_to_destination(tmp_path: Path):
    body = [b"abc", b"def"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://videos.pexels.com/video.mp4"
        return httpx.Response(
            200,
            headers={"Content-Type": "video/mp4", "Content-Length": "6"},
            stream=_ChunkStream(body),
        )

    destination = tmp_path / "candidate.mp4.part"
    receipt = _downloader(handler).download(
        "https://videos.pexels.com/video.mp4",
        destination,
        allowed_host_suffixes={"pexels.com"},
        allowed_mime_types={"video/mp4"},
        max_bytes=10,
    )

    assert destination.read_bytes() == b"abcdef"
    assert receipt.final_url == "https://videos.pexels.com/video.mp4"
    assert receipt.content_type == "video/mp4"
    assert receipt.size_bytes == 6
    assert receipt.sha256 == hashlib.sha256(b"abcdef").hexdigest()


def test_rejects_declared_oversize_before_iterating_body(tmp_path: Path):
    stream = _ChunkStream([b"must-not-be-read"])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "image/jpeg", "Content-Length": "999"},
            stream=stream,
        )

    destination = tmp_path / "oversize.jpg.part"
    with pytest.raises(SafeDownloadError) as error:
        _downloader(handler).download(
            "https://images.pexels.com/photo.jpg",
            destination,
            allowed_host_suffixes={"pexels.com"},
            allowed_mime_types={"image/jpeg"},
            max_bytes=8,
        )

    assert error.value.code == "size_limit_exceeded"
    assert stream.iterated is False
    assert not destination.exists()


def test_stops_chunked_body_at_limit_and_removes_partial_file(tmp_path: Path):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "image/jpeg"},
            stream=_ChunkStream([b"1234", b"5678", b"9"]),
        )

    destination = tmp_path / "chunked.jpg.part"
    with pytest.raises(SafeDownloadError) as error:
        _downloader(handler).download(
            "https://cdn.pixabay.com/photo.jpg",
            destination,
            allowed_host_suffixes={"pixabay.com"},
            allowed_mime_types={"image/jpeg"},
            max_bytes=8,
        )

    assert error.value.code == "size_limit_exceeded"
    assert not destination.exists()


def test_rejects_redirect_to_private_address_before_second_request(tmp_path: Path):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/private.mp4"})

    destination = tmp_path / "private.mp4.part"
    with pytest.raises(SafeDownloadError) as error:
        _downloader(handler).download(
            "https://videos.pexels.com/video.mp4",
            destination,
            allowed_host_suffixes={"pexels.com"},
            allowed_mime_types={"video/mp4"},
            max_bytes=100,
        )

    assert error.value.code == "unsafe_address"
    assert seen == ["https://videos.pexels.com/video.mp4"]
    assert not destination.exists()


def test_rejects_provider_lookalike_host_without_resolving_it(tmp_path: Path):
    resolver_called = False

    def resolver(*_args, **_kwargs):
        nonlocal resolver_called
        resolver_called = True
        return []

    downloader = SafeHttpDownloader(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
        resolver=resolver,
    )
    with pytest.raises(SafeDownloadError) as error:
        downloader.download(
            "https://pexels.com.evil.test/file.jpg",
            tmp_path / "lookalike.jpg.part",
            allowed_host_suffixes={"pexels.com"},
            allowed_mime_types={"image/jpeg"},
            max_bytes=100,
        )

    assert error.value.code == "host_not_allowed"
    assert resolver_called is False


def test_rejects_response_mime_outside_declared_media_kind(tmp_path: Path):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=b"<html>error</html>",
        )

    destination = tmp_path / "wrong-mime.jpg.part"
    with pytest.raises(SafeDownloadError) as error:
        _downloader(handler).download(
            "https://cdn.pixabay.com/photo.jpg",
            destination,
            allowed_host_suffixes={"pixabay.com"},
            allowed_mime_types={"image/jpeg"},
            max_bytes=100,
        )

    assert error.value.code == "content_type_rejected"
    assert not destination.exists()


def test_cross_host_redirect_does_not_forward_authorization(tmp_path: Path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "videos.pexels.com":
            return httpx.Response(
                302,
                headers={"Location": "https://images.pexels.com/final.jpg"},
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "image/jpeg"},
            stream=_ChunkStream([b"jpeg-like-bytes"]),
        )

    destination = tmp_path / "redirect.jpg.part"
    _downloader(handler).download(
        "https://videos.pexels.com/start.jpg",
        destination,
        headers={"Authorization": "secret", "Accept": "image/jpeg"},
        allowed_host_suffixes={"pexels.com"},
        allowed_mime_types={"image/jpeg"},
        max_bytes=100,
    )

    assert requests[0].headers["authorization"] == "secret"
    assert "authorization" not in requests[1].headers
    assert requests[1].headers["accept"] == "image/jpeg"
