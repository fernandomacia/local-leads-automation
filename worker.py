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
from scraper.maps_scraper import scrape_incrementally, MAPS_ISSUES
from scraper.web_analyzer import analyze
from ai.message_generator import generate
from api.client import (
    claim_next_search_job,
    check_known_domains,
    report_leads,
    complete_search_job,
    fail_search_job,
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


def _maps_issues(job: dict) -> dict[str, str]:
    """Return Maps card issues for fields missing from the job payload."""
    return {k: label for k, (field, label) in MAPS_ISSUES.items() if not job.get(field)}


def map_to_api_shape(lead: dict) -> dict:
    """Map a scraped lead to the ``POST /jobs/{id}/leads`` payload shape.

    Optional fields are omitted when empty rather than sent as "". Sending ""
    relies on ConvertEmptyStringsToNull being in the middleware stack; omitting
    the key is correct regardless of server configuration and keeps the batch
    rows homogeneous, which the bulk INSERT requires.
    """
    payload: dict = {"business_name": lead.get("lead", "")}
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
            payload[api_key] = value
    return payload


def map_analysis_to_api_shape(analysis: dict, message: dict, maps_issues: dict) -> dict:
    """Map web-analyzer output and a generated message to the analysis PATCH shape.

    Every field is optional on the API side, so only populated values are sent.

    Args:
        maps_issues: Google Maps listing gaps from ``_maps_issues``. Required
            rather than defaulting, because these findings reach the customer
            through the generated pitch and the agent has to be able to check
            them against the panel — silently omitting them is the failure this
            parameter exists to prevent.
    """
    payload = {}
    if analysis.get("cms"):
        payload["cms"] = analysis["cms"]
    if analysis.get("email"):
        payload["email"] = analysis["email"]

    social = {f: analysis[f] for f in _SOCIAL_FIELDS if analysis.get(f)}
    if social:
        payload["social_networks"] = social

    if analysis.get("seo_score") is not None:
        payload["seo_score"] = analysis["seo_score"]
        # Rides along with the score, {} included: only a page we actually fetched can be
        # called compliant. Gating on the score keeps NULL exclusive to "not analyzed" —
        # sending {} unconditionally would make an unreachable site read as clean.
        payload["compliance_issues"] = analysis.get("compliance_issues", {})
    if analysis.get("seo_issues"):
        payload["seo_issues"] = analysis["seo_issues"]

    # Sent unconditionally, {} included, unlike compliance_issues above. These come
    # from the job payload rather than from fetching the site, so they are known
    # whether or not the website loaded: {} genuinely means "the listing is complete",
    # and NULL stays exclusive to "the worker never processed this lead".
    payload["maps_issues"] = maps_issues

    if message.get("subject"):
        payload["email_subject"] = message["subject"]
    if message.get("body"):
        payload["email_body"] = message["body"]
    # Always send phone_script (even "") so NULL stays exclusive to "not yet analyzed"
    payload["phone_script"] = message.get("phone_script", "")

    return payload


def _flush_batch(search_id: str, batch: list[dict], skip: set[str]) -> int:
    """Submit a batch of leads, updating the skip set with newly found known domains.

    Calls check_known_domains so future scraper pages avoid opening detail tabs for
    domains already in the system. Returns the count of leads actually inserted
    (server-side deduplication handles the final filter).
    """
    if not batch:
        return 0
    domains = [b["website"] for b in batch if b.get("website")]
    if domains:
        skip.update(check_known_domains(domains))
    return report_leads(search_id, batch)


def run_search_job(job: dict) -> int:
    """Discover businesses for a search job, reporting new leads in batches.

    Returns:
        Total number of new leads actually inserted (after server-side dedup).
    """
    print(f"[>] Search: {job['profession']} en {job['city']}")
    # skip starts empty and is populated from check_known_domains responses so
    # subsequent Maps pages silently bypass already-known domains without opening
    # a detail tab for each. The set is passed by reference so scrape_incrementally
    # sees every update made inside _flush_batch.
    skip: set[str] = set()
    batch: list[dict] = []
    total = 0
    try:
        for lead in scrape_incrementally(
            job["profession"], job["city"], headless=HEADLESS, skip=skip, max_results=job["max_results"]
        ):
            batch.append(map_to_api_shape(lead))
            if len(batch) >= BATCH_SIZE:
                total += _flush_batch(job["id"], batch, skip)
                batch = []
        total += _flush_batch(job["id"], batch, skip)
        complete_search_job(job["id"], total)
        print(f"[+] Search done: {total} leads")
    except Exception as e:
        logger.exception("Search job %s failed", job["id"])
        try:
            fail_search_job(job["id"], str(e))
        except Exception:
            pass
    return total


def run_analysis_job(job: dict, idx: int = 0) -> None:
    """Analyze a single lead's website and generate its outreach message."""
    counter = f" {idx}" if idx else ""
    _progress(f"[>] Analyzing{counter}")
    try:
        analysis = analyze({"lead": job["business_name"], "website": job["website"]})

        cms = analysis.get("cms")
        maps = _maps_issues(job)
        base_context = {
            **analysis,
            "city": job.get("city", ""),
            "profession": job.get("profession", ""),
            "maps_issues": maps,
        }

        if cms == "unreachable":
            if not (job.get("phone") or job.get("email")):
                report_analysis(job["id"], {"failed": True})
                return
            message = generate({**base_context, "has_website": True})
            report_analysis(job["id"], map_analysis_to_api_shape(analysis, message, maps))
            return

        if not job.get("website"):
            # Defensive, not reachable under the current API contract: a lead with no
            # website is only claimable when it has a phone or an email (the API's
            # Lead::hasContactChannel), and no-contact leads are failed at ingest time.
            # This condition mirrors that definition exactly. Kept because without it
            # such a lead would fall through to generate() and burn an LLM call producing
            # a pitch with no way to deliver it; reporting an empty message instead
            # settles the lead so it stops holding its parent search open.
            if not (job.get("phone") or job.get("email")):
                report_analysis(job["id"], map_analysis_to_api_shape(analysis, {}, maps))
                return
            message = generate({**base_context, "has_website": False})
            report_analysis(job["id"], map_analysis_to_api_shape(analysis, message, maps))
            return

        # has_website comes from the analysis, not the raw job: analyze() blanks
        # "website" when the URL is a social profile, because a Facebook page is not a
        # site. Reading job["website"] here would report has_website=True and push the
        # generator into the "has a website" scenario, which pitches on SEO problems —
        # of which there are none, since a social URL yields an empty analysis. The
        # no-website scenario is the correct framing and the actual sales angle.
        message = generate({**base_context, "has_website": bool(analysis.get("website"))})
        report_analysis(job["id"], map_analysis_to_api_shape(analysis, message, maps))
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 402:
            search_id = job.get("lead_search_id")
            if search_id:
                try:
                    report_payment_error(search_id)
                except Exception:
                    pass
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
    was_analyzing = False

    def _done_line() -> str:
        skipped = analysis_total - analysis_idx
        note = f", {skipped} skipped" if skipped > 0 else ""
        return f"[+] Done ({analysis_idx} analyzed{note})"

    while True:
        try:
            if job := claim_next_search_job():
                if was_analyzing:
                    _finish(_done_line())
                    was_analyzing = False
                analysis_total = run_search_job(job)
                analysis_idx = 0
                continue
            if job := claim_next_analysis_job():
                analysis_idx += 1
                was_analyzing = True
                run_analysis_job(job, analysis_idx)
                continue
            if was_analyzing:
                _finish(_done_line())
                was_analyzing = False
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
