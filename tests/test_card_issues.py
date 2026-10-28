"""Tests for scraper/card_issues.py — what a Maps card is missing, read from the card."""

from scraper.card_issues import maps_card_issues


class TestMapsCardIssues:
    def test_all_fields_missing_returns_all_issues(self):
        lead = {"website": "", "phone": "", "address": ""}
        issues = maps_card_issues(lead)
        assert set(issues.keys()) == {"no_website", "no_phone", "no_address"}

    def test_all_fields_present_returns_empty(self):
        lead = {"website": "https://example.com", "phone": "965123456", "address": "Calle 1"}
        assert maps_card_issues(lead) == {}

    def test_only_website_missing(self):
        lead = {"website": "", "phone": "965123456", "address": "Calle 1"}
        issues = maps_card_issues(lead)
        assert list(issues.keys()) == ["no_website"]

    def test_issue_values_are_human_readable_labels(self):
        issues = maps_card_issues({"website": "", "phone": "", "address": ""})
        for label in issues.values():
            assert isinstance(label, str)
            assert len(label) > 0

    def test_read_from_the_scraped_card_not_from_a_later_job(self):
        # The bug this whole arrangement exists for: an agent supplies a website through
        # the panel for a business whose card had none. The card said no_website and the
        # job no longer does, so the reading has to be taken at ingest and kept.
        card = {"website": "", "phone": "965123456", "address": "Calle 1"}
        job_after_the_agent_typed_a_url = {**card, "website": "https://escrita-a-mano.es"}

        assert maps_card_issues(card) == {"no_website": "Sin sitio web en Google Maps"}
        assert maps_card_issues(job_after_the_agent_typed_a_url) == {}
