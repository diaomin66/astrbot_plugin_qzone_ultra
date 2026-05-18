"""Persistent draft store for target-style campus-wall workflows."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from .json_store import AtomicItemStoreFile

DraftStatus = Literal["pending", "approved", "rejected", "recalled", "published"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class DraftPost:
    id: int
    author_uin: int
    author_name: str = ""
    group_id: int = 0
    content: str = ""
    media: list[dict[str, Any]] = field(default_factory=list)
    anonymous: bool = False
    status: DraftStatus = "pending"
    reject_reason: str = ""
    published_fid: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DraftPost":
        status = str(data.get("status") or "pending")
        if status not in {"pending", "approved", "rejected", "recalled", "published"}:
            status = "pending"
        return cls(
            id=int(data.get("id") or 0),
            author_uin=int(data.get("author_uin") or 0),
            author_name=str(data.get("author_name") or ""),
            group_id=int(data.get("group_id") or 0),
            content=str(data.get("content") or ""),
            media=list(data.get("media") or []),
            anonymous=bool(data.get("anonymous") or False),
            status=status,  # type: ignore[arg-type]
            reject_reason=str(data.get("reject_reason") or ""),
            published_fid=str(data.get("published_fid") or ""),
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
        )

    def title(self) -> str:
        author = "匿名投稿" if self.anonymous else (self.author_name or str(self.author_uin or "未知用户"))
        return f"稿件 #{self.id} · {author} · {self.status}"

    def preview(self, *, include_private: bool = True) -> str:
        lines = [self.title()]
        if include_private and not self.anonymous:
            lines.append(f"投稿人: {self.author_name or self.author_uin}")
        if self.group_id:
            lines.append(f"来源群: {self.group_id}")
        if self.content:
            lines.append(self.content)
        if self.media:
            lines.append(f"媒体: {len(self.media)} 个")
        if self.reject_reason:
            lines.append(f"拒绝原因: {self.reject_reason}")
        if self.published_fid:
            lines.append(f"已发布 fid: {self.published_fid}")
        return "\n".join(lines)


class DraftStore:
    def __init__(self, path: Path):
        self.path = path
        self._store = AtomicItemStoreFile(path)

    def _read_payload(self) -> dict[str, Any]:
        return self._store.read()

    def _write_payload(self, payload: dict[str, Any]) -> None:
        self._store.write(payload)

    async def _read_payload_async(self) -> dict[str, Any]:
        return await self._store.read_async()

    async def _write_payload_async(self, payload: dict[str, Any]) -> None:
        await self._store.write_async(payload)

    def list(self, *, status: str | None = None) -> list[DraftPost]:
        payload = self._read_payload()
        return self._items_from_payload(payload, status=status)

    async def list_async(self, *, status: str | None = None) -> list[DraftPost]:
        payload = await self._read_payload_async()
        return self._items_from_payload(payload, status=status)

    @staticmethod
    def _items_from_payload(payload: dict[str, Any], *, status: str | None = None) -> list[DraftPost]:
        items = [DraftPost.from_dict(item) for item in payload.get("items") or [] if isinstance(item, dict)]
        if status:
            items = [item for item in items if item.status == status]
        return sorted(items, key=lambda item: item.id)

    def get(self, draft_id: int | None = None) -> DraftPost | None:
        target_id = self._normalize_id(draft_id)
        if target_id <= 0:
            return None
        items = self.list()
        for item in items:
            if item.id == target_id:
                return item
        return None

    async def get_async(self, draft_id: int | None = None) -> DraftPost | None:
        target_id = self._normalize_id(draft_id)
        if target_id <= 0:
            return None
        items = await self.list_async()
        for item in items:
            if item.id == target_id:
                return item
        return None

    @staticmethod
    def _normalize_id(draft_id: int | None = None) -> int:
        try:
            return int(draft_id or 0)
        except (TypeError, ValueError):
            return 0

    def latest_pending(self) -> DraftPost | None:
        items = self.list(status="pending")
        return items[-1] if items else None

    async def latest_pending_async(self) -> DraftPost | None:
        items = await self.list_async(status="pending")
        return items[-1] if items else None

    def add(
        self,
        *,
        author_uin: int,
        author_name: str = "",
        group_id: int = 0,
        content: str = "",
        media: list[dict[str, Any]] | None = None,
        anonymous: bool = False,
    ) -> DraftPost:
        return self._store.transact(
            lambda payload: self._add_to_payload(
                payload,
                author_uin=author_uin,
                author_name=author_name,
                group_id=group_id,
                content=content,
                media=media,
                anonymous=anonymous,
            )
        )

    async def add_async(
        self,
        *,
        author_uin: int,
        author_name: str = "",
        group_id: int = 0,
        content: str = "",
        media: list[dict[str, Any]] | None = None,
        anonymous: bool = False,
    ) -> DraftPost:
        return await self._store.transact_async(
            lambda payload: self._add_to_payload(
                payload,
                author_uin=author_uin,
                author_name=author_name,
                group_id=group_id,
                content=content,
                media=media,
                anonymous=anonymous,
            )
        )

    def _add_to_payload(
        self,
        payload: dict[str, Any],
        *,
        author_uin: int,
        author_name: str,
        group_id: int,
        content: str,
        media: list[dict[str, Any]] | None,
        anonymous: bool,
    ) -> DraftPost:
        draft_id = int(payload.get("next_id") or 1)
        draft = DraftPost(
            id=draft_id,
            author_uin=author_uin,
            author_name=author_name,
            group_id=group_id,
            content=content,
            media=list(media or []),
            anonymous=anonymous,
        )
        items = [item for item in payload.get("items") or [] if isinstance(item, dict)]
        items.append(draft.to_dict())
        payload["items"] = items
        payload["next_id"] = draft_id + 1
        return draft

    def save(self, draft: DraftPost) -> DraftPost:
        draft.updated_at = now_iso()
        return self._store.transact(lambda payload: self._save_to_payload(payload, draft))

    async def save_async(self, draft: DraftPost) -> DraftPost:
        draft.updated_at = now_iso()
        return await self._store.transact_async(lambda payload: self._save_to_payload(payload, draft))

    def _save_to_payload(self, payload: dict[str, Any], draft: DraftPost) -> DraftPost:
        items: list[dict[str, Any]] = []
        found = False
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            if int(item.get("id") or 0) == draft.id:
                items.append(draft.to_dict())
                found = True
            else:
                items.append(item)
        if not found:
            items.append(draft.to_dict())
        payload["items"] = items
        payload["next_id"] = max(int(payload.get("next_id") or 1), draft.id + 1)
        return draft

    def update(self, draft_id: int, mutator: Callable[[DraftPost], None]) -> DraftPost | None:
        target_id = self._normalize_id(draft_id)
        if target_id <= 0:
            return None
        return self._store.transact(lambda payload: self._update_payload(payload, target_id, mutator))

    async def update_async(self, draft_id: int, mutator: Callable[[DraftPost], None]) -> DraftPost | None:
        target_id = self._normalize_id(draft_id)
        if target_id <= 0:
            return None
        return await self._store.transact_async(lambda payload: self._update_payload(payload, target_id, mutator))

    @staticmethod
    def _update_payload(
        payload: dict[str, Any],
        target_id: int,
        mutator: Callable[[DraftPost], None],
    ) -> DraftPost | None:
        items: list[dict[str, Any]] = []
        updated: DraftPost | None = None
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            if int(item.get("id") or 0) == target_id:
                draft = DraftPost.from_dict(item)
                mutator(draft)
                draft.updated_at = now_iso()
                updated = draft
                items.append(draft.to_dict())
            else:
                items.append(item)
        payload["items"] = items
        return updated
