"""Target-style Qzone post and comment adapters."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from html import unescape
from typing import Any, Iterable
from urllib.parse import urlparse

from .models import FeedEntry
from .utils import truncate


TAG_RE = re.compile(r"<[^>]+>")
EM_RE = re.compile(r"\[em\].*?\[/em\]")
IMG_TAG_RE = re.compile(r"<\s*img\b[^>]*>", re.I | re.S)
IMG_SOURCE_ATTR_RE = re.compile(
    r"""\b(?P<name>src|srcset|data-src|data-original|origin-src|original-src|url)"""
    r"""\s*=\s*(?:(["'])(.*?)\2|([^\s"'<>`]+))""",
    re.I | re.S,
)
CSS_URL_RE = re.compile(r"""url\(\s*(?:(["'])(.*?)\1|([^)]+))\s*\)""", re.I | re.S)
NICKNAME_KEYS = (
    "nickname",
    "nickName",
    "nick_name",
    "nick",
    "name",
    "uinname",
    "userName",
    "username",
    "ownerName",
    "displayName",
)
NICKNAME_CONTAINER_KEYS = (
    "userinfo",
    "userInfo",
    "user",
    "owner",
    "author",
    "poster",
    "host",
    "profile",
    "blogInfo",
    "cell_userinfo",
    "cellUserInfo",
    "_feed_raw",
)
NICKNAME_COLLECTION_KEYS = (
    "users",
    "userlist",
    "userList",
    "userMap",
    "uinMap",
    "profileMap",
)
USER_ID_KEYS = ("uin", "hostuin", "hostUin", "user_id", "userId", "qq", "uinnum")
NESTED_NICKNAME_PATHS = (
    ("data", "userinfo"),
    ("data", "userInfo"),
    ("data", "user"),
    ("data", "owner"),
    ("data", "cell_userinfo"),
    ("data", "cellUserInfo"),
    ("data", "feed", "userinfo"),
    ("data", "feed", "user"),
    ("data", "feed", "owner"),
    ("data", "feed", "cell_userinfo"),
    ("data", "feed", "cellUserInfo"),
    ("feed", "userinfo"),
    ("feed", "user"),
    ("feed", "owner"),
    ("feed", "cell_userinfo"),
    ("feed", "cellUserInfo"),
    ("entry", "userinfo"),
    ("entry", "user"),
    ("entry", "owner"),
    ("entry", "cell_userinfo"),
    ("entry", "cellUserInfo"),
)
IMAGE_URL_KEYS = (
    "origin_url",
    "originUrl",
    "original_url",
    "originalUrl",
    "largeurl",
    "largeUrl",
    "url",
    "pic_url",
    "picUrl",
    "photo_url",
    "photoUrl",
    "photourl",
    "image_url",
    "imageUrl",
    "url3",
    "url2",
    "url1",
    "pre",
    "sloc",
    "smallurl",
    "smallUrl",
    "thumb",
    "thumbnail",
    "cover",
    "coverUrl",
)
IMAGE_ALIAS_PRIORITY_KEYS = (
    "url3",
    "origin_url",
    "originUrl",
    "original_url",
    "originalUrl",
    "largeurl",
    "largeUrl",
    "url2",
    "pic_url",
    "picUrl",
    "photo_url",
    "photoUrl",
    "photourl",
    "image_url",
    "imageUrl",
    "url",
    "url1",
    "pre",
    "sloc",
    "smallurl",
    "smallUrl",
    "thumb",
    "thumbnail",
    "cover",
    "coverUrl",
)
IMAGE_CONTAINER_KEYS = (
    "images",
    "image",
    "pics",
    "pic",
    "picdata",
    "picData",
    "cell_pic",
    "cellPic",
    "photos",
    "photo",
    "photoList",
    "photolist",
    "picList",
    "piclist",
    "imageList",
    "imagelist",
    "media",
    "medias",
    "attachment",
    "attachments",
)
IMAGE_NESTED_CONTAINER_KEYS = (
    "data",
    "feed",
    "entry",
    "original",
    "content",
    "summary",
    "cell",
    "cell_summary",
    "cellSummary",
    "_feed_raw",
)
IMAGE_HTML_KEYS = (
    "html",
    "htmlContent",
    "html_content",
    "contentHtml",
    "content_html",
    "richval",
    "richVal",
    "content",
    "summary",
    "con",
    "msg",
    "message",
    "text",
)
FEED_ID_KEYS = ("fid", "tid", "cellid", "feedid", "feedId", "ugcrightkey", "ugckey")
FEED_FALLBACK_ID_KEYS = ("key",)
FEED_ID_CONTAINER_KEYS = ("common", "comm", "cell_comm", "cellComm", "id", "cell_id", "cellId")
FEED_NODE_HINT_KEYS = (
    "summary",
    "content",
    "con",
    "msg",
    "message",
    "text",
    "html",
    "htmlContent",
    "common",
    "cell_comm",
    "cellComm",
    "userinfo",
    "user",
    "owner",
    "operation",
    "like",
    "comment",
    "feed",
    "original",
    "data",
)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = unescape(value).strip()
    if not text or text[0] != "{" or len(text) > 200_000:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def clean_qzone_text(value: Any) -> str:
    text = str(value or "")
    text = EM_RE.sub("", text)
    text = TAG_RE.sub("", text)
    return unescape(text).strip()


def normalize_image_source(value: Any) -> str:
    source = unescape(str(value or "")).strip().strip("\"'")
    source = source.replace("\\/", "/")
    if not source:
        return ""
    if source.startswith("//"):
        source = f"https:{source}"
    if any(char in source for char in "\r\n\t<>"):
        return ""
    parsed = urlparse(source)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "qlogo" in host or host in {"thirdqq.qlogo.cn", "q.qlogo.cn"}:
        return ""
    if "/headimg" in path or "/headimg_dl" in path:
        return ""
    if "qzone" in host and re.search(r"/qzone/\d+/\d+/50(?:[/?]|$)", path):
        return ""
    source = re.sub(r"/(?:a|m|s)(?=&)", "/b", source)
    source = re.sub(r"([?&])(?:w|h)=\d+(?=&|$)", "", source)
    source = source.replace("?&", "?").rstrip("?&")
    return source


def _normalized_qzone_psc_payload(value: str) -> str:
    payload = unescape(str(value or "")).replace("\\/", "/").strip()
    if not payload:
        return ""
    payload = re.sub(r"/(?:a|m|s)(?=&|$)", "/b", payload)
    payload = re.sub(r"&(?:bo|w|h)=[^&]*", "", payload, flags=re.I)
    return payload.rstrip("!&?")


def image_source_key(source: str) -> str:
    parsed = urlparse(source)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")
    query = parsed.query.strip()
    if "qzone.qq.com" in host and "/photo/" in parsed.path:
        parts = [part for part in parsed.path.split("/") if part]
        try:
            index = parts.index("photo")
        except ValueError:
            index = -1
        if index >= 0 and len(parts) > index + 2:
            return f"qzone-photo:{parts[index + 1]}:{parts[index + 2].split('_', 1)[0]}"
    if path.lower() == "psc" and query.startswith("/"):
        psc_payload = _normalized_qzone_psc_payload(query)
        if psc_payload:
            return f"qzone-psc:{psc_payload}"
    if path.lower().startswith("psc/"):
        psc_payload = _normalized_qzone_psc_payload("/" + path.split("/", 1)[1])
        if psc_payload:
            return f"qzone-psc:{psc_payload}"
    return f"url:{host}{parsed.path}?{parsed.query}"


def dedupe_image_sources(sources: Iterable[Any]) -> list[str]:
    images: list[str] = []
    seen: set[str] = set()
    for source in sources:
        raw_value = str(source or "").strip()
        value = normalize_image_source(source)
        if value:
            key = image_source_key(value)
        elif raw_value.startswith(("data:", "base64://", "blob:")):
            value = raw_value
            key = f"inline:{raw_value}"
        else:
            continue
        if key in seen:
            continue
        seen.add(key)
        images.append(value)
    return images


def _first_mapping(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _first_text(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return clean_qzone_text(value)
    return ""


def _clean_nickname(value: Any, *, hostuin: int = 0) -> str:
    text = clean_qzone_text(value)
    if not text:
        return ""
    if hostuin and text == str(hostuin):
        return ""
    if re.fullmatch(r"\d{5,}", text):
        return ""
    return text


def _first_nickname(
    raw: dict[str, Any],
    *,
    hostuin: int = 0,
    depth: int = 2,
    require_owner: bool = False,
) -> str:
    if require_owner and hostuin and not _mapping_uin(raw):
        return ""
    if not _owner_matches(raw, hostuin=hostuin):
        return ""
    for key in NICKNAME_KEYS:
        nickname = _clean_nickname(raw.get(key), hostuin=hostuin)
        if nickname:
            return nickname
    if depth <= 0:
        return ""
    for key in NICKNAME_CONTAINER_KEYS:
        for item in _iter_nickname_mappings(raw.get(key)):
            nickname = _first_nickname(item, hostuin=hostuin, depth=depth - 1)
            if nickname:
                return nickname
    for key in NICKNAME_COLLECTION_KEYS:
        for item in _iter_nickname_mappings(raw.get(key)):
            nickname = _first_nickname(item, hostuin=hostuin, depth=depth - 1, require_owner=True)
            if nickname:
                return nickname
    return ""


def _iter_mappings(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item


def _iter_nickname_mappings(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for key, item in value.items():
            if isinstance(item, dict):
                candidate = item
                key_text = str(key)
                if key_text.isdigit() and not _mapping_uin(candidate):
                    candidate = dict(item)
                    candidate["uin"] = int(key_text)
                if key_text.isdigit() or any(
                    marker in candidate for marker in (*NICKNAME_KEYS, *USER_ID_KEYS, *NICKNAME_CONTAINER_KEYS)
                ):
                    yield candidate
            elif isinstance(item, list):
                for nested in item:
                    if isinstance(nested, dict):
                        yield nested
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item


def _nested_mapping(raw: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = raw
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _mapping_uin(raw: dict[str, Any]) -> int:
    for key in USER_ID_KEYS:
        value = raw.get(key)
        if value not in (None, ""):
            return _to_int(value)
    return 0


def _owner_matches(raw: dict[str, Any], *, hostuin: int = 0) -> bool:
    owner_uin = _mapping_uin(raw)
    return not hostuin or not owner_uin or owner_uin == hostuin


def extract_nickname(raw: dict[str, Any] | None, *, hostuin: int = 0) -> str:
    """Best-effort owner nickname extraction from common Qzone feed/detail shapes."""

    if not isinstance(raw, dict):
        return ""

    for key in NICKNAME_CONTAINER_KEYS:
        for item in _iter_nickname_mappings(raw.get(key)):
            nickname = _first_nickname(item, hostuin=hostuin)
            if nickname:
                return nickname

    for key in NICKNAME_COLLECTION_KEYS:
        for item in _iter_nickname_mappings(raw.get(key)):
            nickname = _first_nickname(item, hostuin=hostuin, require_owner=True)
            if nickname:
                return nickname

    for path in NESTED_NICKNAME_PATHS:
        require_owner = path[-1] in NICKNAME_COLLECTION_KEYS
        for item in _iter_nickname_mappings(_nested_mapping(raw, *path)):
            nickname = _first_nickname(item, hostuin=hostuin, require_owner=require_owner)
            if nickname:
                return nickname

    direct = _first_nickname(raw, hostuin=hostuin)
    if direct:
        return direct

    return ""


@dataclass(slots=True)
class _ImageCandidate:
    url: str
    key: str


@dataclass(slots=True)
class QzoneComment:
    commentid: str
    uin: int = 0
    nickname: str = ""
    content: str = ""
    created_at: int = 0
    parent_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def brief(self, index: int | None = None) -> str:
        prefix = f"{index}. " if index is not None else ""
        name = _clean_nickname(self.nickname, hostuin=self.uin) or "QQ 空间用户"
        return f"{prefix}{name}: {truncate(self.content, 80)}"


@dataclass(slots=True)
class QzonePost:
    hostuin: int
    fid: str
    appid: int = 311
    summary: str = ""
    nickname: str = ""
    created_at: int = 0
    like_count: int = 0
    comment_count: int = 0
    liked: bool = False
    images: list[str] = field(default_factory=list)
    comments: list[QzoneComment] = field(default_factory=list)
    busi_param: dict[str, Any] = field(default_factory=dict)
    local_id: int = 0
    saved_id: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["comments"] = [item.to_dict() for item in self.comments]
        return data

    def brief(self, index: int | None = None) -> str:
        prefix = f"{index}. " if index is not None else ""
        saved = f"稿件 #{self.saved_id} | " if self.saved_id else ""
        liked = "已赞" if self.liked else "未赞"
        name = extract_nickname({"nickname": self.nickname}, hostuin=self.hostuin)
        if not name:
            name = extract_nickname(self.raw, hostuin=self.hostuin)
        name = name or "QQ 空间用户"
        return (
            f"{prefix}{saved}{name}\n"
            f"{truncate(self.summary, 220)}\n"
            f"{liked} | {self.like_count} 赞 | {self.comment_count} 评论"
        )

    def detail_text(self, index: int | None = None, *, max_comments: int = 8) -> str:
        lines = [self.brief(index)]
        if self.images:
            lines.append("图片: " + ", ".join(self.images[:9]))
        if self.comments:
            lines.append("评论:")
            for offset, comment in enumerate(self.comments[:max_comments]):
                lines.append(comment.brief(offset))
        return "\n".join(lines)


def comment_from_raw(raw: dict[str, Any], *, parent_id: str = "") -> QzoneComment:
    user = _first_mapping(raw.get("user"), raw.get("userinfo"), raw.get("commenter"))
    commentid = raw.get("commentid") or raw.get("commentId") or raw.get("tid") or raw.get("id") or ""
    uin = _to_int(raw.get("uin") or raw.get("commentUin") or user.get("uin") or user.get("user_id"))
    nickname = _first_text(user, "nickname", "name", "uinname") or _first_text(raw, "nickname", "name")
    content = _first_text(raw, "content", "commentContent", "htmlContent", "text")
    created_at = _to_int(raw.get("date") or raw.get("created_at") or raw.get("pubtime") or raw.get("abstime"))
    return QzoneComment(
        commentid=str(commentid),
        uin=uin,
        nickname=nickname,
        content=content,
        created_at=created_at,
        parent_id=str(parent_id or raw.get("parentId") or raw.get("parent_tid") or ""),
        raw=dict(raw),
    )


def _extract_nested_replies(raw: dict[str, Any], parent_id: str) -> list[QzoneComment]:
    replies: list[QzoneComment] = []
    for key in ("replyList", "replylist", "replies", "list_3", "children"):
        for item in _iter_mappings(raw.get(key)):
            replies.append(comment_from_raw(item, parent_id=parent_id))
            replies.extend(_extract_nested_replies(item, parent_id=str(item.get("commentid") or item.get("tid") or parent_id)))
    return replies


def extract_comments(payload: dict[str, Any]) -> list[QzoneComment]:
    candidates: list[Any] = []
    comment_block = payload.get("comment")
    if isinstance(comment_block, dict):
        candidates.extend(
            [
                comment_block.get("comments"),
                comment_block.get("commentlist"),
                comment_block.get("list"),
            ]
        )
    candidates.extend(
        [
            payload.get("comments"),
            payload.get("commentlist"),
            payload.get("list_3"),
        ]
    )
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend([data.get("comments"), data.get("commentlist")])

    comments: list[QzoneComment] = []
    seen: set[tuple[str, int, str]] = set()
    for candidate in candidates:
        for item in _iter_mappings(candidate):
            comment = comment_from_raw(item)
            key = (comment.commentid, comment.uin, comment.content)
            if key not in seen:
                comments.append(comment)
                seen.add(key)
            for reply in _extract_nested_replies(item, comment.commentid):
                reply_key = (reply.commentid, reply.uin, reply.content)
                if reply_key not in seen:
                    comments.append(reply)
                    seen.add(reply_key)
    return comments


def _extract_image_candidates(payload: dict[str, Any], *, fid: str = "", hostuin: int = 0) -> list[_ImageCandidate]:
    candidates: list[_ImageCandidate] = []
    seen_image_keys: set[str] = set()
    seen_nodes: set[int] = set()
    target_fid = str(fid or "").strip()
    target_hostuin = _to_int(hostuin)

    def add(value: Any, *, identity: str = "") -> None:
        if isinstance(value, str):
            value = normalize_image_source(value)
            if not value:
                return
            key = identity or image_source_key(value)
            if key in seen_image_keys:
                return
            seen_image_keys.add(key)
            candidates.append(_ImageCandidate(url=value, key=key))

    def _photo_id_from_pic_id(value: Any) -> tuple[str, str]:
        text = str(value or "").strip()
        if not text:
            return "", ""
        parts = [part for part in text.split(",") if part]
        if len(parts) >= 2:
            return parts[0], parts[1]
        return "", text

    def photo_identity(value: dict[str, Any]) -> str:
        albumid = str(value.get("albumid") or value.get("albumId") or "").strip()
        lloc = str(value.get("lloc") or value.get("realLloc") or value.get("picid") or value.get("picId") or "").strip()
        pic_id_value = value.get("pic_id") or value.get("picId")
        pic_id_source = normalize_image_source(pic_id_value)
        if pic_id_source:
            return image_source_key(pic_id_source)
        if not lloc:
            parsed_album, parsed_lloc = _photo_id_from_pic_id(pic_id_value)
            albumid = albumid or parsed_album
            lloc = parsed_lloc
        if lloc:
            return f"qzone-photo-id:{albumid}:{lloc}"
        quankey = str(value.get("quankey") or value.get("photoKey") or value.get("photokey") or "").strip()
        if quankey:
            return f"qzone-photo-key:{albumid}:{quankey}"
        return ""

    def best_photourl_source(value: Any) -> str:
        if not isinstance(value, dict):
            return ""
        for key in ("1", "0", "14", "11", "2", "3"):
            item = value.get(key)
            if isinstance(item, dict):
                source = normalize_image_source(item.get("url"))
                if source:
                    return source
            else:
                source = normalize_image_source(item)
                if source:
                    return source
        ranked: list[tuple[int, int, str]] = []
        for item in value.values():
            if not isinstance(item, dict):
                continue
            source = normalize_image_source(item.get("url"))
            if not source:
                continue
            width = _to_int(item.get("width"))
            height = _to_int(item.get("height"))
            ranked.append((width * height, max(width, height), source))
        if ranked:
            ranked.sort(reverse=True)
            return ranked[0][2]
        return ""

    def best_mapping_image_source(value: dict[str, Any]) -> str:
        source = best_photourl_source(value.get("photourl"))
        if source:
            return source
        for key in IMAGE_ALIAS_PRIORITY_KEYS:
            source = normalize_image_source(value.get(key))
            if source:
                return source
        return ""

    def looks_like_qzone_photo(value: dict[str, Any]) -> bool:
        return any(
            key in value
            for key in (
                "photourl",
                "lloc",
                "realLloc",
                "pic_id",
                "picid",
                "picId",
                "albumid",
                "albumId",
                "origin_width",
                "origin_height",
            )
        )

    def add_mapping_image(value: dict[str, Any]) -> bool:
        source = best_mapping_image_source(value)
        if not source:
            return False
        identity = photo_identity(value) if looks_like_qzone_photo(value) else ""
        add(source, identity=identity)
        return bool(identity)

    def normalize_feed_id(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        match = re.fullmatch(r"\d+_\d+_([^_]+)_?", text)
        if match:
            return match.group(1)
        return text

    def mapping_feed_id(value: dict[str, Any]) -> str:
        for key in FEED_ID_KEYS:
            candidate = value.get(key)
            if candidate not in (None, ""):
                return normalize_feed_id(candidate)
        if not best_mapping_image_source(value) and any(key in value for key in FEED_NODE_HINT_KEYS):
            for key in FEED_FALLBACK_ID_KEYS:
                candidate = value.get(key)
                if candidate not in (None, ""):
                    return normalize_feed_id(candidate)
        for key in FEED_ID_CONTAINER_KEYS:
            child = _json_mapping(value.get(key))
            if not child:
                continue
            for child_key in (*FEED_ID_KEYS, *FEED_FALLBACK_ID_KEYS):
                candidate = child.get(child_key)
                if candidate not in (None, ""):
                    return normalize_feed_id(candidate)
        return ""

    def mapping_hostuin(value: dict[str, Any]) -> int:
        owner = _mapping_uin(value)
        if owner:
            return owner
        for key in ("userinfo", "user", "owner", "host"):
            child = value.get(key)
            if isinstance(child, dict):
                owner = _mapping_uin(child)
                if owner:
                    return owner
        return 0

    def belongs_to_target(value: dict[str, Any]) -> bool:
        if target_fid:
            node_fid = mapping_feed_id(value)
            if node_fid and node_fid != target_fid:
                return False
        if target_hostuin:
            node_hostuin = mapping_hostuin(value)
            if node_hostuin and node_hostuin != target_hostuin:
                return False
        return True

    def add_html_images(value: Any) -> None:
        if not isinstance(value, str):
            return
        text = unescape(value)
        for tag_match in IMG_TAG_RE.finditer(text):
            tag = tag_match.group(0)
            for attr_match in IMG_SOURCE_ATTR_RE.finditer(tag):
                attr_name = str(attr_match.group("name") or "").lower()
                attr_value = attr_match.group(3) or attr_match.group(4) or ""
                if attr_name == "srcset":
                    for item in attr_value.split(","):
                        add(item.strip().split(" ", 1)[0])
                    continue
                add(attr_value)
        for match in CSS_URL_RE.finditer(text):
            add(match.group(2) or match.group(3) or "")

    def decoded_json_container(value: str) -> Any:
        text = unescape(value).strip()
        if not text or text[0] not in "{[" or len(text) > 200_000:
            return None
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return None

    def walk(value: Any, *, depth: int = 4, scan_values: bool = False) -> None:
        if depth < 0:
            return
        if isinstance(value, str):
            decoded = decoded_json_container(value)
            if isinstance(decoded, (dict, list)):
                walk(decoded, depth=depth - 1, scan_values=scan_values)
                return
            add_html_images(value)
            add(value)
            return
        if isinstance(value, list):
            marker = id(value)
            if marker in seen_nodes:
                return
            seen_nodes.add(marker)
            for item in value:
                walk(item, depth=depth - 1, scan_values=scan_values)
            return
        if not isinstance(value, dict):
            return

        marker = id(value)
        if marker in seen_nodes:
            return
        seen_nodes.add(marker)
        if not belongs_to_target(value):
            return
        if add_mapping_image(value) and looks_like_qzone_photo(value):
            return
        for key in IMAGE_HTML_KEYS:
            add_html_images(value.get(key))
        if depth <= 0:
            return
        for key in IMAGE_CONTAINER_KEYS:
            child = value.get(key)
            if child is None:
                continue
            walk(child, depth=depth - 1, scan_values=True)
        for key in IMAGE_NESTED_CONTAINER_KEYS:
            child = value.get(key)
            if child is None:
                continue
            if not isinstance(child, (dict, list)):
                continue
            walk(child, depth=depth - 1, scan_values=False)
        if scan_values:
            handled = (
                set(IMAGE_URL_KEYS)
                | set(IMAGE_HTML_KEYS)
                | set(IMAGE_CONTAINER_KEYS)
                | set(IMAGE_NESTED_CONTAINER_KEYS)
            )
            for key, child in value.items():
                if key in handled:
                    continue
                walk(child, depth=depth - 1, scan_values=True)

    for key in IMAGE_CONTAINER_KEYS:
        walk(payload.get(key), scan_values=True)
    for key in IMAGE_NESTED_CONTAINER_KEYS:
        child = payload.get(key)
        if isinstance(child, (dict, list)):
            walk(child, scan_values=False)
    for key in IMAGE_HTML_KEYS:
        add_html_images(payload.get(key))
    return candidates


def extract_images(payload: dict[str, Any], *, fid: str = "", hostuin: int = 0) -> list[str]:
    return [item.url for item in _extract_image_candidates(payload, fid=fid, hostuin=hostuin)]


def post_from_entry(
    entry: FeedEntry,
    *,
    detail: dict[str, Any] | None = None,
    local_id: int = 0,
    fallback_raw: dict[str, Any] | None = None,
) -> QzonePost:
    entry_raw = entry.raw if isinstance(entry.raw, dict) else {}
    detail_raw = detail if isinstance(detail, dict) else {}
    fallback = fallback_raw if isinstance(fallback_raw, dict) else {}
    raw = detail_raw or entry_raw
    comments = extract_comments(raw or {})
    images: list[str] = []
    seen_image_keys: set[str] = set()

    def split_embedded_feed_raw(source: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        embedded = source.get("_feed_raw")
        fallback_sources = [embedded] if isinstance(embedded, dict) else []
        if "_feed_raw" not in source:
            return source, fallback_sources
        primary = dict(source)
        primary.pop("_feed_raw", None)
        return primary, fallback_sources

    def append_images_from(source: dict[str, Any]) -> bool:
        if not source:
            return False
        before = len(images)
        for image in _extract_image_candidates(source, fid=entry.fid, hostuin=entry.hostuin):
            key = image.key or image.url
            if key in seen_image_keys:
                continue
            seen_image_keys.add(key)
            images.append(image.url)
        return len(images) > before

    # `_feed_raw` is a cached list snapshot used only as a media fallback.
    # If a detail payload already has media, scanning both sources shows the
    # same Qzone photos twice in WebUI detail cards.
    fallback_sources: list[dict[str, Any]] = []
    detail_source, embedded = split_embedded_feed_raw(detail_raw) if detail_raw else ({}, [])
    fallback_sources.extend(embedded)
    entry_source, embedded = split_embedded_feed_raw(entry_raw) if entry_raw else ({}, [])
    fallback_sources.extend(embedded)
    fallback_source, embedded = split_embedded_feed_raw(fallback) if fallback else ({}, [])
    fallback_sources.extend(embedded)

    append_images_from(detail_source)
    if not images:
        append_images_from(entry_source)
    if not images:
        append_images_from(fallback_source)
    if not images:
        seen_fallback_nodes: set[int] = set()
        for source in fallback_sources:
            marker = id(source)
            if marker in seen_fallback_nodes:
                continue
            seen_fallback_nodes.add(marker)
            append_images_from(source)
    nickname = (
        _clean_nickname(entry.nickname, hostuin=entry.hostuin)
        or extract_nickname(detail_raw, hostuin=entry.hostuin)
        or extract_nickname(entry_raw, hostuin=entry.hostuin)
        or extract_nickname(fallback, hostuin=entry.hostuin)
    )
    post_raw = dict(raw or {})
    if fallback and fallback is not raw:
        post_raw.setdefault("_feed_raw", fallback)
    return QzonePost(
        hostuin=entry.hostuin,
        fid=entry.fid,
        appid=entry.appid,
        summary=clean_qzone_text(entry.summary),
        nickname=nickname,
        created_at=entry.created_at,
        like_count=entry.like_count,
        comment_count=max(entry.comment_count, len(comments)),
        liked=entry.liked,
        images=images,
        comments=comments,
        busi_param=dict(entry.busi_param or {}),
        local_id=local_id,
        raw=post_raw,
    )
