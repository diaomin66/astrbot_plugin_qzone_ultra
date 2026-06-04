from __future__ import annotations

import asyncio
import base64
import logging
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from qzone_bridge import page_api as page_api_module
from qzone_bridge.astrbot_logging import configure_standalone_logging
from qzone_bridge.errors import DaemonUnavailableError, QzoneParseError
from qzone_bridge.page_api import QzonePageApi, page_error_payload
from qzone_bridge import controller as controller_module
from qzone_bridge.daemon import QzoneDaemonService
from qzone_bridge.models import BridgeState, FeedEntry, SessionState
from qzone_bridge.parser import extract_feed_page
from qzone_bridge.social import extract_images, post_from_entry
from qzone_bridge.storage import StateStore


class _Controller:
    def __init__(self):
        self.published = None
        self.deleted = None
        self.liked = None
        self.commented = None
        self.detail_requested = None
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
        self.detail_requested = {
            "hostuin": hostuin,
            "fid": fid,
            "appid": appid,
            "busi_param": busi_param,
        }
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
        self.commented = kwargs
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


def _png_bytes(width: int = 32, height: int = 32, *, pad_bytes: int = 0) -> bytes:
    raw = b"".join(b"\x00" + (b"\x28\x8c\xf0" * width) for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9) + (b"x" * pad_bytes))
        + chunk(b"IEND", b"")
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


def test_page_feed_filters_duplicate_append_items() -> None:
    class _RepeatingController(_Controller):
        async def list_feeds(self, *, hostuin=0, limit=5, cursor="", scope="", record_recent=True):
            payload = await super().list_feeds(
                hostuin=hostuin,
                limit=limit,
                cursor=cursor,
                scope=scope,
                record_recent=record_recent,
            )
            payload["cursor"] = "cursor-2" if cursor else "cursor-1"
            payload["has_more"] = True
            return payload

    api = _api(_RepeatingController())
    first = asyncio.run(api.feed({"scope": "friends", "limit": 5}))
    second = asyncio.run(api.feed({"scope": "friends", "limit": 5, "cursor": first["data"]["cursor"]}))

    assert len(first["data"]["items"]) == 1
    assert second["data"]["items"] == []
    assert second["data"]["has_more"] is False


def test_page_feed_skips_invalid_legacy_recent_entries() -> None:
    class _MixedController(_Controller):
        async def list_feeds(self, *, hostuin=0, limit=5, cursor="", scope="", record_recent=True):
            return {
                "scope": scope or "active",
                "hostuin": hostuin or 10001,
                "cursor": "",
                "has_more": False,
                "items": [
                    {
                        "hostuin": 20002,
                        "fid": "",
                        "appid": 311,
                        "summary": "bad envelope item",
                    },
                    {
                        "hostuin": 20003,
                        "fid": "valid-fid",
                        "appid": 311,
                        "summary": "valid item",
                        "nickname": "Friend",
                    },
                ],
            }

    payload = asyncio.run(_api(_MixedController()).feed({"scope": "friends", "limit": 5}))

    assert payload["ok"] is True
    assert payload["data"]["count"] == 1
    assert payload["data"]["items"][0]["content"] == "valid item"


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


def test_page_detail_reuses_feed_busi_param() -> None:
    controller = _Controller()
    api = _api(controller)
    feed_payload = asyncio.run(api.feed({}))
    post_id = feed_payload["data"]["items"][0]["id"]

    asyncio.run(api.detail({"id": post_id}))

    assert controller.detail_requested["busi_param"] == '{"private":"secret"}'


def test_page_detail_returns_cached_post_when_daemon_detail_times_out(monkeypatch) -> None:
    class _SlowDetailController(_Controller):
        async def detail_feed(self, *, hostuin, fid, appid=311, busi_param=""):
            await asyncio.sleep(1)
            return await super().detail_feed(hostuin=hostuin, fid=fid, appid=appid, busi_param=busi_param)

    monkeypatch.setattr(page_api_module, "PAGE_DETAIL_TIMEOUT_SECONDS", 0.01)
    api = _api(_SlowDetailController())
    feed_payload = asyncio.run(api.feed({}))
    post_id = feed_payload["data"]["items"][0]["id"]

    detail_payload = asyncio.run(api.detail({"id": post_id}))

    assert detail_payload["ok"] is True
    assert detail_payload["data"]["partial"] is True
    assert detail_payload["data"]["post"]["content"] == "hello from qzone"
    assert detail_payload["data"]["post"]["comments"] == []


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


