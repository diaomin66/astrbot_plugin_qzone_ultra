from __future__ import annotations

import asyncio
from pathlib import Path
import types

import httpx
import pytest

from qzone_bridge.client import H5_VIDEO_REQUEST_TIMEOUT_SECONDS, H5_VIDEO_SLICE_REQUEST_TIMEOUT_SECONDS, QzoneClient
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
    assert control["asy_upload"] == 0
    assert control["biz_req"]["sTitle"] == "clip.mp4"
    assert control["biz_req"]["sDesc"] == "hello"
    assert control["biz_req"]["iPlayTime"] == 1000
    assert control["biz_req"]["iNeedFeeds"] == 0
    assert control["biz_req"]["iIsNew"] == 111
    assert control["biz_req"]["extend_info"]["video_type"] == "3"
    assert control["biz_req"]["extend_info"]["qz_video_format"] == "mp4"
    assert control["biz_req"]["extend_info"]["ugc_right"] == "1"
    assert control["biz_req"]["extend_info"]["who"] == "1"


def test_h5_video_cover_control_payload_links_vid_clientkey_and_mix_fields() -> None:
    from qzone_bridge.h5_video import build_h5_video_cover_control_payload

    payload = build_h5_video_cover_control_payload(
        uin=3112333596,
        p_skey="ps-key",
        checksum="b" * 32,
        file_size=4567,
        vid="vid-h5",
        client_key="3112333596_1780399990",
        video_size=123456,
        duration_ms=2345,
        desc="hello",
        cover_path="cover.jpg",
        width=320,
        height=180,
        upload_time=1780399990,
    )

    control = payload["control_req"][0]
    biz_req = control["biz_req"]
    params = biz_req["stExtendInfo"]["mapParams"]
    external = biz_req["stExternalMapExt"]
    assert control["appid"] == "pic_qzone"
    assert control["cmd"] == "FileUpload"
    assert control["token"] == {"type": 4, "data": "ps-key", "appid": 5}
    assert control["checksum"] == "b" * 32
    assert control["check_type"] == 0
    assert control["file_len"] == 4567
    assert control["asy_upload"] == 0
    assert biz_req["iNeedFeeds"] == 1
    assert biz_req["sPicDesc"] == "hello"
    assert biz_req["iAlbumTypeID"] == 7
    assert biz_req["iUploadType"] == 2
    assert biz_req["iBatchID"] == 1780399990
    assert biz_req["iUploadTime"] == 1780399990
    assert biz_req["iPicWidth"] == 320
    assert biz_req["iPicHight"] == 180
    assert biz_req["iDistinctUse"] == 0x37DD
    assert biz_req["mapExt"]["mobile_fakefeeds_clientkey"] == "3112333596_1780399990"
    assert params["vid"] == "vid-h5"
    assert params["clientkey"] == "3112333596_1780399990"
    assert params["raw_width"] == "320"
    assert params["raw_height"] == "180"
    assert params["raw_size"] == "4567"
    assert params["ugc_right"] == "1"
    assert params["who"] == "1"
    assert external["is_client_upload_cover"] == "1"
    assert external["is_pic_video_mix_feeds"] == "1"
    assert external["ugc_right"] == "1"
    assert external["who"] == "1"
    assert external["mix_videoSize"] == "123456"
    assert external["mix_time"] == "2345"


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
    control_calls = [call for call in calls if "FileBatchControl" in str(call["url"])]
    assert len(control_calls) == 1
    assert control_calls[0]["timeout"] == H5_VIDEO_REQUEST_TIMEOUT_SECONDS
    slice_calls = [call for call in calls if call["url"] == "https://h5.qzone.qq.com/webapp/json/sliceUpload/FileUploadVideo"]
    assert len(slice_calls) == 2
    assert all(call["timeout"] == H5_VIDEO_SLICE_REQUEST_TIMEOUT_SECONDS for call in slice_calls)
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


