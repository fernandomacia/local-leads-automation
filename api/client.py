"""API client for the SegurSEO-API scraper job queue.

Each function wraps one endpoint of the worker contract: claiming the next
job, reporting results, and signaling completion or failure.
"""

import requests

from config import API_BASE_URL, API_TOKEN

_HEADERS = {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}


def claim_next_search_job() -> dict | None:
    """Claim the next pending Maps-discovery job, or None if the queue is empty."""
    resp = requests.get(f"{API_BASE_URL}/api/scraper/jobs/next", headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data")


def report_leads(search_id: str, leads: list[dict]) -> int:
    """Submit a batch of mapped leads for a search job. Returns the count created."""
    resp = requests.post(
        f"{API_BASE_URL}/api/scraper/jobs/{search_id}/leads",
        json={"leads": leads}, headers=_HEADERS, timeout=30,
    )
    if not resp.ok:
        # raise_for_status() swallows the response body; include it so API error
        # messages (validation details, 500 causes) reach the worker log.
        raise requests.HTTPError(
            f"POST /jobs/{search_id}/leads → {resp.status_code}: {resp.text}",
            response=resp,
        )
    return resp.json()["data"]["created"]


def complete_search_job(search_id: str, results_count: int) -> None:
    """Mark a search job as complete with the total accumulated lead count."""
    resp = requests.post(
        f"{API_BASE_URL}/api/scraper/jobs/{search_id}/complete",
        json={"results_count": results_count}, headers=_HEADERS, timeout=30,
    )
    resp.raise_for_status()


def fail_search_job(search_id: str, error_message: str) -> None:
    """Mark a search job as failed with an error message."""
    resp = requests.post(
        f"{API_BASE_URL}/api/scraper/jobs/{search_id}/fail",
        json={"error_message": error_message}, headers=_HEADERS, timeout=30,
    )
    resp.raise_for_status()


def release_search_job(search_id: str) -> None:
    """Hand a claimed search back to the queue so it is claimable again at once.

    Idempotent on the API side: a search that has meanwhile been completed, failed, or
    recovered and re-claimed is left exactly as it is and still answers 200.
    """
    resp = requests.post(
        f"{API_BASE_URL}/api/scraper/jobs/{search_id}/release", headers=_HEADERS, timeout=30,
    )
    resp.raise_for_status()


def release_analysis_job(lead_id: str) -> None:
    """Hand a claimed lead back to the analysis queue, refunding the attempt it spent.

    A lead gets three attempts before the API gives up on it permanently, so a claim
    abandoned for a reason that has nothing to do with the lead — the worker being stopped,
    OpenRouter out of credit — must not cost it one.
    """
    resp = requests.post(
        f"{API_BASE_URL}/api/scraper/leads/{lead_id}/release", headers=_HEADERS, timeout=30,
    )
    resp.raise_for_status()


def check_known_domains(domains: list[str]) -> list[str]:
    """Return the subset of the given URLs whose domains are already in the system."""
    resp = requests.post(
        f"{API_BASE_URL}/api/scraper/domains/check",
        json={"domains": domains}, headers=_HEADERS, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"]["known"]


def claim_next_analysis_job() -> dict | None:
    """Claim the next pending lead-analysis job, or None if the queue is empty."""
    resp = requests.get(f"{API_BASE_URL}/api/scraper/leads/next", headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data")


def report_payment_error(search_id: str) -> None:
    """Flag the parent search as blocked by an OpenRouter payment error."""
    resp = requests.post(
        f"{API_BASE_URL}/api/scraper/jobs/{search_id}/payment-error",
        headers=_HEADERS, timeout=30,
    )
    resp.raise_for_status()


def report_analysis(lead_id: str, analysis: dict) -> None:
    """Submit the mapped analysis/outreach result for a single lead."""
    resp = requests.patch(
        f"{API_BASE_URL}/api/scraper/leads/{lead_id}/analysis",
        json=analysis, headers=_HEADERS, timeout=30,
    )
    resp.raise_for_status()
