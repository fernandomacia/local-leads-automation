"""Records what the worker did, in the worker's vocabulary rather than the store's.

worker.py says "this search was released because the API went out of reach"; which table
and which columns that becomes lives here and nowhere else. The outbox stores fragments of
a row, so a run, a search attempt and an analysis are each opened with the columns known
at the time and completed when they end.

The first fragment of a row is its INSERT, so it carries every NOT NULL column of its
table — the ones with a default included, to keep the rule free of exceptions somebody has
to remember. Later fragments carry only what they add.

Fragments are written to the local outbox and sent on from there by ``audit.upload``, on a
thread of its own. ``_start_upload`` and ``_stop_upload`` are the seam, so worker.py talks
to this module and nothing else — and whatever a run cannot send is simply still pending
when the next one starts.

Nothing here raises at the worker. A failure to record is a warning under the "audit"
logger hierarchy and nothing else: a lead must never be reported as failed because its
audit row could not be written.

Without start() every function is a no-op, so the tests and any other entry point are
unaffected by the audit existing at all.
"""

import hashlib
import logging
import socket
import time
import traceback
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps

from audit.log_handler import AuditLogHandler
from audit.outbox import Outbox
from audit.upload import Uploader
from config import APP_VERSION
from scraper.card_issues import maps_card_issues
from scraper.domains import normalize_domain

logger = logging.getLogger(__name__)

# last_seen_at is what tells a run that ended from one that died without saying so, so it
# is written often enough to be a liveness signal and no more: the poll comes round every
# POLL_INTERVAL seconds, and a fragment on each would be traffic rather than information.
HEARTBEAT_INTERVAL = 30.0

# Constants rather than a validated argument: log_entries.kind is a constrained column, so
# a typo would surface as a row the API refuses later instead of a name that is not here.
PAUSE_RATE_LIMIT = "pause_rate_limit"
PAUSE_PAYMENT = "pause_payment"

_PAUSE_MESSAGES = {
    PAUSE_RATE_LIMIT: "Rate limited; waiting {seconds:g}s before claiming again",
    PAUSE_PAYMENT: "Out of OpenRouter credit; waiting {seconds:g}s before claiming again",
}

_outbox: Outbox | None = None
_run_id: str | None = None
_uploader: Uploader | None = None
_last_heartbeat = 0.0
_log_handler: AuditLogHandler | None = None

# The run is module state because there is one per process; the work in progress is not,
# because the tasks that come next — the log handler, and the generator's llm_calls — read
# it from wherever they happen to run rather than being handed it.
_search_attempt_id: ContextVar[str | None] = ContextVar("audit_search_attempt", default=None)
_analysis_id: ContextVar[str | None] = ContextVar("audit_analysis", default=None)
_analysis_clock: ContextVar[float | None] = ContextVar("audit_analysis_clock", default=None)
_llm_call_id: ContextVar[str | None] = ContextVar("audit_llm_call", default=None)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── The contract's types ──────────────────────────────────────────────────────
#
# The API types every column and refuses a value of the wrong one: `"3"` is not an integer
# and `1` is not a boolean. Most of what is recorded here is built in this module and is
# right by construction, but three sources are not — the job dicts the API hands back, the
# scraped card, and OpenRouter's `usage` block — and a float where an integer goes costs the
# whole fragment, hours later, with the reason in a log nobody is reading.
#
# So every value from outside is passed through one of these on its way in. They coerce what
# can be read and answer None for what cannot, which drops that one field rather than the
# fragment carrying it: a token count that arrived unreadable is worth less than the row.


def _as_int(value) -> int | None:
    """An integer for a column typed as integer, or None when the value is not one.

    `isinstance(True, int)` is True in Python, so booleans are excluded first — the API
    would refuse one, and a count of True is not a count. A whole float is accepted because
    JSON has one number type and OpenRouter sends token counts through it.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_number(value) -> int | float | None:
    """A number for a column typed as numeric — a cost, a temperature — or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_bool(value) -> bool | None:
    """A boolean for a column typed as boolean, and nothing else.

    Deliberately not truthiness: `parsed_ok` answers a question, and reading a non-empty
    string as True would invent an answer rather than record one.
    """
    return value if isinstance(value, bool) else None


