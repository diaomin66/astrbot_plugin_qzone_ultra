"""Media helpers for building QQ Space posts from AstrBot messages."""

from __future__ import annotations

import contextlib
import mimetypes
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from .local_media import resolve_trusted_local_media_path
from .source_policy import is_windows_drive_path


QZONE_MAX_IMAGES = 9
QZONE_MIN_IMAGE_SIDE = 16
QZONE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
QZONE_VIDEO_SUFFIXES = {
    ".mp4",
    ".m4v",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".flv",
    ".wmv",
    ".mpeg",
    ".mpg",
    ".3gp",
    ".3g2",
    ".ts",
    ".mts",
    ".m2ts",
}
TEXT_KINDS = {"plain", "text"}
MEDIA_KINDS = {"image", "file", "video", "record", "audio", "voice"}
REFERENCE_KINDS = {"reply", "quote", "quoted", "reference"}
REFERENCE_OWNER_KEYS = (
    "quote",
    "quoted",
    "quoted_message",
    "reply",
    "reply_message",
    "reply_msg",
    "referenced",
    "referenced_message",
    "reference",
    "origin",
    "original",
    "original_message",
    "source_message",
)
MESSAGE_CHAIN_KEYS = (
    "message",
    "messages",
    "chain",
    "message_chain",
    "raw_message",
    "raw_messages",
    "message_list",
    "message_segments",
)
REFERENCE_MEDIA_KEYS = ("image", "images", "media", "medias", "attachment", "attachments", "files")
REFERENCE_MESSAGE_ID_KEYS = (
    "id",
    "message_id",
    "messageId",
    "message_seq",
    "messageSeq",
    "seq",
    "reply_id",
    "replyId",
    "msg_id",
    "msgId",
    "source_msg_id",
    "sourceMsgId",
    "origin_message_id",
    "originMessageId",
)
REFERENCE_MAX_DEPTH = 6
COMPONENT_STRING_RE = re.compile(
    r"\b(?:Image|Video|File|Record|Plain|Reply)\s*\(|\[CQ:(?:image|video|file|record|reply|quote)\b",
    re.I,
)
CQ_SEGMENT_RE = re.compile(r"\[CQ:([A-Za-z0-9_]+)((?:,[^\]]*)?)\]")
PLACEHOLDER_SOURCE_VALUES = {"", "empty", "null", "none", "nil", "undefined", "false"}
MEDIA_URL_SOURCE_KEYS = (
    "url",
    "download_url",
    "downloadUrl",
    "file_url",
    "fileUrl",
    "media_url",
    "mediaUrl",
    "origin_url",
    "originUrl",
    "original_url",
    "originalUrl",
    "cdn_url",
    "cdnUrl",
    "preview_url",
    "previewUrl",
)
MEDIA_LOCAL_SOURCE_KEYS = (
    "path",
    "file_path",
    "filePath",
    "absolute_path",
    "absolutePath",
    "abs_path",
    "absPath",
    "local_path",
    "localPath",
    "source",
    "src",
    "file",
    "file_",
    "attachment_id",
)
MEDIA_BASE64_SOURCE_KEYS = (
    "base64",
    "file_base64",
    "fileBase64",
)
MEDIA_SOURCE_KEYS = MEDIA_URL_SOURCE_KEYS + MEDIA_LOCAL_SOURCE_KEYS + MEDIA_BASE64_SOURCE_KEYS
COMMAND_SEPARATOR_CHARS = ":\uFF1A,\uFF0C;\uFF1B"
COMMAND_PREFIX_CHARS = "/\uFF0F!\uFF01#\uFF03.\uFF0E\u3002~\uFF5E?\uFF1F"
LEADING_SPACE_CHARS = " \t\r\n\f\v\u3000\ufeff\u200b\u200c\u200d"


@dataclass(slots=True)
class PostMedia:
    kind: str
    source: str
    name: str = ""
    mime_type: str = ""
    size: int = 0
    raw_type: str = ""
    trusted_local: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PostPayload:
    content: str
    media: list[PostMedia]
    attachments: list[PostMedia] = field(default_factory=list)

    def to_request_body(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "media": [item.to_dict() for item in self.media],
        }


def _is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https"}


def _is_base64_source(value: str) -> bool:
    return value.startswith("base64://") or value.startswith("data:")