def test_qzone_client_h5_video_cover_upload_posts_pic_qzone_control_and_slices(tmp_path: Path) -> None:
    from PIL import Image

    calls: list[dict[str, object]] = []

    class _HTTP:
        async def request(self, method: str, url: str, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            if "FileBatchControl" in url:
                control = kwargs["json"]["control_req"][0]
                biz_req = control["biz_req"]
                assert control["appid"] == "pic_qzone"
                assert control["cmd"] == "FileUpload"
                assert control["token"]["data"] == "ps-key"
                assert biz_req["stExtendInfo"]["mapParams"]["vid"] == "vid-h5"
                assert biz_req["stExtendInfo"]["mapParams"]["clientkey"] == "3112333596_1780399990"
                assert biz_req["stExternalMapExt"]["is_client_upload_cover"] == "1"
                assert biz_req["stExternalMapExt"]["is_pic_video_mix_feeds"] == "1"
                assert biz_req["stExternalMapExt"]["ugc_right"] == "1"
                assert biz_req["stExternalMapExt"]["who"] == "1"
                assert biz_req["stExternalMapExt"]["mix_videoSize"] == "123456"
                assert biz_req["stExternalMapExt"]["mix_time"] == "2345"
                assert biz_req["stExtendInfo"]["mapParams"]["ugc_right"] == "1"
                assert biz_req["stExtendInfo"]["mapParams"]["who"] == "1"
                assert biz_req["iNeedFeeds"] == 1
                assert biz_req["iPicWidth"] == 4
                assert biz_req["iPicHight"] == 2
                return _response(method, url, {"ret": 0, "data": {"session": "cover-sess", "slice_size": 4096}})
            if url == "https://h5.qzone.qq.com/webapp/json/sliceUpload/FileUpload":
                content = kwargs["content"]
                text = content.decode("latin1")
                assert "pic_qzone" in text
                assert 'name="cmd"' in text
                assert "FileUpload" in text
                assert 'name="biz_req.iUploadType"' in text
                assert kwargs["headers"]["Origin"] == "https://h5.qzone.qq.com"
                return _response(method, url, {"ret": 0, "data": {"lloc": "cover-photo"}})
            raise AssertionError(url)

    client = QzoneClient(
        SessionState(
            uin=3112333596,
            cookies={"uin": "o3112333596", "p_skey": "ps-key", "skey": "s-key", "bkn": "12345"},
        )
    )
    client._client = _HTTP()
    cover = tmp_path / "cover.jpg"
    Image.new("RGB", (4, 2), color=(255, 0, 0)).save(cover, format="JPEG")

    result = asyncio.run(
        client.upload_h5_video_cover(
            cover,
            vid="vid-h5",
            client_key="3112333596_1780399990",
            upload_time=1780399990,
            video_size=123456,
            duration_ms=2345,
            desc="hello",
        )
    )

    assert result.photo_id == "cover-photo"
    assert result.uploaded_bytes == cover.stat().st_size
    assert result.session == "cover-sess"
    control_calls = [call for call in calls if "FileBatchControl" in str(call["url"])]
    slice_calls = [call for call in calls if call["url"] == "https://h5.qzone.qq.com/webapp/json/sliceUpload/FileUpload"]
    assert len(control_calls) == 1
    assert len(slice_calls) == 1
    assert control_calls[0]["timeout"] == H5_VIDEO_REQUEST_TIMEOUT_SECONDS
    assert slice_calls[0]["timeout"] == H5_VIDEO_SLICE_REQUEST_TIMEOUT_SECONDS
    assert all(call["params"]["g_tk"] == 12345 for call in calls)


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
    assert data["who"] == "1"
    assert data["ugc_right"] == 1
    assert data["pic_template"] == ""
    assert data["special_url"] == ""
    assert data["to_tweet"] == 0
    assert data["richtype"] == "3"
    assert data["subrichtype"] == "6"
    assert data["issyncweibo"] == 1
    assert "who=5" in data["richval"]
    assert data["richval"].count("who=") == 1
    assert "rich_flag=4" in data["richval"]
    assert "vid=vid-h5" in data["richval"]
    assert "qzvideo%2Fvid-h5" in data["richval"]
    assert captured["timeout"] == H5_VIDEO_REQUEST_TIMEOUT_SECONDS


def test_daemon_publish_post_uses_h5_cookie_upload_without_a2(
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
                vid="vid-h5",
                uploaded_bytes=5,
                session="sess",
                to_dict=lambda: {
                    "vid": "vid-h5",
                    "uploaded_bytes": 5,
                    "session": "sess",
                },
            )

        async def upload_h5_video_cover(self, path, **kwargs):
            captured["h5_cover_path"] = path
            captured["h5_cover_kwargs"] = kwargs
            return types.SimpleNamespace(
                uploaded_bytes=3,
                session="cover-sess",
                to_dict=lambda: {
                    "uploaded_bytes": 3,
                    "session": "cover-sess",
                },
            )

        async def publish_video_mood(self, content, *, vid, sync_weibo=False):
            captured["publish_content"] = content
            captured["publish_vid"] = vid
            captured["publish_sync_weibo"] = sync_weibo
            return {"code": 0, "tid": "fid-video", "msg": "ok"}

    class _Uploader:
        def __init__(self, **_kwargs):
            raise AssertionError("web cookie video path must not use Tencent upload socket credentials")

    monkeypatch.setattr(daemon_mod, "QzoneTencentVideoUploader", _Uploader)

    service = object.__new__(QzoneDaemonService)
    service.store = types.SimpleNamespace(root=tmp_path)
    service.state = types.SimpleNamespace(session=SessionState(uin=3112333596, cookies={"p_skey": "ps-key"}))
    service.client = _Client()
    service._ensure_session_ready = lambda: None
    service._set_success = lambda defer_save=True: None

    async def fake_wait_for_native_video_feed(**kwargs):
        captured["verify_kwargs"] = kwargs
        return {"fid": "fid-video", "raw": {"vid": "vid-h5"}}

    service._wait_for_native_video_feed = fake_wait_for_native_video_feed

    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"chunk")
    cover_path = tmp_path / "cover.jpg"
    cover_path.write_bytes(b"jpg")
    monkeypatch.setattr(
        daemon_mod,
        "video_cover_media",
        lambda *_args, **_kwargs: PostMedia(kind="image", source=str(cover_path), name="cover.jpg", trusted_local=True),
    )
    video = PostMedia(kind="video", source=str(video_path), name="clip.mp4", mime_type="video/mp4", trusted_local=True)

    payload = asyncio.run(service.publish_post(content="hello", media=[video.to_dict()], content_sanitized=True))

    assert payload["native_video"] is True
    assert payload["status"] == "published_native_video"
    assert payload["vid"] == "vid-h5"
    assert payload["raw"]["method"] == "h5_video_cover_publish"
    assert payload["raw"]["publish_result"]["tid"] == "fid-video"
    assert captured["h5_upload_path"] == video_path
    assert captured["h5_upload_kwargs"]["title"] == "clip.mp4"
    assert captured["h5_upload_kwargs"]["desc"] == ""
    assert captured["h5_upload_kwargs"]["play_time"] == 2345
    assert captured["h5_upload_kwargs"]["upload_time"]
    assert captured["h5_cover_path"] == cover_path
    assert captured["h5_cover_kwargs"]["vid"] == "vid-h5"
    assert captured["h5_cover_kwargs"]["video_path"] == video_path
    assert captured["h5_cover_kwargs"]["video_size"] == 5
    assert captured["h5_cover_kwargs"]["duration_ms"] == 2345
    assert captured["h5_cover_kwargs"]["desc"] == ""
    assert captured["h5_cover_kwargs"]["client_key"] == f"3112333596_{captured['h5_upload_kwargs']['upload_time']}"
    assert captured["h5_cover_kwargs"]["upload_time"] == captured["h5_upload_kwargs"]["upload_time"]
    assert captured["h5_cover_kwargs"]["need_feeds"] == 1
    assert captured["publish_content"] == "hello"
    assert captured["publish_vid"] == "vid-h5"
    assert captured["publish_sync_weibo"] is False
    assert captured["verify_kwargs"] == {"vid": "vid-h5", "fid": "fid-video"}


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

        async def upload_h5_video_cover(self, path, **kwargs):
            captured["h5_cover_path"] = path
            captured["h5_cover_kwargs"] = kwargs
            return types.SimpleNamespace(to_dict=lambda: {"photo_id": "cover-photo"})

        async def publish_video_mood(self, content, *, vid, sync_weibo=False):
            captured["publish_content"] = content
            captured["publish_vid"] = vid
            captured["publish_sync_weibo"] = sync_weibo
            return {"code": 0, "tid": "fid-video"}

    class _Uploader:
        def __init__(self, **_kwargs):
            raise AssertionError("web cookie video path must not use Tencent upload socket credentials")

    monkeypatch.setattr(daemon_mod, "QzoneTencentVideoUploader", _Uploader)

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
    cover_path = tmp_path / "cover.jpg"
    cover_path.write_bytes(b"jpg")
    monkeypatch.setattr(
        daemon_mod,
        "video_cover_media",
        lambda *_args, **_kwargs: PostMedia(kind="image", source=str(cover_path), name="cover.jpg", trusted_local=True),
    )
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
    assert captured["h5_cover_path"] == cover_path
    assert captured["h5_upload_kwargs"]["play_time"] == 2345
    assert captured["h5_upload_kwargs"]["title"] == "clip"
    assert captured["h5_cover_kwargs"]["vid"] == "vid-h5-noext"
    assert captured["h5_cover_kwargs"]["need_feeds"] == 1
    assert captured["publish_content"] == ""
    assert captured["publish_vid"] == "vid-h5-noext"


