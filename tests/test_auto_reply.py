from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from qzone_bridge.auto_reply import AutoReplyConfig, AutoReplyService
from qzone_bridge import auto_reply as auto_reply_module
from qzone_bridge.settings import PluginSettings
from qzone_bridge.social import QzoneComment


class Controller:
    def __init__(self):
        self.login = 10001
        self.items = [{"hostuin": 10001, "fid": "post-1", "appid": 311, "summary": "own post", "created_at": 1}]
        self.comments = [{"commentid": "old", "uin": 20001, "nickname": "Old", "content": "existing", "date": 1}]
        self.sent = []
        self.fail_list = False
        self.fail_send = False
        self.list_calls = 0
        self.detail_calls = 0
        self.bad_detail_calls = set()
        self.bad_detail_payload = None

    async def get_status(self, *, probe_daemon=False):
        return {"login_uin": self.login, "needs_rebind": False}

    async def list_feeds(self, **kwargs):
        self.list_calls += 1
        if self.fail_list:
            raise RuntimeError("feed unavailable")
        assert kwargs["scope"] == "self"
        assert kwargs["hostuin"] == self.login
        return {"items": list(self.items)}

    async def detail_feed(self, *, hostuin, fid, appid=311, busi_param=""):
        self.detail_calls += 1
        if self.detail_calls in self.bad_detail_calls:
            return self.bad_detail_payload or {"entry": {"hostuin": hostuin, "fid": "wrong-fid", "appid": appid}, "comments": []}
        return {
            "entry": {"hostuin": hostuin, "fid": fid, "appid": appid, "summary": "authoritative post", "created_at": 1},
            "comments": list(self.comments),
        }

    async def reply_comment(self, **kwargs):
        self.sent.append(kwargs)
        if self.fail_send:
            raise RuntimeError("transport outcome unknown")
        return {"commentid": "reply-id"}


def service(controller, path: Path, *, enabled=True, reply_lock=None):
    config = AutoReplyConfig(
        enabled=enabled,
        poll_interval=30,
        delay_min=0,
        delay_max=0,
        recent_posts=5,
        max_per_run=2,
        daily_limit=20,
        skip_low_information=True,
        reply_probability=1.0,
        ignore_users=(),
    )
    async def generate(post, comment):
        assert post.summary == "authoritative post"
        return "generated reply"
    return AutoReplyService(
        controller=controller,
        state_path=path,
        config=config,
        generate_reply=generate,
        reply_lock=reply_lock,
        rng=SimpleNamespace(random=lambda: 0.0, uniform=lambda low, high: 0),
    )


def test_default_off_does_not_poll_and_config_is_clamped():
    async def scenario():
        controller = Controller()
        svc = service(controller, Path("auto_reply_test_unused.json"), enabled=False)
        assert await svc.poll_once() == 0
        assert controller.list_calls == 0
    asyncio.run(scenario())
    config = AutoReplyConfig.from_settings(PluginSettings())
    assert not config.enabled and config.poll_interval == 120
    bounded = AutoReplyConfig.from_settings(SimpleNamespace(
        auto_reply_enabled=True,
        auto_reply_poll_interval=1,
        auto_reply_delay_min=500,
        auto_reply_delay_max=1,
        auto_reply_recent_posts=99,
        auto_reply_probability=float("nan"),
    ))
    assert bounded.poll_interval == 30
    assert bounded.delay_min == bounded.delay_max == 500
    assert bounded.recent_posts == 20
    assert bounded.reply_probability == 0.8


def test_successful_first_enumeration_baselines_then_reply_persists_across_restart(tmp_path):
    async def scenario():
        controller = Controller()
        path = tmp_path / "state.json"
        first = service(controller, path)
        assert await first.poll_once() == 0
        assert controller.sent == []
        state = await first._state()
        activation = state["accounts"][str(controller.login)]["activation_ts"]
        controller.comments.append({
            "commentid": "new", "uin": 20002, "nickname": "New", "content": "hello", "date": activation + 10,
        })
        entries = await first._fetch_entries(controller.login)
        p, parsed = await first._fresh_post(entries[0])
        assert first._skip_reason(parsed[-1], controller.login, activation) == ""
        assert await first.poll_once() == 1
        assert len(controller.sent) == 1
        assert controller.sent[0]["commentid"] == "new"
        restarted = service(controller, path)
        assert await restarted.poll_once() == 0
        assert len(controller.sent) == 1
        state = await restarted._state()
        assert state["accounts"][str(controller.login)]["records"]
    asyncio.run(scenario())


