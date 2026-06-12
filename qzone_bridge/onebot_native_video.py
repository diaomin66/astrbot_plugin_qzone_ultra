"""OneBot protocol-end native Qzone video publish contract.

The stable credential boundary for NapCat/LLBot-like protocol ends is often
the protocol process itself: it can use its internal NTQQ session without
exposing QQ upload A2/vLoginData to AstrBot.  This module defines a small
extension-action contract for that shape and only accepts results that include
the uploaded Qzone video id so the daemon can verify the public feed.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import inspect
import json
from pathlib import Path
import re
from typing import Any

from .errors import QzoneRequestError
from .onebot_cookie import iter_onebot_action_callers


def _with_aliases(*actions: str) -> tuple[str, ...]:
    result: list[str] = []
    for action in actions:
        action = str(action or "").strip()
        if not action:
            continue
        result.append(action)
        if not action.startswith("_"):
            result.append(f"_{action}")
    return tuple(result)


NATIVE_VIDEO_PUBLISH_ACTIONS = _with_aliases(
    "publish_qzone_video_mood",
)

NATIVE_VIDEO_ACTION_TIMEOUT_SECONDS = 7200.0
PUBLIC_VISIBILITY_PARAMS = {
    "who": 1,
    "ugc_right": 1,
    "visibility": "public",
    "permission": "public",
    "privacy": "public",
    "visible": "all",
    "visible_to": "all",
    "right": "public",
    "public": True,
}
NON_PUBLIC_TEXT_RE = re.compile(
    "("
    r"private|only\s*self|friend[s]?\s*only|specified|custom|"
    "\u4ec5\u81ea\u5df1|\u79c1\u5bc6|\u597d\u53cb\u53ef\u89c1|"
    "\u90e8\u5206\u53ef\u89c1|\u6307\u5b9a\u597d\u53cb|\u4e0d\u7ed9\u8c01\u770b"
    ")",
    re.I,
)
UNAVAILABLE_TEXT_RE = re.compile(
    r"unsupported|unknown action|action not found|not implemented|not support|no such action|does not expose|not expose",
    re.I,
)
PUBLIC_ECHO_CONTAINER_KEYS = {
    "request",
    "params",
    "param",
    "input",
    "echo",
    "debug",
    "rawrequest",
    "raw_request",
    "sentparams",
    "sent_params",
    "requestparams",
    "request_params",
}
VID_KEYS = {
    "vid",
    "svid",
    "svideoid",
    "videoid",
    "video_id",
    "qzonevideoid",
    "qzone_video_id",
}
FID_KEYS = {
    "fid",
    "tid",
    "feedid",
    "feed_id",
    "cellid",
    "cell_id",
    "topicid",
    "topic_id",
}
STATUS_KEYS = {"ok", "success", "ret", "retcode", "code", "status", "error", "err", "errno"}
MESSAGE_KEYS = {"message", "msg", "errmsg", "error_msg", "error"}
SENSITIVE_KEYS = {
    "cookie",
    "cookies",
    "p_skey",
    "skey",
    "pskey",
    "clientkey",
    "client_key",
    "token",
    "pt4token",
    "ptkey",
    "qzonetoken",
    "a2",
    "a2b64",
    "a2base64",
    "a2hex",
    "a2bytes",
    "a2ticket",
    "a2ticketb64",
    "a2ticketbase64",
    "a2tickethex",
    "a2ticketbytes",
    "vlogindata",
    "vlogindatab64",
    "vlogindatabase64",
    "vlogindatahex",
    "vlogindatabytes",
    "login_data",
    "logindata",
    "logindatab64",
    "logindatabase64",
    "logindatahex",
    "logindatabytes",
    "login_key",
    "loginkey",
    "loginkeyb64",
    "loginkeybase64",
    "loginkeyhex",
    "loginkeybytes",
    "secret",
    "session",
}


@dataclass(frozen=True, slots=True)
class OneBotNativeVideoPublishResult:
    action: str
    vid: str
    fid: str = ""
    source: str = "onebot"
    raw_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def publish_qzone_video_via_onebot(
    bot: Any,
    *,
    video_path: str | Path,
    content: str,
    sync_weibo: bool = False,
    source: str = "onebot",
) -> OneBotNativeVideoPublishResult | None:
    """Call a OneBot extension action that publishes a public Qzone video mood.

    Returns ``None`` when no action appears to be available.  Raises when a
    protocol end reports success but omits the sVid or reports a non-public
    visibility result, because trying another publish action could duplicate
    the post.
    """

    path = str(Path(video_path))
    for action in _unique(NATIVE_VIDEO_PUBLISH_ACTIONS):
        params = _native_video_publish_params(path, content, sync_weibo=sync_weibo)
        try:
            payload = await asyncio.wait_for(
                _call_native_publish_action_once(bot, action, params),
                timeout=NATIVE_VIDEO_ACTION_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            if _is_action_unavailable_error(exc):
                continue
            raise QzoneRequestError(
                "OneBot Qzone video publish action failed after it was invoked; refusing daemon fallback to avoid duplicate publish",
                detail={"action": action, "error": _safe_exception_summary(exc)},
            ) from exc
        result = _extract_native_video_publish_result(payload, action=action, source=source)
        if result is not None:
            return result
    return None


def _native_video_publish_params(path: str, content: str, *, sync_weibo: bool) -> dict[str, Any]:
    base = {
        "content": str(content or ""),
        "text": str(content or ""),
        "desc": str(content or ""),
        "sync_weibo": bool(sync_weibo),
        "appid": 311,
        **PUBLIC_VISIBILITY_PARAMS,
    }
    media = {"type": "video", "file": path, "path": path}
    return {
        "video_path": path,
        "file_path": path,
        "path": path,
        "file": path,
        "video": media,
        "media": [media],
        **base,
    }


def _extract_native_video_publish_result(
    payload: Any,
    *,
    action: str,
    source: str,
) -> OneBotNativeVideoPublishResult | None:
    if _payload_reports_action_unavailable(payload):
        return None
    if _payload_reports_non_public(payload):
        raise QzoneRequestError(
            "OneBot Qzone video publish action returned non-public visibility",
            detail={"action": action, "raw_summary": _safe_payload_summary(payload)},
        )
    if _payload_reports_failure(payload) or not _payload_reports_success(payload):
        raise QzoneRequestError(
            "OneBot Qzone video publish action returned a failure or ambiguous response after invocation; refusing duplicate publish",
            detail={
                "action": action,
                "raw_summary": _safe_payload_summary(payload),
                "required": "retcode=0 plus sVid plus public visibility marker",
            },
        )
    vid = _find_text_key(payload, VID_KEYS)
    if not vid:
        raise QzoneRequestError(
            "OneBot Qzone video publish action returned success without sVid; refusing to duplicate publish",
            detail={
                "action": action,
                "raw_summary": _safe_payload_summary(payload),
                "required": "sVid",
            },
        )
    if not _payload_reports_public(payload):
        raise QzoneRequestError(
            "OneBot Qzone video publish action did not prove public visibility",
            detail={
                "action": action,
                "raw_summary": _safe_payload_summary(payload),
                "required": "public visibility marker",
            },
        )
    fid = _normalize_fid(_find_text_key(payload, FID_KEYS))
    return OneBotNativeVideoPublishResult(
        action=action,
        vid=vid,
        fid=fid,
        source=source,
        raw_summary=_safe_payload_summary(payload),
    )


def _payload_reports_success(payload: Any) -> bool:
    payload = _json_string_payload(payload)
    data = _unwrap(payload)
    if data is not payload:
        if _payload_reports_failure(data):
            return False
        if _payload_reports_success(data):
            return True
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = _normalize_key(key)
            if normalized == "ok" and value is True:
                return True
            if normalized == "success" and value is True:
                return True
            if normalized in {"ret", "retcode", "code", "err", "errno"}:
                try:
                    return int(value) == 0
                except (TypeError, ValueError):
                    continue
            if normalized == "status" and str(value).lower() in {"ok", "success", "done"}:
                return True
    return False


def _payload_reports_failure(payload: Any, *, _depth: int = 0) -> bool:
    payload = _json_string_payload(payload)
    if payload is None or _depth > 8:
        return False
    if isinstance(payload, (list, tuple)):
        return any(_payload_reports_failure(item, _depth=_depth + 1) for item in payload)
    if not isinstance(payload, dict):
        return False
    data = _unwrap(payload)
    if data is not payload and _payload_reports_failure(data, _depth=_depth + 1):
        return True
    for key, value in payload.items():
        normalized = _normalize_key(key)
        if normalized in {"ok", "success"} and value is False:
            return True
        if normalized in {"ret", "retcode", "code", "err", "errno"}:
            try:
                if int(value) != 0:
                    return True
            except (TypeError, ValueError):
                continue
        if normalized == "status" and str(value).strip().lower() in {
            "fail",
            "failed",
            "failure",
            "error",
            "timeout",
            "denied",
        }:
            return True
    return False


def _payload_reports_action_unavailable(payload: Any, *, _depth: int = 0) -> bool:
    payload = _json_string_payload(payload)
    if payload is None or _depth > 8:
        return False
    if isinstance(payload, str):
        return bool(UNAVAILABLE_TEXT_RE.search(payload))
    if isinstance(payload, (int, float, bool, bytes, bytearray)):
        return False
    if isinstance(payload, (list, tuple)):
        return any(_payload_reports_action_unavailable(item, _depth=_depth + 1) for item in payload)
    if not isinstance(payload, dict):
        return False
    for key, value in payload.items():
        normalized = _normalize_key(key)
        if normalized in MESSAGE_KEYS or normalized in STATUS_KEYS:
            if _payload_reports_action_unavailable(value, _depth=_depth + 1):
                return True
        if normalized in {"data", "result", "retdata", "ret_data", "payload", "response"}:
            if _payload_reports_action_unavailable(value, _depth=_depth + 1):
                return True
    return False


def _payload_reports_public(payload: Any, *, _depth: int = 0) -> bool:
    payload = _json_string_payload(payload)
    if payload is None or _depth > 8:
        return False
    if isinstance(payload, (str, int, float, bool, bytes, bytearray)):
        return False
    if isinstance(payload, (list, tuple)):
        return any(
            _payload_reports_public(item, _depth=_depth + 1)
            for item in payload
            if isinstance(item, (dict, list, tuple))
        )
    if not isinstance(payload, dict):
        return False
    for key, value in payload.items():
        normalized = _normalize_key(key)
        if normalized in PUBLIC_ECHO_CONTAINER_KEYS:
            continue
        if normalized in {"ugcright", "ugc_right", "feedright", "viewright"}:
            text = str(value).strip()
            if text in {"1", "1.0"}:
                return True
        if normalized == "public" and value is True:
            return True
        if normalized in {"visibility", "permission", "privacy", "visible", "visibleto", "right"}:
            if _is_public_visibility_value(value):
                return True
        if isinstance(value, (dict, list, tuple)):
            if _payload_reports_public(value, _depth=_depth + 1):
                return True
    return False


def _is_public_visibility_value(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        value = str(int(value)) if float(value).is_integer() else str(value)
    text = str(value).strip().lower()
    return text in {"1", "public", "all", "everyone", "all_visible", "all-visible"}


def _payload_reports_non_public(payload: Any, *, _depth: int = 0) -> bool:
    payload = _json_string_payload(payload)
    if payload is None or _depth > 8:
        return False
    if isinstance(payload, str):
        return bool(NON_PUBLIC_TEXT_RE.search(payload))
    if isinstance(payload, (int, float, bool, bytes, bytearray)):
        return False
    if isinstance(payload, (list, tuple)):
        return any(_payload_reports_non_public(item, _depth=_depth + 1) for item in payload)
    if not isinstance(payload, dict):
        return False
    for key, value in payload.items():
        normalized = _normalize_key(key)
        if normalized == "public" and value is False:
            return True
        if normalized in {"ugcright", "ugc_right", "feedright", "viewright"}:
            text = str(value).strip()
            if text and text not in {"1", "1.0"}:
                return True
        if normalized in {"visibility", "permission", "privacy", "visible", "visibleto", "right"}:
            if _payload_reports_non_public(str(value), _depth=_depth + 1):
                return True
        if _payload_reports_non_public(value, _depth=_depth + 1):
            return True
    return False


def _find_text_key(value: Any, keys: set[str], *, _depth: int = 0) -> str:
    value = _json_string_payload(value)
    if value is None or _depth > 8:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:
            return ""
    if isinstance(value, str):
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _find_text_key(item, keys, _depth=_depth + 1)
            if found:
                return found
        return ""
    if not isinstance(value, dict):
        return ""
    for key, item in value.items():
        if _normalize_key(key) in {_normalize_key(item_key) for item_key in keys} and item not in (None, "", [], {}):
            return str(item)
    for item in value.values():
        found = _find_text_key(item, keys, _depth=_depth + 1)
        if found:
            return found
    return ""


async def _call_native_publish_action_once(bot: Any, action: str, params: dict[str, Any]) -> Any:
    method = getattr(bot, action, None)
    if callable(method):
        result = method(**params)
        if inspect.isawaitable(result):
            return await result
        return result
    callers = iter_onebot_action_callers(bot)
    if not callers:
        raise AttributeError("OneBot client does not expose a supported action caller")
    result = callers[0](action, **params)
    if inspect.isawaitable(result):
        return await result
    return result


def _normalize_fid(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"^\d+_([^_]+)__\d+$", text)
    return match.group(1) if match else text


def _unwrap(payload: Any) -> Any:
    payload = _json_string_payload(payload)
    if not isinstance(payload, dict):
        return payload
    for key in ("data", "result", "retdata", "ret_data", "payload", "response"):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return value
    return payload


def _safe_payload_summary(payload: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "type": type(payload).__name__,
        "has_vid": bool(_find_text_key(payload, VID_KEYS)),
        "has_fid": bool(_find_text_key(payload, FID_KEYS)),
    }
    if isinstance(payload, dict):
        summary["keys"] = [str(key) for key in list(payload.keys())[:12] if _normalize_key(key) not in SENSITIVE_KEYS]
        for key, value in payload.items():
            normalized = _normalize_key(key)
            if normalized in STATUS_KEYS or normalized in MESSAGE_KEYS:
                summary[str(key)] = _redacted_scalar(value)
    data = _unwrap(payload)
    if isinstance(data, dict) and data is not payload:
        summary["data_keys"] = [
            str(key) for key in list(data.keys())[:12] if _normalize_key(key) not in SENSITIVE_KEYS
        ]
    return summary


def _redacted_scalar(value: Any) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {
            "type": "dict",
            "keys": [str(key) for key in list(value.keys())[:12] if _normalize_key(key) not in SENSITIVE_KEYS],
        }
    if isinstance(value, (list, tuple)):
        return {"type": type(value).__name__, "length": len(value)}
    text = str(value)
    if len(text) <= 160:
        return text
    return text[:157] + "..."


def _normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _json_string_payload(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _is_action_unavailable_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(
        marker in text
        for marker in (
            "unsupported",
            "unknown action",
            "action not found",
            "not implemented",
            "not support",
            "no such action",
            "does not expose",
            "not expose",
            "not found",
            "娌℃湁鏂规硶",
            "涓嶆敮鎸?",
        )
    )


def _safe_exception_summary(exc: Exception) -> dict[str, Any]:
    return {"type": type(exc).__name__, "message": _redacted_scalar(str(exc))}


def _unique(items: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)
