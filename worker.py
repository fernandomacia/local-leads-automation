"""SegurSEO-API job queue worker: discovers and analyzes leads on demand.

Polls two job queues — Maps discovery and per-lead website analysis — and
keeps the API's lead database in sync. Runs continuously as a background
daemon, driven entirely by the API.
"""

import random
import time

import requests

from config import BATCH_SIZE, POLL_INTERVAL, SOCIAL_DOMAINS
from scraper.maps_scraper import scrape_incrementally, MAPS_ISSUE_LABELS
from scraper.web_analyzer import analyze
from ai.message_generator import generate
from api.client import (
    claim_next_search_job,
    report_leads,
    complete_search_job,
    fail_search_job,
    claim_next_analysis_job,
    report_payment_error,
    report_analysis,
)

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


def run_search_job(job: dict) -> None:
    """Discover businesses for a search job, reporting new leads in batches."""
    known = set(job["known_domains"])
    batch: list[dict] = []
    total = 0
    try:
        for lead in scrape_incrementally(
            job["profession"], job["city"], skip=known, max_results=job["max_results"]
        ):
            batch.append(map_to_api_shape(lead))
            total += 1
            if len(batch) >= BATCH_SIZE:
                report_leads(job["id"], batch)
                batch = []
        if batch:
            report_leads(job["id"], batch)
        complete_search_job(job["id"], total)
        print(f"[+] Search job {job['id']} complete: {total} new leads")
    except Exception as e:
        print(f"[!] Search job {job['id']} failed: {e}")
        try:
            fail_search_job(job["id"], str(e))
        except Exception:
            pass


def run_analysis_job(job: dict) -> None:
    """Analyze a single lead's website and generate its outreach message."""
    try:
        print(f"[~] Analyzing {job['business_name']} ({job['website'] or 'no website'})")
        analysis = analyze({"lead": job["business_name"], "website": job["website"]})

        cms = analysis.get("cms")
        maps = _maps_issues(job)
        print(f"    cms={cms!r}  seo_score={analysis.get('seo_score')!r}  email={analysis.get('email')!r}")
        print(f"    maps_issues={list(maps.keys())}")

        base_context = {
            **analysis,
            "city": job.get("city", ""),
            "profession": job.get("profession", ""),
            "maps_issues": maps,
        }

        if cms == "unreachable":
            if not (job.get("phone") or job.get("email")):
                print(f"[~] Site unreachable, no contact channel — skipping: {job['business_name']}")
                report_analysis(job["id"], {"failed": True})
                print(f"[+] Analysis done (unreachable, no contact): {job['business_name']}")
                return
            print(f"    Site unreachable — generating broken-website pitch...")
            message = generate({**base_context, "has_website": True})
            print(f"    script_len={len(message.get('phone_script', ''))}")
            report_analysis(job["id"], map_analysis_to_api_shape(analysis, message))
            print(f"[+] Analysis done (unreachable + pitch): {job['business_name']}")
            return

        if not job.get("website"):
            if not (job.get("phone") or job.get("email")):
                print(f"[~] No website and no contact channel — skipping: {job['business_name']}")
                report_analysis(job["id"], map_analysis_to_api_shape(analysis, {}))
                return
            print(f"    No website — generating phone script only...")
            message = generate({**base_context, "has_website": False})
            print(f"    script_len={len(message.get('phone_script', ''))}")
            report_analysis(job["id"], map_analysis_to_api_shape(analysis, message))
            print(f"[+] Analysis done (no website): {job['business_name']}")
            return

        socials = [f for f in _SOCIAL_FIELDS if analysis.get(f)]
        print(f"    socials={socials}")
        print(f"    Generating message (city={job.get('city')!r}, profession={job.get('profession')!r})...")
        message = generate({**base_context, "has_website": True})
        print(f"    subject={message.get('subject', '')[:60]!r}  body_len={len(message.get('body', ''))}  script_len={len(message.get('phone_script', ''))}")

        payload = map_analysis_to_api_shape(analysis, message)
        print(f"    Reporting keys: {list(payload.keys())}")
        report_analysis(job["id"], payload)
        print(f"[+] Analysis done: {job['business_name']}")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 402:
            print(f"[!] OpenRouter payment required — lead left pending for retry: {job['business_name']}")
            search_id = job.get("lead_search_id")
            if search_id:
                try:
                    report_payment_error(search_id)
                except Exception:
                    pass
            raise  # propagate so main() sleeps before retrying
        print(f"[!] Analysis job {job['id']} ({job['business_name']}) failed: {e}")
        try:
            report_analysis(job["id"], {"failed": True})
        except Exception as report_exc:
            print(f"[!] Could not report analysis failure for job {job['id']}: {report_exc}")
    except Exception as e:
        print(f"[!] Analysis job {job['id']} ({job['business_name']}) failed: {e}")
        try:
            report_analysis(job["id"], {"failed": True})
        except Exception as report_exc:
            print(f"[!] Could not report analysis failure for job {job['id']}: {report_exc}")


def main() -> None:
    """Poll the job queue forever, prioritizing search jobs over analysis jobs."""
    print("[+] Worker started, polling for jobs...")
    while True:
        try:
            if job := claim_next_search_job():
                run_search_job(job)
                continue
            if job := claim_next_analysis_job():
                run_analysis_job(job)
                continue
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 402:
                pause = 120
                print(f"[!] OpenRouter sin saldo — reintentando en {pause}s. Recarga créditos en openrouter.ai")
                time.sleep(pause)
                continue
            print(f"[!] API error ({e.__class__.__name__}), retrying in {POLL_INTERVAL}s...")
        except requests.RequestException as e:
            print(f"[!] API error ({e.__class__.__name__}), retrying in {POLL_INTERVAL}s...")
        time.sleep(POLL_INTERVAL + random.uniform(0, 2))


if __name__ == "__main__":
    main()