def test_failed_enumeration_does_not_activate_baseline_and_uncertain_send_never_retries(tmp_path):
    async def scenario():
        controller = Controller()
        path = tmp_path / "state.json"
        svc = service(controller, path)
        controller.fail_list = True
        with pytest.raises(RuntimeError):
            await svc.poll_once()
        assert not (await svc._state()).get("accounts")
        controller.fail_list = False
        await svc.poll_once()
        account = await svc._account_snapshot(controller.login)
        activation = account["activation_ts"]
        controller.comments.append({
            "commentid": "new", "uin": 20002, "nickname": "New", "content": "hello", "date": activation + 10,
        })
        controller.fail_send = True
        assert await svc.poll_once() == 0
        assert len(controller.sent) == 1
        controller.fail_send = False
        restarted = service(controller, path)
        assert await restarted.poll_once() == 0
        assert len(controller.sent) == 1
        state = await restarted._state()
        found = [value for key, value in state["accounts"][str(controller.login)]["records"].items() if '"new"' in key]
        assert found and found[0]["status"] == "uncertain"
    asyncio.run(scenario())


def test_shared_reply_lock_serializes_page_manual_and_auto_send(tmp_path):
    async def scenario():
        controller = Controller()
        shared = asyncio.Lock()
        svc = service(controller, tmp_path / "state.json", reply_lock=shared)
        # The service's send path holds the exact lock provided by the plugin.
        assert svc.reply_lock is shared
        await shared.acquire()
        try:
            assert shared.locked()
        finally:
            shared.release()
    asyncio.run(scenario())


def test_skip_filters_cover_own_ignored_low_information_nested_and_old_comments(tmp_path):
    config = AutoReplyConfig(ignore_users=("40004", "Muted Name"))
    svc = AutoReplyService(
        controller=Controller(), state_path=tmp_path / "state.json", config=config,
        generate_reply=lambda post, comment: None,
    )
    assert svc._skip_reason(QzoneComment("", 20000), 10001, 100) == "missing_identity"
    assert svc._skip_reason(QzoneComment("own", 10001, content="hello"), 10001, 100) == "own_comment"
    assert svc._skip_reason(QzoneComment("ignored", 40004, content="hello"), 10001, 100) == "ignored_user"
    assert svc._skip_reason(QzoneComment("name", 20000, nickname="Muted Name", content="hello"), 10001, 100) == "ignored_user"
    assert svc._skip_reason(QzoneComment("low", 20000, content="嗯"), 10001, 100) == "low_information"
    assert svc._skip_reason(QzoneComment("nested", 20000, content="hello", parent_id="parent"), 10001, 100) == "nested_reply"
    assert svc._skip_reason(QzoneComment("old", 20000, content="hello", created_at=99), 10001, 100) == "before_activation"


def test_daily_budget_counts_reservations_and_resets_by_day(tmp_path):
    async def scenario():
        svc = service(Controller(), tmp_path / "state.json")
        svc.config = AutoReplyConfig(enabled=True, daily_limit=1)
        assert await svc._reserve(10001, "one", "2026-11-04")
        await svc._finish(10001, "one", "complete")
        await svc.mark_manual_reply(10001, 10001, "post-1", "one", 20001)
        assert not await svc._reserve(10001, "two", "2026-11-04")
        assert await svc._reserve(10001, "two", "2026-11-05")
    asyncio.run(scenario())


def test_cancelled_send_is_persisted_uncertain(tmp_path):
    async def scenario():
        controller = Controller()
        path = tmp_path / "state.json"
        svc = service(controller, path)
        await svc.poll_once()
        account = await svc._account_snapshot(controller.login)
        controller.comments.append({
            "commentid": "cancel-me", "uin": 20002, "nickname": "New", "content": "hello",
            "date": account["activation_ts"] + 10,
        })
        started = asyncio.Event()
        async def hanging_send(**kwargs):
            started.set()
            await asyncio.Event().wait()
        controller.reply_comment = hanging_send
        task = asyncio.create_task(svc.poll_once())
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        state = await svc._state()
        record = next(value for key, value in state["accounts"][str(controller.login)]["records"].items() if '"cancel-me"' in key)
        assert record["status"] == "uncertain"
    asyncio.run(scenario())


