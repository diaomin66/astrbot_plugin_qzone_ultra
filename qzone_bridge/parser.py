"""Parsing helpers for QQ空间 payloads."""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime, timedelta
from typing import Any

from .models import FeedEntry
from .social import extract_nickname
from .utils import entire_closing, extract_scripts, firstn, gtk, json_loads, truncate


COOKIE_SECRET_KEYS = ("p_skey", "skey", "pskey", "skey2")
COOKIE_GTK_KEYS = ("g_tk", "gtk", "bkn", "csrf_token")
COOKIE_KEY_ALIASES = {
    "p_skey": "p_skey",
    "pskey": "p_skey",
    "gtk": "g_tk",
    "bkn": "g_tk",
    "csrf_token": "g_tk",
}
FEED_CONTAINER_KEYS = ("feedpage", "main")
FEED_LIST_KEYS = ("vFeeds", "vfeeds", "msglist", "data", "feeds", "feedlist", "feedList")
FEED_CURSOR_KEYS = ("attachinfo", "attach_info", "attachInfo", "attach", "externparam", "res_attach")
FEED_HAS_MORE_KEYS = ("hasmore", "hasMore", "hasMoreFeeds", "has_more")
FEED_EXPLICIT_TIME_KEYS = (
    "time",
    "timeStr",
    "timestr",
    "time_text",
    "timeText",
    "time_desc",
    "timeDesc",
    "abstime",
    "created_time",
    "createdTime",
    "created_time_text",
    "createdTimeText",
    "created_at",
    "createdAt",
    "create_time",
    "createTime",
    "create_time_text",
    "createTimeText",
    "pubtime",
    "pub_time",
    "pubtimeText",
    "pub_time_text",
    "publish_time",
    "publishTime",
    "publish_time_text",
    "publishTimeText",
    "feedtime",
    "feedTime",
    "feedstime",
    "feedtimeText",
    "feedTimeText",
    "feedstimeText",
    "feed_time",
    "feedsTime",
    "feeds_time",
    "feedsTimeText",
    "feeds_time_text",
    "opertime",
    "operTime",
    "opertimeText",
    "operTimeText",
    "operation_time",
    "operationTime",
    "uploadtime",
    "uploadTime",
    "uploadtimeText",
    "uploadTimeText",
    "addtime",
    "addTime",
    "ctime",
)
FEED_GENERIC_TIME_KEYS = (
    "timestamp",
    "date",
)
FEED_TIME_CONTAINER_KEYS = (
    "data",
    "common",
    "cell_comm",
    "cellComm",
    "feed",
    "feedInfo",
    "feedinfo",
    "original",
    "summary",
    "operation",
    "cell",
    "cell_summary",
    "cellSummary",
    "msg",
    "message",
)
FEED_HTML_MARKUP_KEYS = (
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
HTML_TIME_ATTR_KEYS = (
    "data-time",
    "data-abstime",
    "data-pubtime",
    "data-timestamp",
    "data-created-at",
    "data-created-time",
    "time",
    "abstime",
    "pubtime",
    "timestamp",
)
MIN_QZONE_TIMESTAMP_SECONDS = 1_100_000_000
MAX_QZONE_TIMESTAMP_SECONDS = 4_102_444_800
HTML_ATTR_RE_TEMPLATE = r"""\b{name}\s*=\s*(?:(["'])(.*?)\1|([^\s"'<>`]+))"""
HTML_BREAK_RE = re.compile(r"<\s*br\s*/?\s*>", re.I)
HTML_BLOCK_RE = re.compile(r"</\s*(?:p|div|li|tr)\s*>", re.I)
HTML_TAG_RE = re.compile(r"<[^>]+>")
TEXT_TIMESTAMP_RE = re.compile(r"(?<!\d)(\d{10,13})(?!\d)")
TEXT_FULL_DATE_RE = re.compile(
    r"(?P<year>20\d{2}|19\d{2})\s*(?:年|[-/.])\s*"
    r"(?P<month>\d{1,2})\s*(?:月|[-/.])\s*"
    r"(?P<day>\d{1,2})(?!\d)\s*(?:日)?"
    r"(?:\s*(?P<hour>\d{1,2})[:：](?P<minute>\d{1,2})(?:[:：](?P<second>\d{1,2}))?)?"
)
TEXT_MONTH_DAY_RE = re.compile(
    r"(?<!\d)(?P<month>\d{1,2})\s*(?:月|[-/.])\s*"
    r"(?P<day>\d{1,2})(?!\d)\s*(?:日)?\s*"
    r"(?P<hour>\d{1,2})[:：](?P<minute>\d{1,2})(?:[:：](?P<second>\d{1,2}))?"
)
TEXT_RELATIVE_DAY_RE = re.compile(
    r"(?P<day>今天|昨天|前天)\s*"
    r"(?P<hour>\d{1,2})[:：](?P<minute>\d{1,2})(?:[:：](?P<second>\d{1,2}))?"
)
TEXT_RELATIVE_AGO_RE = re.compile(r"(?P<amount>\d{1,3})\s*(?P<unit>秒|分钟|小时|天)前")


def _dig(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        if key in current:
            current = current[key]
            continue
        return None
    return current


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = html_lib.unescape(value).strip()
    if not text or text[0] != "{" or len(text) > 200_000:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "".join(_text(item) for item in value)
    if isinstance(value, dict):
        for key in ("summary", "content", "text", "msg", "title"):
            if key in value:
                result = _text(value.get(key))
                if result:
                    return result
        return ""
    return str(value)


def _html_to_text(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    text = HTML_BREAK_RE.sub("\n", text)
    text = HTML_BLOCK_RE.sub("\n", text)
    text = HTML_TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def _html_attr(markup: Any, name: str) -> str:
    text = _text(markup)
    if not text:
        return ""
    pattern = HTML_ATTR_RE_TEMPLATE.format(name=re.escape(name))
    match = re.search(pattern, text, re.S | re.I)
    if not match:
        return ""
    return html_lib.unescape(match.group(2) or match.group(3) or "").strip()


def _html_markup_candidates(feed_item: dict[str, Any]) -> list[Any]:
    candidates: list[Any] = []
    seen: set[int] = set()

    def add(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, (str, dict, list)):
            marker = id(value)
            if marker in seen:
                return
            seen.add(marker)
            candidates.append(value)

    for key in FEED_HTML_MARKUP_KEYS:
        add(feed_item.get(key))

    for key in FEED_TIME_CONTAINER_KEYS:
        child = feed_item.get(key)
        if isinstance(child, dict):
            for html_key in FEED_HTML_MARKUP_KEYS:
                add(child.get(html_key))

    return candidates


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return default


def _normalize_timestamp_seconds(timestamp: int) -> int:
    if timestamp <= 0:
        return 0
    while timestamp > MAX_QZONE_TIMESTAMP_SECONDS and timestamp > 10_000_000_000:
        timestamp //= 1000
    if not (MIN_QZONE_TIMESTAMP_SECONDS <= timestamp <= MAX_QZONE_TIMESTAMP_SECONDS):
        return 0
    return timestamp


def _datetime_to_timestamp(value: datetime) -> int:
    try:
        return _normalize_timestamp_seconds(int(value.timestamp()))
    except (OSError, OverflowError, ValueError):
        return 0


def _timestamp_from_match(match: re.Match[str], *, default_year: int | None = None) -> int:
    year = int(match.groupdict().get("year") or default_year or datetime.now().year)
    month = int(match.group("month"))
    day = int(match.group("day"))
    hour = int(match.groupdict().get("hour") or 0)
    minute = int(match.groupdict().get("minute") or 0)
    second = int(match.groupdict().get("second") or 0)
    try:
        return _datetime_to_timestamp(datetime(year, month, day, hour, minute, second))
    except ValueError:
        return 0


def _timestamp_from_text(value: str) -> int:
    text = _html_to_text(value)
    if not text:
        text = html_lib.unescape(str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return 0

    for match in TEXT_TIMESTAMP_RE.finditer(text):
        timestamp = _normalize_timestamp_seconds(int(match.group(1)))
        if timestamp:
            return timestamp

    match = TEXT_FULL_DATE_RE.search(text)
    if match:
        timestamp = _timestamp_from_match(match)
        if timestamp:
            return timestamp

    match = TEXT_MONTH_DAY_RE.search(text)
    if match:
        timestamp = _timestamp_from_match(match, default_year=datetime.now().year)
        if timestamp:
            return timestamp

    match = TEXT_RELATIVE_DAY_RE.search(text)
    if match:
        days = {"今天": 0, "昨天": 1, "前天": 2}[match.group("day")]
        base = datetime.now() - timedelta(days=days)
        try:
            return _datetime_to_timestamp(
                datetime(
                    base.year,
                    base.month,
                    base.day,
                    int(match.group("hour")),
                    int(match.group("minute")),
                    int(match.groupdict().get("second") or 0),
                )
            )
        except ValueError:
            return 0

    match = TEXT_RELATIVE_AGO_RE.search(text)
    if match:
        amount = int(match.group("amount"))
        unit = match.group("unit")
        seconds = {
            "秒": amount,
            "分钟": amount * 60,
            "小时": amount * 3600,
            "天": amount * 86400,
        }[unit]
        return _datetime_to_timestamp(datetime.now() - timedelta(seconds=seconds))

    if "刚刚" in text:
        return _datetime_to_timestamp(datetime.now())

    return 0


def _timestamp_seconds(value: Any) -> int:
    timestamp = _normalize_timestamp_seconds(_int(value))
    if timestamp:
        return timestamp
    if isinstance(value, str):
        return _timestamp_from_text(value)
    return 0


def _iter_feed_time_sources(feed_item: dict[str, Any], common: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(value: Any, *, depth: int = 0) -> None:
        value = _json_mapping(value)
        if not value:
            return
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        sources.append(value)
        if depth <= 0:
            return
        for key in FEED_TIME_CONTAINER_KEYS:
            child = _json_mapping(value.get(key))
            if child:
                add(child, depth=depth - 1)

    add(common, depth=2)
    add(feed_item, depth=2)
    data = _json_mapping(feed_item.get("data"))
    if data:
        add(data, depth=2)
    return sources


def _created_at_from_feed_item(feed_item: dict[str, Any], common: dict[str, Any], html_markups: Any) -> int:
    sources = _iter_feed_time_sources(feed_item, common)
    for source in sources:
        for key in FEED_EXPLICIT_TIME_KEYS:
            timestamp = _timestamp_seconds(source.get(key))
            if timestamp:
                return timestamp
    for source in sources:
        for key in FEED_GENERIC_TIME_KEYS:
            timestamp = _timestamp_seconds(source.get(key))
            if timestamp:
                return timestamp
    markups = html_markups if isinstance(html_markups, list) else [html_markups]
    for markup in markups:
        for key in HTML_TIME_ATTR_KEYS:
            timestamp = _timestamp_seconds(_html_attr(markup, key))
            if timestamp:
                return timestamp
    return 0


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n", ""}:
            return False
    return bool(value)


def _clean_nickname_text(value: Any, *, hostuin: int = 0) -> str:
    text = _html_to_text(value).strip()
    if not text:
        return ""
    if hostuin and text == str(hostuin):
        return ""
    if re.fullmatch(r"\d{5,}", text):
        return ""
    return text


def parse_cookie_text(cookie_text: str) -> dict[str, str]:
    cookie_text = cookie_text.strip()
    if not cookie_text:
        return {}
    if cookie_text.startswith("{") or cookie_text.startswith("["):
        payload = json.loads(cookie_text)
        if isinstance(payload, dict):
            return normalize_cookie_fields(payload)
        raise ValueError("cookie JSON must be an object")

    cookie_text = cookie_text.replace("\n", ";")
    cookie_text = cookie_text.replace("\r", ";")
    if cookie_text.lower().startswith("cookie:"):
        cookie_text = cookie_text.split(":", 1)[1].strip()

    cookies: dict[str, str] = {}
    for part in cookie_text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"')
        if key:
            cookies[key] = value
    return normalize_cookie_fields(cookies)


def normalize_cookie_fields(cookies: dict[str, Any]) -> dict[str, str]:
    """Normalize OneBot/browser cookie aliases into Qzone-compatible keys."""

    normalized: dict[str, str] = {}
    for key, value in cookies.items():
        if value in (None, ""):
            continue
        original = str(key).strip()
        if not original:
            continue
        cookie_value = str(value).strip().strip('"')
        if not cookie_value:
            continue

        alias_key = original.lower().replace("-", "_")
        canonical = COOKIE_KEY_ALIASES.get(alias_key, original)
        normalized.setdefault(original, cookie_value)
        normalized.setdefault(canonical, cookie_value)

    if "uin" in normalized and "p_uin" not in normalized:
        normalized["p_uin"] = normalized["uin"]
    if "p_uin" in normalized and "uin" not in normalized:
        normalized["uin"] = normalized["p_uin"]
    return normalized


def cookie_gtk(cookies: dict[str, str]) -> int:
    """Return a usable g_tk from skey-like cookies or direct OneBot tokens."""

    normalized = normalize_cookie_fields(cookies)
    for key in COOKIE_SECRET_KEYS:
        value = normalized.get(key)
        if value:
            return gtk(value)
    for key in COOKIE_GTK_KEYS:
        value = normalized.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text.isdigit():
            return int(text)
    return 0


def normalize_uin(cookies: dict[str, str], override: int | None = None) -> int:
    if override:
        return int(override)
    candidates = [
        cookies.get("uin"),
        cookies.get("p_uin"),
        cookies.get("ptui_loginuin"),
        cookies.get("luin"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        cleaned = str(candidate).strip().lstrip("oO")
        if cleaned.isdigit():
            return int(cleaned)
    return 0


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def compute_unikey(appid: int, hostuin: int, fid: str) -> str:
    if appid == 311:
        return f"https://user.qzone.qq.com/{hostuin}/mood/{fid}"
    return f"https://user.qzone.qq.com/{hostuin}/app/{appid}/{fid}"


def topic_id(appid: int, hostuin: int, fid: str, created_at: int = 0) -> str:
    if appid == 311:
        return f"{hostuin}_{fid}__1"
    return f"{hostuin}_{created_at}"


def parse_index_html(html_text: str) -> dict[str, Any]:
    scripts = extract_scripts(html_text)
    script = firstn(scripts, lambda item: "shine0callback" in item)
    if not script:
        raise ValueError("index page script not found")

    match = re.search(r'window\.shine0callback.*return "([0-9a-f]+?)";', script)
    if not match:
        raise ValueError("qzonetoken not found")
    qzonetoken = match.group(1)

    match = re.search(r"var FrontPage =.*?data\s*:\s*\{", script, re.S)
    if not match:
        raise ValueError("index page data not found")
    data = script[match.end() - 1 : match.end() + entire_closing(script[match.end() - 1 :])]
    payload = json_loads(data)
    if not isinstance(payload, dict):
        raise ValueError("unexpected index payload")
    if isinstance(payload.get("data"), dict):
        payload["data"]["qzonetoken"] = qzonetoken
    else:
        payload["qzonetoken"] = qzonetoken
    return payload


def parse_profile_html(html_text: str) -> dict[str, Any]:
    scripts = extract_scripts(html_text)
    script = firstn(scripts, lambda item: "shine0callback" in item)
    if not script:
        raise ValueError("profile page script not found")

    match = re.search(r'window\.shine0callback.*return "([0-9a-f]+?)";', script)
    if not match:
        raise ValueError("profile qzonetoken not found")
    qzonetoken = match.group(1)

    match = re.search(r"var FrontPage =.*?data\s*:\s*\[", script, re.S)
    if not match:
        raise ValueError("profile page data not found")
    data = script[match.end() - 1 : match.end() + entire_closing(script[match.end() - 1 :], "[")]
    data = re.sub(r",,\]$", "]", data)
    payload = json_loads(data)
    if not isinstance(payload, list):
        raise ValueError("unexpected profile payload")
    if len(payload) < 2:
        raise ValueError("profile payload incomplete")
    info = unwrap_payload(payload[0]) if isinstance(payload[0], dict) else payload[0]
    feedpage = unwrap_payload(payload[1]) if isinstance(payload[1], dict) else payload[1]
    return {"info": info, "feedpage": feedpage, "qzonetoken": qzonetoken}


def unwrap_payload(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload and payload["data"] is not None:
        return payload["data"]
    return payload


def extract_hostuin(feed_item: dict[str, Any], default: int = 0) -> int:
    html_markup = _html_markup_candidates(feed_item)
    candidates = [
        feed_item.get("uin"),
        feed_item.get("hostuin"),
        feed_item.get("hostUin"),
        _html_attr(html_markup, "data-uin"),
        _html_attr(html_markup, "uin"),
        _dig(feed_item, "userinfo", "uin"),
        _dig(feed_item, "user", "uin"),
        _dig(feed_item, "userinfo", "user", "uin"),
        default,
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            value = int(candidate or 0)
        except Exception:
            continue
        if value:
            return value
    return default


def extract_fid(feed_item: dict[str, Any]) -> str:
    html_markup = _html_markup_candidates(feed_item)
    candidates = [
        feed_item.get("fid"),
        feed_item.get("tid"),
        feed_item.get("cellid"),
        feed_item.get("key"),
        feed_item.get("ugckey"),
        feed_item.get("ugcrightkey"),
        _html_attr(html_markup, "data-fid"),
        _html_attr(html_markup, "fid"),
        _html_attr(html_markup, "data-tid"),
        _html_attr(html_markup, "tid"),
        _html_attr(html_markup, "data-cellid"),
        _dig(feed_item, "id", "cellid"),
        _dig(feed_item, "common", "ugcrightkey"),
        _dig(feed_item, "common", "ugckey"),
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return ""


def extract_summary_text(feed_item: dict[str, Any]) -> str:
    candidates = [
        _text(feed_item.get("content")),
        _text(feed_item.get("con")),
        _dig(feed_item, "summary", "summary"),
        _text(feed_item.get("summary")),
        _dig(feed_item, "original", "summary", "summary"),
        _text(feed_item.get("text")),
        _html_to_text(feed_item.get("html")),
    ]
    for key in FEED_HTML_MARKUP_KEYS:
        if key != "html":
            candidates.append(_html_to_text(feed_item.get(key)))
    for candidate in candidates:
        if candidate:
            return truncate(str(candidate).strip(), 500)
    return ""


def _context_owner_nickname(context: Any, *, hostuin: int = 0) -> str:
    if not isinstance(context, dict):
        return ""
    nickname = extract_nickname(context, hostuin=hostuin)
    if nickname:
        return nickname
    for key in ("payload", "feedpage", "data", "main"):
        value = context.get(key)
        if isinstance(value, dict):
            nickname = _context_owner_nickname(value, hostuin=hostuin)
            if nickname:
                return nickname
    for key in ("info", "ownerInfo", "hostInfo", "profileInfo", "profile", "owner"):
        value = context.get(key)
        if isinstance(value, dict):
            nickname = extract_nickname({"owner": value}, hostuin=hostuin)
            if nickname:
                return nickname
    return ""


def extract_feed_entry(
    feed_item: dict[str, Any],
    *,
    default_hostuin: int = 0,
    nickname_context: dict[str, Any] | None = None,
) -> FeedEntry:
    common = _json_mapping(feed_item.get("common")) or _json_mapping(feed_item.get("cell_comm")) or {}
    userinfo = _json_mapping(feed_item.get("userinfo")) or _json_mapping(feed_item.get("user")) or {}
    like = _json_mapping(feed_item.get("like")) or {}
    comment = _json_mapping(feed_item.get("comment")) or {}
    operation = _json_mapping(feed_item.get("operation")) or {}
    original = _json_mapping(feed_item.get("original")) or {}
    html_markups = _html_markup_candidates(feed_item)
    html_markup = html_markups[0] if html_markups else None

    hostuin = extract_hostuin(feed_item, default_hostuin)
    appid = _int(
        common.get("appid")
        or feed_item.get("appid")
        or _html_attr(html_markup, "data-appid")
        or 311,
        311,
    )
    fid = extract_fid(feed_item)
    created_at = _created_at_from_feed_item(feed_item, common, html_markups)
    summary = extract_summary_text(feed_item)
    if not summary:
        summary = extract_summary_text(original)

    curkey = str(
        feed_item.get("curkey")
        or feed_item.get("curlikekey")
        or common.get("curkey")
        or common.get("curlikekey")
        or _html_attr(html_markup, "data-curkey")
        or _html_attr(html_markup, "curkey")
        or compute_unikey(appid, hostuin, fid)
        or ""
    )
    unikey = (
        feed_item.get("unikey")
        or feed_item.get("unlikekey")
        or common.get("unikey")
        or common.get("unlikekey")
        or _html_attr(html_markup, "data-unikey")
        or _html_attr(html_markup, "unikey")
        or compute_unikey(appid, hostuin, fid)
    )
    topic = topic_id(appid, hostuin, fid, created_at)
    direct_nickname = _clean_nickname_text(
        feed_item.get("name")
        or feed_item.get("nickname")
        or userinfo.get("nickname")
        or userinfo.get("name")
        or "",
        hostuin=hostuin,
    )
    nickname = (
        extract_nickname(feed_item, hostuin=hostuin)
        or direct_nickname
        or _context_owner_nickname(nickname_context, hostuin=hostuin)
    )
    like_count = _int(
        like.get("num")
        or like.get("likeNum")
        or like.get("count")
        or feed_item.get("likeNum")
        or feed_item.get("likenum")
        or feed_item.get("like_num")
        or 0
    )
    raw_comments = feed_item.get("commentlist")
    comment_count = _int(
        comment.get("num") or comment.get("commentcount") or feed_item.get("cmtnum") or feed_item.get("commentnum") or 0
    )
    if not comment_count and isinstance(raw_comments, list):
        comment_count = len(raw_comments)
    liked = _bool(
        like.get("isliked")
        if "isliked" in like
        else like.get("ismylike")
        if "ismylike" in like
        else like.get("isLike")
        if "isLike" in like
        else like.get("islike")
        if "islike" in like
        else feed_item.get("isliked")
        if "isliked" in feed_item
        else feed_item.get("liked")
    )
    busi_param = operation.get("busi_param") or {}
    if not isinstance(busi_param, dict):
        busi_param = {}

    return FeedEntry(
        hostuin=hostuin,
        fid=fid,
        appid=appid,
        summary=summary,
        nickname=nickname,
        created_at=created_at,
        like_count=like_count,
        comment_count=comment_count,
        liked=liked,
        curkey=curkey,
        unikey=unikey,
        busi_param=busi_param,
        topic_id=topic,
        raw=feed_item,
    )


def _looks_like_feed_page(value: dict[str, Any]) -> bool:
    return any(key in value for key in (*FEED_LIST_KEYS, *FEED_CURSOR_KEYS, *FEED_HAS_MORE_KEYS))


def normalize_feed_page(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in FEED_CONTAINER_KEYS:
        value = payload.get(key)
        if isinstance(value, dict):
            return value

    data = payload.get("data")
    if isinstance(data, dict):
        for key in FEED_CONTAINER_KEYS:
            value = data.get(key)
            if isinstance(value, dict):
                return value
        if _looks_like_feed_page(data):
            return data

    return payload


def extract_raw_feeds(feedpage: dict[str, Any]) -> list[Any]:
    if not isinstance(feedpage, dict):
        return []
    raw_feeds: Any = []
    for key in FEED_LIST_KEYS:
        value = feedpage.get(key)
        if value:
            raw_feeds = value
            break
    if isinstance(raw_feeds, dict):
        raw_feeds = extract_raw_feeds(raw_feeds)
    if not isinstance(raw_feeds, list):
        return []
    return raw_feeds


def feed_page_has_more(feedpage: dict[str, Any]) -> bool:
    for key in FEED_HAS_MORE_KEYS:
        if key not in feedpage:
            continue
        return _bool(feedpage.get(key))
    return False


def feed_page_cursor(feedpage: dict[str, Any]) -> str:
    for key in FEED_CURSOR_KEYS:
        value = feedpage.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def extract_feed_page(payload: dict[str, Any], *, default_hostuin: int = 0) -> tuple[dict[str, Any], list[FeedEntry]]:
    source_payload = payload if isinstance(payload, dict) else {}
    feedpage = normalize_feed_page(payload)
    if not isinstance(feedpage, dict):
        return {}, []
    nickname_context = {"payload": source_payload, "feedpage": feedpage}
    raw_feeds = extract_raw_feeds(feedpage)
    items = [
        extract_feed_entry(item, default_hostuin=default_hostuin, nickname_context=nickname_context)
        for item in raw_feeds
        if isinstance(item, dict)
    ]
    return feedpage, items
