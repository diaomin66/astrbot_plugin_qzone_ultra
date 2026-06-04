"""Tencent upload SDK protocol primitives for native Qzone video research."""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict, dataclass, field
import hashlib
import os
from pathlib import Path
import socket
import time
from typing import Any, Callable

from .jce import (
    JceField,
    as_bytes,
    as_int,
    as_map,
    as_nodes,
    as_str,
    decode_struct,
    encode_struct,
    field_value,
    jce_struct,
)


QZONE_VIDEO_UPLOAD_APPID = "video_qzone"
QZONE_VIDEO_UPLOAD_HOST = "video.upqzfile.com"
QZONE_VIDEO_UPLOAD_BACKUP_HOST = "video.upqzfilebk.com"
QZONE_VIDEO_UPLOAD_PORT = 80
QZONE_PIC_UPLOAD_APPID = "pic_qzone"
QZONE_PIC_UPLOAD_HOST = "pic.upqzfile.com"
QZONE_PIC_UPLOAD_BACKUP_HOST = "pic.upqzfilebk.com"
QZONE_PIC_UPLOAD_PORT = 80
QZONE_VIDEO_FILE_TYPE = "Video"
QZONE_PIC_FILE_TYPE = "Photo"
QZONE_VIDEO_BUSINESS_TYPE = "QZoneVideo"
QZONE_PIC_BUSINESS_TYPE = "QZonePhoto"
QZONE_VIDEO_CONNECT_TYPE = "Epoll"
QZONE_RECORD_VIDEO_BUSINESS_TYPE = 1
QZONE_PUBLISH_MOOD_UNI_KEY = "publishmood"
QZONE_PUBLISH_MOOD_TYPE = "NS_MOBILE_OPERATION.operation_publishmood_req"
QZONE_VIDEO_UPLOAD_FINISH_UNI_KEY = "rptVSUploadFinish"
QZONE_VIDEO_UPLOAD_FINISH_TYPE = "NS_MOBILE_EXTRA.mobile_video_shuoshuo_upload_finish_req"
QZONE_UNI_INT64_TYPE = "int64"

TENCENT_UPLOAD_CMD_CONTROL = 1
TENCENT_UPLOAD_CMD_FILE = 2
TENCENT_UPLOAD_CHECK_TYPE_MD5 = 0
TENCENT_UPLOAD_CHECK_TYPE_SHA1 = 1
TENCENT_UPLOAD_DEFAULT_SLICE_SIZE = 256 * 1024
TENCENT_UPLOAD_TOKEN_ENC_TYPE = 2

PDU_START_MARKER = 0x04
PDU_END_MARKER = 0x05
PDU_HEADER_LENGTH = 0x17
PDU_TOTAL_OVERHEAD = PDU_HEADER_LENGTH + 2

PDU_OFFSET_VERSION = 0
PDU_OFFSET_CMD = 1
PDU_OFFSET_CHECKSUM = 5
PDU_OFFSET_SEQ = 7
PDU_OFFSET_KEY = 0x0B
PDU_OFFSET_RESPONSE_FLAG = 0x0F
PDU_OFFSET_RESPONSE_INFO = 0x10
PDU_OFFSET_RESERVED = 0x12
PDU_OFFSET_LENGTH = 0x13


class TencentUploadPduError(ValueError):
    """Raised when a Tencent upload PDU frame is malformed."""


class TencentUploadProtocolError(RuntimeError):
    """Raised when Tencent upload JCE responses are malformed."""


class QzoneNativeVideoCredentialError(ValueError):
    """Raised when daemon video upload lacks QQ upload login material."""


class QzoneTencentVideoUploadError(RuntimeError):
    """Raised when the Tencent upload service rejects a video upload."""


@dataclass(frozen=True, slots=True)
class TencentUploadPduHeader:
    cmd: int
    seq: int
    length: int

    def to_bytes(self) -> bytes:
        if self.length < PDU_TOTAL_OVERHEAD:
            raise TencentUploadPduError("PDU length is smaller than Tencent upload framing overhead")
        header = bytearray(PDU_HEADER_LENGTH)
        header[PDU_OFFSET_CMD : PDU_OFFSET_CMD + 4] = _u32be(self.cmd)
        if self.seq:
            header[PDU_OFFSET_SEQ : PDU_OFFSET_SEQ + 4] = _u32be(self.seq)
        header[PDU_OFFSET_LENGTH : PDU_OFFSET_LENGTH + 4] = _u32be(self.length)
        return bytes(header)

    @classmethod
    def from_bytes(cls, header: bytes) -> TencentUploadPduHeader:
        if len(header) != PDU_HEADER_LENGTH:
            raise TencentUploadPduError(f"PDU header must be {PDU_HEADER_LENGTH} bytes")
        return cls(
            cmd=_read_u32be(header, PDU_OFFSET_CMD),
            seq=_read_u32be(header, PDU_OFFSET_SEQ),
            length=_read_u32be(header, PDU_OFFSET_LENGTH),
        )


@dataclass(frozen=True, slots=True)
class TencentUploadPduFrame:
    header: TencentUploadPduHeader
    payload: bytes

    def to_bytes(self) -> bytes:
        expected_length = len(self.payload) + PDU_TOTAL_OVERHEAD
        if self.header.length != expected_length:
            raise TencentUploadPduError("PDU header length does not match payload length")
        return bytes([PDU_START_MARKER]) + self.header.to_bytes() + self.payload + bytes([PDU_END_MARKER])


@dataclass(frozen=True, slots=True)
class NativeVideoDaemonRequirement:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class UploadVideoInfoReq:
    title: str = ""
    desc: str = ""
    flag: int = 0
    upload_time: int = 0
    business_type: int = 0
    business_data: bytes = b""
    play_time: int = 0
    cover_url: str = ""
    is_new: int = 1
    is_original_video: int = 0
    is_format_f20: int = 0
    extend_info: dict[str, str] = field(default_factory=dict)
    height: int = 0
    width: int = 0


@dataclass(frozen=True, slots=True)
class UploadVideoInfoRsp:
    vid: str = ""
    business_type: int = 0
    business_data: bytes = b""


@dataclass(frozen=True, slots=True)
class MultiPicInfo:
    batch_upload_num: int = 0
    current_upload: int = 0
    success_num: int = 0
    fail_num: int = 0