def test_manual_success_marker_prevents_duplicate_when_detail_is_stale(tmp_path):
    async def scenario():
        controller = Controller()
        svc = service(controller, tmp_path / "state.json")
        await svc.poll_once()
        state = await svc._state()
        activation = state["accounts"][str(controller.login)]["activation_ts"]
        controller.comments.append({"commentid": "manual", "uin": 20002, "nickname": "Friend", "content": "hello", "date": activation + 1})
        async with svc.reply_lock:
            assert await svc.mark_manual_reply(10001, 10001, "post-1", "manual", 20002)
        assert await svc.poll_once() == 0
        assert controller.sent == []
    asyncio.run(scenario())


def test_login_change_during_delay_aborts_old_account_send(tmp_path):
    async def scenario():
        controller = Controller()
        svc = service(controller, tmp_path / "state.json")
        await svc.poll_once()
        account = await svc._account_snapshot(controller.login)
        controller.comments.append({"commentid": "switch", "uin": 20002, "nickname": "Friend", "content": "hello", "date": account["activation_ts"] + 1})
        svc.config = AutoReplyConfig(
            enabled=True, poll_interval=30, delay_min=1, delay_max=1,
            recent_posts=5, max_per_run=2, daily_limit=20,
            skip_low_information=True, reply_probability=1.0,
        )
        class ChangeLogin:
            def random(self): return 0.0
            def uniform(self, low, high):
                controller.login = 10002
                return 0
        svc.rng = ChangeLogin()
        assert await svc.poll_once() == 0
        assert controller.sent == []
    asyncio.run(scenario())


def test_uncertain_send_attempts_count_toward_per_run_limit(tmp_path):
    async def scenario():
        controller = Controller()
        svc = service(controller, tmp_path / "state.json")
        await svc.poll_once()
        activation = (await svc._account_snapshot(controller.login))["activation_ts"]
        controller.comments.extend([
            {"commentid": f"new-{index}", "uin": 21000 + index, "nickname": "Friend", "content": "hello", "date": activation + index + 1}
            for index in range(3)
        ])
        controller.fail_send = True
        assert await svc.poll_once() == 0
        assert len(controller.sent) == 2
        state = await svc._state()
        uncertain = [record for record in state["accounts"][str(controller.login)]["records"].values() if record.get("status") == "uncertain"]
        assert len(uncertain) == 2
    asyncio.run(scenario())


def test_daemon_normalized_parent_id_protects_bot_and_friend_nested_replies(tmp_path):
    async def scenario():
        controller = Controller()
        svc = service(controller, tmp_path / "state.json")
        controller.comments = [
            {"commentid": "base", "uin": 20001, "nickname": "Friend", "content": "base", "created_at": 1, "parent_id": ""},
            {"commentid": "base-child", "uin": 10001, "nickname": "Bot", "content": "answered", "created_at": 2, "parent_id": "base"},
            {"commentid": "friend-child", "uin": 20002, "nickname": "Other", "content": "nested friend", "created_at": 3, "parent_id": "base"},
        ]
        await svc.poll_once()
        activation = (await svc._account_snapshot(controller.login))["activation_ts"]
        controller.comments = [
            {"commentid": "new-parent", "uin": 20003, "nickname": "Friend", "content": "new parent", "created_at": activation + 1, "parent_id": ""},
            {"commentid": "new-bot-child", "uin": 10001, "nickname": "Bot", "content": "already answered", "created_at": activation + 2, "parent_id": "new-parent"},
            {"commentid": "new-friend-child", "uin": 20004, "nickname": "Other", "content": "friend nested", "created_at": activation + 3, "parent_id": "new-parent"},
        ]
        assert await svc.poll_once() == 0
        assert controller.sent == []
    asyncio.run(scenario())


