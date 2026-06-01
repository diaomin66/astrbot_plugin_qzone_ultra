from __future__ import annotations

import base64
from pathlib import Path

import pytest

from qzone_bridge.jce import JceField, decode_struct, encode_struct, field_value, jce_struct
from qzone_bridge.tencent_upload import (
    AuthToken,
    FileBatchControlReq,
    FileControlReq,
    FileUploadReq,
    QZONE_PUBLISH_MOOD_TYPE,
    QZONE_PUBLISH_MOOD_UNI_KEY,
    QZONE_RECORD_VIDEO_BUSINESS_TYPE,
    QZONE_UNI_INT64_TYPE,
    QZONE_VIDEO_UPLOAD_APPID,
    QzoneNativeVideoCredentialError,
    QzoneTencentVideoUploader,
    StResult,
    TENCENT_UPLOAD_CMD_CONTROL,
    TENCENT_UPLOAD_CMD_FILE,
    UploadVideoInfoReq,
    decode_upload_pdu,
    decode_file_batch_control_rsp,
    decode_file_upload_rsp,
    decode_upload_video_info_rsp,
    encode_record_video_publish_business_data,
    encode_file_batch_control_req,
    encode_file_upload_req,
    encode_upload_pdu,
    encode_upload_video_info_req,
    qzone_video_upload_credentials_configured,
    qzone_video_upload_credentials_from_env,
    sha1_file,
)


def test_upload_video_info_req_jce_encodes_confirmed_tags() -> None:
    request = UploadVideoInfoReq(
        title="clip.mp4",
        desc="hello",
        flag=2,
        upload_time=1780329600,
        business_type=7,
        business_data=b"biz",
        play_time=1234,
        cover_url="https://example.test/cover.jpg",
        is_new=1,
        is_original_video=1,
        is_format_f20=0,
        extend_info={"video_type": "1", "qz_video_format": "mp4"},
        height=720,
        width=1280,
    )

    nodes = decode_struct(encode_upload_video_info_req(request))

    assert field_value(nodes, 0) == "clip.mp4"
    assert field_value(nodes, 1) == "hello"
    assert field_value(nodes, 2) == 2
    assert field_value(nodes, 3) == 1780329600
    assert field_value(nodes, 4) == 7
    assert field_value(nodes, 5) == b"biz"
    assert field_value(nodes, 6) == 1234
    assert field_value(nodes, 7) == "https://example.test/cover.jpg"
    assert field_value(nodes, 8) == 1
    assert field_value(nodes, 9) == 1
    assert field_value(nodes, 10) == 0
    assert field_value(nodes, 11) == {"video_type": "1", "qz_video_format": "mp4"}
    assert field_value(nodes, 12) == 720
    assert field_value(nodes, 13) == 1280


def test_upload_video_info_rsp_decoder_reads_vid_and_business_data() -> None:
    payload = encode_struct(
        [
            JceField(0, "fake-vid"),
            JceField(1, 8),
            JceField(2, b"business-data"),
        ]
    )

    decoded = decode_upload_video_info_rsp(payload)

    assert decoded.vid == "fake-vid"
    assert decoded.business_type == 8
    assert decoded.business_data == b"business-data"


def test_record_video_publish_business_data_encodes_mobile_uni_attribute() -> None:
    payload = encode_record_video_publish_business_data(
        uin=3112333596,
        content="hello video",
        video_size=4096,
        sync_weibo=True,
        client_key="client-1",
    )

    uni_map = field_value(decode_struct(payload), 0)

    assert set(uni_map) == {"hostuin", QZONE_PUBLISH_MOOD_UNI_KEY}
    assert field_value(decode_struct(uni_map["hostuin"][QZONE_UNI_INT64_TYPE]), 0) == 3112333596

    publish_payload = uni_map[QZONE_PUBLISH_MOOD_UNI_KEY][QZONE_PUBLISH_MOOD_TYPE]
    publish_req = field_value(decode_struct(publish_payload), 0)
    assert field_value(publish_req, 0) == 3112333596
    assert field_value(publish_req, 1) == "hello video"
    assert field_value(publish_req, 3) == 1
    assert field_value(publish_req, 6) is None
    assert field_value(publish_req, 11) == "client-1"
    assert field_value(publish_req, 19)["videoSize"] == "4096"

    source = field_value(publish_req, 8)
    assert field_value(source, 1) == 4
    assert field_value(source, 2) == 1


