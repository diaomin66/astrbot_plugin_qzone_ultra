from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from qzone_bridge.errors import DaemonUnavailableError, QzoneParseError
from qzone_bridge.page_api import QzonePageApi
from qzone_bridge import controller as controller_module
from qzone_bridge.daemon import QzoneDaemonService
from qzone_bridge.models import BridgeState, FeedEntry, SessionState
from qzone_bridge.storage import StateStore


class _Controller:
    def __init__(self):
        self.published = None
        self.deleted = None
        self.liked = None
        self.list_record_recent_values = []
        self.status_probe_values = []
        self.status = {
            "daemon_state": "ready",
            "daemon_port": 18999,
            "daemon_version": "test",
            "cookie_count": 2,
            "needs_rebind": False,
            "login_uin": 10001,
            "login_nickname": "Tester",
        }

    async def get_status(self, *, probe_daemon=False):
        self.status_probe_values.append(probe_daemon)
        return dict(self.status)

    async def list_feeds(self, *, hostuin=0, limit=5, cursor="", scope="", record_recent=True):
        self.list_record_recent_values.append(record_recent)
        return {
            "scope": scope or "active",
            "hostuin": hostuin or 10001,
            "cursor": "next-cursor",
            "has_more": True,
            "items": [
                {
                    "hostuin": 20002,
                    "fid": "fid-secret",
                    "appid": 311,
                    "summary": "hello from qzone",
                    "nickname": "Friend",
                    "created_at": 1710000000,
                    "like_count": 3,
                    "comment_count": 1,
                    "liked": False,
                    "curkey": "curkey-secret",
                    "unikey": "unikey-secret",
                    "busi_param": {"private": "secret"},
                    "raw": {"raw_secret": "hidden"},
                }
            ],
        }

    async def detail_feed(self, *, hostuin, fid, appid=311, busi_param=""):
        return {
            "entry": {
                "hostuin": hostuin,
                "fid": fid,
                "appid": appid,
                "summary": "detail text",
                "nickname": "Friend",
                "created_at": 1710000000,
                "like_count": 3,
                "comment_count": 1,
                "liked": False,
                "raw": {"raw_secret": "hidden"},
            },
            "comments": [
                {
                    "commentid": "comment-1",
                    "uin": 30003,
                    "nickname": "Commenter",
                    "content": "nice",
                    "created_at": 1710000100,
                },
                {
                    "commentid": "comment-self",
                    "uin": 10001,
                    "nickname": "QQ 10001",
                    "content": "self comment",
                    "created_at": 1710000200,
                }
            ],
            "raw": {"raw_secret": "hidden"},
        }

    async def publish_post(self, **kwargs):
        self.published = kwargs
        return {"fid": "new-fid", "message": "ok", "media_count": len(kwargs.get("media") or []), "photo_count": 0}

    async def like_post(self, **kwargs):
        self.liked = kwargs
        return {
            "liked": True,
            "verified": False,
            "summary": "accepted",
            "operation_status": "accepted_pending_verification",
        }

    async def comment_post(self, **kwargs):
        return {"commentid": "comment-new", "message": "ok"}

    async def reply_comment(self, **kwargs):
        return {"commentid": "reply-new", "message": "ok"}

    async def delete_post(self, **kwargs):
        self.deleted = kwargs
        return {"message": "ok"}


def _api(controller: _Controller | None = None) -> QzonePageApi:
    controller = controller or _Controller()
    return QzonePageApi(
        controller=controller,
        post_service_factory=lambda: None,
        settings=SimpleNamespace(max_feed_limit=20),
    )


def test_page_feed_redacts_internal_qzone_fields() -> None:
    api = _api()
    payload = asyncio.run(api.feed({"scope": "friends", "limit": 5}))

    post = payload["data"]["items"][0]
    assert payload["ok"] is True
    assert post["content"] == "hello from qzone"
    assert "id" in post
    assert "fid" not in post
    assert "raw" not in post
    assert "curkey" not in post
    assert "unikey" not in post
    assert "busi_param" not in post
    assert "fid-secret" not in post["id"]
    assert "curkey-secret" not in post["id"]

    ref = api._decode_post_ref(post["id"])
    assert ref.hostuin == 20002
    assert ref.fid == "fid-secret"
    assert api.controller.list_record_recent_values == [False]


def test_page_detail_redacts_raw_but_keeps_comments() -> None:
    api = _api()
    feed_payload = asyncio.run(api.feed({}))
    post_id = feed_payload["data"]["items"][0]["id"]

    detail_payload = asyncio.run(api.detail({"id": post_id}))

    post = detail_payload["data"]["post"]
    assert detail_payload["ok"] is True
    assert post["comments"][0]["content"] == "nice"
    assert post["comments"][1]["author"]["nickname"] == "Tester"
    assert "raw" not in post
    assert "fid" not in post


def test_page_like_preserves_pending_verification_as_success() -> None:
    api = _api()
    feed_payload = asyncio.run(api.feed({}))
    post_id = feed_payload["data"]["items"][0]["id"]

    payload = asyncio.run(api.like({"id": post_id}))

    assert payload["ok"] is True
    assert payload["data"]["verified"] is False
    assert payload["data"]["operation_status"] == "accepted_pending_verification"


def test_page_like_uses_fast_path_and_skips_daemon_readiness_gate() -> None:
    controller = _Controller()
    controller.status["daemon_state"] = "degraded"
    api = _api(controller)
    post_id = api._post_ref_id(20002, "fid-secret", 311)

    payload = asyncio.run(api.like({"id": post_id}))

    assert payload["ok"] is True
    assert controller.liked["fast"] is True
    assert controller.liked["hostuin"] == 20002
    assert controller.status_probe_values == [False]


