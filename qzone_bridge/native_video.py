"""Daemon-side native QQ/Qzone video selection and metadata helpers.

This module intentionally does not build or open QQ/QQNT client protocol
handoff URIs. Video publishing is handled by the local daemon H5 public-create +
permission-repair + public-verification path; unsupported video inputs must be
rejected instead of falling back to a cover-image publish that would falsely
report video success.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

from .media import (
    PostMedia,
    PostPayload,
    is_supported_image,
    is_video_media,
)


def native_video_candidate(post: PostPayload) -> PostMedia | None:
    """Return the single daemon-native-publishable video, or None if invalid."""

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


def _is_native_video_item(item: PostMedia) -> bool:
    return not is_supported_image(item) and (item.kind == "video" or is_video_media(item))


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
