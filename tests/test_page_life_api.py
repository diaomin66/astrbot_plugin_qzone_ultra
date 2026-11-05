from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx

from qzone_bridge.controller import QzoneDaemonController
from qzone_bridge.drafts import DraftStore
from qzone_bridge.errors import QzoneParseError, QzoneRequestError
from qzone_bridge.media import PostPayload
from qzone_bridge.page_api import QzonePageApi
from qzone_bridge.page_life import PageLifeError
from qzone_bridge.models import SessionState


class Controller:
    def __init__(self):
        self.status = {"daemon_state": "ready", "cookie_count": 1, "needs_rebind": False,
                       "login_uin": 123, "login_nickname": "Tester"}
        self.published = []

    async def get_status(self, *, probe_daemon=False):
        return dict(self.status)

    async def publish_post(self, **kwargs):
        self.published.append(kwargs)
        return {"fid": "f1", "message": "ok", "media_count": len(kwargs.get("media") or []), "photo_count": 0}


def _png() -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (2, 2), (20, 80, 160)).save(output, format="PNG")
    return output.getvalue()


def _api(tmp_path: Path, controller: Controller, generator, *, cap=None):
    return QzonePageApi(
        controller=controller, post_service_factory=lambda: None, settings=SimpleNamespace(max_feed_limit=20),
        life_draft_generator=generator, life_capability_provider=cap or (lambda: {"available": True}),
        draft_store=DraftStore(tmp_path / "drafts.json"),
    )


def test_life_start_is_strict_coalesces_saves_one_draft_and_get_never_restarts(tmp_path):
    async def scenario():
        controller = Controller()
        calls = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def generate(progress):
            calls.append(progress)
            progress("image")
            started.set()
            await release.wait()
            return PostPayload("daily", [])

        api = _api(tmp_path, controller, generate)
        with pytest.raises(PageLifeError):
            await api.start_life_draft({"action": "generate_draft", "request_id": "r", "publish": True})
        assert not calls
        first = await api.start_life_draft({"action": "generate_draft", "request_id": "r"})
        await started.wait()
        second = await api.start_life_draft({"action": "generate_draft", "request_id": "r2"})
        assert first["data"]["id"] == second["data"]["id"] and len(calls) == 1
        release.set()
        job = await api._life_jobs.get(first["data"]["id"], 123)
        await job.task
        result = await api.get_life_draft({"id": job.id})
        assert result["data"]["state"] == "succeeded"
        assert result["data"]["content"] == "daily"
        assert len(await api.draft_store.list_async()) == 1
        await api.get_life_draft({})
        assert len(calls) == 1
        await api.close()

    asyncio.run(scenario())


def test_life_generation_account_change_saves_no_draft(tmp_path):
    async def scenario():
        controller = Controller()

        async def generate(_progress):
            controller.status["login_uin"] = 456
            return PostPayload("daily", [])

        api = _api(tmp_path, controller, generate)
        response = await api.start_life_draft({"action": "generate_draft", "request_id": "r"})
        job = await api._life_jobs.get(response["data"]["id"], 123)
        await job.task
        result = await api._life_jobs.get(job.id, 123)
        assert result.state == "failed" and result.message == "登录账号已变化，草稿未保存。"
        assert await api.draft_store.list_async() == []
        await api.close()

    asyncio.run(scenario())


def test_life_request_replay_returns_retained_job_even_if_capability_later_changes(tmp_path):
    async def scenario():
        controller = Controller()
        capability = {"available": True, "message": "ready"}
        calls = []

        async def generate(_progress):
            calls.append(1)
            return PostPayload("daily", [])

        api = _api(tmp_path, controller, generate, cap=lambda: dict(capability))
        first = await api.start_life_draft({"action": "generate_draft", "request_id": "same"})
        job = await api._life_jobs.get(first["data"]["id"], 123)
        await job.task
        capability.update(available=False, message="temporarily unavailable")
        replay = await api.start_life_draft({"action": "generate_draft", "request_id": "same"})
        assert replay["data"]["id"] == job.id and replay["data"]["state"] == "succeeded"
        assert len(calls) == 1
        with pytest.raises(PageLifeError, match="temporarily unavailable"):
            await api.start_life_draft({"action": "generate_draft", "request_id": "new"})
        await api.close()

    asyncio.run(scenario())


