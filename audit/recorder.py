"""Records what the worker did, in the worker's vocabulary rather than the store's.

worker.py says "this search was released because the API went out of reach"; which table
and which columns that becomes lives here and nowhere else. The outbox stores fragments of
a row, so a run, a search attempt and an analysis are each opened with the columns known
at the time and completed when they end.

The first fragment of a row is its INSERT, so it carries every NOT NULL column of its
table — the ones with a default included, to keep the rule free of exceptions somebody has
to remember. Later fragments carry only what they add.

Nothing here raises at the worker. A failure to record is a warning under the "audit"
logger hierarchy and nothing else: a lead must never be reported as failed because its
audit row could not be written.

Without start() every function is a no-op, so the tests and any other entry point are
unaffected by the audit existing at all.
"""

import logging
import socket
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps

from audit.outbox import Outbox
from config import API_BASE_URL, APP_VERSION, AUDIT_DATABASE_URL, ENVIRONMENT
from scraper.card_issues import maps_card_issues
from scraper.domains import normalize_domain

logger = logging.getLogger(__name__)

# last_seen_at is what tells a run that ended from one that died without saying so, so it
# is written often enough to be a liveness signal and no more: the poll comes round every
# POLL_INTERVAL seconds, and a fragment on each would be traffic rather than information.
HEARTBEAT_INTERVAL = 30.0

# What stop() gives the sync thread to finish the upload before the rest is left for the
# next run. The worker is on its way out; this is a courtesy, not a guarantee.
_STOP_TIMEOUT = 10.0

# Constants rather than a validated argument: log_entries.kind has a CHECK, so a typo
# would surface as a fragment Neon refuses hours later instead of a name that is not here.
PAUSE_RATE_LIMIT = "pause_rate_limit"
PAUSE_PAYMENT = "pause_payment"

_PAUSE_MESSAGES = {
    PAUSE_RATE_LIMIT: "Rate limited; waiting {seconds:g}s before claiming again",
    PAUSE_PAYMENT: "Out of OpenRouter credit; waiting {seconds:g}s before claiming again",
}

_outbox: Outbox | None = None
_syncer = None
_run_id: str | None = None
_last_heartbeat = 0.0

# The run is module state because there is one per process; the work in progress is not,
# because the tasks that come next — the log handler, and the generator's llm_calls — read
# it from wherever they happen to run rather than being handed it.
_search_attempt_id: ContextVar[str | None] = ContextVar("audit_search_attempt", default=None)
_analysis_id: ContextVar[str | None] = ContextVar("audit_analysis", default=None)
_analysis_clock: ContextVar[float | None] = ContextVar("audit_analysis_clock", default=None)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _describe(error: BaseException | str) -> str:
    """The error as one line, naming its class when it is an exception.

    search_attempts has a single free-text error column and no error_type of its own —
    unlike analyses — so the class name has nowhere else to go.
    """
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: {error}"
    return str(error)


def _guarded(fn):
    """Make a recording function a no-op before start(), and silent about its own failures."""
    @wraps(fn)
    def recorded(*args, **kwargs):
        if _run_id is None:
            return None
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.warning("Could not record %s", fn.__name__, exc_info=True)
            return None
    return recorded


# ── The run ───────────────────────────────────────────────────────────────────

def start(outbox: Outbox | None = None, dsn: str | None = None) -> str | None:
    """Open a run and begin uploading in the background.

    Args:
        outbox: Where fragments are written; this machine's own outbox when omitted.
        dsn: The shared store to drain into; AUDIT_DATABASE_URL when omitted. Empty means
            everything stays in the local file, which is a working configuration.

    Returns:
        The run id, or None when the run could not be opened — in which case every other
        function stays a no-op and the worker runs unaudited rather than not running.
    """
    global _outbox, _run_id, _last_heartbeat
    if _run_id is not None:
        return _run_id

    try:
        run_id = str(uuid.uuid4())
        box = outbox if outbox is not None else Outbox()
        now = _utcnow()
        box.put("runs", run_id, {
            "machine": socket.gethostname(),
            "environment": ENVIRONMENT,
            "api_base_url": API_BASE_URL,
            "worker_version": APP_VERSION,
            "started_at": now,
            "last_seen_at": now,
        })
        _outbox, _run_id, _last_heartbeat = box, run_id, time.monotonic()
        _start_syncer(AUDIT_DATABASE_URL if dsn is None else dsn)
        return run_id
    except Exception:
        logger.warning("Could not open the audit run; this run is not audited", exc_info=True)
        _outbox, _run_id = None, None
        return None


