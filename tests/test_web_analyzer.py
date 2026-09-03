"""Tests for scraper/web_analyzer.py — no network.

socket.getaddrinfo and requests.get/head are patched so every test runs
without touching the network. The SSRF cases (_is_public_host) are treated
as security-critical: they cover every private/reserved range because they
will not be verified manually in production.
"""

import socket
from unittest.mock import MagicMock, patch

import pytest
import requests

from bs4 import BeautifulSoup

from scraper.cookie_detection import detect_cookie_compliance, detect_legal_pages
from scraper.web_analyzer import (
    MAX_RESPONSE_BYTES,
    _best_email,
    _fetch,
    _host_ok,
    _is_public_host,
    _url_exists,
)


# ── Test helpers ──────────────────────────────────────────────────────────────

def _addr(ip: str) -> list:
    """Return a getaddrinfo-shaped list for a single IP address."""
    if ":" in ip:  # IPv6
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 0, 0, 0))]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


def _mock_get(
    status: int = 200,
    url: str = "https://example.com",
    content_type: str = "text/html; charset=utf-8",
    body: bytes = b"<html><body>Test</body></html>",
) -> MagicMock:
    """Build a requests.get mock response."""
    resp = MagicMock()
    resp.status_code = status
    resp.url = url
    resp.headers = {"Content-Type": content_type}
    resp.raw.read.return_value = body
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    return resp


PUBLIC = _addr("93.184.216.34")   # example.com — globally routable
PRIVATE = _addr("192.168.1.1")    # RFC 1918


# ── _is_public_host (SSRF guard — mandatory coverage) ─────────────────────────

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
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=_addr(ip)):
            assert _is_public_host("target") is False

    def test_public_ipv4_accepted(self):
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=PUBLIC):
            assert _is_public_host("example.com") is True

    def test_dns_failure_returns_false(self):
        with patch("scraper.web_analyzer.socket.getaddrinfo", side_effect=socket.gaierror):
            assert _is_public_host("nonexistent.invalid") is False

    def test_empty_getaddrinfo_result_returns_false(self):
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=[]):
            assert _is_public_host("example.com") is False

    def test_any_private_address_in_multi_record_rejects(self):
        # All resolved IPs must be public — one private one is enough to reject
        mixed = PUBLIC + PRIVATE
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=mixed):
            assert _is_public_host("example.com") is False


# ── _host_ok ──────────────────────────────────────────────────────────────────

class TestHostOk:
    def test_rejects_url_with_private_host(self):
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=PRIVATE):
            assert _host_ok("https://192.168.1.1/path") is False

    def test_accepts_url_with_public_host(self):
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=PUBLIC):
            assert _host_ok("https://example.com/path") is True

    def test_rejects_url_with_no_hostname(self):
        assert _host_ok("not-a-url") is False


# ── _fetch ────────────────────────────────────────────────────────────────────

