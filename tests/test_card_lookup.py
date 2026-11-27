"""Tests for scraper/card_lookup.py — reading a site's identity and accepting a Maps card."""

from bs4 import BeautifulSoup

from scraper.card_lookup import card_matches, identity_from_soup, phone_key, search_queries


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


class TestPhoneKey:
    def test_keeps_the_nine_digits_that_identify_a_spanish_number(self):
        assert phone_key("+34 963 63 80 56") == "963638056"
        assert phone_key("0034-963.638.056") == "963638056"

    def test_too_short_is_no_key(self):
        assert phone_key("112") == ""
        assert phone_key("") == ""


class TestIdentityFromSoup:
    def test_prefers_the_declared_site_name(self):
        soup = _soup('<head><meta property="og:site_name" content="Clínica Sanz">'
                     '<title>Inicio | Otra cosa</title></head>')
        assert identity_from_soup(soup)["name"] == "Clínica Sanz"

    def test_skips_the_title_segment_that_names_the_page(self):
        assert identity_from_soup(_soup("<title>Inicio | Clínica Sanz</title>"))["name"] == "Clínica Sanz"
        assert identity_from_soup(_soup("<title>Clínica Sanz - Home</title>"))["name"] == "Clínica Sanz"

    def test_reads_phones_from_links_and_text_without_repeats(self):
        soup = _soup('<a href="tel:+34963638056">Llámanos</a>'
                     '<p>Tel. 963 63 80 56 · Móvil 600 11 12 22</p>')
        assert identity_from_soup(soup)["phones"] == ["963638056", "600111222"]

    def test_reads_the_town_after_a_postal_code(self):
        soup = _soup("<footer>Plaça del Poble, 1 · 46110 Godella (Valencia)</footer>")
        assert identity_from_soup(soup)["city"] == "Godella"

    def test_a_site_that_says_nothing_gives_empty_answers(self):
        assert identity_from_soup(_soup("<p>hola</p>")) == {"name": "", "phones": [], "city": ""}


class TestSearchQueries:
    def test_domain_first_then_name_and_town(self):
        queries = search_queries("https://www.godela.es/inicio", {"name": "Ajuntament de Godella", "city": "Godella"})
        assert queries == ["godela.es", "Ajuntament de Godella Godella"]

    def test_only_the_domain_when_the_site_gave_no_name(self):
        assert search_queries("https://segurseo.com", {"name": "", "city": ""}) == ["segurseo.com"]


class TestCardMatches:
    def test_same_domain_is_the_same_business(self):
        assert card_matches({"website": "https://www.godela.es/"}, "https://godela.es", [])

    def test_same_phone_is_the_same_business(self):
        assert card_matches({"website": "", "phone": "+34 963 63 80 56"}, "https://godela.es", ["963638056"])

    def test_a_namesake_elsewhere_is_refused(self):
        card = {"website": "https://godella-otra.es", "phone": "961111111"}
        assert not card_matches(card, "https://godela.es", ["963638056"])

    def test_a_card_with_neither_is_refused(self):
        assert not card_matches({"website": "", "phone": ""}, "https://godela.es", [])
