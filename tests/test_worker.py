"""Tests for worker.py — pure mapping and routing logic, no network."""

import io
import sys
from unittest.mock import patch

import pytest
import requests

from api.client import MAX_ERROR_MESSAGE, ReportRejected
from scraper.maps_scraper import maps_card_issues
from worker import (
    _finish,
    _known_domain_check,
    _progress,
    map_analysis_to_api_shape,
    map_to_api_shape,
    run_analysis_job,
    run_search_job,
)


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

    def test_maps_issues_travel_with_the_ingest(self):
        # The one moment the card is in front of us, and the only endpoint the API
        # accepts this on. Reported with the analysis instead, it was discarded.
        result = map_to_api_shape({"lead": "Sin nada", "phone": "965123456"})
        assert result["maps_issues"] == {
            "no_website": "Sin sitio web en Google Maps",
            "no_address": "Sin dirección en Google Maps",
        }

    def test_a_complete_card_sends_an_empty_object_not_nothing(self, sample_lead):
        # {} says "the card was complete"; omitting the key would leave NULL, which the
        # API reads as "no worker has reported on this lead".
        assert map_to_api_shape(sample_lead)["maps_issues"] == {}


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

    def test_compliance_issues_included_when_analyzed(self):
        analysis = {"seo_score": 70, "compliance_issues": {"no_cookie_banner": "Sin aviso"}}
        result = map_analysis_to_api_shape(analysis, {})
        assert result["compliance_issues"] == {"no_cookie_banner": "Sin aviso"}

    def test_empty_compliance_issues_sent_not_omitted(self):
        # {} means "analyzed and clean" — omitting it would leave NULL, which the
        # platform renders as "not analyzed"
        result = map_analysis_to_api_shape({"seo_score": 90, "compliance_issues": {}}, {})
        assert result["compliance_issues"] == {}

    def test_compliance_issues_omitted_when_site_not_analyzed(self):
        # Unreachable/social/no-website leads carry seo_score None; claiming {} there
        # would render an unchecked site as compliant
        result = map_analysis_to_api_shape({"seo_score": None, "compliance_issues": {}}, {})
        assert "compliance_issues" not in result

    def test_compliance_details_ride_along_with_the_issues(self):
        details = {"legal_notice": {"status": "missing", "found_url": None, "method": "link"}}
        analysis = {"seo_score": 70, "compliance_issues": {}, "compliance_details": details}
        assert map_analysis_to_api_shape(analysis, {})["compliance_details"] == details

    def test_site_language_rides_along_with_the_issues(self):
        analysis = {"seo_score": 70, "compliance_details": {}, "compliance_language": "ca"}
        assert map_analysis_to_api_shape(analysis, {})["compliance_language"] == "ca"

    def test_site_language_omitted_when_the_site_declares_none(self):
        analysis = {"seo_score": 70, "compliance_details": {}, "compliance_language": ""}
        assert "compliance_language" not in map_analysis_to_api_shape(analysis, {})

    def test_compliance_checked_at_is_forwarded(self):
        analysis = {"seo_score": 70, "compliance_details": {},
                    "compliance_checked_at": "2026-09-12T08:30:00+00:00"}
        result = map_analysis_to_api_shape(analysis, {})
        assert result["compliance_checked_at"] == "2026-09-12T08:30:00+00:00"

    def test_compliance_checked_at_omitted_when_the_audit_never_ran(self):
        # NULL is the panel's "never audited"; a timestamp with no audit behind it
        # would date a finding that was never made.
        result = map_analysis_to_api_shape({"seo_score": 70, "compliance_details": {}}, {})
        assert "compliance_checked_at" not in result

    def test_compliance_details_omitted_when_site_not_analyzed(self):
        result = map_analysis_to_api_shape({"seo_score": None, "compliance_details": {}}, {})
        assert "compliance_details" not in result

    def test_internal_audit_flags_never_reach_the_api(self):
        # compliance_rendered is worker telemetry, not part of the lead record.
        analysis = {"seo_score": 70, "compliance_details": {}, "compliance_rendered": True}
        assert "compliance_rendered" not in map_analysis_to_api_shape(analysis, {})

    def test_maps_issues_never_sent_with_the_analysis(self):
        # The API drops the key on this endpoint, so sending it wrote nothing while
        # looking like it worked. It travels with the ingest instead.
        result = map_analysis_to_api_shape({"seo_score": 70}, {})
        assert "maps_issues" not in result

    def test_maps_issues_not_sent_even_when_the_site_was_never_fetched(self):
        result = map_analysis_to_api_shape({"seo_score": None}, {})
        assert "maps_issues" not in result
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


