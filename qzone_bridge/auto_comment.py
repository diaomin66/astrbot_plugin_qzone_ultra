"""Auto-comment persistence and decision pipeline."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .json_store import AtomicItemStoreFile
from .social import QzonePost
from .utils import truncate


GenerateStageText = Callable[[str, str, str], Awaitable[str]]
ExecuteCommentText = Callable[[str], Awaitable[str]]

SERIOUS_KEYWORDS = (
    "rip",
    "funeral",
    "surgery",
    "hospital",
    "accident",
    "cancer",
    "depression",
    "\u53bb\u4e16",
    "\u8ba3\u544a",
    "\u846c\u793c",
    "\u60bc\u5ff5",
    "\u54c0\u60bc",
    "\u4e00\u8def\u8d70\u597d",
    "\u4f4f\u9662",
    "\u624b\u672f",
    "\u751f\u75c5",
    "\u764c",
    "\u6291\u90c1",
    "\u96be\u8fc7",
    "\u5d29\u6e83",
    "\u5206\u624b",
    "\u5931\u4e1a",
    "\u4e8b\u6545",
    "\u8f66\u7978",
)
CHECKIN_KEYWORDS = (
    "\u6253\u5361",
    "\u7b7e\u5230",
    "\u65e9\u5b89",
    "\u665a\u5b89",
    "\u4e0a\u73ed",
    "\u4e0b\u73ed",
)


@dataclass(slots=True)
class AutoCommentPipelineConfig:
    enabled: bool = True
    judgment_provider_id: str = ""
    reasoning_provider_id: str = ""
    execution_provider_id: str = ""
    skip_checkins: bool = True
    max_comment_length: int = 60


@dataclass(slots=True)
class AutoCommentPipelineResult:
    should_comment: bool
    comment_text: str = ""
    judgment: str = ""
    reasoning: str = ""
    skip_reason: str = ""


class AutoCommentStateStore:
    def __init__(self, path: Path, *, max_items: int = 500):
        self.path = Path(path)
        self.max_items = max(1, int(max_items or 500))
        self._store = AtomicItemStoreFile(self.path)

    def read_keys(self) -> set[str]:
        payload = self._store.read()
        items = payload.get("commented") if isinstance(payload, dict) else []
        if items is None:
            items = payload.get("items") if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return set()
        keys: set[str] = set()
        for item in items:
            if isinstance(item, dict):
                value = item.get("key")
            else:
                value = item
            if value:
                keys.add(str(value))
        return keys

    def write_keys(self, keys: set[str]) -> None:
        self._store.write({"commented": sorted(str(key) for key in keys if key)[-self.max_items :]})


def _compact_text(value: str) -> str:
    return re.sub(r"[\s\u3000]+", "", str(value or "")).strip()


def _post_context(post: QzonePost) -> str:
    comments = [comment.content for comment in post.comments if comment.content][:5]
    parts = [
        f"author={post.nickname or post.hostuin}",
        f"text={truncate(post.summary or '', 500)}",
        f"images={len(post.images)}",
        f"likes={post.like_count}",
        f"comments={post.comment_count}",
    ]
    if comments:
        parts.append("visible_comments=" + " | ".join(truncate(item, 80) for item in comments))
    return "\n".join(parts)


def _heuristic_skip_reason(post: QzonePost, *, skip_checkins: bool = True) -> str:
    text = " ".join(
        part
        for part in (
            post.summary,
            post.nickname,
            " ".join(comment.content for comment in post.comments if comment.content),
        )
        if part
    )
    compact = _compact_text(text)
    lowered = compact.lower()
    if not compact and not post.images:
        return "empty_post"
    if any(keyword in lowered or keyword in compact for keyword in SERIOUS_KEYWORDS):
        return "serious_or_sensitive_context"
    if skip_checkins and not post.images and len(compact) <= 16:
        if any(keyword in compact for keyword in CHECKIN_KEYWORDS):
            return "low_signal_checkin"
    return ""


def _strip_code_fence(text: str) -> str:
    value = str(text or "").strip()
    match = re.fullmatch(r"```(?:\w+)?\s*(.*?)\s*```", value, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return value


def _parse_judgment(text: str) -> tuple[bool | None, str]:
    raw = _strip_code_fence(text)
    if not raw:
        return None, ""
    try:
        payload = json.loads(raw)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        action = str(payload.get("action") or payload.get("decision") or "").strip().lower()
        should_comment = payload.get("should_comment")
        reason = str(payload.get("reason") or payload.get("risk") or "").strip()
        if isinstance(should_comment, bool):
            return should_comment, reason
        if action in {"comment", "reply", "yes", "true"}:
            return True, reason
        if action in {"skip", "ignore", "no", "false"}:
            return False, reason or "llm_judgment_skip"
    lowered = raw.lower()
    if re.search(r"\b(skip|ignore|do not comment|no comment)\b", lowered):
        return False, raw[:120]
    if re.search(r"\b(comment|reply|safe|yes)\b", lowered):
        return True, raw[:120]
    return None, raw[:120]


def _clean_reasoning(text: str) -> str:
    value = _strip_code_fence(text)
    value = re.sub(r"\s+", " ", value).strip()
    return truncate(value, 500)


def clean_comment_text(text: str, *, max_length: int = 60) -> str:
    value = _strip_code_fence(text)
    try:
        payload = json.loads(value)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        for key in ("comment", "content", "text", "message"):
            item = payload.get(key)
            if isinstance(item, str) and item.strip():
                value = item
                break
    value = re.sub(r"^\s*(?:comment|content|text|message)\s*[:=]\s*", "", value, flags=re.I)
    value = re.sub(r"[\r\n\t]+", " ", value)
    value = value.strip().strip("\"'")
    return truncate(value, max(1, int(max_length or 60))).strip()


class AutoCommentPipeline:
    def __init__(self, config: AutoCommentPipelineConfig):
        self.config = config

    async def run(
        self,
        post: QzonePost,
        *,
        generate_text: GenerateStageText,
        execute_comment: ExecuteCommentText,
    ) -> AutoCommentPipelineResult:
        if not self.config.enabled:
            text = clean_comment_text(await execute_comment(""), max_length=self.config.max_comment_length)
            return AutoCommentPipelineResult(bool(text), comment_text=text, judgment="disabled")

        heuristic_reason = _heuristic_skip_reason(post, skip_checkins=self.config.skip_checkins)
        if heuristic_reason:
            return AutoCommentPipelineResult(False, judgment="heuristic", skip_reason=heuristic_reason)

        judgment_prompt = (
            "You are judging whether an automatic QQ Zone comment is socially appropriate.\n"
            "Return compact JSON only: {\"action\":\"comment|skip\",\"reason\":\"...\"}.\n"
            "Skip sad, serious, crisis, medical, grief, conflict, private, political, or low-signal check-in posts.\n\n"
            f"{_post_context(post)}"
        )
        judgment_text = await generate_text(
            judgment_prompt,
            self.config.judgment_provider_id,
            "Decide whether to comment. Be conservative.",
        )
        should_comment, reason = _parse_judgment(judgment_text)
        if should_comment is False:
            return AutoCommentPipelineResult(False, judgment=judgment_text, skip_reason=reason or "llm_judgment_skip")

        reasoning_prompt = (
            "Plan one natural QQ Zone comment.\n"
            "Describe relationship stance, emotional tone, and one content angle in one short sentence.\n"
            "Do not write the final comment yet.\n\n"
            f"{_post_context(post)}"
        )
        reasoning = _clean_reasoning(
            await generate_text(
                reasoning_prompt,
                self.config.reasoning_provider_id,
                "Reason about tone and relationship before writing.",
            )
        )
        text = clean_comment_text(await execute_comment(reasoning), max_length=self.config.max_comment_length)
        return AutoCommentPipelineResult(bool(text), comment_text=text, judgment=judgment_text, reasoning=reasoning)