@dataclass(frozen=True, slots=True)
class PicExtendInfo:
    effect: int = 0
    quan_info: tuple[Any, ...] = field(default_factory=tuple)
    exif: dict[str, str] | None = None
    user_define_source: str = ""
    params: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class UploadPicInfoReq:
    title: str = ""
    desc: str = ""
    album_name: str = ""
    album_id: str = ""
    album_type_id: int = 7
    bitmap: int = 0
    upload_type: int = 0
    up_pic_type: int = 0
    batch_id: int = 0
    multi_pic_info: MultiPicInfo | None = None
    extend_info: PicExtendInfo | None = None
    pic_path: str = ""
    width: int = 0
    height: int = 0
    water_type: int = 0
    exif_camera_maker: str = ""
    exif_camera_model: str = ""
    exif_time: str = ""
    exif_latitude_ref: str = ""
    exif_latitude: str = ""
    exif_longitude_ref: str = ""
    exif_longitude: str = ""
    need_feeds: int = 0
    upload_time: int = 0
    map_ext: dict[str, str] | None = None
    distinct_use: int = 0x37DD
    other_params: str = ""
    business_type: int = 0
    business_data: bytes | None = None
    external_map_ext: dict[str, str] | None = None
    external_data: dict[str, bytes] | None = None
    resource_type: int = 0


@dataclass(frozen=True, slots=True)
class UploadPicInfoRsp:
    small_url: str = ""
    big_url: str = ""
    album_id: str = ""
    photo_id: str = ""
    sloc: str = ""
    width: int = 0
    height: int = 0
    original_url: str = ""
    original_width: int = 0
    original_height: int = 0
    original_photo_id: str = ""
    pic_type: int = 0
    adapt_url_160: str = ""
    adapt_url_200: str = ""
    adapt_url_400: str = ""
    adapt_url_640: str = ""
    adapt_url_1000: str = ""
    business_type: int = 0
    business_data_rsp: bytes = b""
    real_lloc: str = ""
    photo_md5: str = ""


@dataclass(frozen=True, slots=True)
class AuthToken:
    type: int = TENCENT_UPLOAD_TOKEN_ENC_TYPE
    data: bytes = b""
    ext_key: bytes = b""
    appid: int = 0
    wt_appid: int = 0


@dataclass(frozen=True, slots=True)
class QzoneVideoUploadCredentials:
    login_data: bytes
    login_key: bytes = b""
    token_type: int = TENCENT_UPLOAD_TOKEN_ENC_TYPE
    token_appid: int = 0
    token_wt_appid: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "login_data_length": len(self.login_data),
            "login_key_length": len(self.login_key),
            "token_type": self.token_type,
            "token_appid": self.token_appid,
            "token_wt_appid": self.token_wt_appid,
        }


@dataclass(frozen=True, slots=True)
class StEnvironment:
    qua: str = ""
    device: str = ""
    net: int = 0
    operators: str = ""
    client_ip: int = 0
    refer: str = "mqq"
    entrance: int = 0
    source: int = 0
    device_info: str = ""


@dataclass(frozen=True, slots=True)
class StResult:
    ret: int = 0
    flag: int = 0
    msg: str = ""


@dataclass(frozen=True, slots=True)
class StOffset:
    begin: int = 0
    end: int = 0


@dataclass(frozen=True, slots=True)
class FileControlReq:
    uin: str
    token: AuthToken
    appid: str = QZONE_VIDEO_UPLOAD_APPID
    checksum: str = ""
    check_type: int = TENCENT_UPLOAD_CHECK_TYPE_SHA1
    file_len: int = 0
    env: StEnvironment = field(default_factory=StEnvironment)
    model: int = 0
    biz_req: bytes = b""
    session: str = ""
    need_ip_redirect: bool = False
    asy_upload: int = 1
    dump_req: dict[int, Any] | None = None
    slice_size: int = 0
    extend_info: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FileControlRsp:
    result: StResult = field(default_factory=StResult)
    session: str = ""
    offset: int = 0
    slice_size: int = 0
    biz_rsp: bytes = b""
    offset_list: tuple[StOffset, ...] = field(default_factory=tuple)
    redirect_ip: str = ""
    thread_num: int = 1
    dump_rsp: dict[int, Any] | None = None


@dataclass(frozen=True, slots=True)
class FileBatchControlReq:
    control_req: dict[str, FileControlReq]


@dataclass(frozen=True, slots=True)
class FileBatchControlRsp:
    control_rsp: dict[str, FileControlRsp]


@dataclass(frozen=True, slots=True)
class FileUploadReq:
    uin: str
    appid: str
    session: str
    offset: int
    data: bytes
    checksum: str = ""
    check_type: int = TENCENT_UPLOAD_CHECK_TYPE_SHA1
    send_time: int = 0
    data_type: int = 0
    extend_info: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FileUploadRsp:
    result: StResult = field(default_factory=StResult)
    session: str = ""
    offset: int = 0
    biz_rsp: bytes = b""
    receive_time: int = 0
    response_time: int = 0
    dump_rsp: dict[int, Any] | None = None


@dataclass(frozen=True, slots=True)
class QzoneTencentVideoUploadResult:
    vid: str
    business_type: int
    business_data: bytes
    uploaded_bytes: int
    session: str
    upload_time: int = 0
    client_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "vid": self.vid,
            "business_type": self.business_type,
            "business_data_length": len(self.business_data),
            "uploaded_bytes": self.uploaded_bytes,
            "session": self.session,
            "upload_time": self.upload_time,
            "client_key": self.client_key,
        }


@dataclass(frozen=True, slots=True)
class QzoneTencentPicUploadResult:
    response: UploadPicInfoRsp
    uploaded_bytes: int
    session: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "photo_id": self.response.photo_id,
            "album_id": self.response.album_id,
            "sloc": self.response.sloc,
            "real_lloc": self.response.real_lloc,
            "business_type": self.response.business_type,
            "business_data_rsp_length": len(self.response.business_data_rsp),
            "uploaded_bytes": self.uploaded_bytes,
            "session": self.session,
        }


@dataclass(frozen=True, slots=True)
class QzoneVideoUploadProtocolSpec:
    appid: str = QZONE_VIDEO_UPLOAD_APPID
    hosts: tuple[str, str] = (QZONE_VIDEO_UPLOAD_HOST, QZONE_VIDEO_UPLOAD_BACKUP_HOST)
    port: int = QZONE_VIDEO_UPLOAD_PORT
    file_type: str = QZONE_VIDEO_FILE_TYPE
    business_type: str = QZONE_VIDEO_BUSINESS_TYPE
    connect_type: str = QZONE_VIDEO_CONNECT_TYPE
    pdu_header_length: int = PDU_HEADER_LENGTH
    pdu_total_overhead: int = PDU_TOTAL_OVERHEAD
    control_cmd: int = TENCENT_UPLOAD_CMD_CONTROL
    file_cmd: int = TENCENT_UPLOAD_CMD_FILE
    request_sequence: tuple[dict[str, str], ...] = field(default_factory=tuple)
    requirements: tuple[NativeVideoDaemonRequirement, ...] = field(default_factory=tuple)
    daemon_ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["hosts"] = list(self.hosts)
        data["request_sequence"] = [dict(item) for item in self.request_sequence]
        data["requirements"] = [item.to_dict() for item in self.requirements]
        return data