# ── maps_card_issues ──────────────────────────────────────────────────────────

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


# ── run_analysis_job (browser accounting) ─────────────────────────────────────

_JOB = {"id": "1", "business_name": "Ejemplo SL", "website": "https://ejemplo.es",
        "phone": "965000000", "city": "Elche", "profession": "fontaneros"}


class TestRenderAccounting:
    """The count of leads that needed a browser is what sizes the host."""

    def _run(self, analysis: dict) -> bool:
        with patch("worker.analyze", return_value=analysis), \
             patch("worker.generate", return_value={"subject": "s", "body": "b"}), \
             patch("worker.report_analysis"):
            return run_analysis_job(dict(_JOB))

    def test_reports_a_lead_that_needed_a_browser(self):
        assert self._run({"cms": "wordpress", "seo_score": 60, "compliance_rendered": True}) is True

    def test_reports_a_lead_that_did_not(self):
        assert self._run({"cms": "wordpress", "seo_score": 60}) is False

    def test_an_unreachable_site_still_reports_its_browser_use(self):
        assert self._run({"cms": "unreachable", "compliance_rendered": True}) is True

    def test_a_failed_analysis_counts_no_browser(self):
        with patch("worker.analyze", side_effect=RuntimeError("boom")), \
             patch("worker.report_analysis"):
            assert run_analysis_job(dict(_JOB)) is False


# ── Handing a claimed job back ────────────────────────────────────────────────

class TestReleaseOnInterrupt:
    """A claim abandoned for a reason that is not the job's fault must not cost it.

    Without these, Ctrl+C left the search `scraping` and the lead claimed with one of its
    three attempts spent, and only the API's stale-claim recovery cleared them — half an
    hour later, which is exactly the wait the release endpoints exist to avoid.
    """

    def test_ctrl_c_during_a_search_releases_it(self):
        with patch("worker.scrape_incrementally", side_effect=KeyboardInterrupt), \
             patch("worker.release_search_job") as release:
            with pytest.raises(KeyboardInterrupt):
                run_search_job({"id": "s1", "profession": "fontaneros", "city": "Elche",
                                "max_results": None})

        release.assert_called_once_with("s1")

    def test_ctrl_c_during_an_analysis_releases_the_lead(self):
        with patch("worker.analyze", side_effect=KeyboardInterrupt), \
             patch("worker.release_analysis_job") as release:
            with pytest.raises(KeyboardInterrupt):
                run_analysis_job(dict(_JOB))

        release.assert_called_once_with("1")

    def test_the_interruption_survives_a_release_that_fails(self):
        # Stopping is what the operator asked for; a release that cannot get through is a
        # warning and a wait for stale-claim recovery, never a traceback in its place.
        with patch("worker.analyze", side_effect=KeyboardInterrupt), \
             patch("worker.release_analysis_job", side_effect=RuntimeError("API down")):
            with pytest.raises(KeyboardInterrupt):
                run_analysis_job(dict(_JOB))

    def test_a_402_releases_the_lead_and_warns_the_search(self):
        # OpenRouter out of credit says nothing about the lead. Keeping the claim would
        # spend all three attempts on a funding lapse and have the API give up for good.
        response = requests.Response()
        response.status_code = 402

        with patch("worker.analyze", side_effect=requests.HTTPError(response=response)), \
             patch("worker.report_payment_error") as payment_error, \
             patch("worker.release_analysis_job") as release:
            with pytest.raises(requests.HTTPError):
                run_analysis_job({**_JOB, "lead_search_id": "s1"})

        payment_error.assert_called_once_with("s1")
        release.assert_called_once_with("1")

    def test_an_ordinary_failure_still_fails_the_lead_instead_of_releasing_it(self):
        # The distinction the release is for: a lead whose site cannot be analysed has
        # genuinely used an attempt, and three of those are meant to retire it.
        with patch("worker.analyze", side_effect=RuntimeError("boom")), \
             patch("worker.release_analysis_job") as release, \
             patch("worker.report_analysis") as report:
            assert run_analysis_job(dict(_JOB)) is False

        release.assert_not_called()
        report.assert_called_once_with("1", {"failed": True})


