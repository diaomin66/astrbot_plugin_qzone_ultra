from __future__ import annotations

import asyncio
import base64
import importlib
import inspect
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import sys
import types

import httpx
import pytest

from qzone_bridge.client import QzoneClient
from qzone_bridge.auto_comment import (
    AutoCommentPipeline,
    AutoCommentPipelineConfig,
    AutoCommentStateStore,
)
from qzone_bridge.controller import QzoneDaemonController
from qzone_bridge.errors import DaemonUnavailableError, QzoneBridgeError, QzoneCookieAcquireError, QzoneParseError, QzoneRequestError
from qzone_bridge.models import BridgeState, SessionState
from qzone_bridge.settings import PluginSettings
from qzone_bridge.social import QzonePost
from qzone_bridge.storage import StateStore
from qzone_bridge import source_policy


def _install_astrbot_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Logger:
        def debug(self, *args, **kwargs): ...
        def info(self, *args, **kwargs): ...
        def warning(self, *args, **kwargs): ...
        def exception(self, *args, **kwargs): ...

    def _decorator(*args, **kwargs):
        def wrap(func):
            return func

        return wrap

    def _command_group(*args, **kwargs):
        def wrap(func):
            func.command = _decorator
            return func

        return wrap

    filter_stub = types.SimpleNamespace(
        command=_decorator,
        command_group=_command_group,
        llm_tool=_decorator,
        on_astrbot_loaded=_decorator,
        permission_type=_decorator,
        platform_adapter_type=_decorator,
        PermissionType=types.SimpleNamespace(ADMIN="admin"),
        PlatformAdapterType=types.SimpleNamespace(AIOCQHTTP="aiocqhttp"),
    )

    class _Star:
        def __init__(self, context=None):
            self.context = context

    monkeypatch.setitem(sys.modules, "astrbot", types.ModuleType("astrbot"))
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = _Logger()
    monkeypatch.setitem(sys.modules, "astrbot.api", api_module)
    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = object
    event_module.filter = filter_stub
    monkeypatch.setitem(sys.modules, "astrbot.api.event", event_module)
    star_module = types.ModuleType("astrbot.api.star")
    star_module.Context = object
    star_module.Star = _Star
    monkeypatch.setitem(sys.modules, "astrbot.api.star", star_module)


def _import_main_with_stubs(monkeypatch: pytest.MonkeyPatch):
    _install_astrbot_stubs(monkeypatch)
    sys.modules.pop("main", None)
    return importlib.import_module("main")


def test_main_import_recovers_from_stale_renderer_module(monkeypatch: pytest.MonkeyPatch) -> None:
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "qzone_bridge" or name.startswith("qzone_bridge.")
    }
    import qzone_bridge.publish_renderer as renderer

    try:
        monkeypatch.delattr(renderer, "combine_rendered_post_cards", raising=False)

        main = _import_main_with_stubs(monkeypatch)

        assert main.QzoneStablePlugin.__name__ == "QzoneStablePlugin"
    finally:
        for name in list(sys.modules):
            if name == "qzone_bridge" or name.startswith("qzone_bridge."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.modules.pop("main", None)


def test_main_import_recovers_from_renderer_without_comment_section_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "qzone_bridge" or name.startswith("qzone_bridge.")
    }
    import qzone_bridge.publish_renderer as renderer

    try:
        monkeypatch.delattr(renderer, "SUPPORTS_COMMENT_RESULT_SECTIONS", raising=False)

        main = _import_main_with_stubs(monkeypatch)

        assert main.QzoneStablePlugin.__name__ == "QzoneStablePlugin"
        assert getattr(main._publish_renderer, "SUPPORTS_COMMENT_RESULT_SECTIONS", False) is True
    finally:
        for name in list(sys.modules):
            if name == "qzone_bridge" or name.startswith("qzone_bridge."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.modules.pop("main", None)


def test_main_import_recovers_from_renderer_with_false_comment_section_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "qzone_bridge" or name.startswith("qzone_bridge.")
    }
    import qzone_bridge.publish_renderer as renderer

    try:
        monkeypatch.setattr(renderer, "SUPPORTS_COMMENT_RESULT_SECTIONS", False, raising=False)

        main = _import_main_with_stubs(monkeypatch)

        assert main.QzoneStablePlugin.__name__ == "QzoneStablePlugin"
        assert getattr(main._publish_renderer, "SUPPORTS_COMMENT_RESULT_SECTIONS", False) is True
    finally:
        for name in list(sys.modules):
            if name == "qzone_bridge" or name.startswith("qzone_bridge."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.modules.pop("main", None)


def test_main_import_reloads_cached_qzone_bridge_when_version_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "qzone_bridge" or name.startswith("qzone_bridge.")
    }
    import qzone_bridge

    try:
        expected_version = qzone_bridge.__version__
        monkeypatch.setattr(qzone_bridge, "__version__", "0.6.10", raising=False)

        main = _import_main_with_stubs(monkeypatch)

        assert main.QzoneStablePlugin.__name__ == "QzoneStablePlugin"
        assert sys.modules["qzone_bridge"].__version__ == expected_version
        assert sys.modules["qzone_bridge"] is not qzone_bridge
    finally:
        for name in list(sys.modules):
            if name == "qzone_bridge" or name.startswith("qzone_bridge."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.modules.pop("main", None)


def test_main_import_recovers_from_stale_media_without_video_collect_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "qzone_bridge" or name.startswith("qzone_bridge.")
    }
    import qzone_bridge.media as media_module

    try:
        monkeypatch.delattr(media_module, "collect_message_media", raising=False)

        main = _import_main_with_stubs(monkeypatch)

        assert callable(getattr(main, "collect_message_media", None))
    finally:
        for name in list(sys.modules):
            if name == "qzone_bridge" or name.startswith("qzone_bridge."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.modules.pop("main", None)


def test_main_import_recovers_from_stale_page_api_constructor(monkeypatch: pytest.MonkeyPatch) -> None:
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "qzone_bridge" or name.startswith("qzone_bridge.")
    }
    import qzone_bridge.page_api as page_api

    class _OldQzonePageApi:
        def __init__(self, *, controller, post_service_factory, settings, status_provider=None):
            self.controller = controller

    try:
        monkeypatch.setattr(page_api, "QzonePageApi", _OldQzonePageApi)

        main = _import_main_with_stubs(monkeypatch)

        assert main.QzonePageApi is not _OldQzonePageApi
        assert "preload_scheduler" in inspect.signature(main.QzonePageApi).parameters
    finally:
        for name in list(sys.modules):
            if name == "qzone_bridge" or name.startswith("qzone_bridge."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.modules.pop("main", None)


def test_main_import_recovers_from_stale_llm_without_news_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "qzone_bridge" or name.startswith("qzone_bridge.")
    }
    import qzone_bridge.llm as llm_module

    try:
        monkeypatch.delattr(llm_module.QzoneLLM, "generate_news_post_text", raising=False)

        main = _import_main_with_stubs(monkeypatch)

        assert callable(getattr(main.QzoneLLM, "generate_news_post_text", None))
    finally:
        for name in list(sys.modules):
            if name == "qzone_bridge" or name.startswith("qzone_bridge."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.modules.pop("main", None)


@pytest.mark.parametrize("missing_field", ["news_cron", "life_publish_enabled"])
def test_main_import_recovers_from_stale_settings_without_required_fields(
    monkeypatch: pytest.MonkeyPatch,
    missing_field: str,
) -> None:
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "qzone_bridge" or name.startswith("qzone_bridge.")
    }
    import qzone_bridge.settings as settings_module

    fields = {
        "news_cron",
        "news_offset",
        "news_provider_id",
        "news_prompt",
        "news_scopes",
        "news_keywords",
        "news_custom_rss_urls",
        "news_max_candidates",
        "news_recency_hours",
        "news_once_per_day",
        "news_max_post_length",
        "news_trust_env",
        "native_video_publish",
        "life_publish_enabled",
        "life_publish_use_life_context",
        "life_publish_use_llm_image_prompt",
        "life_publish_use_omnidraw_selfie",
        "life_publish_auto_caption",
        "life_publish_mode",
        "life_publish_failure_policy",
        "life_publish_aspect_ratio",
        "life_publish_size",
        "life_publish_extra_params",
        "life_publish_static_caption",
        "life_publish_image_prompt_template",
        "life_publish_caption_prompt",
    }
    fields.discard(missing_field)

    class _OldPluginSettings:
        __dataclass_fields__ = {field: object() for field in fields}

        @classmethod
        def from_mapping(cls, config):
            return cls()

    try:
        monkeypatch.setattr(settings_module, "PluginSettings", _OldPluginSettings)

        main = _import_main_with_stubs(monkeypatch)

        assert main.PluginSettings is not _OldPluginSettings
        assert "news_cron" in getattr(main.PluginSettings, "__dataclass_fields__", {})
        assert "life_publish_enabled" in getattr(main.PluginSettings, "__dataclass_fields__", {})
    finally:
        for name in list(sys.modules):
            if name == "qzone_bridge" or name.startswith("qzone_bridge."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.modules.pop("main", None)


def test_main_import_tolerates_missing_optional_renderer_exports(monkeypatch: pytest.MonkeyPatch) -> None:
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "qzone_bridge" or name.startswith("qzone_bridge.")
    }
    import qzone_bridge.publish_renderer as renderer

    try:
        for name in (
            "RenderProfile",
            "cached_avatar_source",
            "preload_publish_render_assets",
            "preload_static_render_assets",
            "profile_from_event",
            "render_publish_result_image",
        ):
            monkeypatch.delattr(renderer, name, raising=False)

        main = _import_main_with_stubs(monkeypatch)
        profile = main.RenderProfile(nickname="昵称", user_id="12345", avatar_source="", time_text="12:00")

        assert main.QzoneStablePlugin.__name__ == "QzoneStablePlugin"
        assert profile.nickname == "昵称"
    finally:
        for name in list(sys.modules):
            if name == "qzone_bridge" or name.startswith("qzone_bridge."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.modules.pop("main", None)


def test_auto_bind_cookie_retries_empty_fetch_before_success(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    attempts: list[str] = []
    sleeps: list[float] = []
    bound: dict[str, object] = {}

    class _Controller:
        async def get_status(self, *, probe_daemon=False):
            return {"cookie_count": 0, "needs_rebind": True}

        async def bind_cookie_local(self, cookie_text, *, uin=0, source="manual"):
            bound.update({"cookie_text": cookie_text, "uin": uin, "source": source})
            return {"cookie_count": 4, "needs_rebind": False, "login_uin": uin}

    class _Bot:
        async def call_action(self, action, **params):
            return {}

    class _Event:
        bot = _Bot()

    async def fake_fetch_cookie_text(bot, *, domain):
        attempts.append(domain)
        if len(attempts) < 3:
            return ""
        return "uin=o12345; p_uin=o12345; p_skey=secret; skey=secret"

    async def fake_sleep(delay):
        sleeps.append(delay)

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(auto_bind_cookie=True, cookie_domain="user.qzone.qq.com")
    plugin.controller = _Controller()
    plugin._onebot_client = None
    plugin._cookie_lock = None
    monkeypatch.setattr(main, "fetch_cookie_text", fake_fetch_cookie_text)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    result = asyncio.run(plugin._auto_bind_cookie(_Event(), source="test"))

    assert len(attempts) == 3
    assert sleeps == [main.AUTO_BIND_RETRY_DELAY_SECONDS, main.AUTO_BIND_RETRY_DELAY_SECONDS]
    assert bound == {
        "cookie_text": "uin=o12345; p_uin=o12345; p_skey=secret; skey=secret",
        "uin": 12345,
        "source": "test",
    }
    assert result["login_uin"] == 12345


def test_auto_bind_cookie_fails_after_three_fetch_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    attempts = 0
    sleeps: list[float] = []

    class _Controller:
        async def get_status(self, *, probe_daemon=False):
            return {"cookie_count": 0, "needs_rebind": True}

    class _Bot:
        async def call_action(self, action, **params):
            return {}

    class _Event:
        bot = _Bot()

    async def fake_fetch_cookie_text(bot, *, domain):
        nonlocal attempts
        attempts += 1
        return ""

    async def fake_sleep(delay):
        sleeps.append(delay)

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(auto_bind_cookie=True, cookie_domain="user.qzone.qq.com")
    plugin.controller = _Controller()
    plugin._onebot_client = None
    plugin._cookie_lock = None
    monkeypatch.setattr(main, "fetch_cookie_text", fake_fetch_cookie_text)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    with pytest.raises(QzoneCookieAcquireError):
        asyncio.run(plugin._auto_bind_cookie(_Event()))

    assert attempts == 3
    assert sleeps == [main.AUTO_BIND_RETRY_DELAY_SECONDS, main.AUTO_BIND_RETRY_DELAY_SECONDS]


def test_extract_onebot_client_skips_bogus_event_bot_and_finds_nested_api(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _BogusBot:
        pass

    class _RealApi:
        async def call_action(self, action, **params):
            return {"status": "ok", "action": action, "params": params}

    class _Event:
        bot = _BogusBot()
        platform = types.SimpleNamespace(adapter=types.SimpleNamespace(api=_RealApi()))

    client = main.QzoneStablePlugin._extract_onebot_client(_Event())

    assert client is _Event.platform.adapter.api
    assert client is not _Event.bot


def test_extract_onebot_client_prefers_nested_action_api_over_send_only_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _RealApi:
        async def call_action(self, action, **params):
            return {"status": "ok", "action": action, "params": params}

    class _Event:
        bot = types.SimpleNamespace(api=_RealApi())

        async def send_private_msg(self, **_params):
            return {"status": "ok"}

    client = main.QzoneStablePlugin._extract_onebot_client(_Event())

    assert client is _Event.bot.api
    assert client is not _Event


def test_auto_bind_cookie_does_not_retry_send_only_event_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Controller:
        async def get_status(self, *, probe_daemon=False):
            return {"cookie_count": 0, "needs_rebind": True}

    class _Event:
        async def send_private_msg(self, **_params):
            return {"status": "ok"}

    async def fail_fetch_cookie_text(*_args, **_kwargs):
        raise AssertionError("send-only wrappers cannot fetch cookies")

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(auto_bind_cookie=True, cookie_domain="user.qzone.qq.com")
    plugin.controller = _Controller()
    plugin._onebot_client = None
    plugin._context = None
    plugin._cookie_lock = None
    monkeypatch.setattr(main, "fetch_cookie_text", fail_fetch_cookie_text)

    with pytest.raises(QzoneCookieAcquireError):
        asyncio.run(plugin._auto_bind_cookie(_Event()))


def test_auto_bind_cookie_uses_nested_action_api_instead_of_send_only_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Controller:
        async def get_status(self, *, probe_daemon=False):
            return {"cookie_count": 0, "needs_rebind": True}

        async def bind_cookie_local(self, cookie_text, *, uin=0, source="manual"):
            captured["bound_cookie_text"] = cookie_text
            captured["bound_uin"] = uin
            captured["bound_source"] = source
            return {"cookie_count": 4, "needs_rebind": False, "login_uin": uin}

    class _RealApi:
        async def call_action(self, action, **params):
            return {"status": "ok", "action": action, "params": params}

    real_api = _RealApi()

    class _Event:
        bot = types.SimpleNamespace(api=real_api)

        async def send_private_msg(self, **_params):
            return {"status": "ok"}

    async def fake_fetch_cookie_text(bot, *, domain):
        captured["cookie_client"] = bot
        captured["cookie_domain"] = domain
        return "uin=o12345; p_uin=o12345; p_skey=secret; skey=secret"

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(auto_bind_cookie=True, cookie_domain="user.qzone.qq.com")
    plugin.controller = _Controller()
    plugin._onebot_client = None
    plugin._context = None
    plugin._cookie_lock = None
    monkeypatch.setattr(main, "fetch_cookie_text", fake_fetch_cookie_text)

    result = asyncio.run(plugin._auto_bind_cookie(_Event(), source="test"))

    assert captured["cookie_client"] is real_api
    assert captured["bound_uin"] == 12345
    assert captured["bound_source"] == "test"
    assert result["login_uin"] == 12345


def test_capture_onebot_client_from_context_supports_astrbot_platform_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _RealClient:
        async def call_action(self, action, **params):
            return {"status": "ok"}

    real_client = _RealClient()

    class _Meta:
        name = "aiocqhttp"
        type = "aiocqhttp"

    class _Platform:
        def meta(self):
            return _Meta()

        def get_client(self):
            return real_client

    class _Context:
        platform_manager = types.SimpleNamespace(platform_insts=[_Platform()])

        def get_platform(self, _platform_type):
            return None

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin._context = _Context()
    plugin._onebot_client = None

    assert plugin._capture_onebot_client_from_context() is real_client
    assert plugin._onebot_client is real_client


def test_capture_onebot_client_from_context_supports_nested_context_bot_api(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _RealApi:
        async def call_action(self, action, **params):
            return {"status": "ok"}

    class _Context:
        bot = types.SimpleNamespace(api=_RealApi())

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.context = _Context()
    plugin._context = None
    plugin._onebot_client = None

    assert plugin._capture_onebot_client_from_context() is _Context.bot.api


def test_aiocqhttp_capture_schedules_bootstrap_auto_bind_without_read_prob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Bot:
        async def call_action(self, action, **params):
            return {}

    class _Event:
        bot = _Bot()

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(auto_bind_cookie=True, read_prob=0.0)
    plugin._onebot_client = None
    plugin._auto_bind_bootstrap_task = None
    plugin._auto_bind_bootstrap_succeeded = False

    async def fake_bootstrap(trigger, event=None):
        captured["trigger"] = trigger
        captured["event"] = event
        return True

    plugin._bootstrap_auto_bind = fake_bootstrap

    async def run_capture():
        event = _Event()
        await plugin.qzone_capture_aiocqhttp_client(event)
        task = plugin._auto_bind_bootstrap_task
        assert task is not None
        await task
        return event

    event = asyncio.run(run_capture())

    assert captured == {"trigger": "aiocqhttp capture", "event": event}
    assert plugin._auto_bind_bootstrap_succeeded is True


def test_initialize_schedules_auto_bind_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    bot = object()
    started = False

    async def run_initialize():
        nonlocal started
        blocker = asyncio.Event()
        plugin = object.__new__(main.QzoneStablePlugin)
        plugin.settings = types.SimpleNamespace(auto_bind_cookie=True, cookies_str="")
        plugin._onebot_client = bot
        plugin._auto_bind_bootstrap_task = None
        plugin._auto_bind_bootstrap_succeeded = False
        plugin._start_scheduled_tasks = lambda: None
        plugin._capture_onebot_client_from_context = lambda: bot

        async def fake_bootstrap(trigger, event=None):
            nonlocal started
            started = True
            assert trigger == "initialize"
            await blocker.wait()
            return True

        plugin._bootstrap_auto_bind = fake_bootstrap

        await plugin.initialize()
        task = plugin._auto_bind_bootstrap_task
        assert task is not None
        await asyncio.sleep(0)
        assert started is True
        assert not task.done()
        blocker.set()
        await task
        assert plugin._auto_bind_bootstrap_succeeded is True

    asyncio.run(run_initialize())


def test_astrbot_loaded_schedules_auto_bind_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    bot = object()
    started = False

    async def run_loaded():
        nonlocal started
        blocker = asyncio.Event()
        plugin = object.__new__(main.QzoneStablePlugin)
        plugin.settings = types.SimpleNamespace(auto_bind_cookie=True)
        plugin._onebot_client = bot
        plugin._auto_bind_bootstrap_task = None
        plugin._auto_bind_bootstrap_succeeded = False
        plugin._start_scheduled_tasks = lambda: None
        plugin._capture_onebot_client_from_context = lambda: bot

        async def fake_bootstrap(trigger, event=None):
            nonlocal started
            started = True
            assert trigger == "astrbot load"
            await blocker.wait()
            return True

        plugin._bootstrap_auto_bind = fake_bootstrap

        await plugin.qzone_on_astrbot_loaded()
        task = plugin._auto_bind_bootstrap_task
        assert task is not None
        await asyncio.sleep(0)
        assert started is True
        assert not task.done()
        blocker.set()
        await task
        assert plugin._auto_bind_bootstrap_succeeded is True

    asyncio.run(run_loaded())


def test_auto_bind_bootstrap_failure_can_be_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    bot = object()
    attempts: list[str] = []

    async def run_retries():
        plugin = object.__new__(main.QzoneStablePlugin)
        plugin.settings = types.SimpleNamespace(auto_bind_cookie=True)
        plugin._onebot_client = bot
        plugin._auto_bind_bootstrap_task = None
        plugin._auto_bind_bootstrap_succeeded = False
        plugin._capture_onebot_client_from_context = lambda: bot

        async def fake_bootstrap(trigger, event=None):
            attempts.append(trigger)
            return len(attempts) > 1

        plugin._bootstrap_auto_bind = fake_bootstrap

        plugin._schedule_bootstrap_auto_bind("first")
        first_task = plugin._auto_bind_bootstrap_task
        assert first_task is not None
        await first_task
        assert plugin._auto_bind_bootstrap_succeeded is False

        plugin._schedule_bootstrap_auto_bind("second")
        second_task = plugin._auto_bind_bootstrap_task
        assert second_task is not None
        assert second_task is not first_task
        await second_task
        assert plugin._auto_bind_bootstrap_succeeded is True

    asyncio.run(run_retries())
    assert attempts == ["first", "second"]


def test_aiocqhttp_capture_schedules_auto_bind_when_auto_read_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Bot:
        async def call_action(self, action, **params):
            return {}

    class _Event:
        bot = _Bot()

        def get_group_id(self):
            return 42

        def get_sender_id(self):
            return 7

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        auto_bind_cookie=True,
        read_prob=1.0,
        ignore_groups=["42"],
        ignore_users=[],
    )
    plugin._onebot_client = None
    plugin._auto_bind_bootstrap_task = None
    plugin._auto_bind_bootstrap_succeeded = False

    async def fake_bootstrap(trigger, event=None):
        captured["trigger"] = trigger
        captured["event"] = event
        return True

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("ignored auto-read events should not synchronously bind")

    plugin._bootstrap_auto_bind = fake_bootstrap
    plugin._ensure_cookie_ready = fail_if_called

    async def run_capture():
        event = _Event()
        await plugin.qzone_capture_aiocqhttp_client(event)
        task = plugin._auto_bind_bootstrap_task
        assert task is not None
        await task
        return event

    event = asyncio.run(run_capture())

    assert captured == {"trigger": "aiocqhttp capture", "event": event}
    assert plugin._auto_bind_bootstrap_succeeded is True


def test_aiocqhttp_capture_auto_comment_notifies_current_event_with_comment_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Bot:
        pass

    class _Event:
        bot = _Bot()

        def get_group_id(self):
            return 4242

        def get_sender_id(self):
            return 5151

    class _PostService:
        async def comment_post(self, post, text):
            captured["comment"] = (post.fid, text)

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_posts_for_event(event, prefixes, *, target_id=0, no_commented=False, no_self=False):
        captured["post_lookup"] = {
            "event": event,
            "target_id": target_id,
            "no_commented": no_commented,
            "no_self": no_self,
        }
        return [main.QzonePost(hostuin=12345, fid="fid-1", summary="post", nickname="Alice")]

    async def fake_generate(event, post):
        captured["generate"] = (event, post.fid)
        return "nice comment"

    async def fake_notify(event, post, message, *, comment_text=""):
        captured["notify"] = {
            "event": event,
            "fid": post.fid,
            "message": message,
            "comment_text": comment_text,
        }

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        read_prob=1.0,
        auto_bind_cookie=False,
        ignore_groups=[],
        ignore_users=[],
        like_when_comment=False,
    )
    plugin._onebot_client = None
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._posts_for_event = fake_posts_for_event
    plugin._generate_comment_text = fake_generate
    plugin._post_service = lambda: _PostService()
    plugin._notify_event_post_card = fake_notify

    event = _Event()
    asyncio.run(plugin.qzone_capture_aiocqhttp_client(event))

    assert captured["post_lookup"] == {
        "event": event,
        "target_id": 5151,
        "no_commented": True,
        "no_self": True,
    }
    assert captured["comment"] == ("fid-1", "nice comment")
    assert captured["generate"] == (event, "fid-1")
    assert captured["notify"]["event"] is event
    assert captured["notify"]["fid"] == "fid-1"
    assert captured["notify"]["comment_text"] == "nice comment"
    assert "nice comment" in captured["notify"]["message"]


def test_terminate_cancels_auto_bind_bootstrap_task(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    closed = False

    class _Controller:
        async def close(self):
            nonlocal closed
            closed = True

    async def run_terminate():
        blocker = asyncio.Event()
        plugin = object.__new__(main.QzoneStablePlugin)
        plugin._scheduled_tasks = []
        plugin._publisher_profile_preload_task = None
        plugin._daemon_warmup_task = None
        plugin._auto_bind_bootstrap_task = asyncio.create_task(blocker.wait())
        plugin.controller = _Controller()

        await plugin.terminate()

        assert plugin._auto_bind_bootstrap_task is None

    asyncio.run(run_terminate())
    assert closed is True


def test_auto_bind_cookie_reuses_ready_status_without_fetching(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Controller:
        async def get_status(self, *, probe_daemon=False):
            return {"cookie_count": 4, "needs_rebind": False, "login_uin": 12345}

        async def bind_cookie_local(self, *args, **kwargs):
            raise AssertionError("ready cookie state should not be rebound")

    async def fail_fetch(*args, **kwargs):
        raise AssertionError("ready cookie state should not fetch OneBot cookies")

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(auto_bind_cookie=True, cookie_domain="user.qzone.qq.com")
    plugin.controller = _Controller()
    plugin._onebot_client = object()
    plugin._cookie_lock = None
    monkeypatch.setattr(main, "fetch_cookie_text", fail_fetch)

    result = asyncio.run(plugin._auto_bind_cookie())

    assert result == {"cookie_count": 4, "needs_rebind": False, "login_uin": 12345}


def test_remote_media_download_headers_do_not_send_qzone_cookie() -> None:
    client = QzoneClient(SessionState(uin=12345, cookies={"uin": "o12345", "p_skey": "secret"}))
    try:
        headers = client._media_download_headers()
        assert "Cookie" not in headers
        assert "Referer" not in headers
        assert "Origin" not in headers
    finally:
        asyncio.run(client.close())


def test_publish_renderer_uses_public_qzone_image_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    import qzone_bridge.publish_renderer as renderer

    captured: dict[str, object] = {}

    class _Response:
        status_code = 200
        headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_bytes(self):
            yield b"image-bytes"

    class _Client:
        def stream(self, method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = kwargs.get("headers") or {}
            return _Response()

    monkeypatch.setattr(renderer, "is_remote_media_url_allowed", lambda source: True)
    monkeypatch.setattr(renderer, "_thread_http_client", lambda: _Client())

    data = renderer._read_source_bytes(
        "https://m.qpic.cn/feed-image.jpg",
        max_bytes=1024,
        remote_timeout=0.1,
    )

    headers = captured["headers"]
    assert data == b"image-bytes"
    assert headers["Referer"] == "https://user.qzone.qq.com/"
    assert headers["User-Agent"]
    assert "Cookie" not in headers


def test_remote_media_response_cookies_do_not_pollute_qzone_session(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        status_code = 200
        headers = {"content-type": "image/png"}
        cookies = {"evil": "cookie"}

        async def aiter_bytes(self):
            yield b"abc"

    class _Stream:
        async def __aenter__(self):
            return _Response()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    client = QzoneClient(SessionState(uin=12345, cookies={"uin": "o12345", "p_skey": "secret"}))
    monkeypatch.setattr("qzone_bridge.client.is_remote_media_url_allowed", lambda source: True)
    monkeypatch.setattr(client._client, "stream", lambda *args, **kwargs: _Stream())
    try:
        data, _, _ = asyncio.run(client._load_image_source({"kind": "image", "source": "https://example.test/a.png"}))
        assert data == b"abc"
        assert client.session.cookies["uin"] == "o12345"
        assert client.session.cookies["p_skey"] == "secret"
        assert "evil" not in client.session.cookies
    finally:
        asyncio.run(client.close())


def test_remote_media_policy_blocks_localhost_and_private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not source_policy.is_remote_media_url_allowed("http://127.0.0.1/a.png")
    assert not source_policy.is_remote_media_url_allowed("http://localhost/a.png")

    source_policy.remote_media_host_resolves_safely.cache_clear()
    monkeypatch.setattr(
        source_policy.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(source_policy.socket.AF_INET, 0, 0, "", ("10.0.0.5", 0))],
    )
    assert not source_policy.is_remote_media_url_allowed("https://media.example.test/a.png")


def test_remote_media_policy_allows_trusted_qq_media_domain_with_fake_ip_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_policy.remote_media_host_resolves_safely.cache_clear()
    monkeypatch.setattr(
        source_policy.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(source_policy.socket.AF_INET, 0, 0, "", ("198.18.0.53", 0))],
    )

    assert source_policy.is_remote_media_url_allowed(
        "https://multimedia.nt.qq.com.cn/download?appid=1413&format=origin&rkey=test"
    )


def test_remote_media_policy_allows_exact_qq_multimedia_video_url_from_onebot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_policy.remote_media_host_resolves_safely.cache_clear()
    monkeypatch.setattr(
        source_policy.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(source_policy.socket.AF_INET, 0, 0, "", ("198.18.0.56", 0))],
    )

    assert source_policy.is_remote_media_url_allowed(
        "https://multimedia.nt.qq.com.cn/download?"
        "appid=1413&format=origin&orgfmt=t264&spec=0&"
        "rkey=test-onebot-qq-multimedia-video-rkey"
    )


def test_remote_media_policy_allows_qq_multimedia_download_even_with_private_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_policy.remote_media_host_resolves_safely.cache_clear()
    monkeypatch.setattr(
        source_policy.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(source_policy.socket.AF_INET, 0, 0, "", ("10.0.0.5", 0))],
    )

    assert source_policy.is_remote_media_url_allowed(
        "https://multimedia.nt.qq.com.cn/download?"
        "appid=1413&format=origin&orgfmt=t264&spec=0&"
        "rkey=test-onebot-qq-multimedia-video-rkey"
    )


def test_remote_media_policy_allows_trusted_qq_media_domain_with_mixed_public_and_fake_ip_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_policy.remote_media_host_resolves_safely.cache_clear()
    monkeypatch.setattr(
        source_policy.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (source_policy.socket.AF_INET, 0, 0, "", ("198.18.0.56", 0)),
            (source_policy.socket.AF_INET, 0, 0, "", ("1.1.1.1", 0)),
        ],
    )

    assert source_policy.is_remote_media_url_allowed(
        "https://multimedia.nt.qq.com.cn/download?"
        "appid=1413&format=origin&orgfmt=t264&spec=0&"
        "rkey=test-onebot-qq-multimedia-video-rkey"
    )


def test_remote_media_policy_blocks_untrusted_domain_with_fake_ip_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_policy.remote_media_host_resolves_safely.cache_clear()
    monkeypatch.setattr(
        source_policy.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(source_policy.socket.AF_INET, 0, 0, "", ("198.18.0.53", 0))],
    )

    assert not source_policy.is_remote_media_url_allowed("https://media.example.test/a.mp4")


def test_remote_media_policy_blocks_trusted_domain_with_private_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_policy.remote_media_host_resolves_safely.cache_clear()
    monkeypatch.setattr(
        source_policy.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(source_policy.socket.AF_INET, 0, 0, "", ("10.0.0.5", 0))],
    )

    assert not source_policy.is_remote_media_url_allowed("https://qpic.cn/a.png")


def test_base64_upload_sources_do_not_apply_plugin_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import qzone_bridge.client as client_module

    monkeypatch.setattr(client_module, "MAX_UPLOAD_IMAGE_BYTES", 8, raising=False)

    assert QzoneClient._decode_upload_image_base64("A" * 20, label="图片") == b"\x00" * 15


def test_upload_photo_rejects_forged_image_kind_before_network() -> None:
    async def scenario() -> None:
        client = QzoneClient(SessionState(uin=12345, cookies={"uin": "o12345", "p_skey": "secret"}))
        try:
            async def fail_request(*args, **kwargs):
                raise AssertionError("invalid image bytes should not reach QQ upload")

            client._request_json = fail_request  # type: ignore[method-assign]
            encoded = base64.b64encode(b"not really an image").decode("ascii")
            with pytest.raises(QzoneParseError, match="图片内容"):
                await client.upload_photo(
                    {
                        "kind": "image",
                        "source": f"base64://{encoded}",
                        "name": "fake.png",
                        "mime_type": "image/png",
                    }
                )
        finally:
            await client.close()

    asyncio.run(scenario())


