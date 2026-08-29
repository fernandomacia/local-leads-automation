"""SegurSEO-API job queue worker: discovers and analyzes leads on demand.

Polls two job queues — Maps discovery and per-lead website analysis — and
keeps the API's lead database in sync. Runs continuously as a background
daemon, driven entirely by the API.
"""

import logging
import random
import time

import requests

from config import BATCH_SIZE, HEADLESS, POLL_INTERVAL, SOCIAL_DOMAINS
from scraper.maps_scraper import scrape_incrementally, MAPS_ISSUE_LABELS
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

_MAPS_FIELD_KEYS = {"no_website": "website", "no_phone": "phone", "no_address": "address"}


def _maps_issues(job: dict) -> dict[str, str]:
    """Return Maps card issues for fields missing from the job payload."""
    return {k: MAPS_ISSUE_LABELS[k] for k, field in _MAPS_FIELD_KEYS.items() if not job.get(field)}


def map_to_api_shape(lead: dict) -> dict:
    """Map a scraped lead to the ``POST /jobs/{id}/leads`` payload shape."""
    return {
        "business_name": lead.get("lead", ""),
        "website": lead.get("website", ""),
        "maps_url": lead.get("maps_url", ""),
        "phone": lead.get("phone", ""),
        "address": lead.get("address", ""),
        "zip_code": lead.get("zip_code", ""),
        "city": lead.get("city", ""),
        "province": lead.get("province", ""),
    }


def map_analysis_to_api_shape(analysis: dict, message: dict) -> dict:
    """Map web-analyzer output and a generated message to the analysis PATCH shape.

    Every field is optional on the API side, so only populated values are sent.
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
    if analysis.get("seo_issues"):
        payload["seo_issues"] = analysis["seo_issues"]

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


def run_analysis_job(job: dict, idx: int = 0, total: int = 0) -> None:
    """Analyze a single lead's website and generate its outreach message."""
    counter = f" {idx}/{total}" if total else ""
    print(f"\r[>] Analyzing{counter}", end="", flush=True)
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
            report_analysis(job["id"], map_analysis_to_api_shape(analysis, message))
            return

        if not job.get("website"):
            if not (job.get("phone") or job.get("email")):
                report_analysis(job["id"], map_analysis_to_api_shape(analysis, {}))
                return
            message = generate({**base_context, "has_website": False})
            report_analysis(job["id"], map_analysis_to_api_shape(analysis, message))
            return

        message = generate({**base_context, "has_website": True})
        report_analysis(job["id"], map_analysis_to_api_shape(analysis, message))
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
    print("[+] Worker started")
    analysis_idx = 0
    analysis_total = 0
    was_analyzing = False
    while True:
        try:
            if job := claim_next_search_job():
                if was_analyzing:
                    print(f"\r[+] Done ({analysis_idx} analyzed)" + " " * 10)
                    was_analyzing = False
                analysis_total = run_search_job(job)
                analysis_idx = 0
                continue
            if job := claim_next_analysis_job():
                analysis_idx += 1
                was_analyzing = True
                run_analysis_job(job, analysis_idx, analysis_total)
                continue
            if was_analyzing:
                print(f"\r[+] Done ({analysis_idx} analyzed)" + " " * 10)
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
