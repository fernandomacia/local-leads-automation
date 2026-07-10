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
    resp.raise_for_status()
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


def claim_next_analysis_job() -> dict | None:
    """Claim the next pending lead-analysis job, or None if the queue is empty."""
    resp = requests.get(f"{API_BASE_URL}/api/scraper/leads/next", headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data")


def report_analysis(lead_id: str, analysis: dict) -> None:
    """Submit the mapped analysis/outreach result for a single lead."""
    resp = requests.patch(
        f"{API_BASE_URL}/api/scraper/leads/{lead_id}/analysis",
        json=analysis, headers=_HEADERS, timeout=30,
    )
    resp.raise_for_status()
