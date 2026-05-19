"""Shared policy for media source loading."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urljoin, urlparse

REMOTE_MEDIA_SCHEMES = {"http", "https"}
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
UNSAFE_HOST_NAMES = {"localhost", "localhost.localdomain"}


def is_windows_drive_path(source: str) -> bool:
    return bool(WINDOWS_DRIVE_RE.match(str(source or "")))


def is_remote_media_url_allowed(source: str) -> bool:
    parsed = urlparse(str(source or "").strip())
    if parsed.scheme.lower() not in REMOTE_MEDIA_SCHEMES or not parsed.netloc:
        return False
    host = parsed.hostname
    if not host:
        return False
    return not is_unsafe_media_host(host)


def is_unsafe_media_host(host: str) -> bool:
    normalized = str(host or "").strip().lower().rstrip(".")
    if not normalized or normalized in UNSAFE_HOST_NAMES or normalized.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(normalized.strip("[]"))
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def resolve_remote_media_redirect(base_url: str, location: str) -> str:
    if not location:
        return ""
    resolved = urljoin(base_url, location)
    return resolved if is_remote_media_url_allowed(resolved) else ""