def test_file_batch_control_req_nests_auth_env_and_video_biz_req() -> None:
    video_req = encode_upload_video_info_req(UploadVideoInfoReq(title="clip.mp4", upload_time=1780329600))
    control_req = FileControlReq(
        uin="3112333596",
        token=AuthToken(type=2, data=b"login-data", ext_key=b"login-key", appid=16, wt_appid=32),
        checksum="abc123",
        file_len=1024,
        biz_req=video_req,
        extend_info={"trace": "trace-id"},
    )

    nodes = decode_struct(encode_file_batch_control_req(FileBatchControlReq(control_req={"1": control_req})))
    control_map = field_value(nodes, 0)
    nested = control_map["1"]

    assert field_value(nested, 0) == "3112333596"
    assert field_value(nested, 2) == QZONE_VIDEO_UPLOAD_APPID
    assert field_value(nested, 3) == "abc123"
    assert field_value(nested, 5) == 1024
    assert field_value(nested, 8) == video_req
    assert field_value(nested, 14) == {"trace": "trace-id"}

    token_nodes = field_value(nested, 1)
    assert field_value(token_nodes, 0) == 2
    assert field_value(token_nodes, 1) == b"login-data"
    assert field_value(token_nodes, 2) == b"login-key"
    assert field_value(token_nodes, 3) == 16
    assert field_value(token_nodes, 4) == 32

    env_nodes = field_value(nested, 6)
    assert field_value(env_nodes, 6) == "mqq"


def test_file_batch_control_rsp_and_file_upload_rsp_decoders() -> None:
    video_rsp = encode_struct([JceField(0, "vid-1"), JceField(1, 9), JceField(2, b"biz-rsp")])
    result = jce_struct([JceField(1, 0), JceField(2, 0), JceField(3, "ok")])
    control_rsp = jce_struct(
        [
            JceField(1, result),
            JceField(2, "session-1"),
            JceField(3, 128),
            JceField(4, 4096),
            JceField(5, video_rsp),
        ]
    )
    batch_payload = encode_struct([JceField(0, {"1": control_rsp})])

    decoded_batch = decode_file_batch_control_rsp(batch_payload)
    decoded_control = decoded_batch.control_rsp["1"]

    assert decoded_control.result == StResult(ret=0, flag=0, msg="ok")
    assert decoded_control.session == "session-1"
    assert decoded_control.offset == 128
    assert decoded_control.slice_size == 4096
    assert decode_upload_video_info_rsp(decoded_control.biz_rsp).vid == "vid-1"

    upload_payload = encode_struct(
        [
            JceField(1, result),
            JceField(2, "session-1"),
            JceField(3, 1024),
            JceField(4, video_rsp),
            JceField(5, 1780329601),
            JceField(6, 1780329602),
        ]
    )
    decoded_upload = decode_file_upload_rsp(upload_payload)

    assert decoded_upload.session == "session-1"
    assert decoded_upload.offset == 1024
    assert decoded_upload.receive_time == 1780329601
    assert decoded_upload.response_time == 1780329602
    assert decode_upload_video_info_rsp(decoded_upload.biz_rsp).business_data == b"biz-rsp"


def test_file_upload_req_encodes_slice_fields() -> None:
    nodes = decode_struct(
        encode_file_upload_req(
            request=FileUploadReq(
                uin="3112333596",
                appid=QZONE_VIDEO_UPLOAD_APPID,
                session="session-1",
                offset=4096,
                data=b"chunk",
                send_time=1780329603,
            )
        )
    )

    assert field_value(nodes, 0) == "3112333596"
    assert field_value(nodes, 1) == QZONE_VIDEO_UPLOAD_APPID
    assert field_value(nodes, 2) == "session-1"
    assert field_value(nodes, 3) == 4096
    assert field_value(nodes, 4) == b"chunk"
    assert field_value(nodes, 6) == 1
    assert field_value(nodes, 7) == 1780329603


def test_qzone_tencent_video_uploader_requires_login_data() -> None:
    with pytest.raises(QzoneNativeVideoCredentialError):
        QzoneTencentVideoUploader(uin=3112333596, login_data=b"")


