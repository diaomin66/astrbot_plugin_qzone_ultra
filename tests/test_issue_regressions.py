from __future__ import annotations

import asyncio
import inspect
import types
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pytest

from qzone_bridge.scheduler import cron_delay_seconds
from test_security_hardening import _import_main_with_stubs


def test_cron_delay_applies_offset_only_after_the_base_time() -> None:
    now = datetime(2026, 7, 13, 20, 40, 0)
    calls: list[tuple[int, int]] = []

    def lowest_offset(start: int, end: int) -> int:
        calls.append((start, end))
        return start

    assert cron_delay_seconds("0 21 * * *", 1500, now=now, randint=lowest_offset) == 1200
    assert calls == [(0, 1500)]


def test_daemon_authentication_uses_constant_time_secret_comparison() -> None:
    from qzone_bridge.daemon import create_app

    source = inspect.getsource(create_app)
    assert "hmac.compare_digest(" in source
    assert "supplied_secret == service.state.runtime.secret" not in source


def test_auto_comment_stops_when_login_identity_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {"detail_calls": 0, "warnings": []}

    class _Logger:
        def debug(self, *args, **kwargs): ...
        def info(self, *args, **kwargs): ...
        def exception(self, *args, **kwargs): ...

        def warning(self, message, *args, **kwargs):
            captured["warnings"].append(message % args if args else str(message))

    class _Controller:
        async def list_feeds(self, **kwargs):
            return {
                "items": [
                    asdict(main.FeedEntry(hostuin=12345, fid="self-post", appid=311, summary="自己的动态"))
                ]
            }

        async def get_status(self, **kwargs):
            raise main.QzoneBridgeError("status unavailable")

        async def detail_feed(self, **kwargs):
            captured["detail_calls"] += 1
            raise AssertionError("identity probe failure must stop before selecting a post")

    async def ready(*args, **kwargs):
        return None

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(comment_latest_count=1, max_feed_limit=20)
    plugin.data_dir = tmp_path
    plugin.controller = _Controller()
    plugin._ensure_cookie_ready = ready
    plugin._ensure_daemon = ready
    monkeypatch.setattr(main, "logger", _Logger())

    asyncio.run(plugin._auto_comment_once())

    assert captured["detail_calls"] == 0
    assert any("cannot determine login_uin" in item for item in captured["warnings"])


@pytest.mark.parametrize("command_name", ["qzone_bind", "qzone_autobind"])
def test_bind_commands_report_status_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
    command_name: str,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Event:
        def is_admin(self):
            return True

        def stop_event(self):
            pass

        def plain_result(self, text):
            return text

    class _Controller:
        async def bind_cookie_local(self, cookie):
            return {"daemon_state": "starting", "needs_rebind": True}

    async def auto_bind(*args, **kwargs):
        return {"daemon_state": "starting", "needs_rebind": True}

    async def failed_status():
        raise main.QzoneBridgeError("refresh unavailable")

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(admin_uins=set())
    plugin.controller = _Controller()
    plugin._auto_bind_cookie = auto_bind
    plugin._status_with_recovery = failed_status
    plugin._schedule_publish_render_asset_preload = lambda *args, **kwargs: None
    plugin._schedule_daemon_warmup = lambda *args, **kwargs: None

    async def collect():
        command = getattr(plugin, command_name)
        args = (_Event(), "cookie") if command_name == "qzone_bind" else (_Event(),)
        return [item async for item in command(*args)]

    results = asyncio.run(collect())

    assert len(results) == 1
    assert "Cookie 已绑定，但状态刷新失败" in results[0]
    assert "/qzone status" in results[0]


def test_qzone_list_feed_returns_text_to_the_agent_instead_of_yielding_event_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Event:
        def is_admin(self):
            return True

        def get_self_id(self):
            return 12345

        def plain_result(self, text):
            raise AssertionError("LLM tools must return text instead of sending an event result")

    class _Controller:
        async def list_feeds(self, **kwargs):
            return {
                "items": [
                    asdict(main.FeedEntry(hostuin=12345, fid="fid-1", appid=311, summary="今天很好"))
                ]
            }

    async def ready(*args, **kwargs):
        return None

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(admin_uins=set(), max_feed_limit=20)
    plugin.controller = _Controller()
    plugin._ensure_cookie_ready = ready
    plugin._ensure_daemon = ready

    result = asyncio.run(plugin.tool_list_feed(_Event(), scope="self"))

    assert isinstance(result, str)
    assert "今天很好" in result


def test_all_qzone_llm_tools_are_coroutines_not_async_generators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    tool_names = (
        "tool_get_status",
        "tool_list_feed",
        "tool_detail_feed",
        "tool_view_post",
        "tool_publish_post",
        "tool_comment_post",
        "tool_delete_post",
        "tool_like_post",
    )

    for name in tool_names:
        method = getattr(main.QzoneStablePlugin, name)
        assert inspect.iscoroutinefunction(method), name
        assert not inspect.isasyncgenfunction(method), name
