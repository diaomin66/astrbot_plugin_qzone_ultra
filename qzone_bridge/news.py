"""Google News RSS support for scheduled Qzone news posts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus, urlparse
from xml.etree import ElementTree

import httpx

from .errors import QzoneParseError, QzoneRequestError

GOOGLE_NEWS_CHINA_RSS = "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
GOOGLE_NEWS_WORLD_RSS = "https://news.google.com/rss/headlines/section/topic/WORLD?hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
GOOGLE_NEWS_SEARCH_RSS = "https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
NEWS_SCOPE_ALIASES = {
    "china": ("china",),
    "cn": ("china",),
    "zh": ("china",),
    "中国": ("china",),
    "国内": ("china",),
    "中国新闻": ("china",),
    "国内新闻": ("china",),
    "world": ("world",),
    "international": ("world",),
    "global": ("world",),
    "国际": ("world",),
    "全球": ("world",),
    "国际新闻": ("world",),
    "世界": ("world",),
    "全球新闻": ("world",),
    "mixed": ("china", "world"),
    "mix": ("china", "world"),
    "all": ("china", "world"),
    "混合": ("china", "world"),
    "混合新闻": ("china", "world"),
}


@dataclass(slots=True)
class NewsItem:
    title: str
    source: str = ""
    link: str = ""
    published_at: int = 0
    scope: str = ""
    item_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_news_scopes(values: Any) -> list[str]:
    if values is None or values == "":
        raw_items: list[Any] = []
    elif isinstance(values, str):
        raw_items = [item.strip() for item in re.split(r"[,，\s]+", values) if item.strip()]
    elif isinstance(values, (list, tuple, set)):
        raw_items = list(values)
    else:
        raw_items = [values]

    scopes: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        aliases = NEWS_SCOPE_ALIASES.get(str(item or "").strip().lower(), ())
        for scope in aliases:
            if scope not in seen:
                seen.add(scope)
                scopes.append(scope)
    return scopes or ["china"]


def google_news_rss_urls(
    *,
    scopes: Any = None,
    keywords: list[str] | None = None,
    custom_urls: list[str] | None = None,
) -> list[tuple[str, str]]:
    urls: list[tuple[str, str]] = []
    for scope in normalize_news_scopes(scopes):
        if scope == "world":
            urls.append((scope, GOOGLE_NEWS_WORLD_RSS))
        else:
            urls.append((scope, GOOGLE_NEWS_CHINA_RSS))
    for keyword in keywords or []:
        text = str(keyword or "").strip()
        if text:
            urls.append((f"keyword:{text}", GOOGLE_NEWS_SEARCH_RSS.format(query=quote_plus(text))))
    for url in custom_urls or []:
        text = str(url or "").strip()
        if text:
            urls.append(("custom", text))
    return _dedupe_urls(urls)


def is_google_news_rss_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host == "news.google.com" and parsed.path.startswith("/rss")


def _dedupe_urls(urls: list[tuple[str, str]]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for scope, url in urls:
        if url in seen:
            continue
        seen.add(url)
        result.append((scope, url))
    return result


def clean_google_news_title(title: str, source: str = "") -> str:
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    source_text = str(source or "").strip()
    if source_text and text.endswith(f" - {source_text}"):
        text = text[: -len(source_text) - 3].rstrip()
    return text


def parse_rss_datetime(value: str) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def parse_google_news_rss(xml_text: str, *, scope: str = "") -> list[NewsItem]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise QzoneParseError("Google News RSS 解析失败") from exc
    items: list[NewsItem] = []
    for item in root.findall(".//channel/item"):
        title = _child_text(item, "title")
        source = _child_text(item, "source")
        title = clean_google_news_title(title, source)
        link = _child_text(item, "link")
        published_at = parse_rss_datetime(_child_text(item, "pubDate"))
        if not title:
            continue
        items.append(
            NewsItem(
                title=title,
                source=source,
                link=link,
                published_at=published_at,
                scope=scope,
                item_id=news_item_id(title=title, source=source, link=link),
            )
        )
    return items


def news_item_id(*, title: str, source: str = "", link: str = "") -> str:
    key = "\n".join([str(link or "").strip(), str(source or "").strip(), str(title or "").strip()])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def filter_recent_news(items: list[NewsItem], *, recency_hours: int, now: datetime | None = None) -> list[NewsItem]:
    hours = int(recency_hours or 0)
    if hours <= 0:
        return list(items)
    current = now or datetime.now(timezone.utc)
    threshold = int(current.timestamp()) - hours * 3600
    return [item for item in items if not item.published_at or item.published_at >= threshold]


def merge_news_items(items: list[NewsItem], *, limit: int = 12, seen_ids: set[str] | None = None) -> list[NewsItem]:
    seen = set(seen_ids or set())
    result: list[NewsItem] = []
    for item in sorted(items, key=lambda entry: entry.published_at, reverse=True):
        key = item.item_id or news_item_id(title=item.title, source=item.source, link=item.link)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= max(1, int(limit or 12)):
            break
    return result


def is_news_copy_like(text: str, items: list[NewsItem], *, threshold: float = 0.86) -> bool:
    body = _normalize_copy_text(text)
    if len(body) < 8:
        return False
    for item in items:
        title = _normalize_copy_text(item.title)
        if len(title) < 8:
            continue
        if title in body or body in title:
            return True
        if SequenceMatcher(None, body, title).ratio() >= threshold:
            return True
    return False


class GoogleNewsRSSClient:
    def __init__(self, *, timeout: float = 10.0, user_agent: str = "", trust_env: bool = True) -> None:
        self.timeout = float(timeout or 10.0)
        self.user_agent = user_agent or "Mozilla/5.0"
        self.trust_env = bool(trust_env)

    async def fetch_items(self, urls: list[tuple[str, str]]) -> list[NewsItem]:
        items: list[NewsItem] = []
        errors: list[Exception] = []
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            trust_env=self.trust_env,
            headers={"User-Agent": self.user_agent},
        ) as client:
            for scope, url in urls:
                if not is_google_news_rss_url(url):
                    raise QzoneParseError("自定义新闻 RSS 地址必须是 https://news.google.com/rss 开头。")
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    items.extend(parse_google_news_rss(response.text, scope=scope))
                except (httpx.HTTPError, QzoneParseError) as exc:
                    errors.append(exc)
        if not items and errors:
            raise QzoneRequestError("Google News RSS 获取失败", detail={"errors": [str(exc) for exc in errors[:3]]})
        return items


def _child_text(item: ElementTree.Element, name: str) -> str:
    child = item.find(name)
    return "".join(child.itertext()).strip() if child is not None else ""


def _normalize_copy_text(text: str) -> str:
    return re.sub(r"[\W_]+", "", str(text or "").lower(), flags=re.UNICODE)