def test_page_like_and_comment_reuse_feed_action_metadata() -> None:
    api = _api()
    feed_payload = asyncio.run(api.feed({}))
    post_id = feed_payload["data"]["items"][0]["id"]

    asyncio.run(api.like({"id": post_id}))
    asyncio.run(api.comment({"id": post_id, "content": "nice"}))

    assert api.controller.liked["curkey"] == "curkey-secret"
    assert api.controller.commented["busi_param"] == {"private": "secret"}


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


def test_page_publish_normalizes_uploaded_data_urls() -> None:
    controller = _Controller()
    media = [{"name": "photo.png", "data_url": "data:image/png;base64,AA=="}]

    payload = asyncio.run(_api(controller).publish({"content": "with image", "media": media}))

    assert payload["ok"] is True
    assert controller.published["media"][0]["source"] == "data:image/png;base64,AA=="
    assert controller.published["media"][0]["kind"] == "image"
    assert payload["data"]["post"]["images"] == ["data:image/png;base64,AA=="]


def test_page_publish_accepts_video_only_media_without_text() -> None:
    controller = _Controller()
    media = [
        {
            "kind": "video",
            "name": "clip",
            "mime_type": "video/mp4",
            "source": "base64://" + base64.b64encode(b"fake video bytes").decode("ascii"),
        }
    ]

    payload = asyncio.run(_api(controller).publish({"content": "  ", "media": media}))

    assert payload["ok"] is True
    assert controller.published["content"] == "  "
    assert controller.published["media"][0]["kind"] == "video"
    assert controller.published["media"][0]["mime_type"] == "video/mp4"
    assert payload["data"]["media_count"] == 1
    assert payload["data"]["post"]["images"][0].startswith("base64://")


def test_page_publish_created_post_can_delete_with_created_at(monkeypatch) -> None:
    controller = _Controller()
    monkeypatch.setattr(page_api_module.time, "time", lambda: 1710000999)
    api = _api(controller)

    publish_payload = asyncio.run(api.publish({"content": "temporary post", "media": []}))
    post_id = publish_payload["data"]["post"]["id"]
    delete_payload = asyncio.run(api.delete({"id": post_id}))

    assert delete_payload["ok"] is True
    assert controller.deleted["fid"] == "new-fid"
    assert controller.deleted["created_at"] == 1710000999


def test_page_publish_rejects_empty_content_and_media() -> None:
    with pytest.raises(QzoneParseError):
        asyncio.run(_api().publish({"content": "  ", "media": []}))


def test_page_error_payload_keeps_top_level_message() -> None:
    payload, status = page_error_payload(QzoneParseError("bad upload"))

    assert status == 400
    assert payload["ok"] is False
    assert payload["message"] == "bad upload"
    assert payload["error"]["message"] == "bad upload"


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


def test_page_status_uses_recovery_provider_to_avoid_stale_degraded_state() -> None:
    controller = _Controller()
    controller.status["daemon_state"] = "degraded"
    recovery_called = False

    async def ready_recovery():
        nonlocal recovery_called
        recovery_called = True
        await asyncio.sleep(0)
        return {
            "daemon_state": "ready",
            "daemon_port": 18999,
            "daemon_version": "test",
            "cookie_count": 2,
            "needs_rebind": False,
            "login_uin": 99999,
        }

    api = QzonePageApi(
        controller=controller,
        post_service_factory=lambda: None,
        settings=SimpleNamespace(max_feed_limit=20),
        status_provider=ready_recovery,
    )

    payload = asyncio.run(api.status())

    assert payload["ok"] is True
    assert payload["data"]["login"]["uin"] == 99999
    assert payload["data"]["daemon"]["state"] == "ready"
    assert controller.status_probe_values == []
    assert recovery_called is True


