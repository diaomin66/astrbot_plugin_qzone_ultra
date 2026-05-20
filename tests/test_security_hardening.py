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
