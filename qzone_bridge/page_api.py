"""WebUI Page API adapter for the Qzone plugin.

This module keeps the AstrBot Pages surface separate from chat commands and
LLM tools. It returns browser-friendly, redacted payloads and delegates all
real Qzone operations to the existing controller/service layer.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import hashlib
import io
import json
import mimetypes
import re
import secrets
import time
import httpx
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable
from urllib.parse import unquote_to_bytes, urlparse

from .errors import DaemonUnavailableError, LifeGenerationError, QzoneAuthError, QzoneBridgeError, QzoneNeedsRebind, QzoneParseError, QzoneRequestError
from .media import (
    QZONE_IMAGE_SUFFIXES,
    QZONE_MAX_IMAGES,
    QZONE_MIN_IMAGE_SIDE,
    QZONE_VIDEO_SUFFIXES,
    guess_mime_type,
    image_dimensions_from_bytes,
    is_supported_image,
    is_video_media,
    looks_like_supported_image_bytes,
)
from .models import FeedEntry
from .social import QzoneComment, QzonePost, dedupe_image_sources, post_from_entry
from .local_media import resolve_trusted_local_media_path
from .page_life import DIAGNOSTIC_MESSAGES, PageLifeError, PageLifeJob, PageLifeJobStore, public_diagnostic
from .page_media import open_page_media_stream
from .utils import truncate


PAGE_UPLOAD_MAX_BYTES: int | None = None
PAGE_DEFAULT_LIMIT = 10
PAGE_MAX_LIMIT = 30
PAGE_DETAIL_TIMEOUT_SECONDS = 8.0
PAGE_STATUS_TIMEOUT_SECONDS = 2.0
PAGE_DETAIL_CACHE_TTL_SECONDS = 15.0
PAGE_AI_DRAFT_TIMEOUT_SECONDS = 45.0
PAGE_AI_SLOT_WAIT_SECONDS = 0.05
PAGE_LIFE_PREVIEW_MAX_SOURCE_BYTES = 8 * 1024 * 1024
PAGE_LIFE_PREVIEW_MAX_PIXELS = 20_000_000
PAGE_LIFE_PREVIEW_MAX_EDGE = 512
PAGE_LIFE_PREVIEW_MAX_JPEG_BYTES = 128 * 1024
PAGE_LIFE_PREVIEW_TIMEOUT_SECONDS = 10.0


class _PreviewDiagnosticError(ValueError):
    def __init__(self, code: str, phase: str = "preview_read") -> None:
        self.code = code
        self.phase = phase


def _safe_diagnostic(error: BaseException, phase: str) -> dict[str, Any]:
    """Classify by exact known types only; never inspect exception text or arbitrary attributes."""
    code = "INTERNAL_ERROR"
    try:
        error_type = type(error)
        if error_type is TypeError:
            code = "INTERFACE_ERROR"
        elif (error_type is asyncio.TimeoutError or error_type is TimeoutError
              or error_type is httpx.TimeoutException or error_type is httpx.ConnectTimeout
              or error_type is httpx.ReadTimeout or error_type is httpx.WriteTimeout
              or error_type is httpx.PoolTimeout):
            code = "TIMEOUT"
        elif error_type is asyncio.CancelledError:
            code = "CANCELLED"
        elif error_type is QzoneRequestError:
            code = "REQUEST_ERROR"
        elif error_type is QzoneParseError:
            code = "SOURCE_OR_FORMAT_ERROR"
        elif error_type is QzoneAuthError or error_type is QzoneNeedsRebind:
            code = "AUTH_REQUIRED"
        elif error_type is DaemonUnavailableError:
            code = "DAEMON_UNAVAILABLE"
        elif error_type is _PreviewDiagnosticError:
            candidate = object.__getattribute__(error, "code")
            if type(candidate) is str and candidate in DIAGNOSTIC_MESSAGES:
                code = candidate
            phase_value = object.__getattribute__(error, "phase")
            if type(phase_value) is str and phase_value in {"preview_read", "preview_raster", "preview_wait"}:
                phase = phase_value
    except BaseException:
        # A hostile exception implementation must not leave a job in `sending`.
        code = "INTERNAL_ERROR"
    return {"id": secrets.token_hex(6), "code": code, "phase": phase,
            "message": DIAGNOSTIC_MESSAGES[code], "at": time.time()}


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
        ai_comment_generator: Callable[[QzonePost], Awaitable[str]] | None = None,
        ai_reply_generator: Callable[[QzonePost, QzoneComment], Awaitable[str]] | None = None,
        reply_lock: asyncio.Lock | None = None,
        manual_reply_marker: Callable[[int, int, str, str, int], Awaitable[bool]] | None = None,
        life_draft_generator: Callable[[Callable[[str], None]], Awaitable[Any]] | None = None,
        life_capability_provider: Callable[[], dict[str, Any]] | None = None,
        draft_store: Any = None,
    ):
        self.controller = controller
        self.post_service_factory = post_service_factory
        self.settings = settings
        self.status_provider = status_provider
        self.preload_scheduler = preload_scheduler
        self.ai_comment_generator = ai_comment_generator
        self.ai_reply_generator = ai_reply_generator
        self._ai_generation_slots = asyncio.Semaphore(2)
        self.reply_lock = reply_lock or asyncio.Lock()
        self.manual_reply_marker = manual_reply_marker
        self.life_draft_generator = life_draft_generator
        self.life_capability_provider = life_capability_provider
        self.draft_store = draft_store
        self._life_jobs = PageLifeJobStore()
        self._life_preview_slots = asyncio.Semaphore(2)
        self._life_preview_inflight: dict[tuple[str, str], asyncio.Task[bytes]] = {}
        self._life_owned_upload_ids: dict[str, str] = {}
        self._life_owned_media: dict[str, tuple[str, str]] = {}
        self._life_closed = False
        self._refs_by_id: dict[str, PagePostRef] = {}
        self._ids_by_ref: dict[tuple[int, str, int], str] = {}
        self._feed_seen_refs: dict[tuple[str, int], set[tuple[int, str, int]]] = {}
        self._feed_emitted_cursors: dict[tuple[str, int], set[str]] = {}
        self._uploaded_media_by_id: dict[str, dict[str, Any]] = {}
        self._page_media_by_id: dict[str, dict[str, Any]] = {}
        self._page_media_ids_by_source: dict[str, str] = {}
        self._detail_cache: dict[tuple[int, str, int, str], tuple[float, dict[str, Any]]] = {}
        self._detail_inflight: dict[tuple[int, str, int, str], asyncio.Task[dict[str, Any]]] = {}

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

    async def _life_owner_status(self) -> tuple[dict[str, Any], int]:
        status = await self._ensure_cookie_bound()
        account_uin = _to_int(status.get("login_uin") or status.get("uin"), 0)
        if account_uin <= 0:
            raise PageLifeError("LIFE_INVALID_OWNER", "请先绑定有效 QQ 账号后生成日常草稿。")
        return status, account_uin

    async def _prune_life_media(self) -> None:
        retained = await self._life_jobs.retained_ids()
        cancelled: list[asyncio.Task[bytes]] = []
        for upload_id, job_id in list(self._life_owned_upload_ids.items()):
            if job_id not in retained:
                self._drop_life_upload(upload_id, job_id)
        for media_id, (job_id, _upload_id) in list(self._life_owned_media.items()):
            if job_id not in retained:
                self._life_owned_media.pop(media_id, None)
        for key, task in list(self._life_preview_inflight.items()):
            if key[0] not in retained:
                self._life_preview_inflight.pop(key, None)
                if not task.done():
                    task.cancel()
                    cancelled.append(task)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)

    def _drop_life_upload(self, upload_id: str, job_id: str) -> None:
        if self._life_owned_upload_ids.get(upload_id) != job_id:
            return
        stored = self._uploaded_media_by_id.get(upload_id)
        if isinstance(stored, dict) and stored.get("_life_job_id") == job_id:
            self._uploaded_media_by_id.pop(upload_id, None)
        self._life_owned_upload_ids.pop(upload_id, None)

    def _life_capability_status(self) -> dict[str, Any]:
        if self.life_capability_provider is None:
            return {"available": False, "message": "日常草稿功能当前未就绪。", "life_scheduler": "unknown", "omnidraw": "unknown", "provider": "unknown", "text_only_allowed": False}
        try:
            value = self.life_capability_provider()
        except Exception:
            return {"available": False, "message": "日常草稿依赖状态暂不可查询。", "life_scheduler": "unknown", "omnidraw": "unknown", "provider": "unknown", "text_only_allowed": False}
        if not isinstance(value, dict):
            value = {}
        return {
            "available": bool(value.get("available")),
            "message": _clean_text(value.get("message"), 160),
            "life_scheduler": str(value.get("life_scheduler") or "unknown")[:16],
            "omnidraw": str(value.get("omnidraw") or "unknown")[:16],
            "provider": "unknown",
            "text_only_allowed": bool(value.get("text_only_allowed")),
        }

    async def start_life_draft(self, body: Any) -> dict[str, Any]:
        if self._life_closed:
            raise PageLifeError("LIFE_CLOSED", "日常草稿服务正在关闭，请稍后重试。")
        if not isinstance(body, dict) or set(body) != {"action", "request_id"}:
            raise PageLifeError("LIFE_INVALID_REQUEST", "日常草稿请求格式无效。")
        action, request_id = body.get("action"), body.get("request_id")
        if action != "generate_draft" or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id, flags=re.ASCII):
            raise PageLifeError("LIFE_INVALID_REQUEST", "日常草稿请求格式无效。")
        if self.life_draft_generator is None or self.draft_store is None:
            raise PageLifeError("LIFE_UNAVAILABLE", "日常草稿功能当前未就绪。")
        _status, account_uin = await self._life_owner_status()
        capability = self._life_capability_status()
        await self._prune_life_media()
        job, created = await self._life_jobs.admit(
            request_id, account_uin, allow_new=capability["available"],
            unavailable_message=capability["message"] or "日常草稿依赖尚未就绪。",
        )
        if created:
            task = asyncio.create_task(self._run_life_generation(job.id, account_uin))
            await self._life_jobs.attach_task(job.id, task)
        data = PageLifeJobStore.public_payload(job)
        data["capability"] = capability
        return _success(data, message="草稿生成任务已接收。" if created else "已关联现有草稿生成任务。")

    async def get_life_draft(self, params: Any = None) -> dict[str, Any]:
        params = params or {}
        if not isinstance(params, dict) or set(params) - {"id"}:
            raise PageLifeError("LIFE_INVALID_REQUEST", "日常草稿查询参数无效。")
        _, account_uin = await self._life_owner_status()
        await self._prune_life_media()
        job_id = str(params.get("id") or "").strip()
        if job_id:
            job = await self._life_jobs.get(job_id, account_uin)
        else:
            job = await self._life_jobs.active_or_latest(account_uin)
        data = PageLifeJobStore.public_payload(job) if job else {"state": "idle"}
        data["capability"] = self._life_capability_status()
        return _success(data)

    async def _run_life_generation(self, job_id: str, account_uin: int) -> None:
        try:
            def progress(phase: str) -> None:
                try:
                    asyncio.get_running_loop().create_task(self._life_jobs.set_phase(job_id, phase))
                except RuntimeError:
                    pass

            result = await self.life_draft_generator(progress)  # type: ignore[misc]
            if self._life_closed:
                raise PageLifeError("LIFE_CLOSED", "插件已关闭，日常草稿生成已取消。")
            if result is None or not isinstance(getattr(result, "content", None), str):
                raise PageLifeError("LIFE_GENERATION_FAILED", "日常草稿生成失败，请检查配置后重试。")
            current_status, current_uin = await self._life_owner_status()
            if current_uin != account_uin:
                raise PageLifeError("LIFE_ACCOUNT_CHANGED", "登录账号已变化，草稿未保存。")
            media_objects = list(getattr(result, "media", []) or [])
            if len(media_objects) > QZONE_MAX_IMAGES:
                raise PageLifeError("LIFE_GENERATION_FAILED", "生成媒体数量超出限制，草稿未保存。")
            media_sources: dict[str, dict[str, Any]] = {}
            public_media: list[dict[str, Any]] = []
            draft_media: list[dict[str, Any]] = []
            for index, item in enumerate(media_objects):
                raw = item.to_dict() if callable(getattr(item, "to_dict", None)) else dict(item) if isinstance(item, dict) else {}
                source = str(raw.get("source") or "").strip()
                if not source:
                    continue
                media_id = "lm_" + secrets.token_urlsafe(12)
                upload_id = "life_upload_" + secrets.token_urlsafe(18)
                name = self._safe_upload_filename(raw.get("name") or raw.get("filename") or f"life-{index + 1}.jpg")
                kind = str(raw.get("kind") or "image")
                mime_type = str(raw.get("mime_type") or guess_mime_type(name) or "image/jpeg").split(";", 1)[0]
                size = _to_int(raw.get("size"), 0)
                stored = {**raw, "kind": kind, "name": name, "mime_type": mime_type, "upload_id": upload_id,
                          "trusted_local": bool(raw.get("trusted_local")), "_life_job_id": job_id,
                          "_life_owner_uin": account_uin, "_life_media_id": media_id}
                self._uploaded_media_by_id[upload_id] = stored
                self._life_owned_upload_ids[upload_id] = job_id
                self._life_owned_media[media_id] = (job_id, upload_id)
                media_sources[media_id] = stored
                public_media.append({"kind": kind, "name": name, "mime_type": mime_type,
                                     "upload_id": upload_id, "media_id": media_id, "size": size})
                draft_media.append(dict(raw))
            await self._life_jobs.begin_draft_save(job_id, account_uin)
            current_status, current_uin = await self._life_owner_status()
            if current_uin != account_uin:
                raise PageLifeError("LIFE_ACCOUNT_CHANGED", "登录账号已变化，草稿未保存。")
            draft = await self.draft_store.add_async(
                author_uin=account_uin,
                author_name=str(current_status.get("login_nickname") or current_status.get("nickname") or "WebUI 日常草稿"),
                group_id=0, content=result.content, media=draft_media, anonymous=False,
            )
            _, current_uin = await self._life_owner_status()
            if current_uin != account_uin:
                raise PageLifeError("LIFE_ACCOUNT_CHANGED", "登录账号已变化，草稿未显示。")
            has_media = bool(public_media)
            text_only = bool(self._life_capability_status().get("text_only_allowed"))
            message = "纯文字日常草稿已保存。" if text_only and not has_media else "日常草稿已保存，可载入编辑后手动发布。"
            await self._life_jobs.succeed(
                job_id, draft_id=int(getattr(draft, "id", 0) or 0), content=result.content,
                media=public_media, media_sources=media_sources, has_media=has_media, message=message,
            )
            await self._prune_life_media()
        except asyncio.CancelledError:
            await self._life_jobs.cancel_generation(job_id)
            await self._prune_life_media()
            raise
        except PageLifeError as exc:
            await self._life_jobs.fail(job_id, exc.message)
            await self._prune_life_media()
        except LifeGenerationError as exc:
            await self._life_jobs.fail(job_id, exc.message, code=exc.code, phase=exc.phase)
            await self._prune_life_media()
        except Exception:
            await self._life_jobs.fail(job_id, "日常草稿生成或保存失败，请检查配置后重试。")
            await self._prune_life_media()

    async def life_draft_preview(self, params: Any = None) -> dict[str, Any]:
        if self._life_closed:
            raise PageLifeError("LIFE_CLOSED", "日常草稿服务正在关闭，请稍后重试。")
        params = params or {}
        if not isinstance(params, dict) or set(params) != {"job_id", "media_id"}:
            raise PageLifeError("LIFE_INVALID_REQUEST", "日常草稿预览参数无效。")
        _, account_uin = await self._life_owner_status()
        await self._prune_life_media()
        job_id, media_id = str(params.get("job_id") or ""), str(params.get("media_id") or "")
        job = await self._life_jobs.get(job_id, account_uin)
        owned = self._life_owned_media.get(media_id)
        if owned is None or owned[0] != job.id or job.state != "succeeded":
            raise PageLifeError("LIFE_PREVIEW_UNAVAILABLE", "日常草稿图片预览暂不可用。")
        cached = job.thumbnail_cache.get(media_id)
        if cached is None:
            key = (job.id, media_id)
            task = self._life_preview_inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._life_make_thumbnail(job, media_id))
                self._life_preview_inflight[key] = task
            try:
                cached = await asyncio.shield(task)
            except asyncio.CancelledError:
                diagnostic = _safe_diagnostic(asyncio.CancelledError(), "preview_wait")
                await self._life_jobs.mark_preview_diagnostic(job.id, media_id, diagnostic)
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                raise
            except Exception as error:
                # Revalidate ownership and retention before exposing a failure from a slower read/raster task.
                _, current_uin = await self._life_owner_status()
                if current_uin != account_uin:
                    raise PageLifeError("LIFE_ACCOUNT_CHANGED", "登录账号已变化，不能读取该日常草稿预览。") from None
                await self._life_jobs.get(job_id, current_uin)
                diagnostic = _safe_diagnostic(error, "preview_read")
                if type(error) is _PreviewDiagnosticError:
                    diagnostic = _safe_diagnostic(error, "preview_read")
                diagnostic = await self._life_jobs.mark_preview_diagnostic(job.id, media_id, diagnostic)
                if diagnostic is None:
                    raise PageLifeError("LIFE_PREVIEW_UNAVAILABLE", "日常草稿图片预览暂不可用。") from None
                raise PageLifeError("LIFE_PREVIEW_UNAVAILABLE",
                    f"日常草稿图片预览暂不可用。{diagnostic['code']} {diagnostic['id']}") from None
            finally:
                if task.done():
                    self._life_preview_inflight.pop(key, None)
        _, current_uin = await self._life_owner_status()
        if current_uin != account_uin:
            raise PageLifeError("LIFE_ACCOUNT_CHANGED", "登录账号已变化，不能读取该日常草稿预览。")
        await self._life_jobs.get(job_id, current_uin)
        return _success({"job_id": job.id, "media_id": media_id,
                         "data_url": "data:image/jpeg;base64," + base64.b64encode(cached).decode("ascii")})

    async def _life_make_thumbnail(self, job: PageLifeJob, media_id: str) -> bytes:
        try:
            return await asyncio.wait_for(
                self._life_make_thumbnail_bounded(job, media_id), timeout=PAGE_LIFE_PREVIEW_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise _PreviewDiagnosticError("PREVIEW_TIMEOUT", "preview_wait") from None

    async def _life_make_thumbnail_bounded(self, job: PageLifeJob, media_id: str) -> bytes:
        async with self._life_preview_slots:
            media = job.media_sources.get(media_id)
            if not isinstance(media, dict):
                raise _PreviewDiagnosticError("SOURCE_MISSING")
            source = str(media.get("source") or "")

            async def read_source() -> bytes:
                scheme = urlparse(source).scheme.lower()
                if scheme in {"http", "https"}:
                    try:
                        stream, _status, _headers = await open_page_media_stream(media)
                    except QzoneParseError:
                        raise _PreviewDiagnosticError("REMOTE_POLICY_FAILED") from None
                    chunks: list[bytes] = []
                    total = 0
                    try:
                        async for chunk in stream:
                            total += len(chunk)
                            if total > PAGE_LIFE_PREVIEW_MAX_SOURCE_BYTES:
                                raise _PreviewDiagnosticError("SOURCE_TOO_LARGE")
                            chunks.append(chunk)
                    finally:
                        await stream.aclose()
                    return b"".join(chunks)
                if scheme in {"data", "base64"} or source.startswith("base64://"):
                    if len(source) > (PAGE_LIFE_PREVIEW_MAX_SOURCE_BYTES * 4 // 3) + 4096:
                        raise _PreviewDiagnosticError("ENCODED_SOURCE_TOO_LARGE")
                    data, _mime = self._decode_upload_source(source)
                    if len(data) > PAGE_LIFE_PREVIEW_MAX_SOURCE_BYTES:
                        raise _PreviewDiagnosticError("SOURCE_TOO_LARGE")
                    return data
                if media.get("trusted_local"):
                    path = resolve_trusted_local_media_path(source, name=str(media.get("name") or ""), suffixes=QZONE_IMAGE_SUFFIXES)
                    if path is None:
                        raise _PreviewDiagnosticError("SOURCE_MISSING")
                    return await asyncio.to_thread(lambda: path.open("rb").read(PAGE_LIFE_PREVIEW_MAX_SOURCE_BYTES + 1))
                raise _PreviewDiagnosticError("SOURCE_UNTRUSTED")

            try:
                data = await read_source()
                if not data or len(data) > PAGE_LIFE_PREVIEW_MAX_SOURCE_BYTES:
                    raise _PreviewDiagnosticError("SOURCE_TOO_LARGE")
                try:
                    result = await asyncio.to_thread(self._rasterize_life_preview, data)
                except _PreviewDiagnosticError:
                    raise
                except Exception:
                    raise _PreviewDiagnosticError("RASTER_DECODE_FAILED", "preview_raster") from None
                if self._life_closed or not await self._life_jobs.can_cache_thumbnail(job.id):
                    raise asyncio.CancelledError
                job.thumbnail_cache[media_id] = result
                return result
            except asyncio.CancelledError:
                raise

    @staticmethod
    def _rasterize_life_preview(data: bytes) -> bytes:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP", "GIF", "BMP"}:
                raise _PreviewDiagnosticError("RASTER_UNSUPPORTED", "preview_raster")
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > PAGE_LIFE_PREVIEW_MAX_PIXELS:
                raise _PreviewDiagnosticError("PIXEL_LIMIT", "preview_raster")
            image.seek(0)
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((PAGE_LIFE_PREVIEW_MAX_EDGE, PAGE_LIFE_PREVIEW_MAX_EDGE), Image.Resampling.LANCZOS)
            for quality in (82, 70, 58, 45, 32):
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=quality, optimize=True)
                result = output.getvalue()
                if len(result) <= PAGE_LIFE_PREVIEW_MAX_JPEG_BYTES:
                    return result
        raise _PreviewDiagnosticError("OUTPUT_TOO_LARGE", "preview_raster")

    async def close(self) -> None:
        self._life_closed = True
        tasks = list(self._life_preview_inflight.values())
        self._life_preview_inflight.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        await self._life_jobs.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for upload_id, job_id in list(self._life_owned_upload_ids.items()):
            self._drop_life_upload(upload_id, job_id)
        self._life_owned_media.clear()
        self._life_preview_inflight.clear()

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
    def _detail_key(ref: PagePostRef, busi_param: str = "") -> tuple[int, str, int, str]:
        return (int(ref.hostuin or 0), str(ref.fid or ""), int(ref.appid or 311), str(busi_param or ""))

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
            raise QzoneParseError("媒体列表格式不正确。")
        payload = dict(item)
        upload_id = str(payload.get("upload_id") or "").strip()
        if upload_id:
            stored = self._uploaded_media_by_id.get(upload_id)
            if stored is None:
                raise QzoneParseError("媒体上传缓存已过期，请重新选择图片/视频后再发布。")
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
            raise QzoneParseError("媒体缺少可上传的数据。")
        if not is_supported_image(payload) and not is_video_media(payload):
            raise QzoneParseError("只支持上传图片或视频文件。")
        return payload

    def _page_media_list(self, value: Any) -> list[dict[str, Any]]:
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise QzoneParseError("媒体列表格式不正确。")
        media = [self._page_media_item(item) for item in value]
        if len(media) > QZONE_MAX_IMAGES:
            raise QzoneParseError(f"QQ空间一次最多只能上传 {QZONE_MAX_IMAGES} 个图片/视频。")
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
            "segments": list(post.nickname_segments),
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
        page_media = [self._page_post_media_item(item) for item in post.media if isinstance(item, dict)]
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
            "content_segments": list(post.content_segments),
            "content": post.summary,
            "created_at": post.created_at,
            "appid": post.appid,
            "stats": {
                "likes": post.like_count,
                "comments": post.comment_count,
            },
            "liked": bool(post.liked),
            "images": dedupe_image_sources(post.images)[:9],
            "media": page_media,
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

    def _page_post_media_item(self, item: dict[str, Any]) -> dict[str, Any]:
        payload = dict(item)
        source = str(payload.get("download_url") or payload.get("source") or "").strip()
        if not source:
            return payload
        media_id = self._page_media_ids_by_source.get(source)
        if not media_id:
            media_id = "media_" + secrets.token_urlsafe(18)
            self._page_media_ids_by_source[source] = media_id
        stored = dict(payload)
        stored["source"] = source
        self._page_media_by_id[media_id] = stored
        payload["media_id"] = media_id
        return payload

    def page_media_ref(self, value: Any) -> dict[str, Any]:
        media_id = str(value or "").strip()
        media = self._page_media_by_id.get(media_id)
        if media is None:
            raise QzoneParseError("媒体引用已过期，请刷新说说后重试。")
        return dict(media)

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
        cache_key = self._detail_key(ref, busi_param)
        now = time.monotonic()
        cached = self._detail_cache.get(cache_key)
        if cached and cached[0] > now:
            return copy.deepcopy(cached[1])
        if cached:
            self._detail_cache.pop(cache_key, None)

        inflight = self._detail_inflight.get(cache_key)
        if inflight is not None and not inflight.done():
            return copy.deepcopy(await inflight)

        task = asyncio.create_task(self._detail_uncached(ref, status, busi_param))
        self._detail_inflight[cache_key] = task
        try:
            result = await task
        finally:
            self._detail_inflight.pop(cache_key, None)
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict) and not data.get("partial"):
            self._detail_cache[cache_key] = (
                time.monotonic() + max(0.0, float(PAGE_DETAIL_CACHE_TTL_SECONDS)),
                copy.deepcopy(result),
            )
        return result

    async def _detail_uncached(self, ref: PagePostRef, status: dict[str, Any], busi_param: str) -> dict[str, Any]:
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
            raise QzoneParseError("说说内容或图片/视频不能为空。")
        if not isinstance(media, list):
            raise QzoneParseError("媒体列表格式不正确。")
        if len(media) > QZONE_MAX_IMAGES:
            raise QzoneParseError(f"QQ空间一次最多只能上传 {QZONE_MAX_IMAGES} 个图片/视频。")
        life_job_id = str(body.get("life_job_id") or "").strip()
        life_upload_ids = [str(item.get("upload_id") or "") for item in (body.get("media") or []) if isinstance(item, dict)]
        owned_jobs = {self._life_owned_upload_ids.get(upload_id, "") for upload_id in life_upload_ids if upload_id in self._life_owned_upload_ids}
        if owned_jobs and (not life_job_id or owned_jobs != {life_job_id}):
            raise PageLifeError("LIFE_PROVENANCE_REQUIRED", "日常草稿媒体需要通过原草稿载入并确认发布。")
        life_job: PageLifeJob | None = None
        digest = ""
        should_send = True
        if life_job_id:
            if _to_int(status.get("login_uin") or status.get("uin"), 0) <= 0:
                raise PageLifeError("LIFE_INVALID_OWNER", "请先绑定有效 QQ 账号后发布日常草稿。")
            life_job = await self._life_jobs.get(life_job_id, _to_int(status.get("login_uin") or status.get("uin"), 0))
            if life_job.state != "succeeded":
                raise PageLifeError("LIFE_NOT_READY", "日常草稿尚未生成完成。")
            for upload_id in life_upload_ids:
                owner_job = self._life_owned_upload_ids.get(upload_id)
                if owner_job and owner_job != life_job_id:
                    raise PageLifeError("LIFE_PROVENANCE_MISMATCH", "媒体不属于当前日常草稿。")
            sync_weibo = _to_bool(body.get("sync_weibo"), False)
            canonical_media = [
                {key: value for key, value in item.items() if key not in {"preview_url", "previewUrl", "_life_job_id", "_life_owner_uin", "_life_media_id"}}
                for item in media
            ]
            digest = hashlib.sha256(json.dumps(
                {"content": content, "media": canonical_media, "sync_weibo": sync_weibo},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
            ).encode("utf-8")).hexdigest()
            life_job, should_send = await self._life_jobs.begin_publish(life_job_id, life_job.account_uin, digest)
            if not should_send:
                return _success(dict(life_job.publish_result or {}), message="说说已发布。")
            media = canonical_media
        dispatched = False
        publish_phase = "controller_call"
        try:
            kwargs = {
                "content": content,
                "sync_weibo": _to_bool(body.get("sync_weibo"), False),
                "media": media,
                "content_sanitized": True,
            }
            if life_job is not None:
                kwargs["expected_login_uin"] = life_job.account_uin
            dispatched = True
            payload = await self.controller.publish_post(**kwargs)
            publish_phase = "result_processing"
            data = {
                "message": "说说已发布。" if life_job is not None else (payload.get("message") or "说说已发布。"),
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
                    "images": [] if life_job is not None else [
                        source
                        for source in (self._browser_media_source(item) for item in media if isinstance(item, dict))
                        if source
                    ][:9],
                    "can_comment": True,
                    "can_like": True,
                    "can_delete": True,
                }
            if life_job is not None:
                publish_phase = "bookkeeping"
                marked = await asyncio.shield(self._life_jobs.mark_published(life_job.id, data))
                if not marked:
                    diagnostic = _safe_diagnostic(RuntimeError(), "bookkeeping")
                    await asyncio.shield(self._life_jobs.mark_unknown(life_job.id, diagnostic=diagnostic))
            return _success(data, message="说说已发布。")
        except asyncio.CancelledError:
            if life_job is not None and dispatched:
                diagnostic = _safe_diagnostic(asyncio.CancelledError(), publish_phase)
                await asyncio.shield(self._life_jobs.mark_unknown(life_job.id, diagnostic=diagnostic))
            raise
        except Exception as error:
            if life_job is not None and dispatched:
                diagnostic = _safe_diagnostic(error, publish_phase)
                await asyncio.shield(self._life_jobs.mark_unknown(life_job.id, diagnostic=diagnostic))
                retained = public_diagnostic(life_job.publish_diagnostic) or diagnostic
                raise PageLifeError("LIFE_UNKNOWN",
                    f"发布结果尚不确定，请查询空间状态；系统不会自动重试。{retained['code']} {retained['id']}") from None
            raise

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
        async with self.reply_lock:
            payload = await self.controller.reply_comment(
                hostuin=ref.hostuin,
                fid=ref.fid,
                commentid=commentid,
                comment_uin=comment_uin,
                content=content,
                appid=ref.appid,
            )
            login_uin = _to_int(status.get("login_uin") or status.get("uin"), 0)
            if self.manual_reply_marker is not None and login_uin and ref.hostuin == login_uin:
                await self.manual_reply_marker(login_uin, ref.hostuin, ref.fid, commentid, comment_uin)
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

    async def _ai_generation_context(
        self, body: dict[str, Any] | None, *, reply: bool
    ) -> tuple[QzonePost, QzoneComment | None]:
        body = body or {}
        ref = self._decode_post_ref(body.get("id") or body.get("post_id"))
        if reply:
            commentid = str(body.get("commentid") or body.get("comment_id") or "").strip()
            comment_uin = _to_int(body.get("comment_uin") or body.get("commentUin"), 0)
            if not commentid or len(commentid) > 128 or not comment_uin:
                raise QzoneParseError("缺少要回复的评论。")
        else:
            commentid, comment_uin = "", 0
        status = await self._ensure_cookie_bound()
        busi_param = json.dumps(ref.busi_param, ensure_ascii=False, separators=(",", ":")) if ref.busi_param else ""
        detail = await asyncio.wait_for(
            self._detail_uncached(ref, status, busi_param),
            timeout=max(0.001, float(PAGE_DETAIL_TIMEOUT_SECONDS)),
        )
        data = detail.get("data") if isinstance(detail, dict) else None
        post_data = data.get("post") if isinstance(data, dict) else None
        if not isinstance(post_data, dict) or data.get("partial"):
            raise QzoneParseError("说说详情尚未确认，暂时不能生成草稿。")
        comments = []
        target = None
        for item in post_data.get("comments") or []:
            if not isinstance(item, dict):
                continue
            comment = QzoneComment(
                commentid=str(item.get("id") or item.get("commentid") or ""),
                uin=_to_int(item.get("author", {}).get("uin") if isinstance(item.get("author"), dict) else item.get("uin"), 0),
                nickname=str(item.get("author", {}).get("nickname") if isinstance(item.get("author"), dict) else item.get("nickname") or ""),
                content=str(item.get("content") or ""),
                created_at=_to_int(item.get("created_at"), 0),
                parent_id=str(item.get("parent_id") or ""),
            )
            comments.append(comment)
            if comment.commentid == commentid and comment.uin == comment_uin:
                target = comment
        if reply and target is None:
            raise QzoneParseError("目标评论已不存在或不属于这条说说。")
        author = post_data.get("author") if isinstance(post_data.get("author"), dict) else {}
        stats = post_data.get("stats") if isinstance(post_data.get("stats"), dict) else {}
        post = QzonePost(
            hostuin=ref.hostuin,
            fid=ref.fid,
            appid=ref.appid,
            summary=str(post_data.get("content") or ""),
            nickname=str(author.get("nickname") or ""),
            created_at=_to_int(post_data.get("created_at"), 0),
            like_count=_to_int(stats.get("likes"), 0),
            comment_count=_to_int(stats.get("comments"), 0),
            images=[str(item) for item in post_data.get("images", []) if isinstance(item, str)],
            comments=comments,
            busi_param=ref.busi_param,
        )
        return post, target

    async def _ai_draft(self, body: dict[str, Any] | None, *, reply: bool) -> dict[str, Any]:
        generator = self.ai_reply_generator if reply else self.ai_comment_generator
        if generator is None:
            raise QzoneParseError("AI 草稿功能未配置。")
        try:
            await asyncio.wait_for(self._ai_generation_slots.acquire(), timeout=PAGE_AI_SLOT_WAIT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise QzoneParseError("AI 草稿生成繁忙，请稍后重试。") from exc
        try:
            async def generate() -> str:
                post, comment = await self._ai_generation_context(body, reply=reply)
                try:
                    value = await generator(post, comment) if reply else await generator(post)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise QzoneParseError("AI 草稿生成失败，请稍后重试。") from exc
                return str(value or "").strip()

            try:
                content = await asyncio.wait_for(generate(), timeout=PAGE_AI_DRAFT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as exc:
                raise QzoneParseError("AI 草稿生成超时，请稍后重试。") from exc
            except (QzoneParseError, QzoneNeedsRebind, DaemonUnavailableError):
                raise
            except Exception as exc:
                # Keep controller/provider implementation details out of the Pages response.
                raise QzoneParseError("获取说说上下文失败，请稍后重试。") from exc
        finally:
            self._ai_generation_slots.release()
        content = str(content or "").strip()
        if not content:
            raise QzoneParseError("AI 未生成有效草稿。")
        return _success({"content": content})

    async def ai_comment(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._ai_draft(body, reply=False)

    async def ai_reply(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._ai_draft(body, reply=True)

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
            raise QzoneParseError("媒体内容为空。")
        name = Path(filename or "image.jpg").name
        mime_type = (content_type or mimetypes.guess_type(name)[0] or guess_mime_type(name) or "").split(";", 1)[0]
        suffix = Path(name).suffix.lower()
        media_kind = "video" if suffix in QZONE_VIDEO_SUFFIXES or mime_type.lower().startswith("video/") else "image"
        if media_kind == "image":
            if suffix not in QZONE_IMAGE_SUFFIXES and not mime_type.lower().startswith("image/"):
                raise QzoneParseError("只支持上传图片或视频文件。")
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
                "kind": media_kind,
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
                        "kind": media_kind,
                        "upload_id": upload_id,
                        "name": name,
                        "mime_type": mime_type,
                        "size": len(data),
                    }
                },
                message="媒体已加入发布队列。",
            )
        media = {
            "kind": media_kind,
            "source": "base64://" + base64.b64encode(data).decode("ascii"),
            "name": name,
            "mime_type": mime_type,
            "size": len(data),
        }
        if not is_supported_image(media) and not is_video_media(media):
            raise QzoneParseError("只支持上传图片或视频文件。")
        return _success({"media": media}, message="媒体已加入发布队列。")

    @staticmethod
    def _decode_base64_upload(value: Any) -> bytes:
        compact = "".join(str(value or "").split())
        if not compact:
            raise QzoneParseError("媒体内容为空。")
        compact += "=" * (-len(compact) % 4)
        try:
            return base64.b64decode(compact.encode("ascii"), altchars=b"-_", validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise QzoneParseError("媒体 Base64 数据格式不正确。") from exc

    @staticmethod
    def _decode_upload_source(value: Any) -> tuple[bytes, str]:
        text = str(value or "").strip()
        if not text:
            raise QzoneParseError("媒体内容为空。")
        if text.startswith("data:"):
            header, separator, payload = text.partition(",")
            if not separator:
                raise QzoneParseError("媒体 data_url 格式不正确。")
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
            raise QzoneParseError("媒体上传请求格式不正确。")
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
