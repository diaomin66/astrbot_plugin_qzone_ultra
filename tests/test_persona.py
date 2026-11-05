import asyncio

import pytest

pytest.importorskip(
    "astrbot.core.platform.message_session",
    reason="AstrBot core is required for Persona/session contract integration tests",
)

from qzone_bridge.llm import QzoneLLM
from qzone_bridge.persona import PersonaResolutionError, resolve_persona_prompt
from qzone_bridge.persona_guard import PersonaConsistencyError
from qzone_bridge.settings import PluginSettings


class Manager:
    def __init__(self):
        self.lookups = []

    async def resolve_selected_persona(self, **kwargs):
        self.lookups.append(kwargs)
        pid = kwargs.get("conversation_persona_id") or "default"
        if pid == "[%None]":
            return pid, None, None, False
        return pid, {"prompt": f"persona:{pid}"}, None, False

    async def get_persona_v3_by_id(self, persona_id):
        return {"prompt": "persona:explicit"} if persona_id == "explicit" else None

    async def get_default_persona_v3(self, umo=None):
        return {"prompt": "persona:default"}


class Conversations:
    async def get_curr_conversation_id(self, umo):
        return "cid"

    async def get_conversation(self, umo, cid):
        return {"persona_id": "chosen"}


class Context:
    def __init__(self):
        self.persona_manager = Manager()
        self.conversation_manager = Conversations()
        self.calls = []
        self.platform_manager = type("PlatformManager", (), {
            "get_insts": lambda _self: [type("Platform", (), {
                "meta": lambda _self: type("Meta", (), {"name": "aiocqhttp", "id": "custom-instance-7"})()
            })()]
        })()

    async def get_config(self, umo):
        return {"provider_settings": {"provider": "configured"}}

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return "generated"


def test_incoming_event_uses_selected_persona_and_conversation():
    async def scenario():
        context = Context()
        prompt = await resolve_persona_prompt(
            context, PluginSettings(), type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:1"})()
        )
        assert prompt == "persona:chosen"
        assert context.persona_manager.lookups[0]["umo"] == "custom-instance-7:GroupMessage:1"
        assert context.persona_manager.lookups[0]["provider_settings"] == {"provider": "configured"}
    asyncio.run(scenario())


def test_context_free_override_disable_and_invalid_id():
    async def scenario():
        context = Context()
        settings = PluginSettings(persona_id="explicit")
        assert await resolve_persona_prompt(context, settings, None) == "persona:explicit"
        settings.persona_id = "[%None]"
        assert await resolve_persona_prompt(context, settings, None) == ""
        settings.persona_id = "missing"
        with pytest.raises(PersonaResolutionError):
            await resolve_persona_prompt(context, settings, None)
    asyncio.run(scenario())


def test_context_free_uses_management_group_and_resolver():
    async def scenario():
        context = Context()
        assert await resolve_persona_prompt(context, PluginSettings(manage_group=123), None) == "persona:chosen"
        assert context.persona_manager.lookups[0]["umo"] == "custom-instance-7:GroupMessage:123"
    asyncio.run(scenario())


