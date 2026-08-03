"""Tests for scraper/web_analyzer.py — pure logic, no real network calls."""

from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

from scraper.web_analyzer import (
    _detect_cms,
    _extract_email,
    _identify_platform,
    _score_seo,
    analyze,
)
from tests.conftest import make_soup


# ── CMS detection ─────────────────────────────────────────────────────────────

class TestDetectCms:
    def test_wordpress(self):
        assert _detect_cms('<link href="/wp-content/themes/x/style.css">') == "wordpress"

    def test_wix(self):
        assert _detect_cms('<script src="https://static.wix.com/x.js">') == "wix"

    def test_shopify(self):
        assert _detect_cms('<script src="https://cdn.shopify.com/s/x.js">') == "shopify"

    def test_unknown(self):
        assert _detect_cms("<html><body><p>Hello</p></body></html>") == "unknown"

    def test_first_match_wins(self):
        # wordpress comes before wix in CMS_SIGNATURES — wordpress must win
        html = '/wp-content/x.css static.wix.com'
        assert _detect_cms(html) == "wordpress"


# ── Platform identification ───────────────────────────────────────────────────

class TestIdentifyPlatform:
    def test_instagram(self):
        assert _identify_platform("https://www.instagram.com/mybusiness") == "instagram"

    def test_facebook(self):
        assert _identify_platform("https://facebook.com/mybusiness") == "facebook"

    def test_youtube_subdomain(self):
        assert _identify_platform("https://www.youtube.com/channel/ABC") == "youtube"

    def test_regular_website_returns_none(self):
        assert _identify_platform("https://mybusiness.es") is None

    def test_empty_url_returns_none(self):
        assert _identify_platform("") is None


# ── SEO scoring ───────────────────────────────────────────────────────────────

_FULL_HTML = """
<html lang="es">
<head>
  <title>Mi negocio</title>
  <meta name="description" content="Descripción del negocio">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="canonical" href="https://example.com/">
  <meta property="og:title" content="Mi negocio">
  <script type="application/ld+json">{"@type": "LocalBusiness"}</script>
  <link rel="icon" href="/favicon.ico">
  <script src="https://www.googletagmanager.com/gtag/js?id=G-XXX"></script>
</head>
<body><h1>Bienvenidos</h1><img src="foto.jpg" alt="foto del negocio"></body>
</html>
"""


class TestScoreSeo:
    @patch("scraper.web_analyzer._url_exists", return_value=True)
    def test_perfect_score(self, _):
        score, issues = _score_seo(make_soup(_FULL_HTML), "https://example.com")
        assert score == 100
        assert issues == []

    @patch("scraper.web_analyzer._url_exists", return_value=True)
    def test_no_https_penalized(self, _):
        score, issues = _score_seo(make_soup(_FULL_HTML), "http://example.com")
        assert "no_https" in issues

    @patch("scraper.web_analyzer._url_exists", return_value=True)
    def test_missing_title(self, _):
        html = _FULL_HTML.replace("<title>Mi negocio</title>", "")
        _, issues = _score_seo(make_soup(html), "https://example.com")
        assert "no_title" in issues

    @patch("scraper.web_analyzer._url_exists", return_value=True)
    def test_no_h1(self, _):
        html = _FULL_HTML.replace("<h1>Bienvenidos</h1>", "")
        _, issues = _score_seo(make_soup(html), "https://example.com")
        assert "no_h1" in issues

    @patch("scraper.web_analyzer._url_exists", return_value=True)
    def test_multiple_h1(self, _):
        html = _FULL_HTML.replace("<h1>Bienvenidos</h1>", "<h1>A</h1><h1>B</h1>")
        _, issues = _score_seo(make_soup(html), "https://example.com")
        assert "multiple_h1" in issues
        assert "no_h1" not in issues

    @patch("scraper.web_analyzer._url_exists", return_value=True)
    def test_image_without_alt_penalized(self, _):
        html = _FULL_HTML.replace('alt="foto del negocio"', "")
        _, issues = _score_seo(make_soup(html), "https://example.com")
        assert "no_alt_images" in issues

    @patch("scraper.web_analyzer._url_exists", return_value=False)
    def test_missing_sitemap_and_robots(self, _):
        _, issues = _score_seo(make_soup(_FULL_HTML), "https://example.com")
        assert "no_sitemap" in issues
        assert "no_robots" in issues

    @patch("scraper.web_analyzer._url_exists", return_value=False)
    def test_score_clamps_to_zero(self, _):
        score, _ = _score_seo(make_soup("<html><body></body></html>"), "http://example.com")
        assert score == 0

    @patch("scraper.web_analyzer._url_exists", return_value=True)
    def test_each_issue_deducts_ten_points(self, _):
        html = _FULL_HTML.replace("<title>Mi negocio</title>", "")
        score, issues = _score_seo(make_soup(html), "https://example.com")
        assert score == 100 - len(issues) * 10


# ── Email extraction ──────────────────────────────────────────────────────────

class TestExtractEmail:
    def test_mailto_link(self):
        s = make_soup('<a href="mailto:info@example.com?subject=Hola">Escríbenos</a>')
        assert _extract_email(s, "https://example.com") == "info@example.com"

    def test_regex_match_in_body_text(self):
        s = make_soup("<p>Contacta en hola@example.com para más info.</p>")
        assert _extract_email(s, "https://example.com") == "hola@example.com"

    def test_mailto_takes_priority_over_regex(self):
        s = make_soup(
            '<p>Escríbenos a other@example.com</p>'
            '<a href="mailto:preferred@example.com">Contacto</a>'
        )
        assert _extract_email(s, "https://example.com") == "preferred@example.com"

    @patch("scraper.web_analyzer._fetch", return_value=None)
    def test_no_email_returns_empty_string(self, _):
        s = make_soup("<p>No hay email aquí.</p>")
        assert _extract_email(s, "https://example.com") == ""


# ── analyze() integration ─────────────────────────────────────────────────────

class TestAnalyze:
    def test_empty_website_returns_defaults(self):
        result = analyze({"lead": "Test", "website": ""})
        assert result["cms"] == ""
        assert result["seo_score"] is None
        assert result["email"] == ""

    def test_instagram_url_routed_to_platform_field(self):
        result = analyze({"lead": "Test", "website": "https://www.instagram.com/testbiz"})
        assert result["instagram"] == "https://www.instagram.com/testbiz"
        assert result["website"] == ""
        assert result["cms"] == ""

    @patch("scraper.web_analyzer._fetch", return_value=None)
    def test_unreachable_site_sets_cms_unreachable(self, _):
        result = analyze({"lead": "Test", "website": "https://dead.example.com"})
        assert result["cms"] == "unreachable"
