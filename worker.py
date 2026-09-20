"""SegurSEO-API job queue worker: discovers and analyzes leads on demand.

Polls two job queues — Maps discovery and per-lead website analysis — and
keeps the API's lead database in sync. Runs continuously as a background
daemon, driven entirely by the API.
"""

import logging
import random
import sys
import time

import requests

from config import API_BASE_URL, APP_VERSION, BATCH_SIZE, HEADLESS, POLL_INTERVAL, SOCIAL_DOMAINS
from scraper.maps_scraper import scrape_incrementally, maps_card_issues
from scraper.web_analyzer import analyze
from ai.message_generator import generate
from api.client import (
    ReportRejected,
    claim_next_search_job,
    check_known_domains,
    report_leads,
    complete_search_job,
    fail_search_job,
    release_search_job,
    release_analysis_job,
    claim_next_analysis_job,
    report_payment_error,
    report_analysis,
)

logger = logging.getLogger(__name__)

_SOCIAL_FIELDS = tuple(SOCIAL_DOMAINS.keys())


def _progress(text: str) -> None:
    """Overwrite the current terminal line with a progress update.

    Suppressed entirely when stdout is not a terminal. Under systemd the stream
    is a pipe to journald, which does not honour the carriage return and treats
    an unterminated write as the start of the next line — so the counter would
    arrive as fragments glued onto whatever gets logged next.
    """
    if sys.stdout.isatty():
        print(f"\r{text}", end="", flush=True)


def _finish(text: str) -> None:
    """Print a final line, clearing the in-place counter it replaces on a terminal."""
    print((f"\r{text}" + " " * 10) if sys.stdout.isatty() else text)


# What the API accepts per field, from IngestLeadsRequest and ReportLeadAnalysisRequest.
# Anything over the limit is a 422 on the *whole* request, and neither endpoint forgives
# one: on the ingest it fails the entire search, and a refused analysis report used to
# retire the lead. Cutting here is not defensive — a Maps URL carrying tracking parameters
# passes 255 routinely, and the pitch fields are written by an LLM.
#
# ``clip`` is the policy, and the two halves are not interchangeable. Prose and URLs are
# cut: a shorter address or phone script is a degraded value the agent can still use. An
# identifier is dropped instead, because half a phone number or a truncated email is not a
# shorter answer, it is a wrong one that someone will dial or write to.
_FIELD_LIMITS: dict[str, tuple[int, bool]] = {
    # field:         (limit, clip)
    "business_name": (255,  True),
    "website":       (255,  True),
    "maps_url":      (2000, True),
    "phone":         (30,   False),
    "address":       (255,  True),
    "zip_code":      (10,   False),
    "city":          (100,  True),
    "province":      (100,  True),
    "cms":           (50,   True),
    "email":         (255,  False),
    "phone_script":  (5000, True),
    "found_url":     (2048, False),
    "method":        (20,   True),
}


def _fit(field: str, value):
    """Return the value as the API will accept it, or None when it has to be left out."""
    limit, clip = _FIELD_LIMITS[field]

    if not isinstance(value, str) or len(value) <= limit:
        return value

    if not clip:
        logger.warning("Dropping %s: %d characters, the API accepts %d", field, len(value), limit)
        return None

    logger.warning("Cutting %s to the %d characters the API accepts (was %d)", field, limit, len(value))
    return value[:limit]


def _release(release, job_id: str, what: str) -> None:
    """Hand a claimed job back to the queue, logging rather than raising if that fails.

    Every caller is already handling something else — an interruption the operator asked
    for, or an upstream failure — and a release that cannot get through must not replace
    it with a traceback. The API's stale-claim recovery is the backstop: it costs up to
    SCRAPER_CLAIM_TIMEOUT_MINUTES, which is exactly the wait this call exists to avoid.
    """
    try:
        release(job_id)
    except Exception:
        logger.warning(
            "Could not release the %s in hand; leaving it to stale-claim recovery", what, exc_info=True,
        )


def map_to_api_shape(lead: dict) -> dict:
    """Map a scraped lead to the ``POST /jobs/{id}/leads`` payload shape.

    Optional fields are omitted when empty rather than sent as "". Sending "" relies on
    ConvertEmptyStringsToNull being in the middleware stack, while omitting the key is
    correct regardless of server configuration. It does leave the batch rows
    heterogeneous — different rows carrying different keys — which the API pads for
    itself before the bulk INSERT, and is the reason it has to.
    """
    payload: dict = {"business_name": _fit("business_name", lead.get("lead", ""))}
    for api_key, lead_key in [
        ("website",  "website"),
        ("maps_url", "maps_url"),
        ("phone",    "phone"),
        ("address",  "address"),
        ("zip_code", "zip_code"),
        ("city",     "city"),
        ("province", "province"),
    ]:
        if value := lead.get(lead_key):
            if (value := _fit(api_key, value)) is not None:
                payload[api_key] = value

    # Always sent, {} included: this is the only moment the Maps card is in front of us,
    # and the API accepts it here and nowhere else. {} says the card was complete; NULL
    # is reserved for a lead no worker has reported on.
    payload["maps_issues"] = maps_card_issues(lead)

    return payload