def test_context_umo_uses_astrbot_session_contract_and_ambiguous_falls_back():
    from astrbot.core.platform.message_session import MessageSession

    async def scenario():
        context = Context()
        await resolve_persona_prompt(context, PluginSettings(manage_group=123), None)
        group_umo = context.persona_manager.lookups[-1]["umo"]
        assert str(MessageSession.from_str(group_umo)) == "custom-instance-7:GroupMessage:123"

        context.platform_manager.get_insts = lambda: [
            type("Platform", (), {"meta": lambda _self: type("Meta", (), {"name": "aiocqhttp", "id": "custom-instance-7"})()})(),
            type("Platform", (), {"meta": lambda _self: type("Meta", (), {"name": "aiocqhttp", "id": "other-instance"})()})(),
        ]
        context.persona_manager.lookups.clear()
        assert await resolve_persona_prompt(context, PluginSettings(manage_group=123), None) == "persona:default"
        assert not context.persona_manager.lookups
        context.platform_manager.get_insts = lambda: []
        assert await resolve_persona_prompt(context, PluginSettings(manage_group=123), None) == "persona:default"
        assert not context.persona_manager.lookups

        context.platform_manager.get_insts = lambda: [
            type("Platform", (), {"meta": lambda _self: type("Meta", (), {"name": "aiocqhttp", "id": "custom-instance-7"})()})()
        ]
        await resolve_persona_prompt(context, PluginSettings(admin_uins=[456]), None)
        assert str(MessageSession.from_str(context.persona_manager.lookups[-1]["umo"])) == "custom-instance-7:FriendMessage:456"

        explicit = PluginSettings(context_umo="custom-instance-7:GroupMessage:789")
        await resolve_persona_prompt(context, explicit, None)
        assert context.persona_manager.lookups[-1]["umo"] == explicit.context_umo
        with pytest.raises(PersonaResolutionError):
            await resolve_persona_prompt(context, PluginSettings(context_umo="aiocqhttp:group:123"), None)
    asyncio.run(scenario())


def test_context_umo_setting_roundtrips_and_real_event_ignores_it():
    async def scenario():
        settings = PluginSettings.from_mapping({"llm": {"context_umo": "custom-instance-7:GroupMessage:789"}})
        assert settings.context_umo == "custom-instance-7:GroupMessage:789"
        context = Context()
        event = type("Event", (), {"unified_msg_origin": "custom-instance-7:FriendMessage:1"})()
        await resolve_persona_prompt(context, settings, event)
        assert context.persona_manager.lookups[-1]["umo"] == event.unified_msg_origin
    asyncio.run(scenario())


def test_scheduled_event_uses_shared_resolved_umo():
    from main import QzoneStablePlugin

    context = Context()
    plugin = object.__new__(QzoneStablePlugin)
    plugin._context = context
    plugin.settings = PluginSettings(manage_group=123)
    event = plugin._scheduled_plugin_event(prompt="draft")
    assert event.unified_msg_origin == "custom-instance-7:GroupMessage:123"
    assert event.get_session_id() == event.unified_msg_origin

    plugin.settings.context_umo = "custom-instance-7:FriendMessage:456"
    event = plugin._scheduled_plugin_event(prompt="draft")
    assert event.unified_msg_origin == plugin.settings.context_umo

    context.platform_manager.get_insts = lambda: []
    plugin.settings.context_umo = ""
    event = plugin._scheduled_plugin_event(prompt="draft")
    assert event.unified_msg_origin is None


def test_none_event_provider_and_persona_share_exact_context_umo():
    async def scenario():
        exact = "custom-instance-7:GroupMessage:123"

        class StrictManager:
            def __init__(self):
                self.umo_values = []

            async def resolve_selected_persona(self, **kwargs):
                self.umo_values.append(kwargs["umo"])
                if kwargs["umo"] == exact and kwargs["conversation_persona_id"] == "stored-persona":
                    return "forced-persona", {"prompt": "exact-context-persona"}, "forced-persona", False
                return "default", {"prompt": "wrong-context-default"}, None, False

            async def get_default_persona_v3(self, umo=None):
                return {"prompt": "fallback"}

        class StrictConversations:
            async def get_curr_conversation_id(self, umo):
                return "conversation" if umo == exact else None

            async def get_conversation(self, umo, cid):
                assert umo == exact
                return {"persona_id": "stored-persona"}

        class StrictContext(Context):
            def __init__(self):
                super().__init__()
                self.persona_manager = StrictManager()
                self.conversation_manager = StrictConversations()
                self.provider_umos = []

            async def get_config(self, umo):
                assert umo == exact
                return {"provider_settings": {"source": "exact"}}

            async def get_using_provider(self, *, umo):
                self.provider_umos.append(umo)
                return type("Provider", (), {"text_chat": lambda _self, **kwargs: None})()

            async def get_current_chat_provider_id(self, *, umo):
                self.provider_umos.append(umo)
                return "provider-exact" if umo == exact else "provider-wrong"

        context = StrictContext()
        llm = QzoneLLM(context, PluginSettings(context_umo=exact))
        assert await resolve_persona_prompt(context, llm.settings, None) == "exact-context-persona"
        assert await llm._provider_by_id(None) is not None
        assert await llm.current_provider_id(None) == "provider-exact"
        assert context.provider_umos == [exact, exact]
        assert context.persona_manager.umo_values == [exact]

        inferred = QzoneLLM(context, PluginSettings(manage_group=123))
        assert await resolve_persona_prompt(context, inferred.settings, None) == "exact-context-persona"
        assert await inferred._provider_by_id(None) is not None
        assert await inferred.current_provider_id(None) == "provider-exact"
        assert context.provider_umos[-2:] == [exact, exact]
        assert context.persona_manager.umo_values[-1] == exact
    asyncio.run(scenario())


