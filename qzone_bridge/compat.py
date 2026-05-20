"""Compatibility helpers for AstrBot hot-reload and half-updated plugin trees."""

from __future__ import annotations

import math
import os
import re
import time
import uuid
from html import unescape
from pathlib import Path
from typing import Any


NICKNAME_KEYS = (
    "nickname",
    "nickName",
    "nick_name",
    "nick",
    "name",
    "uinname",
    "userName",
    "username",
    "ownerName",
    "displayName",
)
NICKNAME_CONTAINER_KEYS = (
    "userinfo",
    "userInfo",
    "user",
    "owner",
    "author",
    "poster",
    "host",
    "profile",
    "blogInfo",
    "cell_userinfo",
)
USER_ID_KEYS = ("uin", "hostuin", "hostUin", "user_id", "userId", "qq", "uinnum")
NESTED_NICKNAME_PATHS = (
    ("data", "userinfo"),
    ("data", "userInfo"),
    ("data", "user"),
    ("data", "owner"),
    ("data", "feed", "userinfo"),
    ("feed", "userinfo"),
    ("feed", "user"),
    ("entry", "userinfo"),
    ("entry", "user"),
)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default


def clean_nickname(value: Any, *, hostuin: int = 0) -> str:
    text = unescape(re.sub(r"<[^>]+>", "", re.sub(r"\[em\].*?\[/em\]", "", str(value or "")))).strip()
    if not text:
        return ""
    if hostuin and text == str(hostuin):
        return ""
    if re.fullmatch(r"\d{5,}", text):
        return ""
    return text


def _mapping_uin(raw: dict[str, Any]) -> int:
    for key in USER_ID_KEYS:
        value = raw.get(key)
        if value not in (None, ""):
            return _to_int(value)
    return 0


def _owner_matches(raw: dict[str, Any], *, hostuin: int = 0) -> bool:
    owner_uin = _mapping_uin(raw)
    return not hostuin or not owner_uin or owner_uin == hostuin


def _iter_mappings(value: Any):
    if isinstance(value, dict):
        yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item


