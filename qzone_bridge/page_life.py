"""Bounded in-memory lifecycle for manually generated WebUI Life drafts."""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from dataclasses import dataclass, field
from typing import Any


PAGE_LIFE_JOB_TTL_SECONDS = 15 * 60
PAGE_LIFE_RETAINED_TERMINAL_JOBS = 3
PAGE_LIFE_MAX_REQUEST_IDS_PER_JOB = 16
PAGE_LIFE_PHASES = {"preparing", "context", "image", "caption", "saving"}
PAGE_LIFE_STATES = {"running", "succeeded", "failed", "cancelled"}
PAGE_LIFE_PUBLISH_STATES = {"ready", "sending", "published", "unknown"}

DIAGNOSTIC_MESSAGES = {
    "INTERFACE_ERROR": "调用接口时发生错误",
    "REQUEST_ERROR": "请求处理失败",
    "SOURCE_OR_FORMAT_ERROR": "媒体来源或格式未通过处理",
    "AUTH_REQUIRED": "登录状态需要重新确认",
    "DAEMON_UNAVAILABLE": "后台服务暂不可用",
    "TIMEOUT": "操作等待超时",
    "CANCELLED": "操作被取消，结果按未确认保留",
    "INTERNAL_ERROR": "内部处理发生错误",
    "SOURCE_TOO_LARGE": "图片来源超过大小限制",
    "ENCODED_SOURCE_TOO_LARGE": "图片编码内容超过大小限制",
    "SOURCE_MISSING": "图片来源缺失",
    "SOURCE_UNTRUSTED": "图片来源未通过本地校验",
    "PIXEL_LIMIT": "图片尺寸超过处理限制",
    "RASTER_UNSUPPORTED": "图片格式不受支持",
    "RASTER_DECODE_FAILED": "图片解码失败",
    "OUTPUT_TOO_LARGE": "预览图片超过输出限制",
    "REMOTE_POLICY_FAILED": "图片来源校验未通过",
    "PREVIEW_TIMEOUT": "预览读取或处理超时",
}
DIAGNOSTIC_PHASES = {
    "controller_call", "result_processing", "bookkeeping",
    "preview_read", "preview_raster", "preview_wait",
}


def public_diagnostic(record: Any) -> dict[str, Any] | None:
    """Return only the fixed public diagnostic shape; discard malformed records."""
    try:
        if type(record) is not dict or any(type(key) is not str for key in record) or set(record) != {"id", "code", "phase", "message", "at"}:
            return None
        ident, code, phase, message, at = (record.get(k) for k in ("id", "code", "phase", "message", "at"))
        valid_time = type(at) in {int, float} and 0 <= at <= 1_000_000_000_000 and math.isfinite(float(at))
        if (type(ident) is not str or len(ident) != 12 or any(c not in "0123456789abcdef" for c in ident)
                or type(code) is not str or code not in DIAGNOSTIC_MESSAGES
                or type(phase) is not str or phase not in DIAGNOSTIC_PHASES
                or type(message) is not str or message != DIAGNOSTIC_MESSAGES[code] or not valid_time):
            return None
        return {"id": ident, "code": code, "phase": phase, "message": message, "at": at}
    except Exception:
        return None


class PageLifeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class PageLifeJob:
    id: str
    request_ids: set[str]
    account_uin: int
    state: str
    phase: str
    message: str
    created_at: float
    updated_at: float
    completed_at: float = 0.0
    task: asyncio.Task[Any] | None = None
    draft_id: int = 0
    has_media: bool = False
    content: str = ""
    media: list[dict[str, Any]] = field(default_factory=list)
    media_sources: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    publish_state: str | None = None
    publish_digest: str = field(default="", repr=False)
    publish_result: dict[str, Any] | None = None
    draft_save_started: bool = False
    thumbnail_cache: dict[str, bytes] = field(default_factory=dict, repr=False)
    publish_diagnostic: dict[str, Any] | None = None
    preview_diagnostic: dict[str, Any] | None = None
    preview_diagnostic_key: str = field(default="", repr=False)