def test_page_like_propagates_daemon_request_errors_after_fast_cookie_check() -> None:
    class _FailingController(_Controller):
        async def like_post(self, **kwargs):
            raise DaemonUnavailableError("daemon down")

    controller = _FailingController()
    controller.status["daemon_state"] = "degraded"
    api = _api(controller)
    post_id = api._post_ref_id(20002, "fid-secret", 311)

    with pytest.raises(DaemonUnavailableError):
        asyncio.run(api.like({"id": post_id}))


def test_page_publish_passes_webui_content_as_already_sanitized() -> None:
    controller = _Controller()

    payload = asyncio.run(_api(controller).publish({"content": "qzone post literal", "media": []}))

    assert payload["ok"] is True
    assert controller.published["content"] == "qzone post literal"
    assert controller.published["content_sanitized"] is True
    assert payload["data"]["post"]["author"]["uin"] == 10001
    assert payload["data"]["post"]["author"]["nickname"] == "Tester"


def test_page_comment_and_reply_return_current_author() -> None:
    api = _api()
    post_id = api._post_ref_id(20002, "fid-secret", 311)

    comment_payload = asyncio.run(api.comment({"id": post_id, "content": "nice"}))
    reply_payload = asyncio.run(
        api.reply({
            "id": post_id,
            "commentid": "comment-1",
            "comment_uin": 30003,
            "content": "thanks",
        })
    )

    assert comment_payload["data"]["comment"]["author"] == {
        "uin": 10001,
        "nickname": "Tester",
        "avatar": "",
    }
    assert reply_payload["data"]["reply"]["author"] == {
        "uin": 10001,
        "nickname": "Tester",
        "avatar": "",
    }


def test_page_status_reports_unlimited_upload_bytes() -> None:
    payload = asyncio.run(_api().status())

    assert payload["ok"] is True
    assert payload["data"]["limits"]["upload_bytes"] is None


def test_page_upload_accepts_images_above_old_page_limit() -> None:
    data = b"\x89PNG\r\n\x1a\n" + (b"x" * (8 * 1024 * 1024))

    payload = asyncio.run(_api().upload_media(filename="large.png", content_type="image/png", data=data))

    assert payload["ok"] is True
    assert payload["data"]["media"]["name"] == "large.png"
    assert payload["data"]["media"]["size"] == len(data)


def test_page_delete_rejects_other_users_posts() -> None:
    api = _api()
    feed_payload = asyncio.run(api.feed({}))
    post_id = feed_payload["data"]["items"][0]["id"]

    with pytest.raises(QzoneParseError):
        asyncio.run(api.delete({"id": post_id}))


def test_page_upload_rejects_non_images() -> None:
    with pytest.raises(QzoneParseError):
        asyncio.run(_api().upload_media(filename="note.txt", content_type="text/plain", data=b"not-image"))


def test_controller_rejects_same_api_but_stale_daemon_version() -> None:
    controller = object.__new__(controller_module.QzoneDaemonController)
    payload = {
        "ok": True,
        "data": {
            "daemon_state": "ready",
            "daemon_port": 18999,
            "daemon_version": "0.4.2",
            "bridge_api_version": controller_module.BRIDGE_API_VERSION,
        },
    }

    assert controller._health_payload_is_compatible(payload) is False

    payload["data"]["daemon_version"] = controller_module.BRIDGE_VERSION
    assert controller._health_payload_is_compatible(payload) is True


def test_daemon_fast_like_updates_shared_feed_cache_once(tmp_path) -> None:
    async def scenario() -> FeedEntry:
        store = StateStore(tmp_path)
        state = BridgeState()
        state.session = SessionState(uin=10001, cookies={"uin": "o10001", "p_skey": "token"})
        store.write(state)
        service = QzoneDaemonService(store, secret="secret", port=18999)
        await service.client.close()

        entry = FeedEntry(hostuin=20002, fid="fid-secret", appid=311, summary="", liked=False, like_count=3)

        class _Client:
            def __init__(self):
                self.feed_cache = {(20002, "fid-secret"): entry}

            async def like_post(self, *args, **kwargs):
                return {"ok": True}

        service.client = _Client()
        service.recent_feed_entries = [entry]
        service._set_success = lambda *, defer_save=False: None

        await service.like_post(hostuin=20002, fid="fid-secret", appid=311, fast=True)
        assert entry.liked is True
        assert entry.like_count == 4

        await service.like_post(hostuin=20002, fid="fid-secret", appid=311, fast=True)
        return entry

    entry = asyncio.run(scenario())

    assert entry.liked is True
    assert entry.like_count == 4


def test_daemon_list_feeds_can_fill_cache_without_overwriting_recent(tmp_path) -> None:
    async def scenario() -> list[FeedEntry]:
        store = StateStore(tmp_path)
        state = BridgeState()
        state.session = SessionState(uin=10001, cookies={"uin": "o10001", "p_skey": "token"})
        store.write(state)
        service = QzoneDaemonService(store, secret="secret", port=18999)
        await service.client.close()
        previous = FeedEntry(hostuin=30003, fid="old-fid", appid=311, summary="old")
        service.recent_feed_entries = [previous]

        class _Client:
            def __init__(self):
                self.feed_cache = {}

            async def index(self):
                return {"data": [{"hostuin": 10001, "fid": "new-fid", "content": "new"}]}

            def cache_feed_page(self, hostuin, items):
                for item in items:
                    self.feed_cache[(hostuin or item.hostuin, item.fid)] = item

        service.client = _Client()
        await service.list_feeds(hostuin=10001, limit=5, scope="self", record_recent=False)
        return service.recent_feed_entries

    recent = asyncio.run(scenario())

    assert [entry.fid for entry in recent] == ["old-fid"]
