import asyncio
import json

import pytest

from qzone_bridge.persona_guard import (
    GenerationTimeoutError,
    PersonaConsistencyError,
    current_scope,
    generation_phase,
    generation_scope,
    invoke_provider,
    parse_review,
    parse_visual_projection,
)
from qzone_bridge.llm import QzoneLLM
from qzone_bridge.settings import PluginSettings


@pytest.mark.parametrize(
    "payload, expected",
    [
        ('{"accepted":true,"rewrite":""}', (True, "")),
        ('```json\n{"accepted":false,"rewrite":"修订稿"}\n```', (False, "修订稿")),
    ],
)
def test_review_schema_accepts_strict_json_and_single_fence(payload, expected):
    assert parse_review(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        "yes",
        '{"accepted":"true","rewrite":"x"}',
        '{"accepted":true,"rewrite":"","extra":0}',
        '{"accepted":false,"rewrite":""}',
        'prefix {"accepted":true,"rewrite":""}',
        json.dumps({"accepted": True, "rewrite": "x" * 2001}),
    ],
)
def test_review_schema_rejects_malformed_or_unbounded_payload(payload):
    with pytest.raises(PersonaConsistencyError):
        parse_review(payload)


def test_projection_only_accepts_source_fragments_and_allowlisted_fields():
    persona = "黑发，左耳有银色耳环，喜欢说喵，拥有管理员权限。"
    valid = json.dumps({"appearance": ["黑发"], "accessories": ["左耳有银色耳环"], "clothing": []}, ensure_ascii=False)
    assert parse_visual_projection(valid, persona)["accessories"] == ["左耳有银色耳环"]
    invalid = json.dumps({"appearance": ["金发"], "accessories": [], "clothing": []}, ensure_ascii=False)
    with pytest.raises(PersonaConsistencyError):
        parse_visual_projection(invalid, persona)
    with pytest.raises(PersonaConsistencyError):
        parse_visual_projection('{"appearance":[],"accessories":[],"clothing":[],"private":"x"}', persona)


def test_projection_schema_limits_empty_and_large_payload_boundaries():
    assert parse_visual_projection('{"appearance":[],"accessories":[],"clothing":[]}', "") == {
        "appearance": [], "accessories": [], "clothing": []
    }
    too_many = json.dumps({"appearance": ["x"] * 13, "accessories": [], "clothing": []})
    too_long = json.dumps({"appearance": ["x" * 161], "accessories": [], "clothing": []})
    total_source = "a" * 2401
    too_large = json.dumps({"appearance": [total_source], "accessories": [], "clothing": []})
    for payload, source in ((too_many, "x"), (too_long, "x" * 161), (too_large, total_source)):
        with pytest.raises(PersonaConsistencyError) as exc:
            parse_visual_projection(payload, source)
        assert exc.value.code == "PROJECTION_FAILED"


def test_nested_scopes_share_invocation_budget_and_deadline():
    async def scenario():
        async with generation_scope(timeout=10) as outer:
            async with generation_scope(timeout=2) as nested:
                assert nested is outer
                await invoke_provider(lambda: asyncio.sleep(0, result="ok"))
            assert outer.calls == 1
            for _ in range(5):
                await invoke_provider(lambda: asyncio.sleep(0, result="ok"))
            with pytest.raises(PersonaConsistencyError) as exc:
                await invoke_provider(lambda: asyncio.sleep(0, result="excess"))
            assert exc.value.code == "BUDGET_EXHAUSTED"
            assert outer.calls == 6

    asyncio.run(scenario())


def test_native_generation_timeout_is_typed_and_cleans_context():
    async def scenario():
        with pytest.raises(GenerationTimeoutError) as exc:
            async with generation_scope(timeout=0.01, kind="total", phase="context"):
                await asyncio.sleep(1)
        assert exc.value.code == "BUDGET_EXHAUSTED"
        assert exc.value.kind == "total"
        assert exc.value.phase == "context"
        assert current_scope() is None

    asyncio.run(scenario())