class PageLifeJobStore:
    """One active task, retained request idempotency and a small terminal cache."""

    def __init__(self, *, clock=time.time) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self._jobs: dict[str, PageLifeJob] = {}
        self._request_to_job: dict[str, str] = {}
        self._active_job_id = ""
        self._closed = False

    @staticmethod
    def _new_id(prefix: str) -> str:
        return prefix + secrets.token_urlsafe(18)

    def _prune_locked(self, now: float) -> None:
        terminal = [
            job for job in self._jobs.values()
            if job.state != "running" and job.publish_state != "sending"
        ]
        expired = [job for job in terminal if job.completed_at and now - job.completed_at >= PAGE_LIFE_JOB_TTL_SECONDS]
        for job in expired:
            self._drop_locked(job.id)
        terminal = sorted(
            (
                job for job in self._jobs.values()
                if job.state != "running" and job.publish_state != "sending"
            ),
            key=lambda job: (job.completed_at or job.updated_at, job.created_at),
            reverse=True,
        )
        for job in terminal[PAGE_LIFE_RETAINED_TERMINAL_JOBS:]:
            self._drop_locked(job.id)

    def _drop_locked(self, job_id: str) -> None:
        job = self._jobs.pop(job_id, None)
        if job is None:
            return
        for request_id in job.request_ids:
            if self._request_to_job.get(request_id) == job_id:
                self._request_to_job.pop(request_id, None)
        job.media_sources.clear()
        job.thumbnail_cache.clear()
        if self._active_job_id == job_id:
            self._active_job_id = ""

    async def admit(
        self,
        request_id: str,
        account_uin: int,
        *,
        allow_new: bool = True,
        unavailable_message: str = "日常草稿依赖尚未就绪。",
    ) -> tuple[PageLifeJob, bool]:
        if not isinstance(request_id, str) or not request_id or int(account_uin or 0) <= 0:
            raise PageLifeError("LIFE_INVALID_OWNER", "请先绑定有效 QQ 账号并重新提交。")
        now = self._clock()
        async with self._lock:
            self._prune_locked(now)
            if self._closed:
                raise PageLifeError("LIFE_CLOSED", "日常草稿服务正在关闭，请稍后重试。")
            existing_id = self._request_to_job.get(request_id)
            existing = self._jobs.get(existing_id or "")
            if existing is not None:
                if existing.account_uin != account_uin:
                    raise PageLifeError("LIFE_ACCOUNT_CHANGED", "登录账号已变化，请刷新页面后重试。")
                return existing, False
            active = self._jobs.get(self._active_job_id)
            if active is not None and active.state == "running":
                if active.account_uin != account_uin:
                    raise PageLifeError("LIFE_BUSY", "另一个账号的日常草稿正在生成，请稍后重试。")
                if len(active.request_ids) >= PAGE_LIFE_MAX_REQUEST_IDS_PER_JOB:
                    raise PageLifeError("LIFE_BUSY", "日常草稿请求过多，请稍后重试。")
                active.request_ids.add(request_id)
                self._request_to_job[request_id] = active.id
                return active, False
            if not allow_new:
                raise PageLifeError("LIFE_UNAVAILABLE", unavailable_message)
            terminal = [job for job in self._jobs.values() if job.state != "running"]
            while len(terminal) >= PAGE_LIFE_RETAINED_TERMINAL_JOBS:
                evictable = [job for job in terminal if job.publish_state != "sending"]
                if not evictable:
                    raise PageLifeError("LIFE_BUSY", "日常草稿发布正在处理中，请稍后重试。")
                oldest = min(evictable, key=lambda job: (job.completed_at or job.updated_at, job.created_at))
                self._drop_locked(oldest.id)
                terminal = [job for job in self._jobs.values() if job.state != "running"]
            job_id = self._new_id("life_")
            while job_id in self._jobs:
                job_id = self._new_id("life_")
            job = PageLifeJob(
                id=job_id,
                request_ids={request_id},
                account_uin=account_uin,
                state="running",
                phase="preparing",
                message="正在准备日常草稿…",
                created_at=now,
                updated_at=now,
            )
            self._jobs[job.id] = job
            self._request_to_job[request_id] = job.id
            self._active_job_id = job.id
            return job, True

    async def attach_task(self, job_id: str, task: asyncio.Task[Any]) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if self._closed or job is None or job.state != "running":
                task.cancel()
                return
            job.task = task

    async def active_or_latest(self, account_uin: int) -> PageLifeJob | None:
        now = self._clock()
        async with self._lock:
            self._prune_locked(now)
            active = self._jobs.get(self._active_job_id)
            if active is not None and active.state == "running":
                if active.account_uin != account_uin:
                    return None
                return active
            jobs = [job for job in self._jobs.values() if job.account_uin == account_uin]
            return max(jobs, key=lambda job: job.created_at, default=None)

    async def retained_ids(self) -> set[str]:
        async with self._lock:
            self._prune_locked(self._clock())
            return set(self._jobs)

    async def can_cache_thumbnail(self, job_id: str) -> bool:
        async with self._lock:
            self._prune_locked(self._clock())
            job = self._jobs.get(job_id)
            return not self._closed and job is not None and job.state == "succeeded"

    async def get(self, job_id: str, account_uin: int) -> PageLifeJob:
        async with self._lock:
            self._prune_locked(self._clock())
            job = self._jobs.get(str(job_id or ""))
            if job is None:
                raise PageLifeError("LIFE_EXPIRED", "日常草稿任务已过期，请明确重新生成。")
            if job.account_uin != account_uin:
                raise PageLifeError("LIFE_ACCOUNT_CHANGED", "登录账号已变化，请刷新页面后重试。")
            return job

    async def set_phase(self, job_id: str, phase: str) -> bool:
        if phase not in PAGE_LIFE_PHASES:
            return False
        async with self._lock:
            job = self._jobs.get(job_id)
            if self._closed or job is None or job.state != "running":
                return False
            job.phase = phase
            job.message = {
                "preparing": "正在准备日常草稿…",
                "context": "正在读取生活上下文…",
                "image": "正在准备图片…",
                "caption": "正在生成配文…",
                "saving": "正在保存草稿…",
            }[phase]
            job.updated_at = self._clock()
            return True

    async def begin_draft_save(self, job_id: str, account_uin: int) -> PageLifeJob:
        async with self._lock:
            job = self._jobs.get(job_id)
            if self._closed or job is None or job.state != "running":
                raise PageLifeError("LIFE_CANCELLED", "日常草稿生成已取消。")
            if job.account_uin != account_uin:
                raise PageLifeError("LIFE_ACCOUNT_CHANGED", "登录账号已变化，草稿未保存。")
            if job.draft_save_started:
                raise PageLifeError("LIFE_DUPLICATE_SAVE", "日常草稿正在保存。")
            job.draft_save_started = True
            job.phase = "saving"
            job.message = "正在保存草稿…"
            job.updated_at = self._clock()
            return job

    async def succeed(
        self,
        job_id: str,
        *,
        draft_id: int,
        content: str,
        media: list[dict[str, Any]],
        media_sources: dict[str, dict[str, Any]],
        has_media: bool,
        message: str,
    ) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if self._closed or job is None or job.state != "running":
                return False
            job.state = "succeeded"
            job.phase = ""
            job.message = message
            job.draft_id = int(draft_id)
            job.content = content
            job.media = [dict(item) for item in media]
            job.media_sources = {key: dict(value) for key, value in media_sources.items()}
            job.has_media = bool(has_media)
            job.publish_state = "ready"
            job.updated_at = self._clock()
            job.completed_at = job.updated_at
            if self._active_job_id == job_id:
                self._active_job_id = ""
            self._prune_locked(job.updated_at)
            return True

    async def fail(self, job_id: str, message: str = "日常草稿生成失败，请检查配置后重试。") -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if self._closed or job is None or job.state != "running":
                return False
            job.state = "failed"
            job.phase = ""
            job.message = message
            job.updated_at = self._clock()
            job.completed_at = job.updated_at
            if self._active_job_id == job_id:
                self._active_job_id = ""
            self._prune_locked(job.updated_at)
            return True

    async def cancel_generation(self, job_id: str) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state != "running":
                return False
            job.state = "cancelled"
            job.phase = ""
            job.message = "日常草稿生成已取消。"
            job.updated_at = self._clock()
            job.completed_at = job.updated_at
            if self._active_job_id == job_id:
                self._active_job_id = ""
            task = job.task
        if task is not None and not task.done():
            task.cancel()
        return True

    async def begin_publish(self, job_id: str, account_uin: int, digest: str) -> tuple[PageLifeJob, bool]:
        async with self._lock:
            self._prune_locked(self._clock())
            if self._closed:
                raise PageLifeError("LIFE_CLOSED", "日常草稿服务正在关闭，请稍后重试。")
            job = self._jobs.get(job_id)
            if job is None:
                raise PageLifeError("LIFE_EXPIRED", "日常草稿任务已过期，请重新生成。")
            if job.account_uin != account_uin:
                raise PageLifeError("LIFE_ACCOUNT_CHANGED", "登录账号已变化，不能发布该日常草稿。")
            if job.state != "succeeded":
                raise PageLifeError("LIFE_NOT_READY", "日常草稿尚未生成完成。")
            if job.publish_state == "published":
                if job.publish_digest == digest:
                    return job, False
                raise PageLifeError("LIFE_ALREADY_PUBLISHED", "该日常草稿已发布；编辑后的内容请重新生成草稿。")
            if job.publish_state == "sending":
                raise PageLifeError("LIFE_SENDING", "日常草稿正在发布，请稍后查询状态。")
            if job.publish_state == "unknown":
                diagnostic = public_diagnostic(job.publish_diagnostic)
                suffix = f"{diagnostic['code']} {diagnostic['id']}" if diagnostic else ""
                raise PageLifeError("LIFE_UNKNOWN", f"发布结果尚不确定，请先查询空间状态，不要重复提交。{suffix}")
            if job.publish_state != "ready":
                raise PageLifeError("LIFE_NOT_READY", "日常草稿当前不可发布。")
            job.publish_state = "sending"
            job.publish_digest = digest
            job.updated_at = self._clock()
            return job, True

    async def mark_published(self, job_id: str, result: dict[str, Any]) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.publish_state != "sending":
                return False
            job.publish_state = "published"
            job.publish_result = dict(result)
            job.updated_at = self._clock()
            job.completed_at = job.updated_at
            self._prune_locked(job.updated_at)
            return True

    async def mark_unknown(self, job_id: str, *, diagnostic: dict[str, Any] | None = None) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            safe = public_diagnostic(diagnostic)
            if job.publish_state == "sending":
                job.publish_state = "unknown"
                job.publish_diagnostic = safe
                job.message = "发布结果尚不确定，请查询空间状态；系统不会自动重试。"
                job.updated_at = self._clock()
                job.completed_at = job.updated_at
                self._prune_locked(job.updated_at)
            elif job.publish_state == "published" and safe is not None and safe["phase"] == "bookkeeping" and job.publish_diagnostic is None:
                # The send result is already recorded; retain the bookkeeping problem without downgrading it.
                job.publish_diagnostic = safe
                job.updated_at = self._clock()
                job.completed_at = job.updated_at
                self._prune_locked(job.updated_at)

    async def mark_preview_diagnostic(self, job_id: str, key: str, diagnostic: dict[str, Any]) -> dict[str, Any] | None:
        async with self._lock:
            self._prune_locked(self._clock())
            job = self._jobs.get(job_id)
            if self._closed or job is None or job.state != "succeeded":
                return None
            safe = public_diagnostic(diagnostic)
            if safe is None:
                return None
            previous = public_diagnostic(job.preview_diagnostic)
            if (previous is not None and job.preview_diagnostic_key == key
                    and previous["code"] == safe["code"] and previous["phase"] == safe["phase"]):
                return dict(previous)
            job.preview_diagnostic = safe
            job.preview_diagnostic_key = key
            job.updated_at = self._clock()
            return dict(safe)

    @staticmethod
    def public_payload(job: PageLifeJob, *, include_result: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": job.id,
            "state": job.state,
            "phase": job.phase,
            "message": job.message,
            "created_at": int(job.created_at),
            "updated_at": int(job.updated_at),
            "account_uin": job.account_uin,
            "draft_id": job.draft_id or None,
            "has_media": job.has_media,
            "publish_state": job.publish_state,
            "publish_diagnostic": public_diagnostic(job.publish_diagnostic),
            "preview_diagnostic": public_diagnostic(job.preview_diagnostic),
        }
        if include_result and job.state == "succeeded":
            payload["content"] = job.content
            public_media_fields = {"kind", "name", "mime_type", "upload_id", "media_id", "size"}
            payload["media"] = [
                {key: value for key, value in item.items() if key in public_media_fields}
                for item in job.media
            ]
            if job.publish_state == "published" and job.publish_result is not None:
                result_fields = {"message", "fid", "media_count", "photo_count", "post"}
                payload["publish_result"] = {
                    key: value for key, value in job.publish_result.items() if key in result_fields
                }
        return payload

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            tasks = [job.task for job in self._jobs.values() if job.task is not None and not job.task.done()]
            for job in self._jobs.values():
                if job.state == "running":
                    job.state = "cancelled"
                    job.phase = ""
                    job.message = "插件已关闭，日常草稿生成已取消。"
                    job.completed_at = self._clock()
                    job.updated_at = job.completed_at
                if job.publish_state != "sending":
                    job.media_sources.clear()
                    job.thumbnail_cache.clear()
            self._active_job_id = ""
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.publish_state != "sending":
                    self._drop_locked(job_id)
