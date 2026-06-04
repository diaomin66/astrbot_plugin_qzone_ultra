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
    "get_login_info",
    "get_credentials",
    "get_cookies",
    "get_csrf_token",
)
LOGIN_MISC_DATA_KEYS = (
    "a2",
    "A2",
    "vLoginData",
    "v_login_data",
    "loginData",
    "login_data",
    "uploadLoginData",
    "qzoneUploadLoginData",
)
ONEBOT_LOGIN_MISC_ACTIONS = (
    "get_login_misc_data",
    "get_ntqq_login_misc_data",
    "get_qq_login_misc_data",
    "get_qzone_login_misc_data",
)
LOGIN_MISC_ACTION_PARAM_VARIANTS: tuple[dict[str, str], ...] = tuple(
    params
    for key in LOGIN_MISC_DATA_KEYS
    for params in (
        {"key": key},
        {"name": key},
        {"field": key},
    )
)
PROTOCOL_ENDPOINT_ACTION_ATTEMPTS: tuple[tuple[str, dict[str, Any]], ...] = (
    *(
        (action, params)
        for action in ONEBOT_LOGIN_MISC_ACTIONS
        for params in LOGIN_MISC_ACTION_PARAM_VARIANTS
    ),
    ("llonebot_debug", {"apiClass": "ntUserApi", "method": "getA2", "args": []}),
    ("llonebot_debug", {"apiClass": "ntUserApi", "method": "getA2Bytes", "args": []}),
    ("llonebot_debug", {"apiClass": "ntUserApi", "method": "getQQUploadData", "args": []}),
    ("llonebot_debug", {"apiClass": "ntUserApi", "method": "getQzoneUploadData", "args": []}),
    ("llonebot_debug", {"apiClass": "pmhq", "method": "call", "args": ["getSelfInfo", []]}),
    *(
        (
            "llonebot_debug",
            {
                "apiClass": "pmhq",
                "method": "invoke",
                "args": ["nodeIKernelLoginService/getLoginMiscData", [key]],
            },
        )
        for key in LOGIN_MISC_DATA_KEYS
    ),
    ("get_clientkey", {}),
    ("get_client_key", {}),
    ("get_ntqq_clientkey", {}),
    ("get_ntqq_client_key", {}),
    ("llonebot_debug", {"apiClass": "ntUserApi", "method": "forceFetchClientKey", "args": []}),
    (
        "llonebot_debug",
        {"apiClass": "pmhq", "method": "invoke", "args": ["nodeIKernelTicketService/forceFetchClientKey", [""]]},
    ),
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
CLIENT_KEY_KEYS = {
    "clientkey",
    "client_key",
    "clientKey",
    "keyindex",
    "keyIndex",
}
RAW_LOGIN_DATA_METHOD_HINTS = {
    "geta2",
    "geta2bytes",
    "getqquploaddata",
    "getqzoneuploaddata",
    "getloginmiscdata",
    "nodeikernelloginservicegetloginmiscdata",
}
RAW_LOGIN_DATA_ACTION_HINTS = {
    "getqzonevideouploadcredentials",
    "getvideouploadcredentials",
    "getqzonevideouploadauth",
    "getvideouploadauth",
    "getqzoneuploadcredentials",
    "getuploadcredentials",
    "getqzoneuploadauth",
    "getuploadauth",
    "getqquploadcredentials",
    "getqquploadlogindata",
    "getqquploadauth",
    "getuploadlogindata",
    "getqzoneuploadlogindata",
    "getntqqlogindata",
    "getlogindata",
    "getloginmiscdata",
    "getntqqloginmiscdata",
    "getqqloginmiscdata",
    "getqzoneloginmiscdata",
    "getqquploada2",
    "getqzoneuploada2",
    "getvideouploada2",
}
RAW_LOGIN_DATA_WRAPPER_KEYS = WRAPPER_KEYS + ("value", "ticket", "buffer")
MIN_RAW_LOGIN_DATA_BYTES = 8


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
    client_key_actions: tuple[str, ...] = ()
    error_count: int = 0

    def public_detail(self) -> dict[str, Any]:
        return {
            "credentials_found": self.credentials is not None,
            "attempted_actions": list(self.attempted_actions),
            "returned_actions": list(self.returned_actions),
            "web_credential_actions": list(self.web_credential_actions),
            "client_key_actions": list(self.client_key_actions),
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
    client_key_only: list[str] = []
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
            source_name = f"{source}:{action}"
            credentials = extract_video_upload_credentials(payload, source=source_name)
            if credentials is None and _action_may_return_raw_login_data(action, params):
                credentials = _extract_raw_login_data_payload(
                    payload,
                    source=source_name,
                    trusted_raw=_action_targets_login_data(action, params),
                )
            if credentials is not None:
                return OneBotVideoUploadProbe(
                    credentials=credentials,
                    attempted_actions=tuple(_unique(attempted)),
                    returned_actions=tuple(_unique(returned)),
                    web_credential_actions=tuple(_unique(web_only)),
                    client_key_actions=tuple(_unique(client_key_only)),
                    error_count=error_count,
                )
            if _payload_has_web_credentials(payload):
                web_only.append(action)
            if _payload_has_client_key(payload):
                client_key_only.append(action)
    for action, params in PROTOCOL_ENDPOINT_ACTION_ATTEMPTS:
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
        source_name = f"{source}:{action}"
        credentials = extract_video_upload_credentials(payload, source=source_name)
        if credentials is None and _action_may_return_raw_login_data(action, params):
            credentials = _extract_raw_login_data_payload(
                payload,
                source=source_name,
                trusted_raw=_action_targets_login_data(action, params),
            )
        if credentials is not None:
            return OneBotVideoUploadProbe(
                credentials=credentials,
                attempted_actions=tuple(_unique(attempted)),
                returned_actions=tuple(_unique(returned)),
                web_credential_actions=tuple(_unique(web_only)),
                client_key_actions=tuple(_unique(client_key_only)),
                error_count=error_count,
            )
        if _payload_has_web_credentials(payload):
            web_only.append(action)
        if _payload_has_client_key(payload):
            client_key_only.append(action)
    return OneBotVideoUploadProbe(
        credentials=None,
        attempted_actions=tuple(_unique(attempted)),
        returned_actions=tuple(_unique(returned)),
        web_credential_actions=tuple(_unique(web_only)),
        client_key_actions=tuple(_unique(client_key_only)),
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
    parts = [f"{key}={_safe_label_value(value)}" for key, value in sorted(params.items())]
    return f"{action}:{','.join(parts)}"


def _safe_label_value(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(f"{key}:{_safe_label_value(val)}" for key, val in sorted(value.items())) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_safe_label_value(item) for item in value) + "]"
    text = str(value or "")
    if len(text) > 80:
        return text[:77] + "..."
    return text


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

    normalized_login_data_keys = {_normalize_key(item) for item in LOGIN_DATA_KEYS}
    normalized_login_key_keys = {_normalize_key(item) for item in LOGIN_KEY_KEYS}
    normalized_token_type_keys = {_normalize_key(item) for item in TOKEN_TYPE_KEYS}
    normalized_token_appid_keys = {_normalize_key(item) for item in TOKEN_APPID_KEYS}
    normalized_token_wt_appid_keys = {_normalize_key(item) for item in TOKEN_WT_APPID_KEYS}
    normalized_client_keys = {_normalize_key(key) for key in CLIENT_KEY_KEYS}
    normalized_web_keys = {_normalize_key(key) for key in WEB_CREDENTIAL_KEYS}

    result: dict[str, Any] = {}
    for key, value in payload.items():
        normalized = _normalize_key(key)
        if normalized in normalized_login_data_keys:
            encoded = _value_to_b64(value)
            if encoded:
                result["login_data_b64"] = encoded
        elif normalized in normalized_login_key_keys:
            encoded = _value_to_b64(value)
            if encoded:
                result["login_key_b64"] = encoded
        elif normalized in normalized_token_type_keys:
            result["token_type"] = value
        elif normalized in normalized_token_appid_keys:
            result["token_appid"] = value
        elif normalized in normalized_token_wt_appid_keys:
            result["token_wt_appid"] = value
    if result.get("login_data_b64"):
        return result

    for key in WRAPPER_KEYS:
        if key in payload:
            found = _find_credentials(payload.get(key), _depth=_depth + 1, _seen=_seen)
            if found:
                return found
    for key, value in payload.items():
        if _normalize_key(key) in normalized_client_keys or _normalize_key(key) in normalized_web_keys:
            continue
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


def _payload_has_client_key(payload: Any, *, _depth: int = 0, _seen: set[int] | None = None) -> bool:
    if _seen is None:
        _seen = set()
    if payload is None or _depth > 6:
        return False
    if isinstance(payload, bytes):
        return False
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return False
        lowered = text.lower()
        if "clientkey=" in lowered or "client_key=" in lowered or '"clientkey"' in lowered or '"client_key"' in lowered:
            return True
        if text.startswith("{") or text.startswith("["):
            try:
                return _payload_has_client_key(json.loads(text), _depth=_depth + 1, _seen=_seen)
            except Exception:
                return False
        return False
    if isinstance(payload, (list, tuple)):
        return any(_payload_has_client_key(item, _depth=_depth + 1, _seen=_seen) for item in payload)
    if not isinstance(payload, dict):
        return False
    obj_id = id(payload)
    if obj_id in _seen:
        return False
    _seen.add(obj_id)
    normalized_keys = {_normalize_key(key) for key in CLIENT_KEY_KEYS}
    for key, value in payload.items():
        if _normalize_key(key) in normalized_keys and value not in (None, "", [], {}):
            return True
    return any(
        _payload_has_client_key(value, _depth=_depth + 1, _seen=_seen)
        for value in payload.values()
        if isinstance(value, (dict, list, tuple, str))
    )


def _action_may_return_raw_login_data(action: str, params: dict[str, Any] | None = None) -> bool:
    normalized_action = _normalize_key(action)
    if normalized_action in RAW_LOGIN_DATA_ACTION_HINTS:
        return True
    params = params or {}
    method = _normalize_key(params.get("method"))
    if method in RAW_LOGIN_DATA_METHOD_HINTS:
        return True
    args = params.get("args")
    if isinstance(args, (list, tuple)):
        return any(_normalize_key(item) in RAW_LOGIN_DATA_METHOD_HINTS for item in args if isinstance(item, str))
    return False


def _action_targets_login_data(action: str, params: dict[str, Any] | None = None) -> bool:
    """Return True when the action/params explicitly name A2/vLoginData.

    Some OneBot protocol ends include ``clientKey``/``keyIndex`` in generic
    ticket responses.  Those are Web jump-login materials and must not be
    accepted as Tencent-upload A2.  For targeted login-misc calls, however,
    wrappers may return bookkeeping fields next to a raw ``value``/``data``
    buffer; in that case the raw value is still the requested A2/vLoginData.
    """

    normalized_action = _normalize_key(action)
    params = params or {}
    normalized_login_keys = {_normalize_key(item) for item in (*LOGIN_MISC_DATA_KEYS, *LOGIN_DATA_KEYS)}

    if normalized_action in {_normalize_key(item) for item in ONEBOT_LOGIN_MISC_ACTIONS}:
        for key in ("key", "name", "field"):
            if _normalize_key(params.get(key)) in normalized_login_keys:
                return True

    method = _normalize_key(params.get("method"))
    if method in {"geta2", "geta2bytes", "getqquploaddata", "getqzoneuploaddata"}:
        return True
    args = params.get("args")
    if isinstance(args, (list, tuple)) and args:
        if _normalize_key(args[0]) == "nodeikernelloginservicegetloginmiscdata":
            values = args[1] if len(args) > 1 else []
            if isinstance(values, (list, tuple)):
                return any(_normalize_key(item) in normalized_login_keys for item in values)
            return _normalize_key(values) in normalized_login_keys
    return False


def _extract_raw_login_data_payload(
    payload: Any,
    *,
    source: str = "aiocqhttp",
    trusted_raw: bool = False,
) -> OneBotVideoUploadCredentials | None:
    if _payload_has_client_key(payload) and not trusted_raw:
        return None
    encoded = _find_raw_login_data(payload)
    if not encoded:
        return None
    return OneBotVideoUploadCredentials(login_data_b64=encoded, source=source)


def _find_raw_login_data(payload: Any, *, _depth: int = 0, _seen: set[int] | None = None) -> str:
    if _seen is None:
        _seen = set()
    if payload is None or _depth > 8:
        return ""
    if isinstance(payload, (bytes, bytearray)):
        return _raw_scalar_to_b64(payload)
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ""
        if text.startswith("{") or text.startswith("["):
            try:
                return _find_raw_login_data(json.loads(text), _depth=_depth + 1, _seen=_seen)
            except Exception:
                return ""
        return _raw_scalar_to_b64(text)
    if isinstance(payload, (list, tuple)):
        if all(isinstance(item, int) for item in payload):
            return _raw_scalar_to_b64(payload)
        for item in payload:
            found = _find_raw_login_data(item, _depth=_depth + 1, _seen=_seen)
            if found:
                return found
        return ""
    if not isinstance(payload, dict):
        return ""

    obj_id = id(payload)
    if obj_id in _seen:
        return ""
    _seen.add(obj_id)

    normalized_client_keys = {_normalize_key(key) for key in CLIENT_KEY_KEYS}
    for key in RAW_LOGIN_DATA_WRAPPER_KEYS:
        if key in payload and _normalize_key(key) not in normalized_client_keys:
            found = _find_raw_login_data(payload.get(key), _depth=_depth + 1, _seen=_seen)
            if found:
                return found
    for key, value in payload.items():
        normalized = _normalize_key(key)
        if normalized in normalized_client_keys or normalized in {_normalize_key(item) for item in WEB_CREDENTIAL_KEYS}:
            continue
        if normalized in {_normalize_key(item) for item in LOGIN_DATA_KEYS}:
            found = _raw_scalar_to_b64(value)
            if found:
                return found
        if isinstance(value, (dict, list, tuple)):
            found = _find_raw_login_data(value, _depth=_depth + 1, _seen=_seen)
            if found:
                return found
    return ""


def _raw_scalar_to_b64(value: Any) -> str:
    encoded = _value_to_b64(value)
    if not encoded:
        return ""
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return ""
    return encoded if len(decoded) >= MIN_RAW_LOGIN_DATA_BYTES else ""


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
