from __future__ import annotations

import asyncio
import base64
import io
import sys
import types

from PIL import Image
from qzone_bridge.llm import QzoneLLM
from qzone_bridge.social import QzoneComment, QzonePost
from test_security_hardening import _import_main_with_stubs


def test_hot_reload_replaces_a_cached_bridge_without_image_caption(monkeypatch):
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "qzone_bridge" or name.startswith("qzone_bridge.")
    }
    import qzone_bridge.llm as llm_module

    try:
        monkeypatch.delattr(llm_module.QzoneLLM, "describe_images", raising=False)
        main = _import_main_with_stubs(monkeypatch)
        assert callable(getattr(main.QzoneLLM, "describe_images", None))
    finally:
        for name in list(sys.modules):
            if name == "qzone_bridge" or name.startswith("qzone_bridge."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.modules.pop("main", None)


def test_caption_sends_pixels_to_the_configured_vision_provider():
    image = Image.new("RGB", (32, 32), "red")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    source = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
    calls = []

    class Provider:
        async def text_chat(self, **kwargs):
            calls.append(kwargs)
            return {"completion_text": "图片 1：红色方块。"}

    class Context:
        def get_config(self, umo):
            assert umo == "test-session"
            return {
                "provider_settings": {"default_image_caption_provider_id": "vision"}
            }

        def get_provider_by_id(self, provider_id):
            assert provider_id == "vision"
            return Provider()

    adapter = QzoneLLM(Context(), types.SimpleNamespace(render_remote_timeout=0.35))
    result = asyncio.run(
        adapter.describe_images(
            types.SimpleNamespace(unified_msg_origin="test-session"), [source]
        )
    )
    assert result == "图片 1：红色方块。"
    assert len(calls) == 1
    uri = calls[0]["image_urls"][0]
    assert uri.startswith("data:image/jpeg;base64,")
    with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as sent:
        assert sent.size == (32, 32)
        assert sent.getpixel((0, 0))[0] > 240
    assert source not in calls[0]["prompt"]


def test_download_failure_never_calls_the_vision_provider():
    class Provider:
        async def text_chat(self, **kwargs):
            raise AssertionError(
                "Failed downloads must not be reported as visible images"
            )

    context = types.SimpleNamespace(get_using_provider=lambda: Provider())
    adapter = QzoneLLM(context, types.SimpleNamespace())
    result = asyncio.run(adapter.describe_images(None, ["base64://invalid-image"]))
    assert "未能下载" in result
    assert "不要猜测" in result
    assert asyncio.run(adapter.describe_images(None, [])) == ""


def test_comments_and_replies_use_image_descriptions_without_source_urls():
    calls = []

    class Provider:
        async def text_chat(self, **kwargs):
            calls.append(kwargs["prompt"])
            return {"completion_text": "红色方块很醒目"}

    settings = types.SimpleNamespace(
        comment_prompt="写评论",
        reply_prompt="写回复",
        comment_provider_id="",
        reply_provider_id="",
        comment_max_length=60,
    )
    adapter = QzoneLLM(
        types.SimpleNamespace(get_using_provider=lambda: Provider()), settings
    )

    async def caption(event, images):
        return "图片 1：红色方块。"

    adapter.describe_images = caption
    post = QzonePost(
        hostuin=12345,
        fid="post",
        summary="看看这个",
        images=["https://example.com/private.jpg"],
    )
    comment = QzoneComment(
        commentid="comment", uin=12346, nickname="Friend", content="什么颜色"
    )
    assert asyncio.run(adapter.generate_comment_text(None, post)) == "红色方块很醒目"
    assert (
        asyncio.run(adapter.generate_reply_text(None, post, comment))
        == "红色方块很醒目"
    )
    assert len(calls) == 2
    for prompt in calls:
        assert "红色方块" in prompt
        assert "private.jpg" not in prompt


def test_view_keeps_image_descriptions_when_the_reply_provider_fails(monkeypatch):
    main = _import_main_with_stubs(monkeypatch)
    plugin = object.__new__(main.QzoneStablePlugin)

    async def caption(event, images):
        return "图片 1：红色方块。"

    async def generate(*args, **kwargs):
        raise RuntimeError("Reply provider unavailable")

    plugin._llm_adapter = lambda: types.SimpleNamespace(
        describe_images=caption, generate_text=generate
    )
    post = main.QzonePost(
        hostuin=12345,
        fid="post",
        summary="看看这个",
        images=["https://example.com/image.jpg"],
        local_id=1,
    )
    result = asyncio.run(
        plugin._ask_llm_view_reply(
            None, [post], detail=True, fallback="第 1 条：看看这个"
        )
    )
    assert "红色方块" in result
    assert "第 1 条" in result


def test_read_command_returns_the_description_and_the_card(monkeypatch):
    main = _import_main_with_stubs(monkeypatch)
    plugin = object.__new__(main.QzoneStablePlugin)
    plugin._is_admin = lambda event: True
    plugin._self_id = lambda event: 12345
    plugin._selection_for_event = lambda *args: None
    plugin._format_posts = lambda *args, **kwargs: "第 1 条"
    plugin._command_result = lambda event, text: text

    async def ready(*args, **kwargs):
        pass

    async def posts(*args, **kwargs):
        return [
            main.QzonePost(
                hostuin=12346, fid="post", summary="看看这个", images=["base64://image"]
            )
        ]

    async def reply(*args, **kwargs):
        return "图片是红色方块"

    async def cards(*args, **kwargs):
        assert kwargs["fallback_when_unrendered"] is False
        yield "image-card"

    plugin._ensure_cookie_ready = ready
    plugin._ensure_daemon = ready
    plugin._posts_for_selection = posts
    plugin._ask_llm_view_reply = reply
    plugin._yield_post_card_results = cards

    async def run():
        return [result async for result in plugin.read_feed(None)]

    assert asyncio.run(run()) == ["图片是红色方块", "image-card"]