def test_page_status_falls_back_when_recovery_provider_times_out(monkeypatch) -> None:
    controller = _Controller()
    controller.status["daemon_state"] = "degraded"
    monkeypatch.setattr(page_api_module, "PAGE_STATUS_TIMEOUT_SECONDS", 0.01)

    async def stalled_recovery():
        await asyncio.sleep(1)
        return {"daemon_state": "ready", "login_uin": 99999}

    api = QzonePageApi(
        controller=controller,
        post_service_factory=lambda: None,
        settings=SimpleNamespace(max_feed_limit=20),
        status_provider=stalled_recovery,
    )

    payload = asyncio.run(api.status())

    assert payload["ok"] is True
    assert payload["data"]["daemon"]["state"] == "degraded"
    assert payload["data"]["login"]["uin"] == 10001
    assert controller.status_probe_values == [False]


def test_page_status_returns_minimal_payload_when_snapshot_fails() -> None:
    class _FailingStatusController(_Controller):
        async def get_status(self, *, probe_daemon=False):
            raise RuntimeError("status store unavailable")

    preload_called = False

    def failing_preload(trigger: str) -> None:
        nonlocal preload_called
        preload_called = True
        raise RuntimeError("preload failed")

    api = QzonePageApi(
        controller=_FailingStatusController(),
        post_service_factory=lambda: None,
        settings=SimpleNamespace(max_feed_limit=20),
        preload_scheduler=failing_preload,
    )

    payload = asyncio.run(api.status())

    assert payload["ok"] is True
    assert payload["data"]["daemon"]["state"] == "unknown"
    assert payload["data"]["daemon"]["error"] == "status store unavailable"
    assert payload["data"]["login"]["bound"] is False
    assert preload_called is True


def test_daemon_logging_suppresses_http_wire_debug(monkeypatch) -> None:
    monkeypatch.delenv("QZONE_DAEMON_LOG_LEVEL", raising=False)
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)

    configure_standalone_logging()

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_page_upload_accepts_images_above_old_page_limit() -> None:
    data = _png_bytes(pad_bytes=8 * 1024 * 1024)

    payload = asyncio.run(_api().upload_media(filename="large.png", content_type="image/png", data=data))

    assert payload["ok"] is True
    assert payload["data"]["media"]["name"] == "large.png"
    assert payload["data"]["media"]["size"] == len(data)


def test_page_upload_accepts_video_bytes_and_marks_video_kind() -> None:
    data = b"fake video bytes"

    payload = asyncio.run(_api().upload_media(filename="clip.mp4", content_type="video/mp4", data=data))

    media = payload["data"]["media"]
    assert payload["ok"] is True
    assert media["kind"] == "video"
    assert media["name"] == "clip.mp4"
    assert media["mime_type"] == "video/mp4"
    assert media["source"].startswith("base64://")


def test_page_upload_accepts_json_data_url_fallback() -> None:
    data = _png_bytes()
    body = {
        "name": "fallback.png",
        "data_url": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
    }

    payload = asyncio.run(_api().upload_media_payload(body))

    media = payload["data"]["media"]
    assert payload["ok"] is True
    assert media["name"] == "fallback.png"
    assert media["mime_type"] == "image/png"
    assert media["size"] == len(data)
    assert media["source"].startswith("base64://")


def test_page_upload_accepts_json_base64_source_fallback() -> None:
    data = _png_bytes()
    body = {
        "filename": "source.png",
        "source": "base64://" + base64.b64encode(data).decode("ascii"),
        "content_type": "image/png",
    }

    payload = asyncio.run(_api().upload_media_payload(body))

    assert payload["ok"] is True
    assert payload["data"]["media"]["name"] == "source.png"
    assert payload["data"]["media"]["size"] == len(data)


