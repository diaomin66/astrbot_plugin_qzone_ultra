"""Tencent upload SDK protocol primitives for native Qzone video research."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


QZONE_VIDEO_UPLOAD_APPID = "video_qzone"
QZONE_VIDEO_UPLOAD_HOST = "video.upqzfile.com"
QZONE_VIDEO_UPLOAD_BACKUP_HOST = "video.upqzfilebk.com"
QZONE_VIDEO_UPLOAD_PORT = 80
QZONE_VIDEO_FILE_TYPE = "Video"
QZONE_VIDEO_BUSINESS_TYPE = "QZoneVideo"
QZONE_VIDEO_CONNECT_TYPE = "Epoll"

TENCENT_UPLOAD_CMD_CONTROL = 1
TENCENT_UPLOAD_CMD_FILE = 2

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


def qzone_video_upload_protocol_spec(video_path: str | Path | None = None) -> QzoneVideoUploadProtocolSpec:
    requirements = (
        NativeVideoDaemonRequirement(
            name="jce_codec",
            status="missing",
            detail="Need Tars/JCE codecs and schemas for SLICE_UPLOAD/FileControlReq, SLICE_UPLOAD/FileUploadReq, FileUpload/UploadVideoInfoReq, and FileUpload/UploadVideoInfoRsp.",
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
            "jce": "SLICE_UPLOAD/FileControlReq",
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
    payload["reason"] = "daemon native video upload is not enabled until JCE codecs, QQ upload login material, and final publish RPC are implemented"
    return payload


def _u32be(value: int) -> bytes:
    if value < 0 or value > 0xFFFFFFFF:
        raise TencentUploadPduError("PDU integer field is outside uint32 range")
    return int(value).to_bytes(4, "big", signed=False)


def _read_u32be(buffer: bytes, offset: int) -> int:
    return int.from_bytes(buffer[offset : offset + 4], "big", signed=False)