def test_daemon_h5_video_publish_polls_feed_even_when_publish_response_has_feedinfo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qzone_bridge import daemon as daemon_mod
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.media import PostMedia

    monkeypatch.delenv("QZONE_VIDEO_UPLOAD_LOGIN_DATA_B64", raising=False)
    monkeypatch.delenv("QZONE_UPLOAD_LOGIN_DATA_B64", raising=False)
    captured: dict[str, object] = {}

    class _Client:
        timeout = 1.5

        async def upload_h5_video(self, *_args, **_kwargs):
            return types.SimpleNamespace(
                vid="vid-h5-feedinfo",
                to_dict=lambda: {"vid": "vid-h5-feedinfo"},
            )

        async def upload_h5_video_cover(self, *_args, **_kwargs):
            return types.SimpleNamespace(to_dict=lambda: {"photo_id": "cover-photo"})

        async def publish_video_mood(self, *_args, **_kwargs):
            return {
                "code": 0,
                "subcode": 0,
                "tid": "fid-feedinfo",
                "feedinfo": (
                    '<li id="fct_3112333596_311_0_1780718770_0_1">'
                    '<div id="feed_3112333596_311_0_1780718770_0_1" data-key="fid-feedinfo">'
                    '<div class="f-ct-video">'
                    '<a data-videotype="mood" data-v_vidiourl="http://user.qzone.qq.com/3112333596/qzvideo/vid-h5-feedinfo">'
                    '<img src="http://shp.qpic.cn/qqvideo/0/vid-h5-feedinfo/400" />'
                    '</a></div></div></li>'
                ),
            }

    class _Uploader:
        def __init__(self, **_kwargs):
            raise AssertionError("web cookie video path must not use Tencent upload socket credentials")

    monkeypatch.setattr(daemon_mod, "QzoneTencentVideoUploader", _Uploader)

    service = object.__new__(QzoneDaemonService)
    service.store = types.SimpleNamespace(root=tmp_path)
    service.state = types.SimpleNamespace(session=SessionState(uin=3112333596, cookies={"p_skey": "ps-key"}))
    service.client = _Client()
    service._ensure_session_ready = lambda: None
    service._set_success = lambda defer_save=True: None

    async def fake_wait_for_native_video_feed(**kwargs):
        captured["verify_kwargs"] = kwargs
        return {
            "fid": "fid-feedinfo",
            "hostuin": 3112333596,
            "appid": 311,
            "verification_source": "publishmood_rsp_detail",
            "raw": {"vid": "vid-h5-feedinfo"},
        }

    service._wait_for_native_video_feed = fake_wait_for_native_video_feed

    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"chunk")
    cover_path = tmp_path / "cover.jpg"
    cover_path.write_bytes(b"jpg")
    monkeypatch.setattr(
        daemon_mod,
        "video_cover_media",
        lambda *_args, **_kwargs: PostMedia(kind="image", source=str(cover_path), name="cover.jpg", trusted_local=True),
    )
    video = PostMedia(kind="video", source=str(video_path), name="clip.mp4", mime_type="video/mp4", trusted_local=True)

    payload = asyncio.run(service.publish_post(content="hello", media=[video.to_dict()], content_sanitized=True))

    assert payload["native_video"] is True
    assert payload["vid"] == "vid-h5-feedinfo"
    assert payload["fid"] == "fid-feedinfo"
    assert payload["raw"]["verified_feed"]["verification_source"] == "publishmood_rsp_detail"
    assert payload["raw"]["publish_result"]["feedinfo_present"] is True
    assert "feedinfo" not in payload["raw"]["publish_result"]
    assert captured["verify_kwargs"] == {"vid": "vid-h5-feedinfo", "fid": "fid-feedinfo"}


