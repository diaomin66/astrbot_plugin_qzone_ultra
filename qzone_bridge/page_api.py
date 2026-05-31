"""WebUI Page API adapter for the Qzone plugin.

This module keeps the AstrBot Pages surface separate from chat commands and
LLM tools. It returns browser-friendly, redacted payloads and delegates all
real Qzone operations to the existing controller/service layer.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import mimetypes
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable
from urllib.parse import unquote_to_bytes

from .errors import DaemonUnavailableError, QzoneBridgeError, QzoneNeedsRebind, QzoneParseError
from .media import (
    QZONE_IMAGE_SUFFIXES,
    QZONE_MAX_IMAGES,
    QZONE_MIN_IMAGE_SIDE,
    guess_mime_type,
    image_dimensions_from_bytes,
    is_supported_image,
    looks_like_supported_image_bytes,
)
from .models import FeedEntry
from .social import QzoneComment, QzonePost, post_from_entry
from .utils import truncate


PAGE_UPLOAD_MAX_BYTES: int | None = None
PAGE_DEFAULT_LIMIT = 10
PAGE_MAX_LIMIT = 30
PAGE_DETAIL_TIMEOUT_SECONDS = 8.0
PAGE_STATUS_TIMEOUT_SECONDS = 2.0


@dataclass(slots=True)
class PagePostRef:
    hostuin: int
    fid: str
    appid: int = 311
    curkey: str = ""
    unikey: str = ""
    busi_param: dict[str, Any] = field(default_factory=dict)
    snapshot: dict[str, Any] = field(default_factory=dict)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _clean_text(value: Any, limit: int = 500) -> str:
    return truncate(str(value or "").strip(), limit)


def _is_generic_nickname(value: Any, uin: int = 0) -> bool:
    name = str(value or "").strip()
    compact = name.replace(" ", "")
    return (
        not name
        or name in {"QQ空间用户", "QQ 空间用户", "用户"}
        or (uin and name == str(uin))
        or (len(name) >= 5 and name.isdigit())
        or (compact.lower().startswith("qq") and compact[2:].isdigit())
    )


def _success(data: Any = None, *, message: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True, "data": data if data is not None else {}}
    if message:
        payload["message"] = message
    return payload


def page_error_payload(exc: Exception) -> tuple[dict[str, Any], int]:
    code = getattr(exc, "code", "PAGE_ERROR") or "PAGE_ERROR"
    message = getattr(exc, "message", str(exc)) or "操作失败，请稍后再试。"
    status = 400
    if isinstance(exc, PermissionError):
        code = "PAGE_PERMISSION_DENIED"
        message = "本地文件或进程权限被系统拒绝，请重启 AstrBot 后再试。"
        status = 503
    elif isinstance(exc, QzoneNeedsRebind):
        status = 409
    elif isinstance(exc, DaemonUnavailableError):
        status = 503
    elif isinstance(exc, QzoneBridgeError):
        status = 400
    clean_message = _clean_text(message, 180)
    clean_code = str(code)
    return (
        {
            "ok": False,
            "code": clean_code,
            "message": clean_message,
            "error": {
                "code": clean_code,
                "message": clean_message,
            },
        },
        status,
    )


class QzonePageApi:
    def __init__(
        self,
        *,
        controller: Any,
        post_service_factory: Callable[[], Any],
        settings: Any,
        status_provider: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        preload_scheduler: Callable[[str], None] | None = None,
    ):
        self.controller = controller
        self.post_service_factory = post_service_factory
        self.settings = settings
        self.status_provider = status_provider
        self.preload_scheduler = preload_scheduler
        self._refs_by_id: dict[str, PagePostRef] = {}
        self._ids_by_ref: dict[tuple[int, str, int], str] = {}
        self._feed_seen_refs: dict[tuple[str, int], set[tuple[int, str, int]]] = {}
        self._feed_emitted_cursors: dict[tuple[str, int], set[str]] = {}
        self._uploaded_media_by_id: dict[str, dict[str, Any]] = {}

    @property
    def max_feed_limit(self) -> int:
        configured = _to_int(getattr(self.settings, "max_feed_limit", 20), 20)
        return max(1, min(configured, PAGE_MAX_LIMIT))

    async def _status(self, *, recover: bool = False) -> dict[str, Any]:
        if recover and self.status_provider is not None:
            try:
                status = await asyncio.wait_for(
                    self.status_provider(),
                    timeout=max(0.001, float(PAGE_STATUS_TIMEOUT_SECONDS)),
                )
                if isinstance(status, dict):
                    return status
            except (asyncio.TimeoutError, Exception):
                # Page recovery is best-effort. Fall back to the cheap local
                # status snapshot so the WebUI can still render a useful state.
                pass
        status = await self.controller.get_status(probe_daemon=False)
        return status if isinstance(status, dict) else {}

    def _schedule_preload(self, trigger: str) -> None:
        if self.preload_scheduler is not None:
            try:
                self.preload_scheduler(trigger)
            except Exception:
                pass

    async def _ensure_ready(self) -> dict[str, Any]:
        status = await self._status(recover=True)
        if status.get("needs_rebind") or not _to_int(status.get("cookie_count"), 0):
            raise QzoneNeedsRebind()
        if status.get("daemon_state") != "ready":
            raise DaemonUnavailableError("Qzone daemon is not ready", detail={"daemon_state": status.get("daemon_state")})
        return status

    async def _ensure_cookie_bound(self) -> dict[str, Any]:
        status = await self._status(recover=False)
        if status.get("needs_rebind") or not _to_int(status.get("cookie_count"), 0):
            raise QzoneNeedsRebind()
        return status

    def _limit(self, value: Any, default: int = PAGE_DEFAULT_LIMIT) -> int:
        limit = _to_int(value, default)
        if limit <= 0:
            limit = default
        return max(1, min(limit, self.max_feed_limit))

    def _post_ref_id(
        self,
        hostuin: int,
        fid: str,
        appid: int = 311,
        *,
        curkey: str = "",
        unikey: str = "",
        busi_param: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> str:
        ref = PagePostRef(
            hostuin=int(hostuin or 0),
            fid=str(fid or ""),
            appid=int(appid or 311),
            curkey=str(curkey or ""),
            unikey=str(unikey or ""),
            busi_param=dict(busi_param or {}),
            snapshot=dict(snapshot or {}),
        )
        if not ref.hostuin or not ref.fid:
            raise QzoneParseError("说说引用无效，请刷新页面后重试。")
        key = (ref.hostuin, ref.fid, ref.appid)
        existing = self._ids_by_ref.get(key)
        if existing:
            existing_ref = self._refs_by_id.get(existing)
            if existing_ref is not None:
                if ref.curkey:
                    existing_ref.curkey = ref.curkey
                if ref.unikey:
                    existing_ref.unikey = ref.unikey
                if ref.busi_param:
                    existing_ref.busi_param = ref.busi_param
                if ref.snapshot:
                    existing_ref.snapshot = ref.snapshot
            return existing
        token = "post_" + secrets.token_urlsafe(18)
        while token in self._refs_by_id:
            token = "post_" + secrets.token_urlsafe(18)
        self._refs_by_id[token] = ref
        self._ids_by_ref[key] = token
        return token

    def _decode_post_ref(self, value: Any) -> PagePostRef:
        token = str(value or "").strip()
        if not token:
            raise QzoneParseError("缺少说说引用。")
        ref = self._refs_by_id.get(token)
        if ref is None:
            raise QzoneParseError("说说引用已过期，请刷新页面后重试。")
        return ref

    @staticmethod
    def _feed_key(scope: str, hostuin: int) -> tuple[str, int]:
        return (str(scope or "auto").strip().lower() or "auto", int(hostuin or 0))

    @staticmethod
    def _entry_ref(entry: FeedEntry) -> tuple[int, str, int]:
        return (int(entry.hostuin or 0), str(entry.fid or ""), int(entry.appid or 311))

    @staticmethod
    def _safe_upload_filename(filename: Any) -> str:
        name = Path(str(filename or "image.jpg")).name.strip() or "image.jpg"
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:96].strip("._-")
        return name or "image.jpg"

    def _page_upload_dir(self) -> Path | None:
        data_dir = getattr(self.controller, "data_dir", None)
        if not data_dir:
            return None
        upload_dir = Path(data_dir) / "page_uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        return upload_dir

    @staticmethod
    def _cleanup_upload_dir(upload_dir: Path, *, max_age_seconds: int = 24 * 60 * 60) -> None:
        expires_before = time.time() - max_age_seconds
        for path in upload_dir.glob("upload_*"):
            try:
                if path.is_file() and path.stat().st_mtime < expires_before:
                    path.unlink()
            except OSError:
                pass

    @staticmethod
    def _browser_media_source(item: dict[str, Any]) -> str:
        source = str(item.get("source") or item.get("data_url") or item.get("url") or "")
        if source.startswith(("http://", "https://", "data:", "base64://")):
            return source
        return ""

    def _page_media_item(self, item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise QzoneParseError("图片列表格式不正确。")
        payload = dict(item)
        upload_id = str(payload.get("upload_id") or "").strip()
        if upload_id:
            stored = self._uploaded_media_by_id.get(upload_id)
            if stored is None:
                raise QzoneParseError("图片上传缓存已过期，请重新选择图片后再发布。")
            return dict(stored)
        payload.pop("preview_url", None)
        payload.pop("previewUrl", None)
        if payload.get("trusted_local"):
            payload.pop("trusted_local", None)
        if not payload.get("source"):
            for key in ("data_url", "url", "path", "file"):
                if payload.get(key):
                    payload["source"] = payload[key]
                    break
        payload.setdefault("kind", "image")
        if not payload.get("name") and payload.get("filename"):
            payload["name"] = payload["filename"]
        if not payload.get("source"):
            raise QzoneParseError("图片缺少可上传的数据。")
        if not is_supported_image(payload):
            raise QzoneParseError("只支持上传图片文件。")
        return payload

    def _page_media_list(self, value: Any) -> list[dict[str, Any]]:
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise QzoneParseError("图片列表格式不正确。")
        media = [self._page_media_item(item) for item in value]
        if len(media) > QZONE_MAX_IMAGES:
            raise QzoneParseError(f"QQ空间一次最多只能上传 {QZONE_MAX_IMAGES} 张图片。")
        return media

    @staticmethod
    def _comment_payload(
        comment: QzoneComment,
        index: int,
        *,
        login_author: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        author = {
            "uin": comment.uin,
            "nickname": comment.nickname or "QQ空间用户",
        }
        if login_author and comment.uin and comment.uin == _to_int(login_author.get("uin"), 0):
            login_nickname = str(login_author.get("nickname") or "").strip()
            if login_nickname and _is_generic_nickname(author.get("nickname"), comment.uin):
                author["nickname"] = login_nickname
            login_avatar = str(login_author.get("avatar") or "").strip()
            if login_avatar:
                author["avatar"] = login_avatar
        return {
            "id": comment.commentid,
            "index": index,
            "author": author,
            "content": comment.content,
            "created_at": comment.created_at,
            "parent_id": comment.parent_id,
            "can_reply": bool(comment.commentid and comment.uin),
        }

    @staticmethod
    def _entry_to_post(entry: FeedEntry, index: int) -> QzonePost:
        return post_from_entry(entry, local_id=index)

    @staticmethod
    def _entry_from_ref_snapshot(ref: PagePostRef) -> FeedEntry | None:
        if not ref.snapshot:
            return None
        try:
            return FeedEntry(**ref.snapshot)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _hydrate_entry_ref(entry: FeedEntry, ref: PagePostRef) -> FeedEntry:
        if ref.curkey and not entry.curkey:
            entry.curkey = ref.curkey
        if ref.unikey and not entry.unikey:
            entry.unikey = ref.unikey
        if ref.busi_param and not entry.busi_param:
            entry.busi_param = dict(ref.busi_param)
        return entry

    @staticmethod
    def _login_author_payload(status: dict[str, Any]) -> dict[str, Any]:
        login_uin = _to_int(status.get("login_uin") or status.get("uin"), 0)
        return {
            "uin": login_uin,
            "nickname": (
                status.get("login_nickname")
                or status.get("nickname")
                or status.get("publisher_nickname")
                or ""
            ),
            "avatar": status.get("login_avatar") or status.get("avatar") or "",
        }

    def _post_payload(
        self,
        post: QzonePost,
        *,
        login_uin: int = 0,
        login_author: dict[str, Any] | None = None,
        include_comments: bool = False,
        ref_entry: FeedEntry | None = None,
    ) -> dict[str, Any]:
        author = {
            "uin": post.hostuin,
            "nickname": post.nickname or "QQ空间用户",
        }
        if login_author and login_uin and post.hostuin == login_uin:
            login_nickname = str(login_author.get("nickname") or "").strip()
            if login_nickname and _is_generic_nickname(author.get("nickname"), post.hostuin):
                author["nickname"] = login_nickname
            login_avatar = str(login_author.get("avatar") or "").strip()
            if login_avatar:
                author["avatar"] = login_avatar
        payload = {
            "id": self._post_ref_id(
                post.hostuin,
                post.fid,
                post.appid,
                curkey=getattr(ref_entry, "curkey", ""),
                unikey=getattr(ref_entry, "unikey", ""),
                busi_param=getattr(ref_entry, "busi_param", None) or post.busi_param,
                snapshot=asdict(ref_entry) if ref_entry is not None else {},
            ),
            "local_id": post.local_id,
            "author": author,
            "content": post.summary,
            "created_at": post.created_at,
            "appid": post.appid,
            "stats": {
                "likes": post.like_count,
                "comments": post.comment_count,
            },
            "liked": bool(post.liked),
            "images": list(post.images[:9]),
            "can_comment": bool(post.fid and post.hostuin),
            "can_like": bool(post.fid and post.hostuin),
            "can_delete": bool(login_uin and post.hostuin == login_uin),
        }
        if include_comments:
            payload["comments"] = [
                QzonePageApi._comment_payload(comment, index, login_author=login_author)
                for index, comment in enumerate(post.comments, start=1)
            ]
        return payload

    async def status(self) -> dict[str, Any]:
        self._schedule_preload("page status")
        status_error = ""
        try:
            status = await self._status(recover=True)
        except Exception as exc:
            status = {}
            status_error = str(exc) or type(exc).__name__
        login_uin = _to_int(status.get("login_uin") or status.get("uin"), 0)
        data = {
            "daemon": {
                "state": status.get("daemon_state") or "unknown",
                "port": _to_int(status.get("daemon_port"), 0),
                "version": status.get("daemon_version") or status.get("version") or "",
                "error": _clean_text(status_error or status.get("daemon_start_error"), 180),
            },
            "login": {
                "bound": bool(_to_int(status.get("cookie_count"), 0) and not status.get("needs_rebind")),
                "uin": login_uin,
                "nickname": status.get("login_nickname") or status.get("nickname") or "",
                "avatar": status.get("login_avatar") or status.get("avatar") or "",
                "needs_rebind": bool(status.get("needs_rebind")),
            },
            "limits": {
                "feed": self.max_feed_limit,
                "images": QZONE_MAX_IMAGES,
                "upload_bytes": None,
            },
        }
        return _success(data)

    async def feed(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._schedule_preload("page feed")
        status = await self._ensure_cookie_bound()
        params = params or {}
        hostuin = _to_int(params.get("hostuin") or params.get("target_uin"), 0)
        limit = self._limit(params.get("limit"))
        cursor = str(params.get("cursor") or "")
        scope = str(params.get("scope") or "").strip().lower()
        if scope == "friends":
            scope = "active"
        feed_key = self._feed_key(scope, hostuin)
        if not cursor:
            self._feed_seen_refs[feed_key] = set()
            self._feed_emitted_cursors[feed_key] = set()
        payload = await self.controller.list_feeds(
            hostuin=hostuin,
            limit=limit,
            cursor=cursor,
            scope=scope,
            record_recent=False,
        )
        login_uin = _to_int(status.get("login_uin") or status.get("uin"), 0)
        login_author = self._login_author_payload(status)
        posts: list[dict[str, Any]] = []
        seen_refs = self._feed_seen_refs.setdefault(feed_key, set())
        for index, item in enumerate(payload.get("items") or [], start=1):
            if not isinstance(item, dict):
                continue
            entry = FeedEntry(**item)
            if not entry.hostuin or not entry.fid:
                continue
            entry_ref = self._entry_ref(entry)
            if entry_ref in seen_refs:
                continue
            seen_refs.add(entry_ref)
            post = self._entry_to_post(entry, index)
            posts.append(
                self._post_payload(
                    post,
                    login_uin=login_uin,
                    login_author=login_author,
                    ref_entry=entry,
                )
            )
        next_cursor = str(payload.get("cursor") or "")
        emitted_cursors = self._feed_emitted_cursors.setdefault(feed_key, set())
        has_more = bool(payload.get("has_more")) and bool(next_cursor)
        if next_cursor and next_cursor in emitted_cursors:
            has_more = False
        if next_cursor:
            emitted_cursors.add(next_cursor)
        if cursor and not posts and next_cursor in emitted_cursors:
            has_more = False
        return _success(
            {
                "scope": payload.get("scope") or scope or "auto",
                "hostuin": _to_int(payload.get("hostuin"), hostuin),
                "items": posts,
                "cursor": next_cursor,
                "has_more": has_more,
                "count": len(posts),
            }
        )

    async def detail(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._schedule_preload("page detail")
        status = await self._ensure_cookie_bound()
        params = params or {}
        ref = self._decode_post_ref(params.get("id") or params.get("post_id"))
        busi_param = json.dumps(ref.busi_param, ensure_ascii=False, separators=(",", ":")) if ref.busi_param else ""
        try:
            payload = await asyncio.wait_for(
                self.controller.detail_feed(
                    hostuin=ref.hostuin,
                    fid=ref.fid,
                    appid=ref.appid,
                    busi_param=busi_param,
                ),
                timeout=max(0.001, float(PAGE_DETAIL_TIMEOUT_SECONDS)),
            )
            partial = False
        except (asyncio.TimeoutError, QzoneBridgeError) as exc:
            if isinstance(exc, QzoneNeedsRebind):
                raise
            entry = self._entry_from_ref_snapshot(ref)
            if entry is None:
                raise
            entry = self._hydrate_entry_ref(entry, ref)
            post = post_from_entry(entry, local_id=1)
            login_uin = _to_int(status.get("login_uin") or status.get("uin"), 0)
            return _success(
                {
                    "post": self._post_payload(
                        post,
                        login_uin=login_uin,
                        login_author=self._login_author_payload(status),
                        include_comments=True,
                        ref_entry=entry,
                    ),
                    "partial": True,
                    "message": "详情接口响应较慢，已先显示缓存内容。",
                },
                message="详情接口响应较慢，已先显示缓存内容。",
            )
        entry_data = payload.get("entry") if isinstance(payload, dict) else None
        entry = (
            FeedEntry(**entry_data)
            if isinstance(entry_data, dict)
            else FeedEntry(hostuin=ref.hostuin, fid=ref.fid, appid=ref.appid, summary="")
        )
        entry = self._hydrate_entry_ref(entry, ref)
        post = post_from_entry(entry, detail=payload.get("raw") if isinstance(payload, dict) else None, local_id=1)
        comments = []
        for item in payload.get("comments") or []:
            if not isinstance(item, dict):
                continue
            comments.append(
                QzoneComment(
                    commentid=str(item.get("commentid") or ""),
                    uin=_to_int(item.get("uin"), 0),
                    nickname=str(item.get("nickname") or ""),
                    content=str(item.get("content") or ""),
                    created_at=_to_int(item.get("created_at") or item.get("date"), 0),
                    parent_id=str(item.get("parent_id") or ""),
                )
            )
        if comments:
            post.comments = comments
            post.comment_count = max(post.comment_count, len(comments))
        login_uin = _to_int(status.get("login_uin") or status.get("uin"), 0)
        return _success(
            {
                "post": self._post_payload(
                    post,
                    login_uin=login_uin,
                    login_author=self._login_author_payload(status),
                    include_comments=True,
                    ref_entry=entry,
                ),
                "partial": partial,
            }
        )

    async def publish(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self._schedule_preload("page publish")
        status = await self._ensure_cookie_bound()
        body = body or {}
        content = str(body.get("content") or "")
        media = self._page_media_list(body.get("media"))
        if not content.strip() and not media:
            raise QzoneParseError("说说内容或图片不能为空。")
        if not isinstance(media, list):
            raise QzoneParseError("图片列表格式不正确。")
        if len(media) > QZONE_MAX_IMAGES:
            raise QzoneParseError(f"QQ空间一次最多只能上传 {QZONE_MAX_IMAGES} 张图片。")
        payload = await self.controller.publish_post(
            content=content,
            sync_weibo=_to_bool(body.get("sync_weibo"), False),
            media=media,
            content_sanitized=True,
        )
        data = {
            "message": payload.get("message") or "说说已发布。",
            "media_count": _to_int(payload.get("media_count"), len(media)),
            "photo_count": _to_int(payload.get("photo_count"), len(media)),
        }
        fid = str(payload.get("fid") or "")
        login_uin = _to_int(status.get("login_uin") or status.get("uin"), 0)
        if fid and login_uin:
            created_at = int(time.time())
            ref_entry = FeedEntry(
                hostuin=login_uin,
                fid=fid,
                appid=311,
                summary=content,
                nickname=str(self._login_author_payload(status).get("nickname") or ""),
                created_at=created_at,
            )
            data["post"] = {
                "id": self._post_ref_id(login_uin, fid, 311, snapshot=asdict(ref_entry)),
                "author": self._login_author_payload(status),
                "content": content,
                "created_at": created_at,
                "appid": 311,
                "stats": {"likes": 0, "comments": 0},
                "liked": False,
                "images": [
                    source
                    for source in (self._browser_media_source(item) for item in media if isinstance(item, dict))
                    if source
                ][:9],
                "can_comment": True,
                "can_like": True,
                "can_delete": True,
            }
        return _success(data, message="说说已发布。")

    async def like(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self._schedule_preload("page like")
        await self._ensure_cookie_bound()
        body = body or {}
        ref = self._decode_post_ref(body.get("id") or body.get("post_id"))
        unlike = _to_bool(body.get("unlike"), False)
        payload = await self.controller.like_post(
            hostuin=ref.hostuin,
            fid=ref.fid,
            appid=ref.appid,
            curkey=ref.curkey or ref.unikey,
            unlike=unlike,
            fast=True,
        )
        verified = payload.get("verified", True) is not False
        data = {
            "action": "unlike" if unlike else "like",
            "liked": bool(payload.get("liked", not unlike)),
            "verified": verified,
            "already": bool(payload.get("already")),
            "operation_status": payload.get("operation_status") or ("done" if verified else "accepted_pending_verification"),
            "summary": _clean_text(payload.get("summary"), 160),
        }
        return _success(data, message="已提交到 QQ空间。")

    async def comment(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self._schedule_preload("page comment")
        status = await self._ensure_cookie_bound()
        body = body or {}
        ref = self._decode_post_ref(body.get("id") or body.get("post_id"))
        content = str(body.get("content") or "").strip()
        if not content:
            raise QzoneParseError("评论内容不能为空。")
        payload = await self.controller.comment_post(
            hostuin=ref.hostuin,
            fid=ref.fid,
            content=content,
            appid=ref.appid,
            private=_to_bool(body.get("private"), False),
            busi_param=ref.busi_param,
        )
        return _success(
            {
                "comment": {
                    "id": str(payload.get("commentid") or ""),
                    "content": content,
                    "author": self._login_author_payload(status),
                },
                "message": payload.get("message") or "评论已发送。",
            },
            message="评论已发送。",
        )

    async def reply(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self._schedule_preload("page reply")
        status = await self._ensure_cookie_bound()
        body = body or {}
        ref = self._decode_post_ref(body.get("id") or body.get("post_id"))
        content = str(body.get("content") or "").strip()
        commentid = str(body.get("commentid") or body.get("comment_id") or "")
        comment_uin = _to_int(body.get("comment_uin") or body.get("commentUin"), 0)
        if not content:
            raise QzoneParseError("回复内容不能为空。")
        if not commentid or not comment_uin:
            raise QzoneParseError("缺少要回复的评论。")
        payload = await self.controller.reply_comment(
            hostuin=ref.hostuin,
            fid=ref.fid,
            commentid=commentid,
            comment_uin=comment_uin,
            content=content,
            appid=ref.appid,
        )
        return _success(
            {
                "reply": {
                    "id": str(payload.get("commentid") or ""),
                    "content": content,
                    "author": self._login_author_payload(status),
                },
                "message": payload.get("message") or "回复已发送。",
            },
            message="回复已发送。",
        )

    async def delete(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self._schedule_preload("page delete")
        status = await self._ensure_cookie_bound()
        body = body or {}
        ref = self._decode_post_ref(body.get("id") or body.get("post_id"))
        login_uin = _to_int(status.get("login_uin") or status.get("uin"), 0)
        if not login_uin or ref.hostuin != login_uin:
            raise QzoneParseError("只能删除自己发布的说说。")
        entry = self._entry_from_ref_snapshot(ref)
        created_at = int(entry.created_at or 0) if entry is not None else 0
        payload = await self.controller.delete_post(fid=ref.fid, appid=ref.appid, created_at=created_at)
        return _success({"message": payload.get("message") or "说说已删除。"}, message="说说已删除。")

    async def upload_media(self, *, filename: str, content_type: str = "", data: bytes) -> dict[str, Any]:
        if not data:
            raise QzoneParseError("图片内容为空。")
        name = Path(filename or "image.jpg").name
        mime_type = (content_type or mimetypes.guess_type(name)[0] or guess_mime_type(name) or "").split(";", 1)[0]
        suffix = Path(name).suffix.lower()
        if suffix not in QZONE_IMAGE_SUFFIXES and not mime_type.lower().startswith("image/"):
            raise QzoneParseError("只支持上传图片文件。")
        if not looks_like_supported_image_bytes(data):
            raise QzoneParseError("图片内容不是可上传的图片文件。")
        dimensions = image_dimensions_from_bytes(data)
        if dimensions is not None and min(dimensions) < QZONE_MIN_IMAGE_SIDE:
            raise QzoneParseError(f"图片尺寸过小，请选择至少 {QZONE_MIN_IMAGE_SIDE}×{QZONE_MIN_IMAGE_SIDE} 的图片。")
        upload_dir = self._page_upload_dir()
        if upload_dir is not None:
            upload_id = "upload_" + secrets.token_urlsafe(18)
            safe_name = self._safe_upload_filename(name)
            path = upload_dir / f"{upload_id}_{safe_name}"
            await asyncio.to_thread(path.write_bytes, data)
            self._cleanup_upload_dir(upload_dir)
            stored_media = {
                "kind": "image",
                "source": str(path),
                "name": name,
                "mime_type": mime_type,
                "size": len(data),
                "trusted_local": True,
                "upload_id": upload_id,
            }
            self._uploaded_media_by_id[upload_id] = stored_media
            return _success(
                {
                    "media": {
                        "kind": "image",
                        "upload_id": upload_id,
                        "name": name,
                        "mime_type": mime_type,
                        "size": len(data),
                    }
                },
                message="图片已加入发布队列。",
            )
        media = {
            "kind": "image",
            "source": "base64://" + base64.b64encode(data).decode("ascii"),
            "name": name,
            "mime_type": mime_type,
            "size": len(data),
        }
        if not is_supported_image(media):
            raise QzoneParseError("只支持上传图片文件。")
        return _success({"media": media}, message="图片已加入发布队列。")

    @staticmethod
    def _decode_base64_upload(value: Any) -> bytes:
        compact = "".join(str(value or "").split())
        if not compact:
            raise QzoneParseError("图片内容为空。")
        compact += "=" * (-len(compact) % 4)
        try:
            return base64.b64decode(compact.encode("ascii"), altchars=b"-_", validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise QzoneParseError("图片 Base64 数据格式不正确。") from exc

    @staticmethod
    def _decode_upload_source(value: Any) -> tuple[bytes, str]:
        text = str(value or "").strip()
        if not text:
            raise QzoneParseError("图片内容为空。")
        if text.startswith("data:"):
            header, separator, payload = text.partition(",")
            if not separator:
                raise QzoneParseError("图片 data_url 格式不正确。")
            media_header = header[5:]
            parts = media_header.split(";") if media_header else []
            mime_type = parts[0] if parts and "/" in parts[0] else ""
            if any(part.lower() == "base64" for part in parts[1:]):
                return QzonePageApi._decode_base64_upload(payload), mime_type
            return unquote_to_bytes(payload), mime_type
        if text.startswith("base64://"):
            text = text[len("base64://"):]
        return QzonePageApi._decode_base64_upload(text), ""

    async def upload_media_payload(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        if not isinstance(body, dict):
            raise QzoneParseError("图片上传请求格式不正确。")
        source = (
            body.get("data_url")
            or body.get("source")
            or body.get("base64")
            or body.get("data")
            or body.get("content")
        )
        data, detected_type = self._decode_upload_source(source)
        filename = (
            body.get("filename")
            or body.get("name")
            or body.get("file_name")
            or body.get("fileName")
            or "image.jpg"
        )
        content_type = (
            body.get("content_type")
            or body.get("mime_type")
            or body.get("mime")
            or detected_type
            or ""
        )
        return await self.upload_media(filename=str(filename), content_type=str(content_type), data=data)
