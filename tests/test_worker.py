"""Tests for worker.py — pure mapping and routing logic, no network."""

import pytest

from worker import _maps_issues, map_analysis_to_api_shape, map_to_api_shape


# ── map_to_api_shape ──────────────────────────────────────────────────────────

class TestMapToApiShape:
    def test_all_fields_mapped(self, sample_lead):
        result = map_to_api_shape(sample_lead)
        assert result["business_name"] == "Abogados García"
        assert result["website"] == "https://abogadosgarcia.es"
        assert result["phone"] == "965123456"
        assert result["city"] == "Elche"
        assert result["province"] == "Alicante"
        assert result["zip_code"] == "03201"
        assert result["maps_url"] == sample_lead["maps_url"]

    def test_missing_fields_default_to_empty_string(self):
        result = map_to_api_shape({})
        assert result["business_name"] == ""
        assert result["website"] == ""
        assert result["phone"] == ""

    def test_lead_key_mapped_to_business_name(self):
        result = map_to_api_shape({"lead": "Mi Empresa"})
        assert result["business_name"] == "Mi Empresa"


# ── map_analysis_to_api_shape ─────────────────────────────────────────────────

class TestMapAnalysisToApiShape:
    def test_phone_script_always_present(self):
        result = map_analysis_to_api_shape({}, {})
        assert "phone_script" in result
        assert result["phone_script"] == ""

    def test_empty_phone_script_sent_not_omitted(self):
        result = map_analysis_to_api_shape({}, {"phone_script": ""})
        assert result["phone_script"] == ""

    def test_cms_omitted_when_empty(self):
        result = map_analysis_to_api_shape({"cms": ""}, {})
        assert "cms" not in result

    def test_cms_included_when_set(self):
        result = map_analysis_to_api_shape({"cms": "wordpress"}, {})
        assert result["cms"] == "wordpress"

    def test_seo_score_zero_included(self):
        # seo_score=0 is a valid value — must not be omitted by falsy check
        result = map_analysis_to_api_shape({"seo_score": 0}, {})
        assert result["seo_score"] == 0

    def test_seo_score_none_omitted(self):
        result = map_analysis_to_api_shape({"seo_score": None}, {})
        assert "seo_score" not in result

    def test_social_networks_grouped_and_filtered(self):
        analysis = {
            "instagram": "https://instagram.com/test",
            "facebook": "",
            "youtube": "",
            "linkedin": "",
            "twitter": "",
            "tiktok": "",
        }
        result = map_analysis_to_api_shape(analysis, {})
        assert result["social_networks"] == {"instagram": "https://instagram.com/test"}

    def test_no_socials_omits_social_networks_key(self):
        analysis = {k: "" for k in ("instagram", "facebook", "youtube", "linkedin", "twitter", "tiktok")}
        result = map_analysis_to_api_shape(analysis, {})
        assert "social_networks" not in result

    def test_subject_and_body_included(self):
        message = {"subject": "Asunto", "body": "Cuerpo", "phone_script": "Script"}
        result = map_analysis_to_api_shape({}, message)
        assert result["email_subject"] == "Asunto"
        assert result["email_body"] == "Cuerpo"
        assert result["phone_script"] == "Script"


# ── _maps_issues ──────────────────────────────────────────────────────────────

class TestMapsIssues:
    def test_all_fields_missing_returns_all_issues(self):
        job = {"website": "", "phone": "", "address": ""}
        issues = _maps_issues(job)
        assert set(issues.keys()) == {"no_website", "no_phone", "no_address"}

    def test_all_fields_present_returns_empty(self):
        job = {"website": "https://example.com", "phone": "965123456", "address": "Calle 1"}
        assert _maps_issues(job) == {}

    def test_only_website_missing(self):
        job = {"website": "", "phone": "965123456", "address": "Calle 1"}
        issues = _maps_issues(job)
        assert list(issues.keys()) == ["no_website"]

    def test_issue_values_are_human_readable_labels(self):
        job = {"website": "", "phone": "", "address": ""}
        issues = _maps_issues(job)
        for label in issues.values():
            assert isinstance(label, str)
            assert len(label) > 0
