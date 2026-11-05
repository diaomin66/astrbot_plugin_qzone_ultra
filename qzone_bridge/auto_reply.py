"""Default-off, persistent reply service for comments on the account's own posts."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable

from .json_store import AtomicItemStoreFile
from .models import FeedEntry
from .social import QzoneComment, QzonePost, extract_comments, post_from_entry

logger = logging.getLogger(__name__)


GenerateReply = Callable[[QzonePost, QzoneComment], Awaitable[str]]


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


@dataclass(frozen=True, slots=True)
class AutoReplyConfig:
    enabled: bool = False
    poll_interval: int = 120
    delay_min: int = 30
    delay_max: int = 180
    recent_posts: int = 5
    max_per_run: int = 2
    daily_limit: int = 20
    skip_low_information: bool = True
    reply_probability: float = 0.8
    ignore_users: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings: Any) -> "AutoReplyConfig":
        delay_min = _int(getattr(settings, "auto_reply_delay_min", 30), 30, 0, 3600)
        delay_max = _int(getattr(settings, "auto_reply_delay_max", 180), 180, 0, 3600)
        if delay_max < delay_min:
            delay_max = delay_min
        try:
            probability = float(getattr(settings, "auto_reply_probability", 0.8))
            if not math.isfinite(probability):
                probability = 0.8
        except (TypeError, ValueError, OverflowError):
            probability = 0.8
        ignored = tuple(str(value).strip() for value in (getattr(settings, "auto_reply_ignore_users", ()) or ()) if str(value).strip())
        return cls(
            enabled=bool(getattr(settings, "auto_reply_enabled", False)),
            poll_interval=_int(getattr(settings, "auto_reply_poll_interval", 120), 120, 30, 3600),
            delay_min=delay_min,
            delay_max=delay_max,
            recent_posts=_int(getattr(settings, "auto_reply_recent_posts", 5), 5, 1, 20),
            max_per_run=_int(getattr(settings, "auto_reply_max_per_run", 2), 2, 1, 20),
            daily_limit=_int(getattr(settings, "auto_reply_daily_limit", 20), 20, 1, 500),
            skip_low_information=bool(getattr(settings, "auto_reply_skip_low_information", True)),
            reply_probability=max(0.0, min(1.0, probability)),
            ignore_users=ignored,
        )


class AutoReplyService:
    """Poll own-post comments, durably reserve sends, and avoid uncertain retries."""

    META_KEY = "qzone-auto-reply-state-v1"

    def __init__(
        self,
        *,
        controller: Any,
        state_path: Path,
        config: AutoReplyConfig,
        generate_reply: GenerateReply,
        reply_lock: asyncio.Lock | None = None,
        rng: Any = random,
    ):
        self.controller = controller
        self.store = AtomicItemStoreFile(Path(state_path))
        self.config = config
        self.generate_reply = generate_reply
        self.reply_lock = reply_lock or asyncio.Lock()
        self.rng = rng
        self._poll_lock = asyncio.Lock()
        self._manual_replied: set[str] = set()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not self.config.enabled or (self._task is not None and not self._task.done()):
            return
        self._task = asyncio.create_task(self._run(), name="qzone-auto-reply")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Qzone auto-reply poll failed (%s)", type(exc).__name__)
            await asyncio.sleep(self.config.poll_interval)

    async def _state(self) -> dict[str, Any]:
        payload = await self.store.read_async()
        for item in payload.get("items", []):
            if isinstance(item, dict) and item.get("key") == self.META_KEY and isinstance(item.get("state"), dict):
                return item["state"]
        return {"accounts": {}}

    async def _mutate_state(self, updater: Callable[[dict[str, Any]], Any]) -> Any:
        def mutate(payload: dict[str, Any]) -> Any:
            items = payload.setdefault("items", [])
            entry = next((item for item in items if isinstance(item, dict) and item.get("key") == self.META_KEY), None)
            if entry is None:
                entry = {"key": self.META_KEY, "state": {"accounts": {}}}
                items.append(entry)
            if not isinstance(entry.get("state"), dict):
                entry["state"] = {"accounts": {}}
            state = entry["state"]
            if not isinstance(state.get("accounts"), dict):
                state["accounts"] = {}
            return updater(state)
        return await self.store.transact_async(mutate)

    @staticmethod
    def _identity(login: int, entry: FeedEntry, comment: QzoneComment) -> str:
        return json.dumps(
            [login, entry.hostuin, entry.fid, comment.commentid, comment.uin],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    async def _fetch_entries(self, login: int) -> list[FeedEntry]:
        payload = await asyncio.wait_for(
            self.controller.list_feeds(
                hostuin=login,
                limit=self.config.recent_posts,
                scope="self",
                record_recent=False,
            ),
            timeout=25,
        )
        raw_items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(raw_items, list):
            raise RuntimeError("own-post feed response is not a list")
        entries = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            try:
                entry = FeedEntry(
                    hostuin=int(item.get("hostuin") or 0),
                    fid=str(item.get("fid") or ""),
                    appid=int(item.get("appid") or 311),
                    summary=str(item.get("summary") or ""),
                    nickname=str(item.get("nickname") or ""),
                    created_at=int(item.get("created_at") or 0),
                    like_count=int(item.get("like_count") or 0),
                    comment_count=int(item.get("comment_count") or 0),
                    liked=bool(item.get("liked")),
                    curkey=str(item.get("curkey") or ""),
                    unikey=str(item.get("unikey") or ""),
                    busi_param=item.get("busi_param") if isinstance(item.get("busi_param"), dict) else {},
                    raw=item,
                )
            except (TypeError, ValueError):
                continue
            if entry.hostuin == login and entry.fid:
                entries.append(entry)
        return entries

    async def _fresh_post(self, entry: FeedEntry) -> tuple[QzonePost, list[QzoneComment]]:
        detail = await asyncio.wait_for(
            self.controller.detail_feed(
                hostuin=entry.hostuin,
                fid=entry.fid,
                appid=entry.appid,
                busi_param=json.dumps(entry.busi_param, ensure_ascii=False, separators=(",", ":")) if entry.busi_param else "",
            ),
            timeout=25,
        )
        if not isinstance(detail, dict):
            raise RuntimeError("Qzone detail response is not an object")
        detail_entry = detail.get("entry")
        raw_comments = detail.get("comments")
        if not isinstance(detail_entry, dict) or not isinstance(raw_comments, list):
            raise RuntimeError("Qzone detail response is incomplete")
        if (
            int(detail_entry.get("hostuin") or 0) != entry.hostuin
            or str(detail_entry.get("fid") or "") != entry.fid
            or int(detail_entry.get("appid") or 0) != entry.appid
        ):
            raise RuntimeError("Qzone detail identity does not match the requested post")
        comments = extract_comments({"comments": raw_comments})
        source = detail_entry
        fresh_entry = FeedEntry(
            hostuin=_int(source.get("hostuin"), entry.hostuin, 1, 2**63 - 1),
            fid=str(source.get("fid") or entry.fid),
            appid=_int(source.get("appid"), entry.appid, 1, 100000),
            summary=str(source.get("summary") or entry.summary),
            nickname=str(source.get("nickname") or entry.nickname),
            created_at=_int(source.get("created_at"), entry.created_at, 0, 2**63 - 1),
            like_count=_int(source.get("like_count"), entry.like_count, 0, 2**31 - 1),
            comment_count=_int(source.get("comment_count"), entry.comment_count, 0, 2**31 - 1),
            liked=bool(source.get("liked", entry.liked)),
            curkey=str(source.get("curkey") or entry.curkey),
            unikey=str(source.get("unikey") or entry.unikey),
            busi_param=source.get("busi_param") if isinstance(source.get("busi_param"), dict) else entry.busi_param,
            raw=source,
        )
        post = post_from_entry(fresh_entry, detail=detail.get("raw") if isinstance(detail, dict) else None)
        post.comments = comments
        if isinstance(detail, dict):
            raw = detail.get("raw")
            if isinstance(raw, dict):
                post.raw = raw
        return post, comments

    async def _account_snapshot(self, login: int) -> dict[str, Any]:
        state = await self._state()
        accounts = state.get("accounts")
        account = accounts.get(str(login)) if isinstance(accounts, dict) else None
        return account if isinstance(account, dict) else {}

    async def _baseline(self, login: int, entries: list[FeedEntry], now: int) -> None:
        baselined: list[str] = []
        for entry in entries:
            _, comments = await self._fresh_post(entry)
            baselined.extend(self._identity(login, entry, comment) for comment in comments if comment.commentid and comment.uin)

        def update(state: dict[str, Any]) -> None:
            account = state["accounts"].setdefault(str(login), {})
            if not account.get("initialized"):
                account.update({"initialized": True, "activation_ts": now})
                records = account.setdefault("records", {})
                for key in baselined:
                    records.setdefault(key, {"status": "baseline", "created_at": now, "day": ""})
        await self._mutate_state(update)

    def _skip_reason(self, comment: QzoneComment, login: int, activation_ts: int) -> str:
        if not comment.commentid or not comment.uin:
            return "missing_identity"
        if comment.uin == login:
            return "own_comment"
        ignored_ids = {int(value) for value in self.config.ignore_users if value.isdigit()}
        ignored_names = {value.casefold() for value in self.config.ignore_users if not value.isdigit()}
        if comment.uin in ignored_ids or comment.nickname.casefold() in ignored_names:
            return "ignored_user"
        if comment.parent_id:
            return "nested_reply"
        if comment.created_at and comment.created_at < activation_ts:
            return "before_activation"
        if self.config.skip_low_information and len("".join(comment.content.split())) <= 2:
            return "low_information"
        return ""

    @staticmethod
    def _has_bot_reply(comments: list[QzoneComment], target: QzoneComment, login: int) -> bool:
        return any(item.parent_id == target.commentid and item.uin == login for item in comments)

    async def _reserve(self, login: int, key: str, today: str) -> bool:
        def update(state: dict[str, Any]) -> bool:
            account = state["accounts"].setdefault(str(login), {})
            records = account.setdefault("records", {})
            existing = records.get(key)
            if isinstance(existing, dict) and existing.get("status") in {"baseline", "reserved", "uncertain", "complete"}:
                return False
            used = sum(
                1 for value in records.values()
                if isinstance(value, dict) and value.get("day") == today
                and value.get("status") in {"reserved", "uncertain", "complete"}
            )
            if used >= self.config.daily_limit:
                return False
            records[key] = {"status": "reserved", "day": today, "updated_at": int(__import__("time").time())}
            return True
        return bool(await self._mutate_state(update))

    async def _finish(self, login: int, key: str, status: str) -> None:
        def update(state: dict[str, Any]) -> None:
            record = state["accounts"].setdefault(str(login), {}).setdefault("records", {}).setdefault(key, {})
            record.update({"status": status, "updated_at": int(__import__("time").time())})
        await self._mutate_state(update)

    async def _release_reservation(self, login: int, key: str) -> None:
        def update(state: dict[str, Any]) -> None:
            records = state["accounts"].setdefault(str(login), {}).setdefault("records", {})
            record = records.get(key)
            if isinstance(record, dict) and record.get("status") == "reserved":
                records.pop(key, None)
        await self._mutate_state(update)

    async def mark_manual_reply(self, login: int, hostuin: int, fid: str, commentid: str, comment_uin: int) -> bool:
        """Persist a successful manual reply so a stale detail snapshot cannot trigger auto-reply."""
        login, hostuin, comment_uin = int(login or 0), int(hostuin or 0), int(comment_uin or 0)
        if not login or hostuin != login or not fid or not commentid or not comment_uin:
            return False
        key = json.dumps([login, hostuin, str(fid), str(commentid), comment_uin], ensure_ascii=False, separators=(",", ":"))
        self._manual_replied.add(key)
        try:
            def update(state: dict[str, Any]) -> None:
                account = state["accounts"].setdefault(str(login), {})
                record = account.setdefault("records", {}).setdefault(key, {})
                record.update({"status": "complete", "updated_at": int(__import__("time").time()), "source": "manual"})
                record.setdefault("day", "")
            await self._mutate_state(update)
        except Exception as exc:
            logger.error("Could not persist manual Qzone reply marker (%s)", type(exc).__name__)
        return True

    async def poll_once(self) -> int:
        if not self.config.enabled:
            return 0
        async with self._poll_lock:
            status = await asyncio.wait_for(self.controller.get_status(probe_daemon=False), timeout=15)
            login = int(status.get("login_uin") or status.get("uin") or 0) if isinstance(status, dict) else 0
            if login <= 0 or status.get("needs_rebind"):
                return 0
            entries = await self._fetch_entries(login)
            now = int(__import__("time").time())
            account = await self._account_snapshot(login)
            if not account.get("initialized"):
                await self._baseline(login, entries, now)
                return 0
            activation_ts = int(account.get("activation_ts") or now)
            records = account.get("records") if isinstance(account.get("records"), dict) else {}
            handled = 0
            attempts = 0
            for entry in entries:
                post, comments = await self._fresh_post(entry)
                for comment in comments:
                    key = self._identity(login, entry, comment)
                    if key in records or key in self._manual_replied or self._skip_reason(comment, login, activation_ts):
                        continue
                    if self.rng.random() >= self.config.reply_probability:
                        await self._finish(login, key, "skipped")
                        continue
                    if attempts >= self.config.max_per_run:
                        return handled
                    if self.config.delay_max:
                        await asyncio.sleep(self.rng.uniform(self.config.delay_min, self.config.delay_max))
                    try:
                        text = str(await self.generate_reply(post, comment) or "").strip()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        continue  # LLM errors remain retryable.
                    if not text:
                        continue
                    async with self.reply_lock:
                        fresh_post, fresh_comments = await self._fresh_post(entry)
                        fresh_target = next(
                            (item for item in fresh_comments if item.commentid == comment.commentid and item.uin == comment.uin and not item.parent_id),
                            None,
                        )
                        if fresh_target is None or self._has_bot_reply(fresh_comments, fresh_target, login):
                            await self._finish(login, key, "complete")
                            continue
                        if key in self._manual_replied:
                            continue
                        # A delay or slow LLM can cross the local midnight boundary.
                        if not await self._reserve(login, key, date.today().isoformat()):
                            continue
                        try:
                            latest_status = await asyncio.wait_for(
                                self.controller.get_status(probe_daemon=False), timeout=15
                            )
                        except asyncio.CancelledError:
                            await asyncio.shield(self._release_reservation(login, key))
                            raise
                        except Exception:
                            await self._release_reservation(login, key)
                            raise
                        latest_login = int(latest_status.get("login_uin") or latest_status.get("uin") or 0) if isinstance(latest_status, dict) else 0
                        if not isinstance(latest_status, dict) or latest_login != login or latest_status.get("needs_rebind"):
                            await self._release_reservation(login, key)
                            return handled
                        attempts += 1
                        try:
                            await self.controller.reply_comment(
                                hostuin=entry.hostuin,
                                fid=entry.fid,
                                commentid=fresh_target.commentid,
                                comment_uin=fresh_target.uin,
                                content=text,
                                appid=entry.appid,
                            )
                        except asyncio.CancelledError:
                            await asyncio.shield(self._finish(login, key, "uncertain"))
                            raise
                        except Exception:
                            await self._finish(login, key, "uncertain")
                            continue
                        await self._finish(login, key, "complete")
                        handled += 1
            return handled
