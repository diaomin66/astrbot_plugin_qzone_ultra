from __future__ import annotations

import asyncio

import pytest

from qzone_bridge.page_life import PageLifeError, PageLifeJobStore


def test_admission_coalesces_active_same_account_and_request_replay():
    async def scenario():
        store = PageLifeJobStore()
        first, started = await store.admit("request-a", 123)
        same_request, replay_started = await store.admit("request-a", 123)
        other_tab, tab_started = await store.admit("request-b", 123)
        assert started and not replay_started and not tab_started
        assert first is same_request is other_tab
        assert other_tab.request_ids == {"request-a", "request-b"}
        await store.set_phase(first.id, "image")
        assert (await store.active_or_latest(123)).phase == "image"
        with pytest.raises(PageLifeError, match="另一个账号"):
            await store.admit("other-account", 456)
        assert await store.fail(first.id)
        replay, started_again = await store.admit("request-a", 123)
        assert replay.id == first.id and not started_again and replay.state == "failed"
        await store.close()

    asyncio.run(scenario())


def test_terminal_cache_is_bounded_to_three_and_expires_without_replay():
    async def scenario():
        now = [1000.0]
        store = PageLifeJobStore(clock=lambda: now[0])
        jobs = []
        for index in range(4):
            job, started = await store.admit(f"request-{index}", 123)
            assert started
            jobs.append(job)
            await store.fail(job.id)
            now[0] += 1
        with pytest.raises(PageLifeError, match="已过期"):
            await store.get(jobs[0].id, 123)
        assert (await store.get(jobs[1].id, 123)).id == jobs[1].id
        now[0] += 15 * 60
        with pytest.raises(PageLifeError, match="已过期"):
            await store.get(jobs[1].id, 123)
        replacement, started = await store.admit("request-0", 123)
        assert started and replacement.id != jobs[0].id
        await store.close()

    asyncio.run(scenario())


def test_publish_gate_is_single_consumer_and_returns_only_cached_same_digest():
    async def scenario():
        store = PageLifeJobStore()
        job, _ = await store.admit("r1", 123)
        await store.succeed(
            job.id, draft_id=7, content="caption", media=[{
                "kind": "image", "upload_id": "opaque", "source": "C:/private/a.png",
                "trusted_local": True,
            }], media_sources={}, has_media=True, message="草稿已保存。",
        )
        first, should_send = await store.begin_publish(job.id, 123, "digest-a")
        assert should_send and first.publish_state == "sending"
        with pytest.raises(PageLifeError, match="正在发布"):
            await store.begin_publish(job.id, 123, "digest-a")
        safe = {"message": "说说已发布。", "fid": "fid1", "media_count": 1,
                "photo_count": 1, "raw": {"cookie": "SECRET"}}
        await store.mark_published(job.id, safe)
        cached, should_send = await store.begin_publish(job.id, 123, "digest-a")
        assert cached is first and not should_send
        with pytest.raises(PageLifeError, match="编辑后的内容"):
            await store.begin_publish(job.id, 123, "digest-b")
        public = store.public_payload(first)
        assert public["publish_result"] == {
            "message": "说说已发布。", "fid": "fid1", "media_count": 1, "photo_count": 1,
        }
        assert public["media"] == [{"kind": "image", "upload_id": "opaque"}]
        assert "source" not in str(public) and "SECRET" not in str(public)
        await store.close()

    asyncio.run(scenario())


def test_public_diagnostic_allowlist_rejects_malformed_and_hostile_records():
    async def scenario():
        store = PageLifeJobStore()
        job, _ = await store.admit("r", 123)
        await store.fail(job.id)
        job.publish_diagnostic = {
            "id": "0123456789ab", "code": "INTERNAL_ERROR", "phase": "controller_call",
            "message": "内部处理发生错误", "at": 1.0, "secret": "TOKEN_SENTINEL",
        }
        assert store.public_payload(job)["publish_diagnostic"] is None
        class BadDict(dict):
            def get(self, *_args):
                raise AssertionError("untrusted mapping getter invoked")
        job.publish_diagnostic = BadDict({
            "id": "0123456789ab", "code": "INTERNAL_ERROR", "phase": "controller_call",
            "message": "内部处理发生错误", "at": 1.0,
        })
        assert store.public_payload(job)["publish_diagnostic"] is None
        job.publish_diagnostic = {
            "id": "0123456789ab", "code": "INTERNAL_ERROR", "phase": "controller_call",
            "message": "内部处理发生错误", "at": 10 ** 10000,
        }
        assert store.public_payload(job)["publish_diagnostic"] is None
        await store.close()
    asyncio.run(scenario())


