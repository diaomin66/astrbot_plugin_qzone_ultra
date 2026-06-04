from __future__ import annotations

import asyncio
from pathlib import Path
import types

import httpx
import pytest

from qzone_bridge.client import QzoneClient
from qzone_bridge.models import SessionState


def _response(method: str, url: str, payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request(method, url))


def test_h5_video_control_payload_uses_qzone_cookie_token() -> None:
    from qzone_bridge.h5_video import build_h5_video_control_payload

    payload = build_h5_video_control_payload(
        uin=3112333596,
        p_skey="ps-key",
        checksum="a" * 40,
        file_size=1234,
        title="clip.mp4",
        desc="hello",
        play_time=1000,
        upload_time=1780399990,
        video_format="mp4",
    )

    control = payload["control_req"][0]
    assert control["appid"] == "video_qzone"
    assert control["cmd"] == "FileUploadVideo"
    assert control["token"] == {"type": 4, "data": "ps-key", "appid": 5}
    assert control["checksum"] == "a" * 40
    assert control["check_type"] == 1
    assert control["file_len"] == 1234
    assert control["env"] == {"refer": "qzone", "deviceInfo": "h5"}
    assert control["biz_req"]["sTitle"] == "clip.mp4"
    assert control["biz_req"]["sDesc"] == "hello"
    assert control["biz_req"]["iPlayTime"] == 1000
    assert control["biz_req"]["extend_info"]["video_type"] == "3"
    assert control["biz_req"]["extend_info"]["qz_video_format"] == "mp4"


def test_h5_video_slice_multipart_marks_blob_as_octet_stream_by_default() -> None:
    from qzone_bridge.h5_video import encode_h5_video_slice_multipart

    body, content_type = encode_h5_video_slice_multipart(
        uin=3112333596,
        session="sess",
        seq=1,
        offset=0,
        end=3,
        slice_size=3,
        chunk=b"abc",
        boundary="BOUNDARY",
    )
    text = body.decode("latin1")
    data_header = text.split('name="data"', 1)[1].split("\r\n\r\n", 1)[0]

    assert content_type == "multipart/form-data; boundary=BOUNDARY"
    assert 'filename="blob"' in data_header
    assert "Content-Type: application/octet-stream" in data_header
    assert 'name="appid"' in text
    assert "video_qzone" in text

    fallback_body, _ = encode_h5_video_slice_multipart(
        uin=3112333596,
        session="sess",
        seq=1,
        offset=0,
        end=3,
        slice_size=3,
        chunk=b"abc",
        boundary="BOUNDARY",
        data_content_type=None,
    )
    fallback_header = fallback_body.decode("latin1").split('name="data"', 1)[1].split("\r\n\r\n", 1)[0]
    assert "Content-Type" not in fallback_header


def test_h5_video_gtk_prefers_skey_bkn_while_token_uses_p_skey() -> None:
    from qzone_bridge.h5_video import h5_video_gtk, h5_video_token_data
    from qzone_bridge.parser import cookie_gtk

    session = SessionState(
        uin=3112333596,
        cookies={
            "p_skey": "ps-key",
            "skey": "s-key",
        },
    )

    assert h5_video_token_data(session) == "ps-key"
    assert h5_video_gtk(session.cookies) == cookie_gtk({"skey": "s-key"})
    assert h5_video_gtk({"p_skey": "ps-key", "bkn": "12345"}) == 12345


