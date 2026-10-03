"""Outbound URL validation (SSRF protection for user-supplied provider base URLs)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeUrlError(ValueError):
    pass


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or (
            isinstance(addr, ipaddress.IPv6Address)
            and addr.ipv4_mapped is not None
            and not _is_public(str(addr.ipv4_mapped))
        )
    )


def check_url_shape(url: str, allow_http: bool) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or (parsed.scheme == "http" and not allow_http):
        raise UnsafeUrlError("base URL must use https")
    if not parsed.hostname:
        raise UnsafeUrlError("base URL must include a host")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("base URL must not contain credentials")
    return parsed.hostname


async def assert_public_url(url: str, allow_private: bool = False) -> None:
    """Resolve the host and refuse private/loopback/link-local/metadata addresses.

    Called both when a key is saved and immediately before each outbound call,
    which narrows (but cannot fully close) the DNS-rebinding window.
    """
    host = check_url_shape(url, allow_http=allow_private)
    if allow_private:
        return
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError("base URL host does not resolve") from exc
    for info in infos:
        ip = str(info[4][0])
        if not _is_public(ip):
            raise UnsafeUrlError("base URL resolves to a non-public address")