def map_analysis_to_api_shape(analysis: dict, message: dict) -> dict:
    """Map web-analyzer output and a generated message to the analysis PATCH shape.

    Every field is optional on the API side, so only populated values are sent.

    **No ``maps_issues`` here.** The API drops the key on this endpoint rather than
    refusing it, so sending it looked like it worked and wrote nothing. It belongs to the
    ingest, where the card is actually read — see ``maps_card_issues``.
    """
    payload = {}
    if analysis.get("cms"):
        payload["cms"] = _fit("cms", analysis["cms"])
    if (email := _fit("email", analysis.get("email"))):
        payload["email"] = email

    social = {f: analysis[f] for f in _SOCIAL_FIELDS if analysis.get(f)}
    if social:
        payload["social_networks"] = social

    if analysis.get("seo_score") is not None:
        payload["seo_score"] = analysis["seo_score"]
        # Rides along with the score, {} included: only a page we actually fetched can be
        # called compliant. Gating on the score keeps NULL exclusive to "not analyzed" —
        # sending {} unconditionally would make an unreachable site read as clean.
        payload["compliance_issues"] = analysis.get("compliance_issues", {})
        # The evidence behind each finding (which URL was checked, in which language,
        # why a check did not apply). Rides along with the issues so the panel can
        # justify a finding when the business owner disputes it on the call.
        payload["compliance_details"] = _fit_compliance_details(analysis.get("compliance_details", {}))
        # The language the site declares, which tells the agent what to call in. Sent
        # apart from the per-document entries: those hold the language whose lexicon
        # matched each link, and on a bilingual site the two differ.
        if language := analysis.get("compliance_language"):
            payload["compliance_language"] = language
        if checked_at := analysis.get("compliance_checked_at"):
            payload["compliance_checked_at"] = checked_at
    if analysis.get("seo_issues"):
        payload["seo_issues"] = analysis["seo_issues"]

    # Always send phone_script (even "") so NULL stays exclusive to "not yet analyzed"
    payload["phone_script"] = _fit("phone_script", message.get("phone_script", ""))

    return payload


def _fit_compliance_details(details: dict) -> dict:
    """Trim the two fields of a compliance entry the API constrains, leaving the rest alone.

    The entries themselves belong to the compliance module and reach the API unvalidated on
    purpose — ``ReportLeadAnalysisRequest::validated()`` is overridden to preserve keys it
    has no rule for, so the analyser can learn to report more without a release there. Only
    the shape the API *does* enforce is fixed here, and ``found_url`` is dropped rather than
    cut because a truncated URL is no longer the evidence it was recorded as.
    """
    if not isinstance(details, dict):
        return details

    fitted = {}
    for document, entry in details.items():
        if not isinstance(entry, dict):
            fitted[document] = entry
            continue

        entry = dict(entry)
        for field in ("found_url", "method"):
            # Only a string over its limit is touched. `found_url: None` is evidence in its
            # own right — we looked and there was nothing — and the key stays either way,
            # because a URL dropped for being absurd means the same thing to the panel.
            if isinstance(entry.get(field), str):
                entry[field] = _fit(field, entry[field])
        fitted[document] = entry

    return fitted


def _known_domain_check():
    """Return the predicate the scraper asks about each business it extracts.

    Asked per business, not per batch. The batched version answered after a lote of leads
    had already been reported, so the first BATCH_SIZE businesses of every search went
    unchecked — and with ``max_results`` at or below that, which is exactly what a quick
    sample sets, the answer arrived after the cap had been spent on businesses the system
    had all along. A sample of ten in a town already worked could return nothing new.

    A positive answer is cached, because a domain the system knows does not stop being
    known. A negative one is not, deliberately: once a lead is reported the API knows its
    domain, so a second card for the same business — a branch sharing one website — is
    skipped on the next question rather than ingested twice.

    A failed check answers "not known". The consequence of being wrong that way is a lead
    the API deduplicates at ingest anyway; the consequence of raising here would be losing
    the search over a check that is only an optimisation.
    """
    known: set[str] = set()

    def is_known(domain: str) -> bool:
        if not domain:
            return False

        if domain in known:
            return True

        try:
            if check_known_domains([domain]):
                known.add(domain)
                return True
        except Exception:
            logger.warning(
                "Could not check whether %s is already in the system; treating it as new",
                domain, exc_info=True,
            )

        return False

    return is_known