class TestFetch:
    def test_rejects_private_host_before_connecting(self):
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=PRIVATE), \
             patch("scraper.web_analyzer.requests.get") as mock_get:
            result = _fetch("https://192.168.1.1/")
        assert result is None
        mock_get.assert_not_called()

    def test_happy_path_returns_four_tuple(self):
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=PUBLIC), \
             patch("scraper.web_analyzer.requests.get", return_value=_mock_get()):
            result = _fetch("https://example.com")
        assert result is not None
        html, soup, final_url, invalid_ssl = result
        assert "Test" in html
        assert soup is not None
        assert final_url == "https://example.com"
        assert invalid_ssl is False

    def test_rejects_open_redirect_to_private_host(self):
        # Initial URL is public but redirect lands on a private IP
        resp = _mock_get(url="http://192.168.1.1/")
        with patch("scraper.web_analyzer.socket.getaddrinfo", side_effect=[PUBLIC, PRIVATE]), \
             patch("scraper.web_analyzer.requests.get", return_value=resp):
            assert _fetch("https://example.com") is None

    def test_rejects_non_html_content_type(self):
        resp = _mock_get(content_type="application/json")
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=PUBLIC), \
             patch("scraper.web_analyzer.requests.get", return_value=resp):
            assert _fetch("https://example.com") is None

    def test_returns_none_on_5xx(self):
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=PUBLIC), \
             patch("scraper.web_analyzer.requests.get", return_value=_mock_get(status=500)):
            assert _fetch("https://example.com") is None

    def test_returns_none_on_connection_timeout(self):
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=PUBLIC), \
             patch("scraper.web_analyzer.requests.get", side_effect=requests.Timeout):
            assert _fetch("https://example.com") is None

    def test_caps_response_at_max_bytes(self):
        resp = _mock_get()
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=PUBLIC), \
             patch("scraper.web_analyzer.requests.get", return_value=resp):
            _fetch("https://example.com")
        resp.raw.read.assert_called_once_with(MAX_RESPONSE_BYTES, decode_content=True)

    def test_ssl_error_sets_invalid_ssl_and_retries(self):
        good_resp = _mock_get()
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=PUBLIC), \
             patch("scraper.web_analyzer.requests.get", side_effect=[
                 requests.exceptions.SSLError, good_resp
             ]):
            result = _fetch("https://example.com")
        assert result is not None
        _, _, _, invalid_ssl = result
        assert invalid_ssl is True

    def test_ssl_error_still_returns_html(self):
        body = b"<html><body>SSL fail site</body></html>"
        good_resp = _mock_get(body=body)
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=PUBLIC), \
             patch("scraper.web_analyzer.requests.get", side_effect=[
                 requests.exceptions.SSLError, good_resp
             ]):
            result = _fetch("https://example.com")
        assert result is not None
        html, _, _, _ = result
        assert "SSL fail site" in html


# ── _url_exists ───────────────────────────────────────────────────────────────

class TestUrlExists:
    def test_rejects_private_host_before_connecting(self):
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=PRIVATE), \
             patch("scraper.web_analyzer.requests.head") as mock_head:
            assert _url_exists("https://192.168.1.1/sitemap.xml") is False
        mock_head.assert_not_called()

    def test_returns_true_on_200(self):
        resp = MagicMock()
        resp.status_code = 200
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=PUBLIC), \
             patch("scraper.web_analyzer.requests.head", return_value=resp):
            assert _url_exists("https://example.com/sitemap.xml") is True

    def test_returns_false_on_404(self):
        resp = MagicMock()
        resp.status_code = 404
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=PUBLIC), \
             patch("scraper.web_analyzer.requests.head", return_value=resp):
            assert _url_exists("https://example.com/sitemap.xml") is False

    def test_returns_false_on_timeout(self):
        with patch("scraper.web_analyzer.socket.getaddrinfo", return_value=PUBLIC), \
             patch("scraper.web_analyzer.requests.head", side_effect=requests.Timeout):
            assert _url_exists("https://example.com/sitemap.xml") is False


# ── _best_email ───────────────────────────────────────────────────────────────

class TestBestEmail:
    def test_prefers_site_domain_over_agency(self):
        emails = ["agency@designstudio.com", "info@miempresa.es"]
        assert _best_email(emails, "miempresa.es") == "info@miempresa.es"

    def test_prefers_subdomain_of_site_domain(self):
        emails = ["agency@other.com", "info@mail.miempresa.es"]
        assert _best_email(emails, "miempresa.es") == "info@mail.miempresa.es"

    def test_falls_back_to_first_clean_when_no_domain_match(self):
        emails = ["agency@designstudio.com", "contact@otherdomain.com"]
        result = _best_email(emails, "miempresa.es")
        assert result in ("agency@designstudio.com", "contact@otherdomain.com")

    def test_filters_known_noise_domains(self):
        emails = ["errors@sentry.io", "hello@wixpress.com", "info@miempresa.es"]
        assert _best_email(emails, "miempresa.es") == "info@miempresa.es"

    def test_filters_fake_tld_image_misparsed_as_email(self):
        emails = ["hero@2x.png", "icon@logo.svg", "contact@miempresa.es"]
        assert _best_email(emails, "miempresa.es") == "contact@miempresa.es"

    def test_filters_example_prefix(self):
        emails = ["example@miempresa.es", "contact@miempresa.es"]
        assert _best_email(emails, "miempresa.es") == "contact@miempresa.es"

    def test_returns_empty_when_all_noise(self):
        assert _best_email(["errors@sentry.io", "hero@2x.png"], "miempresa.es") == ""

    def test_empty_list_returns_empty(self):
        assert _best_email([], "miempresa.es") == ""


