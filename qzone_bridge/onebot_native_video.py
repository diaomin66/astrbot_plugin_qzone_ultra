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
import json
from pathlib import Path
import re
from typing import Any

from .errors import QzoneRequestError
from .onebot_cookie import call_onebot_action


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
    "publish_qzone_video_shuoshuo",
    "publish_qzone_video_post",
    "qzone_publish_video_mood",
    "qzone_publish_video_shuoshuo",
    "qzone_publish_video_post",
    "upload_qzone_video_mood",
    "upload_qzone_video_shuoshuo",
    "upload_qzone_video_post",
    "publish_qzone_video",
    "qzone_publish_video",
    "upload_qzone_video",
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
    "a2",
    "vlogindata",
    "login_data",
    "login_key",
    "secret",
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
        for params in _native_video_publish_param_variants(path, content, sync_weibo=sync_weibo):
            try:
                payload = await asyncio.wait_for(
                    call_onebot_action(bot, action, **params),
                    timeout=NATIVE_VIDEO_ACTION_TIMEOUT_SECONDS,
                )
            except Exception:
                continue
            result = _extract_native_video_publish_result(payload, action=action, source=source)
            if result is not None:
                return result
            if _payload_reports_success(payload):
                raise QzoneRequestError(
                    "OneBot Qzone video publish action returned success without sVid; refusing to duplicate publish",
                    detail={
                        "action": action,
                        "raw_summary": _safe_payload_summary(payload),
                        "required": "sVid",
                    },
                )
            if _payload_reports_non_public(payload):
                raise QzoneRequestError(
                    "OneBot Qzone video publish action returned non-public visibility",
                    detail={"action": action, "raw_summary": _safe_payload_summary(payload)},
                )
    return None


def _native_video_publish_param_variants(path: str, content: str, *, sync_weibo: bool) -> tuple[dict[str, Any], ...]:
    base = {
        "content": str(content or ""),
        "text": str(content or ""),
        "desc": str(content or ""),
        "sync_weibo": bool(sync_weibo),
        "appid": 311,
        **PUBLIC_VISIBILITY_PARAMS,
    }
    media = {"type": "video", "file": path, "path": path}
    return (
        {"video_path": path, "file_path": path, **base},
        {"path": path, **base},
        {"file": path, **base},
        {"video": media, **base},
        {"media": [media], **base},
    )


def _extract_native_video_publish_result(
    payload: Any,
    *,
    action: str,
    source: str,
) -> OneBotNativeVideoPublishResult | None:
    if _payload_reports_non_public(payload):
        raise QzoneRequestError(
            "OneBot Qzone video publish action returned non-public visibility",
            detail={"action": action, "raw_summary": _safe_payload_summary(payload)},
        )
    vid = _find_text_key(payload, VID_KEYS)
    if not vid:
        return None
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
    if data is not payload:
        return _payload_reports_success(data)
    return False


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
        if normalized in {"ugcright", "ugc_right", "feedright", "viewright"}:
            text = str(value).strip()
            if text and text not in {"1", "1.0"}:
                return True
        if normalized in {"visibility", "permission", "privacy", "visible", "right"}:
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


def _unique(items: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)
