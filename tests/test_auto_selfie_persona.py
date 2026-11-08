import asyncio
import copy
import types

import pytest
from test_security_hardening import _import_main_with_stubs


@pytest.mark.parametrize("mode", ["draft", "publish"])
def test_schedule_selection_updates_prompt_and_passes_persona_id(
    monkeypatch, tmp_path, mode
):
    main = _import_main_with_stubs(monkeypatch)
    captured = {}
    original = {
        "outfit": "女仆服",
        "schedule": "09:00 写代码\n22:30 睡觉",
        "current_activity": "09:00 写代码",
        "current_time": "09:15",
    }

    class Omni:
        async def resolve_selfie_context(self, action, *, life_context):
            data = copy.deepcopy(life_context)
            data["outfit"] = "深蓝工装卫衣"
            data["selfie_persona"] = "成熟版工装"
            return {"persona_id": "workwear", "life_context": data}

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        life_publish_enabled=True,
        life_publish_mode=mode,
        life_publish_failure_policy="skip",
    )
    plugin._omnidraw_plugin = lambda: Omni()

    async def life_context():
        return original, plugin._life_context_text(original)

    async def image_prompt(event, context):
        captured["image_context"] = context
        return "写代码自拍，穿深蓝工装卫衣"

    async def generate(event, prompt, *, persona_id):
        captured["persona_id"] = persona_id
        return [main.PostMedia(kind="image", source=str(tmp_path / "image.png"))], {
            "success": True
        }

    async def caption(event, *, life_context, image_prompt):
        captured["caption_context"] = life_context
        return "今天继续写代码"

    async def draft(post, **kwargs):
        captured["post"] = post
        assert plugin.settings.life_publish_mode == mode
        return {"draft_id": 1}

    plugin._get_life_context = life_context
    plugin._generate_life_image_prompt = image_prompt
    plugin._generate_life_selfie_media = generate
    plugin._generate_life_caption = caption
    plugin._publish_or_draft_scheduled_post = draft
    result = asyncio.run(plugin._auto_life_publish_once())
    assert result == {"draft_id": 1}
    assert captured["persona_id"] == "workwear"
    assert "当前活动：09:00 写代码" in captured["image_context"]
    assert "当前时间：09:15" in captured["image_context"]
    assert "深蓝工装卫衣" in captured["image_context"]
    assert "成熟版工装" in captured["caption_context"]
    assert original["outfit"] == "女仆服"


def test_selfie_persona_id_reaches_return_result_tool(monkeypatch, tmp_path):
    main = _import_main_with_stubs(monkeypatch)
    captured = {}

    class Omni:
        async def tool_generate_selfie(
            self, event, action, *, persona_id, return_result, **kwargs
        ):
            captured.update(persona_id=persona_id, return_result=return_result)
            return {
                "success": True,
                "images": [{"file_path": str(tmp_path / "selfie.png")}],
            }

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(
        life_publish_use_omnidraw_selfie=True, life_publish_image_retry_count=0
    )
    plugin._omnidraw_plugin = lambda: Omni()
    plugin._scheduled_plugin_event = lambda **kwargs: object()
    media, result = asyncio.run(
        plugin._generate_life_selfie_media(None, "Q版写代码", persona_id="chibi")
    )
    assert result["success"] and len(media) == 1
    assert captured == {"persona_id": "chibi", "return_result": True}


@pytest.mark.parametrize("resolver_raises", [False, True])
def test_legacy_or_failed_selector_keeps_existing_generation_path(
    monkeypatch, tmp_path, resolver_raises
):
    main = _import_main_with_stubs(monkeypatch)
    calls = []

    class Omni:
        pass

    if resolver_raises:

        async def resolve(action, *, life_context):
            raise RuntimeError("selector unavailable")

        Omni.resolve_selfie_context = staticmethod(resolve)

    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = types.SimpleNamespace(life_publish_failure_policy="skip")
    plugin._omnidraw_plugin = lambda: Omni()

    async def life():
        return {"outfit": "女仆服"}, "今日穿搭：女仆服"

    async def prompt(event, context):
        assert context == "今日穿搭：女仆服"
        return "在窗边喝咖啡"

    async def generate(event, prompt):
        calls.append(prompt)
        return [main.PostMedia(kind="image", source=str(tmp_path / "selfie.png"))], {
            "success": True
        }

    async def caption(event, **kwargs):
        return "咖啡时间"

    async def publish(post, **kwargs):
        return {"fid": "fixture"}

    plugin._get_life_context = life
    plugin._generate_life_image_prompt = prompt
    plugin._generate_life_selfie_media = generate
    plugin._generate_life_caption = caption
    plugin._publish_or_draft_scheduled_post = publish
    assert asyncio.run(plugin._auto_life_publish_once()) == {"fid": "fixture"}
    assert calls == ["在窗边喝咖啡"]