def test_text_chat_keeps_persona_and_task_prompt():
    async def scenario():
        class Provider:
            async def text_chat(self, *, prompt, contexts, system_prompt):
                assert contexts == []
                assert "persona:chosen" in system_prompt
                assert "task instructions" in system_prompt
                return "ok"

        class ProviderContext(Context):
            async def get_using_provider(self, **kwargs):
                return Provider()

        context = ProviderContext()
        llm = QzoneLLM(context, PluginSettings())
        event = type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:1"})()
        assert await llm.generate_text(event, "write", system_prompt="task instructions") == "ok"
    asyncio.run(scenario())


def test_context_llm_fallback_preserves_system_prompt():
    async def scenario():
        context = Context()
        llm = QzoneLLM(context, PluginSettings())
        event = type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:1"})()
        assert await llm.generate_text(event, "write", system_prompt="task instructions") == "generated"
        assert "persona:chosen" in context.calls[0]["system_prompt"]
    asyncio.run(scenario())


def test_selected_disabled_persona_does_not_fallback_to_default():
    async def scenario():
        context = Context()
        async def disabled(**kwargs):
            return "[%None]", None, None, False
        context.persona_manager.resolve_selected_persona = disabled
        event = type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:1"})()
        assert await resolve_persona_prompt(context, PluginSettings(), event) == ""
    asyncio.run(scenario())


def test_persona_runtime_errors_stop_instead_of_falling_back():
    async def scenario():
        context = Context()
        async def broken_resolver(**kwargs):
            raise RuntimeError("sensitive provider/account marker")
        context.persona_manager.resolve_selected_persona = broken_resolver
        llm = QzoneLLM(context, PluginSettings())
        event = type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:1"})()
        with pytest.raises(PersonaResolutionError):
            await llm.generate_text(event, "prompt")
        assert context.calls == []

        class BrokenConversation:
            async def get_curr_conversation_id(self, umo):
                raise RuntimeError("conversation storage failure")
        context.conversation_manager = BrokenConversation()
        with pytest.raises(PersonaResolutionError):
            await resolve_persona_prompt(context, PluginSettings(), event)
    asyncio.run(scenario())


def test_explicit_override_uses_legacy_database_persona_and_missing_manager_errors():
    async def scenario():
        class LegacyManager:
            async def get_persona(self, persona_id):
                assert persona_id == "db-id"
                return type("DBPersona", (), {"system_prompt": "legacy DB prompt"})()

        context = type("LegacyContext", (), {"persona_manager": LegacyManager()})()
        assert await resolve_persona_prompt(context, PluginSettings(persona_id="db-id"), None) == "legacy DB prompt"
        with pytest.raises(PersonaResolutionError):
            await resolve_persona_prompt(None, PluginSettings(persona_id="db-id"), None)
    asyncio.run(scenario())


