"""Streaming media proxy used by the Qzone Page."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx

from .errors import QzoneParseError, QzoneRequestError
from .source_policy import is_remote_media_url_allowed, resolve_remote_media_redirect


PAGE_MEDIA_CHUNK_SIZE = 256 * 1024
PAGE_MEDIA_REDIRECT_LIMIT = 3


def _content_disposition(filename: str, *, download: bool) -> str:
    disposition = "attachment" if download else "inline"
    safe_ascii = "".join(char if 32 <= ord(char) < 127 and char not in {'"', "\\"} else "_" for char in filename)
    safe_ascii = safe_ascii.strip(" .") or "qzone-media"
    return f"{disposition}; filename=\"{safe_ascii}\"; filename*=UTF-8''{quote(filename or safe_ascii, safe='')}"


async def open_page_media_stream(
    media: dict[str, Any],
    *,
    range_header: str = "",
    download: bool = False,
    client: httpx.AsyncClient | None = None,
) -> tuple[AsyncIterator[bytes], int, dict[str, str]]:
    source = str(media.get("source") or "").strip()
    if not is_remote_media_url_allowed(source):
        raise QzoneParseError("媒体地址无效或不安全，请刷新说说后重试。")

    owns_client = client is None
    client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
    )
    request_headers = {
        "Accept": "*/*",
        "Referer": "https://user.qzone.qq.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
    }
    if range_header:
        request_headers["Range"] = range_header

    response: httpx.Response | None = None
    current = source
    try:
        for _ in range(PAGE_MEDIA_REDIRECT_LIMIT + 1):
            request = client.build_request("GET", current, headers=request_headers)
            response = await client.send(request, stream=True)
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = resolve_remote_media_redirect(current, response.headers.get("location", ""))
            await response.aclose()
            response = None
            if not location:
                raise QzoneRequestError("QQ 媒体重定向地址无效。")
            current = location
        if response is None or response.status_code not in {200, 206}:
            status = response.status_code if response is not None else 502
            if response is not None:
                await response.aclose()
            raise QzoneRequestError(f"QQ 媒体读取失败（HTTP {status}），请刷新说说后重试。")
    except Exception:
        if response is not None:
            await response.aclose()
        if owns_client:
            await client.aclose()
        raise

    mime_type = str(media.get("mime_type") or response.headers.get("content-type") or "application/octet-stream")
    filename = str(media.get("name") or "qzone-media")
    headers = {
        "Content-Type": mime_type,
        "Content-Disposition": _content_disposition(filename, download=download),
        "Accept-Ranges": response.headers.get("accept-ranges") or "bytes",
        "Cache-Control": "no-store" if download else "private, max-age=60",
        "X-Content-Type-Options": "nosniff",
    }
    for name in ("content-length", "content-range", "etag", "last-modified"):
        value = response.headers.get(name)
        if value:
            headers[name.title()] = value

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_bytes(PAGE_MEDIA_CHUNK_SIZE):
                if chunk:
                    yield chunk
        finally:
            await response.aclose()
            if owns_client:
                await client.aclose()

    return body(), response.status_code, headers
