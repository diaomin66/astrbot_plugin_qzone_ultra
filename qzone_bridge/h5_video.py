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
QZONE_H5_PIC_APPID = "pic_qzone"
QZONE_H5_VIDEO_TOKEN_TYPE = 4
QZONE_H5_VIDEO_TOKEN_APPID = 5
QZONE_H5_VIDEO_CONTROL_CMD = "FileUploadVideo"
QZONE_H5_PIC_CONTROL_CMD = "FileUpload"
QZONE_H5_VIDEO_CHECK_TYPE_SHA1 = 1
QZONE_H5_PIC_CHECK_TYPE_MD5_COMPAT = 0
QZONE_H5_DEFAULT_SLICE_SIZE = 256 * 1024
QZONE_PUBLIC_UGC_RIGHT = 1
QZONE_SELF_UGC_RIGHT = 64
QZONE_PUBLIC_WHO = "1"
# Current Qzone Web's mood video model uses type=3 and subType=7 for uploaded
# videos (see app/v8/models/mood/video/1.0 in the captured Qzone modules).
# Using the older 6 variant can create an appid=4 album/video side effect that
# is visible on phones but not editable as a normal public video mood.
QZONE_LOCAL_VIDEO_SUBRICHTYPE = "7"


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


@dataclass(frozen=True, slots=True)
class QzoneH5VideoCoverUploadResult:
    checksum: str
    uploaded_bytes: int
    session: str = ""
    slice_size: int = 0
    photo_id: str = ""
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


def md5_file(path: str | Path) -> str:
    digest = hashlib.md5()
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
    # Keep the uploaded video resource public-capable.  The follow-up shuoshuo
    # creation step is public too; if the upload/cover metadata is self-only,
    # emotion_cgi_update can flip the mood's ugc_right to 1 while the embedded
    # video remains access-denied (Qzone reports video_right=640).
    video_extend.setdefault("ugc_right", str(QZONE_PUBLIC_UGC_RIGHT))
    video_extend.setdefault("who", QZONE_PUBLIC_WHO)
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
                    "iNeedFeeds": 0,
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


def resolve_h5_cover_image_size(path: str | Path, width: int = 0, height: int = 0) -> tuple[int, int]:
    if int(width or 0) > 0 and int(height or 0) > 0:
        return int(width), int(height)
    try:
        from PIL import Image

        with Image.open(path) as image:
            return int(width or image.width or 0), int(height or image.height or 0)
    except Exception:
        return int(width or 0), int(height or 0)