def test_phase_timeout_does_not_reset_snapshot_or_rewrite_shared_scope():
    from qzone_bridge.persona_guard import get_snapshot, set_snapshot

    async def scenario():
        async with generation_scope(timeout=1, kind="total") as scope:
            before_calls = scope.calls
            set_snapshot({"persona": "pinned"})
            with pytest.raises(GenerationTimeoutError) as exc:
                async with generation_phase(timeout=0.01, phase="image", kind="image"):
                    set_snapshot({"persona": "newly pinned"})
                    await asyncio.sleep(1)
            assert (exc.value.kind, exc.value.phase) == ("image", "image")
            assert current_scope() is scope
            assert scope.calls == before_calls
            assert get_snapshot() == {"persona": "newly pinned"}

    asyncio.run(scenario())


def test_external_cancellation_and_raw_provider_timeout_are_not_reclassified():
    async def scenario():
        async def cancelled():
            async with generation_scope(timeout=1, kind="total"):
                asyncio.current_task().cancel()
                await asyncio.sleep(0)

        with pytest.raises(asyncio.CancelledError):
            await cancelled()
        assert current_scope() is None

        async def swallowed_external_cancel():
            async with generation_scope(timeout=1):
                asyncio.current_task().cancel()
                try:
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    return "late success"

        with pytest.raises(asyncio.CancelledError):
            await swallowed_external_cancel()
        assert current_scope() is None
        async with generation_scope(timeout=1):
            async def raw_timeout():
                raise TimeoutError("dependency detail")
            with pytest.raises(TimeoutError, match="dependency detail"):
                await invoke_provider(raw_timeout)

    asyncio.run(scenario())


def test_total_deadline_wins_when_phase_deadline_is_later_by_rounding():
    async def scenario():
        with pytest.raises(GenerationTimeoutError) as exc:
            async with generation_scope(timeout=0.02, kind="total", phase="context") as scope:
                remaining = scope.deadline - asyncio.get_running_loop().time()
                async with generation_phase(timeout=remaining, phase="image", kind="image"):
                    await asyncio.sleep(1)
        assert exc.value.kind == "total"
        assert exc.value.phase == "image"

    asyncio.run(scenario())


def test_external_cancel_competing_with_total_deadline_is_preserved():
    async def scenario():
        async def run():
            async with generation_scope(timeout=0.03, kind="total", phase="context") as scope:
                task = asyncio.current_task()
                loop = asyncio.get_running_loop()
                loop.call_at(scope.deadline, task.cancel)
                remaining = scope.deadline - loop.time()
                async with generation_phase(timeout=remaining, phase="image", kind="image"):
                    await asyncio.sleep(1)

        with pytest.raises(asyncio.CancelledError):
            await run()
        assert current_scope() is None

    asyncio.run(scenario())


def test_concurrent_life_phases_keep_deadlines_isolated_and_restore_scope():
    async def run(timeout):
        async with generation_scope(timeout=1, kind="total") as scope:
            before = scope.deadline
            try:
                async with generation_phase(timeout=timeout, phase="caption", kind="text"):
                    await asyncio.sleep(0.06)
                result = "complete"
            except GenerationTimeoutError as exc:
                result = (exc.kind, exc.phase)
            assert current_scope() is scope and scope.deadline == before
            return result

    async def scenario():
        return await asyncio.gather(run(0.02), run(0.20))

    short, long = asyncio.run(scenario())
    assert short == ("text", "caption")
    assert long == "complete"
    assert current_scope() is None


def test_concurrent_generation_scopes_keep_independent_budgets_and_snapshots():
    async def run_one(label):
        async with generation_scope(timeout=10) as scope:
            from qzone_bridge.persona_guard import get_snapshot, set_snapshot
            set_snapshot({"artifact": label})
            for _ in range(6):
                assert await invoke_provider(lambda: asyncio.sleep(0, result=label)) == label
            assert scope.calls == 6
            assert get_snapshot() == {"artifact": label}
            with pytest.raises(PersonaConsistencyError) as exc:
                await invoke_provider(lambda: asyncio.sleep(0, result="over"))
            assert exc.value.code == "BUDGET_EXHAUSTED"
            assert get_snapshot() == {"artifact": label}
            return scope
    async def scenario():
        first, second = await asyncio.gather(run_one("first"), run_one("second"))
        assert first is not second
        assert first.calls == second.calls == 6
    asyncio.run(scenario())