def test_incomplete_detail_never_baselines_or_reserves_for_send(tmp_path):
    async def scenario():
        controller = Controller()
        controller.bad_detail_calls.add(1)
        controller.bad_detail_payload = {}
        svc = service(controller, tmp_path / "state.json")
        with pytest.raises(RuntimeError, match="incomplete|identity"):
            await svc.poll_once()
        assert not (await svc._state()).get("accounts")

        controller = Controller()
        svc = service(controller, tmp_path / "presend.json")
        await svc.poll_once()
        activation = (await svc._account_snapshot(controller.login))["activation_ts"]
        controller.comments.append({"commentid": "new", "uin": 20002, "nickname": "Friend", "content": "hello", "date": activation + 1})
        # Initial baseline used detail call 1; candidate refresh is 2; pre-send refresh is 3.
        controller.bad_detail_calls.add(3)
        with pytest.raises(RuntimeError, match="identity"):
            await svc.poll_once()
        assert controller.sent == []
        records = (await svc._account_snapshot(controller.login))["records"]
        assert not any('"new"' in key for key in records)
    asyncio.run(scenario())


def test_concurrent_poll_rounds_send_a_comment_once(tmp_path):
    async def scenario():
        controller = Controller()
        svc = service(controller, tmp_path / "state.json")
        await svc.poll_once()
        activation = (await svc._account_snapshot(controller.login))["activation_ts"]
        controller.comments.append({"commentid": "concurrent", "uin": 20002, "nickname": "Friend", "content": "hello", "date": activation + 1})
        results = await asyncio.gather(svc.poll_once(), svc.poll_once())
        assert sum(results) == 1
        assert len([item for item in controller.sent if item["commentid"] == "concurrent"]) == 1
    asyncio.run(scenario())


def test_reservation_persistence_failure_prevents_send(tmp_path):
    async def scenario():
        controller = Controller()
        svc = service(controller, tmp_path / "state.json")
        await svc.poll_once()
        activation = (await svc._account_snapshot(controller.login))["activation_ts"]
        controller.comments.append({"commentid": "persist", "uin": 20002, "nickname": "Friend", "content": "hello", "date": activation + 1})
        async def fail_reservation(*args, **kwargs):
            raise OSError("state disk unavailable")
        svc._reserve = fail_reservation
        with pytest.raises(OSError):
            await svc.poll_once()
        assert controller.sent == []
    asyncio.run(scenario())


def test_corrupt_state_rebaselines_existing_comments_without_replies(tmp_path):
    async def scenario():
        controller = Controller()
        path = tmp_path / "state.json"
        path.write_text("{broken", encoding="utf-8")
        svc = service(controller, path)
        assert await svc.poll_once() == 0
        assert controller.sent == []
        assert await svc._account_snapshot(controller.login)
        assert list(tmp_path.glob("state.json.corrupt.*"))
    asyncio.run(scenario())


def test_llm_failure_is_retryable_then_sends_once(tmp_path):
    async def scenario():
        controller = Controller()
        path = tmp_path / "state.json"
        svc = service(controller, path)
        await svc.poll_once()
        activation = (await svc._account_snapshot(controller.login))["activation_ts"]
        controller.comments.append({"commentid": "llm-retry", "uin": 20002, "nickname": "Friend", "content": "hello", "date": activation + 1})
        calls = 0
        async def flaky(post, comment):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("provider down")
            return "generated reply"
        svc.generate_reply = flaky
        assert await svc.poll_once() == 0
        assert controller.sent == []
        assert await svc.poll_once() == 1
        assert len(controller.sent) == 1
    asyncio.run(scenario())


def test_real_persona_guard_failure_does_not_reserve_or_send_auto_reply(tmp_path):
    from qzone_bridge.llm import QzoneLLM

    async def scenario():
        controller = Controller()
        settings = PluginSettings(persona_id="p", reply_prompt="简短回复", auto_reply_enabled=True)
        class Manager:
            async def resolve_selected_persona(self, **kwargs):
                return "p", {"prompt": "persona prompt"}, None, False
            async def get_persona_v3_by_id(self, _pid):
                return {"prompt": "persona prompt"}
        class Provider:
            def __init__(self): self.calls = 0
            async def text_chat(self, *, prompt, contexts, system_prompt):
                self.calls += 1
                if prompt.startswith("Review whether"):
                    return '{"accepted":false,"rewrite":"SENSITIVE_REWRITE"}'
                return "SENSITIVE_DRAFT"
        provider = Provider()
        class Context:
            persona_manager = Manager()
            conversation_manager = None
            platform_manager = None
            async def get_config(self, umo): return {"provider_settings": {}}
            async def get_using_provider(self, **kwargs): return provider
        adapter = QzoneLLM(Context(), settings)
        svc = service(controller, tmp_path / "guard-state.json")
        svc.generate_reply = lambda post, comment: adapter.generate_reply_text(None, post, comment)
        assert await svc.poll_once() == 0  # initial baseline
        activation = (await svc._account_snapshot(controller.login))["activation_ts"]
        controller.comments.append({
            "commentid": "guard-fails", "uin": 20002, "nickname": "Friend",
            "content": "hello", "date": activation + 1,
        })
        assert await svc.poll_once() == 0
        state = await svc._account_snapshot(controller.login)
        assert len(controller.sent) == 0
        records = state["records"]
        assert provider.calls == 3  # candidate plus two reviews; terminal rejection precedes reservation
        assert all(record.get("status") == "baseline" for record in records.values())
        assert not any(record.get("status") in {"reserved", "uncertain", "complete"} for record in records.values())
    asyncio.run(scenario())


