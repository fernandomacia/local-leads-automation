"""Tests for worker.py — pure mapping and routing logic, no network."""

import io
import sys
from unittest.mock import patch

import pytest

from worker import _finish, _maps_issues, _progress, map_analysis_to_api_shape, map_to_api_shape


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

    def test_missing_optional_fields_are_omitted(self):
        result = map_to_api_shape({})
        assert result["business_name"] == ""
        assert "website" not in result
        assert "phone" not in result

    def test_lead_key_mapped_to_business_name(self):
        result = map_to_api_shape({"lead": "Mi Empresa"})
        assert result["business_name"] == "Mi Empresa"


# ── map_analysis_to_api_shape ─────────────────────────────────────────────────

class TestMapAnalysisToApiShape:
    def test_phone_script_always_present(self):
        result = map_analysis_to_api_shape({}, {}, {})
        assert "phone_script" in result
        assert result["phone_script"] == ""

    def test_empty_phone_script_sent_not_omitted(self):
        result = map_analysis_to_api_shape({}, {"phone_script": ""}, {})
        assert result["phone_script"] == ""

    def test_cms_omitted_when_empty(self):
        result = map_analysis_to_api_shape({"cms": ""}, {}, {})
        assert "cms" not in result

    def test_cms_included_when_set(self):
        result = map_analysis_to_api_shape({"cms": "wordpress"}, {}, {})
        assert result["cms"] == "wordpress"

    def test_seo_score_zero_included(self):
        # seo_score=0 is a valid value — must not be omitted by falsy check
        result = map_analysis_to_api_shape({"seo_score": 0}, {}, {})
        assert result["seo_score"] == 0

    def test_seo_score_none_omitted(self):
        result = map_analysis_to_api_shape({"seo_score": None}, {}, {})
        assert "seo_score" not in result

    def test_compliance_issues_included_when_analyzed(self):
        analysis = {"seo_score": 70, "compliance_issues": {"no_cookie_banner": "Sin aviso"}}
        result = map_analysis_to_api_shape(analysis, {}, {})
        assert result["compliance_issues"] == {"no_cookie_banner": "Sin aviso"}

    def test_empty_compliance_issues_sent_not_omitted(self):
        # {} means "analyzed and clean" — omitting it would leave NULL, which the
        # platform renders as "not analyzed"
        result = map_analysis_to_api_shape({"seo_score": 90, "compliance_issues": {}}, {}, {})
        assert result["compliance_issues"] == {}

    def test_compliance_issues_omitted_when_site_not_analyzed(self):
        # Unreachable/social/no-website leads carry seo_score None; claiming {} there
        # would render an unchecked site as compliant
        result = map_analysis_to_api_shape({"seo_score": None, "compliance_issues": {}}, {}, {})
        assert "compliance_issues" not in result

    def test_compliance_details_ride_along_with_the_issues(self):
        details = {"legal_notice": {"status": "missing", "found_url": None, "method": "link"}}
        analysis = {"seo_score": 70, "compliance_issues": {}, "compliance_details": details}
        assert map_analysis_to_api_shape(analysis, {}, {})["compliance_details"] == details

    def test_compliance_details_omitted_when_site_not_analyzed(self):
        result = map_analysis_to_api_shape({"seo_score": None, "compliance_details": {}}, {}, {})
        assert "compliance_details" not in result

    def test_maps_issues_forwarded_to_the_payload(self):
        maps = {"no_address": "Sin dirección en la ficha"}
        result = map_analysis_to_api_shape({"seo_score": 70}, {}, maps)
        assert result["maps_issues"] == maps

    def test_empty_maps_issues_sent_not_omitted(self):
        # {} means "the listing is complete", a real finding the agent needs to see;
        # omitting it would leave NULL, which reads as "never processed"
        result = map_analysis_to_api_shape({"seo_score": 70}, {}, {})
        assert result["maps_issues"] == {}

    def test_maps_issues_sent_even_when_the_site_was_never_fetched(self):
        # Unlike compliance_issues, these come from the job payload, not from the
        # fetch, so an unreachable site still has a known Maps listing state
        result = map_analysis_to_api_shape({"seo_score": None}, {}, {"no_website": "Sin web"})
        assert result["maps_issues"] == {"no_website": "Sin web"}
        assert "compliance_issues" not in result

    def test_social_networks_grouped_and_filtered(self):
        analysis = {
            "instagram": "https://instagram.com/test",
            "facebook": "",
            "youtube": "",
            "linkedin": "",
            "twitter": "",
            "tiktok": "",
        }
        result = map_analysis_to_api_shape(analysis, {}, {})
        assert result["social_networks"] == {"instagram": "https://instagram.com/test"}

    def test_no_socials_omits_social_networks_key(self):
        analysis = {k: "" for k in ("instagram", "facebook", "youtube", "linkedin", "twitter", "tiktok")}
        result = map_analysis_to_api_shape(analysis, {}, {})
        assert "social_networks" not in result

    def test_subject_and_body_included(self):
        message = {"subject": "Asunto", "body": "Cuerpo", "phone_script": "Script"}
        result = map_analysis_to_api_shape({}, message, {})
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


# ── Terminal progress output ──────────────────────────────────────────────────

def _capture(is_tty: bool, *calls) -> str:
    """Run the given (fn, text) pairs against a stdout whose isatty() is fixed."""
    buf = io.StringIO()
    buf.isatty = lambda: is_tty
    with patch.object(sys, "stdout", buf):
        for fn, text in calls:
            fn(text)
    return buf.getvalue()


class TestProgressOutput:
    def test_progress_overwrites_in_place_on_a_terminal(self):
        out = _capture(True, (_progress, "[>] Analyzing 3"))
        assert out == "\r[>] Analyzing 3"

    def test_progress_is_silent_when_not_a_terminal(self):
        # journald does not honour \r and would glue an unterminated write onto
        # the next log line, so the counter is dropped rather than fragmented.
        assert _capture(False, (_progress, "[>] Analyzing 3")) == ""

    def test_finish_clears_the_counter_on_a_terminal(self):
        out = _capture(True, (_finish, "[+] Done (3 analyzed)"))
        assert out.startswith("\r[+] Done (3 analyzed)")
        assert out.endswith("\n")

    def test_finish_writes_one_clean_line_when_not_a_terminal(self):
        assert _capture(False, (_finish, "[+] Done (3 analyzed)")) == "[+] Done (3 analyzed)\n"

    def test_piped_run_emits_only_the_final_line(self):
        out = _capture(
            False,
            (_progress, "[>] Analyzing 1"),
            (_progress, "[>] Analyzing 2"),
            (_finish, "[+] Done (2 analyzed)"),
        )
        assert out == "[+] Done (2 analyzed)\n"
        assert "\r" not in out