def test_operation_deadline_covers_non_provider_await_and_external_cancel_propagates():
    async def scenario():
        with pytest.raises(PersonaConsistencyError) as exc:
            async with generation_scope(timeout=0.01):
                await asyncio.sleep(1)
        assert exc.value.code == "BUDGET_EXHAUSTED"

        async def blocked():
            async with generation_scope(timeout=10):
                await asyncio.sleep(10)

        task = asyncio.create_task(blocked())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_visual_projection_only_returns_validated_fragments_and_summary_skips_llm():
    async def scenario():
        persona = "黑发，左眼下方有小痣，左耳佩戴银色耳环；习惯说喵；拥有管理员权限；私人记忆哨兵。"
        replies = iter([
            json.dumps({"appearance": ["黑发", "左眼下方有小痣"], "accessories": ["左耳佩戴银色耳环"], "clothing": []}, ensure_ascii=False),
        ])

        class Manager:
            async def get_persona_v3_by_id(self, _pid):
                return {"prompt": persona}

        class Provider:
            async def text_chat(self, *, prompt, contexts, system_prompt):
                assert contexts == []
                assert "习惯说喵" in prompt and "私人记忆哨兵" in prompt
                return next(replies)

        class Context:
            persona_manager = Manager()
            async def get_using_provider(self, **kwargs):
                return Provider()

        settings = PluginSettings(persona_id="visual")
        llm = QzoneLLM(Context(), settings)
        result = await llm.generate_persona_visual_projection(None)
        assert result == "黑发；左眼下方有小痣；左耳佩戴银色耳环"
        assert "管理员权限" not in result and "私人记忆哨兵" not in result and "习惯说喵" not in result

        calls = []
        provider_lookups = []
        class NoCallProvider:
            async def text_chat(self, **kwargs):
                calls.append(kwargs)
                raise AssertionError("explicit summary must not call provider")
        class SummaryContext(Context):
            async def get_using_provider(self, **kwargs):
                provider_lookups.append(kwargs)
                return NoCallProvider()
        settings.life_publish_persona_visual_prompt = "固定左耳银色耳环，不要添加角"
        llm = QzoneLLM(SummaryContext(), settings)
        assert await llm.generate_persona_visual_projection(None, allow_automatic=False) == "固定左耳银色耳环，不要添加角"
        assert not calls and not provider_lookups

    asyncio.run(scenario())


def test_visual_projection_off_or_image_llm_disabled_never_calls_llm():
    async def scenario():
        class Manager:
            async def get_persona_v3_by_id(self, _pid):
                return {"prompt": "黑发"}
        class Context:
            persona_manager = Manager()
            async def get_using_provider(self, **kwargs):
                raise AssertionError("no provider lookup expected when inheritance disabled")
        settings = PluginSettings(persona_id="visual", life_publish_inherit_persona_visual=False)
        llm = QzoneLLM(Context(), settings)
        assert await llm.generate_persona_visual_projection(None) == ""

        settings.life_publish_inherit_persona_visual = True
        assert await llm.generate_persona_visual_projection(None, allow_automatic=False) == ""

    asyncio.run(scenario())