# ── Fitting a payload the API will accept ─────────────────────────────────────

class TestFieldLimits:
    """A value over its column is a 422 on the whole request, and neither endpoint
    forgives one: the ingest fails the entire search, a refused report abandons the lead."""

    def test_a_long_maps_url_is_cut_not_dropped(self):
        # The common case by far: a Maps URL carrying tracking parameters.
        result = map_to_api_shape({"lead": "X", "maps_url": "https://maps.google.com/?q=" + "a" * 3000})
        assert len(result["maps_url"]) == 2000
        assert result["maps_url"].startswith("https://maps.google.com/?q=")

    def test_a_long_website_keeps_its_host(self):
        # The host is the deduplication key on the API side, and the overflow is always in
        # the query string, so cutting keeps the part that identifies the site.
        result = map_to_api_shape({"lead": "X", "website": "https://ejemplo.es/?ref=" + "b" * 400})
        assert len(result["website"]) == 255
        assert result["website"].startswith("https://ejemplo.es/")

    def test_an_absurd_phone_is_dropped_not_truncated(self):
        # Half a phone number is not a shorter answer, it is a wrong one someone will dial.
        result = map_to_api_shape({"lead": "X", "phone": "9" * 60})
        assert "phone" not in result

    def test_an_absurd_zip_code_is_dropped(self):
        assert "zip_code" not in map_to_api_shape({"lead": "X", "zip_code": "0" * 40})

    def test_a_long_business_name_is_cut(self):
        assert len(map_to_api_shape({"lead": "N" * 400})["business_name"]) == 255

    def test_a_long_pitch_is_cut(self):
        # Written by an LLM, so this is the normal case rather than an anomaly.
        message = {"subject": "S" * 400, "body": "B" * 9000, "phone_script": "P" * 6000}
        result = map_analysis_to_api_shape({}, message)
        assert len(result["email_subject"]) == 255
        assert len(result["phone_script"]) == 5000
        # email_body has no ceiling on the API side, so it goes as written.
        assert len(result["email_body"]) == 9000

    def test_an_absurd_email_is_dropped(self):
        assert "email" not in map_analysis_to_api_shape({"email": "a" * 300 + "@x.es"}, {})

    def test_an_absurd_found_url_becomes_null_and_the_entry_survives(self):
        details = {"legal_notice": {"status": "found", "found_url": "https://x.es/" + "u" * 3000,
                                   "checked_urls": ["https://x.es/aviso"]}}
        entry = map_analysis_to_api_shape(
            {"seo_score": 70, "compliance_details": details}, {},
        )["compliance_details"]["legal_notice"]

        assert entry["found_url"] is None
        # The keys the API has no rule for are preserved — its validated() override exists
        # precisely so the analyser can report more without a release there.
        assert entry["checked_urls"] == ["https://x.es/aviso"]
        assert entry["status"] == "found"

    def test_an_error_message_is_cut_to_what_the_api_stores(self):
        # The 422 this avoids is the worst one to earn: the fail call is what marks the
        # search failed, so its own rejection leaves the search looking like a live run.
        import api.client as client

        with patch("api.client.requests.post") as post:
            client.fail_search_job("s1", "E" * 5000)

        assert len(post.call_args.kwargs["json"]["error_message"]) == MAX_ERROR_MESSAGE


