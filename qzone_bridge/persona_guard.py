"""Strict, bounded helpers for reviewing final persona-aware artifacts."""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal


TimeoutKind = Literal["generic", "total", "image", "text"]
TimeoutPhase = Literal["context", "image_prompt", "image", "caption", "generation"]
_TIMEOUT_KINDS = {"generic", "total", "image", "text"}
_TIMEOUT_PHASES = {"context", "image_prompt", "image", "caption", "generation"}


class PersonaConsistencyError(RuntimeError):
    """Fixed public failure for persona review/projection operations."""

    def __init__(self, code: str = "REVIEW_FAILED"):
        self.code = code if code in {"REVIEW_FAILED", "PROJECTION_FAILED", "BUDGET_EXHAUSTED"} else "REVIEW_FAILED"
        super().__init__(f"人格一致性检查失败（{self.code}）")


class GenerationTimeoutError(PersonaConsistencyError):
    """An owned asyncio deadline expired, with safe fixed classification."""

    def __init__(self, *, kind: str = "generic", phase: str = "generation"):
        self.kind: TimeoutKind = kind if kind in _TIMEOUT_KINDS else "generic"  # type: ignore[assignment]
        self.phase: TimeoutPhase = phase if phase in _TIMEOUT_PHASES else "generation"  # type: ignore[assignment]
        super().__init__("BUDGET_EXHAUSTED")


@dataclass(slots=True)
class GenerationScope:
    deadline: float
    max_calls: int = 6
    calls: int = 0
    deadline_kind: TimeoutKind = "generic"
    phase: TimeoutPhase = "generation"
    last_phase: TimeoutPhase = "generation"

    def consume(self) -> None:
        if self.calls >= self.max_calls:
            raise PersonaConsistencyError("BUDGET_EXHAUSTED")
        loop = asyncio.get_running_loop()
        if loop.time() >= self.deadline:
            raise GenerationTimeoutError(kind=self.deadline_kind, phase=self.phase)
        self.calls += 1


_scope: contextvars.ContextVar[GenerationScope | None] = contextvars.ContextVar("qzone_generation_scope", default=None)
_snapshot: contextvars.ContextVar[Any] = contextvars.ContextVar("qzone_generation_snapshot", default=None)


@asynccontextmanager
async def _deadline_context(scope: GenerationScope, *, phase: str | None = None):
    """Translate only this timeout context's own cancellation into a typed error."""
    timeout_cm = asyncio.timeout_at(scope.deadline)
    task = asyncio.current_task()
    cancellation_baseline = task.cancelling() if task is not None else 0
    try:
        async with timeout_cm:
            yield
    except TimeoutError:
        if timeout_cm.expired():
            raise GenerationTimeoutError(kind=scope.deadline_kind, phase=phase or scope.last_phase) from None
        raise
    else:
        if task is not None and task.cancelling() > cancellation_baseline:
            raise asyncio.CancelledError
        if timeout_cm.expired() or asyncio.get_running_loop().time() >= scope.deadline:
            raise GenerationTimeoutError(kind=scope.deadline_kind, phase=phase or scope.last_phase)


@asynccontextmanager
async def generation_scope(*, timeout: float = 90.0, kind: str = "generic", phase: str = "generation"):
    """Nest scopes without resetting the parent's call count or deadline."""
    loop = asyncio.get_running_loop()
    parent = _scope.get()
    local_deadline = loop.time() + max(0.0, float(timeout))
    deadline = min(parent.deadline, local_deadline) if parent else local_deadline
    parent_wins = parent is not None and parent.deadline <= local_deadline
    scope = parent or GenerationScope(deadline=deadline)
    previous = (scope.deadline, scope.deadline_kind, scope.phase)
    if parent:
        scope.deadline = deadline
        if not parent_wins:
            scope.deadline_kind = kind if kind in _TIMEOUT_KINDS else "generic"  # type: ignore[assignment]
            scope.phase = phase if phase in _TIMEOUT_PHASES else "generation"  # type: ignore[assignment]
    else:
        scope.deadline_kind = kind if kind in _TIMEOUT_KINDS else "generic"  # type: ignore[assignment]
        scope.phase = phase if phase in _TIMEOUT_PHASES else "generation"  # type: ignore[assignment]
        scope.last_phase = scope.phase
    token = _scope.set(scope)
    snapshot_token = _snapshot.set(_snapshot.get())
    try:
        async with _deadline_context(scope):
            yield scope
    finally:
        _snapshot.reset(snapshot_token)
        _scope.reset(token)
        if parent:
            scope.deadline, scope.deadline_kind, scope.phase = previous