def encode_upload_pdu(cmd: int, seq: int, jce_payload: bytes) -> bytes:
    payload = bytes(jce_payload or b"")
    header = TencentUploadPduHeader(cmd=int(cmd), seq=int(seq), length=len(payload) + PDU_TOTAL_OVERHEAD)
    return TencentUploadPduFrame(header=header, payload=payload).to_bytes()


def decode_upload_pdu(frame: bytes) -> TencentUploadPduFrame:
    packet = bytes(frame or b"")
    if len(packet) < PDU_TOTAL_OVERHEAD:
        raise TencentUploadPduError("PDU frame is too short")
    if packet[0] != PDU_START_MARKER:
        raise TencentUploadPduError("PDU frame does not start with 0x04")
    if packet[-1] != PDU_END_MARKER:
        raise TencentUploadPduError("PDU frame does not end with 0x05")
    header = TencentUploadPduHeader.from_bytes(packet[1 : 1 + PDU_HEADER_LENGTH])
    if header.length != len(packet):
        raise TencentUploadPduError("PDU declared length does not match frame length")
    payload_length = header.length - PDU_TOTAL_OVERHEAD
    payload = packet[1 + PDU_HEADER_LENGTH : 1 + PDU_HEADER_LENGTH + payload_length]
    return TencentUploadPduFrame(header=header, payload=payload)


def decode_upload_pdu_size(frame_prefix: bytes) -> int:
    packet = bytes(frame_prefix or b"")
    required = 1 + PDU_HEADER_LENGTH
    if len(packet) < required:
        raise TencentUploadPduError("PDU header prefix is incomplete")
    if packet[0] != PDU_START_MARKER:
        raise TencentUploadPduError("PDU frame does not start with 0x04")
    header = TencentUploadPduHeader.from_bytes(packet[1:required])
    return header.length


def encode_upload_video_info_req(request: UploadVideoInfoReq) -> bytes:
    return encode_struct(
        [
            JceField(0, request.title),
            JceField(1, request.desc),
            JceField(2, request.flag),
            JceField(3, request.upload_time),
            JceField(4, request.business_type),
            JceField(5, request.business_data),
            JceField(6, request.play_time),
            JceField(7, request.cover_url),
            JceField(8, request.is_new),
            JceField(9, request.is_original_video),
            JceField(10, request.is_format_f20),
            JceField(11, dict(request.extend_info or {})),
            JceField(12, request.height),
            JceField(13, request.width),
        ]
    )


def encode_multi_pic_info(info: MultiPicInfo) -> Any:
    return jce_struct(
        [
            JceField(0, info.batch_upload_num),
            JceField(1, info.current_upload),
            JceField(2, info.success_num),
            JceField(3, info.fail_num),
        ]
    )


def encode_pic_extend_info(info: PicExtendInfo) -> Any:
    fields = [JceField(0, info.effect)]
    if info.quan_info:
        fields.append(JceField(1, list(info.quan_info)))
    if info.exif is not None:
        fields.append(JceField(2, dict(info.exif)))
    if info.user_define_source is not None:
        fields.append(JceField(3, info.user_define_source))
    if info.params is not None:
        fields.append(JceField(4, dict(info.params)))
    return jce_struct(fields)


def encode_upload_pic_info_req(request: UploadPicInfoReq) -> bytes:
    fields = [
        JceField(0, request.title),
        JceField(1, request.desc),
        JceField(2, request.album_name),
        JceField(3, request.album_id),
        JceField(4, request.album_type_id),
        JceField(5, request.bitmap),
        JceField(6, request.upload_type),
        JceField(7, request.up_pic_type),
        JceField(8, request.batch_id),
    ]
    if request.multi_pic_info is not None:
        fields.append(JceField(9, encode_multi_pic_info(request.multi_pic_info)))
    if request.extend_info is not None:
        fields.append(JceField(10, encode_pic_extend_info(request.extend_info)))
    fields.extend(
        [
            JceField(11, request.pic_path),
            JceField(12, request.width),
            JceField(13, request.height),
            JceField(14, request.water_type),
            JceField(15, request.exif_camera_maker),
            JceField(16, request.exif_camera_model),
            JceField(17, request.exif_time),
            JceField(18, request.exif_latitude_ref),
            JceField(19, request.exif_latitude),
            JceField(20, request.exif_longitude_ref),
            JceField(21, request.exif_longitude),
            JceField(22, request.need_feeds),
            JceField(23, request.upload_time),
        ]
    )
    if request.map_ext is not None:
        fields.append(JceField(24, dict(request.map_ext)))
    fields.append(JceField(25, request.distinct_use))
    if request.other_params is not None:
        fields.append(JceField(28, request.other_params))
    fields.append(JceField(29, request.business_type))
    if request.business_data is not None:
        fields.append(JceField(30, bytes(request.business_data)))
    if request.external_map_ext is not None:
        fields.append(JceField(31, dict(request.external_map_ext)))
    if request.external_data is not None:
        fields.append(JceField(32, dict(request.external_data)))
    fields.append(JceField(33, request.resource_type))
    return encode_struct(fields)


def decode_upload_video_info_rsp(payload: bytes) -> UploadVideoInfoRsp:
    nodes = decode_struct(payload)
    return UploadVideoInfoRsp(
        vid=as_str(field_value(nodes, 0), ""),
        business_type=as_int(field_value(nodes, 1), 0),
        business_data=as_bytes(field_value(nodes, 2), b""),
    )


def decode_upload_pic_info_rsp(payload: bytes) -> UploadPicInfoRsp:
    nodes = decode_struct(payload)
    return UploadPicInfoRsp(
        small_url=as_str(field_value(nodes, 0), ""),
        big_url=as_str(field_value(nodes, 1), ""),
        album_id=as_str(field_value(nodes, 2), ""),
        photo_id=as_str(field_value(nodes, 3), ""),
        sloc=as_str(field_value(nodes, 4), ""),
        width=as_int(field_value(nodes, 5), 0),
        height=as_int(field_value(nodes, 6), 0),
        original_url=as_str(field_value(nodes, 7), ""),
        original_width=as_int(field_value(nodes, 8), 0),
        original_height=as_int(field_value(nodes, 9), 0),
        original_photo_id=as_str(field_value(nodes, 10), ""),
        pic_type=as_int(field_value(nodes, 11), 0),
        adapt_url_160=as_str(field_value(nodes, 12), ""),
        adapt_url_200=as_str(field_value(nodes, 13), ""),
        adapt_url_400=as_str(field_value(nodes, 14), ""),
        adapt_url_640=as_str(field_value(nodes, 15), ""),
        adapt_url_1000=as_str(field_value(nodes, 16), ""),
        business_type=as_int(field_value(nodes, 18), 0),
        business_data_rsp=as_bytes(field_value(nodes, 19), b""),
        real_lloc=as_str(field_value(nodes, 20), ""),
        photo_md5=as_str(field_value(nodes, 21), ""),
    )


