"""Helpers for acquiring Qzone video upload credentials from OneBot clients."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import json
import re
from typing import Any

from .onebot_cookie import call_onebot_action


VIDEO_UPLOAD_CREDENTIAL_ACTIONS = (
    "get_qzone_video_upload_credentials",
    "get_video_upload_credentials",
    "get_upload_login_data",
    "get_qzone_upload_login_data",
    "get_login_data",
    "get_credentials",
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


async def fetch_video_upload_credentials(bot: Any, *, source: str = "aiocqhttp") -> OneBotVideoUploadCredentials | None:
    """Try protocol-end extension actions and return upload credentials if exposed."""

    for action in VIDEO_UPLOAD_CREDENTIAL_ACTIONS:
        for params in ({"domain": "qzone.qq.com"}, {}):
            try:
                payload = await call_onebot_action(bot, action, **params)
            except Exception:
                continue
            credentials = extract_video_upload_credentials(payload, source=f"{source}:{action}")
            if credentials is not None:
                return credentials
    return None


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
