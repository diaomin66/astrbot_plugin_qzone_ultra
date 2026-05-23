from __future__ import annotations

import asyncio
import importlib
import inspect
from datetime import datetime
from pathlib import Path
import sys
import types

import pytest

from qzone_bridge.client import QzoneClient
from qzone_bridge.controller import QzoneDaemonController
from qzone_bridge.errors import QzoneBridgeError, QzoneCookieAcquireError, QzoneParseError, QzoneRequestError
from qzone_bridge.models import BridgeState, SessionState
from qzone_bridge.settings import PluginSettings
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

    class _Event:
        bot = object()

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

    class _Event:
        bot = object()

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


def test_aiocqhttp_capture_schedules_bootstrap_auto_bind_without_read_prob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    captured: dict[str, object] = {}

    class _Event:
        bot = object()

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

    class _Event:
        bot = object()

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


def test_base64_upload_sources_are_size_limited_before_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    import qzone_bridge.client as client_module

    monkeypatch.setattr(client_module, "MAX_UPLOAD_IMAGE_BYTES", 8)
    with pytest.raises(QzoneParseError, match="大小超过限制"):
        QzoneClient._decode_upload_image_base64("A" * 20, label="图片")


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


def test_public_error_text_redacts_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    exc = QzoneRequestError(
        "QQ 空间拒绝访问",
        status_code=403,
        detail={
            "status_code": 403,
            "url": "https://example.test/path?p_skey=SECRET&ok=1",
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


def test_render_feed_card_limit_is_loaded_from_config() -> None:
    settings = PluginSettings.from_mapping({"render_feed_card_limit": 3})
    assert settings.render_feed_card_limit == 3


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
    assert calls == ["detail", "legacy_recent"]
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
    assert calls == ["detail", "legacy_recent"]
    assert entry["raw"]["_feed_raw"]["pic"][0]["url1"] == "https://qzone.example.test/legacy-feed.jpg"


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