def encode_old_uni_attribute(entries: dict[str, tuple[str, Any]]) -> bytes:
    """Encode the old WUP UniAttribute map used by Qzone mobile requests."""

    wrapped: dict[str, dict[str, bytes]] = {}
    for key, (type_name, value) in entries.items():
        key_text = str(key or "")
        type_text = str(type_name or "")
        if not key_text or not type_text:
            raise TencentUploadProtocolError("UniAttribute entries require non-empty key and type name")
        wrapped[key_text] = {type_text: encode_struct([JceField(0, value)])}
    return encode_struct([JceField(0, wrapped)])


def operation_publish_mood_req_struct(
    *,
    uin: int | str,
    content: str,
    sync_weibo: bool = False,
    weibourl: str = "",
    media_type: int = 0,
    media_bit_type: int = 0,
    busi_param: dict[Any, Any] | None = None,
    client_key: str = "",
    publish_time: int = 0,
    media_sub_type: int = 0,
    srcid: str = "",
    modify_flag: int = 0,
    extend_info: dict[Any, Any] | None = None,
    stored_extend_info: dict[Any, Any] | None = None,
    proto_extend_info: dict[Any, Any] | None = None,
    source_subtype: int = 0,
    source_termtype: int = 4,
    source_apptype: int = 1,
    ugc_right: int = 1,
    shoot_time: int = 0,
) -> Any:
    fields = [
        JceField(0, int(uin or 0)),
        JceField(1, str(content or "")),
        JceField(2, True),
        JceField(3, bool(sync_weibo)),
        JceField(4, str(weibourl or "")),
        JceField(5, int(media_type or 0)),
        JceField(8, _source_struct(source_subtype, source_termtype, source_apptype)),
        JceField(9, int(media_bit_type or 0)),
    ]
    if busi_param is not None:
        fields.append(JceField(10, dict(busi_param)))
    fields.extend(
        [
            JceField(11, str(client_key or "")),
            JceField(12, ""),
            JceField(13, _ugc_right_info_struct(ugc_right)),
            JceField(14, _shoot_info_struct(shoot_time)),
            JceField(15, int(publish_time or 0)),
            JceField(16, int(media_sub_type or 0)),
            JceField(17, str(srcid or "")),
            JceField(18, int(modify_flag or 0)),
        ]
    )
    if extend_info is not None:
        fields.append(JceField(19, dict(extend_info)))
    fields.extend(
        [
            JceField(20, ""),
            JceField(21, ""),
            JceField(22, 0),
            JceField(23, ""),
            JceField(25, 0),
            JceField(26, 0),
            JceField(27, 0),
        ]
    )
    if stored_extend_info is not None:
        fields.append(JceField(28, dict(stored_extend_info)))
    if proto_extend_info is not None:
        fields.append(JceField(29, dict(proto_extend_info)))
    return jce_struct(fields)


def encode_record_video_publish_business_data(
    *,
    uin: int | str,
    content: str,
    video_size: int = 0,
    sync_weibo: bool = False,
    client_key: str = "",
    publish_time: int = 0,
    media_type: int = 1,
    media_bit_type: int = 1,
    media_sub_type: int = 0,
    is_original_video: int = 0,
    is_format_f20: int = 0,
    shoot_params: dict[Any, Any] | None = None,
    stored_extend_info: dict[Any, Any] | None = None,
    proto_extend_info: dict[Any, Any] | None = None,
) -> bytes:
    extend_info = {str(key): value for key, value in dict(shoot_params or {}).items()}
    extend_info.setdefault("has_video", "1")
    extend_info.setdefault("iIsOriginalVideo", str(int(is_original_video or 0)))
    extend_info.setdefault("iIsFormatF20", str(int(is_format_f20 or 0)))
    if int(video_size or 0) > 0:
        extend_info.setdefault("videoSize", str(int(video_size)))
    publish_req = operation_publish_mood_req_struct(
        uin=uin,
        content=content,
        sync_weibo=sync_weibo,
        client_key=client_key,
        publish_time=publish_time,
        media_type=media_type,
        media_bit_type=media_bit_type,
        media_sub_type=media_sub_type,
        extend_info=extend_info,
        stored_extend_info=stored_extend_info,
        proto_extend_info=proto_extend_info,
    )
    return encode_old_uni_attribute(
        {
            "hostuin": (QZONE_UNI_INT64_TYPE, int(uin or 0)),
            QZONE_PUBLISH_MOOD_UNI_KEY: (QZONE_PUBLISH_MOOD_TYPE, publish_req),
        }
    )


def encode_video_shuoshuo_upload_finish_uni_attribute(
    *,
    uin: int | str,
    size: int,
    time_length: int,
) -> bytes:
    finish_req = jce_struct([JceField(0, int(size or 0)), JceField(1, int(time_length or 0))])
    return encode_old_uni_attribute(
        {
            "hostuin": (QZONE_UNI_INT64_TYPE, int(uin or 0)),
            QZONE_VIDEO_UPLOAD_FINISH_UNI_KEY: (QZONE_VIDEO_UPLOAD_FINISH_TYPE, finish_req),
        }
    )


def encode_auth_token(token: AuthToken) -> bytes:
    return encode_struct(
        [
            JceField(0, token.type),
            JceField(1, token.data),
            JceField(2, token.ext_key),
            JceField(3, token.appid),
            JceField(4, token.wt_appid),
        ]
    )


def auth_token_struct(token: AuthToken) -> Any:
    return jce_struct(
        [
            JceField(0, token.type),
            JceField(1, token.data),
            JceField(2, token.ext_key),
            JceField(3, token.appid),
            JceField(4, token.wt_appid),
        ]
    )


def st_environment_struct(env: StEnvironment) -> Any:
    return jce_struct(
        [
            JceField(1, env.qua),
            JceField(2, env.device),
            JceField(3, env.net),
            JceField(4, env.operators),
            JceField(5, env.client_ip),
            JceField(6, env.refer),
            JceField(7, env.entrance),
            JceField(8, env.source),
            JceField(9, env.device_info),
        ]
    )


def encode_file_control_req(request: FileControlReq) -> bytes:
    return encode_struct(_file_control_req_fields(request))