def test_forced_persona_selection_overrides_conversation_and_default():
    async def scenario():
        context = Context()
        async def forced(**kwargs):
            assert kwargs["conversation_persona_id"] == "chosen"
            return "forced-id", {"prompt": "forced persona"}, "forced-id", False
        context.persona_manager.resolve_selected_persona = forced
        event = type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:1"})()
        assert await resolve_persona_prompt(context, PluginSettings(), event) == "forced persona"
    asyncio.run(scenario())


def test_context_free_without_umo_uses_astrbot_default_persona():
    async def scenario():
        context = Context()
        context.conversation_manager = None
        assert await resolve_persona_prompt(context, PluginSettings(), None) == "persona:default"
    asyncio.run(scenario())


def test_real_incoming_event_ignores_context_free_override():
    async def scenario():
        context = Context()
        settings = PluginSettings(persona_id="explicit")
        event = type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:1"})()
        assert await resolve_persona_prompt(context, settings, event) == "persona:chosen"
        assert context.persona_manager.lookups
    asyncio.run(scenario())


def test_configured_and_current_provider_receive_same_persona_and_task_prompt():
    async def scenario():
        prompts = []
        class Provider:
            async def text_chat(self, *, prompt, contexts, system_prompt):
                assert contexts == []
                prompts.append(system_prompt)
                return "draft"
        provider = Provider()
        class ProviderContext(Context):
            async def get_provider_by_id(self, provider_id):
                assert provider_id == "chosen-provider"
                return provider
            async def get_using_provider(self, **kwargs):
                return provider
        context = ProviderContext()
        llm = QzoneLLM(context, PluginSettings())
        event = type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:1"})()
        task = "same task instructions"
        await llm.generate_text(event, "prompt", provider_id="chosen-provider", system_prompt=task)
        await llm.generate_text(event, "prompt", system_prompt=task, prefer_current_provider=True)
        assert len(prompts) == 2 and prompts[0] == prompts[1]
        assert "persona:chosen" in prompts[0] and task in prompts[0]
    asyncio.run(scenario())


def test_inherit_persona_false_skips_resolver_and_keeps_task_prompt():
    async def scenario():
        class Provider:
            async def text_chat(self, *, prompt, contexts, system_prompt):
                assert "task only" == system_prompt
                return "draft"
        class ProviderContext(Context):
            async def get_using_provider(self, **kwargs):
                return Provider()
        context = ProviderContext()
        settings = PluginSettings(persona_id="invalid")
        llm = QzoneLLM(context, settings)
        event = type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:1"})()
        assert await llm.generate_text(event, "prompt", system_prompt="task only", inherit_persona=False) == "draft"
        assert not context.persona_manager.lookups
    asyncio.run(scenario())


def test_guarded_final_text_reviews_and_corrects_once_then_preserves_normalized_text():
    async def scenario():
        context = Context()
        replies = iter(["第一稿。", '{"accepted":false,"rewrite":"修订稿。"}', '{"accepted":true,"rewrite":""}'])
        prompts = []

        async def generate(**kwargs):
            context.calls.append(kwargs)
            prompts.append(kwargs.get("system_prompt", ""))
            return next(replies)

        class Provider:
            async def text_chat(self, *, prompt, contexts, system_prompt):
                return await generate(prompt=prompt, contexts=contexts, system_prompt=system_prompt)
        context.get_using_provider = lambda **kwargs: Provider()
        llm = QzoneLLM(context, PluginSettings())
        result = await llm.generate_guarded_text(
            type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:1"})(),
            "task prompt",
            system_prompt="task system",
            normalizer=lambda text: str(text).strip().rstrip("。"),
            authoritative_facts="FACT-77",
            format_contract="nonempty",
        )
        assert result == "修订稿"
        assert len(context.calls) == 3
        assert "persona:chosen" in context.calls[0]["system_prompt"]
        assert "PERSONA_DATA" in context.calls[1]["prompt"]
        assert "FACT-77" in context.calls[1]["prompt"]
        assert context.calls[1]["system_prompt"].startswith("You are a strict final-artifact reviewer")
        assert len(prompts) == 3

    asyncio.run(scenario())


