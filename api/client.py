"""API client for the SegurSEO-API scraper job queue.

Each function wraps one endpoint of the worker contract: claiming the next
job, reporting results, and signaling completion or failure.
"""

import requests

from config import API_BASE_URL, API_TOKEN

_HEADERS = {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}

# The API caps the failure message it stores. A Playwright traceback passes that easily, and
# the resulting 422 used to be swallowed by the caller — leaving the search claimed instead
# of failed, which is the opposite of the loud failure the call is for.
MAX_ERROR_MESSAGE = 1000


class ReportRejected(requests.HTTPError):
    """The API refused a report this worker sent.

    Raised instead of a plain HTTPError so the caller can tell "my payload is wrong" from
    "the lead could not be analysed". They are not the same thing and they do not deserve
    the same answer: reporting a refused payload as a failed analysis retires a lead that
    was read perfectly well.
    """


def _raise_with_body(resp: requests.Response, label: str, error=requests.HTTPError) -> None:
    """Raise with the response body attached.

    ``raise_for_status()`` swallows it, and the body is the only place the API says *which*
    field it refused — on a 422 the status alone leaves nothing to act on.
    """
    raise error(f"{label} → {resp.status_code}: {resp.text}", response=resp)


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
        _raise_with_body(resp, f"POST /jobs/{search_id}/leads")
    return resp.json()["data"]["created"]


def complete_search_job(search_id: str, results_count: int) -> None:
    """Mark a search job as complete with the total accumulated lead count."""
    resp = requests.post(
        f"{API_BASE_URL}/api/scraper/jobs/{search_id}/complete",
        json={"results_count": results_count}, headers=_HEADERS, timeout=30,
    )
    resp.raise_for_status()


def fail_search_job(search_id: str, error_message: str) -> None:
    """Mark a search job as failed with an error message.

    The message is cut to what the API stores. It is the tail of whatever exception ended
    the search, so it is routinely longer — and a 422 here is the worst one to earn: the
    search stays claimed, which reads as a run still in progress rather than a failed one.
    """
    resp = requests.post(
        f"{API_BASE_URL}/api/scraper/jobs/{search_id}/fail",
        json={"error_message": error_message[:MAX_ERROR_MESSAGE]}, headers=_HEADERS, timeout=30,
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
    """Submit the mapped analysis/outreach result for a single lead.

    Raises ``ReportRejected`` on any refusal, with the body: this is the call whose failure
    used to retire the lead, and it was the only one that did not say why.
    """
    resp = requests.patch(
        f"{API_BASE_URL}/api/scraper/leads/{lead_id}/analysis",
        json=analysis, headers=_HEADERS, timeout=30,
    )
    if not resp.ok:
        _raise_with_body(resp, f"PATCH /leads/{lead_id}/analysis", ReportRejected)
