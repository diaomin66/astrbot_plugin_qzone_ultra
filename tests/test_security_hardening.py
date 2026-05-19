from __future__ import annotations

import asyncio
import importlib
import inspect
from pathlib import Path
import sys
import types

import pytest

from qzone_bridge.client import QzoneClient
from qzone_bridge.controller import QzoneDaemonController
from qzone_bridge.errors import QzoneParseError, QzoneRequestError
from qzone_bridge.models import SessionState
from qzone_bridge.settings import PluginSettings
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


def test_remote_media_download_headers_do_not_send_qzone_cookie() -> None:
    client = QzoneClient(SessionState(uin=12345, cookies={"uin": "o12345", "p_skey": "secret"}))
    try:
        headers = client._media_download_headers()
        assert "Cookie" not in headers
        assert "Referer" not in headers
        assert "Origin" not in headers
    finally:
        asyncio.run(client.close())


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
    assert profile.nickname == "2. 小明"
    assert profile.user_id == "12345"
    assert captured["width"] == 720
    assert captured["remote_timeout"] == 0.01


def test_qzone_commands_render_post_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _import_main_with_stubs(monkeypatch)
    expected_helpers = {
        "view_feed": "_yield_post_card_results",
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