def _start_syncer(dsn: str) -> None:
    """Start draining the outbox, if there is anywhere to drain it to.

    Imported here rather than at module scope so psycopg is only needed by a machine that
    has a DSN: without one the audit is a local file and nothing else. A sync that cannot
    start leaves the run recording locally — which is the whole point of the outbox.
    """
    global _syncer
    if not dsn:
        return
    try:
        from audit.sync import Syncer

        _syncer = Syncer(_outbox, dsn)
        _syncer.start()
    except Exception:
        logger.warning(
            "Could not start the audit sync; records stay in the local outbox", exc_info=True,
        )
        _syncer = None


@_guarded
def heartbeat() -> None:
    """Say the run is still alive, at most once every HEARTBEAT_INTERVAL seconds."""
    global _last_heartbeat
    now = time.monotonic()
    if now - _last_heartbeat < HEARTBEAT_INTERVAL:
        return
    _last_heartbeat = now
    _outbox.put("runs", _run_id, {"last_seen_at": _utcnow()})


def stop(end_reason: str) -> int:
    """Close the run, drain what the time allows, and say what is left.

    Args:
        end_reason: 'stopped' when the operator asked for it, 'crashed' otherwise.

    Returns:
        How many fragments are still waiting to go up — they travel with the next run —
        0 when there is nothing to report, or -1 when the outbox could not be counted.
    """
    global _outbox, _syncer, _run_id
    if _run_id is None:
        return 0

    try:
        _outbox.put("runs", _run_id, {"ended_at": _utcnow(), "end_reason": end_reason})
        return _syncer.stop(timeout=_STOP_TIMEOUT) if _syncer else _outbox.pending_count()
    except Exception:
        logger.warning("Could not close the audit run", exc_info=True)
        return -1
    finally:
        _outbox, _syncer, _run_id = None, None, None
        _search_attempt_id.set(None)
        _analysis_id.set(None)
        _analysis_clock.set(None)


# ── Work in progress ──────────────────────────────────────────────────────────

def current_search_attempt_id() -> str | None:
    """The search attempt being worked on, for the rows that hang off it."""
    return _search_attempt_id.get()


def current_analysis_id() -> str | None:
    """The analysis being worked on, for the rows that hang off it."""
    return _analysis_id.get()


@_guarded
def search_started(job: dict) -> None:
    """Open the row for this run's attempt at a search job.

    A search released and claimed again later is a new attempt, not a second visit to
    this row: what the store is asked is "what happened each time this was worked on".
    """
    attempt_id = str(uuid.uuid4())
    _outbox.put("search_attempts", attempt_id, {
        "run_id": _run_id,
        "api_search_id": str(job.get("id", "")),
        "profession": job.get("profession") or "",
        "city": job.get("city") or "",
        "max_results": job.get("max_results"),
        "started_at": _utcnow(),
        "cards_seen": 0,
        "cards_skipped_known": 0,
        "leads_reported": 0,
        "leads_inserted": 0,
    })
    _search_attempt_id.set(attempt_id)


@_guarded
def search_finished(
    outcome: str,
    *,
    cards_seen: int = 0,
    cards_skipped_known: int = 0,
    leads_reported: int = 0,
    leads_inserted: int = 0,
    error: BaseException | str | None = None,
) -> None:
    """Close the search attempt with how it ended and what it found."""
    attempt_id = _search_attempt_id.get()
    if attempt_id is None:
        return

    # Cleared before the write, so an attempt is never left current by a fragment that
    # could not be stored.
    _search_attempt_id.set(None)
    fields = {
        "ended_at": _utcnow(),
        "outcome": outcome,
        "cards_seen": cards_seen,
        "cards_skipped_known": cards_skipped_known,
        "leads_reported": leads_reported,
        "leads_inserted": leads_inserted,
    }
    if error is not None:
        fields["error"] = _describe(error)
    _outbox.put("search_attempts", attempt_id, fields)