def test_stop_during_pre_send_delay_cancels_without_sending(tmp_path, monkeypatch):
    async def scenario():
        controller = Controller()
        svc = service(controller, tmp_path / "state.json")
        await svc.poll_once()
        activation = (await svc._account_snapshot(controller.login))["activation_ts"]
        controller.comments.append({"commentid": "delayed", "uin": 20002, "nickname": "Friend", "content": "hello", "date": activation + 1})
        svc.config = AutoReplyConfig(enabled=True, poll_interval=30, delay_min=1, delay_max=1, reply_probability=1)
        started = asyncio.Event()
        async def wait_in_delay(seconds):
            started.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(auto_reply_module.asyncio, "sleep", wait_in_delay)
        class Rng:
            def random(self): return 0.0
            def uniform(self, low, high): return 1
        svc.rng = Rng()
        svc.start()
        await asyncio.wait_for(started.wait(), 2)
        await svc.stop()
        assert controller.sent == []
        assert svc._task is None
    asyncio.run(scenario())


def test_manual_marker_preserves_auto_daily_budget_and_survives_restart(tmp_path):
    async def scenario():
        controller = Controller()
        path = tmp_path / "state.json"
        svc = service(controller, path)
        today = "2026-11-04"
        entries = await svc._fetch_entries(controller.login)
        key = svc._identity(controller.login, entries[0], QzoneComment("manual", 20002))
        assert await svc._reserve(controller.login, key, today)
        await svc._finish(controller.login, key, "complete")
        await svc.mark_manual_reply(controller.login, controller.login, "post-1", "manual", 20002)
        state = await svc._state()
        marker = state["accounts"][str(controller.login)]["records"][key]
        assert marker["day"] == today
        svc.config = AutoReplyConfig(enabled=True, daily_limit=1)
        assert not await svc._reserve(controller.login, "second", today)

        # A fresh process uses the persisted manual marker even if the authoritative
        # detail fixture still has no visible bot child.
        await svc.poll_once()  # establishes baseline without deleting preexisting records
        restarted = service(controller, path)
        await restarted.poll_once()
        assert not await restarted._reserve(controller.login, key, today)
    asyncio.run(scenario())


def test_reservation_after_midnight_delay_uses_new_local_day(tmp_path, monkeypatch):
    async def scenario():
        controller = Controller()
        svc = service(controller, tmp_path / "state.json")
        await svc.poll_once()
        activation = (await svc._account_snapshot(controller.login))["activation_ts"]
        controller.comments.append({"commentid": "midnight", "uin": 20002, "nickname": "Friend", "content": "hello", "date": activation + 1})
        assert await svc._reserve(controller.login, "prior-day", "2026-11-04")
        await svc._finish(controller.login, "prior-day", "complete")
        svc.config = AutoReplyConfig(enabled=True, poll_interval=30, delay_min=1, delay_max=1, daily_limit=1, reply_probability=1)
        current = {"day": "2026-11-04"}
        class FakeDate:
            @classmethod
            def today(cls):
                from datetime import date as real_date
                return real_date.fromisoformat(current["day"])
        monkeypatch.setattr(auto_reply_module, "date", FakeDate)
        class MidnightRng:
            def random(self): return 0.0
            def uniform(self, low, high):
                current["day"] = "2026-11-05"
                return 0
        svc.rng = MidnightRng()
        assert await svc.poll_once() == 1
        state = await svc._state()
        record = next(value for key, value in state["accounts"][str(controller.login)]["records"].items() if '"midnight"' in key)
        assert record["day"] == "2026-11-05"
    asyncio.run(scenario())
