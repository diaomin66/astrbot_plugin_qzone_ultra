"""Qzone H5 sliceUpload helpers for daemon-side native video publishing.

This path uses the same Web/H5 cookie material that Qzone pages already need
(`p_skey` + `g_tk`) and does not depend on QQ upload A2/vLoginData material.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
from pathlib import Path
import time
from typing import Any
from urllib.parse import quote
import uuid

from .models import SessionState
from .parser import cookie_gtk, normalize_cookie_fields, unwrap_payload


QZONE_H5_UPLOAD_ORIGIN = "https://h5.qzone.qq.com"
QZONE_H5_VIDEO_APPID = "video_qzone"
QZONE_H5_VIDEO_TOKEN_TYPE = 4
QZONE_H5_VIDEO_TOKEN_APPID = 5
QZONE_H5_VIDEO_CONTROL_CMD = "FileUploadVideo"
QZONE_H5_VIDEO_CHECK_TYPE_SHA1 = 1
QZONE_H5_DEFAULT_SLICE_SIZE = 256 * 1024


@dataclass(frozen=True, slots=True)
class QzoneH5VideoUploadResult:
    vid: str
    checksum: str
    uploaded_bytes: int
    session: str = ""
    slice_size: int = 0
    control_response: dict[str, Any] = field(default_factory=dict)
    upload_responses: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def qzone_h5_video_upload_available(session: SessionState | None) -> bool:
    """Return whether the stored Web session can attempt H5 native video upload."""

    if session is None or not int(getattr(session, "uin", 0) or 0):
        return False
    cookies = normalize_cookie_fields(dict(getattr(session, "cookies", {}) or {}))
    return bool(cookies.get("p_skey") and h5_video_gtk(cookies))


def h5_video_token_data(session: SessionState) -> str:
    cookies = normalize_cookie_fields(dict(session.cookies or {}))
    return str(cookies.get("p_skey") or "")


def h5_video_gtk(cookies: dict[str, str]) -> int:
    """Return the csrf token used by H5 sliceUpload.

    The upload token body uses p_skey, but the H5 sliceUpload URL matches
    LLBot's observed flow and uses bkn/g_tk derived from skey when available.
    Falling back to p_skey keeps manually bound minimal cookies usable.
    """

    normalized = normalize_cookie_fields(dict(cookies or {}))
    direct = str(normalized.get("bkn") or normalized.get("g_tk") or normalized.get("gtk") or "").strip()
    if direct.isdigit():
        return int(direct)
    skey = str(normalized.get("skey") or "").strip()
    if skey:
        return cookie_gtk({"skey": skey})
    return cookie_gtk(normalized)


def sha1_file(path: str | Path) -> str:
    digest = hashlib.sha1()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def h5_video_format(path: str | Path, default: str = "mp4") -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix or default


def build_h5_video_control_payload(
    *,
    uin: int | str,
    p_skey: str,
    checksum: str,
    file_size: int,
    title: str = "",
    desc: str = "",
    play_time: int = 0,
    upload_time: int | None = None,
    video_format: str = "mp4",
    extend_info: dict[str, str] | None = None,
) -> dict[str, Any]:
    upload_time = int(upload_time if upload_time is not None else time.time())
    video_extend = {str(key): str(value) for key, value in dict(extend_info or {}).items()}
    video_extend.setdefault("video_type", "3")
    video_extend.setdefault("qz_video_format", str(video_format or "mp4").lstrip(".") or "mp4")
    return {
        "control_req": [
            {
                "uin": str(uin),
                "token": {
                    "type": QZONE_H5_VIDEO_TOKEN_TYPE,
                    "data": str(p_skey),
                    "appid": QZONE_H5_VIDEO_TOKEN_APPID,
                },
                "appid": QZONE_H5_VIDEO_APPID,
                "checksum": str(checksum),
                "check_type": QZONE_H5_VIDEO_CHECK_TYPE_SHA1,
                "file_len": int(file_size),
                "env": {
                    "refer": "qzone",
                    "deviceInfo": "h5",
                },
                "model": 0,
                "biz_req": {
                    "sTitle": str(title or ""),
                    "sDesc": str(desc or ""),
                    "iFlag": 0,
                    "iUploadTime": upload_time,
                    "iPlayTime": max(0, int(play_time or 0)),
                    "sCoverUrl": "",
                    "iIsNew": 111,
                    "iIsOriginalVideo": 0,
                    "iIsFormatF20": 0,
                    "extend_info": video_extend,
                },
                "session": "",
                "asy_upload": 0,
                "cmd": QZONE_H5_VIDEO_CONTROL_CMD,
            }
        ]
    }


def h5_video_control_url(checksum: str) -> str:
    return f"{QZONE_H5_UPLOAD_ORIGIN}/webapp/json/sliceUpload/FileBatchControl/{checksum}"


def h5_video_slice_url() -> str:
    return f"{QZONE_H5_UPLOAD_ORIGIN}/webapp/json/sliceUpload/{QZONE_H5_VIDEO_CONTROL_CMD}"


def _multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n'
        "\r\n"
        f"{value}\r\n"
    ).encode("utf-8")


def _multipart_blob(
    boundary: str,
    name: str,
    filename: str,
    data: bytes,
    *,
    content_type: str | None = "application/octet-stream",
) -> bytes:
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
    )
    if content_type is not None:
        header += f"Content-Type: {content_type}\r\n"
    header += "\r\n"
    return header.encode("utf-8") + data + b"\r\n"


def encode_h5_video_slice_multipart(
    *,
    uin: int | str,
    session: str,
    seq: int,
    offset: int,
    end: int,
    slice_size: int,
    chunk: bytes,
    boundary: str | None = None,
    data_content_type: str | None = "application/octet-stream",
) -> tuple[bytes, str]:
    boundary = boundary or f"qzoneh5{uuid.uuid4().hex}"
    fields_before_blob = [
        ("uin", str(uin)),
        ("appid", QZONE_H5_VIDEO_APPID),
    ]
    fields_after_blob = [
        ("session", str(session)),
        ("offset", str(int(offset))),
        ("checksum", ""),
        ("check_type", "0"),
        ("retry", "0"),
        ("seq", str(int(seq))),
        ("end", str(int(end))),
        ("cmd", QZONE_H5_VIDEO_CONTROL_CMD),
        ("slice_size", str(int(slice_size))),
        ("biz_req.iUploadType", "0"),
    ]
    body = bytearray()
    for name, value in fields_before_blob:
        body.extend(_multipart_field(boundary, name, value))
    body.extend(
        _multipart_blob(
            boundary,
            "data",
            "blob",
            bytes(chunk or b""),
            content_type=data_content_type,
        )
    )
    for name, value in fields_after_blob:
        body.extend(_multipart_field(boundary, name, value))
    body.extend(f"--{boundary}--\r\n".encode("ascii"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def extract_h5_control_session(payload: Any) -> tuple[str, int]:
    data = unwrap_payload(payload)
    if not isinstance(data, dict):
        return "", QZONE_H5_DEFAULT_SLICE_SIZE
    session = str(data.get("session") or data.get("Session") or "")
    raw_slice_size = data.get("slice_size") or data.get("sliceSize") or QZONE_H5_DEFAULT_SLICE_SIZE
    try:
        slice_size = int(raw_slice_size or QZONE_H5_DEFAULT_SLICE_SIZE)
    except (TypeError, ValueError):
        slice_size = QZONE_H5_DEFAULT_SLICE_SIZE
    return session, max(1, slice_size)


def extract_h5_video_vid(payload: Any) -> str:
    data = unwrap_payload(payload)
    found = _find_text_key(data, {"sVid", "svid"})
    return found or ""


def _find_text_key(value: Any, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in keys and item not in (None, ""):
                return str(item)
        for item in value.values():
            found = _find_text_key(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_text_key(item, keys)
            if found:
                return found
    return ""


def build_qzone_video_richval(*, uin: int | str, vid: str) -> str:
    vid = str(vid or "").strip()
    uin = str(uin or "").strip()
    safe = "-_.!~*'()"
    play_url = quote(f"http://cache.tv.qq.com/qqplayerout.swf?v={vid}&auto=0", safe=safe)
    detail_url = quote(f"http://user.qzone.qq.com/{uin}/qzvideo/{vid}", safe=safe)
    return "&".join(
        [
            f"playurl={play_url}",
            f"detailurl={detail_url}",
            "who=5",
            "rich_flag=4",
            f"vid={vid}",
        ]
    )


def build_qzone_video_publish_payload(
    *,
    uin: int | str,
    content: str,
    vid: str,
    sync_weibo: bool = False,
) -> dict[str, Any]:
    return {
        "syn_tweet_verson": "1",
        "paramstr": "1",
        "who": "1",
        "con": str(content or ""),
        "feedversion": "1",
        "ver": "1",
        "ugc_right": 1,
        "to_sign": 0,
        "hostuin": int(uin or 0),
        "code_version": "1",
        "richtype": "3",
        "subrichtype": "7",
        "richval": build_qzone_video_richval(uin=uin, vid=vid),
        "issyncweibo": int(bool(sync_weibo)),
        "format": "json",
        "qzreferrer": f"https://user.qzone.qq.com/{int(uin or 0)}",
    }
