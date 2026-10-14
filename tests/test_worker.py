"""Tests for worker.py — pure mapping and routing logic, no network."""

import io
import sys
from unittest.mock import patch

import pytest
import requests

from api.client import ReportRejected
from scraper.maps_scraper import maps_card_issues
import logging
from contextlib import ExitStack

import worker
from worker import (
    _PAYMENT_PAUSE,
    _RATE_LIMIT_PAUSE,
    _RULE,
    _analysis_progress,
    main,
    _analysis_done_lines,
    _tally,
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

    def test_the_email_draft_is_never_sent(self):
        # Dropped in 2.7.0: the pitch is made on the call, and the API has no
        # column for a draft any more.
        message = {"subject": "Asunto", "body": "Cuerpo", "phone_script": "Script"}
        result = map_analysis_to_api_shape({}, message)
        assert "email_subject" not in result
        assert "email_body" not in result
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
        out = _capture(True, (_progress, "[>] Analysing 3"))
        assert out.startswith("\r[>] Analysing 3")

    def test_the_line_is_padded_so_a_shorter_one_cannot_leave_a_tail(self):
        # Lead names vary in length, and \r only moves the cursor: without the padding
        # the end of the previous, longer name stayed on screen.
        long_line = _capture(True, (_progress, "[>] Analysing [=] 1/2 — A business with a long name"))
        short_line = _capture(True, (_progress, "[>] Analysing [=] 2/2 — Bar"))
        assert len(long_line) == len(short_line)

    def test_progress_is_silent_when_not_a_terminal(self):
        # journald does not honour \r and would glue an unterminated write onto
        # the next log line, so the counter is dropped rather than fragmented.
        assert _capture(False, (_progress, "[>] Analysing 3")) == ""

    def test_finish_clears_the_counter_on_a_terminal(self):
        out = _capture(True, (_finish, "[+] Done — 3 analysed"))
        assert out.startswith("\r[+] Done — 3 analysed")
        assert out.endswith("\n")

    def test_finish_pads_only_the_line_that_lands_on_the_counter(self):
        # The closing block is two lines; the second starts on a row of its own and
        # padding it would leave trailing spaces in the log.
        out = _capture(True, (_finish, "[+] Done — 1 analysed\n    seo avg 50"))
        first, second = out.rstrip("\n").split("\n")
        assert len(first) > len("\r[+] Done — 1 analysed")
        assert second == "    seo avg 50"

    def test_finish_writes_one_clean_line_when_not_a_terminal(self):
        assert _capture(False, (_finish, "[+] Done — 3 analysed")) == "[+] Done — 3 analysed\n"

    def test_piped_run_emits_only_the_final_line(self):
        out = _capture(
            False,
            (_progress, "[>] Analysing 1"),
            (_progress, "[>] Analysing 2"),
            (_finish, "[+] Done — 2 analysed"),
        )
        assert out == "[+] Done — 2 analysed\n"
        assert "\r" not in out


# ── The progress line and the closing block ───────────────────────────────────

class TestAnalysisProgress:
    def test_the_bar_fills_with_the_run(self):
        assert "1/5" in _analysis_progress(1, 5, "Unive Abogados")
        assert _analysis_progress(5, 5, "Unive").count("\u2588") == 12

    def test_a_long_name_is_cut_rather_than_wrapped(self):
        # A wrapped line breaks the overwrite: \r returns to the start of the last row.
        line = _analysis_progress(1, 5, "Pellicer & Heredia Abogados Hondon de las Nieves y alrededores")
        assert len(line) <= 74

    @pytest.mark.parametrize("idx, total", [(0, 0), (3, 0), (6, 5)])
    def test_without_a_usable_total_it_counts_instead_of_drawing_a_bar(self, idx, total):
        # The analysis queue is global: leads left by an earlier run have no denominator,
        # and dividing by it here would be reported to the API as a failed lead.
        line = _analysis_progress(idx, total, "Ejemplo SL")
        assert "\u2588" not in line and "\u2591" not in line
        assert "/" not in line and "Ejemplo SL" in line


def _search(label: str, found, analysed: int, failed: int = 0) -> dict:
    return {"label": label, "found": found, "analysed": analysed, "failed": failed}


class TestAnalysisDoneLines:
    def test_the_usual_run_closes_the_search_it_belongs_to(self):
        # Read as the third of the three lines a search prints, under its own heading,
        # so it needs no label of its own.
        out = _analysis_done_lines({"s1": _search("abogado en elche", 20, 20)})
        assert out == "[+] Analysis done: 20 analysed"

    def test_failures_are_named_beside_the_analyses(self):
        out = _analysis_done_lines({"s1": _search("abogado en elche", 20, 18, failed=2)})
        assert out == "[+] Analysis done: 18 analysed, 2 failed"

    def test_several_searches_in_one_run_are_each_named(self):
        # What a restart mid-analysis produces: leads from searches this run never
        # headed with a Search line, so an unlabelled count would read as belonging to
        # whichever search came next.
        out = _analysis_done_lines({"s1": _search("abogado en elche", None, 12),
                                    "s2": _search("fontanero en elche", None, 5)})
        assert out.splitlines() == [
            "[+] Analysis done — abogado en elche: 12 analysed",
            "[+] Analysis done — fontanero en elche: 5 analysed",
        ]

    def test_a_lead_that_names_no_search_says_where_it_came_from(self):
        out = _analysis_done_lines({"s1": _search("abogado en elche", None, 2),
                                    "": _search("", None, 1)})
        assert "from an earlier run: 1 analysed" in out


def _lead(search_id: str, profession: str = "abogado", city: str = "elche") -> dict:
    return {"id": "l1", "lead_search_id": search_id, "business_name": "Ejemplo SL",
            "profession": profession, "city": city}


class TestTally:
    def test_a_lead_counts_against_the_search_that_found_it(self):
        searches = {"s1": _search("abogado en elche", 20, 0)}
        _tally(searches, _lead("s1"))["analysed"] += 1
        assert searches["s1"]["analysed"] == 1

    def test_a_lead_from_an_earlier_run_names_its_own_search(self):
        # The normal case after a restart mid-analysis: analyses are claimed before
        # searches, so these arrive before any search has a row. The lead carries the
        # profession and city the pitch is written from, which is the search's name.
        searches = {}
        _tally(searches, _lead("s9", "fontanero", "alicante"))["analysed"] += 1
        assert searches["s9"] == _search("fontanero en alicante", None, 1)

    def test_a_named_search_keeps_its_own_row(self):
        searches = {}
        _tally(searches, _lead("s9", "abogado", "elche"))["analysed"] += 1
        _tally(searches, _lead("s8", "fontanero", "elche"))["analysed"] += 1
        assert [row["label"] for row in searches.values()] == ["abogado en elche",
                                                              "fontanero en elche"]

    def test_a_lead_naming_no_search_falls_into_the_shared_row(self):
        searches = {}
        _tally(searches, {"id": "l1"})["failed"] += 1
        _tally(searches, {"id": "l2"})["failed"] += 1
        assert searches == {"": _search("", None, 0, failed=2)}


# ── run_analysis_job ──────────────────────────────────────────────────────────

_JOB = {"id": "1", "business_name": "Ejemplo SL", "website": "https://ejemplo.es",
        "phone": "965000000", "city": "Elche", "profession": "fontaneros"}


class TestAnalysedOrFailed:
    """What the closing block counts: a lead reported, or a lead given up on."""

    def _run(self, analysis: dict) -> bool:
        with patch("worker.analyze", return_value=analysis), \
             patch("worker.generate", return_value={"phone_script": "Argumentario"}), \
             patch("worker.report_analysis"):
            return run_analysis_job(dict(_JOB))

    def test_a_reported_lead_counts_as_analysed(self):
        assert self._run({"cms": "wordpress", "seo_score": 60}) is True

    def test_an_unreachable_site_is_still_analysed(self):
        # It has a phone, so it is pitched on the site being down — the strongest
        # opening the agent gets, and not a failure.
        assert self._run({"cms": "unreachable"}) is True

    def test_a_lead_nothing_could_be_read_from_counts_as_failed(self):
        with patch("worker.analyze", side_effect=RuntimeError("boom")), \
             patch("worker.report_analysis"):
            assert run_analysis_job(dict(_JOB)) is False

    def test_an_unreachable_site_with_no_contact_channel_counts_as_failed(self):
        with patch("worker.analyze", return_value={"cms": "unreachable"}), \
             patch("worker.report_analysis") as report:
            assert run_analysis_job({**_JOB, "phone": "", "email": ""}) is False
        report.assert_called_once_with("1", {"failed": True})


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

    def test_a_429_releases_the_lead_without_warning_the_search(self):
        # OpenRouter's rate limit, raised once the generator's own retries gave up. It
        # says nothing about the lead and nothing about the account, so the lead goes
        # back to the queue and the search is left alone. Answered as an ordinary HTTP
        # error it was reported `failed: True`, retiring a lead over a limit that had
        # lifted a minute later.
        response = requests.Response()
        response.status_code = 429

        with patch("worker.analyze", side_effect=requests.HTTPError(response=response)), \
             patch("worker.report_payment_error") as payment_error, \
             patch("worker.report_analysis") as report, \
             patch("worker.release_analysis_job") as release:
            with pytest.raises(requests.HTTPError):
                run_analysis_job({**_JOB, "lead_search_id": "s1"})

        release.assert_called_once_with("1")
        payment_error.assert_not_called()
        report.assert_not_called()

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
        result = map_analysis_to_api_shape({}, {"phone_script": "P" * 6000})
        assert len(result["phone_script"]) == 5000

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



# ── A refused report is not a failed lead ────────────────────────────────────

class TestReportRejected:
    def test_a_refused_report_leaves_the_lead_alone(self):
        # Answering {"failed": True} writes analysis_failed_at and retires a lead that was
        # read perfectly well — and nothing afterwards can tell that apart from a site that
        # genuinely could not be read.
        response = requests.Response()
        response.status_code = 422

        with patch("worker.analyze", return_value={"cms": "wordpress", "seo_score": 60}), \
             patch("worker.generate", return_value={"phone_script": "Argumentario"}), \
             patch("worker.report_analysis", side_effect=ReportRejected(response=response)) as report, \
             patch("worker.release_analysis_job") as release:
            # Counted as a failure of the run, though the lead itself is left alone:
            # the operator needs the closing block to say something went wrong.
            assert run_analysis_job(dict(_JOB)) is False

        # Reported once — the rejected payload — and never again with failed: True.
        report.assert_called_once()
        # And not released: the refund would have the re-claim refused identically, burning
        # an LLM call per round for ever. Stale-claim recovery and the attempt ceiling bound it.
        release.assert_not_called()


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


# ── What ends a search, and what only interrupts it ───────────────────────────

class TestSearchFailureVsInterruption:
    _SEARCH = {"id": "s1", "profession": "fontaneros", "city": "Elche", "max_results": None}

    def test_a_broken_scrape_fails_the_search(self):
        # The message is what reaches the panel, where somebody sees it. Bouncing this back
        # to the queue would hide a broken scraper behind a job that never finishes.
        with patch("worker.scrape_incrementally", side_effect=RuntimeError("selector gone")), \
             patch("worker.fail_search_job") as fail, \
             patch("worker.release_search_job") as release:
            run_search_job(dict(self._SEARCH))

        fail.assert_called_once()
        assert fail.call_args.args[0] == "s1"
        release.assert_not_called()

    @pytest.mark.parametrize("error", [
        requests.ConnectionError("API down"),
        requests.Timeout("too slow"),
    ])
    def test_losing_the_api_releases_the_search_instead(self, error):
        # A network fault says nothing about the search, and recording a failure means
        # calling the same API that just could not be reached.
        with patch("worker.scrape_incrementally", side_effect=error), \
             patch("worker.fail_search_job") as fail, \
             patch("worker.release_search_job") as release:
            run_search_job(dict(self._SEARCH))

        release.assert_called_once_with("s1")
        fail.assert_not_called()

    def test_an_api_error_response_still_fails_the_search(self):
        # An HTTPError is the API answering — a payload it refused, say — not the API being
        # out of reach, so it keeps the terminal treatment.
        response = requests.Response()
        response.status_code = 422

        with patch("worker.scrape_incrementally", side_effect=requests.HTTPError(response=response)), \
             patch("worker.fail_search_job") as fail, \
             patch("worker.release_search_job") as release:
            run_search_job(dict(self._SEARCH))

        fail.assert_called_once()
        release.assert_not_called()


# ── The order jobs are claimed in ─────────────────────────────────────────────

class TestStartupLines:
    def _start(self, is_tty: bool) -> str:
        buf = io.StringIO()
        buf.isatty = lambda: is_tty
        with patch.object(sys, "stdout", buf), \
             patch("worker.claim_next_analysis_job", return_value=None), \
             patch("worker.claim_next_search_job", side_effect=KeyboardInterrupt), \
             patch("worker.time.sleep"):
            main()
        return buf.getvalue()

    def test_a_terminal_is_told_how_to_stop_the_worker(self):
        assert "Ctrl+C to stop" in self._start(True)

    def test_a_service_is_not_told_to_press_a_key(self):
        # systemctl stop arrives as SIGTERM, which kills the process without the release
        # Ctrl+C performs, so the advice would be wrong where nobody can follow it.
        out = self._start(False)
        assert "Ctrl+C" not in out
        assert "Worker started" in out


class TestJobOrder:
    """A search is finished before the next is claimed.

    The API already describes that order — pending, scraping, analyzing, completed —
    and claiming searches first left two of them in `analyzing` at once, their leads
    analysed as one indistinguishable stream.
    """

    def test_analyses_are_claimed_before_searches(self, capsys):
        calls = []

        def claim_analysis():
            calls.append("analysis")
            return None

        def claim_search():
            calls.append("search")
            raise KeyboardInterrupt  # one pass is enough to see the order

        with patch("worker.claim_next_analysis_job", side_effect=claim_analysis), \
             patch("worker.claim_next_search_job", side_effect=claim_search), \
             patch("worker.time.sleep"):
            main()

        assert calls == ["analysis", "search"]

    def test_a_search_is_analysed_to_the_end_before_the_next_one_starts(self, capsys):
        events = []
        leads = {"s1": ["a1", "a2"], "s2": ["b1"]}
        searches = iter([
            {"id": "s1", "profession": "abogado", "city": "elche"},
            {"id": "s2", "profession": "fontanero", "city": "elche"},
        ])
        queued: list[tuple[str, str]] = []

        def claim_search():
            job = next(searches, None)
            if job is None:
                raise KeyboardInterrupt
            return job

        def run_search(job):
            # The API queues a search's leads as the scrape reports them.
            events.append(f"scrape:{job['id']}")
            queued.extend((lead, job["id"]) for lead in leads[job["id"]])
            return len(leads[job["id"]])

        def claim_analysis():
            if not queued:
                return None
            lead, search_id = queued.pop(0)
            return {"id": lead, "lead_search_id": search_id, "business_name": lead}

        def run_analysis(job, idx=0, total=0):
            events.append(f"analyse:{job['id']}")
            return True

        with patch("worker.claim_next_analysis_job", side_effect=claim_analysis), \
             patch("worker.claim_next_search_job", side_effect=claim_search), \
             patch("worker.run_search_job", side_effect=run_search), \
             patch("worker.run_analysis_job", side_effect=run_analysis), \
             patch("worker.time.sleep"):
            main()

        assert events == ["scrape:s1", "analyse:a1", "analyse:a2", "scrape:s2", "analyse:b1"]

        # And each search closes its own analyses, rather than one total covering both.
        # The second line arrives on the way out: Ctrl+C is the normal way to stop a
        # run, and discarding what it had got through left no record of it.
        assert capsys.readouterr().out.count("[+] Analysis done: 2 analysed") == 1

    def test_an_interrupted_run_still_reports_what_it_got_through(self, capsys):
        searches = iter([{"id": "s1", "profession": "abogado", "city": "elche"}])
        queued: list[str] = []

        def run_search(job):
            queued.extend(["a1", "a2", "a3"])
            return 5

        def claim_analysis():
            if not queued:
                return None  # the first pass, before the search has been scraped
            lead = queued.pop(0)
            if lead == "a3":
                raise KeyboardInterrupt  # stopped mid-run, two leads in
            return {"id": lead, "lead_search_id": "s1", "business_name": lead,
                    "profession": "abogado", "city": "elche"}

        with patch("worker.claim_next_analysis_job", side_effect=claim_analysis), \
             patch("worker.claim_next_search_job", side_effect=lambda: next(searches, None)), \
             patch("worker.run_search_job", side_effect=run_search), \
             patch("worker.run_analysis_job", return_value=True), \
             patch("worker.time.sleep"):
            main()

        out = capsys.readouterr().out
        assert "[+] Analysis done: 2 analysed" in out
        assert "[+] Worker stopped" in out


# ── Separators ────────────────────────────────────────────────────────────────

class TestSeparators:
    """A rule heads each block of work, so one search's lines tell from the next's."""

    def _run(self, is_tty: bool = True) -> list[str]:
        searches = iter([{"id": "s1", "profession": "abogado", "city": "elche"},
                         {"id": "s2", "profession": "fontanero", "city": "elche"}])
        queued: list[str] = []

        def claim_search():
            job = next(searches, None)
            if job is None:
                raise KeyboardInterrupt
            return job

        def run_search(job):
            # Stands in for the real one, which is what prints the search header.
            print(f"[>] Search: {job['profession']} en {job['city']}")
            queued.append(job["id"])
            return 1

        def claim_analysis():
            if not queued:
                return None
            search_id = queued.pop(0)
            return {"id": "l1", "lead_search_id": search_id, "business_name": "Ejemplo SL",
                    "profession": "abogado", "city": "elche"}

        buf = io.StringIO()
        buf.isatty = lambda: is_tty
        with patch.object(sys, "stdout", buf), \
             patch("worker.claim_next_analysis_job", side_effect=claim_analysis), \
             patch("worker.claim_next_search_job", side_effect=claim_search), \
             patch("worker.run_search_job", side_effect=run_search), \
             patch("worker.run_analysis_job", return_value=True), \
             patch("worker.time.sleep"):
            main()
        return buf.getvalue().replace("\r", "\n").splitlines()

    def test_every_search_is_headed_by_one(self):
        assert self._run().count(_RULE) == 2

    def test_it_comes_before_the_search_rather_than_after_it(self):
        lines = [line.strip() for line in self._run() if line.strip()]
        rule = lines.index(_RULE)
        assert lines[rule + 1].startswith("[>] Search")

    def test_a_piped_run_gets_them_too(self):
        # A run sent to a file is read the same way, and there the rule is the only
        # thing separating one search's lines from the next's.
        assert self._run(is_tty=False).count(_RULE) == 2

    def test_leads_left_by_an_earlier_run_are_headed_too(self):
        # They arrive before any search is claimed, so no search line heads them.
        jobs = iter([{"id": "l1", "lead_search_id": "s9", "business_name": "Ejemplo SL",
                      "profession": "abogado", "city": "elche"}])

        def claim_analysis():
            job = next(jobs, None)
            if job is None:
                raise KeyboardInterrupt
            return job

        buf = io.StringIO()
        buf.isatty = lambda: True
        with patch.object(sys, "stdout", buf), \
             patch("worker.claim_next_analysis_job", side_effect=claim_analysis), \
             patch("worker.claim_next_search_job", return_value=None), \
             patch("worker.run_analysis_job", return_value=True), \
             patch("worker.time.sleep"):
            main()

        assert _RULE in buf.getvalue()


# ── Waiting out a limit ───────────────────────────────────────────────────────

class TestRateLimitPause:
    def _loop_once(self, status: int) -> list[int]:
        """Run one pass whose analysis raises `status`, returning the sleeps taken."""
        response = requests.Response()
        response.status_code = status
        slept: list[int] = []
        raised = []

        def claim_analysis():
            if raised:
                raise KeyboardInterrupt
            raised.append(True)
            return {"id": "l1", "lead_search_id": "s1", "business_name": "Ejemplo SL",
                    "profession": "abogado", "city": "elche"}

        with patch("worker.claim_next_analysis_job", side_effect=claim_analysis), \
             patch("worker.claim_next_search_job", return_value=None), \
             patch("worker.run_analysis_job", side_effect=requests.HTTPError(response=response)), \
             patch("worker.time.sleep", side_effect=slept.append), \
             patch("sys.stdout"):
            main()
        return slept

    def test_a_rate_limit_pauses_a_minute_and_carries_on(self):
        assert _RATE_LIMIT_PAUSE in self._loop_once(429)

    def test_a_payment_error_waits_longer(self):
        # Topping up an account is manual; a rate limit lifts on its own.
        assert _PAYMENT_PAUSE in self._loop_once(402)
        assert _PAYMENT_PAUSE > _RATE_LIMIT_PAUSE


class TestCtrlCIsNeverATraceback:
    """Every point the loop waits at, which is where an interrupt actually lands."""

    def _stop_at(self, **patches) -> str:
        defaults = {"worker.claim_next_analysis_job": {"return_value": None},
                    "worker.claim_next_search_job": {"return_value": None},
                    "worker.time.sleep": {"return_value": None}}
        defaults.update(patches)
        buf = io.StringIO()
        buf.isatty = lambda: False
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, "stdout", buf))
            for target, kwargs in defaults.items():
                stack.enter_context(patch(target, **kwargs))
            main()  # must return rather than raise
        return buf.getvalue()

    def test_while_waiting_to_poll(self):
        # Where an idle worker spends almost every second of its life, and where the
        # polling sleep used to sit outside the handler.
        assert "[+] Worker stopped" in self._stop_at(**{
            "worker.time.sleep": {"side_effect": KeyboardInterrupt}})

    def test_while_waiting_out_a_rate_limit(self):
        response = requests.Response()
        response.status_code = 429
        out = self._stop_at(**{
            "worker.claim_next_analysis_job": {"return_value": {"id": "l1", "business_name": "X"}},
            "worker.run_analysis_job": {"side_effect": requests.HTTPError(response=response)},
            "worker.time.sleep": {"side_effect": KeyboardInterrupt},
        })
        assert "[+] Worker stopped" in out

    def test_while_analysing_a_lead(self):
        out = self._stop_at(**{
            "worker.claim_next_analysis_job": {"return_value": {
                "id": "l1", "business_name": "X", "profession": "abogado", "city": "elche"}},
            "worker.run_analysis_job": {"side_effect": KeyboardInterrupt},
        })
        assert "[+] Worker stopped" in out


class TestLogsDoNotLandOnTheCounter:
    def test_a_record_clears_the_in_place_line_first(self):
        # The counter goes to stdout and logging to stderr, so on a terminal a warning
        # arrived on the same row, spliced onto the lead being analysed.
        buf = io.StringIO()
        buf.isatty = lambda: True
        handler = worker._ProgressAwareHandler(stream=io.StringIO())
        record = logging.LogRecord("x", logging.WARNING, "f", 1, "rate limited", None, None)

        with patch.object(sys, "stdout", buf):
            handler.emit(record)

        assert buf.getvalue() == f"\r{'':<{worker._LINE_WIDTH}}\r"

    def test_nothing_is_written_when_there_is_no_counter_to_clear(self):
        buf = io.StringIO()
        buf.isatty = lambda: False
        handler = worker._ProgressAwareHandler(stream=io.StringIO())
        record = logging.LogRecord("x", logging.WARNING, "f", 1, "rate limited", None, None)

        with patch.object(sys, "stdout", buf):
            handler.emit(record)

        assert buf.getvalue() == ""
