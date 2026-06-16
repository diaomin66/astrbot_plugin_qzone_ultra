"""Standalone Qzone daemon."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import html as html_lib
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from aiohttp import web

from . import BRIDGE_API_VERSION, __version__ as BRIDGE_VERSION
from .astrbot_logging import configure_standalone_logging, get_logger
from .client import QZONE_PUBLIC_VIDEO_ALBUM_NAME, QzoneClient
from .errors import QzoneAuthError, QzoneBridgeError, QzoneNeedsRebind, QzoneParseError, QzoneRequestError
from .h5_video import qzone_h5_video_upload_available
from .local_media import resolve_trusted_local_media_path
from .media import (
    QZONE_MAX_IMAGES,
    QZONE_IMAGE_SUFFIXES,
    QZONE_VIDEO_SUFFIXES,
    PostMedia,
    PostPayload,
    collapse_single_video_cover_companion_media,
    is_supported_image,
    is_video_media,
    media_reference_text,
    normalize_media_list,
    normalize_source,
    sanitize_publish_content,
    split_publishable_images,
    source_name,
)
from .native_video import _probe_video_duration_ms, native_video_candidate
from .video import materialize_video_cover_list, materialize_video_source_list, video_cover_media
from .models import FeedEntry, SessionState
from .parser import (
    compute_unikey,
    extract_feed_page,
    feed_page_cursor,
    feed_page_has_more,
    normalize_uin,
    parse_cookie_text,
    unwrap_payload,
)
from .protocol import SECRET_HEADER, fail, ok
from .selection import NUMERIC_FID_MIN_LENGTH
from .social import extract_comments, extract_images
from .storage import StateStore, ensure_state_secret
from .utils import now_iso, from_iso

log = get_logger(__name__)
LIKE_VERIFY_RETRY_DELAYS_SECONDS = (0.35, 0.85, 1.6)
NATIVE_VIDEO_VERIFY_RETRY_DELAYS_SECONDS = (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 34.0)
NATIVE_VIDEO_VERIFY_DETAIL_LIMIT = 5
NATIVE_VIDEO_PRIVATE_DETAIL_EARLY_STOP_ATTEMPTS = 1
NATIVE_VIDEO_MOOD_VISIBILITY_RETRY_DELAYS_SECONDS = (0.0, 1.0, 2.0, 3.0)
TRUE_TEXT_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_TEXT_VALUES = {"0", "false", "no", "n", "off", ""}
PUBLIC_HEALTH_METHODS = {"GET", "HEAD"}
PUBLIC_HEALTH_PATHS = {"/", "/health"}
AUTHENTICATED_REQUEST_KEY = "qzone_authenticated_request"
FEED_CURSOR_PREFIX = "qzpc_"
LATEST_FEED_REFERENCES = {
    "latest",
    "newest",
    "recent",
    "last",
    "\u6700\u65b0",
    "\u6700\u65b0\u4e00\u6761",
    "\u6700\u8fd1\u4e00\u6761",
    "\u6700\u540e\u4e00\u6761",
}
FEED_REFERENCE_PREFIXES = ("\u7b2c",)
FEED_REFERENCE_SUFFIXES = ("\u6761",)
LOSSY_LATEST_FEED_REFERENCES = {"最新", "最近"}


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in TRUE_TEXT_VALUES:
            return True
        if normalized in FALSE_TEXT_VALUES:
            return False
    return bool(value)


def _coerce_int(value: Any, default: int = 0, *, field: str = "value") -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise QzoneParseError(f"{field} 必须是整数") from exc


def _query_int(request: web.Request, key: str, default: int = 0) -> int:
    return _coerce_int(request.query.get(key), default, field=key)


def _query_bool(request: web.Request, key: str, default: bool = False) -> bool:
    return _coerce_bool(request.query.get(key), default)


def _body_int(body: dict[str, Any], key: str, default: int = 0) -> int:
    return _coerce_int(body.get(key), default, field=key)


def _body_bool(body: dict[str, Any], key: str, default: bool = False) -> bool:
    return _coerce_bool(body.get(key), default)


def _encode_feed_cursor(source: str, *, cursor: str = "", page: int = 0, num: int = 0) -> str:
    payload = {
        "source": str(source or ""),
        "cursor": str(cursor or ""),
        "page": max(0, int(page or 0)),
        "num": max(0, int(num or 0)),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return FEED_CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_feed_cursor(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {"source": "", "cursor": "", "page": 0, "num": 0}
    if not text.startswith(FEED_CURSOR_PREFIX):
        return {"source": "", "cursor": text, "page": 0, "num": 0}
    encoded = text[len(FEED_CURSOR_PREFIX):]
    try:
        padded = encoded + ("=" * (-len(encoded) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise QzoneParseError("动态分页游标无效，请刷新页面后重试。") from exc
    if not isinstance(payload, dict):
        raise QzoneParseError("动态分页游标无效，请刷新页面后重试。")
    return {
        "source": str(payload.get("source") or ""),
        "cursor": str(payload.get("cursor") or ""),
        "page": _coerce_int(payload.get("page"), 0, field="cursor.page"),
        "num": _coerce_int(payload.get("num"), 0, field="cursor.num"),
    }


def _feed_entry_ref(entry: FeedEntry) -> tuple[int, str, int]:
    return (int(entry.hostuin or 0), str(entry.fid or ""), int(entry.appid or 0))


def _feed_page_visit_key(source: str, cursor: str, page: int, num: int) -> tuple[str, str, int, int]:
    source = str(source or "")
    if source == "legacy_feeds":
        return (source, "", max(1, int(page or 1)), max(1, int(num or 1)))
    if source == "legacy_recent":
        return (source, str(cursor or ""), max(1, int(page or 1)), max(1, int(num or 1)))
    return (source, str(cursor or ""), 0, 0)


async def _bridge_response(service: "QzoneDaemonService", action) -> web.Response:
    try:
        payload = await action()
    except QzoneBridgeError as exc:
        service._set_error(exc)
        return fail(exc.code, exc.message, detail=_error_detail(exc))
    except Exception as exc:
        log.exception("qzone daemon unhandled request error")
        wrapped = QzoneRequestError(
            "daemon 内部错误，已返回结构化错误；请查看插件数据目录 daemon.log",
            detail=_safe_error_diagnostic(exc),
        )
        service._set_error(wrapped)
        return fail(wrapped.code, wrapped.message, detail=_error_detail(wrapped), status=500)
    return ok(payload)


def _trusted_daemon_video_path(video: PostMedia) -> Path | None:
    if not video.trusted_local:
        return None
    source = normalize_source(video.source)
    path = resolve_trusted_local_media_path(
        source,
        name=video.name or source_name(source),
        suffixes=QZONE_VIDEO_SUFFIXES,
    )
    if path is not None:
        return path
    if not is_video_media(video):
        return None
    candidate = resolve_trusted_local_media_path(
        source,
        name=video.name or source_name(source),
        suffixes=None,
    )
    if candidate is None:
        return None
    try:
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            return None
    except OSError:
        return None
    return candidate


def _trusted_daemon_image_path(image: PostMedia) -> Path | None:
    if not image.trusted_local:
        return None
    source = normalize_source(image.source)
    return resolve_trusted_local_media_path(
        source,
        name=image.name or source_name(source),
        suffixes=QZONE_IMAGE_SUFFIXES,
    )


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _contains_video_media(media: list[PostMedia]) -> bool:
    return any(not is_supported_image(item) and is_video_media(item) for item in media)


def _media_rejection_summary(media: list[PostMedia]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for index, item in enumerate(media[:10]):
        source = normalize_source(item.source)
        source_type = "empty"
        if source.startswith(("http://", "https://")):
            source_type = "url"
        elif source.startswith(("base64://", "data:")):
            source_type = "embedded"
        elif source:
            source_type = "local"
        source_exists = False
        if source_type == "local":
            if item.kind == "video" or is_video_media(item):
                source_exists = _trusted_daemon_video_path(item) is not None
            elif is_supported_image(item):
                source_exists = _trusted_daemon_image_path(item) is not None
        summary.append(
            {
                "index": index,
                "kind": item.kind,
                "raw_type": item.raw_type,
                "name": item.name or source_name(source),
                "mime_type": item.mime_type,
                "size": item.size,
                "trusted_local": item.trusted_local,
                "source_type": source_type,
                "source_name": source_name(source),
                "source_exists": source_exists,
                "is_video": bool(item.kind == "video" or is_video_media(item)),
                "is_image": bool(is_supported_image(item)),
            }
        )
    if len(media) > len(summary):
        summary.append({"omitted_count": len(media) - len(summary)})
    return summary


def _raw_contains_text(value: Any, needle: str, *, depth: int = 0) -> bool:
    if not needle or depth > 8:
        return False
    if isinstance(value, str):
        return needle in value
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value) == needle
    if isinstance(value, dict):
        return any(
            _raw_contains_text(key, needle, depth=depth + 1) or _raw_contains_text(item, needle, depth=depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_raw_contains_text(item, needle, depth=depth + 1) for item in value)
    return False


PUBLIC_VISIBILITY_RIGHT_VALUES = {"1", "public", "all", "everyone", "all_visible", "all-visible"}
PRIVATE_VISIBILITY_RIGHT_VALUES = {"64"}
PUBLIC_VISIBILITY_BOOL_KEYS = {
    "public",
    "ispublic",
    "is_public",
    "allvisible",
    "all_visible",
}
PUBLIC_VISIBILITY_TEXT_KEYS = {
    "visibility",
    "permission",
    "privacy",
    "visible",
    "visible_to",
    "visibleto",
    "right",
    "scope",
}
PUBLIC_VISIBILITY_TEXT_VALUES = {
    "1",
    "public",
    "all",
    "everyone",
    "all_visible",
    "all-visible",
    "\u5168\u90e8\u4eba\u53ef\u89c1",
    "\u6240\u6709\u4eba\u53ef\u89c1",
    "\u516c\u5f00",
}
PRIVATE_VISIBILITY_KEYWORDS = ("仅自己可见", "仅自己", "私密", "only me", "private")
NON_PUBLIC_VISIBILITY_KEYWORDS = (
    "好友可见",
    "部分可见",
    "部分好友",
    "指定好友",
    "不给谁看",
    "回答问题可见",
    "friends only",
    "friend only",
    "specified friends",
    "custom visibility",
)
PRIVATE_VISIBILITY_BOOL_KEYS = {
    "private",
    "isprivate",
    "is_private",
    "onlyself",
    "only_self",
    "selfvisible",
    "self_visible",
    "secret",
}
PRIVATE_VISIBILITY_RIGHT_KEYS = {
    "ugc_right",
    "ugcright",
    "ugcRight",
    "feedright",
    "feed_right",
    "right",
    "viewright",
    "view_right",
}
ACCESS_DENIED_VISIBILITY_KEYWORDS = (
    "\u4e3b\u4eba\u8bbe\u7f6e\u4fdd\u5bc6",
    "\u4e3b\u4eba\u8bbe\u7f6e\u4e86\u4fdd\u5bc6",
    "\u8bbe\u7f6e\u4fdd\u5bc6",
    "\u6ca1\u6709\u8bbf\u95ee\u64cd\u4f5c\u6743\u9650",
    "\u6ca1\u6709\u6743\u9650",
    "\u6ca1\u6709\u8bbf\u95ee",
    "access denied",
    "permission denied",
    "no permission",
    "not authorized",
    "private",
    "forbidden",
)
APPID4_PUBLIC_PRIV_VALUES = {"0"}
APPID4_PUBLIC_ACCESSRIGHT_VALUES = {"3"}
APPID4_READY_STATUS_VALUES = {"", "2"}
APPID4_PUBLIC_VIDEO_URL_MARKERS = ("photovideo.photo.qq.com",)
APPID4_PUBLIC_VIDEO_URL_STATUS_CODES = {200, 206}
APPID4_PUBLIC_VIDEO_PROBE_MAX_URLS = 3
APPID4_VIDEO_URL_KEYS = {
    "downloadurl",
    "download_url",
    "videourl",
    "video_url",
    "playurl",
    "play_url",
    "rawurl",
    "raw_url",
    "url3",
    "vvidiourl",
    "vvidioswfurl",
    "datavvidiourl",
    "datavvidioswfurl",
}
APPID4_VIDEO_ID_KEYS = {
    "vid",
    "svid",
    "videoid",
    "video_id",
    "vidid",
}
APPID4_VIDEO_LIST_KEYS = {
    "data",
    "items",
    "list",
    "video",
    "videos",
    "videolist",
    "video_list",
    "vlist",
    "v_list",
}
HTML_DATA_ACCESSRIGHT_RE = re.compile(
    r"\bdata-accessright\s*=\s*(?:(?P<quote>[\"'])(?P<quoted>[^\"']*)(?P=quote)|(?P<bare>[^\s\"'<>`]+))",
    re.I,
)
PUBLIC_VIDEO_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
def _visibility_right_text(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float, str)):
        text = str(value).strip()
        return text[:-2] if text.endswith(".0") else text
    return ""


def _value_contains_keyword(value: Any, keywords: tuple[str, ...], *, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        lowered = value.lower()
        return any(keyword in value or keyword.lower() in lowered for keyword in keywords)
    if isinstance(value, dict):
        return any(
            _value_contains_keyword(key, keywords, depth=depth + 1)
            or _value_contains_keyword(item, keywords, depth=depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_value_contains_keyword(item, keywords, depth=depth + 1) for item in value)
    if isinstance(value, (int, float, bool)) or value is None:
        return False
    return _value_contains_keyword(str(value), keywords, depth=depth + 1)


def _error_contains_keyword(exc: Exception, keywords: tuple[str, ...]) -> bool:
    return _value_contains_keyword(str(exc), keywords) or _value_contains_keyword(getattr(exc, "detail", None), keywords)


def _native_video_access_denied_visibility(exc: Exception, *, path: str, fid: str = "") -> dict[str, Any] | None:
    if not _error_contains_keyword(exc, ACCESS_DENIED_VISIBILITY_KEYWORDS):
        return None
    marker = {
        "path": path,
        "kind": "private_access_denied",
        "value": _short_diagnostic_text(exc, limit=120),
    }
    if fid:
        marker["fid"] = fid
    return {
        "public": False,
        "private": True,
        "non_public": True,
        "visibility_markers": [marker],
        "private_markers": [marker],
    }


def _should_stop_native_video_verification_after_private_detail(diagnostics: dict[str, Any]) -> bool:
    if not diagnostics.get("publish_tid_present"):
        return False
    if not diagnostics.get("private_visibility_hits"):
        return False
    direct_detail = diagnostics.get("direct_detail")
    if not isinstance(direct_detail, dict):
        return False
    private_count = int(direct_detail.get("private_access_denied_count") or 0)
    attempts = int(direct_detail.get("attempts") or 0)
    return bool(
        private_count > 0
        and attempts >= max(1, int(NATIVE_VIDEO_PRIVATE_DETAIL_EARLY_STOP_ATTEMPTS or 1))
    )


def _private_visibility_diagnostic(value: Any, *, depth: int = 0, path: str = "") -> list[dict[str, Any]]:
    if depth > 8:
        return []
    markers: list[dict[str, Any]] = []
    if isinstance(value, str):
        lowered = value.lower()
        for keyword in PRIVATE_VISIBILITY_KEYWORDS:
            if keyword.lower() in lowered:
                markers.append({"path": path, "kind": "private_text", "value": _short_diagnostic_text(value, limit=80)})
                break
        else:
            for keyword in NON_PUBLIC_VISIBILITY_KEYWORDS:
                if keyword.lower() in lowered:
                    markers.append(
                        {"path": path, "kind": "non_public_text", "value": _short_diagnostic_text(value, limit=80)}
                    )
                    break
        return markers
    if isinstance(value, (int, float, bool)) or value is None:
        return []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            lowered_key = key_text.replace("-", "_").lower()
            item_path = f"{path}.{key_text}" if path else key_text
            if lowered_key in PRIVATE_VISIBILITY_BOOL_KEYS and bool(item):
                markers.append({"path": item_path, "kind": "private_bool", "value": bool(item)})
            if lowered_key in PUBLIC_VISIBILITY_BOOL_KEYS and item is False:
                markers.append({"path": item_path, "kind": "non_public_bool", "value": False})
            if lowered_key in PRIVATE_VISIBILITY_RIGHT_KEYS:
                item_text = _visibility_right_text(item)
                if item_text in PRIVATE_VISIBILITY_RIGHT_VALUES:
                    markers.append({"path": item_path, "kind": "private_right", "value": item_text})
                elif item_text and item_text not in PUBLIC_VISIBILITY_RIGHT_VALUES:
                    markers.append({"path": item_path, "kind": "non_public_right", "value": item_text})
            markers.extend(_private_visibility_diagnostic(item, depth=depth + 1, path=item_path))
            if len(markers) >= 5:
                return markers[:5]
        return markers
    if isinstance(value, (list, tuple, set)):
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]" if path else f"[{index}]"
            markers.extend(_private_visibility_diagnostic(item, depth=depth + 1, path=item_path))
            if len(markers) >= 5:
                return markers[:5]
    return markers


def _public_visibility_diagnostic(value: Any, *, depth: int = 0, path: str = "") -> list[dict[str, Any]]:
    if depth > 8:
        return []
    markers: list[dict[str, Any]] = []
    if isinstance(value, (int, float, bool)) or value is None:
        return []
    if isinstance(value, str):
        return markers
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            lowered_key = key_text.replace("-", "_").lower()
            item_path = f"{path}.{key_text}" if path else key_text
            if lowered_key in PRIVATE_VISIBILITY_RIGHT_KEYS:
                item_text = _visibility_right_text(item)
                if item_text in PUBLIC_VISIBILITY_RIGHT_VALUES:
                    markers.append({"path": item_path, "kind": "public_right", "value": item_text})
            if lowered_key in PUBLIC_VISIBILITY_BOOL_KEYS and item is True:
                markers.append({"path": item_path, "kind": "public_bool", "value": True})
            if lowered_key in PUBLIC_VISIBILITY_TEXT_KEYS and isinstance(item, (str, int, float)):
                item_text = _visibility_right_text(item).strip().lower()
                if item_text in PUBLIC_VISIBILITY_TEXT_VALUES:
                    markers.append({"path": item_path, "kind": "public_text", "value": item_text})
            if isinstance(item, (dict, list, tuple, set)):
                markers.extend(_public_visibility_diagnostic(item, depth=depth + 1, path=item_path))
            if len(markers) >= 5:
                return markers[:5]
        return markers
    if isinstance(value, (list, tuple, set)):
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]" if path else f"[{index}]"
            markers.extend(_public_visibility_diagnostic(item, depth=depth + 1, path=item_path))
            if len(markers) >= 5:
                return markers[:5]
    return markers


def _normalized_key_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _iter_dict_values(value: Any, *, depth: int = 0, path: str = ""):
    if depth > 8:
        return
    if isinstance(value, dict):
        yield path, value
        for key, item in value.items():
            item_path = f"{path}.{key}" if path else str(key)
            yield from _iter_dict_values(item, depth=depth + 1, path=item_path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]" if path else f"[{index}]"
            yield from _iter_dict_values(item, depth=depth + 1, path=item_path)


def _extract_video_text_values(value: Any, *, depth: int = 0) -> list[str]:
    if depth > 8:
        return []
    values: list[str] = []
    if isinstance(value, str):
        return [html_lib.unescape(value)]
    if isinstance(value, (bytes, bytearray)):
        return [html_lib.unescape(value.decode("utf-8", errors="replace"))]
    if isinstance(value, (int, float, bool)) or value is None:
        return [str(value)] if value not in (None, "") else []
    if isinstance(value, dict):
        for key, item in value.items():
            key_norm = _normalized_key_text(key)
            if key_norm in APPID4_VIDEO_URL_KEYS or key_norm in APPID4_VIDEO_ID_KEYS:
                values.extend(_extract_video_text_values(item, depth=depth + 1))
            elif isinstance(item, (dict, list, tuple)):
                values.extend(_extract_video_text_values(item, depth=depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            values.extend(_extract_video_text_values(item, depth=depth + 1))
    return values[:20]


def _contains_public_video_url(value: Any) -> bool:
    if _value_contains_keyword(value, APPID4_PUBLIC_VIDEO_URL_MARKERS):
        return True
    for text in _extract_video_text_values(value):
        lowered = str(text or "").lower()
        if any(marker in lowered for marker in APPID4_PUBLIC_VIDEO_URL_MARKERS):
            return True
    return False


def _extract_public_video_urls(value: Any, *, depth: int = 0) -> list[str]:
    if depth > 8:
        return []
    urls: list[str] = []
    if isinstance(value, str):
        text = html_lib.unescape(value).replace("\\/", "/")
        for match in PUBLIC_VIDEO_URL_RE.finditer(text):
            candidate = str(match.group(0) or "").strip().rstrip(".,)")
            if not candidate:
                continue
            parsed = urlparse(candidate)
            scheme = (parsed.scheme or "").lower()
            host = (parsed.hostname or "").lower()
            if scheme not in {"http", "https"}:
                continue
            if not any(marker in host for marker in APPID4_PUBLIC_VIDEO_URL_MARKERS):
                continue
            if candidate not in urls:
                urls.append(candidate)
            if len(urls) >= APPID4_PUBLIC_VIDEO_PROBE_MAX_URLS:
                return urls[:APPID4_PUBLIC_VIDEO_PROBE_MAX_URLS]
        return urls
    if isinstance(value, (bytes, bytearray)):
        return _extract_public_video_urls(value.decode("utf-8", errors="replace"), depth=depth + 1)
    if isinstance(value, dict):
        for key, item in value.items():
            key_norm = _normalized_key_text(key)
            if key_norm in APPID4_VIDEO_URL_KEYS or isinstance(item, (dict, list, tuple, str, bytes, bytearray)):
                for candidate in _extract_public_video_urls(item, depth=depth + 1):
                    if candidate not in urls:
                        urls.append(candidate)
                    if len(urls) >= APPID4_PUBLIC_VIDEO_PROBE_MAX_URLS:
                        return urls[:APPID4_PUBLIC_VIDEO_PROBE_MAX_URLS]
        return urls
    if isinstance(value, (list, tuple, set)):
        for item in value:
            for candidate in _extract_public_video_urls(item, depth=depth + 1):
                if candidate not in urls:
                    urls.append(candidate)
                if len(urls) >= APPID4_PUBLIC_VIDEO_PROBE_MAX_URLS:
                    return urls[:APPID4_PUBLIC_VIDEO_PROBE_MAX_URLS]
    return urls


def _safe_public_video_url_text(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    host = parsed.netloc or parsed.hostname or ""
    path = parsed.path or ""
    if not host:
        return ""
    return f"{parsed.scheme or 'https'}://{host}{path}"


def _html_accessright_markers(value: Any, *, path: str = "raw") -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    if isinstance(value, str):
        text = html_lib.unescape(value)
        for match in HTML_DATA_ACCESSRIGHT_RE.finditer(text):
            right = (match.group("quoted") or match.group("bare") or "").strip()
            markers.append({"path": path, "kind": "appid4_accessright", "value": right})
            if len(markers) >= 5:
                return markers
        return markers
    if isinstance(value, dict):
        for key, item in value.items():
            item_path = f"{path}.{key}" if path else str(key)
            markers.extend(_html_accessright_markers(item, path=item_path))
            if len(markers) >= 5:
                return markers[:5]
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            markers.extend(_html_accessright_markers(item, path=item_path))
            if len(markers) >= 5:
                return markers[:5]
    return markers


def _appid4_video_public_markers(value: Any, *, raw: Any = None) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    values = [("", value)]
    if raw is not None:
        values.append(("raw", raw))
    for root_path, root in values:
        for path, mapping in _iter_dict_values(root, path=root_path):
            for key, item in mapping.items():
                key_norm = _normalized_key_text(key)
                item_text = _visibility_right_text(item)
                item_path = f"{path}.{key}" if path else str(key)
                if key_norm == "priv" and item_text in APPID4_PUBLIC_PRIV_VALUES:
                    markers.append({"path": item_path, "kind": "appid4_public_priv", "value": item_text})
                if len(markers) >= 5:
                    return markers[:5]
    return markers[:5]


def _appid4_video_non_public_markers(value: Any, *, raw: Any = None) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    values = [("", value)]
    if raw is not None:
        values.append(("raw", raw))
    for root_path, root in values:
        for path, mapping in _iter_dict_values(root, path=root_path):
            for key, item in mapping.items():
                key_norm = _normalized_key_text(key)
                item_text = _visibility_right_text(item)
                item_path = f"{path}.{key}" if path else str(key)
                if key_norm == "priv" and item_text and item_text not in APPID4_PUBLIC_PRIV_VALUES:
                    markers.append({"path": item_path, "kind": "appid4_non_public_priv", "value": item_text})
                if len(markers) >= 5:
                    return markers[:5]
    return markers[:5]


def _merge_appid4_public_probe_visibility(
    visibility: dict[str, Any],
    probe: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(visibility, dict) or not isinstance(probe, dict):
        return visibility
    state = str(probe.get("state") or "")
    if state == "success":
        marker = {
            "path": str(probe.get("path") or "raw"),
            "kind": "appid4_public_video_probe",
            "value": str(probe.get("status_code") or "200"),
        }
        public_markers = [marker, *list(visibility.get("public_markers") or [])][:5]
        return {
            "public": True,
            "private": False,
            "non_public": False,
            "visibility_markers": [],
            "public_markers": public_markers,
            "private_markers": [],
        }
    if state == "denied":
        marker = {
            "path": str(probe.get("path") or "raw"),
            "kind": "appid4_public_video_probe_denied",
            "value": str(probe.get("status_code") or probe.get("reason") or "denied"),
        }
        visibility_markers = [*list(visibility.get("visibility_markers") or []), marker][:5]
        private_markers = [marker] if "private" in str(marker.get("kind") or "") else []
        return {
            "public": False,
            "private": bool(private_markers),
            "non_public": True,
            "visibility_markers": visibility_markers,
            "public_markers": list(visibility.get("public_markers") or [])[:5],
            "private_markers": private_markers[:5],
        }
    return visibility


def _safe_appid4_public_probe_result(probe: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(probe, dict):
        return {}
    safe: dict[str, Any] = {}
    for key in ("state", "status_code", "content_type", "reason", "url_count", "tested_url_count"):
        if key in probe and probe.get(key) not in (None, ""):
            safe[key] = probe.get(key)
    if probe.get("url"):
        safe["url"] = _safe_public_video_url_text(str(probe.get("url") or ""))
    return safe


def _native_video_public_upload_result(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        try:
            value = value.to_dict()
        except Exception:
            value = {}
    if not isinstance(value, dict):
        return {"type": type(value).__name__}
    allowed = {
        "vid",
        "business_type",
        "business_data_length",
        "uploaded_bytes",
        "upload_time",
        "publish_response",
    }
    return {key: _json_safe_detail(item, key=key) for key, item in value.items() if key in allowed}


def _native_video_public_cover_result(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        try:
            value = value.to_dict()
        except Exception:
            value = {}
    if not isinstance(value, dict):
        return {"type": type(value).__name__}
    allowed = {
        "photo_id",
        "album_id",
        "sloc",
        "real_lloc",
        "business_type",
        "business_data_rsp_length",
        "uploaded_bytes",
    }
    return {key: _json_safe_detail(item, key=key) for key, item in value.items() if key in allowed}


def _extract_mood_fid(value: Any, *, depth: int = 0) -> str:
    if depth > 8:
        return ""
    if isinstance(value, dict):
        for key in ("fid", "tid", "feedskey", "feedsKey", "cellid", "cellId", "id"):
            item = value.get(key)
            if item not in (None, ""):
                return str(item).strip()
        for item in value.values():
            found = _extract_mood_fid(item, depth=depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _extract_mood_fid(item, depth=depth + 1)
            if found:
                return found
    return ""


def _native_video_visibility_diagnostic(item: dict[str, Any], *, raw: Any = None) -> dict[str, Any]:
    try:
        appid = int(item.get("appid") or 0)
    except (TypeError, ValueError):
        appid = 0
    if appid == 4:
        public_markers = _appid4_video_public_markers(item, raw=raw)
        non_public_markers = _appid4_video_non_public_markers(item, raw=raw)
        if not public_markers and not non_public_markers:
            non_public_markers = [{"path": "", "kind": "visibility_unproven", "value": "missing appid=4 public marker"}]
        return {
            "public": bool(public_markers) and not non_public_markers,
            "private": False,
            "non_public": bool(non_public_markers),
            "visibility_markers": non_public_markers[:5],
            "public_markers": public_markers[:5],
            "private_markers": [],
        }
    markers = _private_visibility_diagnostic(item)
    if raw is not None:
        markers.extend(_private_visibility_diagnostic(raw, path="raw"))
    markers = markers[:5]
    public_markers = _public_visibility_diagnostic(item)
    if raw is not None:
        public_markers.extend(_public_visibility_diagnostic(raw, path="raw"))
    public_markers = public_markers[:5]
    private_markers = [marker for marker in markers if str(marker.get("kind") or "").startswith("private")]
    if not markers and not public_markers:
        markers = [{"path": "", "kind": "visibility_unproven", "value": "missing public marker"}]
    return {
        "public": bool(public_markers) and not markers,
        "private": bool(private_markers),
        "non_public": bool(markers),
        "visibility_markers": markers,
        "public_markers": public_markers,
        "private_markers": private_markers[:5],
    }


def _record_native_video_visibility_rejection(diagnostics: dict[str, Any], visibility: dict[str, Any]) -> None:
    if visibility.get("private"):
        _append_diagnostic_sample(diagnostics["private_visibility_hits"], visibility)
        diagnostics["result"] = "private_visibility"
        return
    _append_diagnostic_sample(diagnostics["non_public_visibility_hits"], visibility)
    diagnostics["result"] = "non_public_visibility"


def _native_video_visibility_public(visibility: dict[str, Any], diagnostics: dict[str, Any]) -> bool:
    if visibility.get("public"):
        return True
    _record_native_video_visibility_rejection(diagnostics, visibility)
    return False


def _verified_native_video_feed_item(
    item: dict[str, Any],
    *,
    vid: str,
    login_uin: int,
    raw: Any = None,
) -> bool:
    if not isinstance(item, dict):
        return False
    try:
        appid = int(item.get("appid") or 0)
    except (TypeError, ValueError):
        appid = 0
    if appid not in {4, 311}:
        return False
    try:
        hostuin = int(item.get("hostuin") or item.get("uin") or 0)
    except (TypeError, ValueError):
        hostuin = 0
    if not hostuin:
        return False
    if int(login_uin or 0) and hostuin != int(login_uin or 0):
        return False
    item_raw = raw if raw is not None else item.get("raw")
    if not (_raw_contains_text(item, vid) or _raw_contains_text(item_raw, vid)):
        return False
    if appid == 4 and not _contains_public_video_url(item) and not _contains_public_video_url(item_raw):
        return False
    return True


def _native_video_item_context(
    item: dict[str, Any],
    *,
    login_uin: int,
) -> dict[str, Any] | None:
    """Return appid/hostuin context only for self video candidates."""

    if not isinstance(item, dict):
        return None
    try:
        appid = int(item.get("appid") or 0)
    except (TypeError, ValueError):
        appid = 0
    if appid not in {4, 311}:
        return None
    try:
        hostuin = int(item.get("hostuin") or item.get("uin") or 0)
    except (TypeError, ValueError):
        hostuin = 0
    if not hostuin:
        return None
    if int(login_uin or 0) and hostuin != int(login_uin or 0):
        return None
    return {"appid": appid, "hostuin": hostuin}


def _short_diagnostic_text(value: Any, *, limit: int = 180) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _safe_error_diagnostic(exc: Exception) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": _short_diagnostic_text(exc),
    }
    code = getattr(exc, "code", None)
    if code:
        detail["code"] = str(code)
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        detail["status_code"] = status_code
    error_detail = getattr(exc, "detail", None)
    if error_detail not in (None, "", [], {}):
        detail["detail"] = _json_safe_detail(error_detail)
    return detail


def _append_diagnostic_sample(values: list[Any], value: Any, *, limit: int = 5) -> None:
    if len(values) < limit:
        values.append(value)


def _native_video_verification_failure_message(method_label: str, diagnostics: dict[str, Any]) -> str:
    result = str(diagnostics.get("result") or "")
    if result == "private_visibility":
        return (
            f"QQ 空间{method_label}已生成视频动态，但详情或动态校验显示该视频不是全部人可见"
            "（仅自己可见、保密或无访问权限），已拒绝宣称发布成功"
        )
    if result == "non_public_visibility":
        return (
            f"QQ 空间{method_label}已生成视频动态，但权限标记不是全部人可见，"
            "已拒绝宣称发布成功"
        )
    return (
        f"QQ 空间{method_label}已返回 sVid，但未在最近动态中验证到同一公开视频，"
        "已拒绝宣称发布成功"
    )


def _json_safe_detail(value: Any, *, key: str = "", depth: int = 0) -> Any:
    lowered_key = str(key or "").lower()
    normalized_key = "".join(ch for ch in lowered_key if ch.isalnum())
    if depth > 6:
        return "<omitted>"
    if normalized_key in {
        "cookie",
        "cookies",
        "pskey",
        "skey",
        "pt4token",
        "ptkey",
        "qzonetoken",
        "secret",
        "token",
        "clientkey",
        "clientkeyb64",
        "keyindex",
        "session",
        "a2",
        "a2b64",
        "a2base64",
        "a2hex",
        "a2bytes",
        "a2ticket",
        "a2ticketb64",
        "a2ticketbase64",
        "a2tickethex",
        "a2ticketbytes",
        "vlogindata",
        "vlogindatab64",
        "vlogindatabase64",
        "vlogindatahex",
        "vlogindatabytes",
        "logindata",
        "logindatab64",
        "logindatabase64",
        "logindatahex",
        "logindatabytes",
        "loginkey",
        "loginkeyb64",
        "loginkeybase64",
        "loginkeyhex",
        "loginkeybytes",
    }:
        return "***"
    if lowered_key == "feedinfo":
        text = str(value or "")
        return {"present": bool(text), "length": len(text)}
    if lowered_key == "upload_responses" and isinstance(value, list):
        return {"count": len(value)}
    if lowered_key == "control_response" and isinstance(value, dict):
        return {"present": True, "ret": value.get("ret"), "msg": _short_diagnostic_text(value.get("msg"), limit=120)}
    if isinstance(value, dict):
        return {str(item_key): _json_safe_detail(item_value, key=str(item_key), depth=depth + 1) for item_key, item_value in value.items()}
    if isinstance(value, list):
        items = [_json_safe_detail(item, key=key, depth=depth + 1) for item in value[:5]]
        if len(value) > 5:
            items.append({"omitted_count": len(value) - 5})
        return items
    if isinstance(value, tuple):
        return _json_safe_detail(list(value), key=key, depth=depth)
    if isinstance(value, (str, bytes, bytearray)):
        text = value.decode("utf-8", errors="replace") if isinstance(value, (bytes, bytearray)) else value
        return _short_diagnostic_text(text, limit=500)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _short_diagnostic_text(value, limit=200)


def _item_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _native_video_feed_item_diagnostic(
    item: dict[str, Any],
    *,
    vid: str,
    login_uin: int,
) -> dict[str, Any]:
    raw = item.get("raw")
    appid = _item_int(item.get("appid"))
    hostuin = _item_int(item.get("hostuin") or item.get("uin"))
    fid = str(item.get("fid") or item.get("tid") or item.get("key") or "").strip()
    return {
        "fid": fid,
        "appid": appid,
        "hostuin": hostuin,
        "self_hostuin": bool(hostuin and (not login_uin or hostuin == login_uin)),
        "contains_svid": bool(_raw_contains_text(item, vid) or _raw_contains_text(raw, vid)),
        "has_public_video_url": bool(_contains_public_video_url(item) or _contains_public_video_url(raw)),
    }


class QzoneDaemonService:
    def __init__(
        self,
        store: StateStore,
        *,
        secret: str,
        port: int,
        keepalive_interval: int = 120,
        request_timeout: float = 15.0,
        user_agent: str = "",
        version: str = BRIDGE_VERSION,
    ) -> None:
        self.store = store
        self.state = ensure_state_secret(store.read())
        self.state.runtime.secret = secret
        self.state.runtime.daemon_port = int(port)
        self.state.runtime.daemon_pid = os.getpid()
        self.state.runtime.version = version
        self.state.runtime.started_at = now_iso()
        self.state.runtime.last_seen_at = now_iso()
        self.client = QzoneClient(self.state.session, timeout=request_timeout, user_agent=user_agent)
        self.keepalive_interval = max(30, int(keepalive_interval))
        self.health_state = "idle"
        self._keepalive_task: asyncio.Task | None = None
        self._warmup_task: asyncio.Task | None = None
        self._save_task: asyncio.Task | None = None
        self.recent_feed_entries: list[FeedEntry] = []
        self._closing = False

    def save(self) -> None:
        self.store.write(self.state)
        self.state = ensure_state_secret(self.store.read())
        self.client.update_session(self.state.session)

    def touch(self) -> None:
        self.state.runtime.last_seen_at = now_iso()

    def _session_missing_credentials(self) -> bool:
        session = self.state.session
        return not bool(session.cookies and session.uin)

    def _session_needs_rebind(self) -> bool:
        return bool(self.state.session.needs_rebind or self._session_missing_credentials())

    def _public_daemon_state(self) -> str:
        if self._closing or self.health_state == "stopping":
            return "stopping"
        return "online"

    def _ensure_session_ready(self) -> None:
        if self._session_needs_rebind():
            raise QzoneNeedsRebind()

    def _h5_video_upload_configured(self) -> bool:
        state = getattr(self, "state", None)
        return qzone_h5_video_upload_available(getattr(state, "session", None))

    def _video_upload_summary(self) -> dict[str, Any]:
        summary = self.state.video_upload.summary()
        h5_ready = self._h5_video_upload_configured()
        qq_upload_ready = False
        state_qq_upload_ready = bool(summary.get("configured"))
        ready = bool(h5_ready)
        summary["qq_upload_configured"] = qq_upload_ready
        summary["qq_upload_state_configured"] = state_qq_upload_ready
        summary["web_cookie_configured"] = h5_ready
        summary["h5_upload_available"] = h5_ready
        summary["h5_upload_diagnostic_available"] = h5_ready
        summary["h5_publish_supported"] = h5_ready
        summary["h5_publish_experimental"] = False
        summary["h5_publish_permission_update_required"] = h5_ready
        summary["ready"] = ready
        summary["verification_required"] = ready
        summary["h5_publish_verified"] = h5_ready
        summary["h5_publish_verification_required"] = h5_ready
        summary["configured"] = state_qq_upload_ready
        if h5_ready:
            summary["method"] = "h5_video_publish_update_visibility"
            summary["stability"] = "public_create_without_pic_fakefeed_then_permission_repair_and_public_verification"
            if not summary.get("updated_at"):
                summary["updated_at"] = self.state.session.updated_at
        else:
            summary["method"] = ""
            summary["requires"] = "qzone_web_cookie_p_skey"
        return summary

    def _schedule_save(self) -> None:
        if self._closing:
            return
        task = self._save_task
        if task is not None and not task.done():
            return

        async def runner() -> None:
            await asyncio.sleep(0.05)
            try:
                self.save()
            except Exception:
                log.warning("qzone daemon deferred state save failed", exc_info=True)

        self._save_task = asyncio.create_task(runner())

    def _set_success(self, *, defer_save: bool = False) -> None:
        missing_credentials = self._session_missing_credentials()
        self.health_state = "needs_rebind" if missing_credentials else "ready"
        self.state.session.last_ok_at = now_iso()
        self.state.session.last_error = None
        self.state.session.needs_rebind = missing_credentials
        self.touch()
        if defer_save:
            self._schedule_save()
        else:
            self.save()

    def _set_error(self, exc: Exception) -> None:
        if isinstance(exc, (QzoneNeedsRebind, QzoneAuthError)):
            self.health_state = "needs_rebind"
            self.state.session.needs_rebind = True
            self.state.session.qzonetokens.clear()
            self.client.feed_cache.clear()
            self.recent_feed_entries.clear()
        elif isinstance(exc, QzoneRequestError) and exc.status_code is not None and 400 <= exc.status_code < 500:
            if not self._session_needs_rebind():
                self.health_state = "ready"
            else:
                self.health_state = "needs_rebind"
        else:
            self.health_state = "degraded"
        self.state.session.last_error = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        self.touch()
        self.save()

    def _uptime_seconds(self) -> int:
        runtime = self.state.runtime
        started_at = from_iso(runtime.started_at)
        if started_at:
            return int((datetime.now(timezone.utc) - started_at).total_seconds())
        return 0

    def public_snapshot(self) -> dict[str, Any]:
        runtime = self.state.runtime
        return {
            "daemon_state": self._public_daemon_state(),
            "daemon_port": runtime.daemon_port,
            "daemon_version": runtime.version,
        }

    def snapshot(self) -> dict[str, Any]:
        runtime = self.state.runtime
        session = self.state.session
        return {
            "daemon_state": self.health_state,
            "daemon_pid": runtime.daemon_pid,
            "daemon_port": runtime.daemon_port,
            "daemon_version": runtime.version,
            "bridge_api_version": BRIDGE_API_VERSION,
            "started_at": runtime.started_at,
            "last_seen_at": runtime.last_seen_at,
            "uptime_seconds": self._uptime_seconds(),
            "login_uin": session.uin,
            "session_source": session.source,
            "cookie_summary": self.client.cookie_summary(),
            "cookie_count": self.client.cookie_count,
            "needs_rebind": self._session_needs_rebind(),
            "last_ok_at": session.last_ok_at,
            "last_error": session.last_error,
            "video_upload": self._video_upload_summary(),
            "qzonetoken_hosts": sorted(int(k) for k in session.qzonetokens.keys() if str(k).isdigit()),
            "feed_cache_size": len(self.client.feed_cache),
            "session_revision": session.revision,
        }

    async def bootstrap(self) -> None:
        self.save()
        if self.state.session.cookies and self.state.session.uin and not self.state.session.needs_rebind:
            self.health_state = "ready"
            self._warmup_task = asyncio.create_task(self._background_warmup())
        else:
            self.health_state = "needs_rebind"
            self.save()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def close(self) -> None:
        self._closing = True
        self.health_state = "stopping"
        if self._warmup_task:
            self._warmup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._warmup_task
        if self._keepalive_task:
            self._keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keepalive_task
        if self._save_task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._save_task
        self.health_state = "offline"
        self.state.runtime.daemon_pid = 0
        self.state.runtime.started_at = ""
        self.touch()
        self.save()
        self.client.feed_cache.clear()
        await self.client.close()

    async def warmup(self) -> None:
        self._ensure_session_ready()
        await self.client.mfeeds_get_count()
        self._set_success(defer_save=True)

    async def _background_warmup(self) -> None:
        try:
            await self.warmup()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._set_error(exc)

    async def ensure_token(self, hostuin: int | None = None) -> None:
        self._ensure_session_ready()
        hostuin = int(hostuin or self.state.session.uin or 0)
        if not hostuin:
            raise QzoneNeedsRebind()
        if hostuin == self.state.session.uin:
            if not self.state.session.qzonetokens.get(str(hostuin)):
                await self.client.index()
        else:
            if not self.state.session.qzonetokens.get(str(hostuin)):
                await self.client.profile(hostuin)
        self.save()

    async def bind_cookie(self, cookie_text: str, *, uin: int = 0, source: str = "manual") -> dict[str, Any]:
        cookies = parse_cookie_text(cookie_text)
        if not cookies:
            raise QzoneParseError("Cookie 内容为空或无法解析")
        resolved_uin = normalize_uin(cookies, override=uin)
        if not resolved_uin:
            raise QzoneParseError("Cookie 缺少 uin / p_uin，无法识别登录 QQ")
        self.state.session = SessionState(
            uin=resolved_uin,
            cookies=cookies,
            qzonetokens={},
            source=source,
            updated_at=now_iso(),
            last_ok_at="",
            last_error=None,
            revision=self.state.session.revision + 1,
            needs_rebind=False,
        )
        self.client.update_session(self.state.session)
        self.client.feed_cache.clear()
        self.recent_feed_entries.clear()
        self.save()
        try:
            await self.warmup()
        except Exception as exc:
            self._set_error(exc)
            raise
        return self.snapshot()

    async def unbind(self) -> dict[str, Any]:
        self.state.session = SessionState(
            uin=0,
            cookies={},
            qzonetokens={},
            source="manual",
            updated_at=now_iso(),
            last_ok_at="",
            last_error=None,
            revision=self.state.session.revision + 1,
            needs_rebind=True,
        )
        self.client.update_session(self.state.session)
        self.client.feed_cache.clear()
        self.recent_feed_entries.clear()
        self.save()
        self.health_state = "needs_rebind"
        return self.snapshot()

    @staticmethod
    def _should_fallback_feed_fetch(exc: Exception) -> bool:
        if isinstance(exc, QzoneParseError):
            return True
        if not isinstance(exc, QzoneRequestError):
            return False
        if exc.status_code in {301, 302, 303, 307, 308, 403, 429}:
            return True
        return exc.status_code is not None and exc.status_code >= 500

    async def list_feeds(
        self,
        *,
        hostuin: int = 0,
        limit: int = 5,
        cursor: str = "",
        scope: str = "",
        record_recent: bool = True,
    ) -> dict[str, Any]:
        self._ensure_session_ready()
        if limit <= 0:
            limit = 5
        session_uin = int(self.state.session.uin or 0)
        scope = str(scope or "").strip().lower()
        hostuin = int(hostuin or session_uin or 0)
        if not hostuin:
            raise QzoneNeedsRebind()
        if scope == "active":
            if not session_uin:
                raise QzoneNeedsRebind()
            hostuin = session_uin
        scope = scope or ("self" if hostuin == session_uin else "profile")
        cursor_state = _decode_feed_cursor(cursor)
        cursor_source = str(cursor_state.get("source") or "")
        cursor_value = str(cursor_state.get("cursor") or "")
        cursor_page = max(0, _coerce_int(cursor_state.get("page"), 0, field="cursor.page"))
        cursor_num = max(0, _coerce_int(cursor_state.get("num"), 0, field="cursor.num"))
        items: list[FeedEntry] = []
        next_cursor = ""
        has_more = False
        page_round = 0
        seen_item_refs: set[tuple[int, str, int]] = set()
        visited_pages: set[tuple[str, str, int, int]] = set()
        while len(items) < limit and page_round < 6:
            page_source = cursor_source
            legacy_page = max(1, cursor_page or 1)
            legacy_num = max(1, cursor_num or limit)
            if page_source == "legacy_recent":
                legacy_begin_time = max(0, _coerce_int(cursor_value, 0, field="cursor.cursor"))
                payload = await self.client.legacy_recent_feeds(page=legacy_page, begin_time=legacy_begin_time)
            elif page_source == "legacy_feeds":
                payload = await self.client.legacy_feeds(hostuin, page=legacy_page, num=legacy_num)
            elif scope in {"self", "active"}:
                page_source = "modern_active"
                if not cursor_value:
                    try:
                        payload = unwrap_payload(await self.client.index())
                    except (QzoneRequestError, QzoneParseError) as exc:
                        if not self._should_fallback_feed_fetch(exc):
                            raise
                        log.warning("qzone %s feed primary fetch failed, using legacy fallback: %s", scope, exc)
                        try:
                            if scope == "active":
                                page_source = "legacy_recent"
                                legacy_page = 1
                                payload = await self.client.legacy_recent_feeds()
                            else:
                                page_source = "legacy_feeds"
                                legacy_page = 1
                                legacy_num = limit
                                payload = await self.client.legacy_feeds(hostuin, page=legacy_page, num=legacy_num)
                        except (QzoneRequestError, QzoneParseError) as legacy_exc:
                            log.warning("qzone msglist feed fallback failed, using recent feed fallback: %s", legacy_exc)
                            page_source = "legacy_recent"
                            legacy_page = 1
                            payload = await self.client.legacy_recent_feeds()
                else:
                    payload = unwrap_payload(await self.client.get_active_feeds(cursor_value))
            else:
                page_source = "modern_profile"
                if not cursor_value:
                    try:
                        payload = await self.client.profile(hostuin)
                    except (QzoneRequestError, QzoneParseError) as exc:
                        if not self._should_fallback_feed_fetch(exc):
                            raise
                        log.warning("qzone profile feed primary fetch failed, using legacy fallback: %s", exc)
                        page_source = "legacy_feeds"
                        legacy_page = 1
                        legacy_num = limit
                        payload = await self.client.legacy_feeds(hostuin, page=legacy_page, num=legacy_num)
                else:
                    payload = unwrap_payload(await self.client.get_feeds(hostuin, cursor_value))

            page_key = _feed_page_visit_key(page_source, cursor_value, legacy_page, legacy_num)
            if page_key in visited_pages:
                has_more = False
                next_cursor = ""
                break
            visited_pages.add(page_key)

            default_hostuin = 0 if scope == "active" else hostuin
            feedpage, page_items = extract_feed_page(payload, default_hostuin=default_hostuin)
            if not isinstance(feedpage, dict):
                break
            new_page_items: list[FeedEntry] = []
            for item in page_items:
                item_ref = _feed_entry_ref(item)
                if not item_ref[0] or not item_ref[1]:
                    continue
                if item_ref in seen_item_refs:
                    continue
                seen_item_refs.add(item_ref)
                new_page_items.append(item)
            self.client.cache_feed_page(default_hostuin, new_page_items)
            items.extend(new_page_items)
            if page_items and not new_page_items:
                has_more = False
                next_cursor = ""
                break
            raw_cursor = feed_page_cursor(feedpage)
            if page_source in {"legacy_recent", "legacy_feeds"}:
                has_more = bool(new_page_items) and (
                    feed_page_has_more(feedpage)
                    or page_source == "legacy_recent"
                    or len(new_page_items) >= min(max(1, limit), legacy_num)
                )
                legacy_cursor_value = ""
                if page_source == "legacy_recent":
                    times = [int(item.created_at or 0) for item in new_page_items if int(item.created_at or 0) > 0]
                    if times:
                        legacy_cursor_value = str(min(times))
                    else:
                        has_more = False
                next_cursor = (
                    _encode_feed_cursor(
                        page_source,
                        cursor=legacy_cursor_value,
                        page=legacy_page + 1,
                        num=legacy_num,
                    )
                    if has_more
                    else ""
                )
            else:
                has_more = feed_page_has_more(feedpage) and bool(raw_cursor)
                next_cursor = (
                    _encode_feed_cursor(page_source, cursor=raw_cursor)
                    if has_more
                    else ""
                )
            if not has_more or not next_cursor:
                break
            if page_source == "legacy_recent":
                break
            next_cursor_state = _decode_feed_cursor(next_cursor)
            next_cursor_source = str(next_cursor_state.get("source") or "")
            next_cursor_value = str(next_cursor_state.get("cursor") or "")
            next_cursor_page = max(0, _coerce_int(next_cursor_state.get("page"), 0, field="cursor.page"))
            next_cursor_num = max(0, _coerce_int(next_cursor_state.get("num"), 0, field="cursor.num"))
            next_page_key = _feed_page_visit_key(
                next_cursor_source,
                next_cursor_value,
                next_cursor_page,
                next_cursor_num,
            )
            if next_page_key in visited_pages:
                has_more = False
                next_cursor = ""
                break
            cursor_source = next_cursor_source
            cursor_value = next_cursor_value
            cursor_page = next_cursor_page
            cursor_num = next_cursor_num
            page_round += 1

        visible_items = items[:limit]
        if record_recent:
            self.recent_feed_entries = visible_items
        return {
            "scope": scope,
            "hostuin": hostuin,
            "items": [asdict(item) for item in visible_items],
            "has_more": has_more,
            "cursor": next_cursor,
            "count": len(visible_items),
        }

    def _detail_payload_from_entry(self, entry: FeedEntry) -> dict[str, Any]:
        self.client.cache_feed_page(entry.hostuin, [entry])
        raw = entry.raw if isinstance(entry.raw, dict) else {}
        comments = [item.to_dict() for item in extract_comments(raw)]
        return {"entry": asdict(entry), "comments": comments, "raw": raw}

    @staticmethod
    def _video_list_payload_items(value: Any, *, depth: int = 0) -> list[dict[str, Any]]:
        if depth > 8:
            return []
        if isinstance(value, list):
            items: list[dict[str, Any]] = []
            for item in value:
                if isinstance(item, dict):
                    items.append(item)
                elif isinstance(item, (list, tuple)):
                    items.extend(QzoneDaemonService._video_list_payload_items(item, depth=depth + 1))
            return items[:100]
        if isinstance(value, dict):
            items: list[dict[str, Any]] = []
            for key, item in value.items():
                key_norm = _normalized_key_text(key)
                if key_norm in APPID4_VIDEO_LIST_KEYS and isinstance(item, (list, tuple, dict)):
                    items.extend(QzoneDaemonService._video_list_payload_items(item, depth=depth + 1))
                elif isinstance(item, dict):
                    if (
                        any(_normalized_key_text(child_key) in APPID4_VIDEO_ID_KEYS for child_key in item)
                        or _contains_public_video_url(item)
                    ):
                        items.append(item)
                    else:
                        items.extend(QzoneDaemonService._video_list_payload_items(item, depth=depth + 1))
            return items[:100]
        return []

    async def _probe_public_video_url(self, url: str) -> dict[str, Any]:
        target = str(url or "").strip()
        if not target:
            return {"state": "missing_url", "reason": "empty_url"}
        parsed = urlparse(target)
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        if scheme not in {"http", "https"} or not any(marker in host for marker in APPID4_PUBLIC_VIDEO_URL_MARKERS):
            return {"state": "missing_url", "reason": "unsupported_url", "url": _safe_public_video_url_text(target)}
        timeout = max(5.0, min(float(getattr(self, "request_timeout", 15.0) or 15.0), 15.0))
        headers = {
            "User-Agent": str(getattr(self, "user_agent", "") or "Mozilla/5.0"),
            "Accept": "video/*,*/*;q=0.8",
            "Range": "bytes=0-2047",
        }
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as probe_client:
                response = await probe_client.get(target, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as exc:
            return {
                "state": "error",
                "reason": type(exc).__name__,
                "url": _safe_public_video_url_text(target),
            }
        content_type = str(response.headers.get("content-type") or "").strip().lower()
        payload: dict[str, Any] = {
            "url": _safe_public_video_url_text(target),
            "status_code": int(response.status_code or 0),
            "content_type": content_type,
        }
        if response.status_code in APPID4_PUBLIC_VIDEO_URL_STATUS_CODES and (
            not content_type or "video" in content_type or "octet-stream" in content_type
        ):
            payload["state"] = "success"
            return payload
        if response.status_code in {301, 302, 303, 307, 308, 401, 403, 404, 410}:
            payload["state"] = "denied"
            return payload
        if response.status_code >= 400:
            payload["state"] = "denied"
            return payload
        payload["state"] = "error"
        payload["reason"] = "unexpected_response"
        return payload

    async def _probe_appid4_public_video_access(self, item: dict[str, Any], *, raw: Any = None) -> dict[str, Any]:
        urls: list[str] = []
        for value in (item, raw):
            if value in (None, "", [], {}):
                continue
            for candidate in _extract_public_video_urls(value):
                if candidate not in urls:
                    urls.append(candidate)
                if len(urls) >= APPID4_PUBLIC_VIDEO_PROBE_MAX_URLS:
                    break
            if len(urls) >= APPID4_PUBLIC_VIDEO_PROBE_MAX_URLS:
                break
        if not urls:
            return {"state": "missing_url", "url_count": 0, "tested_url_count": 0}
        first_error: dict[str, Any] | None = None
        first_denied: dict[str, Any] | None = None
        tested = 0
        for url in urls[:APPID4_PUBLIC_VIDEO_PROBE_MAX_URLS]:
            tested += 1
            result = await self._probe_public_video_url(url)
            result = dict(result) if isinstance(result, dict) else {"state": "error", "reason": "invalid_result"}
            result.setdefault("url_count", len(urls))
            result.setdefault("tested_url_count", tested)
            if result.get("state") == "success":
                return result
            if result.get("state") == "denied" and first_denied is None:
                first_denied = result
            elif result.get("state") == "error" and first_error is None:
                first_error = result
        if first_denied is not None:
            first_denied.setdefault("url_count", len(urls))
            first_denied.setdefault("tested_url_count", tested)
            return first_denied
        if first_error is not None:
            first_error.setdefault("url_count", len(urls))
            first_error.setdefault("tested_url_count", tested)
            return first_error
        return {"state": "missing_url", "url_count": len(urls), "tested_url_count": tested}

    async def _verified_native_video_visibility(
        self,
        item: dict[str, Any],
        *,
        raw: Any = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        visibility = _native_video_visibility_diagnostic(item, raw=raw)
        try:
            appid = int(item.get("appid") or 0)
        except (TypeError, ValueError):
            appid = 0
        if appid != 4:
            return visibility, None
        probe = await self._probe_appid4_public_video_access(item, raw=raw)
        return _merge_appid4_public_probe_visibility(visibility, probe), probe

    async def _verify_appid4_video_get_data(
        self,
        *,
        vid: str,
        diagnostics: dict[str, Any],
        count: int = 20,
    ) -> dict[str, Any] | None:
        login_uin = int(self.state.session.uin or 0)
        video_diag = diagnostics.setdefault(
            "video_get_data",
            {
                "fetch_attempts": 0,
                "fetch_errors": [],
                "last_item_count": 0,
                "svid_hits": [],
            },
        )
        client = getattr(self, "client", None)
        if client is None or not hasattr(client, "video_get_data"):
            video_diag["skipped"] = "client_missing_video_get_data"
            return None
        video_diag["fetch_attempts"] += 1
        try:
            payload = unwrap_payload(
                await client.video_get_data(
                    login_uin,
                    get_method=2,
                    start=0,
                    count=count,
                    need_old=1,
                    get_user_info=1,
                )
            )
        except (QzoneRequestError, QzoneParseError) as exc:
            _append_diagnostic_sample(video_diag["fetch_errors"], _safe_error_diagnostic(exc))
            log.debug("qzone appid=4 video_get_data verification failed: %s", exc)
            return None
        raw_payload = payload if isinstance(payload, dict) else {"data": payload}
        items = self._video_list_payload_items(raw_payload)
        video_diag["last_item_count"] = len(items)
        for item in items:
            if not isinstance(item, dict):
                continue
            if not _raw_contains_text(item, vid):
                continue
            item_with_context = {
                "appid": 4,
                "hostuin": login_uin,
                **item,
            }
            visibility, probe = await self._verified_native_video_visibility(item_with_context, raw=item)
            hit = {
                "appid": 4,
                "hostuin": login_uin,
                "contains_svid": True,
                "has_public_video_url": _contains_public_video_url(item),
                "visibility": visibility,
            }
            safe_probe = _safe_appid4_public_probe_result(probe)
            if safe_probe:
                hit["public_probe"] = safe_probe
            _append_diagnostic_sample(video_diag["svid_hits"], hit)
            if not _verified_native_video_feed_item(item_with_context, vid=vid, login_uin=login_uin, raw=item):
                continue
            if not _native_video_visibility_public(visibility, diagnostics):
                continue
            verified = dict(item_with_context)
            verified.setdefault("verification_source", "video_get_data")
            verified.setdefault("visibility", visibility)
            verified.setdefault("raw", item)
            if safe_probe:
                verified.setdefault("public_probe", safe_probe)
            diagnostics["result"] = "verified_video_get_data"
            diagnostics["verified_source"] = "video_get_data"
            return verified
        return None

    async def _detail_from_cached_or_legacy_feed(
        self,
        *,
        hostuin: int,
        fid: str,
        require_created_at: bool = False,
        require_images: bool = False,
    ) -> dict[str, Any] | None:
        cached = self.client.feed_cache.get((hostuin, fid))
        if (
            cached is not None
            and (not require_created_at or cached.created_at > 0)
            and (not require_images or bool(extract_images(cached.raw, fid=fid, hostuin=hostuin)))
        ):
            return self._detail_payload_from_entry(cached)

        fetchers: list[Any] = []
        if hostuin == self.state.session.uin:
            fetchers.append(lambda: self.client.legacy_feeds(hostuin, page=1, num=20))
            fetchers.append(self.client.legacy_recent_feeds)
        else:
            fetchers.append(lambda: self.client.legacy_feeds(hostuin, page=1, num=20))

        for fetch in fetchers:
            try:
                payload = unwrap_payload(await fetch())
            except Exception as exc:
                log.debug("qzone detail feed fallback failed: %s", exc)
                continue
            feedpage, entries = extract_feed_page(payload, default_hostuin=hostuin)
            if not feedpage:
                continue
            self.client.cache_feed_page(hostuin, entries)
            for entry in entries:
                if (
                    entry.fid == fid
                    and (not require_created_at or entry.created_at > 0)
                    and (not require_images or bool(extract_images(entry.raw, fid=fid, hostuin=hostuin)))
                ):
                    return self._detail_payload_from_entry(entry)
        return None

    async def detail_feed(self, *, hostuin: int, fid: str, appid: int = 311, busi_param: str = "") -> dict[str, Any]:
        hostuin = int(hostuin or self.state.session.uin or 0)
        if not hostuin:
            raise QzoneNeedsRebind()
        token_error: Exception | None = None
        try:
            await self.ensure_token(hostuin)
        except (QzoneRequestError, QzoneParseError) as exc:
            if not self._should_fallback_feed_fetch(exc):
                raise
            token_error = exc
            log.warning("qzone detail token probe failed, trying detail without fresh token: %s", exc)

        try:
            payload = unwrap_payload(await self.client.detail(hostuin, fid, appid=appid, busi_param=busi_param))
            if not isinstance(payload, dict):
                raise QzoneParseError("说说详情返回格式异常")
            entry = self.client.feed_entry_from_payload(payload, default_hostuin=hostuin)
            entry_images = extract_images(entry.raw, fid=entry.fid, hostuin=entry.hostuin)
            if entry.created_at <= 0 or not entry_images:
                fallback = await self._detail_from_cached_or_legacy_feed(
                    hostuin=hostuin,
                    fid=fid,
                    require_created_at=entry.created_at <= 0,
                    require_images=not entry_images,
                )
                if fallback is not None:
                    entry = self.client.merge_cached_feed_entry(entry)
            return self._detail_payload_from_entry(entry)
        except (QzoneRequestError, QzoneParseError) as exc:
            if token_error is None and not self._should_fallback_feed_fetch(exc):
                raise
            log.warning("qzone detail primary fetch failed, using feed fallback: %s", exc)
            fallback = await self._detail_from_cached_or_legacy_feed(hostuin=hostuin, fid=fid)
            if fallback is not None:
                return fallback
            raise

    async def view_visitors(self, *, page: int = 1, count: int = 20) -> dict[str, Any]:
        self._ensure_session_ready()
        payload = unwrap_payload(await self.client.get_visitors(page=page, count=count))
        if not isinstance(payload, dict):
            raise QzoneParseError("访客列表返回格式异常")
        raw_items = payload.get("items") or payload.get("visitors") or payload.get("data") or payload.get("list") or []
        if isinstance(raw_items, dict):
            raw_items = raw_items.get("items") or raw_items.get("list") or raw_items.get("visitors") or []
        visitors: list[dict[str, Any]] = []
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                user = item.get("user") if isinstance(item.get("user"), dict) else {}
                visitors.append(
                    {
                        "uin": int(item.get("uin") or user.get("uin") or item.get("user_id") or 0),
                        "nickname": item.get("nickname") or user.get("nickname") or item.get("name") or "",
                        "time": item.get("time") or item.get("visitTime") or item.get("timestamp") or 0,
                        "raw": item,
                    }
                )
        self._set_success(defer_save=True)
        return {"items": visitors, "count": len(visitors), "raw": payload}

    async def _wait_for_native_video_feed(
        self,
        *,
        vid: str,
        fid: str = "",
        stop_after_private_detail: bool = True,
    ) -> dict[str, Any] | None:
        vid = str(vid or "").strip()
        if not vid:
            return None
        fid = str(fid or "").strip()
        login_uin = int(self.state.session.uin or 0)
        last_error: Exception | None = None
        checked_detail_keys: set[tuple[int, str, int]] = set()
        diagnostics: dict[str, Any] = {
            "vid_present": True,
            "publish_tid": fid,
            "publish_tid_present": bool(fid),
            "login_uin": login_uin,
            "attempts": 0,
            "private_visibility_hits": [],
            "non_public_visibility_hits": [],
            "direct_detail": {
                "attempts": 0,
                "private_access_denied_count": 0,
                "contains_svid_without_verified_context": False,
                "errors": [],
            },
            "scopes": {},
            "checked_detail_count": 0,
            "detail_errors": [],
            "result": "pending",
            "stop_after_private_detail": bool(stop_after_private_detail),
        }
        self._last_native_video_verification_diagnostics = diagnostics
        for delay in NATIVE_VIDEO_VERIFY_RETRY_DELAYS_SECONDS:
            diagnostics["attempts"] += 1
            if delay > 0:
                await asyncio.sleep(delay)
            appid4_video = await self._verify_appid4_video_get_data(vid=vid, diagnostics=diagnostics)
            if appid4_video is not None:
                return appid4_video
            if fid:
                hostuin = int(self.state.session.uin or 0)
                diagnostics["direct_detail"]["attempts"] += 1
                try:
                    detail = await self.detail_feed(hostuin=hostuin, fid=fid, appid=311)
                except (QzoneRequestError, QzoneParseError) as exc:
                    last_error = exc
                    _append_diagnostic_sample(diagnostics["direct_detail"]["errors"], _safe_error_diagnostic(exc))
                    visibility = _native_video_access_denied_visibility(exc, path="direct_detail", fid=fid)
                    if visibility is not None:
                        _record_native_video_visibility_rejection(diagnostics, visibility)
                        diagnostics["direct_detail"]["private_access_denied_count"] += 1
                    log.debug("qzone native video direct detail verification failed fid=%s: %s", fid, exc)
                else:
                    entry = detail.get("entry") if isinstance(detail, dict) else None
                    raw = detail.get("raw") if isinstance(detail, dict) else detail
                    diagnostics["direct_detail"]["contains_svid_without_verified_context"] = bool(
                        diagnostics["direct_detail"]["contains_svid_without_verified_context"]
                        or _raw_contains_text(detail, vid)
                    )
                    if isinstance(entry, dict):
                        entry_with_context = {"appid": 311, "hostuin": hostuin, **entry}
                    else:
                        entry_with_context = {"fid": fid, "appid": 311, "hostuin": hostuin}
                    if _verified_native_video_feed_item(
                        entry_with_context,
                        vid=vid,
                        login_uin=login_uin,
                        raw=raw,
                    ):
                        visibility = _native_video_visibility_diagnostic(entry_with_context, raw=raw)
                        if not _native_video_visibility_public(visibility, diagnostics):
                            continue
                        verified = dict(entry) if isinstance(entry, dict) else {"fid": fid, "hostuin": hostuin}
                        verified.setdefault("fid", fid)
                        verified.setdefault("hostuin", hostuin)
                        verified.setdefault("appid", 311)
                        verified.setdefault("raw", detail.get("raw") if isinstance(detail, dict) else detail)
                        verified.setdefault("verification_source", "publishmood_rsp_detail")
                        verified.setdefault("visibility", visibility)
                        diagnostics["result"] = "verified_direct_detail"
                        diagnostics["verified_source"] = "publishmood_rsp_detail"
                        return verified
            for scope in ("self", "active", "profile"):
                scope_diag = diagnostics["scopes"].setdefault(
                    scope,
                    {
                        "fetch_attempts": 0,
                        "fetch_errors": [],
                        "last_item_count": 0,
                        "appid_counts": {},
                        "self_hostuin_mismatch_count": 0,
                        "native_video_candidate_count": 0,
                        "native_video_candidate_fids": [],
                        "svid_hits": [],
                    },
                )
                scope_diag["fetch_attempts"] += 1
                try:
                    page = await self.list_feeds(
                        hostuin=int(self.state.session.uin or 0),
                        limit=20,
                        scope=scope,
                        record_recent=False,
                    )
                except (QzoneRequestError, QzoneParseError) as exc:
                    last_error = exc
                    _append_diagnostic_sample(scope_diag["fetch_errors"], _safe_error_diagnostic(exc))
                    log.debug("qzone native video feed verification fetch failed scope=%s: %s", scope, exc)
                    continue
                raw_items = page.get("items") or []
                items = raw_items if isinstance(raw_items, list) else []
                scope_diag["last_item_count"] = len(items)
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_diag = _native_video_feed_item_diagnostic(item, vid=vid, login_uin=login_uin)
                    appid_key = str(item_diag["appid"])
                    scope_diag["appid_counts"][appid_key] = int(scope_diag["appid_counts"].get(appid_key, 0)) + 1
                    if not item_diag["self_hostuin"]:
                        scope_diag["self_hostuin_mismatch_count"] += 1
                    if item_diag["self_hostuin"] and (
                        item_diag["appid"] == 311
                        or (item_diag["appid"] == 4 and item_diag["has_public_video_url"])
                    ):
                        scope_diag["native_video_candidate_count"] += 1
                        if item_diag["fid"]:
                            _append_diagnostic_sample(scope_diag["native_video_candidate_fids"], item_diag["fid"])
                    if item_diag["contains_svid"]:
                        _append_diagnostic_sample(
                            scope_diag["svid_hits"],
                            {
                                "fid": item_diag["fid"],
                                "appid": item_diag["appid"],
                                "hostuin": item_diag["hostuin"],
                                "accepted_context": bool(
                                    item_diag["self_hostuin"]
                                    and (
                                        item_diag["appid"] == 311
                                        or (item_diag["appid"] == 4 and item_diag["has_public_video_url"])
                                    )
                                ),
                                "has_public_video_url": item_diag["has_public_video_url"],
                            },
                        )
                    raw = item.get("raw")
                    if _verified_native_video_feed_item(item, vid=vid, login_uin=login_uin, raw=raw):
                        visibility, probe = await self._verified_native_video_visibility(item, raw=raw)
                        safe_probe = _safe_appid4_public_probe_result(probe)
                        if safe_probe and scope_diag.get("svid_hits"):
                            try:
                                scope_diag["svid_hits"][-1]["public_probe"] = safe_probe
                            except Exception:
                                pass
                        if not _native_video_visibility_public(visibility, diagnostics):
                            continue
                        item.setdefault("verification_source", f"{scope}_feed")
                        item.setdefault("visibility", visibility)
                        if safe_probe:
                            item.setdefault("public_probe", safe_probe)
                        diagnostics["result"] = "verified_feed"
                        diagnostics["verified_source"] = f"{scope}_feed"
                        return item
                for item in items[:NATIVE_VIDEO_VERIFY_DETAIL_LIMIT]:
                    if not isinstance(item, dict):
                        continue
                    item_context = _native_video_item_context(item, login_uin=login_uin)
                    if item_context is None:
                        continue
                    fid = str(item.get("fid") or item.get("tid") or item.get("key") or "").strip()
                    if not fid:
                        continue
                    try:
                        hostuin = int(item_context["hostuin"])
                        appid = int(item_context["appid"])
                    except (TypeError, ValueError):
                        continue
                    detail_key = (hostuin, fid, appid)
                    if detail_key in checked_detail_keys:
                        continue
                    checked_detail_keys.add(detail_key)
                    diagnostics["checked_detail_count"] = len(checked_detail_keys)
                    try:
                        detail = await self.detail_feed(hostuin=hostuin, fid=fid, appid=appid)
                    except (QzoneRequestError, QzoneParseError) as exc:
                        last_error = exc
                        _append_diagnostic_sample(
                            diagnostics["detail_errors"],
                            {"scope": scope, "fid": fid, **_safe_error_diagnostic(exc)},
                        )
                        visibility = _native_video_access_denied_visibility(
                            exc,
                            path=f"{scope}_detail",
                            fid=fid,
                        )
                        if visibility is not None:
                            _record_native_video_visibility_rejection(diagnostics, visibility)
                        log.debug(
                            "qzone native video detail verification fetch failed scope=%s fid=%s: %s",
                            scope,
                            fid,
                            exc,
                        )
                        continue
                    entry = detail.get("entry") if isinstance(detail, dict) else None
                    raw = detail.get("raw") if isinstance(detail, dict) else detail
                    entry_with_context = {**item_context, **(entry if isinstance(entry, dict) else {})}
                    if _verified_native_video_feed_item(
                        entry_with_context,
                        vid=vid,
                        login_uin=login_uin,
                        raw=raw,
                    ):
                        visibility, probe = await self._verified_native_video_visibility(entry_with_context, raw=raw)
                        safe_probe = _safe_appid4_public_probe_result(probe)
                        if not _native_video_visibility_public(visibility, diagnostics):
                            continue
                        verified = dict(entry) if isinstance(entry, dict) else dict(item)
                        verified.setdefault("fid", fid)
                        verified.setdefault("hostuin", hostuin)
                        verified.setdefault("appid", appid)
                        verified.setdefault("raw", detail.get("raw") if isinstance(detail, dict) else detail)
                        verified.setdefault("verification_source", f"{scope}_detail")
                        verified.setdefault("visibility", visibility)
                        if safe_probe:
                            verified.setdefault("public_probe", safe_probe)
                        diagnostics["result"] = "verified_detail"
                        diagnostics["verified_source"] = f"{scope}_detail"
                        return verified
            if stop_after_private_detail and _should_stop_native_video_verification_after_private_detail(diagnostics):
                diagnostics["result"] = "private_visibility"
                diagnostics["early_stop_reason"] = "publish_tid_detail_access_denied"
                return None
        if last_error is not None:
            diagnostics["last_error"] = _safe_error_diagnostic(last_error)
            log.warning("qzone native video feed verification ended with fetch errors: %s", last_error)
        if diagnostics.get("private_visibility_hits"):
            diagnostics["result"] = "private_visibility"
        elif diagnostics.get("non_public_visibility_hits"):
            diagnostics["result"] = "non_public_visibility"
        else:
            diagnostics["result"] = "not_verified"
        return None

    async def _discover_native_video_mood_fid(self, *, vid: str) -> tuple[str, dict[str, Any]]:
        vid = str(vid or "").strip()
        login_uin = int(self.state.session.uin or 0)
        diagnostics: dict[str, Any] = {"vid": vid, "login_uin": login_uin, "scopes": {}}
        if not vid:
            return "", diagnostics
        for scope in ("self", "active", "profile"):
            scope_diag = diagnostics["scopes"].setdefault(
                scope,
                {"fetch_error": None, "last_item_count": 0, "svid_hits": []},
            )
            try:
                page = await self.list_feeds(
                    hostuin=login_uin,
                    limit=20,
                    scope=scope,
                    record_recent=False,
                )
            except (QzoneRequestError, QzoneParseError) as exc:
                scope_diag["fetch_error"] = _safe_error_diagnostic(exc)
                continue
            raw_items = page.get("items") or []
            items = raw_items if isinstance(raw_items, list) else []
            scope_diag["last_item_count"] = len(items)
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_diag = _native_video_feed_item_diagnostic(item, vid=vid, login_uin=login_uin)
                if not item_diag["contains_svid"]:
                    continue
                hit = {
                    "fid": item_diag["fid"],
                    "appid": item_diag["appid"],
                    "hostuin": item_diag["hostuin"],
                    "accepted_context": bool(item_diag["appid"] == 311 and item_diag["self_hostuin"]),
                }
                _append_diagnostic_sample(scope_diag["svid_hits"], hit)
                if hit["accepted_context"] and hit["fid"]:
                    diagnostics["result"] = "found"
                    diagnostics["fid"] = hit["fid"]
                    diagnostics["source"] = scope
                    return str(hit["fid"]), diagnostics
        diagnostics["result"] = "not_found"
        return "", diagnostics

    async def _wait_for_native_video_mood_visibility_public(self, *, fid: str) -> dict[str, Any] | None:
        fid = str(fid or "").strip()
        login_uin = int(self.state.session.uin or 0)
        diagnostics: dict[str, Any] = {
            "fid": fid,
            "login_uin": login_uin,
            "attempts": 0,
            "errors": [],
            "private_visibility_hits": [],
            "non_public_visibility_hits": [],
            "visibility_hits": [],
            "result": "not_verified",
        }
        if not fid or not login_uin:
            diagnostics["result"] = "missing_fid_or_uin"
            self._last_native_video_mood_visibility_diagnostics = diagnostics
            return None
        for delay in NATIVE_VIDEO_MOOD_VISIBILITY_RETRY_DELAYS_SECONDS:
            if delay:
                await asyncio.sleep(delay)
            diagnostics["attempts"] += 1
            try:
                raw_detail = unwrap_payload(await self.client.legacy_detail(login_uin, fid))
            except (QzoneRequestError, QzoneParseError) as exc:
                _append_diagnostic_sample(diagnostics["errors"], _safe_error_diagnostic(exc))
                visibility = _native_video_access_denied_visibility(exc, path="mood_detail", fid=fid)
                if visibility is not None:
                    _append_diagnostic_sample(diagnostics["visibility_hits"], visibility)
                    diagnostics["result"] = "private_visibility"
                continue
            detail = raw_detail if isinstance(raw_detail, dict) else {"data": raw_detail}
            entry = {
                "fid": str(detail.get("tid") or fid),
                "hostuin": login_uin,
                "appid": 311,
                **detail,
            }
            visibility = _native_video_visibility_diagnostic(entry, raw=detail)
            _append_diagnostic_sample(diagnostics["visibility_hits"], visibility)
            if _native_video_visibility_public(visibility, diagnostics):
                ugc_right_public = _visibility_right_text(detail.get("ugc_right")) in PUBLIC_VISIBILITY_RIGHT_VALUES
                right_public = _visibility_right_text(detail.get("right")) in PUBLIC_VISIBILITY_RIGHT_VALUES
                secret_text = _visibility_right_text(detail.get("secret")).lower()
                secret_flag_clear = detail.get("secret") is False or secret_text in FALSE_TEXT_VALUES
                diagnostics["result"] = "verified_mood_detail"
                diagnostics["verified_source"] = "emotion_cgi_msgdetail_v6"
                self._last_native_video_mood_visibility_diagnostics = diagnostics
                return {
                    "fid": fid,
                    "hostuin": login_uin,
                    "appid": 311,
                    "verification_source": "emotion_cgi_msgdetail_v6",
                    "visibility": visibility,
                    "raw": detail,
                    "ugcright_id": str(detail.get("ugcright_id") or ""),
                    "ugc_right": detail.get("ugc_right"),
                    "right": detail.get("right"),
                    "secret": detail.get("secret"),
                    "privacy_checks": {
                        "ugc_right_public": ugc_right_public,
                        "right_public": right_public,
                        "secret_flag_clear": secret_flag_clear,
                    },
                }
        self._last_native_video_mood_visibility_diagnostics = diagnostics
        return None

    async def _publish_h5_video_with_visibility_update(
        self,
        *,
        content: str,
        sync_weibo: bool,
        media: list[PostMedia],
        video: PostMedia,
        path: Path,
        duration_ms: int,
        file_size: int,
        upload_time: int,
        client_key: str,
    ) -> dict[str, Any]:
        try:
            cover = video_cover_media(video, self.store.root / "video_covers")
            cover_path = _trusted_daemon_image_path(cover)
            if cover_path is None:
                raise QzoneParseError(
                    "daemon H5 视频直发无法生成可读取的视频封面，已停止发布",
                    detail={"name": video.name or path.name},
                )
            public_album = await self.client.ensure_public_video_album()
            album_id = str(
                public_album.get("id")
                or public_album.get("topicId")
                or public_album.get("albumId")
                or public_album.get("albumid")
                or public_album.get("aid")
                or ""
            ).strip()
            album_name = str(
                public_album.get("name")
                or public_album.get("albumname")
                or public_album.get("albumName")
                or QZONE_PUBLIC_VIDEO_ALBUM_NAME
            ).strip()
            if not album_id:
                raise QzoneRequestError(
                    "QQ 空间 H5 公开视频相册未返回可绑定的相册 ID",
                    detail={"public_album": _json_safe_detail(public_album)},
                )
            result = await self.client.upload_h5_video(
                path,
                title=video.name or path.name,
                desc=content,
                play_time=duration_ms,
                upload_time=upload_time,
                extend_info={"clientkey": client_key},
            )
            cover_result = await self.client.upload_h5_video_cover(
                cover_path,
                vid=result.vid,
                video_path=path,
                client_key=client_key,
                upload_time=upload_time,
                video_size=file_size,
                duration_ms=duration_ms,
                desc=content,
                album_id=album_id,
                album_name=album_name,
                album_type_id=0,
            )
        except FileNotFoundError as exc:
            raise QzoneParseError("视频文件不存在，无法进行 Qzone H5 原生视频上传", detail={"path": str(path)}) from exc
        except (OSError, QzoneRequestError, QzoneParseError) as exc:
            if isinstance(exc, (QzoneRequestError, QzoneParseError)):
                raise
            raise QzoneRequestError(
                "Qzone H5 原生视频上传失败",
                detail={"type": type(exc).__name__, "message": str(exc)},
            ) from exc

        publish_result: dict[str, Any] = {}
        publish_error: Exception | None = None
        try:
            raw_publish_result = unwrap_payload(
                await self.client.publish_video_mood(
                    content,
                    vid=result.vid,
                    sync_weibo=sync_weibo,
                )
            )
            publish_result = raw_publish_result if isinstance(raw_publish_result, dict) else {"data": raw_publish_result}
        except (QzoneRequestError, QzoneParseError) as exc:
            publish_error = exc
            log.warning(
                "qzone h5 video publish_v6 returned an error after upload; trying feed discovery before visibility update: %s",
                exc,
            )

        publish_fid = _extract_mood_fid(publish_result)
        discovery: dict[str, Any] = {}
        if not publish_fid:
            publish_fid, discovery = await self._discover_native_video_mood_fid(vid=result.vid)
        if not publish_fid:
            detail: dict[str, Any] = {
                "vid": result.vid,
                "client_key": client_key,
                "public_album": _json_safe_detail(public_album),
                "upload_result": _native_video_public_upload_result(result),
                "cover_upload": _native_video_public_cover_result(cover_result),
                "publish_result": _json_safe_detail(publish_result),
                "fid_discovery": _json_safe_detail(discovery),
            }
            if publish_error is not None:
                detail["publish_error"] = _safe_error_diagnostic(publish_error)
            raise QzoneRequestError(
                "QQ 空间 H5 视频已上传/发布但未拿到或发现视频说说 tid/fid，无法调用权限修改接口，已拒绝宣称发布成功",
                detail=detail,
            ) from publish_error

        try:
            raw_update_result = unwrap_payload(
                await self.client.update_mood_visibility_public(
                    publish_fid,
                    content=content,
                    vid=result.vid,
                )
            )
            visibility_update_result = raw_update_result if isinstance(raw_update_result, dict) else {"data": raw_update_result}
        except (QzoneRequestError, QzoneParseError) as exc:
            raise QzoneRequestError(
                "QQ 空间 H5 视频说说已生成，但修改为全部人可见失败，已拒绝宣称发布成功",
                detail={
                    "vid": result.vid,
                    "fid": publish_fid,
                    "client_key": client_key,
                    "public_album": _json_safe_detail(public_album),
                    "publish_result": _json_safe_detail(publish_result),
                    "publish_error": _safe_error_diagnostic(publish_error) if publish_error is not None else None,
                    "permission_update_error": _safe_error_diagnostic(exc),
                },
            ) from exc

        verified_mood_visibility = await self._wait_for_native_video_mood_visibility_public(fid=publish_fid)
        if verified_mood_visibility is None:
            mood_visibility_diagnostics = getattr(self, "_last_native_video_mood_visibility_diagnostics", None) or {
                "fid": publish_fid,
                "result": "not_verified",
                "diagnostics_available": False,
            }
            raise QzoneRequestError(
                "QQ 空间 H5 视频说说权限接口已返回，但 appid=311 说说包装层未验证为全部人可见，已拒绝宣称发布成功",
                detail={
                    "vid": result.vid,
                    "fid": publish_fid,
                    "client_key": client_key,
                    "public_album": _json_safe_detail(public_album),
                    "publish_result": _json_safe_detail(publish_result),
                    "publish_error": _safe_error_diagnostic(publish_error) if publish_error is not None else None,
                    "permission_update_result": _json_safe_detail(visibility_update_result),
                    "mood_visibility": mood_visibility_diagnostics,
                },
            )

        verified_feed = await self._wait_for_native_video_feed(
            vid=result.vid,
            fid=publish_fid,
            stop_after_private_detail=False,
        )
        if verified_feed is None:
            verification_diagnostics = getattr(self, "_last_native_video_verification_diagnostics", None) or {
                "vid_present": bool(result.vid),
                "publish_tid": publish_fid,
                "publish_tid_present": bool(publish_fid),
                "result": "not_verified",
                "diagnostics_available": False,
            }
            raise QzoneRequestError(
                _native_video_verification_failure_message(
                    "H5 视频上传/发布/权限修改",
                    verification_diagnostics,
                ),
                detail={
                    "vid": result.vid,
                    "fid": publish_fid,
                    "client_key": client_key,
                    "public_album": _json_safe_detail(public_album),
                    "publish_result": _json_safe_detail(publish_result),
                    "publish_error": _safe_error_diagnostic(publish_error) if publish_error is not None else None,
                    "permission_update_result": _json_safe_detail(visibility_update_result),
                    "cover_upload": _native_video_public_cover_result(cover_result),
                    "verification": verification_diagnostics,
                },
            )

        return {
            "fid": str(verified_feed.get("fid") or publish_fid or ""),
            "vid": result.vid,
            "message": "已验证 QQ 空间 H5 视频直发成功，并已通过权限接口改为全部人可见",
            "native_video": True,
            "status": "published_native_video",
            "operation_status": "verified_feed_video_public_after_permission_update",
            "media_count": len(media),
            "photo_count": 1,
            "raw": {
                "method": "h5_video_publish_update_visibility",
                "public_album": _json_safe_detail(public_album),
                "upload_result": _native_video_public_upload_result(result),
                "cover_upload_result": _native_video_public_cover_result(cover_result),
                "publish_result": _json_safe_detail(publish_result),
                "publish_error": _safe_error_diagnostic(publish_error) if publish_error is not None else None,
                "permission_update_result": _json_safe_detail(visibility_update_result),
                "fid_discovery": _json_safe_detail(discovery),
                "video": video.to_dict(),
                "cover": cover.to_dict(),
                "client_key": client_key,
                "verified_mood_visibility": _json_safe_detail(verified_mood_visibility),
                "verified_feed": _json_safe_detail(verified_feed),
            },
        }

    async def _publish_native_video_if_configured(
        self,
        *,
        content: str,
        sync_weibo: bool,
        media: list[PostMedia],
    ) -> dict[str, Any] | None:
        video = native_video_candidate(PostPayload(content=content, media=list(media)))
        if video is None:
            return None
        path = _trusted_daemon_video_path(video)
        if path is None:
            raise QzoneParseError(
                "daemon 原生视频后台直发需要可读取的本地视频文件，已阻止视频封面替代发布",
                detail={"name": video.name or source_name(video.source), "source": video.source},
            )
        self._ensure_session_ready()
        duration_ms = _probe_video_duration_ms(path)
        file_size = _file_size(path)
        now = datetime.now(timezone.utc)
        upload_time = int(now.timestamp())
        batch_time_ms = int(now.timestamp() * 1000)
        upload_uin = getattr(getattr(self, "state", None), "session", SessionState()).uin
        client_key = f"{upload_uin}_{batch_time_ms}"

        if self._h5_video_upload_configured():
            return await self._publish_h5_video_with_visibility_update(
                content=content,
                sync_weibo=sync_weibo,
                media=media,
                video=video,
                path=path,
                duration_ms=duration_ms,
                file_size=file_size,
                upload_time=upload_time,
                client_key=client_key,
            )

        raise QzoneParseError(
            "daemon 原生视频直发固定使用 H5 公开创建 + 权限修复 + 公开校验链路；当前缺少 Qzone Web Cookie/p_skey，"
            "无法公开创建视频说说并调用权限接口修复为全部人可见，已阻止视频封面替代发布。"
            "请先使用 /qzone autobind 绑定 Qzone Cookie。",
            detail={
                "name": video.name or path.name,
                "video_upload_configured": False,
                "web_cookie_configured": self._h5_video_upload_configured(),
                "h5_upload_diagnostic_available": self._h5_video_upload_configured(),
                "h5_publish_supported": False,
                "required": "Web Cookie/p_skey",
                "stable_method": "h5_video_publish_update_visibility",
            },
        )

    async def verify_native_video_feed(
        self,
        *,
        vid: str,
        fid: str = "",
        method: str = "h5_video_publish_update_visibility",
    ) -> dict[str, Any]:
        self._ensure_session_ready()
        vid = str(vid or "").strip()
        fid = str(fid or "").strip()
        if not vid:
            raise QzoneParseError("Qzone video feed verification requires sVid")
        verified_feed = await self._wait_for_native_video_feed(vid=vid, fid=fid)
        if verified_feed is None:
            diagnostics = getattr(self, "_last_native_video_verification_diagnostics", None) or {
                "vid_present": bool(vid),
                "publish_tid": fid,
                "publish_tid_present": bool(fid),
                "result": "not_verified",
                "diagnostics_available": False,
            }
            raise QzoneRequestError(
                _native_video_verification_failure_message(method, diagnostics),
                detail={
                    "vid": vid,
                    "fid": fid,
                    "verification": diagnostics,
                },
            )
        self._set_success(defer_save=True)
        return {
            "fid": str(verified_feed.get("fid") or fid or ""),
            "vid": vid,
            "message": "Verified Qzone native video feed is public",
            "native_video": True,
            "status": "published_native_video",
            "operation_status": "verified_feed_video",
            "raw": {
                "method": method,
                "verified_feed": _json_safe_detail(verified_feed),
            },
        }

    async def publish_post(
        self,
        *,
        content: str,
        sync_weibo: bool = False,
        media: list[dict[str, Any]] | None = None,
        content_sanitized: bool = False,
    ) -> dict[str, Any]:
        content = sanitize_publish_content(content, content_sanitized=content_sanitized)
        normalized_media = collapse_single_video_cover_companion_media(normalize_media_list(media))
        normalized_media, _video_sources_changed = materialize_video_source_list(
            normalized_media,
            self.store.root / "video_sources",
        )
        normalized_media = collapse_single_video_cover_companion_media(normalized_media)
        native_payload = await self._publish_native_video_if_configured(
            content=content,
            sync_weibo=sync_weibo,
            media=normalized_media,
        )
        if native_payload is not None:
            self._set_success(defer_save=True)
            return native_payload
        if _contains_video_media(normalized_media):
            raise QzoneParseError(
                "daemon 原生视频后台直发仅支持单个本地视频，已阻止视频封面替代发布；"
                "请只附带一个视频，并确保已绑定 Qzone Web Cookie/p_skey，以使用 H5 公开创建 + 权限修复 + 公开校验链路",
                detail={
                    "media_count": len(normalized_media),
                    "media": _media_rejection_summary(normalized_media),
                    "required": "Web Cookie/p_skey",
                    "stable_method": "h5_video_publish_update_visibility",
                },
            )
        normalized_media, _video_covers_changed = materialize_video_cover_list(
            normalized_media,
            self.store.root / "video_covers",
        )
        photos, fallback_media = split_publishable_images(normalized_media)
        if len(photos) > QZONE_MAX_IMAGES:
            raise QzoneParseError(f"QQ 空间一次最多只能上传 {QZONE_MAX_IMAGES} 张图片")
        if fallback_media:
            refs = "\n".join(media_reference_text(item) for item in fallback_media)
            content = "\n".join(part for part in (content.strip(), refs) if part)
        if not content.strip() and not photos:
            raise QzoneParseError("说说内容或图片/视频不能为空")
        self._ensure_session_ready()
        payload = unwrap_payload(
            await self.client.publish_mood(
                content,
                sync_weibo=sync_weibo,
                photos=[item.to_dict() for item in photos],
            )
        )
        if not isinstance(payload, dict):
            raise QzoneParseError("说说发布返回格式异常")
        self._set_success(defer_save=True)
        return {
            "fid": payload.get("fid") or payload.get("tid") or "",
            "message": payload.get("msg") or payload.get("message") or "",
            "media_count": len(normalized_media),
            "photo_count": len(photos),
            "raw": payload,
        }

    async def comment_post(
        self,
        *,
        hostuin: int,
        fid: str,
        content: str,
        appid: int = 311,
        private: bool = False,
        busi_param: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not content.strip():
            raise QzoneParseError("评论内容不能为空")
        self._ensure_session_ready()
        payload = unwrap_payload(
            await self.client.add_comment(
                hostuin,
                fid,
                content,
                appid=appid,
                private=private,
                busi_param=busi_param or {},
            )
        )
        if not isinstance(payload, dict):
            raise QzoneParseError("评论发布返回格式异常")
        self._set_success(defer_save=True)
        return {
            "commentid": payload.get("commentid") or payload.get("commentId") or 0,
            "commentLikekey": payload.get("commentLikekey") or "",
            "message": payload.get("msg") or payload.get("message") or "",
            "raw": payload,
        }

    async def reply_comment(
        self,
        *,
        hostuin: int,
        fid: str,
        commentid: str,
        comment_uin: int,
        content: str,
        appid: int = 311,
    ) -> dict[str, Any]:
        if not content.strip():
            raise QzoneParseError("回复内容不能为空")
        self._ensure_session_ready()
        payload = unwrap_payload(
            await self.client.reply_comment(
                hostuin,
                fid,
                commentid,
                comment_uin,
                content,
                appid=appid,
            )
        )
        if not isinstance(payload, dict):
            raise QzoneParseError("回复评论返回格式异常")
        self._set_success(defer_save=True)
        return {
            "commentid": payload.get("commentid") or payload.get("commentId") or 0,
            "message": payload.get("msg") or payload.get("message") or "",
            "raw": payload,
        }

    async def delete_post(self, *, fid: str, appid: int = 311, created_at: int = 0) -> dict[str, Any]:
        if not str(fid or "").strip():
            raise QzoneParseError("说说 fid 不能为空")
        self._ensure_session_ready()
        payload = unwrap_payload(await self.client.delete_post(str(fid), appid=appid, created_at=created_at))
        if not isinstance(payload, dict):
            raise QzoneParseError("删除说说返回格式异常")
        self._set_success(defer_save=True)
        return {
            "fid": fid,
            "message": payload.get("msg") or payload.get("message") or "",
            "raw": payload,
        }

    @staticmethod
    def _feed_reference_index(fid: str, *, hostuin: int, latest: bool = False, index: int = 0) -> int:
        if latest:
            return 1
        if index > 0:
            return int(index)
        fid_text = str(fid or "").strip()
        if not fid_text:
            return 0
        if not hostuin and fid_text.isdigit() and len(fid_text) < NUMERIC_FID_MIN_LENGTH:
            return int(fid_text)
        if fid_text.lower() in LATEST_FEED_REFERENCES or fid_text in LATEST_FEED_REFERENCES:
            return 1
        if fid_text in LOSSY_LATEST_FEED_REFERENCES:
            return 1
        return QzoneDaemonService._localized_feed_reference_index(fid_text)

    @staticmethod
    def _localized_feed_reference_index(fid_text: str) -> int:
        text = str(fid_text or "").strip()
        if not text:
            return 0

        matched_marker = False
        for prefix in FEED_REFERENCE_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                matched_marker = True
                break
        for suffix in FEED_REFERENCE_SUFFIXES:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                matched_marker = True
                break

        if matched_marker and text.isdigit():
            return int(text)
        lossy_text = str(fid_text or "").strip()
        if lossy_text.startswith("?") and lossy_text.endswith("?"):
            lossy_inner = lossy_text.strip("?").strip()
            if lossy_inner.isdigit():
                return int(lossy_inner)
        return 0

    def _recent_feed_reference(self, reference_index: int, *, hostuin: int) -> FeedEntry | None:
        if reference_index <= 0 or reference_index > len(self.recent_feed_entries):
            return None
        entry = self.recent_feed_entries[reference_index - 1]
        if hostuin and entry.hostuin != hostuin:
            return None
        return entry

    async def _resolve_recent_feed_reference(
        self,
        hostuin: int,
        fid: str,
        appid: int,
        curkey: str = "",
        *,
        latest: bool = False,
        index: int = 0,
    ) -> tuple[int, str, int, str]:
        fid_text = str(fid or "").strip()
        target_hostuin = int(hostuin or self.state.session.uin or 0)
        reference_index = self._feed_reference_index(
            fid_text,
            hostuin=int(hostuin or 0),
            latest=latest,
            index=index,
        )
        if reference_index > 0:
            cached_entry = self._recent_feed_reference(reference_index, hostuin=target_hostuin if hostuin else 0)
            if cached_entry is not None:
                return (
                    cached_entry.hostuin,
                    cached_entry.fid,
                    cached_entry.appid or appid,
                    curkey or cached_entry.curkey,
                )
            if not target_hostuin:
                raise QzoneNeedsRebind()
            feed_payload = await self.list_feeds(hostuin=target_hostuin, limit=reference_index, scope="profile")
            items = feed_payload.get("items") or []
            if reference_index > len(items):
                raise QzoneParseError(f"第 {reference_index} 条说说不存在")
            entry = FeedEntry(**items[reference_index - 1])
            return entry.hostuin, entry.fid, entry.appid or appid, curkey or entry.curkey
        return target_hostuin, fid_text, int(appid or 311), curkey

    @staticmethod
    def _http_like_key(appid: int, hostuin: int, fid: str) -> str:
        return compute_unikey(appid, hostuin, fid).replace("https://", "http://", 1)

    async def _refresh_like_entry(self, hostuin: int, fid: str, appid: int) -> FeedEntry | None:
        try:
            payload = unwrap_payload(await self.client.detail(hostuin, fid, appid=appid))
        except Exception as exc:
            log.debug("qzone like verification refresh failed: %s", exc)
        else:
            if isinstance(payload, dict):
                entry = self.client.feed_entry_from_payload(payload, default_hostuin=hostuin)
                self.client.cache_feed_page(hostuin, [entry])
                return entry

        for fetch in (
            self.client.legacy_recent_feeds if hostuin == self.state.session.uin else None,
            lambda: self.client.legacy_feeds(hostuin, page=1, num=20),
        ):
            if fetch is None:
                continue
            try:
                payload = unwrap_payload(await fetch())
            except Exception as exc:
                log.debug("qzone like verification feed fallback failed: %s", exc)
                continue
            feedpage, entries = extract_feed_page(payload, default_hostuin=hostuin)
            if not feedpage:
                continue
            self.client.cache_feed_page(hostuin, entries)
            for entry in entries:
                if entry.fid == fid:
                    return entry
        return None

    @staticmethod
    def _normalize_action_payload(payload: Any) -> dict[str, Any]:
        payload = unwrap_payload(payload)
        if isinstance(payload, dict):
            return payload
        return {"value": payload}

    async def _retry_like_entry_until_fresh(
        self,
        hostuin: int,
        fid: str,
        appid: int,
        target_liked: bool,
        current_entry: FeedEntry | None,
    ) -> FeedEntry | None:
        entry = current_entry
        for delay in LIKE_VERIFY_RETRY_DELAYS_SECONDS:
            await asyncio.sleep(delay)
            refreshed = await self._refresh_like_entry(hostuin, fid, appid)
            if refreshed is not None:
                entry = refreshed
            if entry is None or entry.liked == target_liked:
                return entry
        return entry

    async def like_post(
        self,
        *,
        hostuin: int,
        fid: str,
        appid: int = 311,
        curkey: str = "",
        unlike: bool = False,
        latest: bool = False,
        index: int = 0,
        fast: bool = False,
    ) -> dict[str, Any]:
        self._ensure_session_ready()
        hostuin, fid, appid, curkey = await self._resolve_recent_feed_reference(
            hostuin,
            fid,
            appid,
            curkey,
            latest=latest,
            index=index,
        )
        if not hostuin or not fid:
            raise QzoneParseError("没有指定要点赞的说说")

        target_liked = not unlike
        if fast:
            payload = self._normalize_action_payload(
                await self.client.like_post(hostuin, fid, appid=appid, curkey=curkey, like=target_liked)
            )

            touched_entries: set[int] = set()

            def apply_fast_like(entry: FeedEntry | None) -> None:
                if entry is None:
                    return
                entry_id = id(entry)
                if entry_id in touched_entries:
                    return
                touched_entries.add(entry_id)
                was_liked = bool(entry.liked)
                if was_liked != target_liked:
                    entry.like_count = max(
                        0,
                        int(entry.like_count or 0) + (1 if target_liked else -1),
                    )
                entry.liked = target_liked

            cached_entry = self.client.feed_cache.get((hostuin, fid))
            apply_fast_like(cached_entry)
            for entry in self.recent_feed_entries:
                if entry.hostuin == hostuin and entry.fid == fid:
                    apply_fast_like(entry)
                    break
            self._set_success(defer_save=True)
            return {
                "action": "unlike" if unlike else "like",
                "liked": target_liked,
                "verified": False,
                "already": False,
                "operation_status": "accepted_pending_verification",
                "message": payload.get("msg") or payload.get("message") or "",
                "raw": payload,
            }

        before_entry = await self._refresh_like_entry(hostuin, fid, appid)
        if before_entry is not None and before_entry.liked == target_liked:
            self._set_success(defer_save=True)
            return {
                "action": "unlike" if unlike else "like",
                "liked": before_entry.liked,
                "verified": True,
                "already": True,
                "summary": before_entry.summary,
                "raw": {},
            }

        payload = self._normalize_action_payload(
            await self.client.like_post(hostuin, fid, appid=appid, curkey=curkey, like=not unlike)
        )
        verified_entry = await self._refresh_like_entry(hostuin, fid, appid)
        if verified_entry is not None and verified_entry.liked != target_liked:
            fallback_key = self._http_like_key(appid, hostuin, fid)
            if fallback_key not in {curkey, compute_unikey(appid, hostuin, fid)}:
                payload = self._normalize_action_payload(
                    await self.client.like_post(
                        hostuin,
                        fid,
                        appid=appid,
                        curkey=fallback_key,
                        unikey=fallback_key,
                        like=not unlike,
                    )
                )
                verified_entry = await self._refresh_like_entry(hostuin, fid, appid)

        if verified_entry is not None and verified_entry.liked != target_liked:
            verified_entry = await self._retry_like_entry_until_fresh(
                hostuin,
                fid,
                appid,
                target_liked,
                verified_entry,
            )

        verification: dict[str, Any] | None = None
        if verified_entry is not None and verified_entry.liked != target_liked:
            verification = {
                "expected_liked": target_liked,
                "actual_liked": verified_entry.liked,
            }
            log.debug(
                "qzone like request accepted but verification stayed stale: hostuin=%s fid=%s expected=%s actual=%s",
                hostuin,
                fid,
                target_liked,
                verified_entry.liked,
            )
        self._set_success(defer_save=True)
        result = {
            "action": "unlike" if unlike else "like",
            "liked": verified_entry.liked
            if verified_entry is not None and verified_entry.liked == target_liked
            else target_liked,
            "verified": verified_entry is not None and verified_entry.liked == target_liked,
            "already": False,
            "summary": verified_entry.summary if verified_entry is not None else "",
            "message": payload.get("msg") or payload.get("message") or "",
            "raw": payload,
        }
        if verification is not None:
            result["verification"] = verification
        return result

    async def health(self) -> dict[str, Any]:
        if self._session_needs_rebind():
            if self.health_state != "needs_rebind":
                self.health_state = "needs_rebind"
                self.save()
            return self.snapshot()
        try:
            await self.client.mfeeds_get_count()
        except Exception as exc:
            self._set_error(exc)
            raise
        self._set_success()
        return self.snapshot()

    async def _keepalive_loop(self) -> None:
        while not self._closing:
            await asyncio.sleep(self.keepalive_interval)
            if self._closing:
                break
            if self._session_needs_rebind():
                if self.health_state != "needs_rebind":
                    self.health_state = "needs_rebind"
                    self.save()
                continue
            try:
                await self.health()
            except Exception as exc:
                log.debug("qzone keepalive failed: %s", exc)


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _error_detail(exc: QzoneBridgeError):
    detail = _json_safe_detail(exc.detail)
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        return detail
    if isinstance(detail, dict):
        merged = dict(detail)
        merged.setdefault("status_code", status_code)
        return merged
    if detail is None:
        return {"status_code": status_code}
    return {"status_code": status_code, "detail": detail}


SERVICE_APP_KEY = web.AppKey("qzone_service", QzoneDaemonService)
SHUTDOWN_EVENT_APP_KEY = web.AppKey("qzone_shutdown_event", asyncio.Event)


def create_app(service: QzoneDaemonService, shutdown_event: asyncio.Event | None = None) -> web.Application:
    app = web.Application(client_max_size=128 * 1024 * 1024)
    app[SERVICE_APP_KEY] = service
    if shutdown_event is not None:
        app[SHUTDOWN_EVENT_APP_KEY] = shutdown_event

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        supplied_secret = request.headers.get(SECRET_HEADER)
        authenticated = bool(supplied_secret and supplied_secret == service.state.runtime.secret)
        request[AUTHENTICATED_REQUEST_KEY] = authenticated
        if (
            supplied_secret is None
            and request.method in PUBLIC_HEALTH_METHODS
            and request.path in PUBLIC_HEALTH_PATHS
        ):
            return await handler(request)
        if not authenticated:
            return fail("UNAUTHORIZED", "secret 不正确", status=401)
        return await handler(request)

    app.middlewares.append(auth_middleware)

    async def health(request: web.Request) -> web.Response:
        if request.get(AUTHENTICATED_REQUEST_KEY, False):
            service.touch()
            return ok(service.snapshot())
        return ok(
            service.public_snapshot(),
            meta={
                "authenticated": False,
                "public": True,
                "full_status_requires": SECRET_HEADER,
            },
        )

    async def status(request: web.Request) -> web.Response:
        service.touch()
        service.save()
        return ok(service.snapshot())

    async def bind(request: web.Request) -> web.Response:
        body = await _json_body(request)
        cookie_text = str(body.get("cookie_text") or body.get("cookie") or "")
        source = str(body.get("source") or "manual")
        return await _bridge_response(
            service,
            lambda: service.bind_cookie(cookie_text, uin=_body_int(body, "uin", 0), source=source),
        )

    async def unbind(request: web.Request) -> web.Response:
        return await _bridge_response(service, service.unbind)

    async def feeds(request: web.Request) -> web.Response:
        cursor = request.query.get("cursor") or ""
        scope = request.query.get("scope") or ""
        return await _bridge_response(
            service,
            lambda: service.list_feeds(
                hostuin=_query_int(request, "hostuin", 0),
                limit=_query_int(request, "limit", 5),
                cursor=cursor,
                scope=scope,
                record_recent=_query_bool(request, "record_recent", True),
            ),
        )

    async def detail(request: web.Request) -> web.Response:
        fid = request.query.get("fid") or ""
        busi_param = request.query.get("busi_param") or ""
        return await _bridge_response(
            service,
            lambda: service.detail_feed(
                hostuin=_query_int(request, "hostuin", 0),
                fid=fid,
                appid=_query_int(request, "appid", 311),
                busi_param=busi_param,
            ),
        )

    async def visitors(request: web.Request) -> web.Response:
        return await _bridge_response(
            service,
            lambda: service.view_visitors(
                page=_query_int(request, "page", 1),
                count=_query_int(request, "count", 20),
            ),
        )

    async def post(request: web.Request) -> web.Response:
        async def action() -> dict[str, Any]:
            body = await _json_body(request)
            if not body and int(request.content_length or 0) > 0:
                raise QzoneParseError(
                    "发布请求体为空或无法解析，请重新选择图片后再发布。",
                    detail={
                        "content_length": int(request.content_length or 0),
                        "content_type": request.headers.get("content-type", ""),
                    },
                )
            content = str(body.get("content") or "")
            media = body.get("media") or body.get("attachments") or body.get("photos") or []
            return await service.publish_post(
                content=content,
                sync_weibo=_body_bool(body, "sync_weibo", False),
                media=media,
                content_sanitized=_body_bool(body, "content_sanitized", False),
            )

        return await _bridge_response(
            service,
            action,
        )

    async def comment(request: web.Request) -> web.Response:
        body = await _json_body(request)
        busi_param = body.get("busi_param")
        if not isinstance(busi_param, dict):
            busi_param = {}
        return await _bridge_response(
            service,
            lambda: service.comment_post(
                hostuin=_body_int(body, "hostuin", 0),
                fid=str(body.get("fid") or ""),
                content=str(body.get("content") or ""),
                appid=_body_int(body, "appid", 311),
                private=_body_bool(body, "private", False),
                busi_param=busi_param,
            ),
        )

    async def reply(request: web.Request) -> web.Response:
        body = await _json_body(request)
        return await _bridge_response(
            service,
            lambda: service.reply_comment(
                hostuin=_body_int(body, "hostuin", 0),
                fid=str(body.get("fid") or ""),
                commentid=str(body.get("commentid") or body.get("commentId") or ""),
                comment_uin=_coerce_int(
                    body.get("comment_uin") or body.get("commentUin"),
                    0,
                    field="comment_uin",
                ),
                content=str(body.get("content") or ""),
                appid=_body_int(body, "appid", 311),
            ),
        )

    async def delete(request: web.Request) -> web.Response:
        body = await _json_body(request)
        return await _bridge_response(
            service,
            lambda: service.delete_post(
                fid=str(body.get("fid") or ""),
                appid=_body_int(body, "appid", 311),
                created_at=_body_int(body, "created_at", 0),
            ),
        )

    async def like(request: web.Request) -> web.Response:
        body = await _json_body(request)
        return await _bridge_response(
            service,
            lambda: service.like_post(
                hostuin=_body_int(body, "hostuin", 0),
                fid=str(body.get("fid") or ""),
                appid=_body_int(body, "appid", 311),
                curkey=str(body.get("curkey") or ""),
                unlike=_body_bool(body, "unlike", False),
                latest=_body_bool(body, "latest", False),
                index=_body_int(body, "index", 0),
                fast=_body_bool(body, "fast", False),
            ),
        )

    async def verify_native_video(request: web.Request) -> web.Response:
        body = await _json_body(request)
        return await _bridge_response(
            service,
            lambda: service.verify_native_video_feed(
                vid=str(body.get("vid") or body.get("sVid") or body.get("svid") or ""),
                fid=str(body.get("fid") or body.get("tid") or ""),
                method=str(body.get("method") or "h5_video_publish_update_visibility"),
            ),
        )

    async def shutdown(request: web.Request) -> web.Response:
        service.health_state = "stopping"
        service.touch()
        service.save()
        event = request.app.get(SHUTDOWN_EVENT_APP_KEY)
        if isinstance(event, asyncio.Event):
            asyncio.get_running_loop().call_later(0.1, event.set)
        return ok({"stopping": True})

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    app.router.add_post("/bind", bind)
    app.router.add_post("/unbind", unbind)
    app.router.add_get("/feeds", feeds)
    app.router.add_get("/detail", detail)
    app.router.add_get("/visitors", visitors)
    app.router.add_post("/post", post)
    app.router.add_post("/comment", comment)
    app.router.add_post("/reply", reply)
    app.router.add_post("/delete", delete)
    app.router.add_post("/like", like)
    app.router.add_post("/native-video/verify", verify_native_video)
    app.router.add_post("/shutdown", shutdown)
    return app


async def run_daemon(
    *,
    data_dir: Path,
    port: int,
    secret: str,
    keepalive_interval: int,
    request_timeout: float,
    user_agent: str,
    version: str,
) -> None:
    store = StateStore(data_dir)
    service = QzoneDaemonService(
        store,
        secret=secret,
        port=port,
        keepalive_interval=keepalive_interval,
        request_timeout=request_timeout,
        user_agent=user_agent,
        version=version,
    )
    await service.bootstrap()

    shutdown_event = asyncio.Event()
    app = create_app(service, shutdown_event=shutdown_event)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=port)
    await site.start()
    log.info("Qzone daemon started on 127.0.0.1:%s", port)
    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await service.close()
        await runner.cleanup()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Qzone daemon")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--secret", default=os.getenv("QZONE_BRIDGE_SECRET", ""))
    parser.add_argument("--keepalive-interval", type=int, default=120)
    parser.add_argument("--request-timeout", type=float, default=15.0)
    parser.add_argument("--user-agent", default="")
    parser.add_argument("--version", default=BRIDGE_VERSION)
    args = parser.parse_args()
    if not args.secret:
        parser.error("--secret or QZONE_BRIDGE_SECRET is required")

    configure_standalone_logging()
    asyncio.run(
        run_daemon(
            data_dir=Path(args.data_dir),
            port=args.port,
            secret=args.secret,
            keepalive_interval=args.keepalive_interval,
            request_timeout=args.request_timeout,
            user_agent=args.user_agent,
            version=args.version,
        )
    )


if __name__ == "__main__":
    main()
