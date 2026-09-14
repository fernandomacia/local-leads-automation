"""SSRF guard shared by every component that fetches a URL for a lead.

Two callers rely on this: the HTTP analyzer and the browser rendering fallback.
It lives on its own because a guard that one module owns privately is a guard
the other one inherits by luck — and the URLs reaching it come from the scraped
page itself, which is untrusted input.
"""

import ipaddress
import socket
from urllib.parse import urlparse


def _is_routable(addr: str) -> bool:
    """True when an address is globally routable.

    Private, loopback, link-local, reserved and multicast ranges are what SSRF
    against cloud metadata endpoints and internal services is built on.
    """
    ip = ipaddress.ip_address(addr)
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast)


def is_public_host(host: str) -> bool:
    """Return True only if every address the host resolves to is globally routable."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return bool(infos) and all(_is_routable(addr) for _, _, _, _, (addr, *_) in infos)


def host_ok(url: str) -> bool:
    """Return True if the URL's host resolves to a public IP.

    Applied to every URL before it is requested, including the ones built from
    hrefs found on the analyzed page: a link on an untrusted page is an
    untrusted URL.
    """
    try:
        host = urlparse(url).hostname
        return bool(host) and is_public_host(host)
    except Exception:
        return False


def is_internal_host(url: str) -> bool:
    """True when the URL's host is known to resolve to a non-routable address.

    Deliberately not the inverse of :func:`host_ok`: a host that does not resolve
    at all is unknown, not internal. The distinction matters wherever rejecting a
    URL turns into a finding — an unresolvable legal-page link has to leave the
    audit undecided, not report the document as never published.
    """
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, ValueError):
        return False
    return any(not _is_routable(addr) for _, _, _, _, (addr, *_) in infos)