def encode_file_batch_control_req(request: FileBatchControlReq) -> bytes:
    return encode_struct(
        [
            JceField(
                0,
                {
                    key: jce_struct(_file_control_req_fields(item))
                    for key, item in request.control_req.items()
                },
            )
        ]
    )


def decode_file_batch_control_rsp(payload: bytes) -> FileBatchControlRsp:
    nodes = decode_struct(payload)
    raw_map = as_map(field_value(nodes, 0, {}))
    responses: dict[str, FileControlRsp] = {}
    for key, value in raw_map.items():
        responses[str(key)] = decode_file_control_rsp_nodes(as_nodes(value))
    return FileBatchControlRsp(control_rsp=responses)


def decode_file_control_rsp(payload: bytes) -> FileControlRsp:
    return decode_file_control_rsp_nodes(decode_struct(payload))


def decode_file_control_rsp_nodes(nodes: list[Any]) -> FileControlRsp:
    return FileControlRsp(
        result=decode_st_result_nodes(as_nodes(field_value(nodes, 1))),
        session=as_str(field_value(nodes, 2), ""),
        offset=as_int(field_value(nodes, 3), 0),
        slice_size=as_int(field_value(nodes, 4), 0),
        biz_rsp=as_bytes(field_value(nodes, 5), b""),
        offset_list=tuple(decode_st_offset_nodes(as_nodes(item)) for item in (field_value(nodes, 6, []) or [])),
        redirect_ip=as_str(field_value(nodes, 7), ""),
        thread_num=as_int(field_value(nodes, 8), 1),
        dump_rsp=field_value(nodes, 9),
    )


def encode_file_upload_req(request: FileUploadReq) -> bytes:
    fields = [
        JceField(0, request.uin),
        JceField(1, request.appid),
        JceField(2, request.session),
        JceField(3, request.offset),
        JceField(4, request.data),
        JceField(5, request.checksum),
        JceField(6, request.check_type),
        JceField(7, request.send_time),
        JceField(8, request.data_type),
    ]
    if request.extend_info:
        fields.append(JceField(9, dict(request.extend_info)))
    return encode_struct(fields)


def decode_file_upload_rsp(payload: bytes) -> FileUploadRsp:
    nodes = decode_struct(payload)
    return FileUploadRsp(
        result=decode_st_result_nodes(as_nodes(field_value(nodes, 1))),
        session=as_str(field_value(nodes, 2), ""),
        offset=as_int(field_value(nodes, 3), 0),
        biz_rsp=as_bytes(field_value(nodes, 4), b""),
        receive_time=as_int(field_value(nodes, 5), 0),
        response_time=as_int(field_value(nodes, 6), 0),
        dump_rsp=field_value(nodes, 7),
    )


def decode_st_result_nodes(nodes: list[Any]) -> StResult:
    return StResult(
        ret=as_int(field_value(nodes, 1), 0),
        flag=as_int(field_value(nodes, 2), 0),
        msg=as_str(field_value(nodes, 3), ""),
    )


def decode_st_offset_nodes(nodes: list[Any]) -> StOffset:
    return StOffset(begin=as_int(field_value(nodes, 1), 0), end=as_int(field_value(nodes, 2), 0))


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


def qzone_video_upload_credentials_configured(env: dict[str, str] | None = None) -> bool:
    env_map = env if env is not None else os.environ
    return bool(_env_text(env_map, "QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64", "QZONE_UPLOAD_LOGIN_DATA_B64"))


def qzone_video_upload_credentials_from_env(env: dict[str, str] | None = None) -> QzoneVideoUploadCredentials:
    env_map = env if env is not None else os.environ
    login_data = _env_base64(env_map, "QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64", "QZONE_UPLOAD_LOGIN_DATA_B64")
    if not login_data:
        raise QzoneNativeVideoCredentialError(
            "daemon 原生视频上传缺少 QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64，无法生成 Tencent upload AuthToken"
        )
    return QzoneVideoUploadCredentials(
        login_data=login_data,
        login_key=_env_base64(env_map, "QZONE_VIDEO_UPLOAD_LOGIN_KEY_B64", "QZONE_UPLOAD_LOGIN_KEY_B64"),
        token_type=_env_int(env_map, TENCENT_UPLOAD_TOKEN_ENC_TYPE, "QZONE_VIDEO_UPLOAD_TOKEN_TYPE"),
        token_appid=_env_int(env_map, 0, "QZONE_VIDEO_UPLOAD_TOKEN_APPID"),
        token_wt_appid=_env_int(env_map, 0, "QZONE_VIDEO_UPLOAD_TOKEN_WT_APPID"),
    )


def qzone_video_upload_credentials_from_base64(
    *,
    login_data_b64: str,
    login_key_b64: str = "",
    token_type: int = TENCENT_UPLOAD_TOKEN_ENC_TYPE,
    token_appid: int = 0,
    token_wt_appid: int = 0,
) -> QzoneVideoUploadCredentials:
    login_data = _decode_base64_text(login_data_b64, "login_data_b64")
    if not login_data:
        raise QzoneNativeVideoCredentialError("daemon 原生视频上传缺少 QQ upload 登录材料")
    return QzoneVideoUploadCredentials(
        login_data=login_data,
        login_key=_decode_base64_text(login_key_b64, "login_key_b64") if login_key_b64 else b"",
        token_type=int(token_type or TENCENT_UPLOAD_TOKEN_ENC_TYPE),
        token_appid=int(token_appid or 0),
        token_wt_appid=int(token_wt_appid or 0),
    )


