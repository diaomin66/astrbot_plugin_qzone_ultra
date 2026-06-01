"""Native QQ/Qzone video publish handoff via the QQ client protocol."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlencode, urlparse

from .errors import QzoneParseError
from .media import PostMedia, PostPayload, is_supported_image, is_video_media, normalize_source, source_name
from .source_policy import is_windows_drive_path


QZONE_NATIVE_VIDEO_REQ_TYPE = 4
NATIVE_QZONE_APP_NAME = "QQ空间Ultra"
NATIVE_QZONE_SCHEME = "mqqapi"


class NativeVideoPublishUnavailable(QzoneParseError):
    def __init__(self, message: str, *, detail=None):
        super().__init__(message, detail=detail)


@dataclass(slots=True)
class NativeVideoPublishResult:
    uri: str
    video: PostMedia
    handler: str
    message: str = "已唤起 QQ 原生视频发布窗口，请在 QQ 内确认发布。"


def native_video_candidate(post: PostPayload) -> PostMedia | None:
    """Return a single native-publishable video or None for cover fallback."""

    media = [*post.media, *post.attachments]
    videos = [item for item in media if _is_native_video_item(item)]
    if len(videos) != 1:
        return None
    other_publishable_media = [
        item for item in media
        if item is not videos[0] and (is_supported_image(item) or _is_native_video_item(item))
    ]
    if other_publishable_media:
        return None
    return videos[0]


def publish_native_video_post(
    post: PostPayload,
    *,
    app_name: str = NATIVE_QZONE_APP_NAME,
) -> NativeVideoPublishResult:
    video = native_video_candidate(post)
    if video is None:
        raise NativeVideoPublishUnavailable("当前视频组合不适合原生视频发布，已回退到视频封面发布")
    handler = native_qzone_protocol_handler()
    if not handler:
        raise NativeVideoPublishUnavailable("未检测到 QQ 原生 mqqapi 协议，已回退到视频封面发布")
    uri = build_native_qzone_video_publish_uri(video, post.content, app_name=app_name)
    open_native_qzone_uri(uri)
    return NativeVideoPublishResult(uri=uri, video=video, handler=handler)


def build_native_qzone_video_publish_uri(
    video: PostMedia,
    content: str = "",
    *,
    app_name: str = NATIVE_QZONE_APP_NAME,
) -> str:
    path = _trusted_local_video_path(video)
    duration_ms = _probe_video_duration_ms(path)
    size = video.size or _file_size(path)
    query: list[tuple[str, str]] = [
        ("src_type", "app"),
        ("version", "1"),
        ("file_type", "news"),
        ("videoPath", _b64(str(path))),
        ("videoDuration", _b64(str(duration_ms))),
        ("videoSize", _b64(str(size))),
        ("req_type", _b64(str(QZONE_NATIVE_VIDEO_REQ_TYPE))),
    ]
    if str(content or "").strip():
        query.append(("description", _b64(str(content).strip())))
    if app_name:
        query.append(("app_name", _b64(app_name)))
    return f"{NATIVE_QZONE_SCHEME}://qzone/publish?{urlencode(query)}"


def native_qzone_protocol_handler() -> str:
    if sys.platform.startswith("win"):
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{NATIVE_QZONE_SCHEME}\shell\open\command") as key:
                value, _type = winreg.QueryValueEx(key, "")
                return str(value or "").strip()
        except OSError:
            return ""
    if sys.platform == "darwin":
        return shutil.which("open") or ""
    return shutil.which("xdg-open") or ""


def open_native_qzone_uri(uri: str) -> None:
    if sys.platform.startswith("win"):
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
            return
        except OSError as exc:
            raise NativeVideoPublishUnavailable("QQ 原生视频发布入口唤起失败，已回退到视频封面发布") from exc
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    executable = shutil.which(opener)
    if not executable:
        raise NativeVideoPublishUnavailable("未检测到可用的系统协议打开器，已回退到视频封面发布")
    try:
        subprocess.Popen([executable, uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise NativeVideoPublishUnavailable("QQ 原生视频发布入口唤起失败，已回退到视频封面发布") from exc


def _is_native_video_item(item: PostMedia) -> bool:
    return not is_supported_image(item) and (item.kind == "video" or is_video_media(item))


def _trusted_local_video_path(video: PostMedia) -> Path:
    source = normalize_source(video.source)
    parsed = urlparse(source)
    if parsed.scheme.lower() in {"http", "https", "data"} or source.startswith("base64://"):
        raise NativeVideoPublishUnavailable("原生视频发布需要本地视频文件，远程视频已回退到封面发布")
    if parsed.scheme and not source.startswith("file://") and not is_windows_drive_path(source):
        raise NativeVideoPublishUnavailable("视频来源协议不支持原生发布，已回退到封面发布")
    if not video.trusted_local:
        raise NativeVideoPublishUnavailable("本地视频路径只允许来自 AstrBot 消息附件缓存")
    path = Path(source)
    if not path.is_file():
        raise NativeVideoPublishUnavailable(
            "视频文件不存在，无法原生发布",
            detail={"name": video.name or source_name(source)},
        )
    return path


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _probe_video_duration_ms(path: Path) -> int:
    ffprobe = _ffprobe_executable()
    if not ffprobe:
        return 0
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if result.returncode != 0:
        return 0
    try:
        seconds = float(result.stdout.decode("utf-8", "replace").strip())
    except ValueError:
        return 0
    return max(0, int(round(seconds * 1000)))


def _ffprobe_executable() -> str:
    configured = os.environ.get("QZONE_FFPROBE_PATH", "").strip()
    if configured:
        return configured
    found = shutil.which("ffprobe")
    if found:
        return found
    ffmpeg = os.environ.get("QZONE_FFMPEG_PATH", "").strip() or shutil.which("ffmpeg") or ""
    if ffmpeg:
        candidate = Path(ffmpeg).with_name("ffprobe.exe" if sys.platform.startswith("win") else "ffprobe")
        if candidate.is_file():
            return str(candidate)
    return ""