def _flush_batch(search_id: str, batch: list[dict]) -> int:
    """Submit a batch of leads. Returns the count actually inserted after server-side dedup."""
    if not batch:
        return 0

    return report_leads(search_id, batch)


def run_search_job(job: dict) -> int:
    """Discover businesses for a search job, reporting new leads in batches.

    Returns:
        Total number of new leads actually inserted (after server-side dedup) — including
        after a failure, and deliberately: the leads reported before it are already in the
        system and the API queues them for analysis regardless of the search's own status,
        so the figure is what the caller uses it for, an expectation of the analyses to come.
    """
    print(f"[>] Search: {job['profession']} en {job['city']}")
    # Asked about every business the scraper extracts, so a known one never consumes a slot
    # of max_results — see _known_domain_check. It does not save the detail tab, which has
    # to be opened to learn the website the domain comes from.
    batch: list[dict] = []
    total = 0
    try:
        for lead in scrape_incrementally(
            job["profession"], job["city"], headless=HEADLESS,
            is_known=_known_domain_check(), max_results=job["max_results"],
        ):
            batch.append(map_to_api_shape(lead))
            if len(batch) >= BATCH_SIZE:
                total += _flush_batch(job["id"], batch)
                batch = []
        total += _flush_batch(job["id"], batch)
        complete_search_job(job["id"], total)
        print(f"[+] Search done: {total} leads")
    except KeyboardInterrupt:
        # KeyboardInterrupt is a BaseException, so it never reached the handler below and
        # the search stayed `scraping` until the API's stale-claim recovery noticed —
        # half an hour in which the operator who stopped the worker could not restart it
        # on the same search. Released and re-raised: stopping is what was asked for.
        _finish("[!] Interrupted — releasing the search")
        _release(release_search_job, job["id"], "search")
        raise
    except (requests.ConnectionError, requests.Timeout):
        # The API went out of reach, which says nothing about the search. Marking it failed
        # would retire a perfectly good job over a network blip — and doing so means calling
        # the same API that just could not be reached. Released instead, best effort: if that
        # does not get through either, stale-claim recovery re-queues it.
        #
        # There is no attempt ceiling on a search, so a fault that looks transient and is
        # not would bounce the job for ever. It is throttled by its own cause: claiming a
        # search needs the API too, so while it is unreachable nothing re-runs, and each
        # round leaves a warning behind.
        logger.warning("Lost the API during search %s; releasing it", job["id"], exc_info=True)
        _release(release_search_job, job["id"], "search")
    except Exception as e:
        # Anything else is the scrape itself: a selector that no longer matches, a consent
        # screen that changed, a payload the API refused. Failing the search is the point —
        # it carries the message to the panel, where somebody sees it. Bouncing it silently
        # instead would hide a broken scraper behind a queue that never empties.
        logger.exception("Search job %s failed", job["id"])
        try:
            fail_search_job(job["id"], str(e))
        except Exception:
            pass
    return total