def test_close_cancels_owned_generation_and_prevents_late_completion():
    async def scenario():
        store = PageLifeJobStore()
        job, _ = await store.admit("r1", 123)
        started = asyncio.Event()

        async def generation():
            started.set()
            await asyncio.Future()

        task = asyncio.create_task(generation())
        await store.attach_task(job.id, task)
        await started.wait()
        await store.close()
        assert task.cancelled()
        assert await store.succeed(
            job.id, draft_id=1, content="late", media=[], media_sources={},
            has_media=False, message="saved",
        ) is False

    asyncio.run(scenario())


@pytest.mark.parametrize("account_uin", [0, -1])
def test_admission_requires_positive_captured_account(account_uin: int):
    async def scenario():
        store = PageLifeJobStore()
        with pytest.raises(PageLifeError, match="绑定有效 QQ 账号"):
            await store.admit("request", account_uin)
        await store.close()

    asyncio.run(scenario())


def test_sending_job_is_pinned_from_ttl_and_capacity_pruning():
    async def scenario():
        now = [100.0]
        store = PageLifeJobStore(clock=lambda: now[0])
        job, _ = await store.admit("sending", 10)
        await store.succeed(job.id, draft_id=1, content="x", media=[], media_sources={}, has_media=False, message="ok")
        await store.begin_publish(job.id, 10, "digest")
        now[0] += 901
        assert await store.get(job.id, 10) is job
        for index in range(4):
            terminal, _ = await store.admit(f"terminal-{index}", 10)
            await store.fail(terminal.id)
        assert await store.get(job.id, 10) is job
        assert await store.mark_published(job.id, {"message": "ok"})
        assert job.publish_state == "published"
        await store.close()

    asyncio.run(scenario())


def test_closed_store_blocks_new_publish_but_records_dispatched_result():
    async def scenario():
        store = PageLifeJobStore()
        job, _ = await store.admit("sending", 10)
        await store.succeed(job.id, draft_id=1, content="x", media=[], media_sources={}, has_media=False, message="ok")
        await store.begin_publish(job.id, 10, "digest")
        await store.close()
        assert await store.mark_published(job.id, {"message": "confirmed"})
        assert job.publish_state == "published"

        other = PageLifeJobStore()
        ready, _ = await other.admit("ready", 10)
        await other.succeed(ready.id, draft_id=1, content="x", media=[], media_sources={}, has_media=False, message="ok")
        await other.close()
        with pytest.raises(PageLifeError, match="关闭"):
            await other.begin_publish(ready.id, 10, "digest")

    asyncio.run(scenario())


def test_active_job_caps_request_aliases_and_preserves_known_idempotency():
    async def scenario():
        store = PageLifeJobStore()
        job, started = await store.admit("request-0", 10)
        assert started
        for index in range(1, 16):
            same, started = await store.admit(f"request-{index}", 10)
            assert same is job and not started
        same, started = await store.admit("request-0", 10)
        assert same is job and not started
        with pytest.raises(PageLifeError, match="请求过多"):
            await store.admit("request-overflow", 10)
        await store.close()

    asyncio.run(scenario())


def test_all_terminal_slots_pinned_by_sends_reject_new_generation():
    async def scenario():
        store = PageLifeJobStore()
        jobs = []
        for index in range(3):
            job, _ = await store.admit(f"r{index}", 10)
            await store.succeed(job.id, draft_id=index + 1, content="x", media=[], media_sources={}, has_media=False, message="ok")
            await store.begin_publish(job.id, 10, f"d{index}")
            jobs.append(job)
        with pytest.raises(PageLifeError, match="发布正在处理中"):
            await store.admit("new-generation", 10)
        assert all(job.publish_state == "sending" for job in jobs)
        await store.close()

    asyncio.run(scenario())
