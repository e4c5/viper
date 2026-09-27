"""SSRF guard for the configured SCM base URL.

Enforcement happens at provider construction (orchestrator), not in a pydantic
validator, because it performs DNS resolution.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Hostnames that must never receive an SCM token, whatever the allowlist says.
_METADATA_HOSTNAMES = frozenset(
    {"metadata.google.internal", "metadata", "instance-data"}
)

# Link-local and cloud metadata endpoints (169.254.169.254, fd00:ec2::254).
_BLOCKED_NETWORKS_ALWAYS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::254/128"),
)

# Only applied when SCM_BLOCK_PRIVATE_HOSTS=true — self-hosted SCMs routinely
# live on private networks, so this is opt-in.
_BLOCKED_NETWORKS_PRIVATE = (
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
)


def _host_allowed(host: str, port: int | None, allowed_hosts: str) -> bool:
    """Match host[:port] against the comma-separated allowlist.

    An entry without a port matches any port; an entry starting with '.'
    matches subdomains (e.g. '.example.com' allows 'git.example.com' but not
    'example.com' itself). Matching is case-insensitive.
    """
    host_l = host.lower()
    for raw_entry in allowed_hosts.split(","):
        entry = raw_entry.strip().lower()
        if not entry:
            continue
        if entry.startswith("."):
            if host_l.endswith(entry):
                return True
            continue
        if ":" in entry:
            entry_host, _, entry_port = entry.rpartition(":")
            if entry_host == host_l and port is not None and str(port) == entry_port:
                return True
            continue
        if entry == host_l:
            return True
    return False


def _resolve_host_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve host to IP addresses; empty list when resolution fails."""
    try:
        literal = ipaddress.ip_address(host)
        return [literal]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        # Resolution failure is not a rejection — the request will fail later
        # anyway with a clearer network error.
        logger.debug("could not resolve SCM host %r for policy check: %s", host, exc)
        return []
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        try:
            ips.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    return ips


def validate_scm_base_url(
    url: str, *, allowed_hosts: str | None, block_private: bool
) -> None:
    """Raise ValueError when the SCM base URL fails the egress policy."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port
    if not host:
        raise ValueError(f"SCM_URL {url!r} has no host")

    if isinstance(allowed_hosts, str) and allowed_hosts.strip():
        if not _host_allowed(host, port, allowed_hosts):
            raise ValueError(
                f"SCM_URL host {host!r} (port {port}) is not in "
                f"SCM_ALLOWED_HOSTS ({allowed_hosts!r})"
            )

    if host.lower() in _METADATA_HOSTNAMES:
        raise ValueError(
            f"SCM_URL host {host!r} is a cloud metadata endpoint and is never allowed"
        )

    for ip in _resolve_host_ips(host):
        for network in _BLOCKED_NETWORKS_ALWAYS:
            if ip in network:
                raise ValueError(
                    f"SCM_URL host {host!r} resolves to link-local/metadata "
                    f"address {ip}; refusing to send SCM credentials there"
                )
        if block_private is True:
            if ip in _BLOCKED_NETWORKS_PRIVATE[0] or (
                getattr(ip, "is_loopback", False)
                or getattr(ip, "is_private", False)
                or getattr(ip, "is_unspecified", False)
                or getattr(ip, "is_reserved", False)
                or getattr(ip, "is_multicast", False)
            ):
                raise ValueError(
                    f"SCM_URL host {host!r} resolves to private/reserved "
                    f"address {ip} and SCM_BLOCK_PRIVATE_HOSTS is enabled"
                )