def test_life_publish_requires_provenance_and_never_sends_twice(tmp_path):
    async def scenario():
        controller = Controller()

        async def generate(_progress):
            return PostPayload("daily", [])

        api = _api(tmp_path, controller, generate)
        response = await api.start_life_draft({"action": "generate_draft", "request_id": "r"})
        job = await api._life_jobs.get(response["data"]["id"], 123)
        await job.task
        ordinary = await api.publish({"content": "copied text"})
        assert ordinary["ok"] and "expected_login_uin" not in controller.published[0]
        first = await api.publish({"content": "daily", "life_job_id": job.id})
        second = await api.publish({"content": "daily", "life_job_id": job.id})
        assert len(controller.published) == 2
        assert controller.published[1]["expected_login_uin"] == 123
        assert first == second
        with pytest.raises(PageLifeError):
            await api.publish({"content": "edited", "life_job_id": job.id})
        assert len(controller.published) == 2
        await api.close()

    asyncio.run(scenario())


def test_life_preview_returns_bounded_raster_dataurl_and_does_not_expose_source(tmp_path):
    async def scenario():
        controller = Controller()
        source = "data:image/png;base64," + base64.b64encode(_png()).decode("ascii")

        async def generate(_progress):
            return PostPayload("daily", [{"kind": "image", "source": source, "name": "face.png", "mime_type": "image/png", "trusted_local": True}])

        api = _api(tmp_path, controller, generate)
        response = await api.start_life_draft({"action": "generate_draft", "request_id": "r"})
        job = await api._life_jobs.get(response["data"]["id"], 123)
        await job.task
        state = await api.get_life_draft({"id": job.id})
        media = state["data"]["media"][0]
        assert "source" not in str(state) and "data:image" not in str(state)
        preview = await api.life_draft_preview({"job_id": job.id, "media_id": media["media_id"]})
        assert preview["data"]["data_url"].startswith("data:image/jpeg;base64,")
        assert len(base64.b64decode(preview["data"]["data_url"].split(",", 1)[1])) <= 128 * 1024
        await api.close()

    asyncio.run(scenario())


def test_preview_routes_trusted_http_through_existing_source_policy(monkeypatch, tmp_path):
    async def scenario():
        controller = Controller()
        api = _api(tmp_path, controller, lambda _progress: None)
        job, _ = await api._life_jobs.admit("r", 123)
        await api._life_jobs.succeed(job.id, draft_id=1, content="x", media=[], media_sources={
            "mid": {"kind": "image", "source": "https://media.example.test/a.jpg?sig=SECRET", "name": "a.jpg", "mime_type": "image/jpeg", "trusted_local": True},
        }, has_media=True, message="ok")
        api._life_owned_media["mid"] = (job.id, "up")
        seen = []

        async def stream(_media):
            seen.append(_media["source"])
            async def body():
                yield _png()
            return body(), 200, {}

        monkeypatch.setattr("qzone_bridge.page_api.open_page_media_stream", stream)
        image = await api._life_make_thumbnail(job, "mid")
        assert image.startswith(b"\xff\xd8") and "SECRET" in seen[0]
        await api.close()

    asyncio.run(scenario())


def test_preview_failure_is_retained_separately_and_same_failure_keeps_id(monkeypatch, tmp_path):
    async def scenario():
        controller = Controller()
        api = _api(tmp_path, controller, lambda _progress: None)
        job, _ = await api._life_jobs.admit("r", 123)
        await api._life_jobs.succeed(job.id, draft_id=1, content="x", media=[], media_sources={
            "mid": {"kind": "image", "source": "https://media.example.test/a.jpg?TOKEN_SENTINEL", "name": "a.jpg", "mime_type": "image/jpeg"},
        }, has_media=True, message="ok")
        api._life_owned_media["mid"] = (job.id, "up")
        original = {"id": "0123456789ab", "code": "REQUEST_ERROR", "phase": "controller_call",
                    "message": "请求处理失败", "at": 1.0}
        await api._life_jobs.begin_publish(job.id, 123, "digest")
        await api._life_jobs.mark_unknown(job.id, diagnostic=original)
        count = []
        async def rejected(_media):
            count.append(1)
            raise QzoneParseError("https://media.example.test/a.jpg?TOKEN_SENTINEL")
        monkeypatch.setattr("qzone_bridge.page_api.open_page_media_stream", rejected)
        ids = []
        for _ in range(2):
            with pytest.raises(PageLifeError) as caught:
                await api.life_draft_preview({"job_id": job.id, "media_id": "mid"})
            ids.append(caught.value.message.split()[-1])
            state = (await api.get_life_draft({"id": job.id}))["data"]
            assert state["publish_diagnostic"] == original
            assert state["preview_diagnostic"]["code"] == "REMOTE_POLICY_FAILED"
            assert "TOKEN_SENTINEL" not in str(state) + caught.value.message
        assert count == [1, 1] and ids[0] == ids[1]
        assert len(api._life_preview_inflight) == 0 and controller.published == []
        await api.close()
    asyncio.run(scenario())