# ── A refused report is not a failed lead ────────────────────────────────────

class TestReportRejected:
    def test_a_refused_report_leaves_the_lead_alone(self):
        # Answering {"failed": True} writes analysis_failed_at and retires a lead that was
        # read perfectly well — and nothing afterwards can tell that apart from a site that
        # genuinely could not be read.
        response = requests.Response()
        response.status_code = 422

        with patch("worker.analyze", return_value={"cms": "wordpress", "seo_score": 60}), \
             patch("worker.generate", return_value={"subject": "s", "body": "b"}), \
             patch("worker.report_analysis", side_effect=ReportRejected(response=response)) as report, \
             patch("worker.release_analysis_job") as release:
            assert run_analysis_job(dict(_JOB)) is False

        # Reported once — the rejected payload — and never again with failed: True.
        report.assert_called_once()
        # And not released: the refund would have the re-claim refused identically, burning
        # an LLM call per round for ever. Stale-claim recovery and the attempt ceiling bound it.
        release.assert_not_called()

    def test_a_refused_report_still_counts_the_browser_it_used(self):
        response = requests.Response()
        response.status_code = 422

        with patch("worker.analyze", return_value={"cms": "wordpress", "seo_score": 60,
                                                  "compliance_rendered": True}), \
             patch("worker.generate", return_value={"subject": "s", "body": "b"}), \
             patch("worker.report_analysis", side_effect=ReportRejected(response=response)):
            assert run_analysis_job(dict(_JOB)) is True


# ── Asking whether a business is already in the system ────────────────────────

class TestKnownDomainCheck:
    """The question that decides whether a business consumes a slot of max_results.

    It used to be asked once per reported batch, so the first BATCH_SIZE businesses of a
    search went unchecked — and a quick sample sets max_results at or below that, so the
    answer arrived after the cap had already been spent on businesses we had all along.
    """

    def test_asks_the_api_about_the_first_business(self):
        with patch("worker.check_known_domains", return_value=["ejemplo.es"]) as check:
            assert _known_domain_check()("ejemplo.es") is True

        check.assert_called_once_with(["ejemplo.es"])

    def test_a_known_domain_is_asked_about_once(self):
        # Known does not become unknown, so the answer is worth keeping.
        with patch("worker.check_known_domains", return_value=["ejemplo.es"]) as check:
            is_known = _known_domain_check()
            assert is_known("ejemplo.es") is True
            assert is_known("ejemplo.es") is True

        check.assert_called_once()

    def test_an_unknown_domain_is_asked_about_again(self):
        # Deliberately not cached: once the lead is reported the API knows its domain, so a
        # second card for the same business — a branch sharing one website — is skipped on
        # the next question instead of being ingested twice.
        with patch("worker.check_known_domains", side_effect=[[], ["ejemplo.es"]]) as check:
            is_known = _known_domain_check()
            assert is_known("ejemplo.es") is False
            assert is_known("ejemplo.es") is True

        assert check.call_count == 2

    def test_a_lead_with_no_website_is_never_asked_about(self):
        with patch("worker.check_known_domains") as check:
            assert _known_domain_check()("") is False

        check.assert_not_called()

    def test_a_failed_check_answers_not_known_instead_of_ending_the_search(self):
        # Being wrong this way costs a lead the API deduplicates at ingest anyway. Raising
        # would lose the whole search over a check that is only an optimisation.
        with patch("worker.check_known_domains", side_effect=requests.ConnectionError("down")):
            assert _known_domain_check()("ejemplo.es") is False
