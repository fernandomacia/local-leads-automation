"""Tests for scraper/domains.py — the normalisation the API's answers are compared against."""

import pytest

from scraper.domains import normalize_domain


class TestNormalizeDomain:
    def test_strips_www_prefix(self):
        assert normalize_domain("https://www.example.com/path") == "example.com"

    def test_no_www(self):
        assert normalize_domain("https://example.com") == "example.com"

    def test_strips_port(self):
        assert normalize_domain("https://example.com:8080/path") == "example.com"

    def test_http_scheme(self):
        assert normalize_domain("http://www.example.es/contacto") == "example.es"

    def test_empty_string_returns_empty(self):
        assert normalize_domain("") == ""


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
        assert normalize_domain(url) == expected

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
        assert normalize_domain(url) == as_written

    def test_a_host_that_normalises_to_nothing_stays_empty(self):
        # Both sides agree here: "" is what the API stores for a lead with no usable host.
        assert normalize_domain("https://..") == ""