def _as_text(value) -> str | None:
    """Text for a column typed as text, with empty read as absent."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text or None


def _as_uuid(value) -> str | None:
    """An id the API will accept as a uuid, or None.

    Checked rather than passed through: these are the columns the API resolves against its
    own rows, so a malformed one is not a field it drops but a fragment it refuses — and the
    row it belongs to is then missing from the trail entirely.
    """
    text = _as_text(value)
    if text is None:
        return None
    try:
        uuid.UUID(text)
    except ValueError:
        return None
    return text


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

def start(
    outbox: Outbox | None = None, *, api_base_url: str = "", api_token: str = "",
) -> str | None:
    """Open a run and start filing what the worker does under it.

    There is one environment per API, so the run says which machine it ran on and which
    version of the worker it was — the API it reported to is the one holding the record.

    The API is passed in rather than read from `config`: nothing in `audit/` imports it,
    which is what keeps this package a leaf the tests can drive without an env file. The
    base URL also decides *which* outbox is opened, because fragments written against one
    API can only be sent to that one.

    Args:
        outbox: Where fragments are written; the outbox for `api_base_url` when omitted.
        api_base_url: The API the fragments are destined for. Without it nothing is
            uploaded and the fragments stay local, which is what the tests do.
        api_token: The worker token, the same one `api/client.py` sends.

    Returns:
        The run id, or None when the run could not be opened — in which case every other
        function stays a no-op and the worker runs unaudited rather than not running.
    """
    global _outbox, _run_id, _last_heartbeat
    if _run_id is not None:
        return _run_id

    try:
        run_id = str(uuid.uuid4())
        box = outbox if outbox is not None else Outbox(api_base_url=api_base_url)
        now = _utcnow()
        box.put("runs", run_id, {
            "machine": socket.gethostname(),
            "worker_version": APP_VERSION,
            "started_at": now,
            "last_seen_at": now,
        })
        _outbox, _run_id, _last_heartbeat = box, run_id, time.monotonic()
        _install_log_handler()
        _start_upload(box, api_base_url, api_token)
        return run_id
    except Exception:
        logger.warning("Could not open the audit run; this run is not audited", exc_info=True)
        _outbox, _run_id = None, None
        return None


# ── Sending it on ─────────────────────────────────────────────────────────────
#
# `audit.upload` does the work; these two are the seam, so the recorder stays the only
# thing worker.py talks to. Each API holds the audit of the worker that reports to it, so
# a local run goes up locally and a production run to production — which the outbox
# enforces by being per API, not this.

def _start_upload(outbox: Outbox, api_base_url: str, api_token: str) -> None:
    """Begin sending fragments to the API, if there is one to send them to.

    Without a base URL or a token nothing is uploaded and the fragments stay in the
    outbox: that is the state the tests run in, and the state a misconfigured worker runs
    in rather than failing to start.
    """
    global _uploader
    if not api_base_url or not api_token:
        return
    try:
        _uploader = Uploader(outbox, api_base_url, api_token)
        _uploader.start()
    except Exception:
        logger.warning("Could not start sending the audit; it stays local", exc_info=True)
        _uploader = None


def _stop_upload() -> int:
    """Send what is left before the worker goes, and say how much still is.

    Returns:
        Fragments still pending after the flush, or -1 when nothing could be counted.
        None of it when there was no uploader, in which case the caller counts the outbox
        itself — the fragments are simply all still there.
    """
    global _uploader
    uploader, _uploader = _uploader, None
    if uploader is None:
        return -1
    try:
        return uploader.stop()
    except Exception:
        logger.warning("Could not stop the audit uploader cleanly", exc_info=True)
        return -1


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
    """Close the run and say how much of it is still waiting to be sent.

    The closing fragment is written first and shielded from ``BaseException``, not from
    ``Exception``, and that is the guard that matters: a second Ctrl+C — which is what an
    operator presses when stopping is not instant — landed in this function and left the
    run with no ``ended_at``, which is precisely how the audit records a worker that
    *died*. A run stopped on purpose and filed as a crash is a wrong answer, not a
    missing one. The interrupt still leaves, because stopping is what was asked for; it
    just no longer takes the record with it.

    Args:
        end_reason: 'stopped' when the operator asked for it, 'crashed' otherwise.

    Returns:
        How many fragments are still in the local outbox — they travel with the next run
        — 0 when there is nothing to report, or -1 when the outbox could not be counted.
    """
    global _outbox, _run_id
    if _run_id is None:
        return 0

    outbox, run_id = _outbox, _run_id
    closing = {"ended_at": _utcnow(), "end_reason": end_reason}

    def write_closing() -> bool:
        """Write the fragment, answering a broken outbox the way everything here does."""
        try:
            outbox.put("runs", run_id, closing)
            return True
        except Exception:
            logger.warning("Could not close the audit run", exc_info=True)
            return False

    closed = False
    try:
        try:
            closed = write_closing()
        except BaseException:
            # Nothing but an interrupt reaches this: an ordinary failure is already
            # answered with False. Written once more and then let through — a duplicate
            # closing fragment says the same thing twice, a missing one says the worker
            # never came back.
            write_closing()
            raise
    finally:
        _remove_log_handler()
        _outbox, _run_id = None, None
        _search_attempt_id.set(None)
        _analysis_id.set(None)
        _analysis_clock.set(None)
        _llm_call_id.set(None)

    # After the closing fragment is written, so the flush carries it: the run's own
    # `ended_at` is the last thing that should still be sitting on this machine.
    flushed = _stop_upload()
    if not closed:
        return -1
    if flushed >= 0:
        return flushed
    try:
        return outbox.pending_count()
    except Exception:
        logger.warning("Could not count what the audit has left to send", exc_info=True)
        return -1


# ── Warnings and errors ───────────────────────────────────────────────────────

def _record_log(record: logging.LogRecord) -> None:
    """File one log record as a row, under whatever work was in hand when it was logged.

    The run is module state, so it is visible from any thread. The search attempt and the
    analysis are context variables, and a thread does not inherit the context of the one
    that started it: a record logged from a thread the worker spawned carries no work ids
    and is filed under the run alone. That is the honest answer — better than guessing an
    analysis — and it is why a record can never be attached to the wrong lead.
    """
    if _run_id is None or _outbox is None:
        return

    fields = {
        "run_id": _run_id,
        "logged_at": _utcnow(),
        "kind": "log",
        "level": record.levelname,
        "logger": record.name,
        "search_attempt_id": _search_attempt_id.get(),
        "analysis_id": _analysis_id.get(),
        "message": record.getMessage(),
    }
    if record.exc_info and record.exc_info[0] is not None:
        fields["exception_type"] = record.exc_info[0].__name__
        fields["exception_text"] = "".join(traceback.format_exception(*record.exc_info))
    _outbox.put("log_entries", str(uuid.uuid4()), fields)


def _install_log_handler() -> None:
    """Put the audit on the root logger, once per run."""
    global _log_handler
    if _log_handler is not None:
        return
    _log_handler = AuditLogHandler(_record_log)
    logging.getLogger().addHandler(_log_handler)


def _remove_log_handler() -> None:
    global _log_handler
    if _log_handler is None:
        return
    logging.getLogger().removeHandler(_log_handler)
    _log_handler = None


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
    # The API resolves this against its own lead_searches and refuses the fragment if it
    # cannot, so a job with no usable id has no recordable attempt at all. Recording
    # nothing is better than a row that can only ever be rejected, and the warning says
    # which — under "audit", so it never reaches the operator's terminal.
    search_id = _as_uuid(job.get("id"))
    if search_id is None:
        logger.warning("Search job with no usable id; this attempt is not audited")
        return

    attempt_id = str(uuid.uuid4())
    _outbox.put("search_attempts", attempt_id, {
        "run_id": _run_id,
        "lead_search_id": search_id,
        "profession": _as_text(job.get("profession")) or "",
        "city": _as_text(job.get("city")) or "",
        "max_results": _as_int(job.get("max_results")),
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
        "cards_seen": _as_int(cards_seen) or 0,
        "cards_skipped_known": _as_int(cards_skipped_known) or 0,
        "leads_reported": _as_int(leads_reported) or 0,
        "leads_inserted": _as_int(leads_inserted) or 0,
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
        "domain": normalize_domain(_as_text(lead.get("website")) or "") or None,
        "business_name": _as_text(lead.get("lead")),
        "website": _as_text(lead.get("website")),
        "phone": _as_text(lead.get("phone")),
        "address": _as_text(lead.get("address")),
        # Not one more detail among the rest: it is the only thing tying this card to the
        # lead it became, because the card is recorded before that lead exists and carries
        # no lead id. The API matches on it to empty a card's content when its lead is
        # deleted, so a card stored without it is personal data that erasure cannot reach.
        "maps_url": _as_text(lead.get("maps_url")),
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
    # Same as a search attempt: the API resolves lead_id against its own leads, so a job
    # with no usable id has no recordable analysis.
    lead_id = _as_uuid(job.get("id"))
    if lead_id is None:
        logger.warning("Analysis job with no usable id; this analysis is not audited")
        return

    analysis_id = str(uuid.uuid4())
    _outbox.put("analyses", analysis_id, {
        "run_id": _run_id,
        "lead_id": lead_id,
        "lead_search_id": _as_uuid(job.get("lead_search_id")),
        "started_at": _utcnow(),
        "business_name": _as_text(job.get("business_name")),
        "website": _as_text(job.get("website")),
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
    # A call left open by an analysis that ended is not the next analysis's call.
    _llm_call_id.set(None)

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
def llm_call_started(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> None:
    """Open the row for one call to the model, with the prompts it was given.

    Nothing is recorded outside an analysis: ``llm_calls.analysis_id`` is NOT NULL, so a
    call made with no lead in hand has no row to hang off, and leaving it unrecorded is
    better than a fragment the store refuses hours later.

    The hash is kept beside the prompt because it outlives it: the text is cleared after
    the retention window and the hash is not, so which version of the system prompt
    produced a given pitch stays answerable. There is no parameter for the headers, and
    deliberately: the API key travels in them.
    """
    analysis_id = _analysis_id.get()
    if analysis_id is None:
        return

    call_id = str(uuid.uuid4())
    _outbox.put("llm_calls", call_id, {
        "analysis_id": analysis_id,
        "started_at": _utcnow(),
        "model": _as_text(model) or "",
        "attempts": [],
        "temperature": _as_number(temperature),
        "max_tokens": _as_int(max_tokens),
        "system_prompt": system_prompt,
        "system_prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        "user_prompt": user_prompt,
    })
    _llm_call_id.set(call_id)


@_guarded
def llm_call_finished(
    *,
    attempts: list[dict],
    final_status: int | None = None,
    latency_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cost_usd: float | None = None,
    response_raw: str | None = None,
) -> None:
    """Close the HTTP half of the call: what was attempted, and what came back.

    Written from a ``finally``, so a call that ended in a timeout or a refusal still
    records the attempts it made and the status it got, when it got one. ``response_raw``
    is the content as the model sent it, before the code fences are stripped off.
    """
    call_id = _llm_call_id.get()
    if call_id is None:
        return

    _outbox.put("llm_calls", call_id, {
        "attempts": attempts if isinstance(attempts, list) else [],
        "final_status": _as_int(final_status),
        "latency_ms": _as_int(latency_ms),
        # OpenRouter's usage block is JSON from a third party: the token counts arrive as
        # numbers but not reliably as integers, and `cost` has been seen as a string.
        "prompt_tokens": _as_int(prompt_tokens),
        "completion_tokens": _as_int(completion_tokens),
        "cost_usd": _as_number(cost_usd),
        "response_raw": _as_text(response_raw),
    })


@_guarded
def llm_call_parsed(response_parsed: dict | None, *, parsed_ok: bool) -> None:
    """Close the call with what the worker made of the answer.

    ``parsed_ok`` False is an answer that could not be used, not a call that failed: the
    request succeeded, the tokens were spent and the lead is still reported, with an empty
    pitch. Nothing downstream can tell those two apart, which is why they are told apart
    here.
    """
    call_id = _llm_call_id.get()
    if call_id is None:
        return

    _llm_call_id.set(None)
    _outbox.put("llm_calls", call_id, {
        "response_parsed": response_parsed,
        "parsed_ok": _as_bool(parsed_ok),
    })


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