class QzoneTencentVideoUploader:
    """Synchronous Tencent upload SDK client for daemon-side video experiments.

    This implements the socket/PDU/JCE upload layer. For record-video shuoshuo
    it can embed the mobile publishmood UniAttribute in UploadVideoInfoReq.
    """

    def __init__(
        self,
        *,
        uin: int | str,
        login_data: bytes,
        login_key: bytes = b"",
        token_type: int = TENCENT_UPLOAD_TOKEN_ENC_TYPE,
        token_appid: int = 0,
        token_wt_appid: int = 0,
        host: str = QZONE_VIDEO_UPLOAD_HOST,
        port: int = QZONE_VIDEO_UPLOAD_PORT,
        timeout: float = 30.0,
        socket_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not bytes(login_data or b""):
            raise QzoneNativeVideoCredentialError(
                "daemon 原生视频上传缺少 vLoginData，当前 PC Cookie 登录态无法直接生成 Tencent upload AuthToken"
            )
        self.uin = str(uin or "")
        self.token = AuthToken(
            type=int(token_type),
            data=bytes(login_data or b""),
            ext_key=bytes(login_key or b""),
            appid=int(token_appid or 0),
            wt_appid=int(token_wt_appid or 0),
        )
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.socket_factory = socket_factory or socket.create_connection
        self._seq = 1

    def upload_video(
        self,
        video_path: str | Path,
        *,
        title: str = "",
        desc: str = "",
        play_time: int = 0,
        cover_url: str = "",
        business_type: int = 0,
        business_data: bytes = b"",
        extend_info: dict[str, str] | None = None,
        width: int = 0,
        height: int = 0,
        publish_content: str | None = None,
        sync_weibo: bool = False,
        client_key: str = "",
        publish_time: int = 0,
        upload_time: int = 0,
        is_original_video: int = 0,
        is_format_f20: int = 0,
        media_type: int = 1,
        media_bit_type: int = 1,
        media_sub_type: int = 0,
        shoot_params: dict[Any, Any] | None = None,
        stored_extend_info: dict[Any, Any] | None = None,
        proto_extend_info: dict[Any, Any] | None = None,
    ) -> QzoneTencentVideoUploadResult:
        path = Path(video_path)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        file_size = path.stat().st_size
        upload_time = int(upload_time or time.time())
        publish_time = int(publish_time or upload_time)
        if not business_data and publish_content is not None:
            business_data = encode_record_video_publish_business_data(
                uin=self.uin,
                content=publish_content,
                video_size=file_size,
                sync_weibo=sync_weibo,
                client_key=client_key,
                publish_time=publish_time,
                media_type=media_type,
                media_bit_type=media_bit_type,
                media_sub_type=media_sub_type,
                is_original_video=is_original_video,
                is_format_f20=is_format_f20,
                shoot_params=shoot_params,
                stored_extend_info=stored_extend_info,
                proto_extend_info=proto_extend_info,
            )
            business_type = int(business_type or QZONE_RECORD_VIDEO_BUSINESS_TYPE)
        upload_extend_info = {str(key): str(value) for key, value in dict(extend_info or {}).items()}
        upload_extend_info.setdefault("video_type", "3")
        upload_extend_info.setdefault("qz_video_format", _qzone_video_format(path))
        if client_key:
            upload_extend_info.setdefault("clientkey", str(client_key))
        info_req = UploadVideoInfoReq(
            title=title or path.name,
            desc=desc,
            upload_time=upload_time,
            business_type=business_type,
            business_data=business_data,
            play_time=play_time,
            cover_url=cover_url,
            is_original_video=is_original_video,
            is_format_f20=is_format_f20,
            extend_info=upload_extend_info,
            width=width,
            height=height,
        )
        control_req = FileControlReq(
            uin=self.uin,
            token=self.token,
            checksum=sha1_file(path),
            file_len=file_size,
            biz_req=encode_upload_video_info_req(info_req),
        )
        with self._connect() as sock:
            control_rsp = self._send_control(sock, control_req)
            self._raise_on_result(control_rsp.result, "视频控制包被拒绝")
            if control_rsp.biz_rsp:
                video_rsp = decode_upload_video_info_rsp(control_rsp.biz_rsp)
                if video_rsp.vid:
                    return QzoneTencentVideoUploadResult(
                        vid=video_rsp.vid,
                        business_type=video_rsp.business_type,
                        business_data=video_rsp.business_data,
                        uploaded_bytes=control_rsp.offset or file_size,
                        session=control_rsp.session,
                        upload_time=upload_time,
                        client_key=client_key,
                    )
            return self._upload_video_slices(
                sock,
                path,
                file_size,
                control_rsp,
                upload_time=upload_time,
                client_key=client_key,
            )

    def upload_video_cover(
        self,
        cover_path: str | Path,
        *,
        vid: str,
        video_path: str | Path | None = None,
        client_key: str = "",
        video_size: int = 0,
        duration_ms: int = 0,
        is_original_video: int = 0,
        desc: str = "",
        width: int = 0,
        height: int = 0,
        batch_id: int = 0,
        upload_time: int = 0,
        business_type: int = 0,
        business_data: bytes | None = None,
        extra_map_ext: dict[str, str] | None = None,
        extra_params: dict[str, str] | None = None,
    ) -> QzoneTencentPicUploadResult:
        """Upload the Android video-cover ImageUploadTask for a completed sVid."""

        path = Path(cover_path)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if not str(vid or "").strip():
            raise TencentUploadProtocolError("video cover upload requires sVid")
        file_size = path.stat().st_size
        upload_time = int(upload_time or time.time() * 1000)
        if not client_key and self.uin and upload_time:
            client_key = f"{self.uin}_{upload_time}"
        batch_id = int(batch_id or _batch_id_from_client_key(client_key) or upload_time)
        width, height = _resolve_image_size(path, width, height)
        params = {str(key): str(value) for key, value in dict(extra_params or {}).items()}
        params["vid"] = str(vid)
        if client_key:
            params.setdefault("clientkey", str(client_key))
        params.setdefault("raw_width", str(width))
        params.setdefault("raw_height", str(height))
        params.setdefault("raw_size", str(file_size))
        params.setdefault("show_geo", "0")
        map_ext = {"mobile_fakefeeds_clientkey": str(client_key or "")}
        external_map_ext = {str(key): str(value) for key, value in dict(extra_map_ext or {}).items()}
        external_map_ext.setdefault("is_client_upload_cover", "1")
        external_map_ext.setdefault("is_pic_video_mix_feeds", "1")
        if int(video_size or 0) > 0:
            external_map_ext.setdefault("mix_videoSize", str(int(video_size)))
        external_map_ext.setdefault("mix_isOriginalVideo", str(int(is_original_video or 0)))
        if int(duration_ms or 0) > 0:
            external_map_ext.setdefault("mix_time", str(int(duration_ms)))
        info_req = UploadPicInfoReq(
            desc=desc,
            batch_id=batch_id,
            multi_pic_info=MultiPicInfo(batch_upload_num=1, current_upload=0),
            extend_info=PicExtendInfo(params=params),
            pic_path=str(video_path or path),
            width=width,
            height=height,
            upload_time=upload_time,
            map_ext=map_ext,
            distinct_use=0x37DD,
            business_type=int(business_type or 0),
            business_data=business_data,
            external_map_ext=external_map_ext,
        )
        control_req = FileControlReq(
            uin=self.uin,
            token=self.token,
            appid=QZONE_PIC_UPLOAD_APPID,
            checksum=md5_file(path),
            check_type=TENCENT_UPLOAD_CHECK_TYPE_MD5,
            file_len=file_size,
            biz_req=encode_upload_pic_info_req(info_req),
        )
        with self._connect(host=QZONE_PIC_UPLOAD_HOST, port=QZONE_PIC_UPLOAD_PORT) as sock:
            control_rsp = self._send_control(sock, control_req)
            self._raise_on_result(control_rsp.result, "视频封面控制包被拒绝")
            if control_rsp.biz_rsp:
                pic_rsp = decode_upload_pic_info_rsp(control_rsp.biz_rsp)
                return QzoneTencentPicUploadResult(
                    response=pic_rsp,
                    uploaded_bytes=control_rsp.offset or file_size,
                    session=control_rsp.session,
                )
            return self._upload_pic_slices(sock, path, file_size, control_rsp)

    def _connect(self, *, host: str | None = None, port: int | None = None) -> Any:
        return self.socket_factory((host or self.host, int(port or self.port)), timeout=self.timeout)

    def _send_control(self, sock: Any, request: FileControlReq) -> FileControlRsp:
        payload = encode_file_batch_control_req(FileBatchControlReq(control_req={"1": request}))
        self._send_frame(sock, TENCENT_UPLOAD_CMD_CONTROL, payload)
        frame = self._read_frame(sock)
        batch_rsp = decode_file_batch_control_rsp(frame.payload)
        response = batch_rsp.control_rsp.get("1")
        if response is None:
            raise TencentUploadProtocolError("Tencent upload control response lacks key '1'")
        return response

    def _upload_video_slices(
        self,
        sock: Any,
        path: Path,
        file_size: int,
        control_rsp: FileControlRsp,
        *,
        upload_time: int = 0,
        client_key: str = "",
    ) -> QzoneTencentVideoUploadResult:
        def decode_result(payload: bytes, uploaded_bytes: int, session: str) -> QzoneTencentVideoUploadResult:
            video_rsp = decode_upload_video_info_rsp(payload)
            if not video_rsp.vid:
                raise TencentUploadProtocolError("UploadVideoInfoRsp 缺少 sVid")
            return QzoneTencentVideoUploadResult(
                vid=video_rsp.vid,
                business_type=video_rsp.business_type,
                business_data=video_rsp.business_data,
                uploaded_bytes=uploaded_bytes,
                session=session,
                upload_time=upload_time,
                client_key=client_key,
            )

        return self._upload_slices(
            sock,
            path,
            file_size,
            control_rsp,
            appid=QZONE_VIDEO_UPLOAD_APPID,
            check_type=TENCENT_UPLOAD_CHECK_TYPE_SHA1,
            reject_message="视频分片上传被拒绝",
            missing_message="Tencent upload finished without UploadVideoInfoRsp",
            decode_result=decode_result,
        )

    def _upload_pic_slices(
        self,
        sock: Any,
        path: Path,
        file_size: int,
        control_rsp: FileControlRsp,
    ) -> QzoneTencentPicUploadResult:
        def decode_result(payload: bytes, uploaded_bytes: int, session: str) -> QzoneTencentPicUploadResult:
            return QzoneTencentPicUploadResult(
                response=decode_upload_pic_info_rsp(payload),
                uploaded_bytes=uploaded_bytes,
                session=session,
            )

        return self._upload_slices(
            sock,
            path,
            file_size,
            control_rsp,
            appid=QZONE_PIC_UPLOAD_APPID,
            check_type=TENCENT_UPLOAD_CHECK_TYPE_MD5,
            reject_message="视频封面分片上传被拒绝",
            missing_message="Tencent upload finished without UploadPicInfoRsp",
            decode_result=decode_result,
        )

    def _upload_slices(
        self,
        sock: Any,
        path: Path,
        file_size: int,
        control_rsp: FileControlRsp,
        *,
        appid: str,
        check_type: int,
        reject_message: str,
        missing_message: str,
        decode_result: Callable[[bytes, int, str], Any],
    ) -> Any:
        session = control_rsp.session
        if not session:
            raise TencentUploadProtocolError("Tencent upload control response lacks session")
        slice_size = int(control_rsp.slice_size or TENCENT_UPLOAD_DEFAULT_SLICE_SIZE)
        offset = int(control_rsp.offset or 0)
        with path.open("rb") as handle:
            while offset < file_size:
                handle.seek(offset)
                chunk = handle.read(min(slice_size, file_size - offset))
                if not chunk:
                    break
                upload_req = FileUploadReq(
                    uin=self.uin,
                    appid=appid,
                    session=session,
                    offset=offset,
                    data=chunk,
                    check_type=check_type,
                    send_time=int(time.time()),
                )
                self._send_frame(sock, TENCENT_UPLOAD_CMD_FILE, encode_file_upload_req(upload_req))
                upload_rsp = decode_file_upload_rsp(self._read_frame(sock).payload)
                self._raise_on_result(upload_rsp.result, reject_message)
                if upload_rsp.biz_rsp:
                    return decode_result(
                        upload_rsp.biz_rsp,
                        max(offset + len(chunk), upload_rsp.offset),
                        upload_rsp.session or session,
                    )
                next_offset = int(upload_rsp.offset or 0)
                offset = next_offset if next_offset > offset else offset + len(chunk)
        raise TencentUploadProtocolError(missing_message)

    def _send_frame(self, sock: Any, cmd: int, payload: bytes) -> None:
        frame = encode_upload_pdu(cmd, self._next_seq(), payload)
        sock.sendall(frame)

    def _read_frame(self, sock: Any) -> TencentUploadPduFrame:
        prefix = _recv_exact(sock, 1 + PDU_HEADER_LENGTH)
        length = decode_upload_pdu_size(prefix)
        if length < len(prefix):
            raise TencentUploadPduError("PDU declared length is smaller than header")
        return decode_upload_pdu(prefix + _recv_exact(sock, length - len(prefix)))

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    @staticmethod
    def _raise_on_result(result: StResult, message: str) -> None:
        if int(result.ret or 0) != 0:
            suffix = f"：{result.msg}" if result.msg else f"，ret={result.ret}"
            raise QzoneTencentVideoUploadError(message + suffix)


def qzone_video_upload_protocol_spec(video_path: str | Path | None = None) -> QzoneVideoUploadProtocolSpec:
    credentials_ready = qzone_video_upload_credentials_configured()
    requirements = (
        NativeVideoDaemonRequirement(
            name="jce_codec",
            status="implemented",
            detail="Implemented minimal JCE codecs and schemas for FileControlReq, FileUploadReq, AuthToken, UploadVideoInfoReq, UploadVideoInfoRsp, and upload responses.",
        ),
        NativeVideoDaemonRequirement(
            name="socket_upload_client",
            status="implemented",
            detail="Implemented PDU/JCE control and slice upload client for video_qzone; it requires valid QQ upload login material at runtime.",
        ),
        NativeVideoDaemonRequirement(
            name="publishmood_business_data",
            status="implemented",
            detail="Record-video shuoshuo uses UniAttribute(hostuin, publishmood) as UploadVideoInfoReq.vBusiNessData/iBusiNessType=1 instead of a separate final publish RPC.",
        ),
        NativeVideoDaemonRequirement(
            name="video_cover_pic_qzone_upload",
            status="implemented",
            detail="Implemented the Android ImageUploadTask cover leg: pic_qzone, pic.upqzfile.com:80, UploadPicInfoReq/PicExtendInfo, vid/clientkey/mix_* fields, and MD5 file control.",
        ),
        NativeVideoDaemonRequirement(
            name="feed_vid_verification",
            status="implemented",
            detail="Daemon native video publish must poll recent feeds and verify the returned sVid before reporting success.",
        ),
        NativeVideoDaemonRequirement(
            name="qq_upload_login_material",
            status="configured" if credentials_ready else "missing",
            detail="Need vLoginData and optional vLoginKey compatible with TokenProvider.getAuthToken; configure QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64 for daemon background upload.",
        ),
    )
    sequence = (
        {
            "step": "control",
            "cmd": str(TENCENT_UPLOAD_CMD_CONTROL),
            "jce": "SLICE_UPLOAD/FileBatchControlReq -> FileControlReq",
            "biz_req": "FileUpload/UploadVideoInfoReq",
        },
        {
            "step": "slice",
            "cmd": str(TENCENT_UPLOAD_CMD_FILE),
            "jce": "SLICE_UPLOAD/FileUploadReq",
            "response": "FileUpload/UploadVideoInfoRsp when the upload finishes",
        },
        {
            "step": "cover_control",
            "cmd": str(TENCENT_UPLOAD_CMD_CONTROL),
            "jce": "SLICE_UPLOAD/FileBatchControlReq -> FileControlReq",
            "appid": QZONE_PIC_UPLOAD_APPID,
            "host": QZONE_PIC_UPLOAD_HOST,
            "biz_req": "FileUpload/UploadPicInfoReq with stExtendInfo.mapParams[vid/clientkey] and stExternalMapExt[mix_*]",
        },
        {
            "step": "cover_slice",
            "cmd": str(TENCENT_UPLOAD_CMD_FILE),
            "jce": "SLICE_UPLOAD/FileUploadReq",
            "response": "FileUpload/UploadPicInfoRsp when the cover upload finishes",
        },
        {
            "step": "feed_verification",
            "cmd": "poll",
            "jce": "recent feed raw/detail",
            "input": "the same sVid must appear before daemon reports success",
        },
        {
            "step": "publish_business_data",
            "cmd": "embedded",
            "jce": "UniAttribute(hostuin, publishmood)",
            "input": "UploadVideoInfoReq.iBusiNessType=1 and vBusiNessData before slice upload",
        },
    )
    return QzoneVideoUploadProtocolSpec(
        request_sequence=sequence,
        requirements=requirements,
        daemon_ready=credentials_ready,
    )


def qzone_video_upload_probe(video_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(video_path) if video_path else None
    spec = qzone_video_upload_protocol_spec(path)
    payload = spec.to_dict()
    payload["video_path"] = str(path) if path else ""
    payload["video_readable"] = bool(path and path.is_file())
    payload["reason"] = (
        "daemon native video upload, Android video-cover pic_qzone upload, and feed sVid verification are implemented; "
        "true background publishing requires QQ upload login material via QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64"
    )
    return payload


def _file_control_req_fields(request: FileControlReq) -> list[JceField]:
    fields = [
        JceField(0, request.uin),
        JceField(1, auth_token_struct(request.token)),
        JceField(2, request.appid),
        JceField(3, request.checksum),
        JceField(4, request.check_type),
        JceField(5, request.file_len),
        JceField(6, st_environment_struct(request.env)),
        JceField(7, request.model),
        JceField(8, request.biz_req),
        JceField(9, request.session),
        JceField(10, request.need_ip_redirect),
        JceField(11, request.asy_upload),
        JceField(13, request.slice_size),
        JceField(14, dict(request.extend_info or {})),
    ]
    if request.dump_req is not None:
        fields.insert(12, JceField(12, request.dump_req))
    return fields


def _source_struct(subtype: int, termtype: int, apptype: int) -> Any:
    return jce_struct([JceField(0, int(subtype or 0)), JceField(1, int(termtype or 0)), JceField(2, int(apptype or 0))])


def _qzone_video_format(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in {"mp4", "m4v", "mov"}:
        return "h264"
    if suffix:
        return suffix
    return "h264"


def _batch_id_from_client_key(client_key: str) -> int:
    text = str(client_key or "")
    if "_" not in text:
        return 0
    tail = text.rsplit("_", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return 0


def _resolve_image_size(path: Path, width: int, height: int) -> tuple[int, int]:
    if int(width or 0) > 0 and int(height or 0) > 0:
        return int(width), int(height)
    try:
        from PIL import Image

        with Image.open(path) as image:
            return int(width or image.width or 0), int(height or image.height or 0)
    except Exception:
        return int(width or 0), int(height or 0)


def _ugc_right_info_struct(ugc_right: int) -> Any:
    return jce_struct([JceField(0, int(ugc_right or 0))])


def _shoot_info_struct(shoot_time: int) -> Any:
    return jce_struct([JceField(1, int(shoot_time or 0))])


def _env_text(env: dict[str, str] | os._Environ[str], *keys: str) -> str:
    for key in keys:
        value = str(env.get(key) or "").strip()
        if value:
            return value
    return ""


def _env_base64(env: dict[str, str] | os._Environ[str], *keys: str) -> bytes:
    for key in keys:
        value = str(env.get(key) or "").strip()
        if not value:
            continue
        return _decode_base64_text(value, key)
    return b""


def _decode_base64_text(value: str, field: str) -> bytes:
    try:
        return base64.b64decode("".join(str(value or "").split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise QzoneNativeVideoCredentialError(f"{field} 不是合法的 base64 QQ upload 登录材料") from exc


def _env_int(env: dict[str, str] | os._Environ[str], default: int, *keys: str) -> int:
    value = _env_text(env, *keys)
    if not value:
        return int(default)
    try:
        return int(value)
    except ValueError as exc:
        raise QzoneNativeVideoCredentialError(f"{keys[0]} 必须是整数") from exc


def _recv_exact(sock: Any, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = int(length)
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise TencentUploadPduError("connection closed while reading PDU frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _u32be(value: int) -> bytes:
    if value < 0 or value > 0xFFFFFFFF:
        raise TencentUploadPduError("PDU integer field is outside uint32 range")
    return int(value).to_bytes(4, "big", signed=False)


def _read_u32be(buffer: bytes, offset: int) -> int:
    return int.from_bytes(buffer[offset : offset + 4], "big", signed=False)