def run_analysis_job(job: dict, idx: int = 0) -> bool:
    """Analyze a single lead's website and generate its outreach message.

    Returns:
        Whether the compliance audit had to fall back to a browser. Counted by
        the caller: a lead that renders costs a Chromium process, which is what
        decides how much memory a host running this worker needs.
    """
    counter = f" {idx}" if idx else ""
    _progress(f"[>] Analyzing{counter}")
    rendered = False
    try:
        # profession drives the legal-notice checks that only apply to regulated
        # professions (bar association and membership number, LSSI art. 10.1.c).
        analysis = analyze({
            "lead": job["business_name"],
            "website": job["website"],
            "profession": job.get("profession", ""),
        })
        rendered = bool(analysis.get("compliance_rendered"))

        cms = analysis.get("cms")
        # Read back as the ingest recorded it. The API serves it on GET /leads/next for
        # exactly this, and a lead ingested before it was reported carries NULL — which
        # is "unknown", so the pitch simply makes no Maps argument.
        maps = job.get("maps_issues") or {}
        base_context = {
            **analysis,
            "city": job.get("city", ""),
            "profession": job.get("profession", ""),
            "maps_issues": maps,
        }

        if cms == "unreachable":
            if not (job.get("phone") or job.get("email")):
                report_analysis(job["id"], {"failed": True})
                return rendered
            message = generate({**base_context, "has_website": True})
            report_analysis(job["id"], map_analysis_to_api_shape(analysis, message))
            return rendered

        if not job.get("website"):
            # Defensive, not reachable under the current API contract: a lead with no
            # website is only claimable when it has a phone or an email (the API's
            # Lead::hasContactChannel), and no-contact leads are failed at ingest time.
            # This condition mirrors that definition exactly. Kept because without it
            # such a lead would fall through to generate() and burn an LLM call producing
            # a pitch with no way to deliver it; reporting an empty message instead
            # settles the lead so it stops holding its parent search open.
            if not (job.get("phone") or job.get("email")):
                report_analysis(job["id"], map_analysis_to_api_shape(analysis, {}))
                return rendered
            message = generate({**base_context, "has_website": False})
            report_analysis(job["id"], map_analysis_to_api_shape(analysis, message))
            return rendered

        # has_website comes from the analysis, not the raw job: analyze() blanks
        # "website" when the URL is a social profile, because a Facebook page is not a
        # site. Reading job["website"] here would report has_website=True and push the
        # generator into the "has a website" scenario, which pitches on SEO problems —
        # of which there are none, since a social URL yields an empty analysis. The
        # no-website scenario is the correct framing and the actual sales angle.
        message = generate({**base_context, "has_website": bool(analysis.get("website"))})
        report_analysis(job["id"], map_analysis_to_api_shape(analysis, message))
        return rendered
    except KeyboardInterrupt:
        _finish("[!] Interrupted — releasing the lead")
        _release(release_analysis_job, job["id"], "lead")
        raise
    except ReportRejected:
        # Our payload is wrong; the lead is not. This used to answer {"failed": True},
        # which writes analysis_failed_at and retires a lead that was read perfectly well
        # — and nothing afterwards can tell that from a site that genuinely could not be.
        #
        # The claim is left standing on purpose. Releasing it would refund the attempt and
        # the re-claim would be refused identically, burning an LLM call per round for
        # ever; left alone, stale-claim recovery retries it and the three-attempt ceiling
        # retires it, with the refused fields in the log each time.
        logger.exception(
            "The API refused the analysis report for lead %s (%s); leaving the claim to expire",
            job["id"], job["business_name"],
        )
        return rendered
    except requests.HTTPError as e:
        # 402 comes from OpenRouter, never from this API: the credit ran out mid-pitch.
        # Nothing is wrong with the lead, so it goes back to the queue with its attempt
        # refunded — otherwise a funding lapse spends all three and the API gives up on
        # leads it never actually failed to analyse.
        if e.response is not None and e.response.status_code == 402:
            search_id = job.get("lead_search_id")
            if search_id:
                try:
                    report_payment_error(search_id)
                except Exception:
                    pass
            _release(release_analysis_job, job["id"], "lead")
            raise
        logger.exception("Analysis job %s (%s) failed with HTTP error", job["id"], job["business_name"])
        try:
            report_analysis(job["id"], {"failed": True})
        except Exception:
            pass
    except Exception:
        logger.exception("Analysis job %s (%s) failed", job["id"], job["business_name"])
        try:
            report_analysis(job["id"], {"failed": True})
        except Exception:
            pass
    return False


def main() -> None:
    """Poll the job queue forever, prioritizing search jobs over analysis jobs."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Both printed on every start: the version to match a running worker to a release
    # when diagnosing a contract mismatch, and the target so an interactive run cannot
    # be scraping against development while its operator believes it is production.
    print(f"[+] Worker started (v{APP_VERSION}) — API: {API_BASE_URL}")
    analysis_idx = 0
    analysis_total = 0
    rendered_count = 0
    was_analyzing = False

    def _done_line() -> str:
        skipped = analysis_total - analysis_idx
        note = f", {skipped} skipped" if skipped > 0 else ""
        # Reported because it is the one cost that is not a request: each of these
        # leads launched a Chromium process, which is what sizes the host.
        browser = f", {rendered_count} needed a browser" if rendered_count else ""
        return f"[+] Done ({analysis_idx} analyzed{note}{browser})"

    while True:
        try:
            if job := claim_next_search_job():
                if was_analyzing:
                    _finish(_done_line())
                    was_analyzing = False
                analysis_total = run_search_job(job)
                analysis_idx = 0
                rendered_count = 0
                continue
            if job := claim_next_analysis_job():
                analysis_idx += 1
                was_analyzing = True
                rendered_count += run_analysis_job(job, analysis_idx)
                continue
            if was_analyzing:
                _finish(_done_line())
                was_analyzing = False
        except KeyboardInterrupt:
            # The job in hand, if any, has already released itself on the way out.
            _finish("[+] Worker stopped")
            return
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 402:
                time.sleep(120)
                continue
            logger.error("API HTTP error in main loop: %s", e)
        except requests.RequestException as e:
            logger.warning("API connection error, retrying: %s", e)
        time.sleep(POLL_INTERVAL + random.uniform(0, 2))


if __name__ == "__main__":
    main()