def test_guarded_final_text_sanitizes_provider_exception_and_never_returns_unreviewed_text():
    async def scenario():
        context = Context()
        async def fail(**kwargs):
            raise RuntimeError("SENSITIVE-PROMPT-MARKER")
        context.llm_generate = fail
        llm = QzoneLLM(context, PluginSettings())
        with pytest.raises(PersonaConsistencyError) as error:
            await llm.generate_guarded_text(
                type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:1"})(),
                "task",
                normalizer=str.strip,
            )
        assert "SENSITIVE-PROMPT-MARKER" not in str(error.value)
        assert error.value.code == "REVIEW_FAILED"

    asyncio.run(scenario())


def test_guarded_snapshot_pins_event_provider_across_review_and_restores_for_next_artifact():
    async def scenario():
        selected = {"value": None}
        calls = {"provider_a": [], "provider_b": [], "umos": []}

        class Provider:
            def __init__(self, key):
                self.key = key
            async def text_chat(self, *, prompt, contexts, system_prompt):
                calls[self.key].append((prompt, system_prompt))
                if prompt.startswith("Review whether"):
                    return '{"accepted":true,"rewrite":""}'
                selected["value"] = provider_b
                return "candidate"

        provider_a = Provider("provider_a")
        provider_b = Provider("provider_b")

        class ProviderContext(Context):
            async def get_using_provider(self, **kwargs):
                umo = kwargs.get("umo")
                calls["umos"].append(umo)
                if umo != "custom-instance-7:GroupMessage:8":
                    return provider_b  # Default provider intentionally differs from the event context.
                return selected["value"] or provider_a

        context = ProviderContext()
        llm = QzoneLLM(context, PluginSettings())
        event = type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:8"})()
        assert await llm.generate_guarded_text(event, "task", normalizer=str.strip) == "candidate"
        assert len(calls["provider_a"]) == 2 and not calls["provider_b"]
        assert calls["umos"] == ["custom-instance-7:GroupMessage:8"]

        selected["value"] = provider_b
        assert await llm.generate_guarded_text(event, "task", normalizer=str.strip) == "candidate"
        assert len(calls["provider_b"]) == 2

    asyncio.run(scenario())


def test_guarded_explicit_missing_provider_does_not_fall_back_to_current_provider():
    async def scenario():
        context = Context()
        context.provider_fallback_calls = 0
        async def missing(_provider_id):
            return None
        async def current(**kwargs):
            context.provider_fallback_calls += 1
            return object()
        async def generate(**kwargs):
            raise TypeError("SENSITIVE selected provider unavailable")
        context.get_provider_by_id = missing
        context.get_using_provider = current
        context.llm_generate = generate
        llm = QzoneLLM(context, PluginSettings())
        with pytest.raises(Exception) as error:
            await llm.generate_guarded_text(
                type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:9"})(),
                "task", provider_id="missing-id", normalizer=str.strip,
            )
        assert context.provider_fallback_calls == 0
        assert "SENSITIVE" not in str(error.value)

    asyncio.run(scenario())


def test_guarded_without_fixed_provider_route_fails_before_any_provider_call():
    async def scenario():
        context = Context()
        context.provider_calls = 0
        async def no_provider(**kwargs):
            return None
        async def generate(**kwargs):
            context.provider_calls += 1
            return "unreviewed"
        context.get_using_provider = no_provider
        context.llm_generate = generate
        llm = QzoneLLM(context, PluginSettings())
        with pytest.raises(Exception):
            await llm.generate_guarded_text(
                type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:1"})(),
                "task", normalizer=str.strip,
            )
        assert context.provider_calls == 0

    asyncio.run(scenario())