def test_daemon_h5_video_publish_rejects_mismatched_publish_feedinfo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qzone_bridge import daemon as daemon_mod
    from qzone_bridge.daemon import QzoneDaemonService
    from qzone_bridge.errors import QzoneRequestError
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

        async def upload_h5_video_cover(self, *_args, **_kwargs):
            return types.SimpleNamespace(to_dict=lambda: {"photo_id": "cover-photo"})

        async def publish_video_mood(self, *_args, **_kwargs):
            return {
                "code": 0,
                "tid": "fid-feedinfo",
                "feedinfo": '<li id="fct_3112333596_4_0_1780718770_0_1">vid-h5-feedinfo</li>',
            }

    class _Uploader:
        def __init__(self, **_kwargs):
            raise AssertionError("web cookie video path must not use Tencent upload socket credentials")

    monkeypatch.setattr(daemon_mod, "QzoneTencentVideoUploader", _Uploader)

    service = object.__new__(QzoneDaemonService)
    service.store = types.SimpleNamespace(root=tmp_path)
    service.state = types.SimpleNamespace(session=SessionState(uin=3112333596, cookies={"p_skey": "ps-key"}))
    service.client = _Client()
    service._ensure_session_ready = lambda: None
    service._set_success = lambda defer_save=True: None

    async def fake_wait_for_native_video_feed(**_kwargs):
        return None

    service._wait_for_native_video_feed = fake_wait_for_native_video_feed

    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"chunk")
    cover_path = tmp_path / "cover.jpg"
    cover_path.write_bytes(b"jpg")
    monkeypatch.setattr(
        daemon_mod,
        "video_cover_media",
        lambda *_args, **_kwargs: PostMedia(kind="image", source=str(cover_path), name="cover.jpg", trusted_local=True),
    )
    video = PostMedia(kind="video", source=str(video_path), name="clip.mp4", mime_type="video/mp4", trusted_local=True)

    with pytest.raises(QzoneRequestError) as error:
        asyncio.run(service.publish_post(content="hello", media=[video.to_dict()], content_sanitized=True))

    assert "sVid" in str(error.value)
    assert error.value.detail["publish_result"]["feedinfo_present"] is True
    assert "feedinfo" not in error.value.detail["publish_result"]


