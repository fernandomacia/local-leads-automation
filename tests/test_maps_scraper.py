"""Tests for scraper/maps_scraper.py — pure logic, no real browser."""

from unittest.mock import MagicMock

import pytest

from scraper.maps_scraper import (
    SELECTOR_ADDRESS,
    SELECTOR_NAME,
    SELECTOR_PHONE,
    SELECTOR_WEBSITE,
    _extract_business,
    _normalize_domain,
)


# ── Domain normalization ──────────────────────────────────────────────────────

class TestNormalizeDomain:
    def test_strips_www_prefix(self):
        assert _normalize_domain("https://www.example.com/path") == "example.com"

    def test_no_www(self):
        assert _normalize_domain("https://example.com") == "example.com"

    def test_strips_port(self):
        assert _normalize_domain("https://example.com:8080/path") == "example.com"

    def test_http_scheme(self):
        assert _normalize_domain("http://www.example.es/contacto") == "example.es"

    def test_empty_string_returns_empty(self):
        assert _normalize_domain("") == ""


# ── Business card extraction (address parsing) ────────────────────────────────

def _make_page(
    name: str = "Test Business",
    website: str = "https://example.com",
    phone_href: str = "tel:965123456",
    address_raw: str = "Calle Mayor 1, 03201 Elche, Alicante",
) -> MagicMock:
    """Build a minimal Playwright Page mock for _extract_business."""
    page = MagicMock()

    def locator_side_effect(selector: str) -> MagicMock:
        m = MagicMock()
        if selector == SELECTOR_NAME:
            m.inner_text.return_value = name
        elif selector == SELECTOR_WEBSITE:
            m.count.return_value = 1 if website else 0
            m.first.get_attribute.return_value = website
        elif selector == SELECTOR_PHONE:
            m.count.return_value = 1 if phone_href else 0
            m.first.get_attribute.return_value = phone_href
        elif selector == SELECTOR_ADDRESS:
            m.count.return_value = 1 if address_raw else 0
            m.first.inner_text.return_value = address_raw
        return m

    page.locator.side_effect = locator_side_effect
    return page


class TestExtractBusiness:
    def test_full_spanish_address_parsed(self):
        page = _make_page(address_raw="Calle Mayor 1, 03201 Elche, Alicante")
        result = _extract_business(page, "Elche")
        assert result["zip_code"] == "03201"
        assert result["city"] == "Elche"
        assert result["province"] == "Alicante"
        assert result["address"] == "Calle Mayor 1"

    def test_address_without_zipcode_uses_default_city(self):
        page = _make_page(address_raw="Calle Sin Codigo Postal")
        result = _extract_business(page, "Valencia")
        assert result["zip_code"] == ""
        assert "Valencia" in result["city"]
        assert result["province"] == ""

    def test_leading_icon_chars_stripped_from_address(self):
        # Google Maps prepends non-word Unicode chars (e.g. the pin emoji) as icons
        page = _make_page(address_raw="\U0001f4cdCalle Mayor 1, 03201 Elche, Alicante")
        result = _extract_business(page, "Elche")
        assert result["zip_code"] == "03201"
        assert result["address"].startswith("Calle")

    def test_no_website_returns_empty_string(self):
        page = _make_page(website="")
        result = _extract_business(page, "Elche")
        assert result["website"] == ""

    def test_phone_stripped_of_tel_prefix(self):
        page = _make_page(phone_href="tel:+34965123456")
        result = _extract_business(page, "Elche")
        assert result["phone"] == "+34965123456"

    def test_no_phone_returns_empty_string(self):
        page = _make_page(phone_href="")
        result = _extract_business(page, "Elche")
        assert result["phone"] == ""

    def test_business_name_extracted(self):
        page = _make_page(name="Abogados Garcia & Asociados")
        result = _extract_business(page, "Elche")
        assert result["lead"] == "Abogados Garcia & Asociados"