def test_guarded_deadline_covers_persona_and_provider_lookup_before_generation():
    async def scenario():
        context = Context()
        async def slow_resolver(**kwargs):
            await asyncio.sleep(1)
        context.persona_manager.resolve_selected_persona = slow_resolver
        event = type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:9"})()
        llm = QzoneLLM(context, PluginSettings())
        with pytest.raises(PersonaConsistencyError) as error:
            await llm.generate_guarded_text(event, "task", scope_timeout=0.01)
        assert error.value.code == "BUDGET_EXHAUSTED"
        assert "SENSITIVE" not in str(error.value)
        assert context.calls == []

        context = Context()
        async def slow_lookup(**kwargs):
            await asyncio.sleep(1)
        context.get_using_provider = slow_lookup
        context.get_current_chat_provider_id = None
        llm = QzoneLLM(context, PluginSettings())
        with pytest.raises(PersonaConsistencyError) as error:
            await llm.generate_guarded_text(event, "task", prefer_current_provider=True, scope_timeout=0.01)
        assert error.value.code == "BUDGET_EXHAUSTED"
        assert "SENSITIVE" not in str(error.value)
        assert context.calls == []
    asyncio.run(scenario())


def test_background_umo_is_frozen_before_persona_resolution_await():
    async def scenario():
        settings = PluginSettings(context_umo="custom-instance-7:GroupMessage:10")
        seen = {"config": [], "provider": [], "prompts": []}

        class PersonaManager:
            async def resolve_selected_persona(self, **kwargs):
                return "p", {"prompt": f"persona-for:{kwargs['umo']}"}, None, False

        class Provider:
            async def text_chat(self, *, prompt, contexts, system_prompt):
                seen["prompts"].append(system_prompt)
                return '{"accepted":true,"rewrite":""}' if prompt.startswith("Review whether") else "candidate"

        provider = Provider()

        class Context:
            persona_manager = PersonaManager()
            conversation_manager = None
            platform_manager = None
            async def get_config(self, umo):
                seen["config"].append(umo)
                settings.context_umo = "custom-instance-7:GroupMessage:20"
                return {"provider_settings": {}}
            async def get_using_provider(self, **kwargs):
                seen["provider"].append(kwargs.get("umo"))
                return provider

        llm = QzoneLLM(Context(), settings)
        assert await llm.generate_guarded_text(None, "task", prefer_current_provider=True) == "candidate"
        expected = "custom-instance-7:GroupMessage:10"
        assert seen["config"] == [expected]
        assert seen["provider"] == [expected]
        assert "persona-for:" + expected in seen["prompts"][0]
        assert seen["prompts"][1].startswith("You are a strict final-artifact reviewer")

    asyncio.run(scenario())


def test_raw_generate_text_without_snapshot_resolves_real_event_and_background_umo():
    async def scenario():
        seen = []
        class Provider:
            async def text_chat(self, *, prompt, contexts=None, system_prompt=""):
                return "raw-draft"
        provider = Provider()
        class Context:
            async def get_using_provider(self, **kwargs):
                seen.append(kwargs.get("umo"))
                return provider

        event_umo = "custom-instance-7:GroupMessage:321"
        event = type("Event", (), {"unified_msg_origin": event_umo})()
        llm = QzoneLLM(Context(), PluginSettings(context_umo="custom-instance-7:FriendMessage:654"))
        assert await llm.generate_text(event, "raw", inherit_persona=False) == "raw-draft"
        assert await llm.generate_text(None, "background", inherit_persona=False) == "raw-draft"
        assert seen == [event_umo, "custom-instance-7:FriendMessage:654"]

    asyncio.run(scenario())


