"""Resolve the AstrBot persona applicable to a Qzone generation request."""

from __future__ import annotations

import inspect
from typing import Any

_UNSET_UMO = object()


class PersonaResolutionError(ValueError):
    """A configured Persona override cannot be resolved."""


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _persona_by_id(manager: Any, persona_id: str) -> Any:
    getter = getattr(manager, "get_persona_v3_by_id", None)
    if not callable(getter):
        getter = getattr(manager, "get_persona", None)
    if not callable(getter):
        raise PersonaResolutionError("当前 AstrBot 未提供按 ID 查询人格的兼容接口。")
    try:
        persona = await _await(getter(persona_id))
    except Exception as exc:
        raise PersonaResolutionError("按 ID 读取 AstrBot 人格失败。") from exc
    if persona is None:
        raise PersonaResolutionError(f"配置的人格 ID 不存在：{persona_id}")
    return persona


def _value(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _validated_umo(value: Any, *, management_chat: bool = True) -> str:
    value = str(value or "").strip()
    try:
        from astrbot.core.platform.message_session import MessageSession

        session = MessageSession.from_str(value)
        if not session.platform_id or not session.session_id or str(session) != value:
            raise ValueError("malformed message session")
        if management_chat and session.message_type.value not in {"GroupMessage", "FriendMessage"}:
            raise ValueError("unsupported message session")
    except Exception as exc:
        raise PersonaResolutionError("llm.context_umo 必须是有效的 AstrBot 群聊或私聊会话标识。") from exc
    return value


def resolve_context_umo(context: Any, settings: Any, event: Any = None) -> str | None:
    """Return an explicit, event-owned, or unambiguous management-chat UMO."""
    synthetic = event is None or bool(getattr(event, "_qzone_context_free", False))
    if not synthetic:
        umo = getattr(event, "unified_msg_origin", None)
        return _validated_umo(umo, management_chat=False) if umo else None
    explicit = str(getattr(settings, "context_umo", "") or "").strip()
    if explicit:
        return _validated_umo(explicit)
    group = int(getattr(settings, "manage_group", 0) or 0)
    session_id = str(group) if group > 0 else ""
    message_type = "GroupMessage" if session_id else "FriendMessage"
    if not session_id:
        admins = getattr(settings, "admin_uins", ()) or ()
        session_id = next((str(admin) for admin in admins if str(admin).isdigit() and int(admin) > 0), "")
    if not session_id:
        return None
    manager = getattr(context, "platform_manager", None)
    getter = getattr(manager, "get_insts", None)
    instances = getter() if callable(getter) else getattr(manager, "platform_insts", ())
    matches = []
    for instance in instances or ():
        try:
            meta = instance.meta()
        except Exception:
            continue
        if _value(meta, "name") == "aiocqhttp" and _value(meta, "id"):
            matches.append(meta)
    if len(matches) != 1:
        return None
    try:
        from astrbot.core.platform.message_session import MessageSession
        from astrbot.core.platform.message_type import MessageType

        kind = MessageType.GROUP_MESSAGE if message_type == "GroupMessage" else MessageType.FRIEND_MESSAGE
        return str(MessageSession(str(_value(matches[0], "id")), kind, session_id))
    except Exception as exc:
        raise PersonaResolutionError("无法构造 AstrBot 会话标识。") from exc


async def resolve_persona_prompt(
    context: Any, settings: Any, event: Any, *, resolved_umo: str | None | object = _UNSET_UMO
) -> str:
    """Return the selected Persona prompt without caching mutable Persona state."""
    synthetic = event is None or bool(getattr(event, "_qzone_context_free", False))
    umo = resolve_context_umo(context, settings, event) if resolved_umo is _UNSET_UMO else resolved_umo
    manager = getattr(context, "persona_manager", None)
    if manager is None:
        if synthetic and str(getattr(settings, "persona_id", "") or "").strip():
            raise PersonaResolutionError("已配置人格 ID，但当前 AstrBot 未提供人格管理器。")
        return ""
    override = str(getattr(settings, "persona_id", "") or "").strip()

    if synthetic and override:
        if override == "[%None]":
            return ""
        persona = await _persona_by_id(manager, override)
        return str(_value(persona, "prompt", "") or _value(persona, "system_prompt", "") or "")

    config = None
    getter = getattr(context, "get_config", None)
    if callable(getter) and umo:
        try:
            config = await _await(getter(umo))
        except Exception as exc:
            raise PersonaResolutionError("读取当前人格配置失败。") from exc
    provider_settings = _value(config, "provider_settings", None)
    conversation_persona_id = None
    conversations = getattr(context, "conversation_manager", None)
    if umo and conversations is not None:
        try:
            cid = await _await(conversations.get_curr_conversation_id(umo))
            if cid:
                conversation = await _await(conversations.get_conversation(umo, cid))
                conversation_persona_id = _value(conversation, "persona_id", None)
        except Exception as exc:
            raise PersonaResolutionError("读取当前会话人格失败。") from exc

    resolver = getattr(manager, "resolve_selected_persona", None)
    persona = None
    resolved_selection = False
    if callable(resolver) and umo:
        platform_name = ""
        if not synthetic and callable(getattr(event, "get_platform_name", None)):
            platform_name = str(event.get_platform_name() or "")
        if not platform_name:
            manager = getattr(context, "platform_manager", None)
            getter = getattr(manager, "get_insts", None)
            for instance in getter() if callable(getter) else getattr(manager, "platform_insts", ()) or ():
                try:
                    meta = instance.meta()
                except Exception:
                    continue
                if str(_value(meta, "id", "")) == str(umo).split(":", 1)[0]:
                    platform_name = str(_value(meta, "name", "") or "")
                    break
        try:
            resolved = await _await(resolver(
                umo=umo,
                conversation_persona_id=conversation_persona_id,
                platform_name=platform_name,
                provider_settings=provider_settings,
            ))
            if isinstance(resolved, (tuple, list)) and len(resolved) > 1:
                resolved_selection = True
                if str(resolved[0] or "") == "[%None]":
                    return ""
                persona = resolved[1]
                if persona is None and resolved[0]:
                    raise PersonaResolutionError("AstrBot 当前选中人格未能解析。")
        except Exception as exc:
            raise PersonaResolutionError("解析 AstrBot 当前人格失败。") from exc
    elif conversation_persona_id and conversation_persona_id != "[%None]":
        persona = await _persona_by_id(manager, str(conversation_persona_id))
    elif conversation_persona_id == "[%None]":
        return ""

    if persona is None and not resolved_selection:
        default_getter = getattr(manager, "get_default_persona_v3", None)
        if callable(default_getter):
            try:
                persona = await _await(default_getter(umo))
            except TypeError:
                try:
                    persona = await _await(default_getter())
                except Exception as exc:
                    raise PersonaResolutionError("读取 AstrBot 默认人格失败。") from exc
            except Exception as exc:
                raise PersonaResolutionError("读取 AstrBot 默认人格失败。") from exc
    return str(_value(persona, "prompt", "") or _value(persona, "system_prompt", "") or "")