def test_page_upload_uses_local_token_when_controller_has_data_dir(tmp_path: Path) -> None:
    data = _png_bytes()
    controller = _Controller()
    controller.data_dir = tmp_path
    api = _api(controller)

    upload_payload = asyncio.run(api.upload_media(filename="large.png", content_type="image/png", data=data))
    media = upload_payload["data"]["media"]
    publish_payload = asyncio.run(
        api.publish({"content": "with local image", "media": [{**media, "preview_url": "blob:test"}]})
    )

    sent_media = controller.published["media"][0]
    assert media["upload_id"]
    assert "source" not in media
    assert sent_media["upload_id"] == media["upload_id"]
    assert sent_media["trusted_local"] is True
    assert Path(sent_media["source"]).read_bytes() == data
    assert "preview_url" not in sent_media
    assert publish_payload["data"]["post"]["images"] == []


def test_page_upload_video_token_can_publish_video_only_without_text(tmp_path: Path) -> None:
    data = b"fake video bytes"
    controller = _Controller()
    controller.data_dir = tmp_path
    api = _api(controller)

    upload_payload = asyncio.run(api.upload_media(filename="clip", content_type="video/mp4", data=data))
    media = upload_payload["data"]["media"]
    publish_payload = asyncio.run(
        api.publish({"content": "", "media": [{**media, "preview_url": "blob:video-preview"}]})
    )

    sent_media = controller.published["media"][0]
    assert publish_payload["ok"] is True
    assert media["kind"] == "video"
    assert media["upload_id"]
    assert sent_media["kind"] == "video"
    assert sent_media["trusted_local"] is True
    assert Path(sent_media["source"]).read_bytes() == data
    assert Path(sent_media["source"]).suffix == ""
    assert "preview_url" not in sent_media


def test_page_delete_rejects_other_users_posts() -> None:
    api = _api()
    feed_payload = asyncio.run(api.feed({}))
    post_id = feed_payload["data"]["items"][0]["id"]

    with pytest.raises(QzoneParseError):
        asyncio.run(api.delete({"id": post_id}))


def test_page_delete_passes_feed_created_at_for_real_qzone_delete() -> None:
    class _OwnPostController(_Controller):
        async def list_feeds(self, *, hostuin=0, limit=5, cursor="", scope="", record_recent=True):
            return {
                "scope": "self",
                "hostuin": 10001,
                "cursor": "",
                "has_more": False,
                "items": [
                    {
                        "hostuin": 10001,
                        "fid": "own-fid",
                        "appid": 311,
                        "summary": "own post",
                        "nickname": "Tester",
                        "created_at": 1710000123,
                    }
                ],
            }

    controller = _OwnPostController()
    api = _api(controller)
    feed_payload = asyncio.run(api.feed({"scope": "self"}))
    post_id = feed_payload["data"]["items"][0]["id"]

    payload = asyncio.run(api.delete({"id": post_id}))

    assert payload["ok"] is True
    assert controller.deleted["fid"] == "own-fid"
    assert controller.deleted["created_at"] == 1710000123


def test_page_upload_rejects_non_images() -> None:
    with pytest.raises(QzoneParseError):
        asyncio.run(_api().upload_media(filename="note.txt", content_type="text/plain", data=b"not-image"))


def test_page_upload_rejects_too_small_images_before_publish() -> None:
    with pytest.raises(QzoneParseError, match="尺寸过小"):
        asyncio.run(_api().upload_media(filename="tiny.png", content_type="image/png", data=_png_bytes(1, 1)))


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


def test_daemon_legacy_feed_cursor_keeps_backend_and_page_size(tmp_path) -> None:
    async def scenario() -> tuple[list[tuple[int, int]], list[str], list[str]]:
        store = StateStore(tmp_path)
        state = BridgeState()
        state.session = SessionState(uin=10001, cookies={"uin": "o10001", "p_skey": "token"})
        store.write(state)
        service = QzoneDaemonService(store, secret="secret", port=18999)
        await service.client.close()
        calls: list[tuple[int, int]] = []

        class _Client:
            def __init__(self):
                self.feed_cache = {}

            async def index(self):
                raise QzoneParseError("h5 index redirect")

            async def legacy_feeds(self, hostuin, *, page=1, num=10):
                calls.append((page, num))
                return {
                    "msglist": [
                        {
                            "hostuin": hostuin,
                            "fid": f"legacy-{page}",
                            "content": f"legacy page {page}",
                            "created_time": 1710000000 - page,
                        }
                    ],
                    "hasmore": 1,
                }

            async def get_active_feeds(self, attach_info=""):
                raise AssertionError("legacy cursor must not switch to active feeds")

            def cache_feed_page(self, hostuin, items):
                for item in items:
                    self.feed_cache[(hostuin or item.hostuin, item.fid)] = item

        service.client = _Client()
        first = await service.list_feeds(hostuin=10001, limit=1, scope="self", record_recent=False)
        second = await service.list_feeds(
            hostuin=10001,
            limit=1,
            scope="self",
            cursor=first["cursor"],
            record_recent=False,
        )
        return calls, [item["fid"] for item in first["items"]], [item["fid"] for item in second["items"]]

    calls, first_fids, second_fids = asyncio.run(scenario())

    assert calls == [(1, 1), (2, 1)]
    assert first_fids == ["legacy-1"]
    assert second_fids == ["legacy-2"]


