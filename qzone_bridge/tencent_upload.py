"""Tencent upload SDK protocol primitives for native Qzone video research."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
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
QZONE_VIDEO_FILE_TYPE = "Video"
QZONE_VIDEO_BUSINESS_TYPE = "QZoneVideo"
QZONE_VIDEO_CONNECT_TYPE = "Epoll"

TENCENT_UPLOAD_CMD_CONTROL = 1
TENCENT_UPLOAD_CMD_FILE = 2
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
class AuthToken:
    type: int = TENCENT_UPLOAD_TOKEN_ENC_TYPE
    data: bytes = b""
    ext_key: bytes = b""
    appid: int = 0
    wt_appid: int = 0


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "vid": self.vid,
            "business_type": self.business_type,
            "business_data_length": len(self.business_data),
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


def decode_upload_video_info_rsp(payload: bytes) -> UploadVideoInfoRsp:
    nodes = decode_struct(payload)
    return UploadVideoInfoRsp(
        vid=as_str(field_value(nodes, 0), ""),
        business_type=as_int(field_value(nodes, 1), 0),
        business_data=as_bytes(field_value(nodes, 2), b""),
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
    return encode_struct(
        [
            JceField(0, request.uin),
            JceField(1, request.appid),
            JceField(2, request.session),
            JceField(3, request.offset),
            JceField(4, request.data),
            JceField(5, request.checksum),
            JceField(6, request.check_type),
            JceField(7, request.send_time),
        ]
    )


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


class QzoneTencentVideoUploader:
    """Synchronous Tencent upload SDK client for daemon-side video experiments.

    This implements the socket/PDU/JCE upload layer. A complete background
    Qzone publish still also needs valid QQ upload login material and the final
    Qzone publish RPC that consumes UploadVideoInfoRsp.
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
    ) -> QzoneTencentVideoUploadResult:
        path = Path(video_path)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        file_size = path.stat().st_size
        info_req = UploadVideoInfoReq(
            title=title or path.name,
            desc=desc,
            upload_time=int(time.time()),
            business_type=business_type,
            business_data=business_data,
            play_time=play_time,
            cover_url=cover_url,
            extend_info=dict(extend_info or {}),
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
                    )
            return self._upload_slices(sock, path, file_size, control_rsp)

    def _connect(self) -> Any:
        return self.socket_factory((self.host, self.port), timeout=self.timeout)

    def _send_control(self, sock: Any, request: FileControlReq) -> FileControlRsp:
        payload = encode_file_batch_control_req(FileBatchControlReq(control_req={"1": request}))
        self._send_frame(sock, TENCENT_UPLOAD_CMD_CONTROL, payload)
        frame = self._read_frame(sock)
        batch_rsp = decode_file_batch_control_rsp(frame.payload)
        response = batch_rsp.control_rsp.get("1")
        if response is None:
            raise TencentUploadProtocolError("Tencent upload control response lacks key '1'")
        return response

    def _upload_slices(
        self,
        sock: Any,
        path: Path,
        file_size: int,
        control_rsp: FileControlRsp,
    ) -> QzoneTencentVideoUploadResult:
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
                    appid=QZONE_VIDEO_UPLOAD_APPID,
                    session=session,
                    offset=offset,
                    data=chunk,
                    send_time=int(time.time()),
                )
                self._send_frame(sock, TENCENT_UPLOAD_CMD_FILE, encode_file_upload_req(upload_req))
                upload_rsp = decode_file_upload_rsp(self._read_frame(sock).payload)
                self._raise_on_result(upload_rsp.result, "视频分片上传被拒绝")
                if upload_rsp.biz_rsp:
                    video_rsp = decode_upload_video_info_rsp(upload_rsp.biz_rsp)
                    if not video_rsp.vid:
                        raise TencentUploadProtocolError("UploadVideoInfoRsp 缺少 sVid")
                    return QzoneTencentVideoUploadResult(
                        vid=video_rsp.vid,
                        business_type=video_rsp.business_type,
                        business_data=video_rsp.business_data,
                        uploaded_bytes=max(offset + len(chunk), upload_rsp.offset),
                        session=upload_rsp.session or session,
                    )
                next_offset = int(upload_rsp.offset or 0)
                offset = next_offset if next_offset > offset else offset + len(chunk)
        raise TencentUploadProtocolError("Tencent upload finished without UploadVideoInfoRsp")

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
            name="qq_upload_login_material",
            status="missing",
            detail="Need vLoginData and vLoginKey compatible with TokenProvider.getAuthToken; current PC Qzone cookies do not prove this binary login material.",
        ),
        NativeVideoDaemonRequirement(
            name="native_publish_rpc",
            status="missing",
            detail="Need the final publish RPC body that consumes UploadVideoInfoRsp.sVid and vBusiNessData after Tencent upload succeeds.",
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
            "step": "publish",
            "cmd": "unknown",
            "jce": "Qzone publish queue/RPC still under reverse engineering",
            "input": "UploadVideoInfoRsp.sVid and vBusiNessData",
        },
    )
    return QzoneVideoUploadProtocolSpec(
        request_sequence=sequence,
        requirements=requirements,
        daemon_ready=False,
    )


def qzone_video_upload_probe(video_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(video_path) if video_path else None
    spec = qzone_video_upload_protocol_spec(path)
    payload = spec.to_dict()
    payload["video_path"] = str(path) if path else ""
    payload["video_readable"] = bool(path and path.is_file())
    payload["reason"] = (
        "daemon native video upload wire protocol is implemented, but true background video publishing still needs "
        "QQ upload login material and the final Qzone publish RPC"
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