def _nested_mapping(raw: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = raw
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _first_nickname(raw: dict[str, Any], *, hostuin: int = 0) -> str:
    if not _owner_matches(raw, hostuin=hostuin):
        return ""
    for key in NICKNAME_KEYS:
        nickname = clean_nickname(raw.get(key), hostuin=hostuin)
        if nickname:
            return nickname
    return ""


def fallback_extract_nickname(raw: dict[str, Any] | None, *, hostuin: int = 0) -> str:
    if not isinstance(raw, dict):
        return ""
    for key in NICKNAME_CONTAINER_KEYS:
        for item in _iter_mappings(raw.get(key)):
            nickname = _first_nickname(item, hostuin=hostuin)
            if nickname:
                return nickname
    for path in NESTED_NICKNAME_PATHS:
        nickname = _first_nickname(_nested_mapping(raw, *path), hostuin=hostuin)
        if nickname:
            return nickname
    return _first_nickname(raw, hostuin=hostuin)


def extract_nickname_compat(raw: dict[str, Any] | None, *, hostuin: int = 0, social_module: Any = None) -> str:
    extractor = getattr(social_module, "extract_nickname", None)
    if callable(extractor):
        try:
            nickname = str(extractor(raw, hostuin=hostuin) or "").strip()
        except Exception:
            nickname = ""
        if clean_nickname(nickname, hostuin=hostuin):
            return nickname
    return fallback_extract_nickname(raw, hostuin=hostuin)


def selection_has_explicit_input(selection: Any) -> bool:
    try:
        return bool(getattr(selection, "has_explicit_input"))
    except Exception:
        pass
    for attribute in ("explicit_target", "explicit_selector", "explicit_comment_text"):
        try:
            if bool(getattr(selection, attribute, False)):
                return True
        except Exception:
            pass
    for attribute in ("fid", "comment_text", "target_uin"):
        try:
            if bool(getattr(selection, attribute, "")):
                return True
        except Exception:
            pass
    try:
        selector = str(getattr(selection, "selector", "") or "").strip().lower()
        if selector and selector != "latest":
            return True
    except Exception:
        pass
    try:
        return int(getattr(selection, "start", 1) or 1) != 1 or int(getattr(selection, "end", 1) or 1) != 1
    except Exception:
        return False


def _resample_filter(renderer_module: Any, image_module: Any) -> Any:
    resample = getattr(renderer_module, "QUALITY_RESAMPLE", None)
    if resample is not None:
        return resample
    resampling = getattr(image_module, "Resampling", image_module)
    return getattr(resampling, "LANCZOS", getattr(image_module, "LANCZOS", 1))


def _prune_output_dir(
    renderer_module: Any,
    output_dir: Path,
    *,
    keep: int = 128,
    max_age_seconds: int = 3 * 24 * 3600,
) -> None:
    prune = getattr(renderer_module, "_prune_output_dir", None)
    if callable(prune):
        try:
            prune(output_dir)
            return
        except Exception:
            pass
    try:
        files = sorted(
            [path for path in output_dir.glob("publish_result_*.png") if path.is_file()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    cutoff = time.time() - max_age_seconds
    for index, path in enumerate(files):
        try:
            if index >= keep or path.stat().st_mtime < cutoff:
                os.remove(path)
        except OSError:
            continue


def fallback_combine_rendered_post_cards(
    paths: list[Path],
    output_dir: Path,
    *,
    renderer_module: Any = None,
) -> Path | None:
    if not paths:
        return None
    if len(paths) == 1:
        return paths[0]
    try:
        from PIL import Image, UnidentifiedImageError
    except Exception:
        return None

    images: list[Any] = []
    try:
        for path in paths:
            try:
                with Image.open(path) as opened:
                    images.append(opened.convert("RGB").copy())
            except (OSError, UnidentifiedImageError):
                return None
        if not images:
            return None

        width = max(image.width for image in images)
        gap = max(12, min(32, width // 40))
        height = sum(image.height for image in images) + gap * (len(images) - 1)
        pixel_count = width * height
        scale = 1.0
        max_height = int(getattr(renderer_module, "COMBINED_CARD_MAX_HEIGHT", 12000) or 12000)
        max_pixels = int(getattr(renderer_module, "COMBINED_CARD_MAX_PIXELS", 30_000_000) or 30_000_000)
        if max_height > 0 and height > max_height:
            scale = min(scale, max_height / height)
        if max_pixels > 0 and pixel_count > max_pixels:
            scale = min(scale, math.sqrt(max_pixels / pixel_count))
        if scale < 1.0:
            resized: list[Any] = []
            resample = _resample_filter(renderer_module, Image)
            for image in images:
                target_size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
                resized.append(image.resize(target_size, resample))
                image.close()
            images = resized
            width = max(image.width for image in images)
            gap = max(8, int(gap * scale))
            height = sum(image.height for image in images) + gap * (len(images) - 1)

        canvas = Image.new("RGB", (width, height), (255, 255, 255))
        y = 0
        for image in images:
            canvas.paste(image, (0, y))
            y += image.height + gap

        output_dir.mkdir(parents=True, exist_ok=True)
        _prune_output_dir(renderer_module, output_dir)
        output_path = output_dir / f"publish_result_{int(time.time())}_{uuid.uuid4().hex[:10]}_cards.png"
        canvas.save(output_path, "PNG", optimize=False, compress_level=1)
        canvas.close()
        return output_path
    finally:
        for image in images:
            try:
                image.close()
            except Exception:
                pass


def combine_rendered_post_cards_compat(
    paths: list[Path],
    output_dir: Path,
    *,
    renderer_module: Any = None,
) -> Path | None:
    combiner = getattr(renderer_module, "combine_rendered_post_cards", None)
    if callable(combiner):
        return combiner(paths, output_dir)
    return fallback_combine_rendered_post_cards(paths, output_dir, renderer_module=renderer_module)