def test_daemon_secret_is_not_passed_in_argv(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Process:
        pid = 1234

        def poll(self):
            return None

    def fake_popen(cmd, cwd=None, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        return _Process()

    monkeypatch.setattr("qzone_bridge.controller.subprocess.Popen", fake_popen)
    controller = QzoneDaemonController(plugin_root=tmp_path, data_dir=tmp_path / "data")
    controller._spawn_daemon(18999)

    cmd = [str(item) for item in captured["cmd"]]
    env = captured["env"]
    assert "--secret" not in cmd
    assert isinstance(env, dict)
    assert env.get("QZONE_BRIDGE_SECRET")


def test_daemon_spawn_passes_current_version(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import qzone_bridge

    captured: dict[str, object] = {}

    class _Process:
        pid = 1234

        def poll(self):
            return None

    def fake_popen(cmd, cwd=None, **kwargs):
        captured["cmd"] = cmd
        return _Process()

    monkeypatch.setattr("qzone_bridge.controller.subprocess.Popen", fake_popen)
    controller = QzoneDaemonController(plugin_root=tmp_path, data_dir=tmp_path / "data")
    controller._spawn_daemon(18999)

    cmd = [str(item) for item in captured["cmd"]]
    assert cmd[cmd.index("--version") + 1] == qzone_bridge.__version__


def test_controller_status_merges_h5_video_health_from_daemon(tmp_path) -> None:
    import qzone_bridge

    controller = QzoneDaemonController(plugin_root=tmp_path, data_dir=tmp_path / "data")
    runtime = controller._runtime()

    def seed_state(state):
        state.runtime.daemon_port = runtime.daemon_port
        state.runtime.secret = runtime.secret
        state.runtime.version = qzone_bridge.__version__
        state.session = SessionState(
            uin=12345,
            cookies={"uin": "o12345", "p_skey": "ps-key", "skey": "s-key"},
            source="onebot",
        )

    controller.store.update(seed_state)

    class _Response:
        status_code = 200

        def json(self):
            return {
                "ok": True,
                "data": {
                    "daemon_state": "ready",
                    "daemon_port": runtime.daemon_port,
                    "daemon_version": qzone_bridge.__version__,
                    "bridge_api_version": qzone_bridge.BRIDGE_API_VERSION,
                    "login_uin": 12345,
                    "cookie_count": 4,
                    "needs_rebind": False,
                    "video_upload": {
                        "configured": False,
                        "method": "",
                        "web_cookie_configured": True,
                        "h5_upload_available": True,
                        "h5_upload_diagnostic_available": True,
                        "h5_publish_supported": False,
                    },
                },
            }

    class _Client:
        async def get(self, url, headers=None):
            return _Response()

    original_client = controller._client
    asyncio.run(original_client.aclose())
    controller._client = _Client()  # type: ignore[assignment]

    status = asyncio.run(controller.get_status())

    assert status["daemon_state"] == "ready"
    assert status["video_upload"]["method"] == ""
    assert status["video_upload"]["h5_upload_diagnostic_available"] is True
    assert status["video_upload"]["h5_publish_supported"] is False
    assert status["video_upload"]["web_cookie_configured"] is True


def test_ensure_running_restarts_incompatible_daemon(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import qzone_bridge

    (tmp_path / "daemon_main.py").write_text("# daemon entry", encoding="utf-8")
    controller = QzoneDaemonController(plugin_root=tmp_path, data_dir=tmp_path / "data", start_timeout=0.2)

    def mark_runtime_stale(state):
        state.runtime.version = "0.3.2"

    controller.store.update(mark_runtime_stale)
    runtime = controller._runtime()
    calls: dict[str, object] = {"health": 0, "shutdown": [], "spawn": []}

    class _Response:
        status_code = 200

        def __init__(self, data: dict[str, object]):
            self._data = data

        def json(self):
            return {"ok": True, "data": self._data}

    class _Client:
        async def get(self, url, headers=None):
            calls["health"] = int(calls["health"]) + 1
            if calls["health"] == 1:
                return _Response(
                    {
                        "daemon_state": "ready",
                        "daemon_port": 18999,
                        "daemon_version": "0.3.2",
                    }
                )
            return _Response(
                {
                    "daemon_state": "ready",
                    "daemon_port": 18999,
                    "daemon_version": qzone_bridge.__version__,
                    "bridge_api_version": qzone_bridge.BRIDGE_API_VERSION,
                }
            )

    class _Process:
        pid = 4321

        def poll(self):
            return None

    async def fake_shutdown(port: int, secret: str) -> bool:
        calls["shutdown"].append((port, secret))
        return True

    async def fake_wait(port: int, timeout: float = 3.0) -> bool:
        return True

    def fake_spawn(port: int):
        calls["spawn"].append(port)
        return _Process()

    controller._client = _Client()
    monkeypatch.setattr(controller, "_request_daemon_shutdown", fake_shutdown)
    monkeypatch.setattr(controller, "_wait_for_port_release", fake_wait)
    monkeypatch.setattr(controller, "_spawn_daemon", fake_spawn)
    monkeypatch.setattr("qzone_bridge.controller._port_is_free_async", lambda port: asyncio.sleep(0, result=True))

    status = asyncio.run(controller.ensure_running())

    assert calls["shutdown"] == [(runtime.daemon_port, runtime.secret)]
    assert calls["spawn"] == [runtime.daemon_port]
    assert status["daemon_state"] == "ready"
    assert status["daemon_version"] == qzone_bridge.__version__


def test_ensure_running_does_not_shutdown_foreign_health_service(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import qzone_bridge

    (tmp_path / "daemon_main.py").write_text("# daemon entry", encoding="utf-8")
    controller = QzoneDaemonController(plugin_root=tmp_path, data_dir=tmp_path / "data", start_timeout=0.2)
    runtime = controller._runtime()
    controller._incompatible_daemon = (runtime.daemon_port, runtime.secret)
    calls: dict[str, object] = {"health": 0, "shutdown": [], "spawn": []}

    class _Response:
        status_code = 200

        def __init__(self, data: dict[str, object]):
            self._data = data

        def json(self):
            return {"ok": True, "data": self._data}

    class _Client:
        async def get(self, url, headers=None):
            calls["health"] = int(calls["health"]) + 1
            if calls["health"] == 1:
                return _Response({"service": "other-local-service"})
            return _Response(
                {
                    "daemon_state": "ready",
                    "daemon_port": 19000,
                    "daemon_version": qzone_bridge.__version__,
                    "bridge_api_version": qzone_bridge.BRIDGE_API_VERSION,
                }
            )

    class _Process:
        pid = 4322

        def poll(self):
            return None

    async def fake_shutdown(port: int, secret: str) -> bool:
        calls["shutdown"].append((port, secret))
        return True

    async def fake_port_free(port: int) -> bool:
        return port != runtime.daemon_port

    def fake_spawn(port: int):
        calls["spawn"].append(port)
        return _Process()

    controller._client = _Client()
    monkeypatch.setattr(controller, "_request_daemon_shutdown", fake_shutdown)
    monkeypatch.setattr(controller, "_spawn_daemon", fake_spawn)
    monkeypatch.setattr("qzone_bridge.controller._port_is_free_async", fake_port_free)

    status = asyncio.run(controller.ensure_running())

    assert calls["shutdown"] == []
    assert calls["spawn"] == [runtime.daemon_port + 1]
    assert status["daemon_state"] == "ready"
    assert status["daemon_port"] == runtime.daemon_port + 1


@pytest.mark.parametrize("foreign_mode", ["not_found", "not_json", "not_ok"])
def test_stale_incompatible_marker_is_cleared_for_failed_foreign_health(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    foreign_mode: str,
) -> None:
    import qzone_bridge

    (tmp_path / "daemon_main.py").write_text("# daemon entry", encoding="utf-8")
    controller = QzoneDaemonController(plugin_root=tmp_path, data_dir=tmp_path / "data", start_timeout=0.2)
    runtime = controller._runtime()
    controller._incompatible_daemon = (runtime.daemon_port, runtime.secret)
    calls: dict[str, object] = {"health": 0, "shutdown": [], "spawn": []}

    class _Response:
        def __init__(self, status_code: int, payload: dict[str, object] | None = None, *, broken_json: bool = False):
            self.status_code = status_code
            self._payload = payload or {}
            self._broken_json = broken_json

        def json(self):
            if self._broken_json:
                raise ValueError("not json")
            return self._payload

    class _Client:
        async def get(self, url, headers=None):
            calls["health"] = int(calls["health"]) + 1
            if calls["health"] == 1:
                if foreign_mode == "not_found":
                    return _Response(404)
                if foreign_mode == "not_json":
                    return _Response(200, broken_json=True)
                return _Response(200, {"ok": False, "error": {"code": "FOREIGN"}})
            return _Response(
                200,
                {
                    "ok": True,
                    "data": {
                        "daemon_state": "ready",
                        "daemon_port": 19000,
                        "daemon_version": qzone_bridge.__version__,
                        "bridge_api_version": qzone_bridge.BRIDGE_API_VERSION,
                    },
                },
            )

    class _Process:
        pid = 4324

        def poll(self):
            return None

    async def fake_shutdown(port: int, secret: str) -> bool:
        calls["shutdown"].append((port, secret))
        return True

    async def fake_port_free(port: int) -> bool:
        return port != runtime.daemon_port

    def fake_spawn(port: int):
        calls["spawn"].append(port)
        return _Process()

    controller._client = _Client()
    monkeypatch.setattr(controller, "_request_daemon_shutdown", fake_shutdown)
    monkeypatch.setattr(controller, "_spawn_daemon", fake_spawn)
    monkeypatch.setattr("qzone_bridge.controller._port_is_free_async", fake_port_free)

    status = asyncio.run(controller.ensure_running())

    assert calls["shutdown"] == []
    assert calls["spawn"] == [runtime.daemon_port + 1]
    assert status["daemon_state"] == "ready"


def test_detail_card_after_stale_daemon_restart_has_images_and_real_time(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import qzone_bridge
    from qzone_bridge.models import FeedEntry
    from qzone_bridge.social import post_from_entry

    main = _import_main_with_stubs(monkeypatch)
    (tmp_path / "daemon_main.py").write_text("# daemon entry", encoding="utf-8")
    controller = QzoneDaemonController(plugin_root=tmp_path, data_dir=tmp_path / "data", start_timeout=0.2)
    calls: dict[str, object] = {"health": 0, "shutdown": [], "spawn": [], "request": []}
    created_at = 1_690_000_000

    class _Response:
        status_code = 200

        def __init__(self, payload: dict[str, object]):
            self._payload = payload
            self.text = str(payload)

        def json(self):
            return self._payload

    class _Client:
        async def get(self, url, headers=None):
            calls["health"] = int(calls["health"]) + 1
            if calls["health"] == 1:
                return _Response(
                    {
                        "ok": True,
                        "data": {
                            "daemon_state": "ready",
                            "daemon_port": 18999,
                            "daemon_version": "0.3.2",
                        },
                    }
                )
            return _Response(
                {
                    "ok": True,
                    "data": {
                        "daemon_state": "ready",
                        "daemon_port": 18999,
                        "daemon_version": qzone_bridge.__version__,
                        "bridge_api_version": qzone_bridge.BRIDGE_API_VERSION,
                    },
                }
            )

        async def request(self, method, url, headers=None, params=None, json=None):
            calls["request"].append((method, url))
            raw = {
                "picdata": {"0": {"url1": "//m.qpic.cn/restarted-card.jpg"}},
                "htmlContent": f"<div data-abstime={created_at}>图文说说</div>",
            }
            return _Response(
                {
                    "ok": True,
                    "data": {
                        "entry": {
                            "hostuin": 12345,
                            "fid": "fid-restarted",
                            "appid": 311,
                            "summary": "图文说说",
                            "nickname": "列表昵称",
                            "created_at": created_at,
                            "raw": raw,
                        },
                        "raw": raw,
                        "comments": [],
                    },
                }
            )

    class _Process:
        pid = 4323

        def poll(self):
            return None

    async def fake_shutdown(port: int, secret: str) -> bool:
        calls["shutdown"].append((port, secret))
        return True

    async def fake_wait(port: int, timeout: float = 3.0) -> bool:
        return True

    def fake_spawn(port: int):
        calls["spawn"].append(port)
        return _Process()

    controller._client = _Client()
    monkeypatch.setattr(controller, "_request_daemon_shutdown", fake_shutdown)
    monkeypatch.setattr(controller, "_wait_for_port_release", fake_wait)
    monkeypatch.setattr(controller, "_spawn_daemon", fake_spawn)
    monkeypatch.setattr("qzone_bridge.controller._port_is_free_async", lambda port: asyncio.sleep(0, result=True))

    payload = asyncio.run(controller.detail_feed(hostuin=12345, fid="fid-restarted", appid=311))
    entry = FeedEntry(**payload["entry"])
    post = post_from_entry(entry, detail=payload.get("raw"), local_id=1)
    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.data_dir = tmp_path
    profile = plugin._post_render_profile(post)

    assert calls["shutdown"]
    assert calls["spawn"] == [18999]
    assert calls["request"]
    assert post.images == ["https://m.qpic.cn/restarted-card.jpg"]
    assert post.created_at == created_at
    assert profile.time_text != "未知时间"



def test_controller_video_publish_uses_long_daemon_request_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qzone_bridge.controller import DAEMON_VIDEO_PUBLISH_TIMEOUT_SECONDS

    controller = QzoneDaemonController(
        plugin_root=tmp_path,
        data_dir=tmp_path / "data",
        request_timeout=15.0,
        auto_start_daemon=False,
    )
    captured: dict[str, object] = {}

    class _Response:
        text = "{}"

        def json(self):
            return {"ok": True, "data": {"fid": "fid-video"}}

    class _Client:
        async def request(self, method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return _Response()

    async def fake_probe_health(_port: int) -> bool:
        return True

    controller._client = _Client()
    monkeypatch.setattr(controller, "_probe_health", fake_probe_health)

    payload = asyncio.run(
        controller.publish_post(
            content="",
            media=[{"kind": "video", "source": str(tmp_path / "clip.mp4"), "name": "clip.mp4"}],
            content_sanitized=True,
        )
    )

    assert payload == {"fid": "fid-video"}
    assert captured["method"] == "POST"
    assert str(captured["url"]).endswith("/post")
    assert captured["timeout"] == DAEMON_VIDEO_PUBLISH_TIMEOUT_SECONDS


def test_controller_video_publish_timeout_is_not_retried_to_avoid_duplicate_posts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qzone_bridge.controller import DAEMON_VIDEO_PUBLISH_TIMEOUT_SECONDS

    controller = QzoneDaemonController(
        plugin_root=tmp_path,
        data_dir=tmp_path / "data",
        request_timeout=15.0,
        auto_start_daemon=False,
    )
    calls: list[dict[str, object]] = []

    class _Client:
        async def request(self, method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            raise httpx.ReadTimeout("daemon still uploading video")

    async def fake_probe_health(_port: int) -> bool:
        return True

    controller._client = _Client()
    monkeypatch.setattr(controller, "_probe_health", fake_probe_health)

    with pytest.raises(DaemonUnavailableError) as error:
        asyncio.run(
            controller.publish_post(
                content="",
                media=[{"kind": "video", "source": str(tmp_path / "clip.mp4"), "name": "clip.mp4"}],
                content_sanitized=True,
            )
        )

    assert len(calls) == 1
    assert calls[0]["timeout"] == DAEMON_VIDEO_PUBLISH_TIMEOUT_SECONDS
    assert error.value.detail["error_type"] == "ReadTimeout"
    assert error.value.detail["path"] == "/post"
    assert error.value.detail["timeout"] == DAEMON_VIDEO_PUBLISH_TIMEOUT_SECONDS


def test_controller_daemon_non_json_post_response_is_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = QzoneDaemonController(
        plugin_root=tmp_path,
        data_dir=tmp_path / "data",
        request_timeout=15.0,
        auto_start_daemon=False,
    )

    class _Response:
        status_code = 502
        text = "<html>bad gateway</html>"
        headers = {"content-type": "text/html"}

        def json(self):
            raise ValueError("not json")

    class _Client:
        async def request(self, method, url, **kwargs):
            return _Response()

    async def fake_probe_health(_port: int) -> bool:
        return True

    controller._client = _Client()
    monkeypatch.setattr(controller, "_probe_health", fake_probe_health)

    with pytest.raises(DaemonUnavailableError) as error:
        asyncio.run(
            controller.publish_post(
                content="",
                media=[{"kind": "video", "source": str(tmp_path / "clip.mp4"), "name": "clip.mp4"}],
                content_sanitized=True,
            )
        )

    assert error.value.detail["status_code"] == 502
    assert error.value.detail["content_type"] == "text/html"
    assert error.value.detail["body_preview"] == "<html>bad gateway</html>"


def test_public_error_text_redacts_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    exc = QzoneRequestError(
        "QQ 空间拒绝访问",
        status_code=403,
        detail={
            "status_code": 403,
            "url": "https://multimedia.nt.qq.com.cn/download?p_skey=SECRET&rkey=SECRET&ok=1",
            "location": "https://example.test/login?token=SECRET",
            "text": "cookie=SECRET",
            "log_tail": "SECRET",
        },
    )
    text = main.QzoneStablePlugin._error_text(object(), exc)
    assert "SECRET" not in text
    assert "HTTP 403" in text
    assert "响应详情已隐藏" in text


def test_cookie_backed_read_and_write_entrypoints_require_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    methods = [
        "view_feed",
        "read_feed",
        "comment_feed",
        "like_feed",
        "reply_comment",
        "llm_view_feed",
        "qzone_feed",
        "qzone_detail",
        "tool_list_feed",
        "tool_detail_feed",
        "tool_view_post",
    ]
    for name in methods:
        source = inspect.getsource(getattr(main.QzoneStablePlugin, name))
        assert "if not self._is_admin(event)" in source, name


def test_qzone_post_card_result_uses_publish_renderer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    def fake_render(post, output_dir, *, profile=None, result=None, width=900, remote_timeout=1.5):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "card.png"
        path.write_bytes(b"png")
        captured["post"] = post
        captured["profile"] = profile
        captured["result"] = result
        captured["width"] = width
        captured["remote_timeout"] = remote_timeout
        return path

    class _Event:
        stopped = False

        def stop_event(self):
            self.stopped = True

        def image_result(self, path: str):
            return {"type": "image", "path": path}

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        render_publish_result=True,
        render_result_width=720,
        render_remote_timeout=0.01,
        render_feed_card_limit=5,
        max_feed_limit=20,
    )
    plugin.data_dir = tmp_path
    monkeypatch.setattr(main, "render_publish_result_image", fake_render)

    post = main.QzonePost(
        hostuin=12345,
        fid="fid-1",
        summary="今天的风很轻。",
        nickname="小明",
        created_at=1_700_000_000,
        images=["https://example.test/a.png"],
        local_id=2,
    )
    event = _Event()
    results = asyncio.run(plugin._post_card_results(event, [post], "fallback"))

    assert event.stopped
    assert results == [{"type": "image", "path": str(tmp_path / "rendered_posts" / "card.png")}]
    rendered_post = captured["post"]
    profile = captured["profile"]
    assert rendered_post.content == "今天的风很轻。"
    assert rendered_post.media[0].source == "https://example.test/a.png"
    assert profile.nickname == "小明"
    assert profile.user_id == "12345"
    assert profile.time_text == datetime.fromtimestamp(1_700_000_000).strftime("%m-%d %H:%M")
    assert captured["width"] == 720
    assert captured["remote_timeout"] == 0.01


def test_qzone_post_card_profile_uses_nickname_not_numeric_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    main = _import_main_with_stubs(monkeypatch)
    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.data_dir = tmp_path

    raw_named_post = main.QzonePost(
        hostuin=12345,
        fid="fid-raw",
        summary="",
        nickname="",
        raw={"userinfo": {"uin": 12345, "nickname": "风铃"}},
        local_id=7,
    )
    numeric_post = main.QzonePost(
        hostuin=12345,
        fid="fid-number",
        summary="",
        nickname="12345",
        raw={},
        local_id=3,
    )

    raw_profile = plugin._post_render_profile(raw_named_post)
    numeric_profile = plugin._post_render_profile(numeric_post)

    assert raw_profile.nickname == "风铃"
    assert raw_profile.nickname != "7. 风铃"
    assert numeric_profile.nickname == "QQ 空间用户"


def test_qzone_post_card_range_renders_single_combined_image(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    main = _import_main_with_stubs(monkeypatch)
    rendered_names: set[str] = set()
    fixed_width_flags: list[bool] = []
    sizes = {
        "第一条": (80, 20, (255, 0, 0)),
        "第二条": (60, 20, (0, 255, 0)),
        "第三条": (70, 20, (0, 0, 255)),
    }

    def fake_render(post, output_dir, *, profile=None, result=None, width=900, remote_timeout=1.5, fixed_width=False):
        from PIL import Image

        output_dir.mkdir(parents=True, exist_ok=True)
        rendered_names.add(profile.nickname)
        fixed_width_flags.append(fixed_width)
        path = output_dir / f"{post.content}.png"
        image_width, image_height, color = sizes[post.content]
        Image.new("RGB", (image_width, image_height), color).save(path)
        return path

    class _Event:
        stopped = False

        def stop_event(self):
            self.stopped = True

        def image_result(self, path: str):
            return {"type": "image", "path": path}

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        render_publish_result=True,
        render_result_width=720,
        render_remote_timeout=0.01,
        render_feed_card_limit=5,
        max_feed_limit=20,
    )
    plugin.data_dir = tmp_path
    monkeypatch.setattr(main, "render_publish_result_image", fake_render)

    posts = [
        main.QzonePost(hostuin=10001, fid="fid-1", summary="第一条", nickname="阿一", local_id=1),
        main.QzonePost(hostuin=10002, fid="fid-2", summary="第二条", nickname="阿二", local_id=2),
        main.QzonePost(hostuin=10003, fid="fid-3", summary="第三条", nickname="阿三", local_id=3),
    ]
    event = _Event()
    results = asyncio.run(plugin._post_card_results(event, posts, "fallback"))

    from PIL import Image

    assert event.stopped
    assert len(results) == 1
    assert results[0]["type"] == "image"
    assert Path(results[0]["path"]).name.startswith("publish_result_")
    assert rendered_names == {"阿一", "阿二", "阿三"}
    assert fixed_width_flags == [True, True, True]
    with Image.open(results[0]["path"]) as combined:
        assert combined.width == 80
        assert combined.height > 60
        assert combined.getpixel((0, 0)) == (255, 0, 0)
        assert combined.getpixel((0, 32)) == (0, 255, 0)
        assert combined.getpixel((0, 64)) == (0, 0, 255)


def test_publish_renderer_fixed_width_keeps_range_cards_aligned(tmp_path: Path) -> None:
    from PIL import Image

    from qzone_bridge.media import PostPayload
    from qzone_bridge.publish_renderer import RenderProfile, render_publish_result_image

    short = render_publish_result_image(
        PostPayload(content="短内容", media=[]),
        tmp_path,
        profile=RenderProfile(nickname="阿一", time_text="12:34"),
        width=720,
        remote_timeout=0.01,
        fixed_width=True,
    )
    long = render_publish_result_image(
        PostPayload(content="这是一条更长的说说内容，用来确认范围合成长图里头像、昵称和操作按钮处在同一套宽度坐标里。", media=[]),
        tmp_path,
        profile=RenderProfile(nickname="阿二", time_text="12:34"),
        width=720,
        remote_timeout=0.01,
        fixed_width=True,
    )

    with Image.open(short) as short_image, Image.open(long) as long_image:
        assert short_image.width == long_image.width
        assert short_image.width == 720 * 3


def test_publish_renderer_short_single_image_card_uses_compact_adaptive_width(tmp_path: Path) -> None:
    from PIL import Image

    from qzone_bridge.media import PostMedia, PostPayload
    from qzone_bridge.publish_renderer import RenderProfile, render_publish_result_image

    source = tmp_path / "single.png"
    Image.new("RGB", (640, 960), (238, 238, 238)).save(source)

    rendered = render_publish_result_image(
        PostPayload(
            content="short text",
            media=[PostMedia(kind="image", source=str(source), trusted_local=True)],
        ),
        tmp_path,
        profile=RenderProfile(nickname="user", time_text="06:32"),
        width=900,
        remote_timeout=0.01,
    )

    with Image.open(rendered) as image:
        assert image.width == 560 * 3


def test_collect_post_payload_keeps_video_media_out_of_content(tmp_path: Path) -> None:
    from qzone_bridge.media import collect_post_payload

    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"fake video bytes")
    event = types.SimpleNamespace(
        message=[
            {"type": "Plain", "data": {"text": "qzone post hello"}},
            {
                "type": "Video",
                "data": {
                    "file": str(video_path),
                    "name": "clip.mp4",
                    "mime": "video/mp4",
                    "size": 123,
                },
            },
        ]
    )

    post = collect_post_payload(
        event,
        include_event_text=True,
        command_prefixes=("qzone post",),
    )

    assert post.content == "hello"
    assert post.attachments == []
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].trusted_local is True
    assert "clip.mp4" not in post.content
    assert "[视频" not in post.content


def test_collect_post_payload_strips_fullwidth_comma_command_prefix(tmp_path: Path) -> None:
    from qzone_bridge.media import collect_post_payload

    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"fake video bytes")
    event = types.SimpleNamespace(
        message_str="\uff0cqzone post hello",
        message_obj=types.SimpleNamespace(
            message_str="\uff0cqzone post hello",
            message=[
                {"type": "plain", "data": {"text": "\uff0cqzone post hello"}},
                {"type": "video", "data": {"file": str(video_path), "name": "clip.mp4", "mime_type": "video/mp4"}},
            ],
        ),
    )

    post = collect_post_payload(event, include_event_text=True, command_prefixes=("qzone post",))

    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"


def test_normalize_media_item_detects_common_video_formats(tmp_path: Path) -> None:
    from qzone_bridge.media import normalize_media_item

    for filename in ("clip.mp4", "clip.MOV", "clip.mkv", "clip.webm", "clip.avi", "clip.flv", "clip.3gp"):
        item = normalize_media_item(str(tmp_path / filename), trusted_local=True)
        assert item is not None
        assert item.kind == "video"
        assert item.mime_type.startswith("video/") or Path(filename).suffix.lower() in {".mkv", ".flv"}


def test_collect_post_payload_marks_message_video_url_trusted() -> None:
    from qzone_bridge.media import collect_post_payload

    event = types.SimpleNamespace(
        message_obj=types.SimpleNamespace(
            message=[
                {"type": "text", "data": {"text": "发说说 hello"}},
                {
                    "type": "video",
                    "data": {
                        "file": "clip.mp4",
                        "url": "https://example.test/video/clip.mp4",
                        "mime": "video/mp4",
                    },
                },
            ]
        )
    )

    post = collect_post_payload(event, command_prefixes=("发说说",))

    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == "https://example.test/video/clip.mp4"
    assert post.media[0].trusted_local is True


def test_reference_message_ids_from_reply_segment() -> None:
    from qzone_bridge.media import iter_reference_message_ids

    event = types.SimpleNamespace(
        message_obj=types.SimpleNamespace(
            message=[
                {"type": "reply", "data": {"id": "123456"}},
                {"type": "text", "data": {"text": "发说说 hello"}},
            ]
        )
    )

    assert iter_reference_message_ids(event) == [123456]


def test_materialize_video_covers_replaces_video_with_trusted_cover(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from PIL import Image

    from qzone_bridge import video as video_mod
    from qzone_bridge.media import PostMedia, PostPayload

    def fake_extract(_path: Path, output_path: Path, *, name: str = "") -> None:
        Image.new("RGB", (320, 180), (42, 84, 126)).save(output_path)

    monkeypatch.setattr(video_mod, "_extract_frame_with_ffmpeg", fake_extract)
    source = tmp_path / "clip.webm"
    source.write_bytes(b"fake video bytes")
    post = PostPayload(
        content="hello",
        media=[
            PostMedia(
                kind="video",
                source=str(source),
                name="clip.webm",
                mime_type="video/webm",
                size=source.stat().st_size,
                trusted_local=True,
            )
        ],
    )

    prepared = video_mod.materialize_video_covers(post, tmp_path / "covers")

    assert prepared.content == "hello"
    assert prepared.attachments == []
    assert len(prepared.media) == 1
    cover = prepared.media[0]
    assert cover.kind == "image"
    assert cover.raw_type == "video"
    assert cover.mime_type == "image/jpeg"
    assert cover.trusted_local is True
    assert Path(cover.source).is_file()
    with Image.open(cover.source) as image:
        assert image.size == (320, 180)


def test_materialize_video_covers_downloads_trusted_message_video_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from PIL import Image

    from qzone_bridge import video as video_mod
    from qzone_bridge.media import PostMedia, PostPayload

    seen: dict[str, Path] = {}

    monkeypatch.setattr(video_mod, "is_remote_media_url_allowed", lambda _source: True)

    def fake_download(_source: str, target: Path) -> None:
        target.write_bytes(b"fake video bytes")

    def fake_extract(path: Path, output_path: Path, *, name: str = "") -> None:
        seen["path"] = path
        Image.new("RGB", (320, 180), (42, 84, 126)).save(output_path)

    monkeypatch.setattr(video_mod, "_stream_remote_video", fake_download)
    monkeypatch.setattr(video_mod, "_extract_frame_with_ffmpeg", fake_extract)
    post = PostPayload(
        content="hello",
        media=[
            PostMedia(
                kind="video",
                source="https://example.test/cache/95d20307dfb960194a9210eff4824876.mp4",
                name="95d20307dfb960194a9210eff4824876.mp4",
                mime_type="video/mp4",
                trusted_local=True,
            )
        ],
    )

    prepared = video_mod.materialize_video_covers(post, tmp_path / "covers")

    assert seen["path"].is_file()
    assert seen["path"].parent.name == "video_sources"
    assert prepared.media[0].raw_type == "video"
    assert Path(prepared.media[0].source).is_file()


def test_materialize_video_sources_decodes_trusted_base64_video(tmp_path: Path) -> None:
    from qzone_bridge import video as video_mod
    from qzone_bridge.media import PostMedia, PostPayload

    encoded = base64.b64encode(b"fake video bytes").decode("ascii")
    post = PostPayload(
        content="hello",
        media=[
            PostMedia(
                kind="video",
                source=f"base64://{encoded}",
                name="clip.mp4",
                mime_type="video/mp4",
                trusted_local=True,
            )
        ],
    )

    prepared = video_mod.materialize_video_sources(post, tmp_path / "sources")

    assert prepared.media[0].source != post.media[0].source
    assert prepared.media[0].trusted_local is True
    path = Path(prepared.media[0].source)
    assert path.is_file()
    assert path.read_bytes() == b"fake video bytes"


def test_local_media_repairs_drive_relative_video_path(tmp_path: Path) -> None:
    from qzone_bridge.local_media import resolve_trusted_local_media_path

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake video bytes")
    text = str(source)
    if len(text) < 3 or text[1] != ":" or text[2] not in "\\/":
        pytest.skip("Windows drive path required")
    damaged = text[:2] + text[3:]

    resolved = resolve_trusted_local_media_path(damaged, name=source.name, suffixes={".mp4"})

    assert resolved is not None
    assert resolved.samefile(source)


def test_daemon_publish_post_blocks_video_cover_fallback_without_upload_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.errors import QzoneParseError
    from qzone_bridge.media import PostMedia

    monkeypatch.delenv("QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64", raising=False)
    monkeypatch.delenv("QZONE_UPLOAD_LOGIN_DATA_B64", raising=False)

    class _Client:
        async def publish_mood(self, *_args, **_kwargs):
            raise AssertionError("daemon must not publish a video cover frame when native upload credentials are missing")

    service = object.__new__(QzoneDaemonService)
    service.store = types.SimpleNamespace(root=tmp_path)
    service.client = _Client()
    service._ensure_session_ready = lambda: None
    service._set_success = lambda defer_save=True: None
    video = PostMedia(
        kind="video",
        source=str(tmp_path / "clip.mp4"),
        name="clip.mp4",
        mime_type="video/mp4",
        trusted_local=True,
    )
    Path(video.source).write_bytes(b"fake video bytes")

    with pytest.raises(QzoneParseError) as error:
        asyncio.run(
            service.publish_post(
                content="hello",
                media=[video.to_dict()],
                content_sanitized=True,
            )
        )

    assert "Web Cookie/p_skey" in str(error.value)
    assert error.value.detail["required"] == "Web Cookie/p_skey"


def test_daemon_publish_post_blocks_unsupported_video_mix_cover_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.errors import QzoneParseError
    from qzone_bridge.media import PostMedia

    monkeypatch.setenv("QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64", base64.b64encode(b"login-data").decode("ascii"))

    class _Client:
        async def publish_mood(self, *_args, **_kwargs):
            raise AssertionError("daemon must not publish video mixes as cover images")

    service = object.__new__(QzoneDaemonService)
    service.store = types.SimpleNamespace(root=tmp_path)
    service.client = _Client()
    service._ensure_session_ready = lambda: None
    service._set_success = lambda defer_save=True: None
    first = tmp_path / "clip1.mp4"
    second = tmp_path / "clip2.mp4"
    first.write_bytes(b"fake video bytes 1")
    second.write_bytes(b"fake video bytes 2")
    media = [
        PostMedia(kind="video", source=str(first), name="clip1.mp4", mime_type="video/mp4", trusted_local=True),
        PostMedia(kind="video", source=str(second), name="clip2.mp4", mime_type="video/mp4", trusted_local=True),
    ]

    with pytest.raises(QzoneParseError) as error:
        asyncio.run(service.publish_post(content="hello", media=[item.to_dict() for item in media], content_sanitized=True))

    assert "Web Cookie/p_skey" in str(error.value)
    assert error.value.detail["required"] == "Web Cookie/p_skey"
    assert error.value.detail["media_count"] == 2
    assert [item["source_exists"] for item in error.value.detail["media"]] == [True, True]
    assert [item["source_name"] for item in error.value.detail["media"]] == ["clip1.mp4", "clip2.mp4"]


def test_daemon_publish_post_ignores_a2_credentials_without_h5_cookie(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.errors import QzoneParseError
    from qzone_bridge.media import PostMedia
    from qzone_bridge.models import SessionState

    monkeypatch.setenv("QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64", base64.b64encode(b"login-data").decode("ascii"))

    service = object.__new__(QzoneDaemonService)
    service.store = types.SimpleNamespace(root=tmp_path)
    service.state = types.SimpleNamespace(session=SessionState(uin=3112333596))
    service.client = types.SimpleNamespace(timeout=1.5)
    service._ensure_session_ready = lambda: None
    service._set_success = lambda defer_save=True: (_ for _ in ()).throw(AssertionError("must not mark success"))

    async def fake_wait_for_native_video_feed(**_kwargs):
        raise AssertionError("missing H5 Cookie/p_skey must stop before any publish verification")

    service._wait_for_native_video_feed = fake_wait_for_native_video_feed

    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"chunk")
    video = PostMedia(kind="video", source=str(video_path), name="clip.mp4", mime_type="video/mp4", trusted_local=True)

    with pytest.raises(QzoneParseError) as error:
        asyncio.run(service.publish_post(content="hello", media=[video.to_dict()], content_sanitized=True))

    assert "Web Cookie/p_skey" in str(error.value)
    assert error.value.detail["required"] == "Web Cookie/p_skey"
    assert error.value.detail["stable_method"] == "h5_video_publish_update_visibility"
    assert error.value.detail["web_cookie_configured"] is False


def test_daemon_native_video_verification_checks_feed_detail_for_vid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qzone_bridge import daemon as daemon_mod
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.models import SessionState

    monkeypatch.setattr(daemon_mod, "NATIVE_VIDEO_VERIFY_RETRY_DELAYS_SECONDS", (0.0,))
    calls: dict[str, object] = {"details": []}

    service = object.__new__(QzoneDaemonService)
    service.state = types.SimpleNamespace(session=SessionState(uin=3112333596))

    async def fake_list_feeds(**kwargs):
        calls["list_kwargs"] = kwargs
        return {
            "items": [
                {
                    "fid": "fid-no-inline-vid",
                    "hostuin": 3112333596,
                    "appid": 311,
                    "summary": "内含视频和图片",
                    "raw": {"summary": "内含视频和图片"},
                }
            ]
        }

    async def fake_detail_feed(**kwargs):
        calls["details"].append(kwargs)
        return {
            "entry": {"fid": kwargs["fid"], "summary": "detail", "ugc_right": 1},
            "raw": {"video_id": "1074_target_vid", "is_video": 1, "ugc_right": 1},
        }

    service.list_feeds = fake_list_feeds
    service.detail_feed = fake_detail_feed

    verified = asyncio.run(service._wait_for_native_video_feed(vid="1074_target_vid"))

    assert verified is not None
    assert verified["fid"] == "fid-no-inline-vid"
    assert verified["verification_source"] == "self_detail"
    assert calls["details"] == [{"hostuin": 3112333596, "fid": "fid-no-inline-vid", "appid": 311}]


def test_daemon_native_video_verification_uses_publishmood_tid_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qzone_bridge import daemon as daemon_mod
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.models import SessionState

    monkeypatch.setattr(daemon_mod, "NATIVE_VIDEO_VERIFY_RETRY_DELAYS_SECONDS", (0.0,))
    calls: dict[str, object] = {"details": [], "listed": False}

    service = object.__new__(QzoneDaemonService)
    service.state = types.SimpleNamespace(session=SessionState(uin=3112333596))

    async def fake_detail_feed(**kwargs):
        calls["details"].append(kwargs)
        return {
            "entry": {"fid": kwargs["fid"], "summary": "detail", "ugc_right": 1},
            "raw": {"video": {"vid": "1074_target_vid"}, "ugc_right": 1},
        }

    async def fake_list_feeds(**_kwargs):
        calls["listed"] = True
        return {"items": []}

    service.detail_feed = fake_detail_feed
    service.list_feeds = fake_list_feeds

    verified = asyncio.run(service._wait_for_native_video_feed(vid="1074_target_vid", fid="fid-from-publishmood"))

    assert verified is not None
    assert verified["fid"] == "fid-from-publishmood"
    assert verified["verification_source"] == "publishmood_rsp_detail"
    assert calls["details"] == [{"hostuin": 3112333596, "fid": "fid-from-publishmood", "appid": 311}]
    assert calls["listed"] is False


def test_daemon_native_video_verification_rejects_unproven_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qzone_bridge import daemon as daemon_mod
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.models import SessionState

    monkeypatch.setattr(daemon_mod, "NATIVE_VIDEO_VERIFY_RETRY_DELAYS_SECONDS", (0.0,))
    service = object.__new__(QzoneDaemonService)
    service.state = types.SimpleNamespace(session=SessionState(uin=3112333596))

    async def fake_list_feeds(**_kwargs):
        return {
            "items": [
                {
                    "fid": "fid-visible-unknown",
                    "hostuin": 3112333596,
                    "appid": 311,
                    "raw": {"video": {"vid": "1074_target_vid"}},
                }
            ]
        }

    async def fake_detail_feed(**kwargs):
        return {
            "entry": {"fid": kwargs["fid"], "summary": "detail"},
            "raw": {"video": {"vid": "1074_target_vid"}},
        }

    service.list_feeds = fake_list_feeds
    service.detail_feed = fake_detail_feed

    verified = asyncio.run(service._wait_for_native_video_feed(vid="1074_target_vid"))

    assert verified is None
    diagnostics = service._last_native_video_verification_diagnostics
    assert diagnostics["result"] == "non_public_visibility"
    assert diagnostics["non_public_visibility_hits"][0]["visibility_markers"][0]["kind"] == "visibility_unproven"


def test_daemon_native_video_verification_rejects_public_text_without_visibility_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qzone_bridge import daemon as daemon_mod
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.models import SessionState

    monkeypatch.setattr(daemon_mod, "NATIVE_VIDEO_VERIFY_RETRY_DELAYS_SECONDS", (0.0,))
    service = object.__new__(QzoneDaemonService)
    service.state = types.SimpleNamespace(session=SessionState(uin=3112333596))

    item = {
        "fid": "fid-public-word-only",
        "hostuin": 3112333596,
        "appid": 311,
        "summary": "public",
        "raw": {"video": {"vid": "1074_target_vid"}, "title": "public"},
    }

    async def fake_list_feeds(**_kwargs):
        return {"items": [dict(item)]}

    async def fake_detail_feed(**kwargs):
        return {"entry": {"fid": kwargs["fid"], "summary": "public"}, "raw": dict(item["raw"])}

    service.list_feeds = fake_list_feeds
    service.detail_feed = fake_detail_feed

    verified = asyncio.run(service._wait_for_native_video_feed(vid="1074_target_vid"))

    assert verified is None
    diagnostics = service._last_native_video_verification_diagnostics
    assert diagnostics["result"] == "non_public_visibility"
    assert diagnostics["non_public_visibility_hits"][0]["visibility_markers"][0]["kind"] == "visibility_unproven"


def test_daemon_native_video_verification_requires_explicit_self_hostuin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qzone_bridge import daemon as daemon_mod
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.models import SessionState

    monkeypatch.setattr(daemon_mod, "NATIVE_VIDEO_VERIFY_RETRY_DELAYS_SECONDS", (0.0,))
    service = object.__new__(QzoneDaemonService)
    service.state = types.SimpleNamespace(session=SessionState(uin=3112333596))
    calls: dict[str, int] = {"detail": 0}

    async def fake_list_feeds(**_kwargs):
        return {
            "items": [
                {
                    "fid": "fid-missing-hostuin",
                    "appid": 311,
                    "ugc_right": 1,
                    "raw": {"video": {"vid": "1074_target_vid"}, "ugc_right": 1},
                }
            ]
        }

    async def fake_detail_feed(**_kwargs):
        calls["detail"] += 1
        raise AssertionError("missing hostuin feed item must not be used for detail verification")

    service.list_feeds = fake_list_feeds
    service.detail_feed = fake_detail_feed

    verified = asyncio.run(service._wait_for_native_video_feed(vid="1074_target_vid"))

    assert verified is None
    assert calls["detail"] == 0
    diagnostics = service._last_native_video_verification_diagnostics
    assert diagnostics["result"] == "not_verified"
    assert diagnostics["scopes"]["self"]["native_video_candidate_count"] == 0
    assert diagnostics["scopes"]["self"]["svid_hits"][0]["accepted_context"] is False


def test_daemon_verify_native_video_feed_requires_public_svid() -> None:
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.models import SessionState

    service = object.__new__(QzoneDaemonService)
    service.state = types.SimpleNamespace(session=SessionState(uin=3112333596))
    service._ensure_session_ready = lambda: None
    service._set_success = lambda defer_save=True: None

    async def fake_wait_for_native_video_feed(**kwargs):
        assert kwargs == {"vid": "vid-onebot", "fid": "fid-onebot"}
        return {
            "fid": "fid-onebot",
            "hostuin": 3112333596,
            "appid": 311,
            "ugc_right": 1,
            "raw": {"html": "qzvideo/vid-onebot"},
        }

    service._wait_for_native_video_feed = fake_wait_for_native_video_feed

    payload = asyncio.run(
        service.verify_native_video_feed(
            vid="vid-onebot",
            fid="fid-onebot",
            method="h5_video_publish_update_visibility",
        )
    )

    assert payload["native_video"] is True
    assert payload["status"] == "published_native_video"
    assert payload["operation_status"] == "verified_feed_video"
    assert payload["raw"]["method"] == "h5_video_publish_update_visibility"

    with pytest.raises(QzoneParseError):
        asyncio.run(service.verify_native_video_feed(vid=""))


def test_daemon_bridge_response_wraps_unhandled_errors_as_json() -> None:
    from qzone_bridge import daemon as daemon_mod

    captured: dict[str, object] = {}
    service = types.SimpleNamespace(_set_error=lambda exc: captured.setdefault("error", exc))

    async def action():
        raise RuntimeError("boom")

    response = asyncio.run(daemon_mod._bridge_response(service, action))
    payload = json.loads(response.text)

    assert response.status == 500
    assert payload["ok"] is False
    assert payload["error"]["code"] == "QZONE_REQUEST"
    assert payload["error"]["detail"]["type"] == "RuntimeError"
    assert "boom" in payload["error"]["detail"]["message"]
    assert type(captured["error"]).__name__ == "QzoneRequestError"


def test_daemon_error_detail_compacts_large_video_publish_payload() -> None:
    from qzone_bridge import daemon as daemon_mod

    detail = daemon_mod._error_detail(
        QzoneRequestError(
            "failed",
            detail={
                "publish_result": {"code": 0, "feedinfo": "<li>" + ("x" * 8000) + "</li>"},
                "cover_upload": {"upload_responses": [{"ret": 0}] * 20, "control_response": {"ret": 0, "msg": "ok"}},
            },
        )
    )

    assert detail["publish_result"]["feedinfo"] == {"present": True, "length": 8009}
    assert detail["cover_upload"]["upload_responses"] == {"count": 20}
    assert detail["cover_upload"]["control_response"] == {"present": True, "ret": 0, "msg": "ok"}


def test_daemon_error_detail_redacts_upload_session_identifiers() -> None:
    from qzone_bridge import daemon as daemon_mod

    detail = daemon_mod._error_detail(
        QzoneRequestError(
            "failed",
            detail={
                "upload_result": {
                    "session": "upload-session",
                    "client_key": "3112333596_1780329600123",
                    "a2_b64": "secret-a2",
                    "A2TicketBytes": [1, 2, 3],
                    "vLoginDataB64": "secret-vlogin",
                    "login_data_base64": "secret-login",
                },
                "cover_upload": {"session": "cover-session"},
            },
        )
    )

    assert detail["upload_result"]["session"] == "***"
    assert detail["upload_result"]["client_key"] == "***"
    assert detail["upload_result"]["a2_b64"] == "***"
    assert detail["upload_result"]["A2TicketBytes"] == "***"
    assert detail["upload_result"]["vLoginDataB64"] == "***"
    assert detail["upload_result"]["login_data_base64"] == "***"
    assert detail["cover_upload"]["session"] == "***"


def test_native_video_module_removes_client_handoff_helpers() -> None:
    from qzone_bridge import native_video

    assert not hasattr(native_video, "publish_native_video_post")
    assert not hasattr(native_video, "build_native_qzone_video_publish_uri")
    assert not hasattr(native_video, "native_qzone_protocol_handler")
    assert not hasattr(native_video, "open_native_qzone_uri")


def test_native_video_candidate_requires_single_video_without_other_media(tmp_path: Path) -> None:
    from qzone_bridge.native_video import native_video_candidate
    from qzone_bridge.media import PostMedia, PostPayload

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake video bytes")
    video = PostMedia(kind="video", source=str(source), name="clip.mp4", mime_type="video/mp4", trusted_local=True)

    assert native_video_candidate(PostPayload(content="hello", media=[video])) is video
    assert native_video_candidate(
        PostPayload(
            content="hello",
            media=[video, PostMedia(kind="image", source=str(tmp_path / "cover.jpg"), trusted_local=True)],
        )
    ) is None


def test_collect_post_payload_finally_collapses_video_cover_companion(tmp_path: Path) -> None:
    from qzone_bridge.media import PostMedia, collect_post_payload

    video_path = tmp_path / "clip.mp4"
    cover_path = tmp_path / "clip-cover.jpg"
    video_path.write_bytes(b"fake video bytes")
    cover_path.write_bytes(b"fake image bytes")

    event = types.SimpleNamespace(
        message_obj=types.SimpleNamespace(
            message=[
                {"type": "video", "data": {"file": str(video_path), "mime_type": "video/mp4"}},
                {"type": "text", "data": {"text": "qzone post hello"}},
            ]
        )
    )
    cover = PostMedia(kind="image", source=str(cover_path), name="clip-cover.jpg", trusted_local=True)

    post = collect_post_payload(event, command_prefixes=("qzone post",), extra_media=[cover])

    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == str(video_path)


def test_collapse_duplicate_video_candidates_preserves_distinct_local_videos_with_same_name(tmp_path: Path) -> None:
    from qzone_bridge.media import PostMedia, collapse_single_video_cover_companion_media

    first = tmp_path / "first" / "clip.mp4"
    second = tmp_path / "second" / "clip.mp4"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first video")
    second.write_bytes(b"second video")

    media = collapse_single_video_cover_companion_media(
        [
            PostMedia(kind="video", source=str(first), name="clip.mp4", mime_type="video/mp4", trusted_local=True),
            PostMedia(kind="video", source=str(second), name="clip.mp4", mime_type="video/mp4", trusted_local=True),
        ]
    )

    assert [item.source for item in media] == [str(first), str(second)]


def test_collapse_duplicate_video_candidates_prefers_single_existing_local_video_over_remote_alias(
    tmp_path: Path,
) -> None:
    from qzone_bridge.media import PostMedia, collapse_single_video_cover_companion_media

    local = tmp_path / "converted-from-reply.mp4"
    local.write_bytes(b"local video")

    media = collapse_single_video_cover_companion_media(
        [
            PostMedia(
                kind="video",
                source="https://example.test/download/cache-id-with-different-name.mp4",
                name="cache-id-with-different-name.mp4",
                mime_type="video/mp4",
                trusted_local=True,
            ),
            PostMedia(
                kind="video",
                source=str(local),
                name="converted-from-reply.mp4",
                mime_type="video/mp4",
                trusted_local=True,
            ),
        ]
    )

    assert len(media) == 1
    assert media[0].source == str(local)


def test_daemon_publish_post_collapses_video_cover_companion_before_native_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.errors import QzoneParseError
    from qzone_bridge.media import PostMedia

    monkeypatch.delenv("QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64", raising=False)
    monkeypatch.delenv("QZONE_UPLOAD_LOGIN_DATA_B64", raising=False)

    class _Client:
        async def publish_mood(self, *_args, **_kwargs):
            raise AssertionError("daemon must not publish a quoted video cover companion as a plain image")

    service = object.__new__(QzoneDaemonService)
    service.store = types.SimpleNamespace(root=tmp_path)
    service.client = _Client()
    service._ensure_session_ready = lambda: None
    service._set_success = lambda defer_save=True: None
    video_path = tmp_path / "clip.mp4"
    cover_path = tmp_path / "clip-cover.jpg"
    video_path.write_bytes(b"fake video bytes")
    cover_path.write_bytes(b"fake image bytes")
    media = [
        PostMedia(kind="video", source=str(video_path), name="clip.mp4", mime_type="video/mp4", trusted_local=True),
        PostMedia(kind="image", source=str(cover_path), name="clip-cover.jpg", mime_type="image/jpeg", trusted_local=True),
    ]

    with pytest.raises(QzoneParseError) as error:
        asyncio.run(
            service.publish_post(
                content="hello",
                media=[item.to_dict() for item in media],
                content_sanitized=True,
            )
        )

    assert "Web Cookie/p_skey" in str(error.value)
    assert error.value.detail["required"] == "Web Cookie/p_skey"
    assert error.value.detail["name"] == "clip.mp4"


def test_daemon_publish_post_collapses_duplicate_video_alias_before_native_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.errors import QzoneParseError
    from qzone_bridge.media import PostMedia

    monkeypatch.delenv("QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64", raising=False)
    monkeypatch.delenv("QZONE_UPLOAD_LOGIN_DATA_B64", raising=False)

    class _Client:
        async def publish_mood(self, *_args, **_kwargs):
            raise AssertionError("daemon must not publish a duplicate video alias as a cover image")

    service = object.__new__(QzoneDaemonService)
    service.store = types.SimpleNamespace(root=tmp_path)
    service.client = _Client()
    service._ensure_session_ready = lambda: None
    service._set_success = lambda defer_save=True: None
    video_path = tmp_path / "95d20307dfb960194a9210eff4824876.mp4"
    video_path.write_bytes(b"fake video bytes")
    media = [
        PostMedia(
            kind="video",
            source=str(video_path),
            name="95d20307dfb960194a9210eff4824876.mp4",
            mime_type="video/mp4",
            trusted_local=True,
        ),
        PostMedia(
            kind="video",
            source="https://example.test/video/95d20307dfb960194a9210eff4824876.mp4",
            name="95d20307dfb960194a9210eff4824876.mp4",
            mime_type="video/mp4",
            trusted_local=True,
        ),
    ]

    with pytest.raises(QzoneParseError) as error:
        asyncio.run(
            service.publish_post(
                content="hello",
                media=[item.to_dict() for item in media],
                content_sanitized=True,
            )
        )

    assert "Web Cookie/p_skey" in str(error.value)
    assert error.value.detail["required"] == "Web Cookie/p_skey"
    assert error.value.detail["name"] == "95d20307dfb960194a9210eff4824876.mp4"
    assert error.value.detail.get("media_count") != 2


def test_tencent_upload_pdu_round_trips_control_frame() -> None:
    from qzone_bridge.tencent_upload import (
        PDU_HEADER_LENGTH,
        PDU_OFFSET_CMD,
        PDU_OFFSET_LENGTH,
        PDU_OFFSET_SEQ,
        PDU_TOTAL_OVERHEAD,
        TENCENT_UPLOAD_CMD_CONTROL,
        decode_upload_pdu,
        decode_upload_pdu_size,
        encode_upload_pdu,
    )

    payload = b"jce-bytes"
    frame = encode_upload_pdu(TENCENT_UPLOAD_CMD_CONTROL, 12345, payload)
    header = frame[1 : 1 + PDU_HEADER_LENGTH]

    assert frame[0] == 0x04
    assert frame[-1] == 0x05
    assert len(frame) == len(payload) + PDU_TOTAL_OVERHEAD
    assert header[PDU_OFFSET_CMD : PDU_OFFSET_CMD + 4] == (1).to_bytes(4, "big")
    assert header[PDU_OFFSET_SEQ : PDU_OFFSET_SEQ + 4] == (12345).to_bytes(4, "big")
    assert header[PDU_OFFSET_LENGTH : PDU_OFFSET_LENGTH + 4] == len(frame).to_bytes(4, "big")
    assert decode_upload_pdu_size(frame[: 1 + PDU_HEADER_LENGTH]) == len(frame)

    decoded = decode_upload_pdu(frame)
    assert decoded.header.cmd == TENCENT_UPLOAD_CMD_CONTROL
    assert decoded.header.seq == 12345
    assert decoded.header.length == len(frame)
    assert decoded.payload == payload


def test_tencent_upload_pdu_rejects_malformed_frame() -> None:
    from qzone_bridge.tencent_upload import TencentUploadPduError, decode_upload_pdu

    with pytest.raises(TencentUploadPduError):
        decode_upload_pdu(b"\x04too-short\x05")
    with pytest.raises(TencentUploadPduError):
        decode_upload_pdu(b"\x03" + b"\x00" * 23 + b"\x05")


def test_qzone_video_upload_probe_documents_missing_daemon_requirements(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qzone_bridge.tencent_upload import (
        QZONE_VIDEO_UPLOAD_APPID,
        QZONE_VIDEO_UPLOAD_BACKUP_HOST,
        QZONE_VIDEO_UPLOAD_HOST,
        QZONE_VIDEO_UPLOAD_PORT,
        TENCENT_UPLOAD_CMD_CONTROL,
        TENCENT_UPLOAD_CMD_FILE,
        qzone_video_upload_probe,
    )
    monkeypatch.delenv("QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64", raising=False)
    monkeypatch.delenv("QZONE_UPLOAD_LOGIN_DATA_B64", raising=False)

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video bytes")

    probe = qzone_video_upload_probe(video)

    assert probe["appid"] == QZONE_VIDEO_UPLOAD_APPID
    assert probe["hosts"] == [QZONE_VIDEO_UPLOAD_HOST, QZONE_VIDEO_UPLOAD_BACKUP_HOST]
    assert probe["port"] == QZONE_VIDEO_UPLOAD_PORT
    assert probe["control_cmd"] == TENCENT_UPLOAD_CMD_CONTROL
    assert probe["file_cmd"] == TENCENT_UPLOAD_CMD_FILE
    assert probe["daemon_ready"] is False
    assert probe["video_readable"] is True
    implemented = {item["name"] for item in probe["requirements"] if item["status"] == "implemented"}
    missing = {item["name"] for item in probe["requirements"] if item["status"] == "missing"}
    assert {
        "jce_codec",
        "socket_upload_client",
        "publishmood_business_data",
        "video_cover_pic_qzone_upload",
        "feed_vid_verification",
    } <= implemented
    assert {"qq_upload_login_material"} <= missing


def test_plugin_collects_quoted_video_from_onebot_get_msg(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        async def get_msg(self, *, message_id):
            assert message_id == 123456
            return {
                "data": {
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": "clip.mp4",
                                "url": "https://example.test/video/clip.mp4",
                                "mime": "video/mp4",
                            },
                        }
                    ]
                }
            }

    event = types.SimpleNamespace(
        bot=_Bot(),
        message_obj=types.SimpleNamespace(
            message=[
                {"type": "reply", "data": {"id": "123456"}},
                {"type": "text", "data": {"text": "发说说 hello"}},
            ]
        ),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("发说说",)))

    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == "https://example.test/video/clip.mp4"
    assert post.media[0].trusted_local is True


def test_collect_message_media_parses_raw_cq_video_url() -> None:
    from qzone_bridge.media import collect_message_media, iter_reference_message_ids

    payload = {
        "raw_message": "[CQ:reply,id=123456][CQ:video,file=clip.mp4,url=https://example.test/video/clip.mp4]"
    }

    media = collect_message_media(payload)

    assert iter_reference_message_ids(types.SimpleNamespace(message_obj=types.SimpleNamespace(raw_message=payload))) == [
        123456
    ]
    assert len(media) == 1
    assert media[0].kind == "video"
    assert media[0].source == "https://example.test/video/clip.mp4"
    assert media[0].trusted_local is True


def test_collect_message_media_does_not_treat_bare_onebot_video_file_as_path() -> None:
    from qzone_bridge.media import collect_message_media

    payload = {
        "message": [
            {
                "type": "video",
                "data": {
                    "file": "95d20307dfb960194a9210eff4824876.mp4",
                    "url": "empty",
                    "path": "empty",
                    "file_id": "file-id-1",
                },
            }
        ]
    }

    assert collect_message_media(payload) == []


def test_collect_message_media_ignores_nonexistent_video_path_without_url() -> None:
    from qzone_bridge.media import collect_message_media

    payload = {
        "message": [
            {
                "type": "video",
                "data": {
                    "file": "95d20307dfb960194a9210eff4824876.mp4",
                    "path": r"D:Documents\Tencent Files\3112333596\nt_qq\nt_data\Video\2026-06\OriV9\95d20307dfb960194a9210eff4824876.mp4",
                    "file_size": 1234,
                },
            }
        ]
    }

    assert collect_message_media(payload) == []


def test_collect_message_media_prefers_video_url_over_stale_local_path() -> None:
    from qzone_bridge.media import collect_message_media

    payload = {
        "message": [
            {
                "type": "video",
                "data": {
                    "file": "95d20307dfb960194a9210eff4824876.mp4",
                    "path": r"D:Documents\Tencent Files\3112333596\nt_qq\nt_data\Video\2026-06\OriV9\95d20307dfb960194a9210eff4824876.mp4",
                    "url": "https://example.test/video/clip.mp4",
                    "file_size": 1234,
                },
            }
        ]
    }

    media = collect_message_media(payload)

    assert len(media) == 1
    assert media[0].kind == "video"
    assert media[0].source == "https://example.test/video/clip.mp4"


def test_collect_message_media_accepts_protocol_download_url_for_video() -> None:
    from qzone_bridge.media import collect_message_media

    payload = {
        "message": [
            {
                "type": "video",
                "data": {
                    "file": "95d20307dfb960194a9210eff4824876.mp4",
                    "path": r"D:Documents\Tencent Files\3112333596\nt_qq\nt_data\Video\2026-06\OriV9\95d20307dfb960194a9210eff4824876.mp4",
                    "download_url": "https://example.test/video/clip.mp4",
                    "file_size": 1234,
                },
            }
        ]
    }

    media = collect_message_media(payload)

    assert len(media) == 1
    assert media[0].kind == "video"
    assert media[0].source == "https://example.test/video/clip.mp4"


def test_collect_message_media_accepts_reference_attachment_download_url() -> None:
    from qzone_bridge.media import collect_message_media

    payload = {
        "referenced_message": {
            "attachments": [
                {
                    "type": "video",
                    "source": "95d20307dfb960194a9210eff4824876.mp4",
                    "download_url": "https://example.test/video/clip.mp4",
                    "mime": "video/mp4",
                }
            ]
        }
    }

    media = collect_message_media(payload)

    assert len(media) == 1
    assert media[0].kind == "video"
    assert media[0].source == "https://example.test/video/clip.mp4"


def test_collect_message_media_accepts_object_file_url_for_video() -> None:
    from qzone_bridge.media import collect_message_media

    class _Video:
        type = "Video"
        file = "95d20307dfb960194a9210eff4824876.mp4"
        file_url = "https://example.test/video/clip.mp4"
        mime_type = "video/mp4"

    media = collect_message_media({"message": [_Video()]})

    assert len(media) == 1
    assert media[0].kind == "video"
    assert media[0].source == "https://example.test/video/clip.mp4"


def test_collect_message_media_accepts_video_reference_field_without_extension(tmp_path: Path) -> None:
    from qzone_bridge.media import collect_message_media

    source = tmp_path / "videoseg_no_extension"
    source.write_bytes(b"fake video bytes")
    payload = {
        "referenced_message": {
            "attachments": [
                {
                    "kind": "video",
                    "source": str(source),
                    "name": "clip",
                    "mime_type": "video/mp4",
                }
            ]
        }
    }

    media = collect_message_media(payload)

    assert len(media) == 1
    assert media[0].kind == "video"
    assert media[0].source == str(source)
    assert media[0].trusted_local is True


def test_collect_message_media_ignores_bad_video_reference_fields() -> None:
    from qzone_bridge.media import collect_message_media

    payload = {
        "referenced_message": {
            "attachments": [
                {
                    "type": "video",
                    "path": r"D:Documents\Tencent Files\3112333596\nt_qq\nt_data\Video\2026-06\OriV9\95d20307dfb960194a9210eff4824876.mp4",
                    "file": "95d20307dfb960194a9210eff4824876.mp4",
                    "mime": "video/mp4",
                },
                "clip.mp4",
            ],
            "files": [
                {
                    "kind": "video",
                    "source": r"D:Documents\Tencent Files\3112333596\nt_qq\nt_data\Video\2026-06\OriV9\other.mp4",
                }
            ],
        }
    }

    assert collect_message_media(payload) == []


def test_collect_message_media_keeps_video_reference_field_url() -> None:
    from qzone_bridge.media import collect_message_media

    payload = {
        "referenced_message": {
            "attachments": [
                {
                    "type": "video",
                    "path": r"D:Documents\Tencent Files\3112333596\nt_qq\nt_data\Video\2026-06\OriV9\95d20307dfb960194a9210eff4824876.mp4",
                    "url": "https://example.test/video/clip.mp4",
                    "mime": "video/mp4",
                },
            ],
        }
    }

    media = collect_message_media(payload)

    assert len(media) == 1
    assert media[0].kind == "video"
    assert media[0].source == "https://example.test/video/clip.mp4"


def test_collect_post_payload_promotes_mp4_file_segment_with_url_to_video() -> None:
    from qzone_bridge.media import collect_post_payload

    event = types.SimpleNamespace(
        message_obj=types.SimpleNamespace(
            message=[
                {"type": "text", "data": {"text": "post hello"}},
                {
                    "type": "file",
                    "data": {
                        "file": "clip.mp4",
                        "url": "https://example.test/video/clip.mp4",
                        "mime": "video/mp4",
                    },
                },
            ]
        )
    )

    post = collect_post_payload(event, command_prefixes=("post",))

    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == "https://example.test/video/clip.mp4"
    assert not post.attachments


def test_plugin_resolves_quoted_video_file_id_with_onebot_get_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake video bytes")

    class _Bot:
        def __init__(self) -> None:
            self.get_file_params: list[dict[str, str]] = []

        async def get_msg(self, *, message_id):
            assert message_id == 123456
            return {
                "data": {
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": "95d20307dfb960194a9210eff4824876.mp4",
                                "url": "empty",
                                "path": "empty",
                                "file_id": "video-file-id",
                            },
                        }
                    ]
                }
            }

        async def get_file(self, **params):
            self.get_file_params.append(params)
            assert params in ({"file_id": "video-file-id"}, {"file": "video-file-id"})
            return {"data": {"file": str(source), "file_name": "clip.mp4", "file_size": source.stat().st_size}}

    bot = _Bot()
    event = types.SimpleNamespace(
        bot=bot,
        message_obj=types.SimpleNamespace(
            message=[
                {"type": "reply", "data": {"id": "123456"}},
                {"type": "text", "data": {"text": "post hello"}},
            ]
        ),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert bot.get_file_params
    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == str(source)
    assert post.media[0].trusted_local is True


def test_plugin_resolves_quoted_video_get_file_download_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        def __init__(self) -> None:
            self.get_file_params: list[dict[str, str]] = []

        async def get_msg(self, *, message_id):
            assert message_id == 123456
            return {
                "data": {
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": "empty",
                                "url": "empty",
                                "path": "empty",
                                "file_id": "video-file-id",
                            },
                        }
                    ]
                }
            }

        async def get_file(self, **params):
            self.get_file_params.append(params)
            assert params in ({"file_id": "video-file-id"}, {"file": "video-file-id"})
            return {
                "data": {
                    "download_url": "https://example.test/video/clip.mp4",
                    "file_name": "clip.mp4",
                    "file_size": 1234,
                }
            }

    bot = _Bot()
    event = types.SimpleNamespace(
        bot=bot,
        message_obj=types.SimpleNamespace(
            message=[
                {"type": "reply", "data": {"id": "123456"}},
                {"type": "text", "data": {"text": "post hello"}},
            ]
        ),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert bot.get_file_params
    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == "https://example.test/video/clip.mp4"
    assert post.media[0].trusted_local is True


def test_plugin_resolves_llonebot_video_get_file_base64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    encoded = base64.b64encode(b"fake video bytes").decode("ascii")

    class _Bot:
        def __init__(self) -> None:
            self.get_file_params: list[dict[str, str]] = []

        async def get_msg(self, *, message_id):
            assert message_id == 123456
            return {
                "data": {
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": "empty",
                                "path": "empty",
                                "url": "empty",
                                "file_id": "video-file-id",
                            },
                        }
                    ]
                }
            }

        async def get_file(self, **params):
            self.get_file_params.append(params)
            assert params in (
                {"file_id": "video-file-id"},
                {"file": "video-file-id"},
                {"type": "path", "file_id": "video-file-id"},
                {"type": "url", "file_id": "video-file-id"},
            )
            return {
                "data": {
                    "base64": encoded,
                    "file_name": "clip.mp4",
                    "file_size": len(b"fake video bytes"),
                }
            }

    bot = _Bot()
    event = types.SimpleNamespace(
        bot=bot,
        message_obj=types.SimpleNamespace(
            message=[
                {"type": "reply", "data": {"id": "123456"}},
                {"type": "text", "data": {"text": "post hello"}},
            ]
        ),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert bot.get_file_params
    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == f"base64://{encoded}"
    assert post.media[0].trusted_local is True


def test_plugin_resolves_quoted_video_file_url_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        def __init__(self) -> None:
            self.group_file_url_params: list[dict[str, object]] = []

        async def get_msg(self, *, message_id):
            assert message_id == 123456
            return {
                "data": {
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": "empty",
                                "url": "empty",
                                "path": "empty",
                                "file_id": "video-file-id",
                            },
                        }
                    ]
                }
            }

        async def get_group_file_url(self, **params):
            self.group_file_url_params.append(params)
            assert params == {"group_id": 998877, "file_id": "video-file-id"}
            return {
                "data": {
                    "file_url": "https://example.test/video/clip.mp4",
                    "file_name": "clip.mp4",
                    "file_size": 1234,
                }
            }

    bot = _Bot()
    event = types.SimpleNamespace(
        bot=bot,
        message_obj=types.SimpleNamespace(
            group_id=998877,
            message=[
                {"type": "reply", "data": {"id": "123456"}},
                {"type": "text", "data": {"text": "post hello"}},
            ],
        ),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert bot.group_file_url_params
    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == "https://example.test/video/clip.mp4"
    assert post.media[0].trusted_local is True


def test_plugin_resolves_quoted_video_file_id_with_raw_get_video_url_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        def __init__(self) -> None:
            self.get_video_url_params: list[dict[str, str]] = []

        async def get_msg(self, *, message_id):
            assert message_id == 123456
            return {
                "data": {
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": "empty",
                                "url": "empty",
                                "path": "empty",
                                "file_id": "video-file-id",
                            },
                        }
                    ]
                }
            }

        async def get_file(self, **_params):
            raise RuntimeError("get_file unsupported")

        async def get_group_file_url(self, **_params):
            raise RuntimeError("get_group_file_url unsupported")

        async def get_private_file_url(self, **_params):
            raise RuntimeError("get_private_file_url unsupported")

        async def get_file_url(self, **_params):
            raise RuntimeError("get_file_url unsupported")

        async def get_video_url(self, **params):
            self.get_video_url_params.append(params)
            if params != {"file_id": "video-file-id"}:
                raise RuntimeError("unsupported get_video_url params")
            return {"data": "https://example.test/video/clip.mp4"}

    bot = _Bot()
    event = types.SimpleNamespace(
        bot=bot,
        message_obj=types.SimpleNamespace(
            message=[
                {"type": "reply", "data": {"id": "123456"}},
                {"type": "text", "data": {"text": "post hello"}},
            ]
        ),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert {"file_id": "video-file-id"} in bot.get_video_url_params
    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == "https://example.test/video/clip.mp4"
    assert post.media[0].trusted_local is True


def test_plugin_get_msg_bad_video_path_falls_back_to_file_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake video bytes")

    class _Bot:
        def __init__(self) -> None:
            self.get_file_params: list[dict[str, str]] = []

        async def get_msg(self, *, message_id):
            assert message_id == 123456
            return {
                "data": {
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": "95d20307dfb960194a9210eff4824876.mp4",
                                "path": r"D:Documents\Tencent Files\3112333596\nt_qq\nt_data\Video\2026-06\OriV9\95d20307dfb960194a9210eff4824876.mp4",
                                "file_id": "video-file-id",
                                "file_size": source.stat().st_size,
                            },
                        }
                    ]
                }
            }

        async def get_file(self, **params):
            self.get_file_params.append(params)
            assert params in ({"file_id": "video-file-id"}, {"file": "video-file-id"})
            return {"data": {"file": str(source), "file_name": "clip.mp4", "file_size": source.stat().st_size}}

    bot = _Bot()
    event = types.SimpleNamespace(
        bot=bot,
        message_obj=types.SimpleNamespace(
            message=[
                {"type": "reply", "data": {"id": "123456"}},
                {"type": "text", "data": {"text": "post hello"}},
            ]
        ),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert bot.get_file_params
    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == str(source)
    assert post.media[0].trusted_local is True


def test_plugin_get_msg_attachment_video_file_id_falls_back_to_get_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake video bytes")

    class _Bot:
        def __init__(self) -> None:
            self.get_file_params: list[dict[str, str]] = []

        async def get_msg(self, *, message_id):
            assert message_id == 123456
            return {
                "data": {
                    "attachments": [
                        {
                            "type": "video",
                            "data": {
                                "file": "95d20307dfb960194a9210eff4824876.mp4",
                                "path": r"D:Documents\Tencent Files\3112333596\nt_qq\nt_data\Video\2026-06\OriV9\95d20307dfb960194a9210eff4824876.mp4",
                                "file_id": "video-file-id",
                                "mime": "video/mp4",
                            },
                        }
                    ]
                }
            }

        async def get_file(self, **params):
            self.get_file_params.append(params)
            assert params in ({"file_id": "video-file-id"}, {"file": "video-file-id"})
            return {"data": {"file": str(source), "file_name": "clip.mp4", "file_size": source.stat().st_size}}

    bot = _Bot()
    event = types.SimpleNamespace(
        bot=bot,
        message_obj=types.SimpleNamespace(
            message=[
                {"type": "reply", "data": {"id": "123456"}},
                {"type": "text", "data": {"text": "post hello"}},
            ]
        ),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert bot.get_file_params
    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == str(source)
    assert post.media[0].trusted_local is True


def test_plugin_get_msg_good_video_ignores_bad_attachment_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        async def get_msg(self, *, message_id):
            assert message_id == 123456
            return {
                "data": {
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": "clip.mp4",
                                "url": "https://example.test/video/clip.mp4",
                                "mime": "video/mp4",
                            },
                        }
                    ],
                    "attachments": [
                        {
                            "type": "video",
                            "path": r"D:Documents\Tencent Files\3112333596\nt_qq\nt_data\Video\2026-06\OriV9\95d20307dfb960194a9210eff4824876.mp4",
                            "file": "95d20307dfb960194a9210eff4824876.mp4",
                            "mime": "video/mp4",
                        }
                    ],
                }
            }

    event = types.SimpleNamespace(
        bot=_Bot(),
        message_obj=types.SimpleNamespace(
            message=[
                {"type": "reply", "data": {"id": "123456"}},
                {"type": "text", "data": {"text": "post hello"}},
            ]
        ),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == "https://example.test/video/clip.mp4"


def test_plugin_collects_quoted_video_from_raw_cq_get_msg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        async def get_msg(self, *, message_id):
            assert message_id == 123456
            return {
                "data": {
                    "raw_message": "[CQ:video,file=clip.mp4,url=https://example.test/video/clip.mp4]"
                }
            }

    event = types.SimpleNamespace(
        bot=_Bot(),
        message_obj=types.SimpleNamespace(raw_message="[CQ:reply,id=123456]post hello"),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == "https://example.test/video/clip.mp4"
    assert post.media[0].trusted_local is True


def test_plugin_resolves_quoted_video_with_onebot_call_action_id_variant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    source = tmp_path / "videoseg_no_extension"
    source.write_bytes(b"fake video bytes")
    calls: list[tuple[str, dict[str, object]]] = []

    class _Api:
        async def call_action(self, action: str, **kwargs):
            calls.append((action, kwargs))
            if action != "get_msg" or kwargs != {"id": "123456"}:
                raise RuntimeError("unsupported get_msg params")
            return {
                "data": {
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": str(source),
                                "mime": "video/mp4",
                            },
                        }
                    ]
                }
            }

    event = types.SimpleNamespace(
        bot=types.SimpleNamespace(api=_Api()),
        message_obj=types.SimpleNamespace(
            message=[
                {"type": "reply", "data": {"id": "123456"}},
                {"type": "text", "data": {"text": "post"}},
            ]
        ),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert ("get_msg", {"id": "123456"}) in calls
    assert post.content == ""
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == str(source)
    assert post.media[0].trusted_local is True


def test_plugin_resolves_quoted_video_with_onebot_positional_call_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake video bytes")
    calls: list[tuple[str, dict[str, object]]] = []

    class _Api:
        async def call_action(self, action: str, params: dict[str, object]):
            calls.append((action, dict(params)))
            assert action == "get_msg"
            assert "message_id" in params
            return {
                "data": {
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": str(source),
                                "mime": "video/mp4",
                            },
                        }
                    ]
                }
            }

    event = types.SimpleNamespace(
        bot=types.SimpleNamespace(api=_Api()),
        message_obj=types.SimpleNamespace(
            message=[
                {"type": "reply", "data": {"id": "123456"}},
                {"type": "text", "data": {"text": "post"}},
            ]
        ),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert len(calls) == 1
    assert calls[0][0] == "get_msg"
    assert calls[0][1].get("message_id") in {123456, "123456"}
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == str(source)
    assert post.media[0].trusted_local is True


def test_plugin_collects_astrbot_reply_video_component_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake video bytes")

    class _Video:
        type = "Video"
        file = "95d20307dfb960194a9210eff4824876.mp4"

        async def convert_to_file_path(self):
            return str(source)

    class _Reply:
        type = "Reply"
        id = 123456
        chain = [_Video()]

    event = types.SimpleNamespace(
        message_obj=types.SimpleNamespace(
            message=[
                _Reply(),
                {"type": "text", "data": {"text": "post hello"}},
            ]
        )
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == str(source)
    assert post.media[0].name == "95d20307dfb960194a9210eff4824876.mp4"
    assert post.media[0].trusted_local is True


def test_plugin_collects_astrbot_routed_reply_video_without_cover_companion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake video bytes")
    cover = tmp_path / "clip-cover.jpg"
    cover.write_bytes(b"fake image bytes")

    class _Video:
        type = "Video"
        file = "95d20307dfb960194a9210eff4824876.mp4"
        mime_type = "video/mp4"

        async def convert_to_file_path(self):
            return str(source)

    class _Image:
        type = "Image"
        file = str(cover)
        mime_type = "image/jpeg"

    class _Reply:
        type = "Reply"
        id = 123456
        chain = [_Video(), _Image()]

    event = types.SimpleNamespace(
        message_obj=types.SimpleNamespace(
            message=[
                _Reply(),
                {"type": "text", "data": {"text": "qzone post hello"}},
            ]
        )
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("qzone post",)))

    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == str(source)
    assert post.media[0].trusted_local is True
    assert post.attachments == []

    captured: dict[str, object] = {}

    class _Controller:
        async def publish_post(self, **kwargs):
            captured.update(kwargs)
            return {"native_video": True, "status": "published_native_video"}

    async def _same_post(payload):
        return payload

    async def _render_preview(payload):
        return main.PostPayload(content=payload.content, media=[])

    async def _noop_bind(_event):
        captured["auto_bind_called"] = True

    plugin.settings = types.SimpleNamespace(native_video_publish=True)
    plugin.controller = _Controller()
    plugin._prepare_video_sources = _same_post
    plugin._prepare_publish_payload = _render_preview

    asyncio.run(plugin._publish_post_payload(post, event=event))

    assert captured["content"] == "hello"
    assert captured["content_sanitized"] is True
    assert "auto_bind_called" not in captured
    assert [item["kind"] for item in captured["media"]] == ["video"]
    assert captured["media"][0]["source"] == str(source)


def test_plugin_collapses_same_quoted_video_from_reply_component_and_onebot_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    file_name = "95d20307dfb960194a9210eff4824876.mp4"
    source = tmp_path / file_name
    source.write_bytes(b"fake video bytes")
    raw_command = "\uff0cqzone post live video smoke"

    class _Bot:
        async def get_msg(self, *, message_id):
            assert message_id == 123456
            return {
                "data": {
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": file_name,
                                "url": f"https://example.test/video/{file_name}",
                                "mime": "video/mp4",
                            },
                        }
                    ]
                }
            }

    class _Video:
        type = "Video"
        file = file_name
        mime_type = "video/mp4"

        async def convert_to_file_path(self):
            return str(source)

    class _Reply:
        type = "Reply"
        id = 123456
        chain = [_Video()]

    event = types.SimpleNamespace(
        bot=_Bot(),
        message_obj=types.SimpleNamespace(
            message=[
                _Reply(),
                {"type": "text", "data": {"text": raw_command}},
            ],
            message_str=raw_command,
        ),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("qzone post",)))

    assert ord(raw_command[0]) == 0xFF0C
    assert raw_command[0] != "?"
    assert post.content == "live video smoke"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == str(source)
    assert post.media[0].name == file_name
    assert post.media[0].trusted_local is True
    assert post.attachments == []

    captured: dict[str, object] = {}

    class _Controller:
        async def publish_post(self, **kwargs):
            captured.update(kwargs)
            return {"native_video": True, "status": "published_native_video"}

    async def _same_post(payload):
        return payload

    async def _render_preview(payload):
        return main.PostPayload(content=payload.content, media=[])

    async def _noop_bind(_event):
        captured["auto_bind_called"] = True

    plugin.settings = types.SimpleNamespace(native_video_publish=True)
    plugin.controller = _Controller()
    plugin._prepare_video_sources = _same_post
    plugin._prepare_publish_payload = _render_preview

    asyncio.run(plugin._publish_post_payload(post, event=event))

    assert captured["content"] == "live video smoke"
    assert captured["content_sanitized"] is True
    assert "auto_bind_called" not in captured
    assert [item["kind"] for item in captured["media"]] == ["video"]
    assert captured["media"][0]["source"] == str(source)


