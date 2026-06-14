"""Text renderers for human and LLM-facing output."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .models import FeedEntry
from .utils import to_local_time_text, truncate


def cookie_summary(cookies: dict[str, str]) -> str:
    if not cookies:
        return "无 Cookie"
    keys = [
        "uin",
        "p_uin",
        "skey",
        "p_skey",
        "pskey",
        "g_tk",
        "gtk",
        "bkn",
        "csrf_token",
        "pt4_token",
        "pt_key",
        "qqmusic_key",
        "lvkey",
    ]
    found = [key for key in keys if key in cookies]
    extras = len(cookies) - len(found)
    return f"{len(cookies)} 个 Cookie: " + ", ".join(found + ([f"另 {extras} 个"] if extras > 0 else []))


def format_status(status: dict) -> str:
    """Render `/qzone status` as a compact Chinese summary.

    The controller keeps many diagnostic fields for logs, tests and local APIs.
    Chat commands should not dump those internals directly; administrators only
    need to know whether the service, login state and video publishing path are
    usable, plus one actionable hint when something is wrong.
    """

    needs_rebind = bool(status.get("needs_rebind"))
    lines = [
        "QQ 空间状态",
        f"- 服务：{_format_daemon_status(status)}",
        f"- 账号：{_format_account_status(status, needs_rebind=needs_rebind)}",
        f"- 视频直发：{_format_video_status(status, needs_rebind=needs_rebind)}",
    ]
    hint = _status_hint(status, needs_rebind=needs_rebind)
    if hint:
        lines.append(f"- 提示：{hint}")
    return "\n".join(lines)


def _format_daemon_status(status: dict[str, Any]) -> str:
    state = str(status.get("daemon_state") or "unknown").strip().lower()
    port = _to_int(status.get("daemon_port"), 0)
    state_text = {
        "ready": "正常",
        "needs_rebind": "正常",
        "starting": "启动中",
        "stopped": "未运行",
        "degraded": "异常",
        "unknown": "未知",
    }.get(state, state or "未知")
    if port > 0:
        return f"{state_text}（127.0.0.1:{port}）"
    return state_text


def _format_account_status(status: dict[str, Any], *, needs_rebind: bool) -> str:
    login_uin = _to_int(status.get("login_uin") or status.get("uin"), 0)
    count = _cookie_count(status)
    if login_uin <= 0:
        return "未绑定"
    if needs_rebind:
        if count > 0:
            return f"{login_uin}（需重新绑定，{count} 个 Cookie）"
        return f"{login_uin}（需重新绑定）"
    if count > 0:
        return f"{login_uin}（已绑定，{count} 个 Cookie）"
    summary = str(status.get("cookie_summary") or "").strip()
    if summary and summary not in {"-", "无 Cookie"}:
        return f"{login_uin}（已绑定）"
    return str(login_uin)


def _format_video_status(status: dict[str, Any], *, needs_rebind: bool) -> str:
    video_upload = status.get("video_upload")
    if needs_rebind:
        return "不可用（请先绑定登录态）"
    if not isinstance(video_upload, dict):
        return "不可用"

    web_cookie_ready = bool(video_upload.get("web_cookie_configured") or video_upload.get("h5_upload_available"))
    h5_publish_ready = bool(video_upload.get("h5_publish_supported") and web_cookie_ready)
    ready = h5_publish_ready or bool(video_upload.get("ready") and web_cookie_ready)
    if ready:
        return "可用（公开视频校验）"
    if web_cookie_ready or video_upload.get("h5_upload_diagnostic_available"):
        return "不可用（仅上传诊断可用）"
    return "不可用"


def _status_hint(status: dict[str, Any], *, needs_rebind: bool) -> str:
    if needs_rebind:
        return "发送 /qzone autobind 自动绑定，或使用 /qzone bind <cookie> 手动绑定。"
    start_error = status.get("daemon_start_error")
    message = ""
    if isinstance(start_error, dict):
        message = str(start_error.get("message") or "").strip()
    elif start_error:
        message = str(start_error).strip()
    if not message:
        last_error = status.get("last_error")
        if isinstance(last_error, dict):
            message = str(last_error.get("message") or last_error.get("error") or "").strip()
        elif last_error:
            message = str(last_error).strip()
    return truncate(message, 120) if message else ""


def _cookie_count(status: dict[str, Any]) -> int:
    count = _to_int(status.get("cookie_count"), -1)
    if count >= 0:
        return count
    summary = str(status.get("cookie_summary") or "")
    for part in summary.split():
        value = _to_int(part, -1)
        if value >= 0:
            return value
    return 0


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_feed_entry(entry: FeedEntry, index: int | None = None, *, include_internal: bool = True) -> str:
    prefix = f"{index}. " if index is not None else "- "
    headline = truncate(entry.summary or "(empty)", 90)
    lines = [f"{prefix}{to_local_time_text(entry.created_at)} | {entry.nickname or entry.hostuin}"]
    if include_internal:
        lines.append(
            f"   fid={entry.fid} appid={entry.appid} "
            f"like={entry.like_count} comment={entry.comment_count} liked={entry.liked}"
        )
    else:
        liked_text = "已赞" if entry.liked else "未赞"
        lines.append(f"   {liked_text} | {entry.like_count} 赞 | {entry.comment_count} 评论")
    lines.append(f"   {headline}")
    return "\n".join(lines)


def format_feed_list(
    entries: Iterable[FeedEntry],
    *,
    cursor: str = "",
    has_more: bool = False,
    include_internal: bool = True,
    include_pagination: bool = True,
) -> str:
    rendered = [
        format_feed_entry(entry, i + 1, include_internal=include_internal)
        for i, entry in enumerate(entries)
    ]
    footer = []
    if include_pagination:
        if cursor:
            footer.append(f"cursor={cursor}")
        footer.append(f"has_more={has_more}")
    body = "\n".join(rendered) if rendered else "(no feeds)"
    return "\n".join([body, *footer])


def format_llm_feed_list(entries: Iterable[FeedEntry]) -> str:
    entries = list(entries)
    if not entries:
        return "没有找到可展示的说说。"
    body = format_feed_list(entries, include_internal=False, include_pagination=False)
    return f"{body}\n可以用上面的序号继续指定要查看或操作的说说。"


def format_feed_detail(entry: FeedEntry) -> str:
    lines = [
        "说说详情",
        f"- hostuin: {entry.hostuin}",
        f"- fid: {entry.fid}",
        f"- appid: {entry.appid}",
        f"- time: {to_local_time_text(entry.created_at)}",
        f"- like: {entry.like_count}",
        f"- comment: {entry.comment_count}",
        f"- liked: {entry.liked}",
        f"- summary: {entry.summary or '(empty)'}",
    ]
    return "\n".join(lines)


def format_action_result(title: str, payload: dict) -> str:
    parts = [title]
    for key, value in payload.items():
        if key in {"raw", "detail"}:
            continue
        if isinstance(value, (dict, list)):
            continue
        parts.append(f"- {key}: {value}")
    return "\n".join(parts)


def format_like_result(payload: dict) -> str:
    action = "取消点赞" if payload.get("action") == "unlike" else "点赞"
    summary = truncate(str(payload.get("summary") or ""), 80)
    suffix = f"「{summary}」" if summary else ""
    if payload.get("verified"):
        if payload.get("already"):
            state = "已经是目标状态"
        else:
            state = "已完成"
        liked = "当前已点赞" if payload.get("liked") else "当前未点赞"
        return f"{action}{state}{suffix}，{liked}。"
    return f"{action}已受理，QQ 空间可能还在同步{suffix}。"