def test_qzone_tencent_video_uploader_control_and_slice_flow(tmp_path: Path) -> None:
    video_rsp = encode_struct([JceField(0, "vid-1"), JceField(1, 9), JceField(2, b"biz-rsp")])
    result = jce_struct([JceField(1, 0), JceField(2, 0), JceField(3, "ok")])
    control_rsp = jce_struct(
        [
            JceField(1, result),
            JceField(2, "session-1"),
            JceField(3, 0),
            JceField(4, 5),
        ]
    )
    upload_rsp = encode_struct(
        [
            JceField(1, result),
            JceField(2, "session-1"),
            JceField(3, 5),
            JceField(4, video_rsp),
        ]
    )
    socket = _FakeSocket(
        [
            encode_upload_pdu(TENCENT_UPLOAD_CMD_CONTROL, 101, encode_struct([JceField(0, {"1": control_rsp})])),
            encode_upload_pdu(TENCENT_UPLOAD_CMD_FILE, 102, upload_rsp),
        ]
    )
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"chunk")

    uploader = QzoneTencentVideoUploader(
        uin=3112333596,
        login_data=b"login-data",
        login_key=b"login-key",
        socket_factory=lambda *args, **kwargs: socket,
    )
    result = uploader.upload_video(video, title="clip.mp4")

    assert result.vid == "vid-1"
    assert result.business_type == 9
    assert result.business_data == b"biz-rsp"
    assert result.uploaded_bytes == 5
    assert result.session == "session-1"
    assert [decode_upload_pdu(frame).header.cmd for frame in socket.sent] == [
        TENCENT_UPLOAD_CMD_CONTROL,
        TENCENT_UPLOAD_CMD_FILE,
    ]


def test_qzone_tencent_video_uploader_embeds_record_video_publish_data(tmp_path: Path) -> None:
    video_rsp = encode_struct([JceField(0, "vid-1"), JceField(1, 9), JceField(2, b"biz-rsp")])
    result = jce_struct([JceField(1, 0), JceField(2, 0), JceField(3, "ok")])
    control_rsp = jce_struct([JceField(1, result), JceField(2, "session-1"), JceField(3, 5), JceField(5, video_rsp)])
    socket = _FakeSocket(
        [encode_upload_pdu(TENCENT_UPLOAD_CMD_CONTROL, 101, encode_struct([JceField(0, {"1": control_rsp})]))]
    )
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"chunk")

    uploader = QzoneTencentVideoUploader(
        uin=3112333596,
        login_data=b"login-data",
        login_key=b"login-key",
        socket_factory=lambda *args, **kwargs: socket,
    )
    uploader.upload_video(video, title="clip.mp4", publish_content="hello")

    control_frame = decode_upload_pdu(socket.sent[0])
    control_map = field_value(decode_struct(control_frame.payload), 0)
    control_req = control_map["1"]
    info_nodes = decode_struct(field_value(control_req, 8))

    assert field_value(info_nodes, 4) == QZONE_RECORD_VIDEO_BUSINESS_TYPE
    business_data = field_value(info_nodes, 5)
    uni_map = field_value(decode_struct(business_data), 0)
    publish_payload = uni_map[QZONE_PUBLISH_MOOD_UNI_KEY][QZONE_PUBLISH_MOOD_TYPE]
    publish_req = field_value(decode_struct(publish_payload), 0)
    assert field_value(publish_req, 1) == "hello"
    assert field_value(publish_req, 19)["videoSize"] == "5"


def test_qzone_video_upload_credentials_from_env() -> None:
    env = {
        "QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64": base64.b64encode(b"login-data").decode("ascii"),
        "QZONE_VIDEO_UPLOAD_LOGIN_KEY_B64": base64.b64encode(b"login-key").decode("ascii"),
        "QZONE_VIDEO_UPLOAD_TOKEN_TYPE": "3",
        "QZONE_VIDEO_UPLOAD_TOKEN_APPID": "16",
        "QZONE_VIDEO_UPLOAD_TOKEN_WT_APPID": "32",
    }

    credentials = qzone_video_upload_credentials_from_env(env)

    assert qzone_video_upload_credentials_configured(env) is True
    assert credentials.login_data == b"login-data"
    assert credentials.login_key == b"login-key"
    assert credentials.token_type == 3
    assert credentials.token_appid == 16
    assert credentials.token_wt_appid == 32


def test_qzone_video_upload_credentials_reject_invalid_base64() -> None:
    assert qzone_video_upload_credentials_configured({}) is False
    with pytest.raises(QzoneNativeVideoCredentialError):
        qzone_video_upload_credentials_from_env({"QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64": "***not-base64***"})


def test_sha1_file_matches_video_upload_checksum(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video bytes")

    assert sha1_file(video) == "f449240a1fb4064805c724f16574b68e75b8cfd8"


class _FakeSocket:
    def __init__(self, responses: list[bytes]) -> None:
        self._buffer = b"".join(responses)
        self.sent: list[bytes] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def sendall(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def recv(self, size: int) -> bytes:
        chunk = self._buffer[:size]
        self._buffer = self._buffer[size:]
        return chunk