@_guarded
def card_reported(lead: dict) -> None:
    """Record a Maps card the scrape kept and handed to the ingest.

    ``maps_issues`` is computed here as well as at the ingest, from the same dict and by
    the same pure function, and the duplication is deliberate: it is the only reading of
    the card that outlives the 90-day purge — ``raw``, the name, the website, the phone
    and the address are all cleared — so it is what "how many cards in this province had
    no website" is answered from years later.

    This is not the second reading the ingest rule warns about. That rule is about
    deriving the findings from the job the API hands back, where a website an agent typed
    into the panel has already replaced what the listing showed; here the argument is the
    scraped card itself, in the same pass that reports it.
    """
    attempt_id = _search_attempt_id.get()
    if attempt_id is None:
        return

    _outbox.put("cards", str(uuid.uuid4()), {
        "search_attempt_id": attempt_id,
        "seen_at": _utcnow(),
        "outcome": "reported",
        "maps_issues": maps_card_issues(lead),
        "domain": normalize_domain(lead.get("website") or "") or None,
        "business_name": lead.get("lead") or None,
        "website": lead.get("website") or None,
        "phone": lead.get("phone") or None,
        "address": lead.get("address") or None,
        "raw": lead,
    })


@_guarded
def card_skipped_known(domain: str) -> None:
    """Record a Maps card dropped because the system already has its domain.

    The domain is all there is: the question is answered from the listing, before the
    card's own tab is opened, so the business's name, phone and address were never read.
    It arrives normalised, since that is what the scraper asks the predicate about.
    """
    attempt_id = _search_attempt_id.get()
    if attempt_id is None:
        return

    _outbox.put("cards", str(uuid.uuid4()), {
        "search_attempt_id": attempt_id,
        "seen_at": _utcnow(),
        "outcome": "skipped_known_domain",
        "domain": domain or None,
    })


@_guarded
def analysis_started(job: dict) -> None:
    """Open the row for this attempt at analysing a lead."""
    analysis_id = str(uuid.uuid4())
    _outbox.put("analyses", analysis_id, {
        "run_id": _run_id,
        "api_lead_id": str(job.get("id", "")),
        "api_search_id": job.get("lead_search_id") or None,
        "started_at": _utcnow(),
        "business_name": job.get("business_name") or None,
        "website": job.get("website") or None,
    })
    _analysis_id.set(analysis_id)
    _analysis_clock.set(time.monotonic())


@_guarded
def analysis_finished(
    outcome: str, *, error: BaseException | None = None, result: dict | None = None,
) -> None:
    """Close the analysis with how it ended, how long it took and what the API was sent.

    Args:
        outcome: One of the store's analyses outcomes.
        error: The exception that ended it, split into its class and its message.
        result: The payload handed to the API — recorded before the call rather than
            after, because on a refused report it is the one thing worth having.
    """
    analysis_id = _analysis_id.get()
    if analysis_id is None:
        return

    started = _analysis_clock.get()
    _analysis_id.set(None)
    _analysis_clock.set(None)

    fields: dict = {"ended_at": _utcnow(), "outcome": outcome}
    # Measured on the monotonic clock: the wall clock can step while a lead is being
    # analysed, and a negative duration is worse than none.
    if started is not None:
        fields["duration_ms"] = round((time.monotonic() - started) * 1000)
    if error is not None:
        fields["error_type"] = type(error).__name__
        fields["error_message"] = str(error)
    if result is not None:
        fields["result"] = result
    _outbox.put("analyses", analysis_id, fields)


@_guarded
def pause(kind: str, seconds: float) -> None:
    """Record a pause the loop takes to wait out a limit it cannot do anything about.

    Filed under whatever is in progress, which for the loop's own pauses is the run
    alone: the lead that hit the limit has already been released and closed by the time
    the wait starts, and the two are read together by their timestamps.
    """
    _outbox.put("log_entries", str(uuid.uuid4()), {
        "run_id": _run_id,
        "logged_at": _utcnow(),
        "kind": kind,
        "level": "WARNING",
        "logger": "worker",
        "search_attempt_id": _search_attempt_id.get(),
        "analysis_id": _analysis_id.get(),
        "message": _PAUSE_MESSAGES.get(kind, "Waiting {seconds:g}s").format(seconds=seconds),
    })