def test_raw_generate_text_honors_snapshot_with_explicit_none_umo():
    from qzone_bridge.persona_guard import generation_scope, set_snapshot

    async def scenario():
        seen = []
        class Provider:
            async def text_chat(self, *, prompt, contexts=None, system_prompt=""):
                return "raw-draft"
        class Context:
            async def get_using_provider(self, **kwargs):
                seen.append(kwargs.get("umo"))
                return Provider()
        event = type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:321"})()
        llm = QzoneLLM(Context(), PluginSettings(context_umo="custom-instance-7:GroupMessage:999"))
        async with generation_scope():
            set_snapshot({"persona_prompt": "", "provider_id": "", "provider": None, "umo": None, "pin_provider": False})
            assert await llm.generate_text(event, "raw", inherit_persona=False) == "raw-draft"
        assert seen == [None]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "responses, expected_calls, expected_code",
    [
        (["draft", '{"accepted":true,"rewrite":""}'], 2, None),
        (["draft", '{"accepted":false,"rewrite":"revised"}', '{"accepted":true,"rewrite":""}'], 3, None),
        (["draft", '{"accepted":false,"rewrite":"revised"}', '{"accepted":false,"rewrite":"second"}'], 3, "REVIEW_FAILED"),
        (["draft", "malformed"], 2, "REVIEW_FAILED"),
        (["",], 1, "REVIEW_FAILED"),
    ],
)
def test_guarded_final_text_terminal_paths_are_bounded_and_fixed(responses, expected_calls, expected_code):
    async def scenario():
        replies = iter(responses)
        calls = []
        class Provider:
            async def text_chat(self, *, prompt, contexts, system_prompt):
                calls.append((prompt, system_prompt))
                return next(replies)
        provider = Provider()
        class ProviderContext(Context):
            async def get_using_provider(self, **kwargs):
                return provider
        llm = QzoneLLM(ProviderContext(), PluginSettings())
        event = type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:19"})()
        if expected_code:
            with pytest.raises(PersonaConsistencyError) as exc:
                await llm.generate_guarded_text(event, "task", normalizer=str.strip)
            assert exc.value.code == expected_code
            assert "revised" not in str(exc.value) and "second" not in str(exc.value)
        else:
            result = await llm.generate_guarded_text(event, "task", normalizer=str.strip)
            assert result == ("revised" if len(responses) == 3 else "draft")
        assert len(calls) == expected_calls
        assert len(calls) <= 4
    asyncio.run(scenario())


def test_guarded_normalizer_is_applied_before_review_and_corrected_text_is_normalized():
    async def scenario():
        replies = iter(["draft long tail", '{"accepted":false,"rewrite":"short corrected"}', '{"accepted":true,"rewrite":""}'])
        reviewed = []
        class Provider:
            async def text_chat(self, *, prompt, contexts, system_prompt):
                if prompt.startswith("Review whether"):
                    reviewed.append(prompt)
                    return next(replies)
                return next(replies)
        class ProviderContext(Context):
            async def get_using_provider(self, **kwargs):
                return Provider()
        def normalize(value):
            return value[:12].strip()
        event = type("Event", (), {"unified_msg_origin": "custom-instance-7:GroupMessage:19"})()
        llm = QzoneLLM(ProviderContext(), PluginSettings())
        assert await llm.generate_guarded_text(event, "task", normalizer=normalize) == "short correc"
        assert len(reviewed) == 2
        assert "draft long t" in reviewed[0]
        assert "short correc" in reviewed[1]
    asyncio.run(scenario())


def test_review_disabled_snapshot_preserves_legacy_unsupported_provider_id_fallback():
    async def scenario():
        calls = []
        class LegacyContext(Context):
            async def get_current_chat_provider_id(self, **kwargs):
                return "legacy-current"
            async def llm_generate(self, *, prompt, system_prompt=""):
                calls.append((prompt, system_prompt))
                return "legacy result"
        context = LegacyContext()
        settings = PluginSettings(persona_id="[%None]", persona_consistency_check=True)
        llm = QzoneLLM(context, settings)
        assert await llm.generate_guarded_text(None, "task", prefer_current_provider=True) == "legacy result"
        assert calls == [("task", "")]
    asyncio.run(scenario())
