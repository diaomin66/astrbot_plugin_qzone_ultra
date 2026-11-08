"""Shared policy for media source loading."""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
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
DNS_EXEMPT_TRUSTED_REMOTE_MEDIA_HOSTS = {
    "multimedia.nt.qq.com.cn",
}
DNS_EXEMPT_TRUSTED_REMOTE_MEDIA_PATH_PREFIXES = {
    "multimedia.nt.qq.com.cn": ("/download",),
}


@dataclass(frozen=True, slots=True)
class RemoteMediaUrlCheck:
    allowed: bool
    code: str
    host: str


def is_windows_drive_path(source: str) -> bool:
    return bool(WINDOWS_DRIVE_RE.match(str(source or "")))


def is_remote_media_url_allowed(source: str) -> bool:
    return check_remote_media_url(source).allowed


def check_remote_media_url(source: str) -> RemoteMediaUrlCheck:
    """Validate a remote-media URL once and return safe, fixed diagnostics."""
    try:
        parsed = urlparse(str(source or "").strip())
        scheme = parsed.scheme.lower()
        host = str(parsed.hostname or "").strip().lower().rstrip(".")
    except (TypeError, ValueError, UnicodeError):
        return RemoteMediaUrlCheck(False, "INVALID_URL", "")
    if scheme not in REMOTE_MEDIA_SCHEMES or not parsed.netloc or not host:
        return RemoteMediaUrlCheck(False, "INVALID_URL", host)
    try:
        port = parsed.port
    except ValueError:
        return RemoteMediaUrlCheck(False, "INVALID_URL", host)
    if port is not None and not 0 <= port <= 65535:
        return RemoteMediaUrlCheck(False, "INVALID_URL", host)
    if is_unsafe_media_host(host):
        return RemoteMediaUrlCheck(False, "UNSAFE_HOST", host)
    try:
        if is_dns_exempt_trusted_remote_media_url(source):
            return RemoteMediaUrlCheck(True, "ALLOWED", host)
    except (TypeError, ValueError, UnicodeError):
        return RemoteMediaUrlCheck(False, "INVALID_URL", host)
    code = _remote_media_host_check(host)
    return RemoteMediaUrlCheck(code == "ALLOWED", code, host)


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


def is_dns_exempt_trusted_remote_media_url(source: str) -> bool:
    """Return whether a QQ first-party media URL may bypass local DNS safety checks.

    Some QQ/QQNT media downloads are served from ``multimedia.nt.qq.com.cn`` but
    resolve to RFC 2544 fake/proxy addresses, and in some proxy/DNS setups can
    also appear as non-public addresses. The URL still represents a Tencent
    HTTPS media origin, so allow only the exact host and download path here
    rather than treating the temporary resolver answer as an unsafe user URL.
    """

    parsed = urlparse(str(source or "").strip())
    if parsed.scheme.lower() != "https":
        return False
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    if host not in DNS_EXEMPT_TRUSTED_REMOTE_MEDIA_HOSTS:
        return False
    if is_unsafe_media_host(host):
        return False
    path = parsed.path or "/"
    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in DNS_EXEMPT_TRUSTED_REMOTE_MEDIA_PATH_PREFIXES.get(host, ())
    )


def is_proxy_fake_ip_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(str(value or "").strip().strip("[]"))
    except ValueError:
        return False
    return any(address in network for network in PROXY_FAKE_IP_NETWORKS)


def _remote_media_addresses_are_safe_for_host(host: str, addresses: set[str]) -> bool:
    if not addresses:
        return False
    if is_trusted_remote_media_host(host):
        return all(
            is_proxy_fake_ip_address(address) or not is_unsafe_media_host(address)
            for address in addresses
        )
    return not any(is_unsafe_media_host(address) for address in addresses)


def _remote_media_host_check(host: str) -> str:
    normalized = str(host or "").strip().lower().rstrip(".")
    if is_unsafe_media_host(normalized):
        return "UNSAFE_HOST"
    try:
        ipaddress.ip_address(normalized.strip("[]"))
    except ValueError:
        pass
    else:
        return "ALLOWED"

    try:
        infos = socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
    except Exception:
        return "DNS_LOOKUP_FAILED"
    addresses: set[str] = set()
    for item in infos or ():
        try:
            address = item[4][0]
        except (IndexError, KeyError, TypeError):
            continue
        if address:
            addresses.add(str(address))
    if not addresses:
        return "DNS_LOOKUP_FAILED"
    return "ALLOWED" if _remote_media_addresses_are_safe_for_host(normalized, addresses) else "UNSAFE_DNS"


def remote_media_host_resolves_safely(host: str) -> bool:
    """Compatibility predicate; intentionally performs a fresh DNS lookup."""
    return _remote_media_host_check(host) == "ALLOWED"


def check_remote_media_redirect(base_url: str, location: str) -> tuple[str, RemoteMediaUrlCheck]:
    if not location:
        return "", RemoteMediaUrlCheck(False, "INVALID_URL", "")
    try:
        resolved = urljoin(base_url, location)
    except (TypeError, ValueError, UnicodeError):
        return "", RemoteMediaUrlCheck(False, "INVALID_URL", "")
    result = check_remote_media_url(resolved)
    return (resolved if result.allowed else ""), result


def resolve_remote_media_redirect(base_url: str, location: str) -> str:
    resolved, _result = check_remote_media_redirect(base_url, location)
    return resolved
