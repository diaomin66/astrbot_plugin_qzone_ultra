"""Shared policy for media source loading."""

from __future__ import annotations

import ipaddress
import re
import socket
from functools import lru_cache
from urllib.parse import urljoin, urlparse

REMOTE_MEDIA_SCHEMES = {"http", "https"}
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
UNSAFE_HOST_NAMES = {"localhost", "localhost.localdomain"}
PROXY_FAKE_IP_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
)
TRUSTED_REMOTE_MEDIA_HOST_SUFFIXES = (
    "multimedia.nt.qq.com.cn",
    "qpic.cn",
    "gtimg.cn",
    "qlogo.cn",
    "qzone.qq.com",
    "photo.qq.com",
)


def is_windows_drive_path(source: str) -> bool:
    return bool(WINDOWS_DRIVE_RE.match(str(source or "")))


def is_remote_media_url_allowed(source: str) -> bool:
    parsed = urlparse(str(source or "").strip())
    if parsed.scheme.lower() not in REMOTE_MEDIA_SCHEMES or not parsed.netloc:
        return False
    host = parsed.hostname
    if not host:
        return False
    return not is_unsafe_media_host(host) and remote_media_host_resolves_safely(host)


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


def is_trusted_remote_media_host(host: str) -> bool:
    normalized = str(host or "").strip().lower().rstrip(".")
    if not normalized or is_unsafe_media_host(normalized):
        return False
    return any(normalized == suffix or normalized.endswith(f".{suffix}") for suffix in TRUSTED_REMOTE_MEDIA_HOST_SUFFIXES)


def is_proxy_fake_ip_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(str(value or "").strip().strip("[]"))
    except ValueError:
        return False
    return any(address in network for network in PROXY_FAKE_IP_NETWORKS)


@lru_cache(maxsize=512)
def remote_media_host_resolves_safely(host: str) -> bool:
    normalized = str(host or "").strip().lower().rstrip(".")
    if is_unsafe_media_host(normalized):
        return False
    try:
        ipaddress.ip_address(normalized.strip("[]"))
    except ValueError:
        pass
    else:
        return True

    try:
        infos = socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addresses = {item[4][0] for item in infos if item and item[4]}
    if not addresses:
        return False
    if is_trusted_remote_media_host(normalized) and all(is_proxy_fake_ip_address(address) for address in addresses):
        return True
    return not any(is_unsafe_media_host(address) for address in addresses)


def resolve_remote_media_redirect(base_url: str, location: str) -> str:
    if not location:
        return ""
    resolved = urljoin(base_url, location)
    return resolved if is_remote_media_url_allowed(resolved) else ""
