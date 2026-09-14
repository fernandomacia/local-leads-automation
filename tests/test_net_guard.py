"""Tests for scraper/net_guard.py — the SSRF guard, no network.

socket.getaddrinfo is patched so every test runs offline. Treated as
security-critical: every private/reserved range is covered, because none of this
is verified by hand in production.
"""

import socket
from unittest.mock import patch

import pytest

from scraper.net_guard import host_ok, is_internal_host, is_public_host


def _addr(ip: str) -> list:
    """Return a getaddrinfo-shaped list for a single IP address."""
    if ":" in ip:  # IPv6
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 0, 0, 0))]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


PUBLIC = _addr("93.184.216.34")   # example.com — globally routable
PRIVATE = _addr("192.168.1.1")    # RFC 1918


# ── is_public_host (SSRF guard — mandatory coverage) ─────────────────────────

class TestIsPublicHost:
    @pytest.mark.parametrize("ip", [
        "10.0.0.1",        # RFC 1918 class A
        "172.16.0.1",      # RFC 1918 class B
        "192.168.1.1",     # RFC 1918 class C
        "127.0.0.1",       # IPv4 loopback
        "::1",             # IPv6 loopback
        "169.254.169.254", # AWS/GCP metadata (link-local)
        "0.0.0.0",         # reserved / unspecified
        "224.0.0.1",       # multicast
    ])
    def test_internal_ip_rejected(self, ip):
        with patch("scraper.net_guard.socket.getaddrinfo", return_value=_addr(ip)):
            assert is_public_host("target") is False

    def test_public_ipv4_accepted(self):
        with patch("scraper.net_guard.socket.getaddrinfo", return_value=PUBLIC):
            assert is_public_host("example.com") is True

    def test_dns_failure_returns_false(self):
        with patch("scraper.net_guard.socket.getaddrinfo", side_effect=socket.gaierror):
            assert is_public_host("nonexistent.invalid") is False

    def test_empty_getaddrinfo_result_returns_false(self):
        with patch("scraper.net_guard.socket.getaddrinfo", return_value=[]):
            assert is_public_host("example.com") is False

    def test_any_private_address_in_multi_record_rejects(self):
        # All resolved IPs must be public — one private one is enough to reject
        mixed = PUBLIC + PRIVATE
        with patch("scraper.net_guard.socket.getaddrinfo", return_value=mixed):
            assert is_public_host("example.com") is False


# ── host_ok ──────────────────────────────────────────────────────────────────

class TestHostOk:
    def test_rejects_url_with_private_host(self):
        with patch("scraper.net_guard.socket.getaddrinfo", return_value=PRIVATE):
            assert host_ok("https://192.168.1.1/path") is False

    def test_accepts_url_with_public_host(self):
        with patch("scraper.net_guard.socket.getaddrinfo", return_value=PUBLIC):
            assert host_ok("https://example.com/path") is True

    def test_rejects_url_with_no_hostname(self):
        assert host_ok("not-a-url") is False


# ── is_internal_host ──────────────────────────────────────────────────────────

class TestIsInternalHost:
    """Not the inverse of host_ok: only *known* internal hosts answer True."""

    @pytest.mark.parametrize("ip, host", [
        ("169.254.169.254", "169.254.169.254"),   # cloud metadata
        ("127.0.0.1", "127.0.0.1"),
        ("10.0.0.1", "intranet.example.com"),     # a name pointing inward
        ("::1", "[::1]"),                         # IPv6 literals need brackets in a URL
    ])
    def test_resolved_internal_address_is_internal(self, ip, host):
        with patch("scraper.net_guard.socket.getaddrinfo", return_value=_addr(ip)):
            assert is_internal_host(f"http://{host}/path") is True

    def test_public_address_is_not_internal(self):
        with patch("scraper.net_guard.socket.getaddrinfo", return_value=PUBLIC):
            assert is_internal_host("https://example.com/legal") is False

    def test_unresolvable_host_is_unknown_not_internal(self):
        # The distinction this function exists for: a policy link whose host is
        # down must leave the audit undecided, not report the document missing.
        with patch("scraper.net_guard.socket.getaddrinfo", side_effect=socket.gaierror):
            assert is_internal_host("https://policies.example.org/privacy") is False
        assert host_ok("https://policies.example.org/privacy") is False

    def test_one_internal_address_among_several_is_enough(self):
        with patch("scraper.net_guard.socket.getaddrinfo", return_value=PUBLIC + PRIVATE):
            assert is_internal_host("https://rebind.example.com/") is True

    def test_url_without_a_host_is_not_internal(self):
        assert is_internal_host("/aviso-legal") is False