@asynccontextmanager
async def generation_phase(*, timeout: float, phase: str, kind: str):
    """Temporarily tighten the active deadline without changing snapshot context."""
    scope = _scope.get()
    if scope is None:
        raise RuntimeError("generation_phase requires an active generation scope")
    loop = asyncio.get_running_loop()
    local_deadline = loop.time() + max(0.0, float(timeout))
    previous = (scope.deadline, scope.deadline_kind, scope.phase)
    parent_wins = scope.deadline <= local_deadline
    scope.deadline = min(scope.deadline, local_deadline)
    scope.last_phase = phase if phase in _TIMEOUT_PHASES else "generation"  # type: ignore[assignment]
    scope.phase = phase if phase in _TIMEOUT_PHASES else "generation"  # type: ignore[assignment]
    if not parent_wins:
        scope.deadline_kind = kind if kind in _TIMEOUT_KINDS else "generic"  # type: ignore[assignment]
    try:
        async with _deadline_context(scope, phase=phase):
            yield scope
    finally:
        scope.deadline, scope.deadline_kind, scope.phase = previous


def current_scope() -> GenerationScope | None:
    return _scope.get()


def get_snapshot() -> Any:
    return _snapshot.get()


def set_snapshot(snapshot: Any) -> None:
    _snapshot.set(snapshot)


async def invoke_provider(call: Callable[[], Awaitable[Any]], *, timeout: float | None = None) -> Any:
    scope = current_scope()
    if scope is not None:
        scope.consume()
        remaining = scope.deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise GenerationTimeoutError(kind=scope.deadline_kind, phase=scope.phase)
        timeout = min(timeout, remaining) if timeout is not None else remaining
    if timeout is None:
        return await call()
    timeout_cm = asyncio.timeout(timeout)
    task = asyncio.current_task()
    cancellation_baseline = task.cancelling() if task is not None else 0
    try:
        async with timeout_cm:
            result = await call()
    except TimeoutError:
        if timeout_cm.expired():
            if scope is not None and scope.deadline <= asyncio.get_running_loop().time():
                raise GenerationTimeoutError(kind=scope.deadline_kind, phase=scope.phase) from None
            raise GenerationTimeoutError(kind="generic", phase="generation") from None
        raise
    if task is not None and task.cancelling() > cancellation_baseline:
        raise asyncio.CancelledError
    loop = asyncio.get_running_loop()
    if timeout_cm.expired():
        if scope is not None and scope.deadline <= loop.time():
            raise GenerationTimeoutError(kind=scope.deadline_kind, phase=scope.phase)
        raise GenerationTimeoutError(kind="generic", phase="generation")
    if scope is not None and loop.time() >= scope.deadline:
        raise GenerationTimeoutError(kind=scope.deadline_kind, phase=scope.phase)
    return result


def parse_review(value: str, *, max_rewrite: int = 2000) -> tuple[bool, str]:
    text = str(value or "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        payload = json.loads(text)
    except Exception:
        raise PersonaConsistencyError() from None
    if not isinstance(payload, dict) or set(payload) != {"accepted", "rewrite"}:
        raise PersonaConsistencyError()
    if not isinstance(payload["accepted"], bool) or not isinstance(payload["rewrite"], str):
        raise PersonaConsistencyError()
    rewrite = payload["rewrite"].strip()
    if len(rewrite) > max_rewrite or (not payload["accepted"] and not rewrite):
        raise PersonaConsistencyError()
    return payload["accepted"], rewrite


def parse_visual_projection(value: str, persona: str, *, max_total: int = 2400) -> dict[str, list[str]]:
    text = str(value or "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        payload = json.loads(text)
    except Exception:
        raise PersonaConsistencyError("PROJECTION_FAILED") from None
    fields = ("appearance", "accessories", "clothing")
    if not isinstance(payload, dict) or set(payload) != set(fields):
        raise PersonaConsistencyError("PROJECTION_FAILED")
    result: dict[str, list[str]] = {}
    total = 0
    source = str(persona or "")
    for field in fields:
        values = payload[field]
        if not isinstance(values, list) or len(values) > 12:
            raise PersonaConsistencyError("PROJECTION_FAILED")
        normalized: list[str] = []
        for item in values:
            if not isinstance(item, str):
                raise PersonaConsistencyError("PROJECTION_FAILED")
            fragment = item.strip()
            if not fragment or len(fragment) > 160 or fragment not in source:
                raise PersonaConsistencyError("PROJECTION_FAILED")
            total += len(fragment)
            normalized.append(fragment)
        result[field] = normalized
    if total > max_total:
        raise PersonaConsistencyError("PROJECTION_FAILED")
    return result