def test_daemon_active_feed_returns_legacy_recent_cursor_without_prefetching_repeat(tmp_path) -> None:
    async def scenario() -> tuple[dict[str, object], dict[str, object], list[tuple[int, int]]]:
        store = StateStore(tmp_path)
        state = BridgeState()
        state.session = SessionState(uin=10001, cookies={"uin": "o10001", "p_skey": "token"})
        store.write(state)
        service = QzoneDaemonService(store, secret="secret", port=18999)
        await service.client.close()
        calls: list[tuple[int, int]] = []

        class _Client:
            def __init__(self):
                self.feed_cache = {}

            async def index(self):
                raise QzoneParseError("h5 index redirect")

            async def legacy_recent_feeds(self, page=1, *, begin_time=0):
                calls.append((page, begin_time))
                start = 1 if begin_time == 0 else 7
                return {
                    "data": [
                        {
                            "appid": "311",
                            "key": f"repeat-{index}",
                            "abstime": str(1710000000 - index),
                            "opuin": str(20000 + index),
                            "nickname": f"Friend {index}",
                            "html": f'<li><div class="f-info">same page item {index}</div></li>',
                        }
                        for index in range(start, start + 6)
                    ],
                    "hasmore": 1,
                }

            def cache_feed_page(self, hostuin, items):
                for item in items:
                    self.feed_cache[(hostuin or item.hostuin, item.fid)] = item

        service.client = _Client()
        first = await service.list_feeds(limit=10, scope="active", record_recent=False)
        second = await service.list_feeds(
            limit=10,
            scope="active",
            cursor=str(first["cursor"]),
            record_recent=False,
        )
        return first, second, calls

    first, second, calls = asyncio.run(scenario())

    assert calls == [(1, 0), (2, 1710000000 - 6)]
    assert [item["fid"] for item in first["items"]] == [f"repeat-{index}" for index in range(1, 7)]
    assert [item["fid"] for item in second["items"]] == [f"repeat-{index}" for index in range(7, 13)]
    assert first["count"] == 6
    assert first["has_more"] is True
    assert first["cursor"]


def test_daemon_active_feed_stops_when_modern_cursor_repeats(tmp_path) -> None:
    async def scenario() -> tuple[dict[str, object], list[str]]:
        store = StateStore(tmp_path)
        state = BridgeState()
        state.session = SessionState(uin=10001, cookies={"uin": "o10001", "p_skey": "token"})
        store.write(state)
        service = QzoneDaemonService(store, secret="secret", port=18999)
        await service.client.close()
        calls: list[str] = []

        class _Client:
            def __init__(self):
                self.feed_cache = {}

            async def index(self):
                calls.append("index")
                return {
                    "feedpage": {
                        "vFeeds": [
                            {
                                "hostuin": 20002,
                                "fid": "modern-1",
                                "content": "first page",
                                "created_time": 1710000000,
                            }
                        ],
                        "hasmore": 1,
                        "attachinfo": "cursor-a",
                    }
                }

            async def get_active_feeds(self, attach_info=""):
                calls.append(attach_info)
                return {
                    "feedpage": {
                        "vFeeds": [
                            {
                                "hostuin": 20003,
                                "fid": "modern-2",
                                "content": "second page",
                                "created_time": 1709999999,
                            }
                        ],
                        "hasmore": 1,
                        "attachinfo": "cursor-a",
                    }
                }

            def cache_feed_page(self, hostuin, items):
                for item in items:
                    self.feed_cache[(hostuin or item.hostuin, item.fid)] = item

        service.client = _Client()
        return await service.list_feeds(limit=5, scope="active", record_recent=False), calls

    payload, calls = asyncio.run(scenario())

    assert calls == ["index", "cursor-a"]
    assert [item["fid"] for item in payload["items"]] == ["modern-1", "modern-2"]
    assert payload["has_more"] is False
    assert payload["cursor"] == ""


