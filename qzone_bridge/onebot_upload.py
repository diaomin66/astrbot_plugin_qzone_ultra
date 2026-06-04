"""Helpers for acquiring Qzone video upload credentials from OneBot clients."""

from __future__ import annotations

import base64
import binascii
import asyncio
from dataclasses import dataclass
import json
import re
from typing import Any

from .onebot_cookie import call_onebot_action


VIDEO_UPLOAD_CREDENTIAL_ACTIONS = (
    "get_qzone_video_upload_credentials",
    "get_video_upload_credentials",
    "get_qzone_video_upload_auth",
    "get_video_upload_auth",
    "get_qzone_upload_credentials",
    "get_upload_credentials",
    "get_qzone_upload_auth",
    "get_upload_auth",
    "get_qq_upload_credentials",
    "get_qq_upload_login_data",
    "get_qq_upload_auth",
    "get_upload_login_data",
    "get_qzone_upload_login_data",
    "get_ntqq_login_data",
    "get_login_data",
    "get_credentials",
    "get_cookies",
    "get_csrf_token",
)
LOGIN_DATA_KEYS = {
    "login_data",
    "logindata",
    "login_data_b64",
    "vlogindata",
    "v_login_data",
    "v_login_data_b64",
    "vLoginData",
    "upload_login_data",
    "uploadLoginData",
    "upload_login_data_b64",
    "qzone_upload_login_data",
    "qzoneUploadLoginData",
    "a2",
    "a2_b64",
}
LOGIN_KEY_KEYS = {
    "login_key",
    "loginkey",
    "login_key_b64",
    "vloginkey",
    "v_login_key",
    "v_login_key_b64",
    "vLoginKey",
    "upload_login_key",
    "uploadLoginKey",
    "upload_login_key_b64",
    "qzone_upload_login_key",
    "qzoneUploadLoginKey",
    "a2_key",
    "a2Key",
}
TOKEN_TYPE_KEYS = {"token_type", "tokenType", "type"}
TOKEN_APPID_KEYS = {"token_appid", "tokenAppid", "appid", "app_id"}
TOKEN_WT_APPID_KEYS = {"token_wt_appid", "tokenWtAppid", "wt_appid", "wtAppid"}
WRAPPER_KEYS = ("data", "result", "retdata", "ret_data", "payload", "response", "credentials", "video_upload")
HEX_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{16,}$")
VIDEO_UPLOAD_ACTION_TIMEOUT_SECONDS = 4.0
WEB_CREDENTIAL_KEYS = {
    "cookie",
    "cookies",
    "bkn",
    "g_tk",
    "gtk",
    "csrf_token",
    "csrfToken",
    "skey",
    "p_skey",
    "pskey",
    "qzonetoken",
}


