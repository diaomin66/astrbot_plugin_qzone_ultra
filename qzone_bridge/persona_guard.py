"""Strict, bounded helpers for reviewing final persona-aware artifacts."""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


class PersonaConsistencyError(RuntimeError):
    """Fixed public failure for persona review/projection operations."""

    def __init__(self, code: str = "REVIEW_FAILED"):
        self.code = code if code in {"REVIEW_FAILED", "PROJECTION_FAILED", "BUDGET_EXHAUSTED"} else "REVIEW_FAILED"
        super().__init__(f"人格一致性检查失败（{self.code}）")


@dataclass(slots=True)
class GenerationScope:
    deadline: float
    max_calls: int = 6
    calls: int = 0

    def consume(self) -> None:
        if self.calls >= self.max_calls or time.monotonic() >= self.deadline:
            raise PersonaConsistencyError("BUDGET_EXHAUSTED")
        self.calls += 1


_scope: contextvars.ContextVar[GenerationScope | None] = contextvars.ContextVar("qzone_generation_scope", default=None)
_snapshot: contextvars.ContextVar[Any] = contextvars.ContextVar("qzone_generation_snapshot", default=None)


@asynccontextmanager
async def generation_scope(*, timeout: float = 90.0):
    """Nest scopes without resetting the parent's call count or deadline."""
    parent = _scope.get()
    deadline = min(parent.deadline, time.monotonic() + timeout) if parent else time.monotonic() + timeout
    scope = parent or GenerationScope(deadline=deadline)
    previous_deadline = scope.deadline
    if parent:
        scope.deadline = deadline
    token = _scope.set(scope)
    snapshot_token = _snapshot.set(_snapshot.get())
    task = asyncio.current_task()
    timed_out = False

    def cancel_at_deadline() -> None:
        nonlocal timed_out
        timed_out = True
        if task is not None and not task.done():
            task.cancel()

    loop = asyncio.get_running_loop()
    timer = loop.call_later(max(0.001, deadline - time.monotonic()), cancel_at_deadline)
    try:
        yield scope
    except asyncio.CancelledError:
        if timed_out:
            raise PersonaConsistencyError("BUDGET_EXHAUSTED") from None
        raise
    finally:
        timer.cancel()
        _snapshot.reset(snapshot_token)
        _scope.reset(token)
        if parent:
            scope.deadline = previous_deadline


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
        remaining = scope.deadline - time.monotonic()
        if remaining <= 0:
            raise PersonaConsistencyError("BUDGET_EXHAUSTED")
        timeout = min(timeout, remaining) if timeout is not None else remaining
    if timeout is None:
        return await call()
    try:
        return await asyncio.wait_for(call(), timeout=max(0.001, timeout))
    except asyncio.TimeoutError:
        raise PersonaConsistencyError("BUDGET_EXHAUSTED") from None


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
