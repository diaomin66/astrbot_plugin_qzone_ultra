"""Video cover extraction helpers for publishable QQ Space posts."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import QzoneParseError
from .local_media import is_recoverable_local_media_reference, resolve_trusted_local_media_path
from .media import (
    QZONE_MIN_IMAGE_SIDE,
    QZONE_VIDEO_SUFFIXES,
    PostMedia,
    PostPayload,
    is_supported_image,
    is_video_media,
    normalize_source,
    source_name,
)
from .source_policy import is_windows_drive_path


VIDEO_COVER_MAX_EDGE = 1600
VIDEO_COVER_MIME_TYPE = "image/jpeg"
FFMPEG_TIMEOUT_SECONDS = 30


def materialize_video_covers(post: PostPayload, cover_dir: Path) -> PostPayload:
    """Replace publish-time video media with local JPEG cover images."""

    media, media_changed = materialize_video_cover_list(post.media, cover_dir)
    attachments: list[PostMedia] = []
    attachments_changed = False
    for item in post.attachments:
        if _needs_video_cover(item):
            media.append(video_cover_media(item, cover_dir))
            attachments_changed = True
        else:
            attachments.append(item)
    if not media_changed and not attachments_changed:
        return post
    return PostPayload(content=post.content, media=media, attachments=attachments)


def materialize_video_cover_list(
    media: Iterable[PostMedia],
    cover_dir: Path,
) -> tuple[list[PostMedia], bool]:
    publishable: list[PostMedia] = []
    changed = False
    for item in media:
        if _needs_video_cover(item):
            publishable.append(video_cover_media(item, cover_dir))
            changed = True
            continue
        publishable.append(item)
    return publishable, changed


def video_cover_media(video: PostMedia, cover_dir: Path) -> PostMedia:
    source = normalize_source(video.source)
    path = _trusted_local_video_path(video, source)
    if not path.is_file():
        raise QzoneParseError("视频文件不存在，无法提取封面", detail={"name": video.name or source_name(source)})

    cover_dir.mkdir(parents=True, exist_ok=True)
    cover_path = _cover_path_for_video(path, video, cover_dir)
    if not _valid_cover_file(cover_path):
        temp_path = _temp_cover_path(cover_dir, cover_path.stem)
        try:
            _extract_frame_with_ffmpeg(path, temp_path, name=video.name or path.name)
            _normalize_cover_image(temp_path, cover_path)
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink()

    try:
        size = cover_path.stat().st_size
    except OSError:
        size = 0
    return PostMedia(
        kind="image",
        source=str(cover_path),
        name=f"{_safe_stem(video.name or path.name or 'video')}.jpg",
        mime_type=VIDEO_COVER_MIME_TYPE,
        size=size,
        raw_type="video",
        trusted_local=True,
    )


def _needs_video_cover(item: PostMedia) -> bool:
    return not is_supported_image(item) and (item.kind == "video" or is_video_media(item))


def _trusted_local_video_path(video: PostMedia, source: str) -> Path:
    parsed = urlparse(source)
    if parsed.scheme.lower() in {"http", "https", "data"} or source.startswith("base64://"):
        raise QzoneParseError("暂不支持远程视频直传，请引用本地消息视频后再发说说")
    if (
        parsed.scheme
        and not source.startswith("file://")
        and not is_windows_drive_path(source)
        and not is_recoverable_local_media_reference(source)
    ):
        raise QzoneParseError("视频来源协议不受支持，无法提取封面")
    if not video.trusted_local:
        raise QzoneParseError("本地视频路径只允许来自 AstrBot 消息附件缓存")
    path = resolve_trusted_local_media_path(
        source,
        name=video.name or source_name(source),
        suffixes=QZONE_VIDEO_SUFFIXES,
    )
    if path is None:
        raise QzoneParseError(
            "视频文件不存在，无法提取封面",
            detail={"name": video.name or source_name(source), "source": source},
        )
    return path


def _cover_path_for_video(path: Path, video: PostMedia, cover_dir: Path) -> Path:
    stat = path.stat()
    seed = f"{path.resolve(strict=False)}|{stat.st_mtime_ns}|{stat.st_size}".encode("utf-8", "ignore")
    digest = hashlib.sha1(seed).hexdigest()[:20]
    stem = _safe_stem(video.name or path.name or "video")
    return cover_dir / f"video_cover_{digest}_{stem}.jpg"


def _safe_stem(name: str) -> str:
    stem = Path(str(name or "video")).stem or "video"
    stem = re.sub(r"[^0-9A-Za-z._-]+", "_", stem).strip("._-")
    return stem[:80] or "video"


def _valid_cover_file(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        with Image.open(path) as opened:
            opened.verify()
        return True
    except (OSError, UnidentifiedImageError):
        return False


def _temp_cover_path(cover_dir: Path, stem: str) -> Path:
    handle, name = tempfile.mkstemp(prefix=f"{stem}_", suffix=".jpg", dir=str(cover_dir))
    os.close(handle)
    path = Path(name)
    with contextlib.suppress(OSError):
        path.unlink()
    return path


def _extract_frame_with_ffmpeg(path: Path, output_path: Path, *, name: str = "") -> None:
    ffmpeg = _ffmpeg_executable()
    if not ffmpeg:
        raise QzoneParseError(
            "无法提取视频封面：未找到 ffmpeg，请安装 ffmpeg 或安装 imageio-ffmpeg 依赖",
            detail={"name": name or path.name},
        )

    errors: list[str] = []
    for offset in ("1", "0", "3"):
        if output_path.exists():
            with contextlib.suppress(OSError):
                output_path.unlink()
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            offset,
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-an",
            str(output_path),
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=FFMPEG_TIMEOUT_SECONDS,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
            continue
        if result.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0:
            return
        stderr = result.stderr.decode("utf-8", "replace").strip()
        if stderr:
            errors.append(stderr[-500:])
    raise QzoneParseError(
        "无法提取视频封面，请确认视频文件未损坏且 ffmpeg 可读取该格式",
        detail={"name": name or path.name, "ffmpeg": errors[-1] if errors else ""},
    )


def _ffmpeg_executable() -> str:
    configured = os.environ.get("QZONE_FFMPEG_PATH", "").strip()
    if configured:
        return configured
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return ""


def _normalize_cover_image(source: Path, target: Path) -> None:
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            elif image.mode == "RGBA":
                base = Image.new("RGB", image.size, (255, 255, 255))
                base.paste(image, mask=image.getchannel("A"))
                image = base
            else:
                image = image.copy()
    except (OSError, UnidentifiedImageError) as exc:
        raise QzoneParseError("视频封面不是有效图片，无法发布") from exc

    width, height = image.size
    if width <= 0 or height <= 0:
        raise QzoneParseError("视频封面尺寸无效，无法发布")
    scale = 1.0
    min_side = min(width, height)
    max_side = max(width, height)
    if min_side < QZONE_MIN_IMAGE_SIDE:
        scale = max(scale, QZONE_MIN_IMAGE_SIDE / max(1, min_side))
    if max_side * scale > VIDEO_COVER_MAX_EDGE:
        scale = min(scale, VIDEO_COVER_MAX_EDGE / max(1, max_side))
    if scale != 1.0:
        image = image.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))),
            Image.Resampling.LANCZOS,
        )
    if min(image.size) < QZONE_MIN_IMAGE_SIDE:
        raise QzoneParseError("视频封面尺寸过小，无法发布到 QQ 空间")
    image.save(target, "JPEG", quality=92, optimize=True)