def test_qzone_client_h5_video_upload_posts_control_and_slices(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class _HTTP:
        async def request(self, method: str, url: str, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            if "FileBatchControl" in url:
                control = kwargs["json"]["control_req"][0]
                assert control["token"]["data"] == "ps-key"
                assert control["appid"] == "video_qzone"
                return _response(method, url, {"ret": 0, "data": {"session": "sess", "slice_size": 3}})
            if "FileUploadVideo" in url:
                content = kwargs["content"]
                header = content.decode("latin1").split('name="data"', 1)[1].split("\r\n\r\n", 1)[0]
                assert 'filename="blob"' in header
                assert "Content-Type: application/octet-stream" in header
                assert kwargs["headers"]["Origin"] == "https://h5.qzone.qq.com"
                seq = int(kwargs["params"]["seq"])
                payload = {"ret": 0, "data": {"offset": kwargs["params"]["end"], "biz": {}}}
                if seq == 2:
                    payload["data"]["biz"]["sVid"] = "vid-h5"
                return _response(method, url, payload)
            raise AssertionError(url)

    expected_gtk = 1234567
    client = QzoneClient(
        SessionState(
            uin=3112333596,
            cookies={"uin": "o3112333596", "p_skey": "ps-key", "skey": "s-key", "bkn": str(expected_gtk)},
        )
    )
    client._client = _HTTP()
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"abcde")

    result = asyncio.run(client.upload_h5_video(video, title="clip.mp4", desc="hello", play_time=1000))

    assert result.vid == "vid-h5"
    assert result.uploaded_bytes == 5
    assert result.session == "sess"
    assert [call["url"] for call in calls].count("https://h5.qzone.qq.com/webapp/json/sliceUpload/FileUploadVideo") == 2
    assert all(call["params"]["g_tk"] == expected_gtk for call in calls)


def test_qzone_client_h5_video_upload_retries_without_blob_content_type_on_115(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class _HTTP:
        async def request(self, method: str, url: str, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            if "FileBatchControl" in url:
                return _response(method, url, {"ret": 0, "data": {"session": "sess", "slice_size": 10}})
            if "FileUploadVideo" in url:
                content = kwargs["content"]
                header = content.decode("latin1").split('name="data"', 1)[1].split("\r\n\r\n", 1)[0]
                if "Content-Type: application/octet-stream" in header:
                    return _response(
                        method,
                        url,
                        {"ret": -115, "msg": "bad content type", "data": {"ret": -115}},
                    )
                return _response(method, url, {"ret": 0, "data": {"offset": "5", "biz": {"sVid": "vid-retry"}}})
            raise AssertionError(url)

    client = QzoneClient(SessionState(uin=3112333596, cookies={"uin": "o3112333596", "p_skey": "ps-key"}))
    client._client = _HTTP()
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"abcde")

    result = asyncio.run(client.upload_h5_video(video, title="clip.mp4", desc="hello", play_time=1000))

    slice_calls = [call for call in calls if "FileUploadVideo" in str(call["url"])]
    assert result.vid == "vid-retry"
    assert len(slice_calls) == 2
    assert slice_calls[0]["params"]["retry"] == 0
    assert slice_calls[1]["params"]["retry"] == 1


def test_qzone_client_publish_video_mood_uses_web_richval() -> None:
    captured: dict[str, object] = {}

    class _HTTP:
        async def request(self, method: str, url: str, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return _response(method, url, {"code": 0, "tid": "fid-1"})

    client = QzoneClient(SessionState(uin=3112333596, cookies={"uin": "o3112333596", "p_skey": "ps-key"}))
    client._client = _HTTP()

    payload = asyncio.run(client.publish_video_mood("hello", vid="vid-h5", sync_weibo=True))

    assert payload["tid"] == "fid-1"
    data = captured["data"]
    assert data["richtype"] == "3"
    assert data["subrichtype"] == "7"
    assert data["issyncweibo"] == 1
    assert "rich_flag=4" in data["richval"]
    assert "vid=vid-h5" in data["richval"]
    assert "qzvideo%2Fvid-h5" in data["richval"]


def test_daemon_publish_post_uses_h5_cookie_upload_without_a2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qzone_bridge import daemon as daemon_mod
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.media import PostMedia

    monkeypatch.delenv("QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64", raising=False)
    monkeypatch.delenv("QZONE_UPLOAD_LOGIN_DATA_B64", raising=False)
    monkeypatch.setattr(
        daemon_mod,
        "QzoneTencentVideoUploader",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("H5 cookie path must not require A2 uploader")),
    )
    monkeypatch.setattr(daemon_mod, "_probe_video_duration_ms", lambda _path: 2345)
    captured: dict[str, object] = {}

    class _Client:
        timeout = 1.5

        async def upload_h5_video(self, path, **kwargs):
            captured["h5_upload_path"] = path
            captured["h5_upload_kwargs"] = kwargs
            return types.SimpleNamespace(
                vid="vid-h5",
                checksum="sha1",
                uploaded_bytes=5,
                session="sess",
                slice_size=3,
                to_dict=lambda: {
                    "vid": "vid-h5",
                    "checksum": "sha1",
                    "uploaded_bytes": 5,
                    "session": "sess",
                    "slice_size": 3,
                },
            )

        async def publish_video_mood(self, content, **kwargs):
            captured["publish_content"] = content
            captured["publish_kwargs"] = kwargs
            return {"tid": "fid-video"}

    service = object.__new__(QzoneDaemonService)
    service.store = types.SimpleNamespace(root=tmp_path)
    service.state = types.SimpleNamespace(session=SessionState(uin=3112333596, cookies={"p_skey": "ps-key"}))
    service.client = _Client()
    service._ensure_session_ready = lambda: None
    service._set_success = lambda defer_save=True: None

    async def fake_wait_for_native_video_feed(**_kwargs):
        return {"fid": "fid-video", "raw": {"vid": "vid-h5"}}

    service._wait_for_native_video_feed = fake_wait_for_native_video_feed

    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"chunk")
    video = PostMedia(kind="video", source=str(video_path), name="clip.mp4", mime_type="video/mp4", trusted_local=True)

    payload = asyncio.run(service.publish_post(content="hello", media=[video.to_dict()], content_sanitized=True))

    assert payload["native_video"] is True
    assert payload["status"] == "published_native_video"
    assert payload["vid"] == "vid-h5"
    assert payload["raw"]["method"] == "h5_slice_upload"
    assert captured["h5_upload_path"] == video_path
    assert captured["h5_upload_kwargs"]["play_time"] == 2345
    assert captured["publish_content"] == "hello"
    assert captured["publish_kwargs"] == {"vid": "vid-h5", "sync_weibo": False}


def test_daemon_h5_video_publish_accepts_trusted_no_extension_video(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qzone_bridge import daemon as daemon_mod
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.media import PostMedia

    monkeypatch.delenv("QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64", raising=False)
    monkeypatch.delenv("QZONE_UPLOAD_LOGIN_DATA_B64", raising=False)
    monkeypatch.setattr(daemon_mod, "_probe_video_duration_ms", lambda _path: 2345)
    captured: dict[str, object] = {}

    class _Client:
        timeout = 1.5

        async def upload_h5_video(self, path, **kwargs):
            captured["h5_upload_path"] = path
            captured["h5_upload_kwargs"] = kwargs
            return types.SimpleNamespace(
                vid="vid-h5-noext",
                to_dict=lambda: {"vid": "vid-h5-noext"},
            )

        async def publish_video_mood(self, content, **kwargs):
            captured["publish_content"] = content
            captured["publish_kwargs"] = kwargs
            return {"tid": "fid-video", "feedinfo": "qzvideo/vid-h5-noext"}

    service = object.__new__(QzoneDaemonService)
    service.store = types.SimpleNamespace(root=tmp_path)
    service.state = types.SimpleNamespace(session=SessionState(uin=3112333596, cookies={"p_skey": "ps-key"}))
    service.client = _Client()
    service._ensure_session_ready = lambda: None
    service._set_success = lambda defer_save=True: None

    async def fake_wait_for_native_video_feed(**_kwargs):
        return {"fid": "fid-video", "raw": {"vid": "vid-h5-noext"}}

    service._wait_for_native_video_feed = fake_wait_for_native_video_feed

    video_path = tmp_path / "videoseg_no_extension"
    video_path.write_bytes(b"chunk")
    video = PostMedia(
        kind="video",
        source=str(video_path),
        name="clip",
        mime_type="video/mp4",
        raw_type="video",
        trusted_local=True,
    )

    payload = asyncio.run(service.publish_post(content="", media=[video.to_dict()], content_sanitized=True))

    assert payload["native_video"] is True
    assert payload["status"] == "published_native_video"
    assert payload["vid"] == "vid-h5-noext"
    assert captured["h5_upload_path"] == video_path
    assert captured["h5_upload_kwargs"]["play_time"] == 2345
    assert captured["publish_content"] == ""


def test_daemon_h5_video_publish_accepts_publish_result_feedinfo_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.media import PostMedia

    monkeypatch.delenv("QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64", raising=False)
    monkeypatch.delenv("QZONE_UPLOAD_LOGIN_DATA_B64", raising=False)

    class _Client:
        timeout = 1.5

        async def upload_h5_video(self, *_args, **_kwargs):
            return types.SimpleNamespace(
                vid="vid-h5-feedinfo",
                to_dict=lambda: {"vid": "vid-h5-feedinfo"},
            )

        async def publish_video_mood(self, *_args, **_kwargs):
            return {
                "code": 0,
                "tid": "fid-feedinfo",
                "feedinfo": '<div class="f-ct-video" data-v_vidiourl="qzvideo/vid-h5-feedinfo"></div>',
            }

    service = object.__new__(QzoneDaemonService)
    service.store = types.SimpleNamespace(root=tmp_path)
    service.state = types.SimpleNamespace(session=SessionState(uin=3112333596, cookies={"p_skey": "ps-key"}))
    service.client = _Client()
    service._ensure_session_ready = lambda: None
    service._set_success = lambda defer_save=True: None

    async def fake_wait_for_native_video_feed(**_kwargs):
        raise AssertionError("publish_result feedinfo already verifies the sVid")

    service._wait_for_native_video_feed = fake_wait_for_native_video_feed

    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"chunk")
    video = PostMedia(kind="video", source=str(video_path), name="clip.mp4", mime_type="video/mp4", trusted_local=True)

    payload = asyncio.run(service.publish_post(content="hello", media=[video.to_dict()], content_sanitized=True))

    assert payload["status"] == "published_native_video"
    assert payload["vid"] == "vid-h5-feedinfo"
    assert payload["fid"] == "fid-feedinfo"
    assert payload["raw"]["verified_feed"]["verification_source"] == "publish_result_feedinfo"