def test_parser_accepts_top_level_legacy_recent_feed_list() -> None:
    payload = [
        {
            "appid": "311",
            "key": "legacy-key-1",
            "abstime": "1710000000",
            "opuin": "20002",
            "nickname": "Friend",
            "html": '<li data-key="legacy-key-1"><div class="f-info">来自好友的动态</div></li>',
        }
    ]

    feedpage, entries = extract_feed_page(payload, default_hostuin=0)

    assert feedpage["data"] == payload
    assert len(entries) == 1
    assert entries[0].hostuin == 20002
    assert entries[0].fid == "legacy-key-1"
    assert entries[0].summary == "来自好友的动态"
    assert entries[0].nickname == "Friend"
    assert entries[0].created_at == 1710000000


def test_parser_prefers_legacy_recent_data_list_over_metadata_container() -> None:
    payload = {
        "main": {"metadata": "not feeds"},
        "data": [
            {
                "appid": "311",
                "key": "legacy-key-2",
                "abstime": "1710000001",
                "opuin": "20003",
                "nickname": "Another",
                "html": '<li><div class="f-info">外层 data 才是真动态</div></li>',
            }
        ],
    }

    feedpage, entries = extract_feed_page(payload, default_hostuin=0)

    assert feedpage is payload
    assert len(entries) == 1
    assert entries[0].hostuin == 20003
    assert entries[0].fid == "legacy-key-2"
    assert entries[0].summary == "外层 data 才是真动态"


def test_parser_prefers_nested_f_info_text_for_legacy_html() -> None:
    payload = [
        {
            "appid": "311",
            "key": "legacy-key-3",
            "opuin": "20004",
            "nickname": "Nested",
            "html": (
                '<li class="f-single f-s-s">'
                '<div class="f-single-head">Nested 14:34</div>'
                '<div class="f-info">只要这一段正文</div>'
                '<div class="f-op">浏览2次 评论</div>'
                "</li>"
            ),
        }
    ]

    _feedpage, entries = extract_feed_page(payload, default_hostuin=0)

    assert entries[0].summary == "只要这一段正文"


def test_parser_strips_legacy_photo_card_chrome_when_text_is_metadata_only() -> None:
    payload = [
        {
            "appid": "311",
            "key": "photo-only",
            "opuin": "20004",
            "uin": "20004",
            "nickname": "椰子",
            "summary": "椰子\n昨天 12:06\n\n浏览13次\n\n+1\n\n雪碧bir共1人觉得很赞\n\n评论",
            "html": (
                '<li class="f-single f-s-s">'
                '<div class="f-single-head">椰子 昨天 12:06</div>'
                '<div class="f-op">浏览13次</div>'
                '<div class="f-like">+1</div>'
                '<div class="f-like-list">雪碧bir共1人觉得很赞</div>'
                '<div class="f-comment">评论</div>'
                "</li>"
            ),
        }
    ]

    _feedpage, entries = extract_feed_page(payload, default_hostuin=0)

    assert entries[0].summary == ""


def test_parser_keeps_legacy_caption_while_stripping_card_chrome() -> None:
    payload = [
        {
            "appid": "311",
            "key": "photo-with-caption",
            "opuin": "20004",
            "uin": "20004",
            "nickname": "椰子",
            "summary": "椰子\n昨天 12:06\n今天很开心\n浏览13次\n评论",
        }
    ]

    _feedpage, entries = extract_feed_page(payload, default_hostuin=0)

    assert entries[0].summary == "今天很开心"