def _is_placeholder_source(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in PLACEHOLDER_SOURCE_VALUES


def base64_media_source(data: dict[str, Any]) -> str:
    """Return a normalized base64:// media source from OneBot-style payloads."""

    for key in MEDIA_BASE64_SOURCE_KEYS:
        value = data.get(key)
        if value is None or _is_placeholder_source(value):
            continue
        source = str(value).strip()
        if not source:
            continue
        if _is_base64_source(source):
            return normalize_source(source) or source
        return f"base64://{''.join(source.split())}"
    return ""


def _cq_unescape(value: str) -> str:
    return (
        value.replace("&#44;", ",")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&amp;", "&")
    )


def _cq_attrs(value: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for chunk in value.split(","):
        if not chunk or "=" not in chunk:
            continue
        key, raw = chunk.split("=", 1)
        key = key.strip()
        if key:
            attrs[key] = _cq_unescape(raw)
    return attrs


def parse_cq_message(text: str) -> list[dict[str, Any]]:
    """Parse OneBot/CQ code text into segment dicts while preserving plain text."""

    value = str(text or "")
    if "[CQ:" not in value:
        return []
    segments: list[dict[str, Any]] = []
    cursor = 0
    for match in CQ_SEGMENT_RE.finditer(value):
        if match.start() > cursor:
            plain = _cq_unescape(value[cursor : match.start()])
            if plain:
                segments.append({"type": "text", "data": {"text": plain}})
        kind = match.group(1).strip().lower()
        attrs = _cq_attrs(match.group(2)[1:] if match.group(2).startswith(",") else match.group(2))
        segments.append({"type": kind, "data": attrs})
        cursor = match.end()
    if cursor < len(value):
        plain = _cq_unescape(value[cursor:])
        if plain:
            segments.append({"type": "text", "data": {"text": plain}})
    return segments


def looks_like_supported_image_bytes(data: bytes) -> bool:
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data.startswith((b"GIF87a", b"GIF89a")):
        return True
    if data.startswith(b"BM"):
        return True
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def image_dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"BM") and len(data) >= 26:
        width = abs(int.from_bytes(data[18:22], "little", signed=True))
        height = abs(int.from_bytes(data[22:26], "little", signed=True))
        return width, height
    if data.startswith(b"\xff\xd8\xff"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                return None
            marker = data[index]
            index += 1
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                return None
            segment_length = int.from_bytes(data[index:index + 2], "big")
            if segment_length < 2 or index + segment_length > len(data):
                return None
            if marker in {
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            } and segment_length >= 7:
                height = int.from_bytes(data[index + 3:index + 5], "big")
                width = int.from_bytes(data[index + 5:index + 7], "big")
                return width, height
            index += segment_length
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height
        if chunk == b"VP8 " and len(data) >= 30:
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return width, height
        if chunk == b"VP8L" and len(data) >= 25:
            value = int.from_bytes(data[21:25], "little")
            width = 1 + (value & 0x3FFF)
            height = 1 + ((value >> 14) & 0x3FFF)
            return width, height
    return None


def _is_local_source(value: str) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    if parsed.scheme.lower() in {"http", "https"} or _is_base64_source(value):
        return False
    if parsed.scheme and not is_windows_drive_path(value) and not re.match(r"^[A-Za-z]:", value):
        return False
    return True


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _looks_like_path(value: str) -> bool:
    if not value:
        return False
    if value.startswith("file://"):
        return True
    if Path(value).exists():
        return True
    return bool(re.match(r"^[a-zA-Z]:", value) or value.startswith(("/", "\\")))


def normalize_source(value: Any) -> str:
    if value is None:
        return ""
    source = str(value).strip()
    if _is_placeholder_source(source):
        return ""
    if source.startswith("file://"):
        parsed = urlparse(source)
        if parsed.netloc and parsed.path:
            return unquote(f"//{parsed.netloc}{parsed.path}")
        path = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:[\\/]", path):
            return path[1:]
        return path
    return source


def _first_source_candidate(*values: Any) -> str:
    normalized = [normalize_source(value) for value in values]
    normalized = [value for value in normalized if value]
    for value in normalized:
        if _is_base64_source(value):
            return value
    for value in normalized:
        if _is_url(value):
            continue
        with contextlib.suppress(OSError):
            if Path(value).is_file():
                return value
    for value in normalized:
        if _is_url(value):
            return value
    for value in normalized:
        if not _is_url(value) and _looks_like_path(value):
            return value
    for value in normalized:
        return value
    return ""


def source_name(source: str) -> str:
    if not source:
        return ""
    if _is_url(source) or source.startswith("file://"):
        name = Path(normalize_source(source)).name
    elif _is_base64_source(source):
        name = ""
    else:
        name = Path(source).name
    return name or ""


def guess_mime_type(name_or_source: str) -> str:
    if not name_or_source or _is_base64_source(name_or_source):
        return ""
    guessed, _ = mimetypes.guess_type(name_or_source)
    return guessed or ""


def is_supported_image(media: PostMedia | dict[str, Any]) -> bool:
    if isinstance(media, dict):
        kind = str(media.get("kind") or media.get("type") or "").lower()
        source = _first_source_candidate(
            media.get("source"),
            media.get("url"),
            media.get("download_url"),
            media.get("downloadUrl"),
            media.get("file_url"),
            media.get("fileUrl"),
            media.get("media_url"),
            media.get("mediaUrl"),
            media.get("path"),
            media.get("file_path"),
            media.get("filePath"),
            media.get("absolute_path"),
            media.get("absolutePath"),
            media.get("abs_path"),
            media.get("absPath"),
            media.get("local_path"),
            media.get("localPath"),
            media.get("file"),
            media.get("file_"),
        )
        name = str(media.get("name") or source_name(source) or "")
        mime_type = str(media.get("mime_type") or media.get("mime") or guess_mime_type(name or source) or "")
    else:
        kind = media.kind
        source = media.source
        name = media.name
        mime_type = media.mime_type or guess_mime_type(name or source)

    if kind == "image":
        return True
    if mime_type.lower().startswith("image/"):
        return True
    suffix = Path(name or source).suffix.lower()
    return suffix in QZONE_IMAGE_SUFFIXES


def is_video_media(media: PostMedia | dict[str, Any]) -> bool:
    if isinstance(media, dict):
        kind = str(media.get("kind") or media.get("type") or "").lower()
        source = _first_source_candidate(
            media.get("source"),
            media.get("url"),
            media.get("download_url"),
            media.get("downloadUrl"),
            media.get("file_url"),
            media.get("fileUrl"),
            media.get("media_url"),
            media.get("mediaUrl"),
            media.get("path"),
            media.get("file_path"),
            media.get("filePath"),
            media.get("absolute_path"),
            media.get("absolutePath"),
            media.get("abs_path"),
            media.get("absPath"),
            media.get("local_path"),
            media.get("localPath"),
            media.get("file"),
            media.get("file_"),
        )
        name = str(media.get("name") or media.get("filename") or media.get("file_name") or source_name(source) or "")
        mime_type = str(media.get("mime_type") or media.get("mime") or guess_mime_type(name or source) or "")
        raw_type = str(media.get("raw_type") or "").lower()
    else:
        kind = media.kind
        source = media.source
        name = media.name
        mime_type = media.mime_type or guess_mime_type(name or source)
        raw_type = media.raw_type

    if str(kind).lower() == "video" or str(raw_type).lower() == "video":
        return True
    if str(mime_type).lower().startswith("video/"):
        return True
    suffix = Path(name or source).suffix.lower()
    return suffix in QZONE_VIDEO_SUFFIXES


def normalize_media_item(item: Any, *, default_kind: str = "file", trusted_local: bool = False) -> PostMedia | None:
    if item is None:
        return None
    if isinstance(item, PostMedia):
        if trusted_local and _is_local_source(item.source) and not item.trusted_local:
            return PostMedia(
                kind=item.kind,
                source=item.source,
                name=item.name,
                mime_type=item.mime_type,
                size=item.size,
                raw_type=item.raw_type,
                trusted_local=True,
            )
        return item
    if isinstance(item, str):
        source = normalize_source(item)
        if not source:
            return None
        name = source_name(source)
        media = PostMedia(
            kind=default_kind,
            source=source,
            name=name,
            mime_type=guess_mime_type(name or source),
            trusted_local=trusted_local and _is_local_source(source),
        )
        if is_supported_image(media):
            media.kind = "image"
        elif is_video_media(media):
            media.kind = "video"
        return media
    if isinstance(item, dict):
        source = _first_source_candidate(
            item.get("source"),
            item.get("url"),
            item.get("download_url"),
            item.get("downloadUrl"),
            item.get("file_url"),
            item.get("fileUrl"),
            item.get("media_url"),
            item.get("mediaUrl"),
            item.get("path"),
            item.get("file_path"),
            item.get("filePath"),
            item.get("absolute_path"),
            item.get("absolutePath"),
            item.get("abs_path"),
            item.get("absPath"),
            item.get("local_path"),
            item.get("localPath"),
            item.get("file"),
            item.get("file_"),
        )
        if not source:
            return None
        kind = str(item.get("kind") or item.get("type") or default_kind).lower()
        if kind == "voice":
            kind = "audio"
        name = str(item.get("name") or item.get("filename") or item.get("file_name") or source_name(source) or "")
        mime_type = str(item.get("mime_type") or item.get("mime") or guess_mime_type(name or source) or "")
        size_value = item.get("size") or item.get("file_size") or item.get("fileSize") or 0
        try:
            size = int(size_value or 0)
        except (TypeError, ValueError):
            size = 0
        item_trusted_local = trusted_local or _bool_value(
            item.get("trusted_local") or item.get("trusted_local_source") or item.get("from_message")
        )
        raw_type = str(item.get("raw_type") or kind)
        media = PostMedia(
            kind=kind,
            source=source,
            name=name,
            mime_type=mime_type,
            size=size,
            raw_type=raw_type,
            trusted_local=item_trusted_local or _is_local_source(source),
        )
        if is_supported_image(media):
            media.kind = "image"
        elif is_video_media(media):
            media.kind = "video"
        return media
    return None


def normalize_media_list(
    items: Iterable[Any] | None,
    *,
    default_kind: str = "file",
    trusted_local: bool = False,
) -> list[PostMedia]:
    if isinstance(items, (str, dict, PostMedia)):
        items = [items]
    media: list[PostMedia] = []
    for item in items or []:
        normalized = normalize_media_item(item, default_kind=default_kind, trusted_local=trusted_local)
        if normalized:
            media.append(normalized)
    return media


def split_publishable_images(media: Iterable[PostMedia]) -> tuple[list[PostMedia], list[PostMedia]]:
    images: list[PostMedia] = []
    fallback: list[PostMedia] = []
    for item in media:
        if is_supported_image(item):
            normalized = PostMedia(
                kind="image",
                source=item.source,
                name=item.name or source_name(item.source),
                mime_type=item.mime_type or guess_mime_type(item.name or item.source),
                size=item.size,
                raw_type=item.raw_type or item.kind,
                trusted_local=item.trusted_local,
            )
            images.append(normalized)
        else:
            fallback.append(item)
    return images, fallback


def media_reference_text(media: PostMedia) -> str:
    labels = {
        "file": "文件",
        "video": "视频",
        "audio": "音频",
        "record": "语音",
        "voice": "语音",
        "image": "图片",
    }
    label = labels.get(media.kind, "附件")
    name = media.name or source_name(media.source) or label
    if media.source and media.source != name:
        return f"[{label}: {name}] {media.source}"
    return f"[{label}: {name}]"


def _component_kind(component: Any) -> str:
    if isinstance(component, str):
        return "plain"
    if isinstance(component, dict):
        raw = component.get("type") or component.get("kind") or component.get("message_type") or ""
    else:
        raw = getattr(component, "type", None) or getattr(component, "kind", None) or component.__class__.__name__
    kind = str(raw or "").split(".")[-1].lower()
    aliases = {
        "plain": "plain",
        "text": "plain",
        "image": "image",
        "picture": "image",
        "file": "file",
        "video": "video",
        "record": "record",
        "voice": "audio",
        "audio": "audio",
        "reply": "reply",
        "replymessage": "reply",
        "reply_message": "reply",
        "quote": "quote",
        "quotemessage": "quote",
        "quote_message": "quote",
        "quoted": "quote",
        "reference": "reference",
    }
    return aliases.get(kind, kind)


def _component_mapping(component: Any) -> dict[str, Any]:
    if isinstance(component, dict):
        data = component.get("data")
        merged = dict(component)
        if isinstance(data, dict):
            merged.update(data)
        return merged
    data: dict[str, Any] = {}
    component_data = getattr(component, "data", None)
    if isinstance(component_data, dict):
        data.update(component_data)
    for attr in (
        "text",
        "content",
        "message",
        "file",
        "file_",
        "file_id",
        "fileId",
        "file_unique",
        "fileUnique",
        "file_name",
        "fileName",
        "source",
        "src",
        "url",
        "download_url",
        "downloadUrl",
        "file_url",
        "fileUrl",
        "media_url",
        "mediaUrl",
        "origin_url",
        "originUrl",
        "original_url",
        "originalUrl",
        "cdn_url",
        "cdnUrl",
        "preview_url",
        "previewUrl",
        "path",
        "file_path",
        "filePath",
        "absolute_path",
        "absolutePath",
        "abs_path",
        "absPath",
        "local_path",
        "localPath",
        "name",
        "filename",
        "thumb",
        "thumbnail",
        "cover",
        "mime",
        "mime_type",
        "size",
        "file_size",
        "fileSize",
        "id",
        "message_id",
        "messageId",
        "message_seq",
        "messageSeq",
        "seq",
    ):
        if hasattr(component, attr):
            data[attr] = getattr(component, attr)
    return data


def _mapping_value(owner: Any, key: str) -> Any:
    if owner is None:
        return None
    if isinstance(owner, dict):
        if key in owner:
            return owner.get(key)
        data = owner.get("data")
        if isinstance(data, dict):
            return data.get(key)
        return None
    if hasattr(owner, key):
        return getattr(owner, key)
    data = getattr(owner, "data", None)
    if isinstance(data, dict):
        return data.get(key)
    return None


def _iter_mapping_values(owner: Any, keys: Iterable[str]) -> Iterable[Any]:
    for key in keys:
        value = _mapping_value(owner, key)
        if value not in (None, "", [], (), {}):
            yield value


def _is_traversable_reference_value(value: Any) -> bool:
    return value is not None and not isinstance(value, (str, bytes, bytearray, int, float, bool))


def _component_text(component: Any) -> str:
    data = _component_mapping(component)
    for key in ("text", "content", "message"):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    if isinstance(component, str):
        return component
    return ""


def _source_needs_local_file(source: str) -> bool:
    if not source or _is_url(source) or _is_base64_source(source):
        return False
    return _is_local_source(source) and _looks_like_path(source)


def _local_media_source_exists(
    source: str,
    *,
    kind: str = "",
    name: str = "",
    mime_type: str = "",
) -> bool:
    if not _source_needs_local_file(source):
        return True
    descriptor = {"kind": kind, "source": source, "name": name, "mime_type": mime_type}
    if is_video_media(descriptor):
        suffixes = QZONE_VIDEO_SUFFIXES
    elif is_supported_image(descriptor):
        suffixes = QZONE_IMAGE_SUFFIXES
    else:
        suffixes = QZONE_IMAGE_SUFFIXES | QZONE_VIDEO_SUFFIXES
    return resolve_trusted_local_media_path(source, name=name, suffixes=suffixes) is not None


def _choose_media_source(data: dict[str, Any], *, kind: str = "") -> str:
    candidates = [data.get(key) for key in MEDIA_SOURCE_KEYS]
    normalized = [normalize_source(value) for value in candidates if value not in (None, "")]
    normalized = [value for value in normalized if value and not _is_placeholder_source(value)]
    name = str(data.get("name") or data.get("filename") or data.get("file_name") or data.get("fileName") or "")
    mime_type = str(data.get("mime_type") or data.get("mime") or guess_mime_type(name) or "")
    source_kind = kind or str(data.get("kind") or data.get("type") or "")
    for value in normalized:
        if _is_base64_source(value):
            return value
    for value in normalized:
        if not _is_url(value) and _looks_like_path(value):
            candidate_name = name or source_name(value)
            candidate_mime = mime_type or guess_mime_type(candidate_name or value)
            if _local_media_source_exists(value, kind=source_kind, name=candidate_name, mime_type=candidate_mime):
                return value
    for value in normalized:
        if _is_url(value):
            return value
    source = base64_media_source(data)
    if source:
        return source
    return ""


def _component_media(component: Any, kind: str, *, trusted_message: bool = False) -> PostMedia | None:
    data = _component_mapping(component)
    source = _choose_media_source(data, kind=kind)
    if not source:
        return None
    name = str(data.get("name") or data.get("filename") or data.get("file_name") or data.get("fileName") or source_name(source) or "")
    mime_type = str(data.get("mime_type") or data.get("mime") or guess_mime_type(name or source) or "")
    try:
        size = int(data.get("size") or data.get("file_size") or data.get("fileSize") or 0)
    except (TypeError, ValueError):
        size = 0
    media = PostMedia(
        kind=kind,
        source=source,
        name=name,
        mime_type=mime_type,
        size=size,
        raw_type=kind,
        trusted_local=trusted_message or _is_local_source(source),
    )
    if is_supported_image(media):
        media.kind = "image"
    elif is_video_media(media):
        media.kind = "video"
    return media


def _reference_media_is_usable(item: PostMedia) -> bool:
    if not item.source:
        return False
    if _is_url(item.source) or _is_base64_source(item.source):
        return True
    if item.kind == "video" or is_video_media(item):
        return (
            resolve_trusted_local_media_path(
                item.source,
                name=item.name or source_name(item.source),
                suffixes=QZONE_VIDEO_SUFFIXES,
            )
            is not None
        )
    return True


def _event_message_text(event: Any) -> str:
    message_obj = getattr(event, "message_obj", None)
    for owner in (event, message_obj):
        value = getattr(owner, "message_str", None)
        if isinstance(value, str) and value:
            return value
        getter = getattr(owner, "get_message_str", None)
        if callable(getter):
            with contextlib.suppress(Exception):
                value = getter()
            if isinstance(value, str) and value:
                return value
    return ""


def iter_event_components(event: Any) -> list[Any]:
    message_obj = getattr(event, "message_obj", None)
    candidates = [
        getattr(message_obj, "message", None),
        getattr(message_obj, "messages", None),
        getattr(message_obj, "chain", None),
        getattr(message_obj, "message_chain", None),
        getattr(event, "message", None),
        getattr(event, "messages", None),
        getattr(event, "chain", None),
        getattr(event, "message_chain", None),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        if isinstance(candidate, (list, tuple)) and candidate:
            return list(candidate)
        inner = getattr(candidate, "chain", None) or getattr(candidate, "messages", None)
        if isinstance(inner, (list, tuple)) and inner:
            return list(inner)
    raw = getattr(message_obj, "raw_message", None) or getattr(event, "raw_message", None)
    if isinstance(raw, list) and raw:
        return list(raw)
    if isinstance(raw, dict) and isinstance(raw.get("message"), list) and raw.get("message"):
        return list(raw["message"])
    if isinstance(raw, dict) and isinstance(raw.get("raw_message"), str):
        segments = parse_cq_message(raw["raw_message"])
        if segments:
            return segments
    if isinstance(raw, str):
        segments = parse_cq_message(raw)
        if segments:
            return segments
    event_text = _event_message_text(event)
    if event_text:
        segments = parse_cq_message(event_text)
        if segments:
            return segments
        return [event_text]
    return []


def _media_from_reference_field(value: Any, *, key: str) -> list[PostMedia]:
    if value in (None, "", [], (), {}):
        return []
    default_kind = "image" if key in {"image", "images"} else "file"
    if isinstance(value, dict):
        values: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]
    return [
        item
        for item in normalize_media_list(values, default_kind=default_kind, trusted_local=True)
        if _reference_media_is_usable(item)
    ]


def _collect_referenced_media(
    value: Any,
    *,
    seen: set[int],
    depth: int = 0,
    trusted_message: bool = True,
) -> list[PostMedia]:
    if isinstance(value, str):
        media: list[PostMedia] = []
        for segment in parse_cq_message(value):
            media.extend(
                _collect_referenced_media(
                    segment,
                    seen=seen,
                    depth=depth + 1,
                    trusted_message=trusted_message,
                )
            )
        return media
    if depth > REFERENCE_MAX_DEPTH or not _is_traversable_reference_value(value):
        return []

    marker = id(value)
    if marker in seen:
        return []
    seen.add(marker)

    media: list[PostMedia] = []
    if isinstance(value, (list, tuple, set)):
        for item in value:
            media.extend(_collect_referenced_media(item, seen=seen, depth=depth + 1, trusted_message=trusted_message))
        return media

    kind = _component_kind(value)
    if kind in MEDIA_KINDS:
        item = _component_media(value, kind, trusted_message=trusted_message)
        if item:
            media.append(item)

    for key in REFERENCE_MEDIA_KEYS:
        for nested in _iter_mapping_values(value, (key,)):
            media.extend(_media_from_reference_field(nested, key=key))

    for nested in _iter_mapping_values(value, MESSAGE_CHAIN_KEYS):
        if isinstance(nested, str) or _is_traversable_reference_value(nested):
            media.extend(_collect_referenced_media(nested, seen=seen, depth=depth + 1, trusted_message=trusted_message))

    for nested in _iter_mapping_values(value, REFERENCE_OWNER_KEYS):
        if isinstance(nested, str) or _is_traversable_reference_value(nested):
            media.extend(_collect_referenced_media(nested, seen=seen, depth=depth + 1, trusted_message=trusted_message))

    return media


def iter_referenced_media(event: Any) -> list[PostMedia]:
    """Return media attached to quoted/replied messages without importing their text."""

    seen: set[int] = set()
    media: list[PostMedia] = []
    message_obj = getattr(event, "message_obj", None)
    for owner in (message_obj, event):
        for value in _iter_mapping_values(owner, REFERENCE_OWNER_KEYS):
            media.extend(_collect_referenced_media(value, seen=seen))

    raw = getattr(message_obj, "raw_message", None) or getattr(event, "raw_message", None)
    if isinstance(raw, str) or _is_traversable_reference_value(raw):
        media.extend(_collect_referenced_media(raw, seen=seen))

    for component in iter_event_components(event):
        kind = _component_kind(component)
        if kind in REFERENCE_KINDS:
            media.extend(_collect_referenced_media(component, seen=seen))
            continue
        for value in _iter_mapping_values(component, REFERENCE_OWNER_KEYS):
            media.extend(_collect_referenced_media(value, seen=seen))

    return media


def collect_message_media(payload: Any) -> list[PostMedia]:
    """Return media segments from a message payload fetched from the platform."""

    media = _collect_referenced_media(payload, seen=set(), trusted_message=True)
    result: list[PostMedia] = []
    seen: set[tuple[str, str]] = set()
    for item in media:
        key = _media_dedupe_key(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _coerce_reference_message_id(value: Any) -> int | str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError:
        return text
    return number if number > 0 else None


def _reference_message_id(value: Any) -> int | str | None:
    data = _component_mapping(value)
    for key in REFERENCE_MESSAGE_ID_KEYS:
        identifier = _coerce_reference_message_id(data.get(key))
        if identifier is not None:
            return identifier
    return None


def _collect_reference_message_ids(value: Any, *, seen: set[int], depth: int = 0) -> list[int | str]:
    if isinstance(value, str):
        result: list[int | str] = []
        for segment in parse_cq_message(value):
            result.extend(_collect_reference_message_ids(segment, seen=seen, depth=depth + 1))
        return result
    if depth > REFERENCE_MAX_DEPTH or not _is_traversable_reference_value(value):
        return []
    marker = id(value)
    if marker in seen:
        return []
    seen.add(marker)

    result: list[int | str] = []
    if isinstance(value, (list, tuple, set)):
        for item in value:
            result.extend(_collect_reference_message_ids(item, seen=seen, depth=depth + 1))
        return result

    kind = _component_kind(value)
    if kind in REFERENCE_KINDS:
        identifier = _reference_message_id(value)
        if identifier is not None:
            result.append(identifier)

    for nested in _iter_mapping_values(value, MESSAGE_CHAIN_KEYS):
        if isinstance(nested, str) or _is_traversable_reference_value(nested):
            result.extend(_collect_reference_message_ids(nested, seen=seen, depth=depth + 1))
    for nested in _iter_mapping_values(value, REFERENCE_OWNER_KEYS):
        if isinstance(nested, str) or _is_traversable_reference_value(nested):
            result.extend(_collect_reference_message_ids(nested, seen=seen, depth=depth + 1))
    return result


def iter_reference_message_ids(event: Any) -> list[int | str]:
    """Return referenced platform message ids from reply/quote segments."""

    seen_values: set[str] = set()
    result: list[int | str] = []
    seen_objects: set[int] = set()

    def append(values: Iterable[int | str]) -> None:
        for value in values:
            key = str(value)
            if not key or key in seen_values:
                continue
            seen_values.add(key)
            result.append(value)

    message_obj = getattr(event, "message_obj", None)
    for owner in (message_obj, event):
        for value in _iter_mapping_values(owner, REFERENCE_OWNER_KEYS):
            append(_collect_reference_message_ids(value, seen=seen_objects))

    raw = getattr(message_obj, "raw_message", None) or getattr(event, "raw_message", None)
    append(_collect_reference_message_ids(raw, seen=seen_objects))

    for component in iter_event_components(event):
        kind = _component_kind(component)
        if kind in REFERENCE_KINDS:
            identifier = _reference_message_id(component)
            if identifier is not None:
                append([identifier])
        for value in _iter_mapping_values(component, REFERENCE_OWNER_KEYS):
            append(_collect_reference_message_ids(value, seen=seen_objects))

    return result


def _media_dedupe_key(item: PostMedia) -> tuple[str, str]:
    return (item.kind, item.source)


def _append_collected_media(
    item: PostMedia,
    *,
    media: list[PostMedia],
    attachments: list[PostMedia],
    reference_parts: list[str],
    seen: set[tuple[str, str]],
    add_attachment_reference: bool,
) -> None:
    key = _media_dedupe_key(item)
    if key in seen:
        return
    seen.add(key)
    if item.kind in {"image", "video"}:
        media.append(item)
        return
    attachments.append(item)
    if add_attachment_reference:
        reference_parts.append(media_reference_text(item))


def _strip_leading_command_noise(text: str) -> tuple[str, bool]:
    stripped_noise = False
    value = text.lstrip(LEADING_SPACE_CHARS)
    stripped_noise = stripped_noise or value != text
    while value:
        match = re.match(r"\[CQ:at,[^\]]+\]\s*", value, re.I)
        if match:
            value = value[match.end() :].lstrip(LEADING_SPACE_CHARS)
            stripped_noise = True
            continue
        mention_boundary = re.escape(LEADING_SPACE_CHARS + COMMAND_SEPARATOR_CHARS + COMMAND_PREFIX_CHARS)
        match = re.match(r"@\S+?(?:[" + mention_boundary + r"]+|$)", value)
        if match:
            value = value[match.end() :].lstrip(LEADING_SPACE_CHARS)
            stripped_noise = True
            continue
        break
    return value, stripped_noise


def _strip_command_separator(text: str) -> str:
    value = text.lstrip()
    if value[:1] in COMMAND_SEPARATOR_CHARS:
        value = value[1:].lstrip()
    return value


def strip_command_prefix(text: str, prefixes: Iterable[str]) -> str:
    stripped, stripped_noise = _strip_leading_command_noise(text)
    for prefix in prefixes:
        prefix = prefix.strip().lstrip("/\uff0f").strip()
        if not prefix:
            continue
        command_marker = r"(?:[" + re.escape(COMMAND_PREFIX_CHARS) + r"]+\s*)?"
        pattern = r"^" + command_marker + r"\s*" + r"\s+".join(re.escape(part) for part in prefix.split())
        match = re.match(pattern, stripped, re.I)
        if match:
            return _strip_command_separator(stripped[match.end() :])
    if stripped_noise:
        return text
    return text


def looks_like_component_string(text: str) -> bool:
    return bool(text and COMPONENT_STRING_RE.search(text))


def join_text_parts_for_command_scan(parts: Iterable[str]) -> str:
    text = ""
    for part in parts:
        if not part:
            continue
        if text and not text[-1].isspace() and not part[0].isspace():
            text += " "
        text += part
    return text


def strip_command_prefix_from_parts(text: str, parts: Iterable[str], prefixes: Iterable[str]) -> str:
    stripped = strip_command_prefix(text, prefixes).strip()
    if stripped != text:
        return stripped
    spaced = join_text_parts_for_command_scan(parts).strip()
    if spaced and spaced != text:
        stripped_spaced = strip_command_prefix(spaced, prefixes).strip()
        if stripped_spaced != spaced:
            return stripped_spaced
    return stripped


def sanitize_publish_content(
    content: Any,
    *,
    content_sanitized: bool = False,
    command_prefixes: Iterable[str] = ("qzone post",),
) -> str:
    value = str(content or "")
    if not content_sanitized:
        value = strip_command_prefix(value, command_prefixes).strip()
    return value


def collect_post_payload(
    event: Any,
    *,
    fallback_content: str = "",
    include_event_text: bool = True,
    command_prefixes: Iterable[str] = (),
    extra_media: Iterable[Any] | None = None,
) -> PostPayload:
    content_parts: list[str] = []
    reference_parts: list[str] = []
    media: list[PostMedia] = []
    attachments: list[PostMedia] = []
    seen_media: set[tuple[str, str]] = set()
    first_text = True
    event_prefix_stripped = False
    components = iter_event_components(event)
    event_text = _event_message_text(event)
    event_text_consumed = False

    for component in components:
        kind = _component_kind(component)
        if kind in TEXT_KINDS:
            text = _component_text(component)
            if first_text and command_prefixes:
                original_text = text
                text = strip_command_prefix(text, command_prefixes)
                event_prefix_stripped = text != original_text
            first_text = False
            if include_event_text and text:
                content_parts.append(text)
            continue
        if kind in MEDIA_KINDS:
            item = _component_media(component, kind, trusted_message=True)
            if not item:
                continue
            _append_collected_media(
                item,
                media=media,
                attachments=attachments,
                reference_parts=reference_parts,
                seen=seen_media,
                add_attachment_reference=True,
            )

    for item in iter_referenced_media(event):
        _append_collected_media(
            item,
            media=media,
            attachments=attachments,
            reference_parts=reference_parts,
            seen=seen_media,
            add_attachment_reference=True,
        )

    for item in normalize_media_list(extra_media, trusted_local=False):
        _append_collected_media(
            item,
            media=media,
            attachments=attachments,
            reference_parts=reference_parts,
            seen=seen_media,
            add_attachment_reference=True,
        )
    if include_event_text and command_prefixes and event_text:
        event_content = strip_command_prefix(event_text, command_prefixes).strip()
        if event_content != event_text.strip():
            event_text_consumed = True
            if media and looks_like_component_string(event_content):
                content_parts = []
            else:
                content_parts = [event_content] if event_content else []
            event_prefix_stripped = True
    if include_event_text and not content_parts and components and not event_text_consumed:
        if event_text and not (media and looks_like_component_string(event_text)):
            content_parts.append(event_text)
    content = "".join(content_parts).strip() if include_event_text else ""
    if content and command_prefixes and not event_prefix_stripped:
        content = strip_command_prefix_from_parts(content, content_parts, command_prefixes)
    fallback = str(fallback_content or "").strip()
    if command_prefixes:
        fallback = strip_command_prefix(fallback, command_prefixes).strip()
    use_fallback = bool(fallback and not (media and looks_like_component_string(fallback)))
    if not content and use_fallback:
        content = fallback
    if not include_event_text and use_fallback:
        content = fallback
    if reference_parts:
        refs = "\n".join(reference_parts)
        content = "\n".join(part for part in (content, refs) if part)
    return PostPayload(content=content, media=media, attachments=attachments)