def test_plugin_collapses_quoted_video_when_onebot_lookup_uses_different_remote_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    source = tmp_path / "converted-from-reply.mp4"
    source.write_bytes(b"fake video bytes")
    raw_command = "\uff0cqzone post live video smoke"

    class _Bot:
        async def get_msg(self, *, message_id):
            assert message_id == 123456
            return {
                "data": {
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": "remote-cache-id.mp4",
                                "url": "https://example.test/video/remote-cache-id.mp4",
                                "mime": "video/mp4",
                            },
                        }
                    ]
                }
            }

    class _Video:
        type = "Video"
        file = "onebot-file-id"
        mime_type = "video/mp4"

        async def convert_to_file_path(self):
            return str(source)

    class _Reply:
        type = "Reply"
        id = 123456
        chain = [_Video()]

    event = types.SimpleNamespace(
        bot=_Bot(),
        message_obj=types.SimpleNamespace(
            message=[
                _Reply(),
                {"type": "text", "data": {"text": raw_command}},
            ],
            message_str=raw_command,
        ),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("qzone post",)))

    assert ord(raw_command[0]) == 0xFF0C
    assert post.content == "live video smoke"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == str(source)
    assert post.media[0].trusted_local is True
    assert post.attachments == []


def test_plugin_prefers_astrbot_video_converter_over_stale_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake video bytes")

    class _Video:
        type = "Video"
        file = "95d20307dfb960194a9210eff4824876.mp4"
        path = r"D:Documents\Tencent Files\3112333596\nt_qq\nt_data\Video\2026-06\OriV9\95d20307dfb960194a9210eff4824876.mp4"

        async def convert_to_file_path(self):
            return str(source)

    class _Reply:
        type = "Reply"
        id = 123456
        chain = [_Video()]

    event = types.SimpleNamespace(
        message_obj=types.SimpleNamespace(
            message=[
                _Reply(),
                {"type": "text", "data": {"text": "post hello"}},
            ]
        )
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert post.content == "hello"
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == str(source)
    assert post.media[0].trusted_local is True


def test_plugin_collects_astrbot_reply_video_component_without_extension(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    source = tmp_path / "videoseg_no_extension"
    source.write_bytes(b"fake video bytes")

    class _Video:
        type = "Video"
        file = str(source)
        mime_type = "video/mp4"

        async def convert_to_file_path(self):
            return str(source)

    class _Reply:
        type = "Reply"
        id = 123456
        chain = [_Video()]

    event = types.SimpleNamespace(
        message_obj=types.SimpleNamespace(
            message=[
                _Reply(),
                {"type": "text", "data": {"text": "post"}},
            ]
        )
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert post.content == ""
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == str(source)
    assert post.media[0].mime_type == "video/mp4"
    assert post.media[0].trusted_local is True


def test_plugin_resolves_astrbot_reply_video_component_via_onebot_file_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake video bytes")

    class _Bot:
        def __init__(self) -> None:
            self.get_file_params: list[dict[str, object]] = []

        async def get_msg(self, **_kwargs):
            raise RuntimeError("get_msg unavailable")

        async def get_file(self, **params):
            self.get_file_params.append(params)
            if params != {"file": "95d20307dfb960194a9210eff4824876.mp4"}:
                raise RuntimeError("unsupported get_file params")
            return {"data": {"file": str(source), "file_name": "clip.mp4", "file_size": source.stat().st_size}}

    class _Video:
        type = "Video"
        file = "95d20307dfb960194a9210eff4824876.mp4"
        mime_type = "video/mp4"

        async def convert_to_file_path(self):
            raise RuntimeError("not a local path")

    class _Reply:
        type = "Reply"
        id = 123456
        chain = [_Video()]

    bot = _Bot()
    event = types.SimpleNamespace(
        bot=bot,
        message_obj=types.SimpleNamespace(
            message=[
                _Reply(),
                {"type": "text", "data": {"text": "post"}},
            ]
        ),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert {"file": "95d20307dfb960194a9210eff4824876.mp4"} in bot.get_file_params
    assert post.content == ""
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == str(source)
    assert post.media[0].trusted_local is True


def test_plugin_resolves_quoted_video_file_identifier_with_get_video_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        def __init__(self) -> None:
            self.get_video_params: list[dict[str, object]] = []

        async def get_msg(self, *, message_id):
            assert message_id == 123456
            return {
                "data": {
                    "message": [
                        {
                            "type": "video",
                            "data": {
                                "file": "95d20307dfb960194a9210eff4824876.mp4",
                                "url": "empty",
                                "path": "empty",
                                "mime": "video/mp4",
                            },
                        }
                    ]
                }
            }

        async def get_file(self, **_params):
            raise RuntimeError("get_file unsupported for video")

        async def get_video(self, **params):
            self.get_video_params.append(params)
            if params != {"file": "95d20307dfb960194a9210eff4824876.mp4"}:
                raise RuntimeError("unsupported get_video params")
            return {
                "data": {
                    "download_url": "https://example.test/video/clip.mp4",
                    "file_name": "clip.mp4",
                    "file_size": 1234,
                }
            }

    bot = _Bot()
    event = types.SimpleNamespace(
        bot=bot,
        message_obj=types.SimpleNamespace(
            message=[
                {"type": "reply", "data": {"id": "123456"}},
                {"type": "text", "data": {"text": "post"}},
            ]
        ),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert {"file": "95d20307dfb960194a9210eff4824876.mp4"} in bot.get_video_params
    assert post.content == ""
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == "https://example.test/video/clip.mp4"


def test_plugin_resolves_raw_cq_video_file_stem_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake video bytes")

    class _Bot:
        def __init__(self) -> None:
            self.get_file_params: list[dict[str, object]] = []

        async def get_msg(self, *, message_id):
            assert message_id == 123456
            return {"data": {"raw_message": "[CQ:video,file=95d20307dfb960194a9210eff4824876.mp4]"}}

        async def get_file(self, **params):
            self.get_file_params.append(params)
            if params != {"file": "95d20307dfb960194a9210eff4824876"}:
                raise RuntimeError("unsupported get_file params")
            return {"data": {"file": str(source), "file_name": "clip.mp4", "file_size": source.stat().st_size}}

    bot = _Bot()
    event = types.SimpleNamespace(
        bot=bot,
        message_obj=types.SimpleNamespace(raw_message="[CQ:reply,id=123456]post"),
    )
    plugin = object.__new__(main.QzoneStablePlugin)

    post = asyncio.run(plugin._collect_target_post_payload(event, "", ("post",)))

    assert {"file": "95d20307dfb960194a9210eff4824876"} in bot.get_file_params
    assert post.content == ""
    assert len(post.media) == 1
    assert post.media[0].kind == "video"
    assert post.media[0].source == str(source)
    assert post.media[0].trusted_local is True


def test_materialize_video_sources_accepts_trusted_no_extension_video(tmp_path: Path) -> None:
    from qzone_bridge.media import PostMedia, PostPayload
    from qzone_bridge.video import materialize_video_sources

    source = tmp_path / "videoseg_no_extension"
    source.write_bytes(b"fake video bytes")
    post = PostPayload(
        content="",
        media=[
            PostMedia(
                kind="video",
                source=str(source),
                name="clip",
                mime_type="video/mp4",
                raw_type="video",
                trusted_local=True,
            )
        ],
    )

    prepared = materialize_video_sources(post, tmp_path / "sources")

    assert prepared.media[0].kind == "video"
    assert prepared.media[0].source == str(source)
    assert prepared.media[0].trusted_local is True

def test_plugin_publish_sends_native_video_to_daemon_without_client_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake video bytes")
    original = main.PostPayload(
        content="hello",
        media=[
            main.PostMedia(
                kind="video",
                source=str(source),
                name="clip.mp4",
                mime_type="video/mp4",
                trusted_local=True,
            )
        ],
    )
    cover = main.PostPayload(
        content="hello",
        media=[main.PostMedia(kind="image", source=str(tmp_path / "cover.jpg"), raw_type="video", trusted_local=True)],
    )
    captured: dict[str, object] = {}

    async def fake_prepare(post):
        assert post is original
        return cover

    class _Controller:
        async def publish_post(self, **kwargs):
            captured["publish_kwargs"] = kwargs
            return {"vid": "vid-1", "native_video": True, "status": "submitted_native_upload"}

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(native_video_publish=True)
    plugin.data_dir = tmp_path
    plugin.controller = _Controller()
    plugin._prepare_publish_payload = fake_prepare

    render_post, payload = asyncio.run(plugin._publish_post_payload(original, event="evt"))

    assert not hasattr(main, "publish_native_video_post")
    assert render_post is cover
    assert payload["vid"] == "vid-1"
    assert captured["publish_kwargs"] == {
        "content": "hello",
        "sync_weibo": False,
        "media": [original.media[0].to_dict()],
        "content_sanitized": True,
    }


def test_plugin_publish_uses_daemon_h5_even_when_onebot_protocol_native_video_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake video bytes")
    original = main.PostPayload(
        content="hello",
        media=[
            main.PostMedia(
                kind="video",
                source=str(source),
                name="clip.mp4",
                mime_type="video/mp4",
                trusted_local=True,
            )
        ],
    )
    cover = main.PostPayload(
        content="hello",
        media=[main.PostMedia(kind="image", source=str(tmp_path / "cover.jpg"), raw_type="video", trusted_local=True)],
    )
    captured: dict[str, object] = {"onebot_called": False, "verify_called": False}
    bot = object()

    async def fake_prepare(post):
        assert post is original
        return cover

    async def fake_onebot_publish(client, **kwargs):
        captured["onebot_called"] = True
        captured["onebot_client"] = client
        captured["onebot_kwargs"] = kwargs
        raise AssertionError("daemon H5 video publish must run before any OneBot protocol-end publish action")

    class _Controller:
        async def verify_native_video_feed(self, **kwargs):
            captured["verify_called"] = True
            captured["verify_kwargs"] = kwargs
            raise AssertionError("OneBot verification should not run when daemon H5 publish succeeds")

        async def publish_post(self, **kwargs):
            captured["publish_kwargs"] = kwargs
            return {
                "native_video": True,
                "status": "published_native_video",
                "operation_status": "verified_feed_video_public_after_permission_update",
                "vid": "vid-h5",
                "fid": "fid-h5",
                "raw": {"method": "h5_video_publish_update_visibility"},
            }

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(native_video_publish=True)
    plugin.data_dir = tmp_path
    plugin.controller = _Controller()
    plugin._prepare_publish_payload = fake_prepare
    plugin._capture_onebot_client = lambda event=None: bot
    assert not hasattr(main, "publish_qzone_video_via_onebot")

    render_post, payload = asyncio.run(plugin._publish_post_payload(original, event=types.SimpleNamespace()))

    assert render_post is cover
    assert payload["vid"] == "vid-h5"
    assert payload["fid"] == "fid-h5"
    assert payload["operation_status"] == "verified_feed_video_public_after_permission_update"
    assert payload["raw"]["method"] == "h5_video_publish_update_visibility"
    assert captured["onebot_called"] is False
    assert captured["verify_called"] is False
    assert captured["publish_kwargs"] == {
        "content": "hello",
        "sync_weibo": False,
        "media": [original.media[0].to_dict()],
        "content_sanitized": True,
    }


def test_plugin_publish_prefers_ready_daemon_h5_over_broken_onebot_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake video bytes")
    original = main.PostPayload(
        content="hello",
        media=[
            main.PostMedia(
                kind="video",
                source=str(source),
                name="clip.mp4",
                mime_type="video/mp4",
                trusted_local=True,
            )
        ],
    )
    cover = main.PostPayload(
        content="hello",
        media=[main.PostMedia(kind="image", source=str(tmp_path / "cover.jpg"), raw_type="video", trusted_local=True)],
    )
    captured: dict[str, object] = {"onebot_called": False}

    async def fake_prepare(post):
        return cover

    class _Controller:
        async def get_status(self, **kwargs):
            captured["status_kwargs"] = kwargs
            return {
                "video_upload": {
                    "ready": True,
                    "configured": False,
                    "qq_upload_configured": False,
                    "web_cookie_configured": True,
                    "h5_upload_available": True,
                    "h5_publish_supported": True,
                    "method": "h5_video_publish_update_visibility",
                }
            }

        async def publish_post(self, **kwargs):
            captured["publish_kwargs"] = kwargs
            return {
                "vid": "vid-h5",
                "fid": "fid-h5",
                "native_video": True,
                "operation_status": "verified_feed_video_public_after_permission_update",
                "raw": {"method": "h5_video_publish_update_visibility"},
            }

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(native_video_publish=True)
    plugin.data_dir = tmp_path
    plugin.controller = _Controller()
    plugin._prepare_publish_payload = fake_prepare
    plugin._capture_onebot_client = lambda event=None: object()
    assert not hasattr(main, "publish_qzone_video_via_onebot")

    render_post, payload = asyncio.run(plugin._publish_post_payload(original, event=types.SimpleNamespace()))

    assert render_post is cover
    assert payload["operation_status"] == "verified_feed_video_public_after_permission_update"
    assert payload["raw"]["method"] == "h5_video_publish_update_visibility"
    assert captured["onebot_called"] is False
    assert captured["publish_kwargs"] == {
        "content": "hello",
        "sync_weibo": False,
        "media": [original.media[0].to_dict()],
        "content_sanitized": True,
    }


def test_plugin_publish_routes_mixed_video_media_to_daemon_instead_of_cover_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    video_source = tmp_path / "clip.mp4"
    image_source = tmp_path / "photo.jpg"
    video_source.write_bytes(b"fake video bytes")
    image_source.write_bytes(b"fake image bytes")
    video = main.PostMedia(
        kind="video",
        source=str(video_source),
        name="clip.mp4",
        mime_type="video/mp4",
        trusted_local=True,
    )
    image = main.PostMedia(
        kind="image",
        source=str(image_source),
        name="photo.jpg",
        mime_type="image/jpeg",
        trusted_local=True,
    )
    original = main.PostPayload(content="hello", media=[video, image])
    rendered_cover = main.PostPayload(
        content="hello",
        media=[main.PostMedia(kind="image", source=str(tmp_path / "rendered.jpg"), raw_type="video", trusted_local=True)],
    )
    captured: dict[str, object] = {}

    async def fake_prepare(post):
        assert post is original
        return rendered_cover

    class _Controller:
        async def publish_post(self, **kwargs):
            captured["publish_kwargs"] = kwargs
            return {"ok": False, "message": "unsupported mix rejected by daemon"}

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(native_video_publish=True)
    plugin.data_dir = tmp_path
    plugin.controller = _Controller()
    plugin._prepare_publish_payload = fake_prepare

    render_post, payload = asyncio.run(plugin._publish_post_payload(original, event="evt"))

    assert render_post is rendered_cover
    assert payload["message"] == "unsupported mix rejected by daemon"
    assert captured["publish_kwargs"] == {
        "content": "hello",
        "sync_weibo": False,
        "media": [video.to_dict(), image.to_dict()],
        "content_sanitized": True,
    }


def test_plugin_does_not_expose_a2_video_upload_auto_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)

    assert not hasattr(main, "probe_video_upload_credentials")
    assert not hasattr(main.QzoneStablePlugin, "_auto_bind_video_upload_credentials")
    assert not hasattr(main.QzoneStablePlugin, "_maybe_bind_video_upload_credentials")


def test_videoauth_command_reports_h5_only_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Controller:
        async def bind_video_upload_credentials_local(self, **_kwargs):
            captured["a2_bind_called"] = True
            raise AssertionError("/qzone videoauth must not bind QQ upload A2/vLoginData")

    class _Event:
        def is_admin(self):
            return True

        def plain_result(self, text):
            return text

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(admin_uins=set())
    plugin.controller = _Controller()

    async def collect_results():
        return [item async for item in plugin.qzone_videoauth(_Event(), "bG9naW4=")]

    results = asyncio.run(collect_results())

    assert len(results) == 1
    assert "H5 chain" in results[0]
    assert "A2/vLoginData fallback is disabled" in results[0]
    assert captured == {}


def test_autovideoauth_binds_web_cookie_without_a2_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    class _Bot:
        async def call_action(self, action, **params):
            return {}

    bot = _Bot()
    captured: dict[str, object] = {"status_calls": 0, "cookie_bound": False}

    ready_status = {
        "daemon_state": "ready",
        "login_uin": 12345,
        "cookie_count": 4,
        "needs_rebind": False,
        "video_upload": {
            "configured": False,
            "method": "",
            "ready": False,
            "verification_required": False,
            "qq_upload_configured": False,
            "h5_upload_available": True,
            "h5_publish_supported": False,
            "h5_publish_verification_required": False,
            "web_cookie_configured": True,
            "requires": "qzone_web_cookie_p_skey",
        },
    }

    async def fake_fetch_cookie_text(client, *, domain):
        captured["cookie_client"] = client
        captured["cookie_domain"] = domain
        return "uin=o12345; p_uin=o12345; p_skey=ps-key; skey=s-key"

    class _Controller:
        async def get_status(self, **kwargs):
            captured["status_calls"] += 1
            captured["last_status_kwargs"] = kwargs
            if not captured["cookie_bound"]:
                return {
                    "daemon_state": "needs_rebind",
                    "cookie_count": 0,
                    "needs_rebind": True,
                    "video_upload": {"configured": False},
                }
            return dict(ready_status)

        async def bind_cookie_local(self, cookie_text, *, uin=0, source="manual"):
            captured["bound_cookie_text"] = cookie_text
            captured["bound_uin"] = uin
            captured["bound_source"] = source
            captured["cookie_bound"] = True
            return dict(ready_status)

    class _Event:
        def is_admin(self):
            return True

        def plain_result(self, text):
            return text

        def stop_event(self):
            captured["stopped"] = True

    event = _Event()
    event.bot = bot

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        admin_uins=set(),
        auto_bind_cookie=False,
        cookie_domain="user.qzone.qq.com",
    )
    plugin.controller = _Controller()
    plugin._onebot_client = None
    plugin._context = None
    plugin._cookie_lock = None
    plugin._video_upload_lock = None
    plugin._schedule_publish_render_asset_preload = lambda *args, **kwargs: None

    async def fake_status_with_recovery():
        return await plugin.controller.get_status()

    plugin._status_with_recovery = fake_status_with_recovery
    monkeypatch.setattr(main, "fetch_cookie_text", fake_fetch_cookie_text)

    async def collect_results():
        return [item async for item in plugin.qzone_autovideoauth(event)]

    results = asyncio.run(collect_results())

    assert len(results) == 1
    assert "- 账号：12345（已绑定，4 个 Cookie）" in results[0]
    assert "- 视频直发：不可用（仅上传诊断可用）" in results[0]
    assert "h5_video_publish_supported" not in results[0]
    assert captured["cookie_client"] is bot
    assert captured["bound_uin"] == 12345
    assert captured["bound_source"] == "onebot"


def test_autovideoauth_accepts_h5_publish_ready_without_a2_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {"status_calls": 0}

    ready_status = {
        "daemon_state": "ready",
        "login_uin": 12345,
        "cookie_count": 4,
        "needs_rebind": False,
        "video_upload": {
            "configured": False,
            "method": "h5_video_publish_update_visibility",
            "ready": True,
            "verification_required": True,
            "qq_upload_configured": False,
            "h5_upload_available": True,
            "h5_publish_supported": True,
            "h5_publish_permission_update_required": True,
            "h5_publish_verification_required": True,
            "web_cookie_configured": True,
        },
    }

    class _Controller:
        async def get_status(self, **kwargs):
            captured["status_calls"] += 1
            captured["last_status_kwargs"] = kwargs
            return dict(ready_status)

    class _Event:
        def is_admin(self):
            return True

        def plain_result(self, text):
            return text

        def stop_event(self):
            captured["stopped"] = True

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(admin_uins=set(), auto_bind_cookie=False)
    plugin.controller = _Controller()
    plugin._onebot_client = object()
    plugin._context = None
    plugin._cookie_lock = None
    plugin._video_upload_lock = None
    plugin._schedule_publish_render_asset_preload = lambda *args, **kwargs: None

    async def fake_status_with_recovery():
        return await plugin.controller.get_status()

    plugin._status_with_recovery = fake_status_with_recovery

    async def collect_results():
        return [item async for item in plugin.qzone_autovideoauth(_Event())]

    results = asyncio.run(collect_results())

    assert len(results) == 1
    assert "- 账号：12345（已绑定，4 个 Cookie）" in results[0]
    assert "- 视频直发：可用（公开视频校验）" in results[0]
    assert "video_upload_method" not in results[0]
    assert "h5_video_publish_supported" not in results[0]


def test_status_renderer_summarizes_cookie_h5_path_without_raw_internal_fields() -> None:
    from qzone_bridge.render import format_status

    rendered = format_status(
        {
            "daemon_state": "ready",
            "login_uin": 12345,
            "cookie_count": 4,
            "needs_rebind": False,
            "video_upload": {
                "configured": False,
                "qq_upload_configured": False,
                "web_cookie_configured": True,
                "h5_upload_available": True,
                "h5_publish_supported": False,
                "h5_publish_experimental": False,
                "h5_publish_verification_required": False,
                "ready": False,
                "verification_required": False,
                "method": "",
                "requires": "qq_upload_a2_vlogin_data",
            },
        }
    )

    assert rendered.splitlines() == [
        "QQ 空间状态",
        "- 服务：正常",
        "- 账号：12345（已绑定，4 个 Cookie）",
        "- 视频直发：不可用（仅上传诊断可用）",
    ]
    assert "qq_upload_configured" not in rendered
    assert "web_cookie_configured" not in rendered
    assert "video_upload_verification_required" not in rendered
    assert "h5_video_publish_supported" not in rendered
    assert "video_upload_method" not in rendered


def test_onebot_video_upload_credentials_ignore_web_cookie_tokens() -> None:
    from qzone_bridge.onebot_upload import extract_video_upload_credentials

    payload = {
        "data": {
            "clientKey": "client-key-from-pmhq",
            "p_skey": "web-p-skey",
            "qzonetoken": "web-qzonetoken",
            "cookie": "uin=o12345; p_skey=web-p-skey;",
        }
    }

    assert extract_video_upload_credentials(payload) is None


def test_onebot_video_upload_credentials_ignore_binary_clientkey_tokens() -> None:
    from qzone_bridge.onebot_upload import extract_video_upload_credentials

    payload = {"data": {"clientKey": b"binary-clientkey-not-a2", "keyIndex": "19"}}

    assert extract_video_upload_credentials(payload) is None


def test_onebot_video_upload_probe_reports_cookie_only_credentials() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    class _Bot:
        async def call_action(self, action: str, **_params):
            if action == "get_credentials":
                return {
                    "data": {
                        "cookies": "uin=o12345; skey=web-skey; p_skey=web-pskey;",
                        "csrf_token": 123456,
                    }
                }
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is None
    assert "get_credentials" in probe.returned_actions
    assert "get_credentials" in probe.web_credential_actions


def test_onebot_video_upload_probe_reports_clientkey_only_without_binding() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    class _Bot:
        async def call_action(self, action: str, **params):
            if action == "get_clientkey":
                return {"data": {"clientkey": "web-jump-client-key", "keyIndex": "19"}}
            if action == "llonebot_debug" and params.get("method") == "forceFetchClientKey":
                return {"clientKey": "web-jump-client-key", "keyIndex": "19"}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is None
    assert "get_clientkey" in probe.returned_actions
    assert "get_clientkey" in probe.client_key_actions
    assert "llonebot_debug" in probe.client_key_actions


def test_onebot_video_upload_probe_accepts_llonebot_debug_raw_a2_material() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"binary-a2-login-data"

    class _Bot:
        async def call_action(self, action: str, **params):
            if action == "llonebot_debug" and params.get("method") == "getA2Bytes":
                return {"type": "Buffer", "data": list(raw_a2)}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source == "test:llonebot_debug"


def test_onebot_video_upload_probe_accepts_llonebot_login_misc_a2_material() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"binary-a2-from-login-misc"
    raw_a2_hex = raw_a2.hex()

    class _Bot:
        async def call_action(self, action: str, **params):
            if (
                action == "llonebot_debug"
                and params.get("apiClass") == "pmhq"
                and params.get("method") == "invoke"
                and params.get("args") == ["nodeIKernelLoginService/getLoginMiscData", ["a2"]]
            ):
                return {"result": 0, "value": raw_a2_hex}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source == "test:llonebot_debug"


def test_onebot_video_upload_probe_accepts_llonebot_pmhq_call_login_misc() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"binary-a2-from-pmhq-call"

    class _Bot:
        async def call_action(self, action: str, **params):
            if (
                action == "llonebot_debug"
                and params.get("apiClass") == "pmhq"
                and params.get("method") == "call"
                and params.get("args") == ["loginService.getLoginMiscData", ["a2"]]
            ):
                return {"result": 0, "value": {"type": "Buffer", "data": list(raw_a2)}}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source == "test:llonebot_debug"


def test_onebot_video_upload_probe_accepts_llonebot_pmhq_call_nested_result_value() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"binary-a2-from-nested-pmhq-call"

    class _Bot:
        async def call_action(self, action: str, **params):
            if (
                action == "llonebot_debug"
                and params.get("apiClass") == "pmhq"
                and params.get("method") == "call"
                and params.get("args") == ["wrapperSession.getLoginService().getLoginMiscData", ["a2"]]
            ):
                return {"type": "call", "data": {"result": {"value": raw_a2.hex()}}}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source == "test:llonebot_debug"


def test_onebot_video_upload_probe_rejects_llonebot_pmhq_error_string_result() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    class _Bot:
        async def call_action(self, action: str, **params):
            if (
                action == "llonebot_debug"
                and params.get("apiClass") == "pmhq"
                and params.get("method") == "call"
                and params.get("args") == ["wrapperSession.getLoginService().getLoginMiscData", ["a2"]]
            ):
                return {
                    "type": "call",
                    "data": {"result": "Error: wrapperSession.getLoginService().getLoginMiscData is not available"},
                }
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is None
    assert "llonebot_debug" in probe.returned_actions


def test_onebot_video_upload_probe_accepts_generic_onebot_login_misc_a2_material() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"binary-a2-from-generic-onebot"

    class _Bot:
        async def call_action(self, action: str, **params):
            if action == "get_login_misc_data" and params == {"key": "a2"}:
                return {"result": 0, "value": raw_a2.hex()}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source == "test:get_login_misc_data"
    assert "get_login_misc_data:key=a2" in probe.attempted_actions


def test_onebot_video_upload_probe_accepts_underscored_protocol_extension() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"binary-a2-from-underscored-onebot-extension"

    class _Bot:
        async def call_action(self, action: str, **params):
            if action == "_get_login_misc_data" and params == {"key": "a2"}:
                return {"status": "ok", "retcode": 0, "data": {"value": raw_a2.hex()}}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source == "test:_get_login_misc_data"
    assert "_get_login_misc_data:key=a2" in probe.attempted_actions


def test_onebot_video_upload_probe_accepts_generic_onebot_a2_action_buffer() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"binary-a2-from-generic-action"

    class _Bot:
        async def call_action(self, action: str, **_params):
            if action == "get_qzone_video_upload_a2":
                return {"result": 0, "value": {"type": "Buffer", "data": list(raw_a2)}}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source == "test:get_qzone_video_upload_a2"


def test_onebot_video_upload_probe_accepts_generic_onebot_a2_ticket_action() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"binary-a2-ticket-from-generic-action"

    class _Bot:
        async def call_action(self, action: str, **_params):
            if action == "get_a2_ticket":
                return {"result": 0, "value": {"type": "Buffer", "data": list(raw_a2)}}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source == "test:get_a2_ticket"


@pytest.mark.parametrize(
    "alias",
    [
        "_get_a2_ticket",
        "_get_ntqq_a2_ticket",
        "_get_qzone_video_upload_a2_ticket",
        "get_ntqq_a2_ticket",
        "get_nt_a2_ticket",
        "get_qzone_video_upload_a2_ticket",
        "get_qzone_upload_a2_ticket",
        "get_video_upload_a2_ticket",
        "get_qq_upload_a2_ticket",
    ],
)
def test_onebot_video_upload_probe_accepts_a2_ticket_action_aliases(alias: str) -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = f"binary-a2-ticket-from-{alias}".encode("ascii")

    class _Bot:
        async def call_action(self, action: str, **_params):
            if action == alias:
                return {"result": 0, "value": {"type": "Buffer", "data": list(raw_a2)}}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source == f"test:{alias}"


def test_onebot_video_upload_probe_accepts_llonebot_pmhq_get_a2_ticket() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"binary-a2-ticket-from-pmhq"

    class _Bot:
        async def call_action(self, action: str, **params):
            if (
                action == "llonebot_debug"
                and params.get("apiClass") == "pmhq"
                and params.get("method") == "call"
                and params.get("args") == ["wrapperSession.getTicketService().getA2Ticket", []]
            ):
                return {"result": 0, "value": raw_a2.hex()}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source == "test:llonebot_debug"


@pytest.mark.parametrize(
    "value",
    [
        "Error: wrapperSession.getTicketService().getA2Ticket failed",
        base64.b64encode(b"Error: wrapperSession.getTicketService().getA2Ticket failed").decode("ascii"),
    ],
)
def test_onebot_video_upload_probe_rejects_pmhq_a2_ticket_error_value(value: str) -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    class _Bot:
        async def call_action(self, action: str, **params):
            if (
                action == "llonebot_debug"
                and params.get("apiClass") == "pmhq"
                and params.get("method") == "call"
                and params.get("args") == ["wrapperSession.getTicketService().getA2Ticket", []]
            ):
                return {"result": 0, "value": value}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is None
    assert "llonebot_debug" in probe.returned_actions


def test_onebot_video_upload_probe_rejects_buffer_wrapped_pmhq_error_value() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    error_text = b"Error: wrapperSession.getTicketService().getA2Ticket is not a function"

    class _Bot:
        async def call_action(self, action: str, **params):
            if (
                action == "llonebot_debug"
                and params.get("apiClass") == "pmhq"
                and params.get("method") == "call"
                and params.get("args") == ["wrapperSession.getTicketService().getA2Ticket", []]
            ):
                return {"result": 0, "value": {"type": "Buffer", "data": list(error_text)}}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is None
    assert "llonebot_debug" in probe.returned_actions


def test_onebot_video_upload_probe_rejects_failed_status_even_with_value() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"binary-a2-from-failed-status"

    class _Bot:
        async def call_action(self, action: str, **params):
            if action == "get_login_misc_data" and params == {"key": "a2"}:
                return {"result": -1, "errMsg": "login misc unavailable", "value": raw_a2.hex()}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is None
    assert "get_login_misc_data" in probe.returned_actions


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("invoke", ["nodeIKernelTicketService/getA2Ticket", []]),
        ("call", ["wrapperSession.getTicketService().GetA2Ticket", []]),
    ],
)
def test_onebot_video_upload_probe_accepts_llonebot_pmhq_a2_ticket_variants(method: str, args: list[object]) -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = f"binary-a2-ticket-from-pmhq-{method}".encode("ascii")

    class _Bot:
        async def call_action(self, action: str, **params):
            if (
                action == "llonebot_debug"
                and params.get("apiClass") == "pmhq"
                and params.get("method") == method
                and params.get("args") == args
            ):
                return {"result": 0, "value": raw_a2.hex()}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source == "test:llonebot_debug"


@pytest.mark.parametrize("key", ["a2Ticket", "a2_ticket"])
def test_onebot_video_upload_credentials_accept_a2_ticket_aliases(key: str) -> None:
    from qzone_bridge.onebot_upload import extract_video_upload_credentials

    credentials = extract_video_upload_credentials({"data": {key: b"binary-a2-ticket-alias".hex()}})

    assert credentials is not None
    assert credentials.login_data_b64 == base64.b64encode(b"binary-a2-ticket-alias").decode("ascii")


def test_onebot_video_upload_probe_accepts_embedded_ntqq_login_misc_service() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"binary-a2-from-embedded-napcat-service"

    class _LoginService:
        async def getLoginMiscData(self, key: str):
            if key == "a2":
                return {"result": 0, "value": raw_a2.hex()}
            raise RuntimeError("unsupported key")

    class _Session:
        def getLoginService(self):
            return _LoginService()

    class _Context:
        session = _Session()

    class _Bot:
        context = _Context()

        async def call_action(self, *_args, **_params):
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source.startswith("test:embedded:")
    assert any("getLoginService().getLoginMiscData:key=a2" in item for item in probe.returned_actions)


def test_onebot_video_upload_probe_accepts_embedded_ntqq_ticket_service() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"binary-a2-from-embedded-ticket-service"

    class _TicketService:
        async def getA2Ticket(self):
            return {"result": 0, "value": raw_a2.hex()}

    class _Session:
        def getTicketService(self):
            return _TicketService()

    class _Context:
        session = _Session()

    class _Bot:
        context = _Context()

        async def call_action(self, *_args, **_params):
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source.startswith("test:embedded:")
    assert any("getTicketService().getA2Ticket" in item for item in probe.returned_actions)


def test_onebot_video_upload_probe_ignores_async_http_get_as_pmhq_getter(recwarn: pytest.WarningsRecorder) -> None:
    from qzone_bridge.onebot_upload import _pmhq_candidates

    class _HttpClientLike:
        async def get(self, _url: str):
            return {"not": "pmhq"}

    candidates = _pmhq_candidates("owner", _HttpClientLike())

    assert candidates == []
    assert not [warning for warning in recwarn if issubclass(warning.category, RuntimeWarning)]


def test_onebot_video_upload_probe_accepts_embedded_napcat_wrapper_login_service() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"binary-a2-from-napcat-wrapper-service"

    class _LoginService:
        async def getLoginMiscData(self, key: str):
            if key == "a2":
                return {"result": 0, "value": raw_a2.hex()}
            raise RuntimeError("unsupported key")

    class _LoginServiceWrapper:
        def get(self):
            return _LoginService()

    class _Wrapper:
        NodeIKernelLoginService = _LoginServiceWrapper()

    class _NapCatCore:
        wrapper = _Wrapper()

    class _Bot:
        napcat = _NapCatCore()

        async def call_action(self, *_args, **_params):
            raise RuntimeError("default NapCat OneBot actions do not expose A2")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source.startswith("test:embedded:bot.napcat.wrapper.NodeIKernelLoginService")


def test_onebot_video_upload_probe_records_llbot_empty_login_misc_response() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    class _Bot:
        async def call_action(self, action: str, **params):
            if (
                action == "llonebot_debug"
                and params.get("apiClass") == "pmhq"
                and params.get("method") == "call"
                and params.get("args") == ["loginService.getLoginMiscData", ["a2"]]
            ):
                return {
                    "type": "call",
                    "data": {
                        "result": {
                            "result": -1,
                            "errMsg": "GetMiscData Fail, DbActionId::result = nullptr",
                            "value": "",
                        }
                    },
                }
            if action == "get_clientkey":
                return {"data": {"clientkey": "web-jump-client-key", "keyIndex": "19"}}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is None
    assert "llonebot_debug:apiClass=pmhq,args=[loginService.getLoginMiscData,[a2]],method=call" in (
        probe.empty_login_data_actions
    )
    assert "get_clientkey" in probe.client_key_actions


def test_onebot_video_upload_probe_rejects_default_napcat_web_credentials_and_clientkey() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    class _Bot:
        async def call_action(self, action: str, **_params):
            if action == "get_credentials":
                return {"data": {"cookies": "uin=o12345; p_skey=ps-key;", "token": 123456}}
            if action == "get_cookies":
                return {"data": {"cookies": "uin=o12345; p_skey=ps-key;", "bkn": "123456"}}
            if action == "get_clientkey":
                return {"data": {"clientkey": "web-jump-client-key", "keyIndex": "19"}}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is None
    assert "get_credentials" in probe.web_credential_actions
    assert "get_cookies" in probe.web_credential_actions
    assert "get_clientkey" in probe.client_key_actions


def test_onebot_video_upload_probe_accepts_targeted_login_misc_even_with_clientkey_metadata() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"binary-a2-with-clientkey-metadata"

    class _Bot:
        async def call_action(self, action: str, **params):
            if action == "get_login_misc_data" and params == {"key": "a2"}:
                return {
                    "result": 0,
                    "value": raw_a2.hex(),
                    "clientKey": "web-clientkey-bookkeeping",
                    "keyIndex": "19",
                }
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source == "test:get_login_misc_data"


def test_onebot_video_upload_probe_accepts_targeted_login_misc_raw_binary_string() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_text = "\x01\x02binary-a2-from-js-string-\xff"
    expected = bytes(ord(ch) for ch in raw_text)

    class _Bot:
        async def call_action(self, action: str, **params):
            if action == "get_login_misc_data" and params == {"key": "a2"}:
                return {"result": 0, "value": raw_text}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(expected).decode("ascii")
    assert probe.credentials.source == "test:get_login_misc_data"


def test_onebot_video_upload_credentials_accept_hex_alias_fields() -> None:
    from qzone_bridge.onebot_upload import extract_video_upload_credentials

    payload = {
        "data": {
            "a2_hex": b"binary-a2-alias".hex(),
            "vLoginKeyHex": b"binary-key-alias".hex(),
        }
    }

    credentials = extract_video_upload_credentials(payload, source="test")

    assert credentials is not None
    assert credentials.login_data_b64 == base64.b64encode(b"binary-a2-alias").decode("ascii")
    assert credentials.login_key_b64 == base64.b64encode(b"binary-key-alias").decode("ascii")


def test_onebot_video_upload_probe_does_not_accept_login_misc_clientkey_as_a2() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    class _Bot:
        async def call_action(self, action: str, **params):
            if (
                action == "llonebot_debug"
                and params.get("apiClass") == "pmhq"
                and params.get("method") == "invoke"
                and params.get("args") == ["nodeIKernelLoginService/getLoginMiscData", ["a2"]]
            ):
                return {"result": 1, "value": ""}
            if action == "get_clientkey":
                return {"result": 0, "value": "web-clientkey-not-a2", "keyIndex": "19"}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is None
    assert "get_clientkey" in probe.client_key_actions


def test_onebot_video_upload_probe_does_not_accept_hex_clientkey_value_as_a2() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    hex_like_clientkey = "00112233445566778899aabbccddeeff"

    class _Bot:
        async def call_action(self, action: str, **_params):
            if action == "get_qzone_video_upload_credentials":
                return {"result": 0, "value": hex_like_clientkey, "keyIndex": "19"}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is None
    assert "get_qzone_video_upload_credentials" in probe.client_key_actions


def test_onebot_video_upload_probe_does_not_treat_file_trans_sig_as_a2() -> None:
    from qzone_bridge.onebot_upload import _action_may_return_raw_login_data, _action_targets_login_data

    params = {"apiClass": "pmhq", "method": "invoke", "args": ["nodeIKernelTicketService/forceFetchFileTransSig", []]}

    assert _action_may_return_raw_login_data("llonebot_debug", params) is False
    assert _action_targets_login_data("llonebot_debug", params) is False


@pytest.mark.parametrize(
    ("method", "args", "payload"),
    [
        ("invoke", ["nodeIKernelTicketService/forceFetchFileTransSig", []], {"value": b"file-trans-sig".hex()}),
        ("invoke", ["nodeIKernelTicketService/getA2Ticket", []], {"value": b"not-a2".hex(), "ForceFetchFileTransSig": "present"}),
        ("call", ["wrapperSession.getTicketService().getA2Ticket", []], {"fileTransSig": {"type": "Buffer", "data": list(b"sig")}, "value": b"not-a2".hex()}),
    ],
)
def test_onebot_video_upload_probe_rejects_file_trans_sig_end_to_end(
    method: str,
    args: list[object],
    payload: dict[str, object],
) -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    class _Bot:
        async def call_action(self, action: str, **params):
            if (
                action == "llonebot_debug"
                and params.get("apiClass") == "pmhq"
                and params.get("method") == method
                and params.get("args") == args
            ):
                return payload
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is None


@pytest.mark.parametrize("metadata_key", ["PSKey", "Cookie", "ForceFetchFileTransSig"])
def test_onebot_video_upload_probe_rejects_non_a2_ticket_metadata_as_raw_a2(metadata_key: str) -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    hex_like_non_a2 = "00112233445566778899aabbccddeeff"

    class _Bot:
        async def call_action(self, action: str, **_params):
            if action == "get_qzone_video_upload_credentials":
                return {"result": 0, "value": hex_like_non_a2, metadata_key: "not-upload-a2"}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is None
    assert "get_qzone_video_upload_credentials" in probe.returned_actions


def test_onebot_video_upload_probe_rejects_force_fetch_client_key_buffer_as_a2() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    class _Bot:
        async def call_action(self, action: str, **params):
            if action == "llonebot_debug" and params.get("method") == "forceFetchClientKey":
                return {"type": "Buffer", "data": list(b"client-key-buffer")}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is None
    assert "llonebot_debug" in probe.client_key_actions


def test_onebot_video_upload_credentials_accept_binary_a2_material() -> None:
    from qzone_bridge.onebot_upload import extract_video_upload_credentials

    payload = {
        "data": {
            "a2_b64": base64.b64encode(b"binary-a2-login-data").decode("ascii"),
            "vLoginKey": base64.b64encode(b"binary-login-key").decode("ascii"),
            "token_type": 2,
        }
    }

    credentials = extract_video_upload_credentials(payload, source="test")

    assert credentials is not None
    assert credentials.login_data_b64 == base64.b64encode(b"binary-a2-login-data").decode("ascii")
    assert credentials.login_key_b64 == base64.b64encode(b"binary-login-key").decode("ascii")
    assert credentials.source == "test"


def test_onebot_video_upload_credentials_accept_node_buffer_login_data() -> None:
    from qzone_bridge.onebot_upload import extract_video_upload_credentials

    payload = {
        "data": {
            "vLoginData": {"type": "Buffer", "data": list(b"node-buffer-a2")},
            "vLoginKey": {"0": 110, "1": 111, "2": 100, "3": 101, "4": 45, "5": 107, "6": 101, "7": 121},
        }
    }

    credentials = extract_video_upload_credentials(payload, source="test")

    assert credentials is not None
    assert credentials.login_data_b64 == base64.b64encode(b"node-buffer-a2").decode("ascii")
    assert credentials.login_key_b64 == base64.b64encode(b"node-key").decode("ascii")


def test_onebot_call_action_supports_nested_api() -> None:
    from qzone_bridge.onebot_cookie import call_onebot_action

    calls: list[tuple[str, dict[str, object]]] = []

    class _Api:
        async def call_action(self, action: str, **params):
            calls.append((action, params))
            return {"ok": True}

    result = asyncio.run(call_onebot_action(types.SimpleNamespace(api=_Api()), "get_msg", id="123456"))

    assert result == {"ok": True}
    assert calls == [("get_msg", {"id": "123456"})]


def test_onebot_call_action_supports_protocol_client_positional_params() -> None:
    from qzone_bridge.onebot_cookie import call_onebot_action

    calls: list[tuple[str, dict[str, object]]] = []

    class _Bot:
        async def call_action(self, action: str, params: dict[str, object]):
            calls.append((action, dict(params)))
            return {"ok": True, "params": params}

    result = asyncio.run(call_onebot_action(_Bot(), "get_msg", message_id=123456))

    assert result == {"ok": True, "params": {"message_id": 123456}}
    assert calls == [("get_msg", {"message_id": 123456})]


def test_onebot_call_action_supports_call_api_alias() -> None:
    from qzone_bridge.onebot_cookie import call_onebot_action

    calls: list[tuple[str, dict[str, object]]] = []

    class _Bot:
        async def call_api(self, action: str, params: dict[str, object]):
            calls.append((action, dict(params)))
            return {"ok": True, "params": params}

    result = asyncio.run(call_onebot_action(_Bot(), "get_msg", message_id=123456))

    assert result == {"ok": True, "params": {"message_id": 123456}}
    assert calls == [("get_msg", {"message_id": 123456})]


def test_onebot_call_action_supports_generic_request_data_wrapper() -> None:
    from qzone_bridge.onebot_cookie import call_onebot_action

    calls: list[tuple[str, dict[str, object]]] = []

    class _Api:
        async def request(self, action: str, data: dict[str, object]):
            calls.append((action, dict(data)))
            return {"ok": True, "data": data}

    result = asyncio.run(call_onebot_action(types.SimpleNamespace(api=_Api()), "get_msg", message_id=123456))

    assert result == {"ok": True, "data": {"message_id": 123456}}
    assert calls == [("get_msg", {"message_id": 123456})]


def test_onebot_call_action_supports_single_envelope_protocol_clients() -> None:
    from qzone_bridge.onebot_cookie import call_onebot_action

    calls: list[dict[str, object]] = []

    class _Bot:
        async def request(self, payload: dict[str, object]):
            calls.append(payload)
            return {"ok": True, "payload": payload}

    result = asyncio.run(call_onebot_action(_Bot(), "get_msg", message_id=123456))

    assert result == {"ok": True, "payload": {"action": "get_msg", "params": {"message_id": 123456}}}
    assert calls == [{"action": "get_msg", "params": {"message_id": 123456}}]


def test_onebot_call_action_supports_send_api_alias() -> None:
    from qzone_bridge.onebot_cookie import call_onebot_action

    calls: list[tuple[str, dict[str, object]]] = []

    class _Bot:
        async def send_api(self, action: str, params: dict[str, object]):
            calls.append((action, dict(params)))
            return {"ok": True, "params": params}

    result = asyncio.run(call_onebot_action(_Bot(), "get_msg", message_id=123456))

    assert result == {"ok": True, "params": {"message_id": 123456}}
    assert calls == [("get_msg", {"message_id": 123456})]


def test_onebot_video_upload_probe_accepts_generic_envelope_extension_action() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"generic-envelope-a2"
    calls: list[dict[str, object]] = []

    class _Bot:
        async def request(self, payload: dict[str, object]):
            calls.append(payload)
            if payload == {"action": "get_qzone_video_upload_credentials", "params": {"domain": "qzone.qq.com"}}:
                return {"data": {"login_data_b64": base64.b64encode(raw_a2).decode("ascii")}}
            raise RuntimeError("unsupported action")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="onebot"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source == "onebot:get_qzone_video_upload_credentials"
    assert calls[0] == {"action": "get_qzone_video_upload_credentials", "params": {"domain": "qzone.qq.com"}}


def test_onebot_video_upload_probe_uses_protocol_dispatcher_without_implementation_name() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"generic-protocol-dispatcher-a2"
    calls: list[tuple[str, dict[str, object]]] = []

    class _ProtocolEndpoint:
        async def api_call(self, action: str, params: dict[str, object]):
            calls.append((action, dict(params)))
            if action == "get_video_upload_credentials" and params == {"domain": "qzone.qq.com"}:
                return {"status": "ok", "retcode": 0, "data": {"a2_hex": raw_a2.hex()}}
            raise RuntimeError("unsupported action")

    probe = asyncio.run(probe_video_upload_credentials(_ProtocolEndpoint(), source="onebot"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source == "onebot:get_video_upload_credentials"
    assert calls[0] == ("get_qzone_video_upload_credentials", {"domain": "qzone.qq.com"})
    assert ("get_video_upload_credentials", {"domain": "qzone.qq.com"}) in calls


def test_onebot_video_upload_probe_accepts_llonebot_pmhq_httpsend_login_misc() -> None:
    from qzone_bridge.onebot_upload import probe_video_upload_credentials

    raw_a2 = b"llonebot-pmhq-httpsend-a2"

    class _Bot:
        async def call_action(self, action: str, **params):
            if (
                action == "llonebot_debug"
                and params.get("apiClass") == "pmhq"
                and params.get("method") == "httpSend"
                and params.get("args")
                == [{"type": "call", "data": {"func": "loginService.getLoginMiscData", "args": ["a2"]}}]
            ):
                return {"data": {"result": {"value": raw_a2.hex()}}}
            raise RuntimeError("unsupported")

    probe = asyncio.run(probe_video_upload_credentials(_Bot(), source="test"))

    assert probe.credentials is not None
    assert probe.credentials.login_data_b64 == base64.b64encode(raw_a2).decode("ascii")
    assert probe.credentials.source == "test:llonebot_debug"


def test_capture_onebot_client_from_context_supports_llbot_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)

    bot = types.SimpleNamespace(call_api=lambda action, params=None: None)
    seen: list[str] = []

    class _Context:
        def get_platform(self, platform_type: str):
            seen.append(platform_type)
            if platform_type == "llbot":
                return types.SimpleNamespace(bot=bot)
            return None

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin._context = _Context()
    plugin._onebot_client = None

    assert plugin._capture_onebot_client_from_context() is bot
    assert plugin._onebot_client is bot
    assert "llbot" in seen


def test_plugin_publish_blocks_cover_payload_when_native_video_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    from qzone_bridge.errors import QzoneParseError

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake video bytes")
    original = main.PostPayload(
        content="hello",
        media=[
            main.PostMedia(
                kind="video",
                source=str(source),
                name="clip.mp4",
                mime_type="video/mp4",
                trusted_local=True,
            )
        ],
    )
    captured: dict[str, object] = {}

    async def fake_prepare(post):
        raise AssertionError("video publish must not render a cover when native video publishing is disabled")

    class _Controller:
        async def publish_post(self, **kwargs):
            captured["publish_kwargs"] = kwargs
            raise AssertionError("video publish must not fall back to image/cover publishing")

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(native_video_publish=False)
    plugin.data_dir = tmp_path
    plugin.controller = _Controller()
    plugin._prepare_publish_payload = fake_prepare

    with pytest.raises(QzoneParseError) as error:
        asyncio.run(plugin._publish_post_payload(original))

    assert "native_video_publish" in str(error.value)
    assert "Qzone Web Cookie/p_skey" in str(error.value)
    assert captured == {}


def test_publish_renderer_draws_video_play_overlay() -> None:
    from PIL import Image, ImageDraw, ImageFont

    from qzone_bridge.media import PostMedia
    from qzone_bridge.publish_renderer import _ImagePreview, _draw_preview_tile

    canvas = Image.new("RGB", (300, 180), (255, 255, 255))
    source = Image.new("RGB", (300, 180), (180, 20, 20))
    preview = _ImagePreview(
        media=PostMedia(kind="image", source="cover.jpg", raw_type="video"),
        image=source,
        failed=False,
    )

    _draw_preview_tile(
        ImageDraw.Draw(canvas),
        canvas,
        preview,
        0,
        0,
        300,
        180,
        ImageFont.load_default(),
        crop=False,
        scale=1,
    )

    assert canvas.getpixel((150, 90)) != (180, 20, 20)


def test_publish_renderer_draws_comment_section_separated_from_original(tmp_path: Path) -> None:
    from PIL import Image

    from qzone_bridge.media import PostPayload
    from qzone_bridge.publish_renderer import (
        COMMENT_ACCENT,
        COMMENT_BG,
        LINE,
        RENDER_SCALE,
        RenderProfile,
        render_publish_result_image,
    )

    base = render_publish_result_image(
        PostPayload(content="原始说说内容", media=[]),
        tmp_path,
        profile=RenderProfile(nickname="阿一", time_text="12:34"),
        width=720,
        remote_timeout=0.01,
        fixed_width=True,
    )
    commented = render_publish_result_image(
        PostPayload(content="原始说说内容", media=[]),
        tmp_path,
        profile=RenderProfile(nickname="阿一", time_text="12:34"),
        result={"comment": "这是一条和原文分开的评论内容"},
        width=720,
        remote_timeout=0.01,
        fixed_width=True,
    )

    with Image.open(base) as base_image, Image.open(commented) as commented_image:
        assert commented_image.height > base_image.height
        bg_coords = [
            (x, y)
            for y in range(commented_image.height)
            for x in range(commented_image.width)
            if commented_image.getpixel((x, y)) == COMMENT_BG
        ]
        accent_coords = [
            (x, y)
            for y in range(commented_image.height)
            for x in range(commented_image.width)
            if commented_image.getpixel((x, y)) == COMMENT_ACCENT
        ]
        assert bg_coords
        assert accent_coords
        min_bg_y = min(y for _x, y in bg_coords)
        max_bg_x = max(x for x, _y in bg_coords)
        min_accent_x = min(x for x, _y in accent_coords)
        max_accent_x = max(x for x, _y in accent_coords)
        min_accent_y = min(y for _x, y in accent_coords)
        max_accent_y = max(y for _x, y in accent_coords)
        top_edge_accent = [(x, y) for x, y in accent_coords if y == min_bg_y]
        top_edge_bg = [(x, y) for x, y in bg_coords if y == min_bg_y]
        upper_vertical_accent = [
            (x, y)
            for x, y in accent_coords
            if min_bg_y + 20 * RENDER_SCALE <= y <= min_bg_y + 45 * RENDER_SCALE
        ]
        vertical_contact_y = min_bg_y + 28 * RENDER_SCALE
        vertical_accent_edge = [x for x, y in accent_coords if y == vertical_contact_y]
        vertical_bg_edge = [x for x, y in bg_coords if y == vertical_contact_y]
        assert min_bg_y > base_image.height // 2
        assert max_bg_x > commented_image.width - 100
        assert max_accent_y - min_accent_y > 80 * RENDER_SCALE
        assert max_accent_x - min_accent_x > 38 * RENDER_SCALE
        assert top_edge_accent
        assert top_edge_bg
        assert 0 <= min(x for x, _y in top_edge_bg) - max(x for x, _y in top_edge_accent) <= RENDER_SCALE
        assert upper_vertical_accent
        assert vertical_accent_edge
        assert vertical_bg_edge
        assert 0 <= min(vertical_bg_edge) - max(vertical_accent_edge) <= RENDER_SCALE
        bottom_tail = [
            (x, y)
            for x, y in accent_coords
            if y >= max_accent_y - 3 * RENDER_SCALE
        ]
        assert max(x for x, _y in bottom_tail) - min(x for x, _y in bottom_tail) > 18 * RENDER_SCALE
        right_cap_y_values = [y for x, y in accent_coords if x == max_accent_x]
        assert max(right_cap_y_values) - min(right_cap_y_values) >= 2 * RENDER_SCALE
        upper_curve_y = max_accent_y - 30 * RENDER_SCALE
        lower_curve_y = max_accent_y - 10 * RENDER_SCALE
        upper_curve_x_values = [x for x, y in accent_coords if y == upper_curve_y]
        lower_curve_x_values = [x for x, y in accent_coords if y == lower_curve_y]
        assert upper_curve_x_values
        assert lower_curve_x_values
        assert max(lower_curve_x_values) - max(upper_curve_x_values) >= 6 * RENDER_SCALE
        divider_y_candidates = [
            y
            for x in range(commented_image.width // 10, commented_image.width - commented_image.width // 10)
            for y in range(base_image.height // 2, min_bg_y)
            if commented_image.getpixel((x, y)) == LINE
        ]
        assert divider_y_candidates
        divider_y = min(divider_y_candidates)
        assert min_bg_y - divider_y >= 44 * RENDER_SCALE


def test_qzone_post_card_range_combines_when_renderer_combiner_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    monkeypatch.delattr(main._publish_renderer, "combine_rendered_post_cards", raising=False)
    sizes = {
        "第一条": (80, 20, (255, 0, 0)),
        "第二条": (60, 20, (0, 255, 0)),
    }

    def fake_render(post, output_dir, *, profile=None, result=None, width=900, remote_timeout=1.5, fixed_width=False):
        from PIL import Image

        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{post.content}.png"
        image_width, image_height, color = sizes[post.content]
        Image.new("RGB", (image_width, image_height), color).save(path)
        return path

    class _Event:
        stopped = False

        def stop_event(self):
            self.stopped = True

        def image_result(self, path: str):
            return {"type": "image", "path": path}

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        render_publish_result=True,
        render_result_width=720,
        render_remote_timeout=0.01,
        render_feed_card_limit=5,
        max_feed_limit=20,
    )
    plugin.data_dir = tmp_path
    monkeypatch.setattr(main, "render_publish_result_image", fake_render)

    posts = [
        main.QzonePost(hostuin=10001, fid="fid-1", summary="第一条", nickname="阿一", local_id=1),
        main.QzonePost(hostuin=10002, fid="fid-2", summary="第二条", nickname="阿二", local_id=2),
    ]

    results = asyncio.run(plugin._post_card_results(_Event(), posts, "fallback"))

    from PIL import Image

    assert len(results) == 1
    assert results[0]["type"] == "image"
    with Image.open(results[0]["path"]) as combined:
        assert combined.width == 80
        assert combined.height > 40
        assert combined.getpixel((0, 0)) == (255, 0, 0)
        assert combined.getpixel((0, 32)) == (0, 255, 0)


def test_compat_fallback_combiner_prunes_stale_rendered_images(tmp_path: Path) -> None:
    import os

    from PIL import Image

    from qzone_bridge.compat import fallback_combine_rendered_post_cards

    output_dir = tmp_path / "rendered"
    output_dir.mkdir()
    for index in range(132):
        stale = output_dir / f"publish_result_stale_{index}.png"
        stale.write_bytes(b"old")
        old_time = 1_700_000_000 + index
        os.utime(stale, (old_time, old_time))

    first = output_dir / "first.png"
    second = output_dir / "second.png"
    Image.new("RGB", (24, 12), (255, 0, 0)).save(first)
    Image.new("RGB", (24, 12), (0, 255, 0)).save(second)

    result = fallback_combine_rendered_post_cards([first, second], output_dir, renderer_module=types.SimpleNamespace())

    assert result is not None and result.exists()
    assert len(list(output_dir.glob("publish_result_*.png"))) <= 129


def test_qzone_commands_render_post_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    expected_helpers = {
        "view_feed": "_yield_post_card_results",
        "read_feed": "_yield_post_card_results",
        "comment_feed": "_yield_post_card_results",
        "like_feed": "_yield_post_card_results",
        "qzone_feed": "_yield_post_card_results",
        "qzone_detail": "_yield_post_card_results",
        "qzone_comment": "_yield_post_card_results",
        "qzone_like": "_yield_post_card_results",
    }
    for method_name, helper in expected_helpers.items():
        source = inspect.getsource(getattr(main.QzoneStablePlugin, method_name))
        assert helper in source, method_name

    like_source = inspect.getsource(main.QzoneStablePlugin.like_feed)
    assert 'with_detail=True' in like_source


def test_qzone_comment_renders_card_with_comment_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Event:
        def is_admin(self):
            return True

        def stop_event(self):
            pass

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    class _Controller:
        async def comment_post(self, *, hostuin: int, fid: str, content: str):
            captured["comment"] = (hostuin, fid, content)
            return {"ok": True}

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(admin_uins=[])
    plugin.data_dir = tmp_path
    plugin.controller = _Controller()
    post = main.QzonePost(hostuin=12345, fid="fid-1", summary="原文", nickname="自己", local_id=1)

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_detail(*args, **kwargs):
        return post

    async def fake_yield_cards(event, selected_posts, fallback_text, **kwargs):
        captured["cards"] = (selected_posts, fallback_text, kwargs)
        yield {"type": "image", "path": str(tmp_path / "qzone-comment-card.png")}

    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._post_from_detail_target = fake_detail
    plugin._yield_post_card_results = fake_yield_cards

    async def collect_results():
        results = []
        async for item in plugin.qzone_comment(_Event(), 12345, "fid-1", "评论内容"):
            results.append(item)
        return results

    results = asyncio.run(collect_results())

    assert captured["comment"] == (12345, "fid-1", "评论内容")
    assert captured["cards"][0] == [post]
    assert captured["cards"][2]["comment_texts"] == {id(post): "评论内容"}
    assert results[0]["type"] == "plain"
    assert results[1] == {"type": "image", "path": str(tmp_path / "qzone-comment-card.png")}


@pytest.mark.parametrize("command_name", ["qzone_detail", "qzone_comment", "qzone_like"])
def test_direct_qzone_commands_render_original_post_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command_name: str,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Event:
        stopped = False

        def is_admin(self):
            return True

        def stop_event(self):
            self.stopped = True

        def image_result(self, path: str):
            return {"type": "image", "path": path}

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    class _Controller:
        async def detail_feed(self, *, hostuin: int, fid: str, appid: int = 311):
            return {
                "entry": {
                    "hostuin": hostuin,
                    "fid": fid,
                    "appid": appid,
                    "summary": "原说说内容",
                    "nickname": "真实昵称",
                    "created_at": 1_690_000_000,
                    "raw": {"summary": "原说说内容"},
                },
                "raw": {"summary": "原说说内容"},
                "comments": [],
            }

        async def comment_post(self, *, hostuin: int, fid: str, content: str):
            return {"ok": True}

        async def like_post(self, *, hostuin: int, fid: str, appid: int = 311, unlike: bool = False):
            return {"ok": True, "liked": not unlike, "summary": "原说说内容"}

    def fake_render(post, output_dir, *, profile=None, result=None, width=900, remote_timeout=1.5, fixed_width=False):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{command_name}.png"
        path.write_bytes(b"png")
        captured.setdefault("profiles", []).append(profile)
        return path

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        admin_uins=[],
        render_publish_result=True,
        render_result_width=720,
        render_remote_timeout=0.01,
        render_feed_card_limit=5,
        max_feed_limit=20,
    )
    plugin.data_dir = tmp_path
    plugin.controller = _Controller()

    async def fake_ready(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "render_publish_result_image", fake_render)
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready

    async def collect_results():
        event = _Event()
        results = []
        if command_name == "qzone_detail":
            iterator = plugin.qzone_detail(event, 12345, "fid-direct", 311)
        elif command_name == "qzone_comment":
            iterator = plugin.qzone_comment(event, 12345, "fid-direct", "评论内容")
        else:
            iterator = plugin.qzone_like(event, 12345, "fid-direct", 311, False)
        async for item in iterator:
            results.append(item)
        return results

    results = asyncio.run(collect_results())

    profiles = captured["profiles"]
    assert profiles[-1].time_text == datetime.fromtimestamp(1_690_000_000).strftime("%m-%d %H:%M")
    assert any(item.get("type") == "image" for item in results)


def test_qzone_detail_renders_cached_feed_image_from_detail_raw(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Event:
        def is_admin(self):
            return True

        def stop_event(self):
            pass

        def image_result(self, path: str):
            return {"type": "image", "path": path}

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    class _Controller:
        async def detail_feed(self, *, hostuin: int, fid: str, appid: int = 311):
            raw = {
                "summary": "detail text",
                "_feed_raw": {"pic": [{"url1": "https://qzone.example.test/cached-feed.jpg"}]},
            }
            return {
                "entry": {
                    "hostuin": hostuin,
                    "fid": fid,
                    "appid": appid,
                    "summary": "detail text",
                    "nickname": "detail nickname",
                    "created_at": 1_690_000_000,
                    "raw": raw,
                },
                "raw": raw,
                "comments": [],
            }

    def fake_render(post, output_dir, *, profile=None, result=None, width=900, remote_timeout=1.5, fixed_width=False):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "detail-card.png"
        path.write_bytes(b"png")
        captured["post"] = post
        return path

    async def fake_ready(*args, **kwargs):
        return None

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        admin_uins=[],
        render_publish_result=True,
        render_result_width=720,
        render_remote_timeout=0.01,
        render_feed_card_limit=5,
        max_feed_limit=20,
    )
    plugin.data_dir = tmp_path
    plugin.controller = _Controller()
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    monkeypatch.setattr(main, "render_publish_result_image", fake_render)

    async def collect_results():
        results = []
        async for item in plugin.qzone_detail(_Event(), 12345, "fid-detail-image", 311):
            results.append(item)
        return results

    results = asyncio.run(collect_results())
    rendered_post = captured["post"]

    assert rendered_post.media[0].source == "https://qzone.example.test/cached-feed.jpg"
    assert results == [{"type": "image", "path": str(tmp_path / "rendered_posts" / "detail-card.png")}]


@pytest.mark.parametrize(
    ("command_name", "message_str"),
    [
        ("view_feed", "看说说 1"),
        ("like_feed", "赞说说 1"),
    ],
)
def test_chinese_feed_commands_render_original_post_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command_name: str,
    message_str: str,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {"profiles": []}
    created_at = 1_690_123_456

    class _Event:
        stopped = False

        def __init__(self):
            self.message_str = message_str

        def is_admin(self):
            return True

        def stop_event(self):
            self.stopped = True

        def image_result(self, path: str):
            return {"type": "image", "path": path}

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    class _PostService:
        async def like_post(self, post):
            captured["liked_fid"] = post.fid
            return {"ok": True, "liked": True}

    post = main.QzonePost(
        hostuin=12345,
        fid="fid-feed",
        summary="原说说内容",
        nickname="真实昵称",
        created_at=created_at,
        local_id=1,
    )

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_posts_for_event(event, names, **kwargs):
        captured["names"] = names
        captured["post_kwargs"] = kwargs
        return [post]

    def fake_render(post_payload, output_dir, *, profile=None, result=None, width=900, remote_timeout=1.5, fixed_width=False):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{command_name}.png"
        path.write_bytes(b"png")
        captured["profiles"].append(profile)
        captured["render_result"] = result
        return path

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        admin_uins=[],
        render_publish_result=True,
        render_result_width=720,
        render_remote_timeout=0.01,
        render_feed_card_limit=5,
        max_feed_limit=20,
    )
    plugin.data_dir = tmp_path
    plugin.controller = types.SimpleNamespace()
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._posts_for_event = fake_posts_for_event
    plugin._post_service = lambda: _PostService()
    monkeypatch.setattr(main, "render_publish_result_image", fake_render)

    async def collect_results():
        event = _Event()
        results = []
        iterator = plugin.view_feed(event) if command_name == "view_feed" else plugin.like_feed(event)
        async for item in iterator:
            results.append(item)
        return results

    results = asyncio.run(collect_results())

    profiles = captured["profiles"]
    assert profiles
    assert profiles[0].nickname == "真实昵称"
    assert profiles[0].time_text == datetime.fromtimestamp(created_at).strftime("%m-%d %H:%M")
    assert captured["post_kwargs"]["with_detail"] is True
    assert any(result["type"] == "image" for result in results)
    if command_name == "like_feed":
        assert captured["liked_fid"] == "fid-feed"


def test_auto_comment_admin_feedback_sends_rendered_card(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        def __init__(self):
            self.sent: list[dict[str, object]] = []

        def send_group_msg(self, *, group_id: int, message):
            self.sent.append({"group_id": group_id, "message": message})

    async def fake_render_card(self, post):
        path = tmp_path / "auto-card.png"
        path.write_bytes(b"png")
        return path

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(send_admin=True, manage_group=123456, admin_uins=[])
    plugin._onebot_client = _Bot()
    plugin._context = None
    monkeypatch.setattr(main.QzoneStablePlugin, "_render_qzone_post_card", fake_render_card)

    post = main.QzonePost(hostuin=12345, fid="fid-1", summary="一条自动评论目标说说", nickname="小明")
    asyncio.run(plugin._notify_admin_post_card(None, post, "定时自动评论完成"))

    assert plugin._onebot_client.sent
    message = plugin._onebot_client.sent[0]["message"]
    assert plugin._onebot_client.sent[0]["group_id"] == 123456
    assert message[0]["type"] == "text"
    assert "定时自动评论完成" in message[0]["data"]["text"]
    assert message[1]["type"] == "image"
    assert message[1]["data"]["file"].startswith("file:///")


def test_auto_comment_event_feedback_sends_rendered_card_to_current_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Bot:
        def __init__(self):
            self.sent: list[dict[str, object]] = []

        def send_group_msg(self, *, group_id: int, message):
            self.sent.append({"group_id": group_id, "message": message})

    class _Event:
        bot = _Bot()

        def get_group_id(self):
            return 4242

        def get_sender_id(self):
            return 5151

    async def fake_render_card(self, post, *, comment_text=""):
        captured["comment_text"] = comment_text
        path = tmp_path / "event-card.png"
        path.write_bytes(b"png")
        return path

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace()
    plugin._onebot_client = None
    plugin._context = None
    monkeypatch.setattr(main.QzoneStablePlugin, "_render_qzone_post_card", fake_render_card)

    post = main.QzonePost(hostuin=12345, fid="fid-1", summary="post", nickname="Alice")
    event = _Event()
    asyncio.run(plugin._notify_event_post_card(event, post, "auto comment done", comment_text="nice"))

    assert captured["comment_text"] == "nice"
    assert event.bot.sent
    assert event.bot.sent[0]["group_id"] == 4242
    message = event.bot.sent[0]["message"]
    assert message[0]["type"] == "text"
    assert "auto comment done" in message[0]["data"]["text"]
    assert message[1]["type"] == "image"
    assert message[1]["data"]["file"].startswith("file:///")


def test_auto_comment_event_feedback_sends_private_when_no_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        def __init__(self):
            self.sent: list[dict[str, object]] = []

        def send_private_msg(self, *, user_id: int, message):
            self.sent.append({"user_id": user_id, "message": message})

    class _Event:
        bot = _Bot()

        def get_group_id(self):
            return 0

        def get_sender_id(self):
            return 5151

    async def fake_render_card(self, post, *, comment_text=""):
        path = tmp_path / "event-card.png"
        path.write_bytes(b"png")
        return path

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace()
    plugin._onebot_client = None
    plugin._context = None
    monkeypatch.setattr(main.QzoneStablePlugin, "_render_qzone_post_card", fake_render_card)

    post = main.QzonePost(hostuin=12345, fid="fid-1", summary="post", nickname="Alice")
    event = _Event()
    asyncio.run(plugin._notify_event_post_card(event, post, "auto comment done", comment_text="nice"))

    assert event.bot.sent
    assert event.bot.sent[0]["user_id"] == 5151


def test_auto_comment_admin_feedback_falls_back_to_astrbot_global_admins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Context:
        def get_config(self):
            return {"admins_id": ["2134084530", "not-a-qq", "2134084530"]}

    class _Bot:
        def __init__(self):
            self.sent: list[dict[str, object]] = []

        def send_private_msg(self, *, user_id: int, message):
            self.sent.append({"user_id": user_id, "message": message})

    async def fake_render_card(self, post, *, comment_text=""):
        path = tmp_path / "auto-card.png"
        path.write_bytes(b"png")
        return path

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(send_admin=True, manage_group=0, admin_uins=[])
    plugin._onebot_client = _Bot()
    plugin._context = _Context()
    monkeypatch.setattr(main.QzoneStablePlugin, "_render_qzone_post_card", fake_render_card)

    post = main.QzonePost(hostuin=12345, fid="fid-1", summary="一条自动评论目标说说", nickname="小明")
    asyncio.run(plugin._notify_admin_post_card(None, post, "定时自动评论完成", comment_text="写得真好"))

    assert plugin._onebot_client.sent
    assert plugin._onebot_client.sent[0]["user_id"] == 2134084530
    message = plugin._onebot_client.sent[0]["message"]
    assert message[0]["type"] == "text"
    assert message[1]["type"] == "image"


def test_admin_notification_logs_when_no_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, list[str]] = {"logs": []}

    class _Logger:
        def debug(self, *args, **kwargs): ...

        def info(self, *args, **kwargs): ...

        def exception(self, *args, **kwargs): ...

        def warning(self, message, *args, **kwargs):
            captured["logs"].append(message % args if args else str(message))

    class _Context:
        def get_config(self):
            return {"admins_id": []}

    class _Bot:
        def send_private_msg(self, *, user_id: int, message):
            raise AssertionError("no private send target should be attempted")

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(manage_group=0, admin_uins=[])
    plugin._context = _Context()
    monkeypatch.setattr(main, "logger", _Logger())

    sent = asyncio.run(plugin._send_admin_outgoing(_Bot(), "hello"))

    assert sent == 0
    assert any("no target" in item for item in captured["logs"])


def test_admin_notification_supports_onebot_api_call_action(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    calls: list[tuple[str, dict[str, object]]] = []

    class _Context:
        def get_config(self):
            return {"admins_id": ["2134084530"]}

    class _Api:
        async def call_action(self, action: str, **kwargs):
            calls.append((action, kwargs))

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(manage_group=0, admin_uins=[])
    plugin._context = _Context()

    sent = asyncio.run(plugin._send_admin_outgoing(types.SimpleNamespace(api=_Api()), "hello"))

    assert sent == 1
    assert calls == [("send_private_msg", {"user_id": 2134084530, "message": "hello"})]


def test_admin_notification_supports_onebot_direct_call_action(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    calls: list[tuple[str, dict[str, object]]] = []

    class _Context:
        def get_config(self):
            return {"admins_id": ["2134084530"]}

    class _Bot:
        async def call_action(self, action: str, **kwargs):
            calls.append((action, kwargs))

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(manage_group=0, admin_uins=[])
    plugin._context = _Context()

    sent = asyncio.run(plugin._send_admin_outgoing(_Bot(), "hello"))

    assert sent == 1
    assert calls == [("send_private_msg", {"user_id": 2134084530, "message": "hello"})]


def test_admin_notification_supports_onebot_request_data_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    calls: list[tuple[str, dict[str, object]]] = []

    class _Context:
        def get_config(self):
            return {"admins_id": ["2134084530"]}

    class _Api:
        async def request(self, action: str, data: dict[str, object]):
            calls.append((action, dict(data)))

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(manage_group=0, admin_uins=[])
    plugin._context = _Context()

    sent = asyncio.run(plugin._send_admin_outgoing(types.SimpleNamespace(api=_Api()), "hello"))

    assert sent == 1
    assert calls == [("send_private_msg", {"user_id": 2134084530, "message": "hello"})]


def test_capture_onebot_client_from_context_get_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        async def call_action(self, action: str, **kwargs): ...

    bot = _Bot()

    class _Context:
        def get_platform(self, platform_type: str):
            assert platform_type == "aiocqhttp"
            return types.SimpleNamespace(bot=bot)

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin._context = _Context()
    plugin._onebot_client = None

    assert plugin._capture_onebot_client_from_context() is bot
    assert plugin._onebot_client is bot


def test_capture_onebot_client_from_context_get_client(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        async def call_action(self, action: str, **kwargs): ...

    bot = _Bot()

    class _Platform:
        def get_client(self):
            return bot

    class _Context:
        def get_platform(self, platform_type: str):
            assert platform_type == "aiocqhttp"
            return _Platform()

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin._context = _Context()
    plugin._onebot_client = None

    assert plugin._capture_onebot_client_from_context() is bot
    assert plugin._onebot_client is bot


def test_capture_onebot_client_from_context_onebot_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        async def call_api(self, action: str, params: dict[str, object]): ...

    bot = _Bot()
    seen: list[str] = []

    class _Context:
        def get_platform(self, platform_type: str):
            seen.append(platform_type)
            if platform_type == "onebot":
                return types.SimpleNamespace(client=bot)
            return None

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin._context = _Context()
    plugin._onebot_client = None

    assert plugin._capture_onebot_client_from_context() is bot
    assert plugin._onebot_client is bot
    assert seen[:2] == ["aiocqhttp", "onebot"]


def test_capture_onebot_client_from_context_extended_onebot_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        async def call_api(self, action: str, params: dict[str, object]): ...

    bot = _Bot()
    seen: list[str] = []

    class _Context:
        def get_platform(self, platform_type: str):
            seen.append(platform_type)
            if platform_type == "onebot_v12":
                return types.SimpleNamespace(client=bot)
            return None

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin._context = _Context()
    plugin._onebot_client = None

    assert plugin._capture_onebot_client_from_context() is bot
    assert plugin._onebot_client is bot
    assert "onebot_v12" in seen


def test_capture_onebot_client_from_platform_manager_onebot_name(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        async def call_action(self, action: str, **kwargs): ...

    bot = _Bot()

    class _Platform:
        def __init__(self):
            self.bot = bot

        def meta(self):
            return types.SimpleNamespace(name="onebot-v11")

    class _Context:
        platform_manager = types.SimpleNamespace(platform_insts=[_Platform()])

        def get_platform(self, platform_type: str):
            return None

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin._context = _Context()
    plugin._onebot_client = None

    assert plugin._capture_onebot_client_from_context() is bot
    assert plugin._onebot_client is bot


def test_capture_onebot_client_from_message_object(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        async def call_action(self, action: str, **kwargs): ...

    bot = _Bot()
    event = types.SimpleNamespace(message_obj=types.SimpleNamespace(bot=bot))
    plugin = object.__new__(main.QzoneStablePlugin)
    plugin._context = None
    plugin._onebot_client = None

    assert plugin._capture_onebot_client(event) is bot
    assert plugin._onebot_client is bot


def test_admin_notifications_warn_when_onebot_client_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, list[str]] = {"logs": []}

    class _Logger:
        def debug(self, *args, **kwargs): ...

        def info(self, *args, **kwargs): ...

        def exception(self, *args, **kwargs): ...

        def warning(self, message, *args, **kwargs):
            captured["logs"].append(message % args if args else str(message))

    class _Context:
        def get_platform(self, platform_type: str):
            return None

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(send_admin=True)
    plugin._context = _Context()
    plugin._onebot_client = None
    monkeypatch.setattr(main, "logger", _Logger())

    post = main.QzonePost(hostuin=12345, fid="fid-1", summary="post")
    payload = main.PostPayload(content="post", media=[])
    asyncio.run(plugin._notify_admin_post_card(None, post, "comment done"))
    asyncio.run(plugin._notify_admin_publish_result(payload, {"fid": "fid-1"}, "publish done"))

    assert any("post card notification skipped: no OneBot client" in item for item in captured["logs"])
    assert any("publish admin notification skipped: no OneBot client" in item for item in captured["logs"])


def test_auto_publish_once_notifies_admin_with_rendered_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {"logs": []}

    class _Logger:
        def debug(self, *args, **kwargs): ...

        def warning(self, *args, **kwargs): ...

        def exception(self, *args, **kwargs): ...

        def info(self, message, *args, **kwargs):
            captured["logs"].append(message % args if args else str(message))

    class _Controller:
        async def publish_post(self, **kwargs):
            captured["publish_kwargs"] = kwargs
            return {"fid": "fid-published", "message": "ok"}

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_generate(event, topic):
        return "今天自动发一条说说"

    async def fake_notify(post, payload, message):
        captured["notified"] = {
            "post": post,
            "payload": payload,
            "message": message,
        }

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(send_admin=True)
    plugin.data_dir = tmp_path
    plugin.controller = _Controller()
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._generate_post_text = fake_generate
    plugin._notify_admin_publish_result = fake_notify
    monkeypatch.setattr(main, "logger", _Logger())

    asyncio.run(plugin._auto_publish_once())

    assert captured["publish_kwargs"] == {"content": "今天自动发一条说说", "content_sanitized": True}
    assert captured["notified"]["payload"]["fid"] == "fid-published"
    assert captured["notified"]["post"].content == "今天自动发一条说说"
    assert "定时自动发布" in captured["notified"]["message"]
    assert any("scheduled publish started" in item for item in captured["logs"])
    assert any("scheduled publish succeeded" in item for item in captured["logs"])


def test_auto_news_publish_once_publishes_and_records_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {"logs": []}

    class _Logger:
        def debug(self, *args, **kwargs): ...

        def warning(self, *args, **kwargs): ...

        def exception(self, *args, **kwargs): ...

        def info(self, message, *args, **kwargs):
            captured["logs"].append(message % args if args else str(message))

    class _Controller:
        async def publish_post(self, **kwargs):
            captured["publish_kwargs"] = kwargs
            return {"fid": "fid-news", "message": "ok"}

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_candidates(**kwargs):
        captured["candidate_kwargs"] = kwargs
        return [
            main.NewsItem(
                title="航天员返回地球后最想洗头",
                source="羊城晚报",
                link="https://news.google.com/rss/articles/example",
                published_at=1772250185,
                scope="china",
                item_id="news-1",
            )
        ]

    async def fake_generate(event, items):
        captured["generate_items"] = items
        return "人在太空待久了，回到地面第一件小事都很具体。"

    async def fake_notify(post, payload, message):
        captured["notified"] = {
            "post": post,
            "payload": payload,
            "message": message,
        }

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(news_once_per_day=True)
    plugin.data_dir = tmp_path
    plugin.controller = _Controller()
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._news_candidates = fake_candidates
    plugin._generate_original_news_post_text = fake_generate
    plugin._notify_admin_publish_result = fake_notify
    monkeypatch.setattr(main, "logger", _Logger())

    asyncio.run(plugin._auto_news_publish_once())

    assert captured["publish_kwargs"] == {
        "content": "人在太空待久了，回到地面第一件小事都很具体。",
        "content_sanitized": True,
    }
    assert captured["candidate_kwargs"] == {"seen_ids": set()}
    assert captured["notified"]["payload"]["fid"] == "fid-news"
    assert captured["notified"]["post"].content == "人在太空待久了，回到地面第一件小事都很具体。"
    assert "新闻自动发布完成" in captured["notified"]["message"]

    state = json.loads((tmp_path / "news_publish_state.json").read_text(encoding="utf-8"))
    assert state["last_date"] == plugin._news_today_key()
    assert state["published"][0]["candidate_ids"] == ["news-1"]
    assert state["published"][0]["fid"] == "fid-news"
    assert any("scheduled news publish succeeded" in item for item in captured["logs"])


def test_auto_news_publish_once_skips_after_daily_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(news_once_per_day=True)
    plugin.data_dir = tmp_path
    (tmp_path / "news_publish_state.json").write_text(
        json.dumps({"last_date": plugin._news_today_key(), "published": []}),
        encoding="utf-8",
    )

    async def fail_candidates(**kwargs):
        raise AssertionError("already-published day should not fetch RSS")

    plugin._news_candidates = fail_candidates

    asyncio.run(plugin._auto_news_publish_once())


def test_news_fetch_command_caches_custom_candidate_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Event:
        message_str = "新闻说说 获取 2 混合"

        def is_admin(self):
            return True

        def get_sender_id(self):
            return 12345

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    async def fake_candidates(**kwargs):
        captured["kwargs"] = kwargs
        return [
            main.NewsItem(title="第一条新闻", source="来源甲", published_at=1772250185, scope="china", item_id="news-1"),
            main.NewsItem(title="第二条新闻", source="来源乙", published_at=1772240000, scope="world", item_id="news-2"),
        ]

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(admin_uins=[], news_max_candidates=12)
    plugin.data_dir = tmp_path
    plugin._news_candidates = fake_candidates

    async def collect_results():
        results = []
        async for item in plugin.news_feed_fetch(_Event()):
            results.append(item)
        return results

    results = asyncio.run(collect_results())

    assert captured["kwargs"] == {"scope_override": "混合", "seen_ids": set(), "limit": 2}
    assert results[0]["type"] == "plain"
    assert "1. 第一条新闻" in results[0]["text"]
    assert "2. 第二条新闻" in results[0]["text"]
    assert "新闻说说 发布 <序号>" in results[0]["text"]
    cache = json.loads((tmp_path / "news_candidates.json").read_text(encoding="utf-8"))
    assert cache["requested_limit"] == 2
    assert [item["item_id"] for item in cache["items"]] == ["news-1", "news-2"]


def test_news_publish_command_uses_cached_selection_and_records_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Event:
        message_str = "新闻说说 发布 2"

        def is_admin(self):
            return True

        def get_sender_id(self):
            return 12345

        def get_self_id(self):
            return 998877

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    class _Controller:
        async def publish_post(self, **kwargs):
            captured["publish_kwargs"] = kwargs
            return {"fid": "fid-news-manual", "message": "ok"}

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_generate(event, items):
        captured["generate_items"] = items
        return "这条新闻适合写成一段原创短评。"

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(admin_uins=[], render_publish_result=False)
    plugin.data_dir = tmp_path
    plugin.posts = main.PostStore(tmp_path / "posts.json")
    plugin.controller = _Controller()
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._generate_original_news_post_text = fake_generate
    plugin._save_news_candidates_cache(
        [
            main.NewsItem(title="第一条新闻", source="来源甲", item_id="news-1"),
            main.NewsItem(title="第二条新闻", source="来源乙", item_id="news-2"),
        ],
        requested_limit=2,
    )

    async def collect_results():
        results = []
        async for item in plugin.news_feed_publish(_Event()):
            results.append(item)
        return results

    results = asyncio.run(collect_results())

    assert captured["generate_items"][0].item_id == "news-2"
    assert captured["publish_kwargs"] == {"content": "这条新闻适合写成一段原创短评。", "content_sanitized": True}
    assert "发布结果" in results[0]["text"]
    state = json.loads((tmp_path / "news_publish_state.json").read_text(encoding="utf-8"))
    assert state["published"][0]["candidate_ids"] == ["news-2"]
    assert state["published"][0]["fid"] == "fid-news-manual"


def test_generate_original_news_post_text_retries_copy_like_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    plugin = object.__new__(main.QzoneStablePlugin)
    item = main.NewsItem(title="航天员返回地球后最想洗头", source="羊城晚报", item_id="news-1")
    responses = ["航天员返回地球后最想洗头", "回到地面后，最想念的也许就是那些日常小事。"]

    async def fake_generate(event, items):
        return responses.pop(0)

    plugin._generate_news_post_text = fake_generate

    text = asyncio.run(plugin._generate_original_news_post_text(None, [item]))

    assert text == "回到地面后，最想念的也许就是那些日常小事。"
    assert responses == []


def test_auto_publish_once_uses_life_scheduler_and_omnidraw_selfie_return_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {"publish": None, "selfie_calls": []}

    class _LifePlugin:
        async def get_life_context(self):
            return {
                "outfit": "白色毛衣和牛仔裙",
                "schedule": "上午整理房间，下午去咖啡厅看书",
                "meta": {"style": "清新日常"},
            }

    class _OmniDrawPlugin:
        async def tool_generate_selfie(self, event, action: str, **kwargs):
            captured["selfie_calls"].append((event, action, kwargs))
            return {
                "success": True,
                "message": "ok",
                "images": [
                    {
                        "file_path": str(tmp_path / "selfie.png"),
                        "url": "https://example.invalid/fallback.png",
                    }
                ],
            }

    class _Metadata:
        def __init__(self, star_cls):
            self.star_cls = star_cls

    class _Context:
        def __init__(self):
            self.life = _LifePlugin()
            self.omnidraw = _OmniDrawPlugin()

        def get_registered_star(self, name):
            if name == "astrbot_plugin_life_scheduler":
                return _Metadata(self.life)
            if name == "astrbot_plugin_omnidraw":
                return _Metadata(self.omnidraw)
            return None

    class _Controller:
        async def publish_post(self, **kwargs):
            captured["publish"] = kwargs
            return {"fid": "fid-life", "photo_count": len(kwargs.get("media") or [])}

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_generate_prompt(event, life_context):
        captured["life_context"] = life_context
        return "坐在咖啡厅窗边看书，白色毛衣和牛仔裙，手机自拍"

    async def fake_caption(event, *, life_context, image_prompt):
        captured["caption_args"] = (life_context, image_prompt)
        return "下午在咖啡香里偷一点安静。"

    async def fake_notify(post, payload, message):
        captured["notify"] = (post, payload, message)

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = PluginSettings.from_mapping(
        {
            "admin_uins": "123456",
            "life_publish": {
                "enabled": True,
                "aspect_ratio": "3:4",
                "size": "1024x1365",
                "extra_params": "--style selfie",
            },
        }
    )
    plugin.data_dir = tmp_path
    plugin._context = _Context()
    plugin.controller = _Controller()
    plugin._onebot_client = None
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._generate_life_image_prompt = fake_generate_prompt
    plugin._generate_life_caption = fake_caption
    plugin._notify_admin_publish_result = fake_notify

    asyncio.run(plugin._auto_publish_once())

    selfie_event, action, kwargs = captured["selfie_calls"][0]
    assert action == "坐在咖啡厅窗边看书，白色毛衣和牛仔裙，手机自拍"
    assert kwargs["return_result"] is True
    assert kwargs["aspect_ratio"] == "3:4"
    assert kwargs["size"] == "1024x1365"
    assert kwargs["extra_params"] == "--style selfie"
    assert kwargs["refs"] == ""
    assert selfie_event.get_sender_id() == 123456
    publish = captured["publish"]
    assert publish["content"] == "下午在咖啡香里偷一点安静。"
    assert publish["content_sanitized"] is True
    assert publish["media"][0]["source"] == str(tmp_path / "selfie.png")
    assert publish["media"][0]["kind"] == "image"
    assert captured["notify"][0].media[0].source == str(tmp_path / "selfie.png")


def test_auto_life_publish_accepts_manual_event_and_force_publishes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {"publish": None, "selfie_calls": []}

    class _Event:
        unified_msg_origin = "aiocqhttp:group:654"
        message_str = "发日常说说全自动完整发布"

        def get_sender_id(self):
            return 987654

        def get_group_id(self):
            return 654

        def get_message_type(self):
            return "group"

        def get_session_id(self):
            return self.unified_msg_origin

    class _LifePlugin:
        async def get_life_context(self):
            return {"outfit": "浅色卫衣", "schedule": "傍晚散步"}

    class _OmniDrawPlugin:
        async def tool_generate_selfie(self, event, action: str, **kwargs):
            captured["selfie_calls"].append((event, action, kwargs))
            return {"success": True, "images": [{"file_path": str(tmp_path / "manual-selfie.png")}]}

    class _Metadata:
        def __init__(self, star_cls):
            self.star_cls = star_cls

    class _Context:
        def __init__(self):
            self.life = _LifePlugin()
            self.omnidraw = _OmniDrawPlugin()

        def get_registered_star(self, name):
            if name == "astrbot_plugin_life_scheduler":
                return _Metadata(self.life)
            if name == "astrbot_plugin_omnidraw":
                return _Metadata(self.omnidraw)
            return None

    class _Controller:
        async def publish_post(self, **kwargs):
            captured["publish"] = kwargs
            return {"fid": "fid-life-manual"}

    async def fake_ready(*args, **kwargs):
        captured["ready_args"] = args
        return None

    async def fake_daemon(*args, **kwargs):
        return None

    async def fake_generate_prompt(event, life_context):
        captured["prompt_event"] = event
        captured["life_context"] = life_context
        return "傍晚散步时的自然手机自拍"

    async def fake_caption(event, *, life_context, image_prompt):
        captured["caption_event"] = event
        return "晚风刚刚好。"

    async def fake_notify(post, payload, message):
        captured["notify"] = (post, payload, message)

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = PluginSettings.from_mapping(
        {
            "life_publish": {
                "enabled": True,
                "mode": "draft",
            }
        }
    )
    plugin.data_dir = tmp_path
    plugin._context = _Context()
    plugin.controller = _Controller()
    plugin._onebot_client = None
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_daemon
    plugin._generate_life_image_prompt = fake_generate_prompt
    plugin._generate_life_caption = fake_caption
    plugin._notify_admin_publish_result = fake_notify

    event = _Event()
    payload = asyncio.run(plugin._auto_life_publish_once(event, force_publish=True))

    assert payload["fid"] == "fid-life-manual"
    assert captured["prompt_event"] is event
    assert captured["caption_event"] is event
    selfie_event, action, kwargs = captured["selfie_calls"][0]
    assert selfie_event is event
    assert action == "傍晚散步时的自然手机自拍"
    assert kwargs["return_result"] is True
    assert captured["ready_args"][0] is event
    publish = captured["publish"]
    assert publish["content"] == "晚风刚刚好。"
    assert publish["media"][0]["source"] == str(tmp_path / "manual-selfie.png")


def test_manual_life_publish_command_invokes_full_publish_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Event:
        stopped = False

        def stop_event(self):
            self.stopped = True

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    async def fake_auto_life(event, *, force_publish=False):
        captured["event"] = event
        captured["force_publish"] = force_publish
        return {"fid": "fid-command"}

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin._auto_life_publish_once = fake_auto_life

    event = _Event()

    async def collect_results():
        return [item async for item in plugin.publish_life_feed_auto(event)]

    results = asyncio.run(collect_results())

    assert captured["event"] is event
    assert captured["force_publish"] is True
    assert event.stopped is True
    assert results[0]["type"] == "plain"
    assert "fid-command" in results[0]["text"]


def test_life_selfie_media_prefers_generate_selfie_return_result_method(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _OmniDrawPlugin:
        async def generate_selfie(self, *, action: str, return_result: bool = False, **kwargs):
            captured["generate_selfie"] = {"action": action, "return_result": return_result, **kwargs}
            return {
                "success": True,
                "images": [
                    {
                        "url": "https://example.invalid/selfie.png",
                        "file_path": str(tmp_path / "preferred.png"),
                    }
                ],
            }

        async def tool_generate_selfie(self, *args, **kwargs):
            captured["tool_generate_selfie"] = True
            return {"success": False, "images": []}

    class _Metadata:
        def __init__(self, star_cls):
            self.star_cls = star_cls

    class _Context:
        def get_registered_star(self, name):
            if name == "astrbot_plugin_omnidraw":
                return _Metadata(_OmniDrawPlugin())
            return None

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = PluginSettings.from_mapping(
        {
            "admin_uins": "123456",
            "life_publish": {
                "aspect_ratio": "9:16",
                "size": "1024x1792",
                "extra_params": "--quality high",
            },
        }
    )
    plugin._context = _Context()

    media, result = asyncio.run(plugin._generate_life_selfie_media(None, "雨天窗边自拍"))

    assert result["success"] is True
    assert captured["generate_selfie"]["action"] == "雨天窗边自拍"
    assert captured["generate_selfie"]["return_result"] is True
    assert captured["generate_selfie"]["aspect_ratio"] == "9:16"
    assert captured["generate_selfie"]["size"] == "1024x1792"
    assert captured["generate_selfie"]["extra_params"] == "--quality high"
    assert "tool_generate_selfie" not in captured
    assert media[0].source == str(tmp_path / "preferred.png")

def test_auto_life_publish_skip_policy_does_not_publish_when_omnidraw_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {"published": False}

    class _LifePlugin:
        async def get_life_context(self):
            return {"outfit": "休闲装", "schedule": "在家看书"}

    class _Metadata:
        def __init__(self, star_cls):
            self.star_cls = star_cls

    class _Context:
        def get_registered_star(self, name):
            if name == "astrbot_plugin_life_scheduler":
                return _Metadata(_LifePlugin())
            return None

    class _Controller:
        async def publish_post(self, **kwargs):
            captured["published"] = True
            return {"fid": "should-not-publish"}

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_generate_prompt(event, life_context):
        return "在家看书的自拍"

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = PluginSettings.from_mapping({"life_publish": {"enabled": True}})
    plugin.data_dir = tmp_path
    plugin._context = _Context()
    plugin.controller = _Controller()
    plugin._onebot_client = None
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._generate_life_image_prompt = fake_generate_prompt

    asyncio.run(plugin._auto_publish_once())

    assert captured["published"] is False

def test_auto_life_publish_text_only_policy_creates_draft_without_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = PluginSettings.from_mapping(
        {
            "life_publish": {
                "enabled": True,
                "mode": "draft",
                "failure_policy": "text_only",
                "auto_caption": False,
                "static_caption": "今天只写文字。",
            }
        }
    )
    plugin.data_dir = tmp_path
    plugin.drafts = main.DraftStore(tmp_path / "drafts.json")
    plugin._context = types.SimpleNamespace(get_registered_star=lambda name: None)
    plugin._onebot_client = None
    plugin._capture_onebot_client = lambda event=None: None
    plugin._render_markdown_image = lambda *args, **kwargs: None

    async def fake_prompt(event, life_context):
        return "无法生图但有提示词"

    plugin._generate_life_image_prompt = fake_prompt

    asyncio.run(plugin._auto_publish_once())

    draft = asyncio.run(plugin.drafts.latest_pending_async())
    assert draft is not None
    assert draft.content == "今天只写文字。"
    assert draft.media == []

def test_life_publish_settings_are_loaded_from_config() -> None:
    settings = PluginSettings.from_mapping(
        {
            "life_publish": {
                "enabled": True,
                "mode": "draft",
                "failure_policy": "text_only",
                "aspect_ratio": "9:16",
                "size": "1024x1792",
                "extra_params": "--seed 1",
                "static_caption": "今天也有好好生活。",
                "use_life_context": False,
                "use_llm_image_prompt": False,
                "use_omnidraw_selfie": False,
                "auto_caption": False,
            }
        }
    )

    assert settings.life_publish_enabled is True
    assert settings.life_publish_mode == "draft"
    assert settings.life_publish_failure_policy == "text_only"
    assert settings.life_publish_aspect_ratio == "9:16"
    assert settings.life_publish_size == "1024x1792"
    assert settings.life_publish_extra_params == "--seed 1"
    assert settings.life_publish_static_caption == "今天也有好好生活。"
    assert settings.life_publish_use_life_context is False
    assert settings.life_publish_use_llm_image_prompt is False
    assert settings.life_publish_use_omnidraw_selfie is False
    assert settings.life_publish_auto_caption is False

def test_life_publish_settings_reject_unknown_choices() -> None:
    settings = PluginSettings.from_mapping(
        {"life_publish": {"mode": "send_now", "failure_policy": "unknown"}}
    )

    assert settings.life_publish_mode == "publish"
    assert settings.life_publish_failure_policy == "skip"


def test_notify_admin_publish_result_sends_rendered_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Bot:
        def __init__(self):
            self.sent: list[dict[str, object]] = []

        def send_group_msg(self, *, group_id: int, message):
            self.sent.append({"group_id": group_id, "message": message})

    async def fake_profile(event=None, **kwargs):
        return main.RenderProfile(nickname="发布者", user_id="99999", avatar_source="", time_text="08:30")

    def fake_render(post, output_dir, *, profile=None, result=None, width=900, remote_timeout=0.35, fixed_width=False):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "scheduled-publish.png"
        path.write_bytes(b"png")
        captured["post"] = post
        captured["profile"] = profile
        captured["result"] = result
        captured["width"] = width
        return path

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        send_admin=True,
        manage_group=123456,
        admin_uins=[],
        render_publish_result=True,
        render_result_width=720,
        render_remote_timeout=0.01,
    )
    plugin.data_dir = tmp_path
    plugin._onebot_client = _Bot()
    plugin._context = None
    plugin._publisher_render_profile = fake_profile
    monkeypatch.setattr(main, "_render_publish_result_image", fake_render)

    post = main.PostPayload(content="定时发布内容", media=[])
    asyncio.run(plugin._notify_admin_publish_result(post, {"fid": "fid-published"}, "定时自动发布完成"))

    assert captured["post"] is post
    assert captured["result"]["fid"] == "fid-published"
    assert captured["profile"].nickname == "发布者"
    assert captured["width"] == 720
    assert plugin._onebot_client.sent[0]["group_id"] == 123456
    message = plugin._onebot_client.sent[0]["message"]
    assert message[0]["type"] == "text"
    assert "定时自动发布完成" in message[0]["data"]["text"]
    assert message[1]["type"] == "image"
    assert message[1]["data"]["file"].startswith("file:///")


def test_auto_comment_once_comments_configured_active_latest_posts_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {"comments": [], "notifications": [], "logs": []}
    login_uin = 99999

    entries = [
        main.FeedEntry(hostuin=11111, fid="fid-1", appid=311, summary="第一条好友动态", nickname="阿一"),
        main.FeedEntry(hostuin=login_uin, fid="fid-self", appid=311, summary="自己的动态", nickname="自己"),
        main.FeedEntry(hostuin=22222, fid="fid-2", appid=311, summary="第二条好友动态", nickname="阿二"),
        main.FeedEntry(hostuin=33333, fid="fid-3", appid=311, summary="已经处理过的动态", nickname="阿三"),
    ]

    class _Logger:
        def debug(self, *args, **kwargs): ...

        def warning(self, *args, **kwargs): ...

        def exception(self, *args, **kwargs): ...

        def info(self, message, *args, **kwargs):
            captured["logs"].append(message % args if args else str(message))

    class _Controller:
        async def list_feeds(self, *, hostuin=0, limit=5, cursor="", scope=""):
            captured["list_feeds"] = {"hostuin": hostuin, "limit": limit, "cursor": cursor, "scope": scope}
            return {"items": [asdict(entry) for entry in entries]}

        async def get_status(self, *, probe_daemon=False):
            return {"login_uin": login_uin}

        async def detail_feed(self, *, hostuin: int, fid: str, appid: int = 311):
            for entry in entries:
                if entry.hostuin == hostuin and entry.fid == fid:
                    return {"entry": asdict(entry), "comments": [], "raw": entry.raw}
            return {"entry": {}, "comments": [], "raw": {}}

    class _PostStore:
        async def upsert_async(self, post):
            captured.setdefault("stored", []).append(post.fid)

    class _PostService:
        async def comment_post(self, post, text):
            captured["comments"].append((post.hostuin, post.fid, text))
            return {"commentid": f"comment-{post.fid}"}

        async def like_post(self, post):
            captured.setdefault("likes", []).append(post.fid)
            return {"liked": True}

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_generate(event, post):
        return f"评论 {post.fid}"

    async def fake_notify(event, post, message, *, comment_text=""):
        captured["notifications"].append((post.fid, message, comment_text))

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        comment_latest_count=2,
        max_feed_limit=20,
        like_when_comment=True,
        send_admin=True,
    )
    plugin.data_dir = tmp_path
    plugin.controller = _Controller()
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._generate_comment_text = fake_generate
    plugin._post_store = lambda: _PostStore()
    plugin._post_service = lambda: _PostService()
    plugin._notify_admin_post_card = fake_notify
    (tmp_path / "auto_comment_state.json").write_text(
        '{"commented": ["33333:fid-3"]}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "logger", _Logger())

    asyncio.run(plugin._auto_comment_once())

    assert captured["list_feeds"]["hostuin"] == 0
    assert captured["list_feeds"]["scope"] == "active"
    assert captured["list_feeds"]["limit"] >= 2
    assert captured["comments"] == [
        (11111, "fid-1", "评论 fid-1"),
        (22222, "fid-2", "评论 fid-2"),
    ]
    assert captured["likes"] == ["fid-1", "fid-2"]
    assert captured["notifications"] == [
        ("fid-1", "定时自动评论了 阿一 的说说：评论 fid-1", "评论 fid-1"),
        ("fid-2", "定时自动评论了 阿二 的说说：评论 fid-2", "评论 fid-2"),
    ]
    saved = (tmp_path / "auto_comment_state.json").read_text(encoding="utf-8")
    assert "11111:fid-1" in saved
    assert "22222:fid-2" in saved
    assert "33333:fid-3" in saved
    assert "99999:fid-self" not in saved
    assert any("scheduled comment started" in item for item in captured["logs"])
    assert any("scheduled comment succeeded" in item and "commented=2" in item for item in captured["logs"])


def test_auto_comment_marks_comment_done_before_like_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {"comments": [], "logs": []}

    entry = main.FeedEntry(hostuin=11111, fid="fid-like-fails", appid=311, summary="好友动态", nickname="阿一")

    class _Logger:
        def debug(self, *args, **kwargs): ...

        def exception(self, *args, **kwargs): ...

        def info(self, message, *args, **kwargs):
            captured["logs"].append(message % args if args else str(message))

        def warning(self, message, *args, **kwargs):
            captured["logs"].append(message % args if args else str(message))

    class _Controller:
        async def list_feeds(self, *, hostuin=0, limit=5, cursor="", scope=""):
            return {"items": [asdict(entry)]}

        async def get_status(self, *, probe_daemon=False):
            return {"login_uin": 99999}

        async def detail_feed(self, *, hostuin: int, fid: str, appid: int = 311):
            return {"entry": asdict(entry), "comments": [], "raw": entry.raw}

    class _PostStore:
        async def upsert_async(self, post):
            return None

    class _PostService:
        async def comment_post(self, post, text):
            captured["comments"].append((post.hostuin, post.fid, text))
            return {"commentid": "comment-1"}

        async def like_post(self, post):
            raise RuntimeError("like failed")

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_generate(event, post):
        return "写得真好"

    async def fake_notify(event, post, message, *, comment_text=""):
        captured["notified"] = post.fid

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(comment_latest_count=1, max_feed_limit=20, like_when_comment=True)
    plugin.data_dir = tmp_path
    plugin.controller = _Controller()
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._generate_comment_text = fake_generate
    plugin._post_store = lambda: _PostStore()
    plugin._post_service = lambda: _PostService()
    plugin._notify_admin_post_card = fake_notify
    monkeypatch.setattr(main, "logger", _Logger())

    asyncio.run(plugin._auto_comment_once())

    assert captured["comments"] == [(11111, "fid-like-fails", "写得真好")]
    assert captured["notified"] == "fid-like-fails"
    saved = (tmp_path / "auto_comment_state.json").read_text(encoding="utf-8")
    assert "11111:fid-like-fails" in saved
    assert any("like failed after comment" in item for item in captured["logs"])

    asyncio.run(plugin._auto_comment_once())
    assert captured["comments"] == [(11111, "fid-like-fails", "写得真好")]


def test_auto_comment_skips_candidate_when_detail_check_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {"comments": [], "logs": []}

    entry = main.FeedEntry(hostuin=11111, fid="fid-detail-fails", appid=311, summary="好友动态", nickname="阿一")

    class _Logger:
        def debug(self, *args, **kwargs): ...

        def exception(self, *args, **kwargs): ...

        def info(self, message, *args, **kwargs):
            captured["logs"].append(message % args if args else str(message))

        def warning(self, message, *args, **kwargs):
            captured["logs"].append(message % args if args else str(message))

    class _Controller:
        async def list_feeds(self, *, hostuin=0, limit=5, cursor="", scope=""):
            return {"items": [asdict(entry)]}

        async def get_status(self, *, probe_daemon=False):
            return {"login_uin": 99999}

        async def detail_feed(self, *, hostuin: int, fid: str, appid: int = 311):
            raise RuntimeError("detail failed")

    class _PostService:
        async def comment_post(self, post, text):
            captured["comments"].append((post.hostuin, post.fid, text))
            return {"commentid": "comment-1"}

    async def fake_ready(*args, **kwargs):
        return None

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(comment_latest_count=1, max_feed_limit=20, like_when_comment=False)
    plugin.data_dir = tmp_path
    plugin.controller = _Controller()
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._post_service = lambda: _PostService()
    monkeypatch.setattr(main, "logger", _Logger())

    asyncio.run(plugin._auto_comment_once())

    assert captured["comments"] == []
    assert not (tmp_path / "auto_comment_state.json").exists()
    assert any("detail check failed" in item for item in captured["logs"])
    assert any("no eligible posts" in item for item in captured["logs"])


def test_auto_comment_once_skips_sensitive_posts_before_comment_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {"comments": [], "logs": []}
    entry = main.FeedEntry(
        hostuin=11111,
        fid="fid-sensitive",
        appid=311,
        summary="\u4f4f\u9662\u624b\u672f",
        nickname="tester",
    )

    class _Logger:
        def debug(self, *args, **kwargs): ...

        def exception(self, *args, **kwargs): ...

        def info(self, message, *args, **kwargs):
            captured["logs"].append(message % args if args else str(message))

        def warning(self, message, *args, **kwargs):
            captured["logs"].append(message % args if args else str(message))

    class _Controller:
        async def list_feeds(self, *, hostuin=0, limit=5, cursor="", scope=""):
            return {"items": [asdict(entry)]}

        async def get_status(self, *, probe_daemon=False):
            return {"login_uin": 99999}

        async def detail_feed(self, *, hostuin: int, fid: str, appid: int = 311):
            return {"entry": asdict(entry), "comments": [], "raw": entry.raw}

    class _PostStore:
        async def upsert_async(self, post):
            return None

    class _PostService:
        async def comment_post(self, post, text):
            captured["comments"].append((post.hostuin, post.fid, text))

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_generate(event, post):
        raise AssertionError("sensitive post should be skipped before comment generation")

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(comment_latest_count=1, max_feed_limit=20, like_when_comment=False)
    plugin.data_dir = tmp_path
    plugin.controller = _Controller()
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._generate_comment_text = fake_generate
    plugin._post_store = lambda: _PostStore()
    plugin._post_service = lambda: _PostService()
    monkeypatch.setattr(main, "logger", _Logger())

    asyncio.run(plugin._auto_comment_once())

    assert captured["comments"] == []
    assert not (tmp_path / "auto_comment_state.json").exists()
    assert any("serious_or_sensitive_context" in item for item in captured["logs"])


def test_active_feed_scope_uses_home_timeline_without_defaulting_items_to_login_uin() -> None:
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.models import BridgeState

    cached: list[tuple[int, list[object]]] = []

    class _Client:
        async def index(self):
            return {
                "feedpage": {
                    "vFeeds": [
                        {
                            "fid": "fid-friend",
                            "appid": 311,
                            "content": "好友动态",
                            "userinfo": {"uin": 22222, "nickname": "好友"},
                        }
                    ]
                }
            }

        async def get_active_feeds(self, attach_info=""):
            raise AssertionError("first active page should use index()")

        def cache_feed_page(self, hostuin, items):
            cached.append((hostuin, items))

    service = object.__new__(QzoneDaemonService)
    service.state = BridgeState(session=SessionState(uin=99999, cookies={"p_skey": "x"}))
    service.client = _Client()
    service.recent_feed_entries = []
    service._ensure_session_ready = lambda: None

    payload = asyncio.run(service.list_feeds(hostuin=0, limit=1, scope="active"))

    assert payload["scope"] == "active"
    assert payload["hostuin"] == 99999
    assert payload["items"][0]["hostuin"] == 22222
    assert cached[0][0] == 0


def test_render_feed_card_limit_is_loaded_from_config() -> None:
    settings = PluginSettings.from_mapping({"render_feed_card_limit": 3})
    assert settings.render_feed_card_limit == 3


def test_page_status_profile_fetch_is_independent_from_publish_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Bot:
        async def get_stranger_info(self, **kwargs):
            return {"nickname": "Tester", "avatar": "https://example.test/avatar.png"}

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(render_publish_result=False, render_remote_timeout=0.01)
    plugin._publisher_profile_cache = None
    plugin._publisher_profile_preload_task = None
    plugin._onebot_client = _Bot()
    plugin._context = None

    enriched = asyncio.run(plugin._status_with_live_profile({"login_uin": 10001, "cookie_count": 2}))

    assert enriched["login_nickname"] == "Tester"
    assert enriched["login_avatar"] == "https://example.test/avatar.png"
    assert plugin._cached_profile_has_display_name(10001) is True


def test_comment_latest_count_is_loaded_from_trigger_config() -> None:
    settings = PluginSettings.from_mapping({"trigger": {"comment_latest_count": 3}})
    assert settings.comment_latest_count == 3


def test_auto_comment_pipeline_config_is_loaded_from_webui_schema_mapping() -> None:
    settings = PluginSettings.from_mapping(
        {
            "llm": {
                "comment_pipeline_enabled": False,
                "comment_judgment_provider_id": "judge-provider",
                "comment_reasoning_provider_id": "reason-provider",
                "comment_execution_provider_id": "execute-provider",
                "comment_skip_checkins": False,
            }
        }
    )

    assert settings.comment_pipeline_enabled is False
    assert settings.comment_judgment_provider_id == "judge-provider"
    assert settings.comment_reasoning_provider_id == "reason-provider"
    assert settings.comment_execution_provider_id == "execute-provider"
    assert settings.comment_skip_checkins is False


def test_standard_data_dir_falls_back_to_plugin_data_and_migrates_auto_comment_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    plugin_root = tmp_path / "plugin"
    legacy_dir = plugin_root / "data" / "qzone"
    legacy_dir.mkdir(parents=True)
    (plugin_root / "metadata.yaml").write_text("name: astrbot_plugin_qzone_ultra\n", encoding="utf-8")
    (legacy_dir / "auto_comment_state.json").write_text('{"commented":["111:fid"]}\n', encoding="utf-8")

    monkeypatch.setattr(main, "_star_tools_data_dir", lambda plugin_name: None)

    data_dir = main._standard_data_dir(plugin_root)

    assert data_dir == plugin_root / "data" / "plugin_data" / "astrbot_plugin_qzone_ultra"
    assert (data_dir / "auto_comment_state.json").read_text(encoding="utf-8") == '{"commented":["111:fid"]}\n'
    assert (data_dir / ".legacy-qzone-migration.json").exists()


def test_auto_comment_state_store_uses_atomic_json_payload(tmp_path: Path) -> None:
    store = AutoCommentStateStore(tmp_path / "auto_comment_state.json", max_items=2)

    store.write_keys({"333:fid-3", "111:fid-1", "222:fid-2"})

    assert store.read_keys() == {"222:fid-2", "333:fid-3"}
    saved = (tmp_path / "auto_comment_state.json").read_text(encoding="utf-8")
    assert "commented" in saved
    assert "111:fid-1" not in saved


def test_auto_comment_pipeline_runs_judgment_reasoning_and_execution() -> None:
    post = QzonePost(hostuin=11111, fid="fid-1", summary="normal day", nickname="tester")
    calls: list[tuple[str, str]] = []
    pipeline = AutoCommentPipeline(
        AutoCommentPipelineConfig(
            judgment_provider_id="judge-provider",
            reasoning_provider_id="reason-provider",
            execution_provider_id="execute-provider",
        )
    )

    async def generate_text(prompt: str, provider_id: str, system_prompt: str) -> str:
        calls.append((provider_id, prompt))
        if provider_id == "judge-provider":
            return '{"action":"comment","reason":"safe"}'
        return "friendly classmate tone"

    async def execute_comment(reasoning: str) -> str:
        calls.append(("execute", reasoning))
        return "Looks nice"

    result = asyncio.run(
        pipeline.run(
            post,
            generate_text=generate_text,
            execute_comment=execute_comment,
        )
    )

    assert result.should_comment is True
    assert result.comment_text == "Looks nice"
    assert calls[0][0] == "judge-provider"
    assert calls[1][0] == "reason-provider"
    assert calls[2] == ("execute", "friendly classmate tone")


def test_auto_comment_pipeline_skips_sensitive_context_before_execution() -> None:
    post = QzonePost(hostuin=11111, fid="fid-1", summary="\u4f4f\u9662\u624b\u672f", nickname="tester")

    async def generate_text(prompt: str, provider_id: str, system_prompt: str) -> str:
        raise AssertionError("judgment provider should not be called for heuristic skip")

    async def execute_comment(reasoning: str) -> str:
        raise AssertionError("execution should not run for heuristic skip")

    result = asyncio.run(
        AutoCommentPipeline(AutoCommentPipelineConfig()).run(
            post,
            generate_text=generate_text,
            execute_comment=execute_comment,
        )
    )

    assert result.should_comment is False
    assert result.skip_reason == "serious_or_sensitive_context"


def test_disabled_auto_comment_pipeline_keeps_legacy_direct_generation() -> None:
    post = QzonePost(hostuin=11111, fid="fid-1", summary="\u4f4f\u9662\u624b\u672f", nickname="tester")

    async def generate_text(prompt: str, provider_id: str, system_prompt: str) -> str:
        raise AssertionError("disabled pipeline should not call judgment or reasoning providers")

    async def execute_comment(reasoning: str) -> str:
        assert reasoning == ""
        return "legacy direct comment"

    result = asyncio.run(
        AutoCommentPipeline(AutoCommentPipelineConfig(enabled=False)).run(
            post,
            generate_text=generate_text,
            execute_comment=execute_comment,
        )
    )

    assert result.should_comment is True
    assert result.comment_text == "legacy direct comment"
    assert result.judgment == "disabled"


def test_news_settings_are_loaded_from_config() -> None:
    settings = PluginSettings.from_mapping(
        {
            "llm": {"news_provider_id": "news-provider", "news_prompt": "写原创新闻短评"},
            "trigger": {"news_cron": "30 8 * * *", "news_offset": 60},
            "news": {
                "scopes": ["china", "international"],
                "keywords": ["科技"],
                "custom_rss_urls": ["https://news.google.com/rss/search?q=test"],
                "max_candidates": 8,
                "recency_hours": 24,
                "once_per_day": False,
                "max_post_length": 120,
                "trust_env": True,
            },
        }
    )

    assert settings.news_provider_id == "news-provider"
    assert settings.news_prompt == "写原创新闻短评"
    assert settings.news_cron == "30 8 * * *"
    assert settings.news_offset == 60
    assert settings.news_scopes == ["china", "world"]
    assert settings.news_keywords == ["科技"]
    assert settings.news_custom_rss_urls == ["https://news.google.com/rss/search?q=test"]
    assert settings.news_max_candidates == 8
    assert settings.news_recency_hours == 24
    assert settings.news_once_per_day is False
    assert settings.news_max_post_length == 120
    assert settings.news_trust_env is True
    assert PluginSettings.from_mapping({}).news_trust_env is True


def test_conf_schema_user_facing_config_text_is_chinese() -> None:
    schema_text = Path("_conf_schema.json").read_text(encoding="utf-8")
    for fragment in (
        "Auto-comment",
        "Local daemon",
        "Keepalive interval",
        "Request timeout",
        "Startup timeout",
        "Default feed limit",
        "Max feed limit",
        "Auto start daemon",
        "Auto bind cookie",
        "Admin QQ numbers",
        "Custom user-agent",
        "Render publish result image",
        "Publish result image width",
        "Feed card render limit",
        "Publish result remote image timeout",
    ):
        assert fragment not in schema_text


def test_start_scheduled_tasks_can_add_news_after_existing_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    async def run_case():
        blocker = asyncio.Event()
        started: list[tuple[str, str, int]] = []

        async def fake_scheduled_loop(name, cron, offset, action):
            started.append((name, cron, offset))
            await blocker.wait()

        plugin = object.__new__(main.QzoneStablePlugin)
        plugin.settings = types.SimpleNamespace(
            publish_cron="0 8 * * *",
            publish_offset=0,
            news_cron="",
            news_offset=0,
            comment_cron="",
            comment_offset=0,
        )
        plugin._scheduled_tasks = []
        plugin._scheduled_loop = fake_scheduled_loop

        plugin._start_scheduled_tasks()
        await asyncio.sleep(0)
        assert started == [("publish", "0 8 * * *", 0)]

        plugin.settings.news_cron = "30 8 * * *"
        plugin.settings.news_offset = 60
        plugin._start_scheduled_tasks()
        await asyncio.sleep(0)

        assert started == [("publish", "0 8 * * *", 0), ("news", "30 8 * * *", 60)]

        plugin._start_scheduled_tasks()
        await asyncio.sleep(0)
        assert started == [("publish", "0 8 * * *", 0), ("news", "30 8 * * *", 60)]

        blocker.set()
        await asyncio.gather(*plugin._scheduled_tasks, return_exceptions=True)

    asyncio.run(run_case())


def test_google_news_rss_parser_cleans_titles_and_sources() -> None:
    from qzone_bridge.news import parse_google_news_rss

    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel><item>
      <title>航天员返回地球后最想洗头 - 羊城晚报</title>
      <link>https://news.google.com/rss/articles/example</link>
      <source url="https://example.com">羊城晚报</source>
      <pubDate>Sat, 30 May 2026 03:43:05 GMT</pubDate>
    </item></channel></rss>
    """

    items = parse_google_news_rss(xml, scope="china")

    assert len(items) == 1
    assert items[0].title == "航天员返回地球后最想洗头"
    assert items[0].source == "羊城晚报"
    assert items[0].link == "https://news.google.com/rss/articles/example"
    assert items[0].published_at > 0
    assert items[0].scope == "china"
    assert items[0].item_id


def test_news_copy_like_detection_rejects_titles() -> None:
    from qzone_bridge.news import NewsItem, is_news_copy_like

    items = [NewsItem(title="航天员返回地球后最想洗头", source="羊城晚报")]

    assert is_news_copy_like("航天员返回地球后最想洗头", items)
    assert not is_news_copy_like("人在太空待久了，回到地面第一件小事都能变成很具体的幸福。", items)


def test_news_candidates_default_to_trust_env_true(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, *, timeout: float, user_agent: str, trust_env: bool) -> None:
            captured["timeout"] = timeout
            captured["user_agent"] = user_agent
            captured["trust_env"] = trust_env

        async def fetch_items(self, urls):
            captured["urls"] = urls
            return []

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.data_dir = tmp_path
    plugin.settings = types.SimpleNamespace(
        request_timeout=15.0,
        user_agent="UA",
        news_keywords=[],
        news_custom_rss_urls=[],
        news_recency_hours=36,
        news_max_candidates=12,
        news_scopes=["china"],
    )
    monkeypatch.setattr(main, "GoogleNewsRSSClient", _Client)

    result = asyncio.run(plugin._news_candidates(seen_ids=set()))

    assert result == []
    assert captured["trust_env"] is True


def test_public_error_text_includes_news_fetch_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    plugin = object.__new__(main.QzoneStablePlugin)
    exc = main.QzoneBridgeError(
        "Google News RSS 获取失败",
        detail={
            "trust_env": False,
            "errors": [
                {
                    "message": "ConnectError: [Errno 11001] getaddrinfo failed",
                    "url": "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
                }
            ],
        },
    )

    text = plugin._error_text(exc)

    assert "Google News RSS 获取失败" in text
    assert "未使用系统代理" in text
    assert "ConnectError" in text


def test_qzone_post_nickname_prefers_matching_owner_and_never_briefs_qq_number() -> None:
    from qzone_bridge.models import FeedEntry
    from qzone_bridge.social import QzoneComment, QzonePost, extract_nickname, post_from_entry

    raw = {
        "userinfo": {"uin": 22222, "nickname": "错误昵称"},
        "owner": {"uin": 12345, "nickname": "正确昵称"},
        "name": "泛字段昵称",
    }
    assert extract_nickname(raw, hostuin=12345) == "正确昵称"

    post = QzonePost(hostuin=12345, fid="fid-1", summary="没有昵称时不要露出 QQ 号", nickname="12345")
    text = post.brief(1)
    assert "12345" not in text
    assert "QQ 空间用户" in text

    nested_owner = {
        "cell_userinfo": {
            "12345": {"uin": 12345, "nick": "实际昵称"},
            "22222": {"uin": 22222, "nick": "别人昵称"},
        }
    }
    assert extract_nickname(nested_owner, hostuin=12345) == "实际昵称"
    assert extract_nickname({"cellUserInfo": {"12345": {"nick": "驼峰昵称"}}}, hostuin=12345) == "驼峰昵称"
    assert extract_nickname({"userMap": {"12345": {"nickname": "映射昵称"}}}, hostuin=12345) == "映射昵称"
    assert extract_nickname({"profileMap": [{"nickname": "评论者"}, {"uin": 12345, "nickname": "主人"}]}, hostuin=12345) == "主人"
    assert extract_nickname({"users": [{"nickname": "评论者"}, {"uin": 22222, "nickname": "别人"}]}, hostuin=12345) == ""

    entry = FeedEntry(
        hostuin=12345,
        fid="fid-2",
        appid=311,
        summary="详情里没有昵称，但列表 raw 里有",
        nickname="12345",
        raw=nested_owner,
    )
    detailed = post_from_entry(entry, detail={"content": "detail payload without owner nickname"}, local_id=1)
    assert detailed.nickname == "实际昵称"

    comment_text = QzoneComment(commentid="c1", uin=22334455, nickname="", content="空昵称评论").brief(1)
    assert "22334455" not in comment_text
    assert "QQ 空间用户" in comment_text


def test_default_feed_page_owner_context_fills_missing_nickname() -> None:
    from qzone_bridge.parser import extract_feed_page

    payload = {
        "info": {"uin": 12345, "nickname": "默认昵称"},
        "feedpage": {
            "vFeeds": [
                {
                    "fid": "fid-default",
                    "summary": {"summary": "默认读说说应该有昵称"},
                }
            ]
        },
    }
    _feedpage, entries = extract_feed_page(payload, default_hostuin=12345)

    assert len(entries) == 1
    assert entries[0].hostuin == 12345
    assert entries[0].nickname == "默认昵称"


def test_default_feed_page_skips_numeric_info_nickname_for_owner_context() -> None:
    from qzone_bridge.parser import extract_feed_page

    payload = {
        "info": {"uin": 12345, "nickname": "12345"},
        "ownerInfo": {"uin": 12345, "nickname": "真实昵称"},
        "feedpage": {
            "vFeeds": [
                {
                    "fid": "fid-default",
                    "summary": {"summary": "默认读说说不能把 QQ 号当昵称"},
                }
            ]
        },
    }
    _feedpage, entries = extract_feed_page(payload, default_hostuin=12345)

    assert len(entries) == 1
    assert entries[0].nickname == "真实昵称"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"time": 1_690_000_000}, 1_690_000_000),
        ({"created_at": 1_690_000_000}, 1_690_000_000),
        ({"createdTime": 1_690_000_000}, 1_690_000_000),
        ({"createTime": 1_690_000_000}, 1_690_000_000),
        ({"pubtime": 1_690_000_000}, 1_690_000_000),
        ({"common": {"timestamp": 1_690_000_000_000}}, 1_690_000_000),
        ({"common": {"date": 1_690_000_000}}, 1_690_000_000),
    ],
)
def test_feed_entry_extracts_common_qzone_time_aliases(payload: dict[str, object], expected: int) -> None:
    from qzone_bridge.parser import extract_feed_entry

    payload = {
        "hostuin": 12345,
        "fid": "fid-time-alias",
        "summary": "发布时间别名",
        **payload,
    }
    entry = extract_feed_entry(
        payload,
        default_hostuin=12345,
    )

    assert entry.created_at == expected


def test_feed_entry_ignores_unreasonable_generic_timestamps() -> None:
    from qzone_bridge.parser import extract_feed_entry

    entry = extract_feed_entry(
        {
            "hostuin": 12345,
            "fid": "fid-invalid-timestamp",
            "summary": "异常时间戳",
            "timestamp": 99_999_999_999_999_999,
        },
        default_hostuin=12345,
    )

    assert entry.created_at == 0


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"data": {"feedsTime": 1_690_000_001_000}}, 1_690_000_001),
        ({"cell_comm": {"opertime": 1_690_000_002}}, 1_690_000_002),
        ({"original": {"uploadTime": "1690000003000"}}, 1_690_000_003),
        ({"timestamp": 1_690_000_004}, 1_690_000_004),
    ],
)
def test_feed_entry_extracts_nested_qzone_time_aliases(payload: dict[str, object], expected: int) -> None:
    from qzone_bridge.parser import extract_feed_entry

    entry = extract_feed_entry(
        {
            "hostuin": 12345,
            "fid": "fid-nested-time-alias",
            "summary": "time alias",
            **payload,
        },
        default_hostuin=12345,
    )

    assert entry.created_at == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"timeStr": "2026-05-20 13:45:06"},
            int(datetime(2026, 5, 20, 13, 45, 6).timestamp()),
        ),
        (
            {"feedstimeText": "2026-05-20 13:45:07"},
            int(datetime(2026, 5, 20, 13, 45, 7).timestamp()),
        ),
        (
            {"data": {"pubtimeText": "2026年5月20日 13:45"}},
            int(datetime(2026, 5, 20, 13, 45).timestamp()),
        ),
        ({"html": '<div data-abstime=1690000000>图文说说</div>'}, 1_690_000_000),
        ({"htmlContent": "<div data-abstime=1690000001>图文说说</div>"}, 1_690_000_001),
        ({"contentHtml": '<div timestamp="1690000002">图文说说</div>'}, 1_690_000_002),
    ],
)
def test_feed_entry_extracts_real_qzone_textual_time_aliases(payload: dict[str, object], expected: int) -> None:
    from qzone_bridge.parser import extract_feed_entry

    entry = extract_feed_entry(
        {
            "hostuin": 12345,
            "fid": "fid-real-time-alias",
            "summary": "真实发布时间",
            **payload,
        },
        default_hostuin=12345,
    )

    assert entry.created_at == expected


def test_post_from_entry_preserves_feed_images_when_detail_omits_media() -> None:
    from qzone_bridge.models import FeedEntry
    from qzone_bridge.social import post_from_entry

    entry = FeedEntry(
        hostuin=12345,
        fid="fid-feed-image",
        appid=311,
        summary="detail text",
        nickname="viewer",
        created_at=1_690_000_000,
        raw={
            "summary": "list text",
            "pic": [
                {
                    "url1": "https://qzone.example.test/list-image.jpg",
                }
            ],
        },
    )

    post = post_from_entry(
        entry,
        detail={"summary": "detail text"},
        fallback_raw=entry.raw,
        local_id=1,
    )

    assert post.images == ["https://qzone.example.test/list-image.jpg"]


def test_post_from_entry_extracts_nested_qzone_image_aliases() -> None:
    from qzone_bridge.models import FeedEntry
    from qzone_bridge.social import post_from_entry

    entry = FeedEntry(
        hostuin=12345,
        fid="fid-nested-image",
        appid=311,
        summary="image aliases",
        raw={
            "cell_pic": {
                "photoList": [
                    {"originUrl": "https://qzone.example.test/origin.jpg"},
                    {"pre": "https://qzone.example.test/preview.jpg"},
                ]
            },
            "media": [{"smallUrl": "https://qzone.example.test/small.jpg"}],
        },
    )

    post = post_from_entry(entry, local_id=1)

    assert post.images == [
        "https://qzone.example.test/origin.jpg",
        "https://qzone.example.test/preview.jpg",
        "https://qzone.example.test/small.jpg",
    ]


def test_extract_images_handles_real_qzone_protocol_relative_sources() -> None:
    from qzone_bridge.social import extract_images

    payload = {
        "picdata": {
            "0": {"url1": "//m.qpic.cn/feed-a.jpg"},
            "1": {"smallurl": "https://qzone.example.test/feed-b.jpg"},
        },
        "cell_pic": '{"photoList":[{"originUrl":"//qzonestyle.gtimg.cn/feed-c.jpg"}]}',
        "html": (
            '<div><img src="//m.qpic.cn/feed-d.jpg">'
            '<img data-src="https://qzone.example.test/feed-e.jpg"></div>'
        ),
    }

    assert extract_images(payload) == [
        "https://m.qpic.cn/feed-a.jpg",
        "https://qzone.example.test/feed-b.jpg",
        "https://qzonestyle.gtimg.cn/feed-c.jpg",
        "https://m.qpic.cn/feed-d.jpg",
        "https://qzone.example.test/feed-e.jpg",
    ]


def test_extract_images_scans_textual_html_feed_fields() -> None:
    from qzone_bridge.social import extract_images

    payload = {
        "content": (
            '<img srcset="//m.qpic.cn/feed-f.jpg 1x, '
            'https://qzone.example.test/feed-g.jpg 2x">'
        ),
        "summary": (
            '<span style="background:url(//qzonestyle.gtimg.cn/feed-h.jpg)">'
            "图文说说</span>"
        ),
    }

    assert extract_images(payload) == [
        "https://m.qpic.cn/feed-f.jpg",
        "https://qzone.example.test/feed-g.jpg",
        "https://qzonestyle.gtimg.cn/feed-h.jpg",
    ]


def test_extract_images_ignores_unsafe_sources_and_handles_cycles() -> None:
    from qzone_bridge.social import extract_images

    cyclic: list[object] = []
    cyclic.append(cyclic)
    payload = {
        "images": [
            "base64://not-from-qzone-feed",
            "data:image/png;base64,AAAA",
            "not a url",
            "file:///tmp/not-remote.png",
            "https://qzone.example.test/ok.jpg",
            cyclic,
        ],
    }

    assert extract_images(payload) == ["https://qzone.example.test/ok.jpg"]


def test_extract_images_collapses_aliases_from_one_qzone_photo_object() -> None:
    from qzone_bridge.social import extract_images

    payload = {
        "picdata": {
            "0": {
                "url1": "https://m.qpic.cn/one-photo-small.jpg",
                "url2": "https://m.qpic.cn/one-photo-large.jpg",
                "url3": "https://m.qpic.cn/one-photo-original.jpg",
                "smallurl": "https://qzone.example.test/one-photo-thumb.jpg",
            }
        }
    }

    assert extract_images(payload) == ["https://m.qpic.cn/one-photo-original.jpg"]


def test_extract_images_collapses_real_qzone_picdata_variants_by_photo_identity() -> None:
    from qzone_bridge.social import extract_images

    payload = {
        "cell_pic": {
            "picdata": [
                {
                    "albumid": "album-a",
                    "lloc": "photo-a",
                    "sloc": "https://qzone.example.test/photo-a-small.jpg",
                    "photourl": {
                        "0": {"url": "https://qzone.example.test/photo-a-original.jpg", "width": 1080, "height": 1935},
                        "1": {"url": "https://qzone.example.test/photo-a-large.jpg", "width": 1080, "height": 1935},
                        "11": {"url": "https://qzone.example.test/photo-a-thumb.jpg", "width": 400, "height": 716},
                    },
                }
            ]
        }
    }

    assert extract_images(payload) == ["https://qzone.example.test/photo-a-large.jpg"]


def test_post_from_entry_deduplicates_real_msglist_detail_photo() -> None:
    from qzone_bridge.models import FeedEntry
    from qzone_bridge.social import post_from_entry

    entry = FeedEntry(
        hostuin=12345,
        fid="fid-photo",
        appid=311,
        summary="photo",
        raw={
            "tid": "fid-photo",
            "pic": [
                {
                    "pic_id": ",album-a,photo-a",
                    "url1": "https://qzone.example.test/photo-a-small.jpg",
                    "url2": "https://qzone.example.test/photo-a-large.jpg",
                    "url3": "https://qzone.example.test/photo-a-original.jpg",
                }
            ],
        },
    )

    post = post_from_entry(
        entry,
        detail={
            "cell_id": {"cellid": "fid-photo"},
            "cell_pic": {
                "picdata": [
                    {
                        "albumid": "album-a",
                        "lloc": "photo-a",
                        "photourl": {
                            "1": {"url": "https://qzone.example.test/photo-a-large.jpg"},
                            "11": {"url": "https://qzone.example.test/photo-a-thumb.jpg"},
                        },
                    }
                ]
            },
        },
        fallback_raw=entry.raw,
        local_id=1,
    )

    assert post.images == ["https://qzone.example.test/photo-a-large.jpg"]


def test_extract_images_keeps_current_photo_when_photo_has_storage_key() -> None:
    from qzone_bridge.social import extract_images

    payload = {
        "fid": "fid-with-image",
        "hostuin": 12345,
        "picdata": {
            "0": {
                "key": "photo-storage-key",
                "url3": "https://m.qpic.cn/current-photo.jpg",
            }
        },
    }

    assert extract_images(payload, fid="fid-with-image", hostuin=12345) == [
        "https://m.qpic.cn/current-photo.jpg"
    ]


def test_post_from_entry_scopes_detail_images_to_current_fid() -> None:
    from qzone_bridge.models import FeedEntry
    from qzone_bridge.social import post_from_entry

    entry = FeedEntry(
        hostuin=12345,
        fid="fid-text-only",
        appid=311,
        summary="hello",
        raw={"fid": "fid-text-only", "summary": "hello"},
    )
    detail_payload_with_neighbor_feed = {
        "fid": "fid-text-only",
        "summary": "hello",
        "data": [
            {"fid": "fid-text-only", "summary": "hello"},
            {
                "fid": "fid-with-image",
                "summary": "想我吗",
                "pic": [{"url3": "https://m.qpic.cn/neighbor-image.jpg"}],
            },
        ],
    }

    post = post_from_entry(entry, detail=detail_payload_with_neighbor_feed, local_id=2)

    assert post.images == []


def test_post_from_entry_scopes_json_cell_comm_neighbor_images_to_current_fid() -> None:
    from qzone_bridge.models import FeedEntry
    from qzone_bridge.social import post_from_entry

    entry = FeedEntry(
        hostuin=12345,
        fid="fid-text-only",
        appid=311,
        summary="hello",
        raw={"fid": "fid-text-only", "summary": "hello"},
    )
    detail_payload_with_neighbor_feed = {
        "fid": "fid-text-only",
        "summary": "hello",
        "feed": [
            {"cell_comm": '{"fid":"fid-text-only"}', "summary": "hello"},
            {
                "cell_comm": '{"fid":"fid-with-image"}',
                "summary": "想我吗",
                "pic": [{"url3": "https://m.qpic.cn/neighbor-json-cell.jpg"}],
            },
        ],
    }

    post = post_from_entry(entry, detail=detail_payload_with_neighbor_feed, local_id=2)

    assert post.images == []


def test_extract_feed_entry_reads_time_from_json_cell_comm() -> None:
    from qzone_bridge.parser import extract_feed_entry

    entry = extract_feed_entry(
        {
            "fid": "fid-json-time",
            "hostuin": 12345,
            "summary": "图文说说",
            "cell_comm": '{"abstime":1690000000}',
        }
    )

    assert entry.created_at == 1_690_000_000


def test_extract_feed_entry_reads_real_msglist_comm_time() -> None:
    from qzone_bridge.parser import extract_feed_entry

    entry = extract_feed_entry(
        {
            "id": {"cellid": "fid-msglist"},
            "comm": {
                "appid": 311,
                "time": 1_779_489_120,
                "ugckey": "12345_311_fid-msglist_",
                "ugcrightkey": "fid-msglist",
            },
            "summary": {"summary": "msglist text"},
        },
        default_hostuin=12345,
    )

    assert entry.fid == "fid-msglist"
    assert entry.appid == 311
    assert entry.created_at == 1_779_489_120
    assert entry.summary == "msglist text"


def test_extract_feed_entry_reads_real_shuoshuo_detail_cell_fields() -> None:
    from qzone_bridge.parser import extract_feed_entry

    entry = extract_feed_entry(
        {
            "cell_id": {"cellid": "fid-detail"},
            "cell_comm": {
                "appid": 311,
                "time": 1_779_489_121,
                "ugckey": "12345_311_fid-detail_",
                "ugcrightkey": "fid-detail",
            },
            "cell_summary": {"summary": "detail text"},
        },
        default_hostuin=12345,
    )

    assert entry.fid == "fid-detail"
    assert entry.appid == 311
    assert entry.created_at == 1_779_489_121
    assert entry.summary == "detail text"


def test_detail_post_keeps_feed_raw_nickname_when_detail_omits_owner(tmp_path: Path) -> None:
    from qzone_bridge.models import FeedEntry
    from qzone_bridge.post_service import QzonePostService

    class _Controller:
        async def detail_feed(self, *, hostuin: int, fid: str, appid: int = 311):
            return {
                "entry": {
                    "hostuin": hostuin,
                    "fid": fid,
                    "appid": appid,
                    "summary": "详情内容",
                    "nickname": "",
                    "raw": {"summary": "详情内容"},
                },
                "raw": {"summary": "详情内容"},
                "comments": [],
            }

    entry = FeedEntry(
        hostuin=12345,
        fid="fid-default",
        appid=311,
        summary="列表内容",
        raw={"owner": {"uin": 12345, "nickname": "列表昵称"}},
    )
    service = QzonePostService(_Controller(), types.SimpleNamespace(), max_feed_limit=20)

    post = asyncio.run(service._detail_post(entry, local_id=1, required=True))

    assert post.nickname == "列表昵称"
    assert "QQ 空间用户" not in post.brief(1)


def test_detail_post_preserves_feed_created_at_when_detail_omits_time(tmp_path: Path) -> None:
    from qzone_bridge.models import FeedEntry
    from qzone_bridge.post_service import QzonePostService

    class _Controller:
        async def detail_feed(self, *, hostuin: int, fid: str, appid: int = 311):
            return {
                "entry": {
                    "hostuin": hostuin,
                    "fid": fid,
                    "appid": appid,
                    "summary": "详情内容",
                    "nickname": "列表昵称",
                    "raw": {"summary": "详情内容"},
                },
                "raw": {"summary": "详情内容"},
                "comments": [],
            }

    entry = FeedEntry(
        hostuin=12345,
        fid="fid-time",
        appid=311,
        summary="列表内容",
        nickname="列表昵称",
        created_at=1_690_000_000,
    )
    service = QzonePostService(_Controller(), types.SimpleNamespace(), max_feed_limit=20)

    post = asyncio.run(service._detail_post(entry, local_id=1, required=True))

    assert post.created_at == 1_690_000_000


def test_detail_post_preserves_feed_images_when_detail_omits_media(tmp_path: Path) -> None:
    from qzone_bridge.models import FeedEntry
    from qzone_bridge.post_service import QzonePostService

    class _Controller:
        async def detail_feed(self, *, hostuin: int, fid: str, appid: int = 311):
            return {
                "entry": {
                    "hostuin": hostuin,
                    "fid": fid,
                    "appid": appid,
                    "summary": "detail text",
                    "nickname": "list nickname",
                    "raw": {"summary": "detail text"},
                },
                "raw": {"summary": "detail text"},
                "comments": [],
            }

    entry = FeedEntry(
        hostuin=12345,
        fid="fid-detail-image",
        appid=311,
        summary="list text",
        nickname="list nickname",
        created_at=1_690_000_000,
        raw={"pic": [{"url1": "https://qzone.example.test/feed-image.jpg"}]},
    )
    service = QzonePostService(_Controller(), types.SimpleNamespace(), max_feed_limit=20)

    post = asyncio.run(service._detail_post(entry, local_id=1, required=True))

    assert post.images == ["https://qzone.example.test/feed-image.jpg"]


def test_client_detail_payload_preserves_cached_created_at_when_detail_omits_time() -> None:
    from qzone_bridge.models import FeedEntry

    client = QzoneClient(SessionState(uin=12345, cookies={}))
    try:
        client.feed_cache[(12345, "fid-cached")] = FeedEntry(
            hostuin=12345,
            fid="fid-cached",
            appid=311,
            summary="列表内容",
            nickname="列表昵称",
            created_at=1_690_000_000,
            curkey="cached-curkey",
            unikey="cached-unikey",
        )

        entry = client.feed_entry_from_payload(
            {
                "hostuin": 12345,
                "fid": "fid-cached",
                "summary": "详情内容",
                "nickname": "详情昵称",
            },
            default_hostuin=12345,
        )
    finally:
        asyncio.run(client.close())

    assert entry.created_at == 1_690_000_000
    assert entry.curkey == "cached-curkey"
    assert entry.unikey == "cached-unikey"


def test_client_detail_payload_preserves_cached_raw_for_detail_cards() -> None:
    from qzone_bridge.models import FeedEntry
    from qzone_bridge.social import post_from_entry

    client = QzoneClient(SessionState(uin=12345, cookies={}))
    try:
        client.feed_cache[(12345, "fid-cached-image")] = FeedEntry(
            hostuin=12345,
            fid="fid-cached-image",
            appid=311,
            summary="list text",
            nickname="list nickname",
            created_at=1_690_000_000,
            raw={"pic": [{"url1": "https://qzone.example.test/cached-feed.jpg"}]},
        )

        entry = client.feed_entry_from_payload(
            {
                "hostuin": 12345,
                "fid": "fid-cached-image",
                "summary": "detail text",
                "nickname": "detail nickname",
                "created_at": 1_690_000_000,
            },
            default_hostuin=12345,
        )
    finally:
        asyncio.run(client.close())

    post = post_from_entry(entry, detail=entry.raw, local_id=1)

    assert entry.raw.get("_feed_raw") == {"pic": [{"url1": "https://qzone.example.test/cached-feed.jpg"}]}
    assert post.images == ["https://qzone.example.test/cached-feed.jpg"]


def test_daemon_detail_feed_uses_legacy_feed_time_when_primary_detail_omits_time(tmp_path: Path) -> None:
    from qzone_bridge.daemon import QzoneDaemonService

    store = StateStore(tmp_path)
    store.write(
        BridgeState(
            session=SessionState(
                uin=12345,
                cookies={"uin": "12345", "p_skey": "token"},
                qzonetokens={"12345": "token"},
                needs_rebind=False,
            )
        )
    )
    service = QzoneDaemonService(store, secret="secret", port=8765, keepalive_interval=30, request_timeout=0.01)
    calls: list[str] = []

    async def fake_detail(hostuin: int, fid: str, *, appid: int = 311, busi_param: str = ""):
        calls.append("detail")
        return {
            "hostuin": hostuin,
            "fid": fid,
            "appid": appid,
            "summary": "详情内容",
            "nickname": "详情昵称",
        }

    async def fake_legacy_recent_feeds():
        calls.append("legacy_recent")
        return {
            "vFeeds": [
                {
                    "hostuin": 12345,
                    "fid": "fid-daemon-time",
                    "appid": 311,
                    "summary": "列表内容",
                    "nickname": "列表昵称",
                    "created_at": 1_690_000_000,
                    "curkey": "legacy-curkey",
                    "unikey": "legacy-unikey",
                    "pic": [{"url1": "https://qzone.example.test/legacy-time.jpg"}],
                }
            ]
        }

    async def fake_legacy_feeds(hostuin: int, *, page: int = 1, num: int = 20):
        calls.append("legacy_profile")
        return {"vFeeds": []}

    service.client.detail = fake_detail
    service.client.legacy_recent_feeds = fake_legacy_recent_feeds
    service.client.legacy_feeds = fake_legacy_feeds

    try:
        payload = asyncio.run(service.detail_feed(hostuin=12345, fid="fid-daemon-time", appid=311))
    finally:
        asyncio.run(service.client.close())

    entry = payload["entry"]
    assert calls == ["detail", "legacy_profile", "legacy_recent"]
    assert entry["summary"] == "详情内容"
    assert entry["nickname"] == "详情昵称"
    assert entry["created_at"] == 1_690_000_000
    assert entry["curkey"] == "legacy-curkey"
    assert entry["unikey"] == "legacy-unikey"


def test_daemon_detail_feed_uses_legacy_feed_media_when_primary_detail_omits_images(tmp_path: Path) -> None:
    from qzone_bridge.daemon import QzoneDaemonService

    store = StateStore(tmp_path)
    store.write(
        BridgeState(
            session=SessionState(
                uin=12345,
                cookies={"uin": "12345", "p_skey": "token"},
                qzonetokens={"12345": "token"},
                needs_rebind=False,
            )
        )
    )
    service = QzoneDaemonService(store, secret="secret", port=8765, keepalive_interval=30, request_timeout=0.01)
    calls: list[str] = []

    async def fake_detail(hostuin: int, fid: str, *, appid: int = 311, busi_param: str = ""):
        calls.append("detail")
        return {
            "hostuin": hostuin,
            "fid": fid,
            "appid": appid,
            "summary": "detail text",
            "nickname": "detail nickname",
            "created_at": 1_690_000_000,
        }

    async def fake_legacy_recent_feeds():
        calls.append("legacy_recent")
        return {
            "vFeeds": [
                {
                    "hostuin": 12345,
                    "fid": "fid-daemon-image",
                    "appid": 311,
                    "summary": "list text",
                    "nickname": "list nickname",
                    "created_at": 1_690_000_000,
                    "pic": [{"url1": "https://qzone.example.test/legacy-feed.jpg"}],
                }
            ]
        }

    async def fake_legacy_feeds(hostuin: int, *, page: int = 1, num: int = 20):
        calls.append("legacy_profile")
        return {"vFeeds": []}

    service.client.detail = fake_detail
    service.client.legacy_recent_feeds = fake_legacy_recent_feeds
    service.client.legacy_feeds = fake_legacy_feeds

    try:
        payload = asyncio.run(service.detail_feed(hostuin=12345, fid="fid-daemon-image", appid=311))
    finally:
        asyncio.run(service.client.close())

    entry = payload["entry"]
    assert calls == ["detail", "legacy_profile", "legacy_recent"]
    assert entry["raw"]["_feed_raw"]["pic"][0]["url1"] == "https://qzone.example.test/legacy-feed.jpg"


def test_daemon_detail_feed_ignores_neighbor_media_when_recovering_current_images(tmp_path: Path) -> None:
    from qzone_bridge.daemon import QzoneDaemonService

    store = StateStore(tmp_path)
    store.write(
        BridgeState(
            session=SessionState(
                uin=12345,
                cookies={"uin": "12345", "p_skey": "token"},
                qzonetokens={"12345": "token"},
                needs_rebind=False,
            )
        )
    )
    service = QzoneDaemonService(store, secret="secret", port=8765, keepalive_interval=30, request_timeout=0.01)
    calls: list[str] = []

    async def fake_detail(hostuin: int, fid: str, *, appid: int = 311, busi_param: str = ""):
        calls.append("detail")
        return {
            "hostuin": hostuin,
            "fid": fid,
            "appid": appid,
            "summary": "hello",
            "nickname": "椰子",
            "created_at": 1_690_000_000,
            "feed": [
                {"fid": fid, "hostuin": hostuin, "summary": "hello"},
                {
                    "fid": "fid-neighbor",
                    "hostuin": hostuin,
                    "summary": "想我吗",
                    "pic": [{"url3": "https://m.qpic.cn/neighbor-image.jpg"}],
                },
            ],
        }

    async def fake_legacy_recent_feeds():
        calls.append("legacy_recent")
        return {
            "vFeeds": [
                {
                    "hostuin": 12345,
                    "fid": "fid-current",
                    "appid": 311,
                    "summary": "hello",
                    "nickname": "椰子",
                    "created_at": 1_690_000_000,
                    "pic": [{"url3": "https://m.qpic.cn/current-image.jpg"}],
                }
            ]
        }

    async def fake_legacy_feeds(hostuin: int, *, page: int = 1, num: int = 20):
        calls.append("legacy_profile")
        return {"vFeeds": []}

    service.client.detail = fake_detail
    service.client.legacy_recent_feeds = fake_legacy_recent_feeds
    service.client.legacy_feeds = fake_legacy_feeds

    try:
        payload = asyncio.run(service.detail_feed(hostuin=12345, fid="fid-current", appid=311))
    finally:
        asyncio.run(service.client.close())

    entry = payload["entry"]
    assert calls == ["detail", "legacy_profile", "legacy_recent"]
    assert entry["raw"]["_feed_raw"]["pic"][0]["url3"] == "https://m.qpic.cn/current-image.jpg"


def test_post_render_profile_keeps_nickname_without_social_extractor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    monkeypatch.delattr(main._social, "extract_nickname", raising=False)

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.data_dir = tmp_path
    post = main.QzonePost(
        hostuin=12345,
        fid="fid-1",
        nickname="12345",
        raw={"cell_userinfo": {"12345": {"nick": "正确昵称"}}},
        local_id=1,
    )

    profile = plugin._post_render_profile(post)

    assert profile.nickname == "正确昵称"


def test_post_render_profile_does_not_use_current_time_when_created_at_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.data_dir = tmp_path

    post = main.QzonePost(hostuin=12345, fid="fid-no-time", summary="no time", created_at=0)

    profile = plugin._post_render_profile(post)

    assert profile.time_text == "未知时间"


def test_manual_comment_feed_does_not_hide_selected_posts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Event:
        message_str = "评说说 1 已经看到啦"

        def is_admin(self):
            return True

        def get_self_id(self):
            return 12345

        def stop_event(self):
            captured["stopped"] = True

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    class _PostService:
        async def comment_post(self, post, content, *, private=False):
            captured["comment"] = (post.fid, content, private)
            return {"ok": True}

        async def like_post(self, post):
            captured["liked"] = post.fid
            return {"ok": True}

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        admin_uins=[],
        like_when_comment=False,
        max_feed_limit=20,
        render_publish_result=True,
    )
    plugin.data_dir = tmp_path
    plugin.controller = types.SimpleNamespace()
    post = main.QzonePost(hostuin=12345, fid="fid-1", summary="自己的说说", nickname="自己", local_id=1)

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_posts(selection, **kwargs):
        captured["selection"] = selection
        captured["post_kwargs"] = kwargs
        return [post]

    async def fake_yield_cards(*args, **kwargs):
        if False:
            yield None

    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._posts_for_selection = fake_posts
    plugin._post_service = lambda: _PostService()
    plugin._yield_post_card_results = fake_yield_cards

    async def collect_results():
        results = []
        async for item in plugin.comment_feed(_Event()):
            results.append(item)
        return results

    results = asyncio.run(collect_results())

    assert captured["post_kwargs"]["no_commented"] is False
    assert captured["post_kwargs"]["no_self"] is False
    assert captured["post_kwargs"]["with_detail"] is True
    assert captured["comment"] == ("fid-1", "已经看到啦", False)
    assert results == [{"type": "plain", "text": "已评论第 1 条：已经看到啦"}]


def test_manual_comment_feed_renders_card_with_comment_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Event:
        message_str = "评说说 1 已经看到啦"
        stopped = False

        def is_admin(self):
            return True

        def get_self_id(self):
            return 12345

        def stop_event(self):
            self.stopped = True

        def image_result(self, path: str):
            return {"type": "image", "path": path}

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    class _PostService:
        async def comment_post(self, post, content, *, private=False):
            captured["comment"] = (post.fid, content, private)
            return {"ok": True}

        async def like_post(self, post):
            captured["liked"] = post.fid
            return {"ok": True}

    def fake_render(post, output_dir, *, profile=None, result=None, width=900, remote_timeout=1.5, fixed_width=False):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "comment-card.png"
        path.write_bytes(b"png")
        captured["render_post"] = post
        captured["render_result"] = result
        captured["render_profile"] = profile
        return path

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        admin_uins=[],
        like_when_comment=False,
        max_feed_limit=20,
        render_publish_result=True,
        render_result_width=720,
        render_remote_timeout=0.01,
        render_feed_card_limit=5,
    )
    plugin.data_dir = tmp_path
    plugin.controller = types.SimpleNamespace()
    post = main.QzonePost(hostuin=12345, fid="fid-1", summary="自己的说说", nickname="自己", local_id=1)

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_posts(selection, **kwargs):
        captured["post_kwargs"] = kwargs
        return [post]

    monkeypatch.setattr(main, "render_publish_result_image", fake_render)
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._posts_for_selection = fake_posts
    plugin._post_service = lambda: _PostService()

    async def collect_results():
        results = []
        async for item in plugin.comment_feed(_Event()):
            results.append(item)
        return results

    results = asyncio.run(collect_results())

    rendered_post = captured["render_post"]
    assert captured["comment"] == ("fid-1", "已经看到啦", False)
    assert rendered_post.content == "自己的说说"
    assert captured["render_result"]["comment"] == "已经看到啦"
    assert results[0] == {"type": "image", "path": str(tmp_path / "rendered_posts" / "comment-card.png")}
    assert results[1] == {"type": "plain", "text": "已评论第 1 条：已经看到啦"}


@pytest.mark.parametrize("render_fails", [False, True])
def test_manual_comment_feed_returns_text_when_card_rendering_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    render_fails: bool,
) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Event:
        message_str = "评说说 1 已经看到啦"

        def is_admin(self):
            return True

        def get_self_id(self):
            return 12345

        def stop_event(self):
            pass

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

        if render_fails:
            def image_result(self, path: str):
                return {"type": "image", "path": path}

    class _PostService:
        async def comment_post(self, post, content, *, private=False):
            return {"ok": True}

        async def like_post(self, post):
            return {"ok": True}

    def broken_render(*args, **kwargs):
        raise RuntimeError("render failed")

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        admin_uins=[],
        like_when_comment=False,
        max_feed_limit=20,
        render_publish_result=True,
        render_result_width=720,
        render_remote_timeout=0.01,
        render_feed_card_limit=5,
    )
    plugin.data_dir = tmp_path
    plugin.controller = types.SimpleNamespace()
    post = main.QzonePost(hostuin=12345, fid="fid-1", summary="自己的说说", nickname="自己", local_id=1)

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_posts(selection, **kwargs):
        return [post]

    if render_fails:
        monkeypatch.setattr(main, "render_publish_result_image", broken_render)
    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._posts_for_selection = fake_posts
    plugin._post_service = lambda: _PostService()

    async def collect_results():
        results = []
        async for item in plugin.comment_feed(_Event()):
            results.append(item)
        return results

    assert asyncio.run(collect_results()) == [{"type": "plain", "text": "已评论第 1 条：已经看到啦"}]


def test_manual_comment_feed_preserves_successful_cards_when_later_comment_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Event:
        message_str = "评说说 1~2 已经看到啦"

        def is_admin(self):
            return True

        def get_self_id(self):
            return 12345

        def stop_event(self):
            pass

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    class _PostService:
        async def comment_post(self, post, content, *, private=False):
            if post.local_id == 2:
                raise QzoneBridgeError("评论失败")
            return {"ok": True}

        async def like_post(self, post):
            return {"ok": True}

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        admin_uins=[],
        like_when_comment=False,
        max_feed_limit=20,
        render_publish_result=True,
    )
    plugin.data_dir = tmp_path
    plugin.controller = types.SimpleNamespace()
    posts = [
        main.QzonePost(hostuin=12345, fid="fid-1", summary="第一条", nickname="自己", local_id=1),
        main.QzonePost(hostuin=12345, fid="fid-2", summary="第二条", nickname="自己", local_id=2),
    ]

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_posts(selection, **kwargs):
        return posts

    async def fake_yield_cards(event, selected_posts, fallback_text, **kwargs):
        captured["cards"] = (selected_posts, fallback_text, kwargs)
        yield {"type": "image", "path": str(tmp_path / "partial-comment-card.png")}

    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._posts_for_selection = fake_posts
    plugin._post_service = lambda: _PostService()
    plugin._yield_post_card_results = fake_yield_cards

    async def collect_results():
        results = []
        async for item in plugin.comment_feed(_Event()):
            results.append(item)
        return results

    results = asyncio.run(collect_results())

    assert captured["cards"][0] == [posts[0]]
    assert captured["cards"][2]["comment_texts"] == {id(posts[0]): "已经看到啦"}
    assert results[0] == {"type": "image", "path": str(tmp_path / "partial-comment-card.png")}
    assert results[1]["type"] == "plain"
    assert "已评论第 1 条：已经看到啦" in results[1]["text"]
    assert "第 2 条评论失败：评论失败" in results[1]["text"]


@pytest.mark.parametrize(
    ("message_str", "expected_start", "expected_end"),
    [
        ("读说说 1~2", 1, 2),
        ("/读说说：1", 1, 1),
        ("／读说说 2", 2, 2),
        ("1~2", 1, 2),
    ],
)
def test_read_feed_command_renders_cards_without_commenting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    message_str: str,
    expected_start: int,
    expected_end: int,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Event:
        message_str = ""

        def is_admin(self):
            return True

        def get_self_id(self):
            return 12345

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    class _PostService:
        async def comment_post(self, post, content, *, private=False):
            raise AssertionError("读说说 should not publish comments")

        async def like_post(self, post):
            raise AssertionError("读说说 should not like posts")

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        admin_uins=[],
        like_when_comment=True,
        max_feed_limit=20,
        render_publish_result=True,
    )
    plugin.data_dir = tmp_path
    plugin.controller = types.SimpleNamespace()
    posts = [
        main.QzonePost(hostuin=10001, fid="fid-1", summary="第一条", nickname="阿一", local_id=1),
        main.QzonePost(hostuin=10002, fid="fid-2", summary="第二条", nickname="阿二", local_id=2),
    ]

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_posts(selection, **kwargs):
        captured["selection"] = selection
        captured["post_kwargs"] = kwargs
        return posts

    async def fake_yield_cards(event, selected_posts, fallback_text, **kwargs):
        captured["cards"] = (selected_posts, fallback_text, kwargs)
        yield {"type": "image", "path": str(tmp_path / "read-cards.png")}

    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._posts_for_selection = fake_posts
    plugin._post_service = lambda: _PostService()
    plugin._yield_post_card_results = fake_yield_cards

    async def collect_results():
        results = []
        event = _Event()
        event.message_str = message_str
        async for item in plugin.read_feed(event):
            results.append(item)
        return results

    results = asyncio.run(collect_results())

    selection = captured["selection"]
    assert selection.start == expected_start
    assert selection.end == expected_end
    assert captured["post_kwargs"]["no_commented"] is False
    assert captured["post_kwargs"]["no_self"] is False
    assert captured["post_kwargs"]["with_detail"] is True
    assert captured["cards"][0] == posts
    assert captured["cards"][2] == {}
    assert results == [{"type": "image", "path": str(tmp_path / "read-cards.png")}]


def test_read_feed_requires_admin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    main = _import_main_with_stubs(monkeypatch)

    class _Event:
        message_str = "读说说 1"

        def is_admin(self):
            return False

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(admin_uins=[])
    plugin.data_dir = tmp_path

    async def collect_results():
        results = []
        async for item in plugin.read_feed(_Event()):
            results.append(item)
        return results

    assert asyncio.run(collect_results()) == [{"type": "plain", "text": "只有管理员可以查看说说。"}]


def test_empty_manual_comment_feed_keeps_auto_comment_safety_filters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Event:
        message_str = "评说说"

        def is_admin(self):
            return True

        def get_self_id(self):
            return 12345

        def stop_event(self):
            captured["stopped"] = True

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(admin_uins=[], like_when_comment=False, max_feed_limit=20)
    plugin.data_dir = tmp_path
    plugin.controller = types.SimpleNamespace()

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_posts(selection, **kwargs):
        captured["selection"] = selection
        captured["post_kwargs"] = kwargs
        return []

    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._posts_for_selection = fake_posts

    async def collect_results():
        results = []
        async for item in plugin.comment_feed(_Event()):
            results.append(item)
        return results

    results = asyncio.run(collect_results())

    assert captured["post_kwargs"]["no_commented"] is True
    assert captured["post_kwargs"]["no_self"] is True
    assert captured["post_kwargs"]["with_detail"] is True
    assert results == [{"type": "plain", "text": "没有找到可评论的说说。可以先用 看说说 1~3 确认编号或范围。"}]


def test_manual_comment_feed_handles_old_selection_without_explicit_property(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    monkeypatch.delattr(main.PostSelection, "has_explicit_input", raising=False)
    captured: dict[str, object] = {}

    class _Event:
        message_str = "评说说 1 已经看到啦"

        def is_admin(self):
            return True

        def get_self_id(self):
            return 12345

        def stop_event(self):
            captured["stopped"] = True

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

    class _PostService:
        async def comment_post(self, post, content, *, private=False):
            captured["comment"] = (post.fid, content, private)
            return {"ok": True}

        async def like_post(self, post):
            return {"ok": True}

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        admin_uins=[],
        like_when_comment=False,
        max_feed_limit=20,
        render_publish_result=True,
    )
    plugin.data_dir = tmp_path
    plugin.controller = types.SimpleNamespace()
    post = main.QzonePost(hostuin=12345, fid="fid-1", summary="自己的说说", nickname="自己", local_id=1)

    async def fake_ready(*args, **kwargs):
        return None

    async def fake_posts(selection, **kwargs):
        captured["post_kwargs"] = kwargs
        return [post]

    async def fake_yield_cards(*args, **kwargs):
        if False:
            yield None

    plugin._ensure_cookie_ready = fake_ready
    plugin._ensure_daemon = fake_ready
    plugin._posts_for_selection = fake_posts
    plugin._post_service = lambda: _PostService()
    plugin._yield_post_card_results = fake_yield_cards

    async def collect_results():
        results = []
        async for item in plugin.comment_feed(_Event()):
            results.append(item)
        return results

    results = asyncio.run(collect_results())

    assert captured["post_kwargs"]["no_commented"] is False
    assert captured["post_kwargs"]["no_self"] is False
    assert captured["comment"] == ("fid-1", "已经看到啦", False)
    assert results == [{"type": "plain", "text": "已评论第 1 条：已经看到啦"}]
