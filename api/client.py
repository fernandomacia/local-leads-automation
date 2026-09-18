"""API client for the SegurSEO-API scraper job queue.

Each function wraps one endpoint of the worker contract: claiming the next
job, reporting results, and signaling completion or failure.
"""

import requests

from config import API_BASE_URL, API_TOKEN

_TIMEOUT = 30

# One connection, reused. The worker asks about every business it extracts — one request
# per Maps card — so a fresh connection per question would be a TLS handshake per card.
_SESSION = requests.Session()
_SESSION.headers.update({"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"})

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


def _request(method: str, path: str, *, json=None, error=requests.HTTPError) -> requests.Response:
    """Send one worker-contract request, or raise with the response body attached.

    The base URL, the token, the timeout and the failure handling were repeated in ten
    functions, and only one of them attached the body on a failure — the one whose 422
    somebody had had to diagnose. That is the wrong way round: the body is where the API
    names the field it refused, and ``raise_for_status()`` throws it away, so a 422 arrived
    as a status with nothing to act on. Every call carries it now.
    """
    resp = _SESSION.request(method, f"{API_BASE_URL}{path}", json=json, timeout=_TIMEOUT)

    if not resp.ok:
        raise error(f"{method} {path} → {resp.status_code}: {resp.text}", response=resp)

    return resp


def claim_next_search_job() -> dict | None:
    """Claim the next pending Maps-discovery job, or None if the queue is empty."""
    return _request("GET", "/api/scraper/jobs/next").json().get("data")


def report_leads(search_id: str, leads: list[dict]) -> int:
    """Submit a batch of mapped leads for a search job. Returns the count created."""
    resp = _request("POST", f"/api/scraper/jobs/{search_id}/leads", json={"leads": leads})

    return resp.json()["data"]["created"]


def complete_search_job(search_id: str, results_count: int) -> None:
    """Mark a search job as complete with the total accumulated lead count."""
    _request("POST", f"/api/scraper/jobs/{search_id}/complete", json={"results_count": results_count})


def fail_search_job(search_id: str, error_message: str) -> None:
    """Mark a search job as failed with an error message.

    The message is cut to what the API stores. It is the tail of whatever exception ended
    the search, so it is routinely longer — and a 422 here is the worst one to earn: the
    search stays claimed, which reads as a run still in progress rather than a failed one.
    """
    _request(
        "POST", f"/api/scraper/jobs/{search_id}/fail",
        json={"error_message": error_message[:MAX_ERROR_MESSAGE]},
    )


def release_search_job(search_id: str) -> None:
    """Hand a claimed search back to the queue so it is claimable again at once.

    Idempotent on the API side: a search that has meanwhile been completed, failed, or
    recovered and re-claimed is left exactly as it is and still answers 200.
    """
    _request("POST", f"/api/scraper/jobs/{search_id}/release")


def release_analysis_job(lead_id: str) -> None:
    """Hand a claimed lead back to the analysis queue, refunding the attempt it spent.

    A lead gets three attempts before the API gives up on it permanently, so a claim
    abandoned for a reason that has nothing to do with the lead — the worker being stopped,
    OpenRouter out of credit — must not cost it one.
    """
    _request("POST", f"/api/scraper/leads/{lead_id}/release")


def check_known_domains(domains: list[str]) -> list[str]:
    """Return the subset of the given URLs whose domains are already in the system."""
    resp = _request("POST", "/api/scraper/domains/check", json={"domains": domains})

    return resp.json()["data"]["known"]


def claim_next_analysis_job() -> dict | None:
    """Claim the next pending lead-analysis job, or None if the queue is empty."""
    return _request("GET", "/api/scraper/leads/next").json().get("data")


def report_payment_error(search_id: str) -> None:
    """Flag the parent search as blocked by an OpenRouter payment error."""
    _request("POST", f"/api/scraper/jobs/{search_id}/payment-error")


def report_analysis(lead_id: str, analysis: dict) -> None:
    """Submit the mapped analysis/outreach result for a single lead.

    Raises ``ReportRejected`` rather than a plain HTTPError: this is the call whose failure
    used to retire the lead, and telling a refused payload from a lead that could not be
    analysed is what stops it doing that again.
    """
    _request("PATCH", f"/api/scraper/leads/{lead_id}/analysis", json=analysis, error=ReportRejected)