def test_daemon_video_verification_rejects_active_album_upload_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qzone_bridge import daemon as daemon_mod
    from qzone_bridge.daemon import QzoneDaemonService

    monkeypatch.setattr(daemon_mod, "NATIVE_VIDEO_VERIFY_RETRY_DELAYS_SECONDS", (0,))
    service = object.__new__(QzoneDaemonService)
    service.state = types.SimpleNamespace(session=SessionState(uin=487231935, cookies={"p_skey": "ps-key"}))

    album_item = {
        "hostuin": 487231935,
        "fid": "album-feed",
        "appid": 4,
        "summary": "上传1个视频到《说说和日志相册》",
        "raw": {"html": "qzvideo/vid-only-in-album-feed"},
    }

    async def fake_list_feeds(*, scope, **_kwargs):
        if scope == "active":
            return {"items": [dict(album_item)]}
        return {"items": []}

    async def fake_detail_feed(**_kwargs):
        return {"entry": dict(album_item), "raw": dict(album_item["raw"])}

    service.list_feeds = fake_list_feeds
    service.detail_feed = fake_detail_feed

    result = asyncio.run(service._wait_for_native_video_feed(vid="vid-only-in-album-feed"))

    assert result is None
    diagnostics = service._last_native_video_verification_diagnostics
    assert diagnostics["result"] == "not_verified"
    assert diagnostics["scopes"]["active"]["appid_counts"] == {"4": 1}
    assert diagnostics["scopes"]["active"]["native_video_candidate_count"] == 0
    assert diagnostics["scopes"]["active"]["svid_hits"] == [
        {
            "fid": "album-feed",
            "appid": 4,
            "hostuin": 487231935,
            "accepted_context": False,
        }
    ]