def build_h5_video_cover_control_payload(
    *,
    uin: int | str,
    p_skey: str,
    checksum: str,
    file_size: int,
    vid: str,
    client_key: str,
    video_size: int = 0,
    duration_ms: int = 0,
    desc: str = "",
    cover_path: str | Path = "",
    width: int = 0,
    height: int = 0,
    upload_time: int | None = None,
    batch_id: int | None = None,
    is_original_video: int = 0,
    need_feeds: int = 0,
    extra_map_ext: dict[str, str] | None = None,
    extra_params: dict[str, str] | None = None,
    album_id: str = "",
    album_name: str = "",
    album_type_id: int | None = None,
) -> dict[str, Any]:
    upload_time = int(upload_time if upload_time is not None else time.time())
    batch_id = int(batch_id if batch_id is not None else upload_time)
    path_text = str(cover_path or "")
    params = {str(key): str(value) for key, value in dict(extra_params or {}).items()}
    params["vid"] = str(vid)
    if client_key:
        params.setdefault("clientkey", str(client_key))
    params.setdefault("raw_width", str(int(width or 0)))
    params.setdefault("raw_height", str(int(height or 0)))
    params.setdefault("raw_size", str(int(file_size or 0)))
    params.setdefault("show_geo", "0")
    params.setdefault("ugc_right", str(QZONE_PUBLIC_UGC_RIGHT))
    params.setdefault("who", QZONE_PUBLIC_WHO)
    album_id = str(album_id or "").strip()
    album_name = str(album_name or "")
    if album_id:
        params.setdefault("albumid", album_id)
        params.setdefault("album_id", album_id)
        params.setdefault("topicId", album_id)
        params.setdefault("priv", "1")
        params.setdefault("privacy", "1")
        params.setdefault("accessright", "1")
    external_map_ext = {str(key): str(value) for key, value in dict(extra_map_ext or {}).items()}
    # This cover upload is only a resource-binding step for direct video
    # shuoshuo publish.  If pic_qzone also creates a fake feed, Qzone can emit
    # a separate appid=4 "说说和日志相册" item with privacy=3/data-accessright=3.
    # That private album/video layer is what phones later show as not editable;
    # emotion_cgi_update only repairs the appid=311 mood wrapper.  Therefore
    # default to no fake feed and let publish_v6 create the single public mood.
    external_map_ext.setdefault("is_client_upload_cover", "1")
    if int(need_feeds or 0):
        external_map_ext.setdefault("is_pic_video_mix_feeds", "1")
    else:
        external_map_ext.pop("is_pic_video_mix_feeds", None)
    external_map_ext.setdefault("ugc_right", str(QZONE_PUBLIC_UGC_RIGHT))
    external_map_ext.setdefault("who", QZONE_PUBLIC_WHO)
    if album_id:
        external_map_ext.setdefault("albumid", album_id)
        external_map_ext.setdefault("album_id", album_id)
        external_map_ext.setdefault("topicId", album_id)
        external_map_ext.setdefault("priv", "1")
        external_map_ext.setdefault("privacy", "1")
        external_map_ext.setdefault("accessright", "1")
    if int(video_size or 0) > 0:
        external_map_ext.setdefault("mix_videoSize", str(int(video_size)))
    external_map_ext.setdefault("mix_isOriginalVideo", str(int(is_original_video or 0)))
    if int(duration_ms or 0) > 0:
        external_map_ext.setdefault("mix_time", str(int(duration_ms)))
    resolved_album_type_id = 7 if album_type_id is None else int(album_type_id)
    if album_id and album_type_id is None:
        resolved_album_type_id = 0
    return {
        "control_req": [
            {
                "uin": str(uin),
                "token": {
                    "type": QZONE_H5_VIDEO_TOKEN_TYPE,
                    "data": str(p_skey),
                    "appid": QZONE_H5_VIDEO_TOKEN_APPID,
                },
                "appid": QZONE_H5_PIC_APPID,
                "checksum": str(checksum),
                "check_type": QZONE_H5_PIC_CHECK_TYPE_MD5_COMPAT,
                "file_len": int(file_size),
                "env": {
                    "refer": "qzone",
                    "deviceInfo": "h5",
                },
                "model": 0,
                "biz_req": {
                    "sPicTitle": "",
                    "sPicDesc": str(desc or ""),
                    "sAlbumName": album_name if album_id else "",
                    "sAlbumID": album_id,
                    "iAlbumTypeID": resolved_album_type_id,
                    "iBitmap": 0,
                    "iUploadType": 2,
                    "iUpPicType": 0,
                    "iBatchID": batch_id,
                    "sPicPath": path_text,
                    "iPicWidth": int(width or 0),
                    "iPicHight": int(height or 0),
                    "iWaterType": 0,
                    "iDistinctUse": 0x37DD,
                    "iNeedFeeds": int(need_feeds or 0),
                    "iUploadTime": upload_time,
                    "mapExt": {"mobile_fakefeeds_clientkey": str(client_key or "")} if int(need_feeds or 0) else {},
                    "stExtendInfo": {"mapParams": params},
                    "stExternalMapExt": external_map_ext,
                    "mutliPicInfo": {
                        "iBatUploadNum": 1,
                        "iCurUpload": 0,
                        "iSuccNum": 0,
                        "iFailNum": 0,
                    },
                },
                "session": "",
                "asy_upload": 0,
                "cmd": QZONE_H5_PIC_CONTROL_CMD,
            }
        ]
    }


def h5_video_control_url(checksum: str) -> str:
    return f"{QZONE_H5_UPLOAD_ORIGIN}/webapp/json/sliceUpload/FileBatchControl/{checksum}"


def h5_video_cover_control_url(checksum: str) -> str:
    return h5_video_control_url(checksum)