# ── detect_cookie_compliance ──────────────────────────────────────────────────

def _parse(html: str):
    return BeautifulSoup(html, "html.parser")


# A consent-gated tracker. Every CMP fixture is paired with one, because without
# a tracker there is no consent obligation and the assertion would hold even if
# CMP detection were broken.
_TRACKER = '<script src="https://www.googletagmanager.com/gtag/js?id=G-X"></script>'


class TestDetectCookieCompliance:
    @pytest.mark.parametrize("cmp_html", [
        '<script src="https://consent.cookiebot.com/uc.js"></script>',
        '<script src="/wp-content/plugins/complianz-gdpr/cookiebanner/js/complianz.min.js"></script>',
        '<div id="cmplz-cookiebanner-container"></div>',
        '<div class="cky-consent-container"></div>',
        '<script type="text/plain" data-cookieconsent="statistics"></script>',
        '<div id="segurseo-cookie-banner"></div>',
    ])
    def test_detects_common_cmps(self, cmp_html):
        html = _TRACKER + cmp_html
        assert "no_cookie_banner" not in detect_cookie_compliance(html, _parse(html))

    def test_trackers_without_banner_is_an_issue(self):
        assert "no_cookie_banner" in detect_cookie_compliance(_TRACKER, _parse(_TRACKER))

    def test_no_banner_and_no_trackers_is_not_an_issue(self):
        # A site with only technical cookies has no consent obligation. Reporting it
        # would tell a compliant business it is breaking the law.
        html = '<html><body><h1>Despacho</h1><a href="/politica-cookies">Cookies</a></body></html>'
        assert "no_cookie_banner" not in detect_cookie_compliance(html, _parse(html))

    def test_cookieless_analytics_does_not_require_a_banner(self):
        html = '<script src="https://plausible.io/js/script.js"></script>'
        assert "no_cookie_banner" not in detect_cookie_compliance(html, _parse(html))

    def test_hand_rolled_banner_is_recognised(self):
        # The real-world case that started this: a custom banner with no CMP behind it.
        html = ('<script src="https://www.google-analytics.com/analytics.js"></script>'
                '<div class="aviso-cookies-bar">Solo usamos cookies necesarias '
                '<button>Aceptar</button></div>')
        assert "no_cookie_banner" not in detect_cookie_compliance(html, _parse(html))

    def test_unrelated_cookie_word_is_not_consent_ui(self):
        # A bakery's ".cookie-recipe" must not read as a banner: the generic matcher
        # needs a banner-ish word in the same token, which this lacks.
        html = _TRACKER + '<div class="cookie-recipe">Receta de galletas</div>'
        assert "no_cookie_banner" in detect_cookie_compliance(html, _parse(html))

    def test_detects_cookie_policy_link_by_text(self):
        html = '<a href="/legal">Política de cookies</a>'
        assert "no_cookie_policy" not in detect_cookie_compliance(html, _parse(html))

    def test_detects_cookie_policy_link_by_href(self):
        html = '<a href="/politica-cookies">Aviso legal</a>'
        assert "no_cookie_policy" not in detect_cookie_compliance(html, _parse(html))

    def test_reports_missing_policy_on_bare_page(self):
        html = "<html><body><h1>Inicio</h1></body></html>"
        assert "no_cookie_policy" in detect_cookie_compliance(html, _parse(html))

    @pytest.mark.parametrize("href", ["#", "#aceptar", "javascript:void(0)", "mailto:info@x.es", ""])
    def test_banner_own_controls_do_not_count_as_a_policy(self, href):
        # A consent banner renders "Aceptar cookies" as an anchor; counting it would
        # report a policy page that does not exist.
        html = f'<div class="cookie-banner"><a href="{href}">Aceptar cookies</a></div>'
        assert "no_cookie_policy" in detect_cookie_compliance(html, _parse(html))

    def test_navigable_policy_link_still_counts(self):
        html = '<div class="cookie-banner"><a href="#">Aceptar cookies</a>' \
               '<a href="/politica-de-cookies">Más información</a></div>'
        assert "no_cookie_policy" not in detect_cookie_compliance(html, _parse(html))

    def test_complianz_type_text_plain_with_data_cmplz_attribute(self):
        # Complianz blocks third-party scripts by rewriting their type to text/plain
        # and adding data-cmplz-* attributes — a strong signal even without the CDN URL.
        html = _TRACKER + '<script type="text/plain" data-cmplz-src="https://example.com/t.js"></script>'
        assert "no_cookie_banner" not in detect_cookie_compliance(html, _parse(html))

    def test_no_false_positive_on_realistic_wordpress_homepage(self):
        # Representative HTML of a WordPress site with Complianz active.
        # Ensures a legitimate compliant site is not flagged.
        html = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Mi Empresa</title>