def test_daemon_video_verification_accepts_profile_mood_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qzone_bridge import daemon as daemon_mod
    from qzone_bridge.daemon import QzoneDaemonService

    monkeypatch.setattr(daemon_mod, "NATIVE_VIDEO_VERIFY_RETRY_DELAYS_SECONDS", (0,))
    service = object.__new__(QzoneDaemonService)
    service.state = types.SimpleNamespace(session=SessionState(uin=487231935, cookies={"p_skey": "ps-key"}))

    mood_item = {
        "hostuin": 487231935,
        "fid": "mood-feed",
        "appid": 311,
        "summary": "real video mood",
        "raw": {"html": "qzvideo/vid-in-visible-mood"},
    }

    async def fake_list_feeds(*, scope, **_kwargs):
        if scope == "profile":
            return {"items": [dict(mood_item)]}
        return {"items": []}

    async def fake_detail_feed(**_kwargs):
        raise AssertionError("profile feed match should not need detail fallback")

    service.list_feeds = fake_list_feeds
    service.detail_feed = fake_detail_feed

    result = asyncio.run(service._wait_for_native_video_feed(vid="vid-in-visible-mood"))

    assert result is not None
    assert result["fid"] == "mood-feed"
    assert result["verification_source"] == "profile_feed"
    assert result["visibility"]["public"] is True


def test_daemon_video_verification_rejects_private_mood_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qzone_bridge import daemon as daemon_mod
    from qzone_bridge.daemon import QzoneDaemonService

    monkeypatch.setattr(daemon_mod, "NATIVE_VIDEO_VERIFY_RETRY_DELAYS_SECONDS", (0,))
    service = object.__new__(QzoneDaemonService)
    service.state = types.SimpleNamespace(session=SessionState(uin=487231935, cookies={"p_skey": "ps-key"}))

    private_item = {
        "hostuin": 487231935,
        "fid": "private-mood-feed",
        "appid": 311,
        "summary": "real video mood",
        "ugc_right": 64,
        "raw": {"html": "qzvideo/vid-private-mood", "title": "仅自己可见"},
    }

    async def fake_list_feeds(*, scope, **_kwargs):
        if scope == "profile":
            return {"items": [dict(private_item)]}
        return {"items": []}

    async def fake_detail_feed(**_kwargs):
        return {"entry": dict(private_item), "raw": dict(private_item["raw"])}

    service.list_feeds = fake_list_feeds
    service.detail_feed = fake_detail_feed

    result = asyncio.run(service._wait_for_native_video_feed(vid="vid-private-mood"))

    assert result is None
    diagnostics = service._last_native_video_verification_diagnostics
    assert diagnostics["result"] == "private_visibility"
    assert diagnostics["private_visibility_hits"]
    assert diagnostics["private_visibility_hits"][0]["private"] is True


def test_daemon_video_verification_rejects_friend_visible_mood_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qzone_bridge import daemon as daemon_mod
    from qzone_bridge.daemon import QzoneDaemonService

    monkeypatch.setattr(daemon_mod, "NATIVE_VIDEO_VERIFY_RETRY_DELAYS_SECONDS", (0,))
    service = object.__new__(QzoneDaemonService)
    service.state = types.SimpleNamespace(session=SessionState(uin=487231935, cookies={"p_skey": "ps-key"}))

    friend_visible_item = {
        "hostuin": 487231935,
        "fid": "friend-visible-mood-feed",
        "appid": 311,
        "summary": "real video mood",
        "ugc_right": 2,
        "raw": {"html": "qzvideo/vid-friend-visible", "title": "好友可见"},
    }

    async def fake_list_feeds(*, scope, **_kwargs):
        if scope == "profile":
            return {"items": [dict(friend_visible_item)]}
        return {"items": []}

    async def fake_detail_feed(**_kwargs):
        return {"entry": dict(friend_visible_item), "raw": dict(friend_visible_item["raw"])}

    service.list_feeds = fake_list_feeds
    service.detail_feed = fake_detail_feed

    result = asyncio.run(service._wait_for_native_video_feed(vid="vid-friend-visible"))

    assert result is None
    diagnostics = service._last_native_video_verification_diagnostics
    assert diagnostics["result"] == "non_public_visibility"
    assert diagnostics["non_public_visibility_hits"]
    visibility = diagnostics["non_public_visibility_hits"][0]
    assert visibility["public"] is False
    assert visibility["private"] is False
    assert visibility["non_public"] is True
    assert visibility["visibility_markers"]