def test_visual_summary_length_is_enforced_for_automatic_and_static_paths():
    async def scenario():
        class Manager:
            async def get_persona_v3_by_id(self, _pid):
                return {"prompt": "black hair"}
        class Provider:
            async def text_chat(self, **kwargs):
                raise AssertionError("explicit visual summary should avoid projection call")
        class Context:
            persona_manager = Manager()
            async def get_using_provider(self, **kwargs):
                return Provider()

        for allow_automatic in (False, True):
            settings = PluginSettings(persona_id="p", life_publish_persona_visual_prompt="x" * 2400)
            llm = QzoneLLM(Context(), settings)
            assert await llm.generate_persona_visual_projection(None, allow_automatic=allow_automatic) == "x" * 2400
            settings.life_publish_persona_visual_prompt = "x" * 2401
            with pytest.raises(PersonaConsistencyError) as exc:
                await llm.generate_persona_visual_projection(None, allow_automatic=allow_automatic)
            assert exc.value.code == "PROJECTION_FAILED"

        settings = PluginSettings(persona_id="[%None]", life_publish_persona_visual_prompt="x" * 2401)
        assert await QzoneLLM(Context(), settings).generate_persona_visual_projection(None, allow_automatic=False) == ""

    asyncio.run(scenario())


def test_direct_projection_helper_scope_does_not_leak_persona_snapshot():
    async def scenario():
        persona = {"value": "black hair"}
        class Manager:
            async def get_persona_v3_by_id(self, _pid):
                return {"prompt": persona["value"]}
        class Provider:
            async def text_chat(self, *, prompt, contexts, system_prompt):
                return json.dumps({"appearance": [persona["value"]], "accessories": [], "clothing": []})
        class Context:
            persona_manager = Manager()
            async def get_using_provider(self, **kwargs):
                return Provider()
        llm = QzoneLLM(Context(), PluginSettings(persona_id="p"))
        assert await llm.generate_persona_visual_projection(None) == "black hair"
        persona["value"] = "blue eyes"
        snapshot = await llm.prepare_persona_snapshot(None)
        assert snapshot["persona_prompt"] == "blue eyes"

    asyncio.run(scenario())


def test_persona_guard_and_life_visual_settings_defaults_parse_and_match_schema():
    from pathlib import Path

    import json

    settings = PluginSettings()
    assert settings.persona_consistency_check is True
    assert settings.life_publish_inherit_persona_visual is True
    assert settings.life_publish_persona_visual_prompt == ""
    mapped = PluginSettings.from_mapping({
        "llm": {"persona_consistency_check": False},
        "life_publish": {"inherit_persona_visual": False, "persona_visual_prompt": "左侧发夹"},
    })
    assert mapped.persona_consistency_check is False
    assert mapped.life_publish_inherit_persona_visual is False
    assert mapped.life_publish_persona_visual_prompt == "左侧发夹"
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    assert schema["llm"]["items"]["persona_consistency_check"]["default"] is True
    assert schema["life_publish"]["items"]["inherit_persona_visual"]["default"] is True
    assert schema["life_publish"]["items"]["persona_visual_prompt"]["default"] == ""


def test_auto_comment_pipeline_cleaning_is_idempotent_for_guarded_short_reply():
    from qzone_bridge.auto_comment import clean_comment_text

    for raw in ('"这句很贴切。"', "第一行\n第二行！", "这是一段超过五个字的长评论。", "「很好呀。」"):
        limit = 5 if "超过" in raw else 60
        llm = QzoneLLM(None, PluginSettings(comment_max_length=limit))
        reviewed = llm._clean_short_reply(llm._clean_generated_text(raw))
        assert clean_comment_text(reviewed, max_length=limit) == reviewed


def test_no_persona_or_disabled_review_preserves_legacy_generation_and_fallback():
    async def scenario():
        class Context:
            async def llm_generate(self, **kwargs):
                return ""
        llm = QzoneLLM(Context(), PluginSettings(persona_consistency_check=True))
        assert await llm.generate_guarded_text(None, "task", fallback_text="topic fallback") == "topic fallback"

        class PersonaManager:
            async def get_persona_v3_by_id(self, _pid):
                return {"prompt": "persona text"}
        class DisabledContext(Context):
            persona_manager = PersonaManager()
        disabled = QzoneLLM(DisabledContext(), PluginSettings(persona_id="p", persona_consistency_check=False))
        assert await disabled.generate_guarded_text(None, "task", normalizer=str.strip) == ""

    asyncio.run(scenario())