<link rel="stylesheet" href="/wp-content/plugins/complianz-gdpr/cookiebanner/css/cookiebanner.min.css">
<script src="https://www.googletagmanager.com/gtag/js?id=G-ABC123"></script>
</head>
<body>
<nav><a href="/politica-cookies">Política de cookies</a></nav>
<div id="cmplz-cookiebanner-container" class="cmplz-cookiebanner"></div>
<h1>Bienvenidos</h1>
</body>
</html>"""
        soup = _parse(html)
        issues = detect_cookie_compliance(html, soup)
        assert issues == [], f"False positive on compliant site: {issues}"


# ── detect_legal_pages ────────────────────────────────────────────────────────

class TestDetectLegalPages:
    def test_both_pages_linked_reports_nothing(self):
        html = ('<footer><a href="/aviso-legal">Aviso legal</a>'
                '<a href="/politica-de-privacidad">Política de privacidad</a></footer>')
        assert detect_legal_pages(_parse(html)) == []

    def test_bare_page_reports_both(self):
        html = "<html><body><h1>Inicio</h1></body></html>"
        assert set(detect_legal_pages(_parse(html))) == {"no_legal_notice", "no_privacy_policy"}

    @pytest.mark.parametrize("footer", [
        '<a href="/avis-legal">Avís legal</a><a href="/privacitat">Política de privacitat</a>',
        '<a href="/legal">Nota legal</a><a href="/proteccion-datos">Protección de datos</a>',
    ])
    def test_valencian_and_alternate_wordings_are_recognised(self, footer):
        # Bilingual footers are the norm in Elche and the rest of the province;
        # missing these variants would flag a fully compliant site.
        assert detect_legal_pages(_parse(footer)) == []

    def test_matches_on_href_when_link_text_is_generic(self):
        html = '<a href="/aviso-legal">Más información</a><a href="/privacy">Leer</a>'
        assert detect_legal_pages(_parse(html)) == []

    def test_matches_on_text_when_href_is_opaque(self):
        html = '<a href="/p/1">Aviso legal</a><a href="/p/2">Privacidad</a>'
        assert detect_legal_pages(_parse(html)) == []

    def test_only_privacy_present_reports_legal_notice(self):
        html = '<a href="/politica-de-privacidad">Privacidad</a>'
        assert detect_legal_pages(_parse(html)) == ["no_legal_notice"]

    @pytest.mark.parametrize("href", ["#", "javascript:void(0)", "mailto:legal@x.es"])
    def test_non_navigable_links_are_ignored(self, href):
        html = f'<a href="{href}">Aviso legal</a><a href="{href}">Privacidad</a>'
        assert set(detect_legal_pages(_parse(html))) == {"no_legal_notice", "no_privacy_policy"}

    def test_patterns_do_not_match_across_two_separate_links(self):
        # "Aviso" and "Legal" in different links must not combine into "aviso legal";
        # the " | " separator between entries is what prevents it.
        html = '<a href="/a">Aviso</a><a href="/b">Legal</a><a href="/c">Privacidad</a>'
        assert "no_legal_notice" in detect_legal_pages(_parse(html))
