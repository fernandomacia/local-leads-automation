"""Tests for scraper/maps_scraper.py — pure logic, no real browser."""

from unittest.mock import MagicMock, patch

import pytest

from scraper.maps_scraper import (
    SELECTOR_ADDRESS,
    SELECTOR_NAME,
    SELECTOR_PHONE,
    SELECTOR_WEBSITE,
    _extract_business,
    _normalize_domain,
    canonical_website,
    scrape_incrementally,
)


# ── The website as recorded ───────────────────────────────────────────────────

class TestCanonicalWebsite:
    def test_the_card_link_is_reduced_to_the_site(self):
        # The lead that prompted this: the Business Profile pointed at a tracked,
        # per-locality landing page, so the audit scored an English satellite page and
        # reported a law firm in Alicante as an English-language site.
        assert canonical_website(
            "http://www.pellicerheredia.com/en/hondon-de-las-nieves-lawyers"
            "?utm_source=Google&utm_medium=My%20Business"
        ) == "http://www.pellicerheredia.com"

    @pytest.mark.parametrize("href, expected", [
        ("https://ejemplo.es/", "https://ejemplo.es"),
        ("https://ejemplo.es", "https://ejemplo.es"),
        ("https://www.ejemplo.es/tienda/categoria#top", "https://www.ejemplo.es"),
        ("http://ejemplo.es?utm_source=Google", "http://ejemplo.es"),
    ])
    def test_query_fragment_and_path_are_dropped(self, href, expected):
        assert canonical_website(href) == expected

    @pytest.mark.parametrize("href, expected", [
        ("https://minegocio.wixsite.com/peluqueria/contacto",
         "https://minegocio.wixsite.com/peluqueria"),
        ("https://sites.google.com/view/minegocio/inicio",
         "https://sites.google.com/view/minegocio"),
        ("https://linktr.ee/minegocio?utm_source=Google", "https://linktr.ee/minegocio"),
    ])
    def test_a_site_that_lives_on_a_path_keeps_it(self, href, expected):
        # Trimming these to the host would audit the builder's own marketing homepage
        # and file the findings under the lead.
        assert canonical_website(href) == expected

    def test_a_path_hosted_url_with_no_path_is_left_at_the_host(self):
        assert canonical_website("https://linktr.ee/") == "https://linktr.ee"

    def test_the_scheme_is_kept_as_the_card_wrote_it(self):
        # http is a finding of its own (no_https); upgrading it here would hide it.
        assert canonical_website("http://ejemplo.es/x").startswith("http://")

    def test_credentials_never_survive(self):
        # They would reach the panel and every request the analysis makes.
        assert canonical_website("https://user:pass@ejemplo.es/x") == "https://ejemplo.es"

    def test_a_port_is_kept(self):
        assert canonical_website("https://ejemplo.es:8443/a/b") == "https://ejemplo.es:8443"

    @pytest.mark.parametrize("href", ["", "   ", "mailto:info@ejemplo.es", "ejemplo.es/tienda"])
    def test_anything_that_is_not_an_http_url_is_left_alone(self, href):
        # Including the scheme-less case: it cannot be confirmed to be a URL, and the
        # Maps website button always carries a scheme.
        assert canonical_website(href) == href.strip()


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


