"""Server-side request forgery guard for admin-supplied upstream URLs.

An admin (or a stolen admin session) can make the gateway connect anywhere. The
guard refuses URLs that resolve to loopback, link-local (cloud metadata lives at
169.254.169.254), multicast or reserved addresses, and refuses private and
cluster-internal addresses unless the host name matches an allow-list of
in-cluster patterns. It runs when a server is saved and again before every
connection, so a DNS record changed after review is caught too. The gateway's
egress NetworkPolicy is the backstop for what a resolver race could slip past.
"""

from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import socket
from urllib.parse import urlsplit

BLOCKED_HOSTNAMES = {"localhost", "metadata", "metadata.google.internal", "metadata.goog", "instance-data", "kubernetes", "kubernetes.default", "kubernetes.default.svc", "kubernetes.default.svc.cluster.local"}
METADATA_ADDRS = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254"), ipaddress.ip_address("100.100.100.200")}


class UnsafeURL(ValueError):
    pass


def _always_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if ip in METADATA_ADDRS:
        return "a cloud metadata endpoint"
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        return "a link-local address"
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return "a non-routable address"
    return None


def _is_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")


async def check_url(url: str, allowed_internal_hosts: list[str], allow_all: bool = False) -> None:
    """Raise UnsafeURL if the gateway must not connect to `url`."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("URL must start with http:// or https://")
    if not parts.hostname:
        raise UnsafeURL("URL has no host")
    if parts.username or parts.password:
        raise UnsafeURL("put credentials in the credential field, not the URL")
    if allow_all:
        return
    host = parts.hostname.lower().rstrip(".")
    if host in BLOCKED_HOSTNAMES:
        raise UnsafeURL(f"host {host!r} is not allowed")
    host_allowed_internal = any(fnmatch.fnmatch(host, pattern) for pattern in allowed_internal_hosts if pattern)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise UnsafeURL(f"host {host!r} does not resolve") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        reason = _always_blocked(ip)
        if reason:
            raise UnsafeURL(f"{host} resolves to {reason} ({ip})")
        if _is_internal(ip) and not host_allowed_internal:
            raise UnsafeURL(f"{host} resolves to a private address ({ip}); only allow-listed in-cluster hosts may")