def test_preview_failure_rechecks_owner_before_retaining_or_disclosing(monkeypatch, tmp_path):
    async def scenario():
        controller = Controller()
        api = _api(tmp_path, controller, lambda _progress: None)
        job, _ = await api._life_jobs.admit("r", 123)
        await api._life_jobs.succeed(job.id, draft_id=1, content="x", media=[], media_sources={
            "mid": {"kind": "image", "source": "https://media.example.test/a.jpg", "name": "a.jpg", "mime_type": "image/jpeg"},
        }, has_media=True, message="ok")
        api._life_owned_media["mid"] = (job.id, "up")
        async def switch_then_fail(_media):
            controller.status["login_uin"] = 456
            raise QzoneParseError("untrusted private detail")
        monkeypatch.setattr("qzone_bridge.page_api.open_page_media_stream", switch_then_fail)
        with pytest.raises(PageLifeError) as caught:
            await api.life_draft_preview({"job_id": job.id, "media_id": "mid"})
        assert caught.value.code == "LIFE_ACCOUNT_CHANGED"
        assert job.preview_diagnostic is None
        await api.close()
    asyncio.run(scenario())


def test_preview_cancellation_closes_stream_and_retains_fixed_record(monkeypatch, tmp_path):
    async def scenario():
        controller = Controller()
        api = _api(tmp_path, controller, lambda _progress: None)
        job, _ = await api._life_jobs.admit("r", 123)
        await api._life_jobs.succeed(job.id, draft_id=1, content="x", media=[], media_sources={
            "mid": {"kind": "image", "source": "https://media.example.test/a.jpg", "name": "a.jpg", "mime_type": "image/jpeg"},
        }, has_media=True, message="ok")
        api._life_owned_media["mid"] = (job.id, "up")
        started, closed = asyncio.Event(), asyncio.Event()
        async def stream_body():
            try:
                started.set()
                await asyncio.Future()
                yield b""
            finally:
                closed.set()
        class Stream:
            def __aiter__(self): return stream_body()
            async def aclose(self): closed.set()
        async def open_stream(_media): return Stream(), 200, {}
        monkeypatch.setattr("qzone_bridge.page_api.open_page_media_stream", open_stream)
        task = asyncio.create_task(api.life_draft_preview({"job_id": job.id, "media_id": "mid"}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        record = (await api.get_life_draft({"id": job.id}))["data"]["preview_diagnostic"]
        assert record["code"] == "CANCELLED" and record["phase"] == "preview_wait"
        await api.close()
    asyncio.run(scenario())


def test_preview_rechecks_current_account_after_thumbnail_processing(monkeypatch, tmp_path):
    async def scenario():
        controller = Controller()
        api = _api(tmp_path, controller, lambda _progress: None)
        job, _ = await api._life_jobs.admit("r", 123)
        media = {"kind": "image", "source": "https://media.example.test/a.jpg", "name": "a.jpg", "mime_type": "image/jpeg"}
        await api._life_jobs.succeed(job.id, draft_id=1, content="x", media=[{"kind": "image", "upload_id": "up", "media_id": "mid"}], media_sources={"mid": media}, has_media=True, message="ok")
        api._life_owned_media["mid"] = (job.id, "up")

        async def stream(_media):
            async def body():
                controller.status["login_uin"] = 456
                yield _png()
            return body(), 200, {}

        monkeypatch.setattr("qzone_bridge.page_api.open_page_media_stream", stream)
        with pytest.raises(PageLifeError, match="账号已变化"):
            await api.life_draft_preview({"job_id": job.id, "media_id": "mid"})
        await api.close()

    asyncio.run(scenario())


def test_encoded_preview_size_is_checked_before_decode(monkeypatch, tmp_path):
    async def scenario():
        controller = Controller()
        api = _api(tmp_path, controller, lambda _progress: None)
        job, _ = await api._life_jobs.admit("r", 123)
        huge = "data:image/png;base64," + ("A" * (8 * 1024 * 1024 * 2))
        await api._life_jobs.succeed(job.id, draft_id=1, content="x", media=[], media_sources={
            "mid": {"kind": "image", "source": huge, "name": "a.png", "mime_type": "image/png"},
        }, has_media=True, message="ok")

        def forbidden_decode(_source):
            raise AssertionError("oversized data must be rejected before decoding")

        monkeypatch.setattr(api, "_decode_upload_source", forbidden_decode)
        with pytest.raises(ValueError):
            await api._life_make_thumbnail(job, "mid")
        await api.close()

    asyncio.run(scenario())


def test_life_publish_postprocessing_failure_marks_unknown_and_never_retries(tmp_path, monkeypatch):
    async def scenario():
        controller = Controller()

        async def generate(_progress):
            return PostPayload("daily", [])

        api = _api(tmp_path, controller, generate)
        start = await api.start_life_draft({"action": "generate_draft", "request_id": "r"})
        job = await api._life_jobs.get(start["data"]["id"], 123)
        await job.task

        def broken_author(_status):
            raise RuntimeError("private detail")

        monkeypatch.setattr(api, "_login_author_payload", broken_author)
        with pytest.raises(PageLifeError, match="结果尚不确定"):
            await api.publish({"content": "daily", "life_job_id": job.id})
        assert len(controller.published) == 1
        assert job.publish_state == "unknown"
        assert job.publish_diagnostic["code"] == "INTERNAL_ERROR"
        assert job.publish_diagnostic["phase"] == "result_processing"
        assert "private detail" not in str((await api.get_life_draft({"id": job.id}))["data"])
        with pytest.raises(PageLifeError, match="不确定"):
            await api.publish({"content": "daily", "life_job_id": job.id})
        assert len(controller.published) == 1
        await api.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(("failure", "expected_code"), [
    (TypeError("TOKEN secret call args"), "INTERFACE_ERROR"),
    (QzoneRequestError("https://HOST/path?TOKEN=secret", detail={"rkey": "secret"}), "REQUEST_ERROR"),
])
def test_life_publish_diagnostic_is_safe_retained_and_never_retries(tmp_path, failure, expected_code):
    async def scenario():
        controller = Controller()
        calls = []
        async def fail(**kwargs):
            calls.append(kwargs)
            raise failure
        controller.publish_post = fail
        async def generate(_progress):
            return PostPayload("daily", [])
        api = _api(tmp_path, controller, generate)
        start = await api.start_life_draft({"action": "generate_draft", "request_id": "r"})
        job = await api._life_jobs.get(start["data"]["id"], 123)
        await job.task
        with pytest.raises(PageLifeError) as caught:
            await api.publish({"content": "daily", "life_job_id": job.id})
        assert caught.value.code == "LIFE_UNKNOWN"
        assert expected_code in caught.value.message
        assert job.publish_state == "unknown" and len(calls) == 1
        public = (await api.get_life_draft({"id": job.id}))["data"]["publish_diagnostic"]
        assert set(public) == {"id", "code", "phase", "message", "at"}
        assert public["code"] == expected_code and public["phase"] == "controller_call"
        assert len(public["id"]) == 12
        assert not any(secret in str(public) + caught.value.message for secret in ("TOKEN", "HOST", "secret", "rkey", "https://"))
        with pytest.raises(PageLifeError) as repeated:
            await api.publish({"content": "daily", "life_job_id": job.id})
        again = (await api.get_life_draft({"id": job.id}))["data"]["publish_diagnostic"]
        assert len(calls) == 1 and again == public and public["id"] in repeated.value.message
        await api.close()
    asyncio.run(scenario())


def test_life_publish_classifier_avoids_hostile_exception_class_properties(tmp_path):
    class HostileMeta(type):
        def __hash__(cls):
            raise RuntimeError("TOKEN_HASH_SENTINEL")
        def __eq__(cls, _other):
            raise RuntimeError("TOKEN_EQ_SENTINEL")
    class HostileFailure(Exception, metaclass=HostileMeta):
        @property
        def __class__(self):
            raise RuntimeError("SHOULD_NOT_BE_READ")
    async def scenario():
        controller = Controller()
        calls = []
        async def fail(**_kwargs):
            calls.append(1)
            raise HostileFailure("COOKIE_SENTINEL")
        controller.publish_post = fail
        api = _api(tmp_path, controller, lambda _progress: asyncio.sleep(0, result=PostPayload("daily", [])))
        start = await api.start_life_draft({"action": "generate_draft", "request_id": "hostile"})
        job = await api._life_jobs.get(start["data"]["id"], 123)
        await job.task
        with pytest.raises(PageLifeError) as caught:
            await api.publish({"content": "daily", "life_job_id": job.id})
        assert job.publish_state == "unknown" and job.publish_diagnostic["code"] == "INTERNAL_ERROR"
        assert calls == [1] and not any(secret in caught.value.message for secret in (
            "COOKIE_SENTINEL", "TOKEN_CODE_SENTINEL", "TOKEN_DETAIL_SENTINEL",
            "TOKEN_HASH_SENTINEL", "TOKEN_EQ_SENTINEL", "SHOULD_NOT_BE_READ",
        ))
        await api.close()
    asyncio.run(scenario())


def test_bookkeeping_failure_after_mark_published_keeps_published_and_diagnostic(tmp_path, monkeypatch):
    async def scenario():
        controller = Controller()
        async def generate(_progress):
            return PostPayload("daily", [])
        api = _api(tmp_path, controller, generate)
        start = await api.start_life_draft({"action": "generate_draft", "request_id": "bookkeeping"})
        job = await api._life_jobs.get(start["data"]["id"], 123)
        await job.task
        original = api._life_jobs.mark_published
        async def mark_then_fail(job_id, result):
            await original(job_id, result)
            raise RuntimeError("private bookkeeping detail")
        monkeypatch.setattr(api._life_jobs, "mark_published", mark_then_fail)
        with pytest.raises(PageLifeError):
            await api.publish({"content": "daily", "life_job_id": job.id})
        assert len(controller.published) == 1 and job.publish_state == "published"
        assert job.publish_diagnostic["phase"] == "bookkeeping"
        assert "private bookkeeping detail" not in str((await api.get_life_draft({"id": job.id}))["data"])
        result = await api.publish({"content": "daily", "life_job_id": job.id})
        assert result["ok"] and len(controller.published) == 1
        await api.close()
    asyncio.run(scenario())


def test_cancelled_life_publish_retains_unknown_and_never_retries(tmp_path):
    async def scenario():
        controller = Controller()
        entered = asyncio.Event()
        calls = []
        async def wait_publish(**_kwargs):
            calls.append(1)
            entered.set()
            await asyncio.Future()
        controller.publish_post = wait_publish
        async def generate(_progress): return PostPayload("daily", [])
        api = _api(tmp_path, controller, generate)
        start = await api.start_life_draft({"action": "generate_draft", "request_id": "cancel"})
        job = await api._life_jobs.get(start["data"]["id"], 123)
        await job.task
        request = asyncio.create_task(api.publish({"content": "daily", "life_job_id": job.id}))
        await entered.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert job.publish_state == "unknown" and job.publish_diagnostic["code"] == "CANCELLED"
        with pytest.raises(PageLifeError):
            await api.publish({"content": "daily", "life_job_id": job.id})
        assert calls == [1]
        await api.close()
    asyncio.run(scenario())


def test_life_publish_real_controller_transport_failure_retains_safe_diagnostic(tmp_path, monkeypatch):
    async def scenario():
        controller = QzoneDaemonController(plugin_root=tmp_path, data_dir=tmp_path / "data", auto_start_daemon=False)
        state = controller.store.read()
        state.runtime.secret = "COOKIE_SENTINEL"
        state.runtime.daemon_port = 8765
        state.session = SessionState(uin=123, cookies={"uin": "o123", "p_skey": "TOKEN_SENTINEL"})
        controller.store.write(state)
        controller._health_cache_data = (8765, "COOKIE_SENTINEL", {
            "daemon_state": "ready", "login_uin": 123, "cookie_count": 2, "needs_rebind": False,
        })
        async def healthy(_port):
            return True
        monkeypatch.setattr(controller, "_probe_health", healthy)
        calls = []
        async def transport(method, url, **kwargs):
            calls.append((method, url, kwargs))
            raise httpx.ConnectError("URL_SENTINEL TOKEN_SENTINEL")
        monkeypatch.setattr(controller._client, "request", transport)
        async def generate(_progress):
            return PostPayload("daily", [])
        api = _api(tmp_path, controller, generate)
        start = await api.start_life_draft({"action": "generate_draft", "request_id": "real-controller"})
        job = await api._life_jobs.get(start["data"]["id"], 123)
        await job.task
        with pytest.raises(PageLifeError) as caught:
            await api.publish({"content": "daily", "life_job_id": job.id})
        assert len(calls) == 1 and calls[0][0] == "POST" and calls[0][1].endswith("/post")
        assert calls[0][2]["json"]["expected_login_uin"] == 123
        assert job.publish_state == "unknown"
        record = (await api.get_life_draft({"id": job.id}))["data"]["publish_diagnostic"]
        assert record["code"] == "DAEMON_UNAVAILABLE" and record["phase"] == "controller_call"
        assert record["id"] in caught.value.message
        assert "SENTINEL" not in str(record) + caught.value.message
        await api.close()
        await controller._client.aclose()
    asyncio.run(scenario())


def test_life_publish_cached_post_never_exposes_signed_media_url(tmp_path):
    async def scenario():
        controller = Controller()

        async def generate(_progress):
            return PostPayload("daily", [{
                "kind": "image", "source": "https://media.example.test/a.jpg?sig=SECRET", "name": "a.jpg",
                "mime_type": "image/jpeg", "trusted_local": True,
            }])

        api = _api(tmp_path, controller, generate)
        start = await api.start_life_draft({"action": "generate_draft", "request_id": "r"})
        job = await api._life_jobs.get(start["data"]["id"], 123)
        await job.task
        public_media = (await api.get_life_draft({"id": job.id}))["data"]["media"]
        result = await api.publish({"content": "daily", "media": public_media, "life_job_id": job.id})
        assert "SECRET" not in str(result)
        assert "media.example.test" not in str(result)
        assert "SECRET" not in str(job.publish_result)
        await api.close()

    asyncio.run(scenario())


def test_close_revokes_all_and_only_life_upload_tokens_and_preserves_draft_source(tmp_path):
    async def scenario():
        controller = Controller()
        original = tmp_path / "generated.jpg"
        original.write_bytes(_png())

        async def generate(_progress):
            return PostPayload("daily", [{
                "kind": "image", "source": str(original), "name": "generated.jpg", "mime_type": "image/jpeg", "trusted_local": True,
            }])

        api = _api(tmp_path, controller, generate)
        normal = {"kind": "image", "source": str(original), "upload_id": "ordinary_upload", "trusted_local": True}
        api._uploaded_media_by_id["ordinary_upload"] = normal
        start = await api.start_life_draft({"action": "generate_draft", "request_id": "r"})
        job = await api._life_jobs.get(start["data"]["id"], 123)
        await job.task
        life_upload = job.media[0]["upload_id"]
        assert life_upload in api._uploaded_media_by_id and original.exists()
        assert len(await api.draft_store.list_async()) == 1
        await api.close()
        assert life_upload not in api._uploaded_media_by_id
        assert life_upload not in api._life_owned_upload_ids
        assert "ordinary_upload" in api._uploaded_media_by_id
        assert original.exists() and len(await api.draft_store.list_async()) == 1

        controller.status["login_uin"] = 456
        for body in (
            {"content": "old life media", "media": [{"upload_id": life_upload}]},
            {"content": "old life media", "life_job_id": job.id, "media": [{"upload_id": life_upload}]},
        ):
            with pytest.raises((QzoneParseError, PageLifeError)):
                await api.publish(body)
        assert controller.published == []

    asyncio.run(scenario())


def test_close_cancels_late_generation_after_life_upload_registration(tmp_path, monkeypatch):
    async def scenario():
        controller = Controller()
        entered_save = asyncio.Event()

        async def generate(_progress):
            return PostPayload("daily", [{
                "kind": "image", "source": "https://media.example.test/a.jpg", "name": "a.jpg", "mime_type": "image/jpeg",
            }])

        api = _api(tmp_path, controller, generate)

        async def delayed_save(**_kwargs):
            entered_save.set()
            await asyncio.Future()

        monkeypatch.setattr(api.draft_store, "add_async", delayed_save)
        api._uploaded_media_by_id["ordinary_upload"] = {"kind": "image", "source": "local", "upload_id": "ordinary_upload"}
        start = await api.start_life_draft({"action": "generate_draft", "request_id": "r"})
        job = await api._life_jobs.get(start["data"]["id"], 123)
        await entered_save.wait()
        life_tokens = set(api._life_owned_upload_ids)
        assert len(life_tokens) == 1
        await api.close()
        assert job.state == "cancelled"
        assert not api._life_owned_upload_ids
        assert not (life_tokens & set(api._uploaded_media_by_id))
        assert "ordinary_upload" in api._uploaded_media_by_id
        assert controller.published == []

    asyncio.run(scenario())