class TestNormalizeDomainAgreesWithTheApi:
    """The comparison this function exists for is against domains the API normalised.

    `POST /domains/check` answers with the output of `App\\Values\\Domain::from()`, so a
    disagreement is not a style difference: the skip set never matches, the business is
    re-ingested, its site is re-analysed at the owner's expense and someone rings them
    again. These expectations were produced by running that value object over the same
    inputs — when it changes, this is where the two are pinned.
    """

    # (input, what Domain::from() returns)
    CASES = [
        ("https://WWW.Example.com:8080/shop/", "example.com"),
        # Punycode. This was the whole divergence: the API stores the xn-- form, so no
        # accented domain ever matched, and they are common in this market.
        ("https://PELUQUERÍA.es",              "xn--peluquera-n5a.es"),
        ("peluquería.es",                      "xn--peluquera-n5a.es"),
        ("https://mañana.ejemplo.es",          "xn--maana-pta.ejemplo.es"),
        # No scheme. urlparse sees no host without one, so this used to return "".
        ("ejemplo.es",                         "ejemplo.es"),
        ("www.ejemplo.es/tienda",              "ejemplo.es"),
        # netloc carries userinfo, so this used to normalise to "user".
        ("https://user:pass@ejemplo.es/x",     "ejemplo.es"),
        # A trailing dot is the same site.
        ("http://ejemplo.es.",                 "ejemplo.es"),
        ("https://SUBdominio.Ejemplo.ES",      "subdominio.ejemplo.es"),
        ("",                                   ""),
    ]

    @pytest.mark.parametrize("url,expected", CASES)
    def test_matches_the_api(self, url, expected):
        assert _normalize_domain(url) == expected

    # The one place the two do not agree, pinned so it is read as a decision rather than
    # found as a surprise: PHP runs ICU's UTS-46, which encodes these, while `idna` enforces
    # IDNA2008 validity and refuses them. The worker then keeps the name as written, the
    # API's answer does not match it, and the cost is one slot in max_results — never a
    # duplicate lead, because the API normalises what it is sent at ingest.
    @pytest.mark.parametrize("url,as_written", [
        ("https://🍕.es",   "🍕.es"),
        ("https://ñ_x.es",  "ñ_x.es"),
    ])
    def test_a_host_idna_refuses_is_left_as_written(self, url, as_written):
        assert _normalize_domain(url) == as_written

    def test_a_host_that_normalises_to_nothing_stays_empty(self):
        # Both sides agree here: "" is what the API stores for a lead with no usable host.
        assert _normalize_domain("https://..") == ""


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
        # Asserted as equality, not containment: the fallback used to arrive as
        # "**Valencia**", and `in` is what let that pass. The asterisks were meant to mark
        # the value as inferred, but nothing read them and they travelled into leads.city,
        # where the panel sorted the lead under `*` and showed an agent the markup.
        page = _make_page(address_raw="Calle Sin Codigo Postal")
        result = _extract_business(page, "Valencia")
        assert result["city"] == "Valencia"
        # An unparsed address is legible without a marker: these two are what say so.
        assert result["zip_code"] == ""
        assert result["province"] == ""

    def test_leading_icon_chars_stripped_from_address(self):
        # Google Maps prepends non-word Unicode chars (e.g. the pin emoji) as icons
        page = _make_page(address_raw="\U0001f4cdCalle Mayor 1, 03201 Elche, Alicante")
        result = _extract_business(page, "Elche")
        assert result["zip_code"] == "03201"
        assert result["address"].startswith("Calle")

    def test_the_extracted_website_is_the_canonical_one(self):
        page = _make_page(website="https://ejemplo.es/es/servicios?utm_source=Google")
        assert _extract_business(page, "Elche")["website"] == "https://ejemplo.es"

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


# ── A quick sample must not be spent on businesses we already have ────────────

def _run_incrementally(leads, is_known, max_results):
    """Drive scrape_incrementally over a fixed set of cards, with no real browser."""
    with patch("scraper.maps_scraper.sync_playwright"), \
         patch("scraper.maps_scraper._start_search",
               return_value=(MagicMock(), MagicMock(), MagicMock())), \
         patch("scraper.maps_scraper._visible_listing_hrefs",
               return_value=[f"/h{i}" for i in range(len(leads))]), \
         patch("scraper.maps_scraper._extract_with_retries", side_effect=leads), \
         patch("scraper.maps_scraper._scroll_feed"):
        return list(scrape_incrementally("fontaneros", "Elche", max_results=max_results,
                                         is_known=is_known))


class TestKnownBusinessesDoNotConsumeTheCap:
    def test_a_sample_of_one_skips_the_businesses_already_in_the_system(self):
        # The bug this replaces: the answer used to arrive once a batch had been reported,
        # so with max_results at or below BATCH_SIZE the cap was spent before the first
        # answer came back — a sample of ten in a worked town could return nothing new.
        cards = [
            {"lead": "Ya trabajado",  "website": "https://conocido.es"},
            {"lead": "Ya trabajado2", "website": "https://tambien.es"},
            {"lead": "Nuevo",         "website": "https://nuevo.es"},
        ]

        yielded = _run_incrementally(
            cards, is_known=lambda d: d in {"conocido.es", "tambien.es"}, max_results=1,
        )

        assert [lead["lead"] for lead in yielded] == ["Nuevo"]

    def test_without_the_question_every_card_counts(self):
        # The default, for any caller that has nothing to ask.
        cards = [{"lead": "Uno", "website": "https://uno.es"},
                 {"lead": "Dos", "website": "https://dos.es"}]

        assert len(_run_incrementally(cards, is_known=None, max_results=1)) == 1

    def test_a_lead_with_no_website_is_never_skipped(self):
        # There is no domain to ask about, and a business with no site is the best kind of
        # lead this project has.
        cards = [{"lead": "Sin web", "website": ""}]

        assert [lead["lead"] for lead in _run_incrementally(
            cards, is_known=lambda d: True, max_results=1,
        )] == ["Sin web"]