def h5_video_slice_url() -> str:
    return f"{QZONE_H5_UPLOAD_ORIGIN}/webapp/json/sliceUpload/{QZONE_H5_VIDEO_CONTROL_CMD}"


def h5_video_cover_slice_url() -> str:
    return f"{QZONE_H5_UPLOAD_ORIGIN}/webapp/json/sliceUpload/{QZONE_H5_PIC_CONTROL_CMD}"


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


def encode_h5_video_cover_slice_multipart(
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
        ("appid", QZONE_H5_PIC_APPID),
    ]
    fields_after_blob = [
        ("session", str(session)),
        ("offset", str(int(offset))),
        ("checksum", ""),
        ("check_type", "0"),
        ("retry", "0"),
        ("seq", str(int(seq))),
        ("end", str(int(end))),
        ("cmd", QZONE_H5_PIC_CONTROL_CMD),
        ("slice_size", str(int(slice_size))),
        ("biz_req.iUploadType", "2"),
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


def extract_h5_video_cover_photo_id(payload: Any) -> str:
    data = unwrap_payload(payload)
    found = _find_text_key(
        data,
        {
            "photoid",
            "photoId",
            "sPhotoID",
            "lloc",
            "LLoc",
            "sloc",
            "SLoc",
            "picid",
            "picId",
        },
    )
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
    """Build the Web/H5 video richval used by taotao publish/update CGIs."""

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
    """Build the old Web/H5 video-shuoshuo publish payload.

    This endpoint is used only as the creation step.  Publish it as public at
    creation time: recent Qzone creates a mixed appid=311 mood + appid=4
    video/album object, and making the creation request self-visible can leave
    the embedded appid=4 video layer stuck in a private, phone-uneditable
    state even when a later emotion_cgi_update flips the mood wrapper's
    ugc_right to 1.  Callers still run emotion_cgi_update as an idempotent
    public repair step and must verify the final public video feed.
    """

    uin_int = int(uin or 0)
    return {
        "syn_tweet_verson": "1",
        "paramstr": "1",
        "who": "1",
        "con": str(content or ""),
        "feedversion": "1",
        "ver": "1",
        "ugc_right": QZONE_PUBLIC_UGC_RIGHT,
        "to_sign": 0,
        "hostuin": uin_int,
        "code_version": "1",
        "richtype": "3",
        "subrichtype": QZONE_LOCAL_VIDEO_SUBRICHTYPE,
        "richval": build_qzone_video_richval(uin=uin_int, vid=vid),
        "issyncweibo": int(bool(sync_weibo)),
        "format": "json",
        "qzreferrer": f"https://user.qzone.qq.com/{uin_int}",
    }


def build_qzone_video_visibility_update_payload(
    *,
    uin: int | str,
    fid: str,
    content: str = "",
    vid: str = "",
) -> dict[str, Any]:
    """Build the PC Web edit payload that changes a mood to public visibility.

    The privacy edit endpoint is ``emotion_cgi_update``.  Its successful
    response is not enough to prove the mood became public, so callers still
    verify the public feed/detail after this request.

    Keep this body intentionally aligned with onebot-qzone's ``ugc_right``
    implementation for normal shuoshuo privacy edits: it edits only the
    permission fields and leaves ``richtype`` / ``richval`` /
    ``subrichtype`` empty.  Re-sending the local-video ``richval`` here can
    make Qzone report ``ugc_right=1`` on the wrapper mood while the attached
    video resource stays access-denied (``video_right=640``).
    """

    uin_int = int(uin or 0)
    _ = str(vid or "").strip()
    fid = str(fid or "").strip()
    data: dict[str, Any] = {
        "syn_tweet_verson": "1",
        "tid": fid,
        "paramstr": "1",
        "pic_template": "",
        "richtype": "",
        "richval": "",
        "special_url": "",
        "subrichtype": "",
        "con": str(content or ""),
        "feedversion": "1",
        "ver": "1",
        "ugc_right": str(QZONE_PUBLIC_UGC_RIGHT),
        "to_sign": "0",
        "ugcright_id": fid,
        "hostuin": str(uin_int),
        "code_version": "1",
        "format": "fs",
        "qzreferrer": f"https://user.qzone.qq.com/{uin_int}/main",
    }
    return data