def test_parser_filters_official_qzone_and_ad_legacy_recent_items() -> None:
    payload = [
        {
            "appid": "6600",
            "key": "advertisement_outlink_0",
            "opuin": "0",
            "uin": "0",
            "nickname": "",
            "html": '<li><div class="f-info">广告内容</div></li>',
        },
        {
            "appid": "5000",
            "key": "20050606_1710000000",
            "opuin": "20050606",
            "uin": "20050606",
            "nickname": "官方Qzone",
            "html": '<li><div class="f-info">官方推荐</div></li>',
        },
        {
            "appid": "311",
            "key": "valid-feed",
            "opuin": "20004",
            "uin": "20004",
            "nickname": "Friend",
            "html": '<li><div class="f-info">正常好友动态</div></li>',
        },
    ]

    _feedpage, entries = extract_feed_page(payload, default_hostuin=0)

    assert [entry.fid for entry in entries] == ["valid-feed"]
    assert entries[0].summary == "正常好友动态"


def test_legacy_recent_avatar_is_not_treated_as_post_image() -> None:
    avatar = "http://qlogo1.store.qq.com/qzone/3112333596/3112333596/50?1777117780"
    payload = {
        "appid": "311",
        "key": "feed-with-avatar",
        "opuin": "3112333596",
        "uin": "3112333596",
        "nickname": "椰子",
        "logimg": avatar,
        "html": f'<li><img src="{avatar}"><div class="f-info">只有文字没有配图</div></li>',
    }

    _feedpage, entries = extract_feed_page([payload], default_hostuin=0)
    post = post_from_entry(entries[0])

    assert extract_images(payload, fid="feed-with-avatar", hostuin=3112333596) == []
    assert post.images == []


def test_qzone_photo_prefers_unscaled_big_image_url() -> None:
    medium = "http://photo.store.qq.com/psc?/abc/token/m&bo=1"
    big = "http://photo.store.qq.com/psc?/abc/token/b&bo=1"
    scaled = "http://photo.store.qq.com/psc?/abc/token/b&bo=1&w=392&h=5000"
    entry = FeedEntry(
        hostuin=10001,
        fid="photo-feed",
        appid=311,
        summary="photo",
        raw={
            "hostuin": 10001,
            "fid": "photo-feed",
            "pic": [
                {
                    "pic_id": ",album,lloc",
                    "url1": medium,
                    "url2": big,
                    "url3": scaled,
                    "smallurl": medium,
                }
            ],
        },
    )

    post = post_from_entry(entry)

    assert post.images == [big]


def test_daemon_active_feed_legacy_recent_array_fallback_not_empty(tmp_path) -> None:
    async def scenario() -> dict[str, object]:
        store = StateStore(tmp_path)
        state = BridgeState()
        state.session = SessionState(uin=10001, cookies={"uin": "o10001", "p_skey": "token"})
        store.write(state)
        service = QzoneDaemonService(store, secret="secret", port=18999)
        await service.client.close()

        class _Client:
            def __init__(self):
                self.feed_cache = {}

            async def index(self):
                raise QzoneParseError("h5 index redirect")

            async def legacy_recent_feeds(self, page=1, *, begin_time=0):
                return [
                    {
                        "appid": "311",
                        "key": f"legacy-recent-{page}",
                        "abstime": str(1710000000 - page),
                        "opuin": "20002",
                        "nickname": "Friend",
                        "html": f'<li><div class="f-info">recent page {page}</div></li>',
                    }
                ]

            async def get_active_feeds(self, attach_info=""):
                raise AssertionError("legacy cursor must not switch to active feeds")

            def cache_feed_page(self, hostuin, items):
                for item in items:
                    self.feed_cache[(hostuin or item.hostuin, item.fid)] = item

        service.client = _Client()
        return await service.list_feeds(limit=1, scope="active", record_recent=False)

    payload = asyncio.run(scenario())

    assert payload["items"]
    assert payload["items"][0]["fid"] == "legacy-recent-1"
    assert payload["items"][0]["hostuin"] == 20002
