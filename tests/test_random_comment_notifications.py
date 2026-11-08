from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

import pytest
from test_security_hardening import _import_main_with_stubs

from qzone_bridge.settings import PluginSettings


def test_random_comment_notification_is_enabled_by_default() -> None:
    assert PluginSettings.from_mapping({}).random_comment_notify is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [(False, False), (True, True), ("false", False), ("true", True), ("0", False), ("1", True), (None, True)],
)
def test_random_comment_notification_parses_boolean_configuration(value, expected) -> None:
    settings = PluginSettings.from_mapping({"trigger": {"random_comment_notify": value}})

    assert settings.random_comment_notify is expected


@pytest.mark.parametrize("notify", [False, True])
@pytest.mark.parametrize("like", [False, True])
@pytest.mark.parametrize("group_id", [0, 4242])
def test_random_comment_notification_does_not_control_comments_or_likes(
    monkeypatch: pytest.MonkeyPatch,
    notify: bool,
    like: bool,
    group_id: int,
) -> None:
    main = _import_main_with_stubs(monkeypatch)
    monkeypatch.setattr(main.random, "random", lambda: 0.0)
    post = main.QzonePost(hostuin=12345, fid="fid-1", summary="Example post", nickname="Alice")
    service = types.SimpleNamespace(comment_post=AsyncMock(), like_post=AsyncMock())
    event = types.SimpleNamespace(
        get_group_id=lambda: group_id,
        get_sender_id=lambda: 5151,
    )
    plugin = object.__new__(main.QzoneStablePlugin)
    plugin.settings = main.PluginSettings.from_mapping(
        {
            "auto_bind_cookie": False,
            "trigger": {
                "read_prob": 1.0,
                "random_comment_notify": notify,
                "like_when_comment": like,
                "send_admin": False,
            },
        }
    )
    plugin._capture_onebot_client = lambda event: None
    plugin._ensure_cookie_ready = AsyncMock()
    plugin._ensure_daemon = AsyncMock()
    plugin._posts_for_event = AsyncMock(return_value=[post])
    plugin._generate_comment_text = AsyncMock(return_value="nice comment")
    plugin._post_service = lambda: service
    plugin._notify_event_post_card = AsyncMock()

    asyncio.run(plugin.qzone_capture_aiocqhttp_client(event))

    service.comment_post.assert_awaited_once_with(post, "nice comment")
    if like:
        service.like_post.assert_awaited_once_with(post)
    else:
        service.like_post.assert_not_awaited()
    if notify:
        plugin._notify_event_post_card.assert_awaited_once_with(
            event,
            post,
            "已自动评论 Alice 的说说：nice comment",
            comment_text="nice comment",
        )
    else:
        plugin._notify_event_post_card.assert_not_awaited()
    assert plugin._posts_for_event.await_args.kwargs["no_commented"] is True
    assert plugin._posts_for_event.await_args.kwargs["no_self"] is True