@dataclass(frozen=True, slots=True)
class OneBotVideoUploadCredentials:
    login_data_b64: str
    login_key_b64: str = ""
    token_type: int = 2
    token_appid: int = 0
    token_wt_appid: int = 0
    source: str = "aiocqhttp"

    def to_request_body(self) -> dict[str, Any]:
        return {
            "login_data_b64": self.login_data_b64,
            "login_key_b64": self.login_key_b64,
            "token_type": self.token_type,
            "token_appid": self.token_appid,
            "token_wt_appid": self.token_wt_appid,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class OneBotVideoUploadProbe:
    credentials: OneBotVideoUploadCredentials | None = None
    attempted_actions: tuple[str, ...] = ()
    returned_actions: tuple[str, ...] = ()
    web_credential_actions: tuple[str, ...] = ()
    error_count: int = 0

    def public_detail(self) -> dict[str, Any]:
        return {
            "credentials_found": self.credentials is not None,
            "attempted_actions": list(self.attempted_actions),
            "returned_actions": list(self.returned_actions),
            "web_credential_actions": list(self.web_credential_actions),
            "error_count": self.error_count,
        }


async def fetch_video_upload_credentials(bot: Any, *, source: str = "aiocqhttp") -> OneBotVideoUploadCredentials | None:
    """Try protocol-end extension actions and return upload credentials if exposed."""

    probe = await probe_video_upload_credentials(bot, source=source)
    return probe.credentials


async def probe_video_upload_credentials(bot: Any, *, source: str = "aiocqhttp") -> OneBotVideoUploadProbe:
    """Probe OneBot standard and extension actions for QQ upload binary material.

    Standard OneBot/NapCat-style ``get_credentials`` usually returns web
    cookies plus csrf/bkn. Those are useful for Qzone web binding, but are not
    enough for the stable mobile ``video_qzone`` upload protocol. This probe
    records cookie-only responses separately and only returns credentials when
    a real vLoginData/A2-like binary field is present.
    """

    attempted: list[str] = []
    returned: list[str] = []
    web_only: list[str] = []
    error_count = 0
    for action in _unique(VIDEO_UPLOAD_CREDENTIAL_ACTIONS):
        for params in _video_upload_action_param_variants():
            attempted.append(_action_label(action, params))
            try:
                payload = await asyncio.wait_for(
                    call_onebot_action(bot, action, **params),
                    timeout=VIDEO_UPLOAD_ACTION_TIMEOUT_SECONDS,
                )
            except Exception:
                error_count += 1
                continue
            returned.append(action)
            credentials = extract_video_upload_credentials(payload, source=f"{source}:{action}")
            if credentials is not None:
                return OneBotVideoUploadProbe(
                    credentials=credentials,
                    attempted_actions=tuple(_unique(attempted)),
                    returned_actions=tuple(_unique(returned)),
                    web_credential_actions=tuple(_unique(web_only)),
                    error_count=error_count,
                )
            if _payload_has_web_credentials(payload):
                web_only.append(action)
    return OneBotVideoUploadProbe(
        credentials=None,
        attempted_actions=tuple(_unique(attempted)),
        returned_actions=tuple(_unique(returned)),
        web_credential_actions=tuple(_unique(web_only)),
        error_count=error_count,
    )


def _video_upload_action_param_variants() -> tuple[dict[str, Any], ...]:
    return (
        {"domain": "qzone.qq.com"},
        {"domain": "user.qzone.qq.com"},
        {"domain": "h5.qzone.qq.com"},
        {"appid": "video_qzone"},
        {"app_id": "video_qzone"},
        {"service": "video_qzone"},
        {"type": "video_qzone"},
        {},
    )


def _action_label(action: str, params: dict[str, Any]) -> str:
    if not params:
        return action
    key, value = next(iter(params.items()))
    return f"{action}:{key}={value}"


def _unique(values: tuple[str, ...] | list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def extract_video_upload_credentials(payload: Any, *, source: str = "aiocqhttp") -> OneBotVideoUploadCredentials | None:
    found = _find_credentials(payload)
    if not found:
        return None
    login_data = found.get("login_data_b64") or ""
    if not login_data:
        return None
    return OneBotVideoUploadCredentials(
        login_data_b64=login_data,
        login_key_b64=found.get("login_key_b64") or "",
        token_type=_as_int(found.get("token_type"), 2),
        token_appid=_as_int(found.get("token_appid"), 0),
        token_wt_appid=_as_int(found.get("token_wt_appid"), 0),
        source=source,
    )


def _find_credentials(payload: Any, *, _depth: int = 0, _seen: set[int] | None = None) -> dict[str, Any] | None:
    if _seen is None:
        _seen = set()
    if payload is None or _depth > 8:
        return None
    if isinstance(payload, bytes):
        encoded = _bytes_to_b64(payload)
        return {"login_data_b64": encoded} if encoded else None
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return None
        if text.startswith("{") or text.startswith("["):
            try:
                return _find_credentials(json.loads(text), _depth=_depth + 1, _seen=_seen)
            except Exception:
                return None
        return None
    if isinstance(payload, (list, tuple)):
        for item in payload:
            found = _find_credentials(item, _depth=_depth + 1, _seen=_seen)
            if found:
                return found
        return None
    if not isinstance(payload, dict):
        return None

    obj_id = id(payload)
    if obj_id in _seen:
        return None
    _seen.add(obj_id)

    result: dict[str, Any] = {}
    for key, value in payload.items():
        normalized = _normalize_key(key)
        if normalized in {_normalize_key(item) for item in LOGIN_DATA_KEYS}:
            encoded = _value_to_b64(value)
            if encoded:
                result["login_data_b64"] = encoded
        elif normalized in {_normalize_key(item) for item in LOGIN_KEY_KEYS}:
            encoded = _value_to_b64(value)
            if encoded:
                result["login_key_b64"] = encoded
        elif normalized in {_normalize_key(item) for item in TOKEN_TYPE_KEYS}:
            result["token_type"] = value
        elif normalized in {_normalize_key(item) for item in TOKEN_APPID_KEYS}:
            result["token_appid"] = value
        elif normalized in {_normalize_key(item) for item in TOKEN_WT_APPID_KEYS}:
            result["token_wt_appid"] = value
    if result.get("login_data_b64"):
        return result

    for key in WRAPPER_KEYS:
        if key in payload:
            found = _find_credentials(payload.get(key), _depth=_depth + 1, _seen=_seen)
            if found:
                return found
    for value in payload.values():
        if isinstance(value, (dict, list, tuple, str, bytes)):
            found = _find_credentials(value, _depth=_depth + 1, _seen=_seen)
            if found:
                return found
    return None


def _payload_has_web_credentials(payload: Any, *, _depth: int = 0, _seen: set[int] | None = None) -> bool:
    if _seen is None:
        _seen = set()
    if payload is None or _depth > 6:
        return False
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return False
        if "uin=" in text and ("skey=" in text or "p_skey=" in text):
            return True
        if text.startswith("{") or text.startswith("["):
            try:
                return _payload_has_web_credentials(json.loads(text), _depth=_depth + 1, _seen=_seen)
            except Exception:
                return False
        return False
    if isinstance(payload, (list, tuple)):
        return any(_payload_has_web_credentials(item, _depth=_depth + 1, _seen=_seen) for item in payload)
    if not isinstance(payload, dict):
        return False
    obj_id = id(payload)
    if obj_id in _seen:
        return False
    _seen.add(obj_id)
    normalized_keys = {_normalize_key(key) for key in WEB_CREDENTIAL_KEYS}
    for key, value in payload.items():
        if _normalize_key(key) in normalized_keys and value not in (None, "", [], {}):
            return True
    return any(
        _payload_has_web_credentials(value, _depth=_depth + 1, _seen=_seen)
        for value in payload.values()
        if isinstance(value, (dict, list, tuple, str))
    )


def _value_to_b64(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return _bytes_to_b64(value)
    if isinstance(value, bytearray):
        return _bytes_to_b64(bytes(value))
    if isinstance(value, list) and all(isinstance(item, int) for item in value):
        try:
            return _bytes_to_b64(bytes(item & 0xFF for item in value))
        except ValueError:
            return ""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("base64://"):
        text = text[len("base64://") :]
    if HEX_RE.match(text):
        raw = text[2:] if text.lower().startswith("0x") else text
        if len(raw) % 2 == 0:
            try:
                return _bytes_to_b64(bytes.fromhex(raw))
            except ValueError:
                return ""
    try:
        decoded = base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        return ""
    return _bytes_to_b64(decoded) if decoded else ""


def _bytes_to_b64(value: bytes) -> str:
    data = bytes(value or b"")
    return base64.b64encode(data).decode("ascii") if data else ""


def _normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
