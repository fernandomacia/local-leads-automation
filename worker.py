"""SegurSEO-API job queue worker: discovers and analyzes leads on demand.

Polls two job queues — Maps discovery and per-lead website analysis — and
keeps the API's lead database in sync. Runs continuously as a background
daemon, driven entirely by the API.
"""

# Before any import that reaches config.py, which loads the env file at import time.
# Guarded so importing this module (the tests do) never prompts.
if __name__ == "__main__":
    from env_select import choose_env_file

    choose_env_file()

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
from audit import recorder

logger = logging.getLogger(__name__)

_SOCIAL_FIELDS = tuple(SOCIAL_DOMAINS.keys())


# The progress line is rewritten in place, so every write pads to one width: a lead
# with a short name following one with a long name would otherwise leave the tail of
# the longer still on screen.
_LINE_WIDTH = 74
_BAR_WIDTH = 12
_NAME_WIDTH = 30

# Drawn with blocks where the stream can encode them. A daemon started under LANG=C
# cannot, and a UnicodeEncodeError raised while printing the counter would be caught by
# the analysis handler and reported to the API as a lead that failed to analyse.
try:
    "█░…─".encode(sys.stdout.encoding or "ascii")
    _BAR_FULL, _BAR_EMPTY, _ELLIPSIS, _RULE_CHAR = "█", "░", "…", "─"
except (UnicodeEncodeError, LookupError):
    _BAR_FULL, _BAR_EMPTY, _ELLIPSIS, _RULE_CHAR = "#", "-", "..", "-"

# Printed whether or not this is a terminal: a run piped to a file is read the same way,
# and the rule is what makes one search's lines tell from the next's.
_RULE = _RULE_CHAR * _LINE_WIDTH


# How long the loop waits out a limit it cannot do anything about. The payment pause is
# longer because topping up an account is manual; a rate limit lifts on its own.
_PAYMENT_PAUSE = 120
_RATE_LIMIT_PAUSE = 60


class _ProgressAwareHandler(logging.StreamHandler):
    """Clear the in-place counter before a log record lands on top of it.

    Logging writes to stderr and the counter to stdout, so on a terminal a warning
    arrived on the same row as the lead being analysed, starting at whatever column the
    counter had padded to and leaving the two spliced together.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if sys.stdout.isatty():
            print(f"\r{'':<{_LINE_WIDTH}}\r", end="", flush=True)
        super().emit(record)


def _progress(text: str) -> None:
    """Overwrite the current terminal line with a progress update.

    Suppressed entirely when stdout is not a terminal. Under systemd the stream
    is a pipe to journald, which does not honour the carriage return and treats
    an unterminated write as the start of the next line — so the counter would
    arrive as fragments glued onto whatever gets logged next.
    """
    if sys.stdout.isatty():
        print(f"\r{text:<{_LINE_WIDTH}}", end="", flush=True)


def _finish(text: str) -> None:
    """Print a final line or block, clearing the in-place counter it replaces.

    Only the first line is padded: it is the one that lands on top of the counter,
    and the lines after it start on a row of their own.
    """
    first, newline, rest = text.partition("\n")
    print(f"\r{first:<{_LINE_WIDTH}}{newline}{rest}" if sys.stdout.isatty() else text)


# A lead whose search was not scraped in this round — left queued by an earlier run —
# is counted apart: its total is unknown here, and attributing it to a search the
# operator just launched would be a wrong answer rather than a missing one.
_EARLIER = ""


def _plural(count: int, singular: str, plural: str = "") -> str:
    """Return the count with its noun agreeing: "1 lead", "4 leads"."""
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _tally(searches: dict, job: dict) -> dict:
    """Return the row this lead's counts belong to, creating it when the search is new.

    A search scraped in this round already has a row, with the leads it found. A lead the
    API hands back from an earlier run — the normal case after the worker is restarted
    mid-analysis, since analyses are claimed before searches — names its own search in
    its payload, so it gets a row of its own titled after it and no lead count: this
    worker never scraped it, and reporting a number there would be inventing one.
    Only a lead that names no search at all falls into the shared row.
    """
    search_id = job.get("lead_search_id") or ""
    if search_id in searches:
        return searches[search_id]

    profession, city = job.get("profession") or "", job.get("city") or ""
    label = f"{profession} en {city}" if profession and city else profession or city
    key = search_id if label else _EARLIER
    return searches.setdefault(key, {"label": label, "found": None, "analysed": 0, "failed": 0})


def _analysis_done_lines(searches: dict) -> str:
    """Return the line that closes a run of analyses, under the search it belongs to.

    One search per run is the normal case — its analyses finish before the next is
    claimed — so this is one line, read as the third of the three a search prints.
    A restart mid-analysis hands back leads from searches this run never headed with a
    Search line, and then each is named: without the label its count would read as
    belonging to whichever search came next.
    """
    named = len(searches) > 1
    lines = []
    for tally in searches.values():
        counts = f"{tally['analysed']} analysed"
        if tally["failed"]:
            counts += f", {tally['failed']} failed"
        label = tally["label"] or "from an earlier run"
        lines.append(f"[+] Analysis done — {label}: {counts}" if named
                     else f"[+] Analysis done: {counts}")
    return "\n".join(lines)


def _analysis_progress(idx: int, total: int, name: str) -> str:
    """Return the in-place line for the lead being analysed.

    The bar needs a denominator, and the analysis queue is global: a worker that starts
    on leads left behind by an earlier run has no total, and one handed more leads than
    the search it just scraped would run past the end of its own bar. Both fall back to
    a bare counter rather than a wrong bar — and so does an unnumbered call, which must
    never divide by its missing total: this line is printed from inside the analysis, so
    an exception raised here would be reported to the API as a lead that failed.
    """
    if len(name) > _NAME_WIDTH:
        name = name[:_NAME_WIDTH - len(_ELLIPSIS)] + _ELLIPSIS

    if idx < 1 or total < idx:
        return f"[>] Analysing{f' {idx}' if idx >= 1 else ''} — {name}"

    filled = round(_BAR_WIDTH * idx / total)
    bar = f"{_BAR_FULL * filled}{_BAR_EMPTY * (_BAR_WIDTH - filled)}"
    return f"[>] Analysing [{bar}] {idx}/{total} — {name}"


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
    "social_url":    (500,  False),
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

    # Sent only when the card carried a profile, unlike maps_issues below: the analysis
    # writes this column too, from the links on the business's own site, so an empty
    # object here would claim the card was read and found none where NULL leaves the
    # answer to the step that can still find one.
    if social := {platform: fitted
                  for platform, url in (lead.get("social_networks") or {}).items()
                  if (fitted := _fit("social_url", url)) is not None}:
        payload["social_networks"] = social

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
    recorder.search_started(job)
    # Asked about every business the scraper extracts, so a known one never consumes a slot
    # of max_results — see _known_domain_check. It does not save the detail tab, which has
    # to be opened to learn the website the domain comes from.
    batch: list[dict] = []
    total = 0
    seen = 0
    reported = 0
    # The outcome is recorded in a finally, so the path that re-raises is covered like the
    # ones that return; anything that does not name its own outcome is a failure.
    outcome, error = "failed", None
    try:
        for lead in scrape_incrementally(
            job["profession"], job["city"], headless=HEADLESS,
            is_known=_known_domain_check(), max_results=job["max_results"],
        ):
            seen += 1
            batch.append(map_to_api_shape(lead))
            if len(batch) >= BATCH_SIZE:
                total += _flush_batch(job["id"], batch)
                reported += len(batch)
                batch = []
        total += _flush_batch(job["id"], batch)
        reported += len(batch)
        complete_search_job(job["id"], total)
        outcome = "completed"
        print(f"[+] Search done: {_plural(total, 'lead')}")
    except KeyboardInterrupt:
        # KeyboardInterrupt is a BaseException, so it never reached the handler below and
        # the search stayed `scraping` until the API's stale-claim recovery noticed —
        # half an hour in which the operator who stopped the worker could not restart it
        # on the same search. Released and re-raised: stopping is what was asked for.
        outcome = "released_interrupted"
        _finish("[!] Interrupted — releasing the search")
        _release(release_search_job, job["id"], "search")
        raise
    except (requests.ConnectionError, requests.Timeout) as e:
        # The API went out of reach, which says nothing about the search. Marking it failed
        # would retire a perfectly good job over a network blip — and doing so means calling
        # the same API that just could not be reached. Released instead, best effort: if that
        # does not get through either, stale-claim recovery re-queues it.
        #
        # There is no attempt ceiling on a search, so a fault that looks transient and is
        # not would bounce the job for ever. It is throttled by its own cause: claiming a
        # search needs the API too, so while it is unreachable nothing re-runs, and each
        # round leaves a warning behind.
        outcome, error = "released_api_unreachable", e
        logger.warning("Lost the API during search %s; releasing it", job["id"], exc_info=True)
        _release(release_search_job, job["id"], "search")
    except Exception as e:
        # Anything else is the scrape itself: a selector that no longer matches, a consent
        # screen that changed, a payload the API refused. Failing the search is the point —
        # it carries the message to the panel, where somebody sees it. Bouncing it silently
        # instead would hide a broken scraper behind a queue that never empties.
        error = e
        logger.exception("Search job %s failed", job["id"])
        try:
            fail_search_job(job["id"], str(e))
        except Exception:
            pass
    finally:
        recorder.search_finished(
            outcome, cards_seen=seen, leads_reported=reported, leads_inserted=total, error=error,
        )
    return total


def run_analysis_job(job: dict, idx: int = 0, total: int = 0) -> bool:
    """Analyze a single lead's website and generate its outreach message.

    Args:
        idx: Position of this lead in the run, for the progress counter.
        total: Leads the run expects, or 0 when there is no way to know — see
            ``_analysis_progress``.

    Returns:
        Whether the lead was analysed — False when it was reported as failed, so the
        caller can count it against the search it came from. Counted there rather than
        printed here, so a run of fifty leads still leaves one closing block.
    """
    _progress(_analysis_progress(idx, total, job["business_name"]))
    recorder.analysis_started(job)
    # Recorded in a finally, so the three paths that re-raise are covered like the ones
    # that return; anything that does not name its own outcome is a lead given up on.
    # ``sent`` is assigned before the call it describes: on a refused report, the payload
    # the API would not take is the one thing worth having afterwards.
    outcome, error, sent = "failed", None, None
    try:
        # profession drives the legal-notice checks that only apply to regulated
        # professions (bar association and membership number, LSSI art. 10.1.c).
        analysis = analyze({
            "lead": job["business_name"],
            "website": job["website"],
            "profession": job.get("profession", ""),
        })
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
                sent = {"failed": True}
                report_analysis(job["id"], sent)
                return False
            message = generate({**base_context, "has_website": True})
            sent = map_analysis_to_api_shape(analysis, message)
            report_analysis(job["id"], sent)
            outcome = "analysed"
            return True

        if not job.get("website"):
            # Defensive, not reachable under the current API contract: a lead with no
            # website is only claimable when it has a phone or an email (the API's
            # Lead::hasContactChannel), and no-contact leads are failed at ingest time.
            # This condition mirrors that definition exactly. Kept because without it
            # such a lead would fall through to generate() and burn an LLM call producing
            # a pitch with no way to deliver it; reporting an empty message instead
            # settles the lead so it stops holding its parent search open.
            if not (job.get("phone") or job.get("email")):
                sent = map_analysis_to_api_shape(analysis, {})
                report_analysis(job["id"], sent)
                outcome = "analysed"
                return True
            message = generate({**base_context, "has_website": False})
            sent = map_analysis_to_api_shape(analysis, message)
            report_analysis(job["id"], sent)
            outcome = "analysed"
            return True

        # has_website comes from the analysis, not the raw job: analyze() blanks
        # "website" when the URL is a social profile, because a Facebook page is not a
        # site. Reading job["website"] here would report has_website=True and push the
        # generator into the "has a website" scenario, which pitches on SEO problems —
        # of which there are none, since a social URL yields an empty analysis. The
        # no-website scenario is the correct framing and the actual sales angle.
        message = generate({**base_context, "has_website": bool(analysis.get("website"))})
        sent = map_analysis_to_api_shape(analysis, message)
        report_analysis(job["id"], sent)
        outcome = "analysed"
        return True
    except KeyboardInterrupt:
        outcome = "released_interrupted"
        _finish("[!] Interrupted — releasing the lead")
        _release(release_analysis_job, job["id"], "lead")
        raise
    except ReportRejected as e:
        # Our payload is wrong; the lead is not. This used to answer {"failed": True},
        # which writes analysis_failed_at and retires a lead that was read perfectly well
        # — and nothing afterwards can tell that from a site that genuinely could not be.
        #
        # The claim is left standing on purpose. Releasing it would refund the attempt and
        # the re-claim would be refused identically, burning an LLM call per round for
        # ever; left alone, stale-claim recovery retries it and the three-attempt ceiling
        # retires it, with the refused fields in the log each time.
        outcome, error = "report_rejected", e
        logger.exception(
            "The API refused the analysis report for lead %s (%s); leaving the claim to expire",
            job["id"], job["business_name"],
        )
        return False
    except requests.HTTPError as e:
        # Both of these come from OpenRouter rather than from this API, and neither says
        # anything about the lead, so it goes back to the queue with its attempt refunded.
        # 402 is the credit running out mid-pitch; 429 is its rate limit, raised only
        # after the generator's own retries have given up. Answering 429 the way every
        # other HTTP error is answered — `failed: True` — retired a lead for good over a
        # limit that had lifted a minute later, and nothing afterwards can tell that from
        # a site that genuinely could not be read.
        status = e.response.status_code if e.response is not None else None
        error = e
        if status in (402, 429):
            outcome = "released_payment" if status == 402 else "released_rate_limit"
            if status == 402 and (search_id := job.get("lead_search_id")):
                try:
                    report_payment_error(search_id)
                except Exception:
                    pass
            _release(release_analysis_job, job["id"], "lead")
            raise
        logger.exception("Analysis job %s (%s) failed with HTTP error", job["id"], job["business_name"])
        sent = {"failed": True}
        try:
            report_analysis(job["id"], sent)
        except Exception:
            pass
    except Exception as e:
        error = e
        logger.exception("Analysis job %s (%s) failed", job["id"], job["business_name"])
        sent = {"failed": True}
        try:
            report_analysis(job["id"], sent)
        except Exception:
            pass
    finally:
        recorder.analysis_finished(outcome, error=error, result=sent)
    return False


def _audit_stopped(end_reason: str) -> None:
    """Close the audit run, and say what it could not upload before the worker went away.

    Records left in the outbox are not lost — the next run drains them before it starts
    on its own — but they are not in the store yet, and the line is what stops somebody
    looking for a run that is still on this machine.
    """
    pending = recorder.stop(end_reason)
    if pending > 0:
        print(f"[i] {_plural(pending, 'audit record')} still local — they go up on the next run")


def main() -> None:
    """Poll the job queue forever, finishing each search before claiming the next.

    Analyses are claimed first, so a search is scraped and then analysed to the end
    before another is started. The API already describes that order — a search goes
    pending, scraping, analyzing, completed — and claiming searches first left two of
    them sitting in `analyzing` at once, their leads analysed as one indistinguishable
    stream. The cost is that a second search waits for the first to finish rather than
    being discovered early.
    """
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[_ProgressAwareHandler()],
    )
    # Both printed on every start: the version to match a running worker to a release
    # when diagnosing a contract mismatch, and the target so an interactive run cannot
    # be scraping against development while its operator believes it is production.
    print(f"[+] Worker started (v{APP_VERSION}) — API: {API_BASE_URL}")
    # Only on a terminal, and only for Ctrl+C: under a service manager the way to stop
    # this is a stop command, which arrives as SIGTERM and kills the process outright —
    # the claim it was holding then waits for the API's stale-claim recovery instead of
    # being handed back. Printing the advice there would be printing the wrong advice.
    if sys.stdout.isatty():
        print("[i] Ctrl+C to stop — the job in hand is handed back to the queue")
    # After the lines above, so the audit never delays what the operator is waiting to
    # read, and after the env file has been chosen, which is what names the environment.
    recorder.start()
    analysis_idx = 0
    # Keyed by search id, so the closing block can name what each count belongs to;
    # emptied once its line is printed. See _analysis_done_lines.
    searches: dict[str, dict] = {}
    was_analyzing = False

    try:
        while True:
            try:
                if job := claim_next_analysis_job():
                    # Leads left queued by an earlier run arrive before any search has been
                    # claimed, so nothing has headed them: the rule is their only separator.
                    if not was_analyzing and not searches:
                        print(_RULE)
                    analysis_idx += 1
                    was_analyzing = True
                    # The denominator for the bar: the leads this round is expected to carry.
                    # Normally one search's own count, since its analyses finish before the
                    # next is claimed; a leftover from an earlier run has none, which
                    # _analysis_progress answers by dropping the bar.
                    expected = sum(s["found"] for s in searches.values() if s["found"])
                    tally = _tally(searches, job)
                    if run_analysis_job(job, analysis_idx, expected):
                        tally["analysed"] += 1
                    else:
                        tally["failed"] += 1
                    continue
                if job := claim_next_search_job():
                    if was_analyzing:
                        _finish(_analysis_done_lines(searches))
                    # Reset whether or not anything was analysed: a search that found no
                    # leads produces no analyses, and its row used to linger and be counted
                    # under the next search's block.
                    searches, analysis_idx, was_analyzing = {}, 0, False
                    print(_RULE)
                    searches[job["id"]] = {
                        "label": f"{job['profession']} en {job['city']}",
                        "found": run_search_job(job), "analysed": 0, "failed": 0,
                    }
                    continue
                if was_analyzing:
                    _finish(_analysis_done_lines(searches))
                    searches, analysis_idx, was_analyzing = {}, 0, False
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                if status == 402:
                    recorder.pause(recorder.PAUSE_PAYMENT, _PAYMENT_PAUSE)
                    time.sleep(_PAYMENT_PAUSE)
                    continue
                if status == 429:
                    # The rate limit is reckoned per minute, so moving straight to the next
                    # lead would spend it the same way. Said out loud because the run
                    # otherwise goes quiet for a minute with nothing to explain it.
                    _finish("[!] Rate limited — pausing for a minute")
                    recorder.pause(recorder.PAUSE_RATE_LIMIT, _RATE_LIMIT_PAUSE)
                    time.sleep(_RATE_LIMIT_PAUSE)
                    continue
                logger.error("API HTTP error in main loop: %s", e)
            except requests.RequestException as e:
                logger.warning("API connection error, retrying: %s", e)
            recorder.heartbeat()
            time.sleep(POLL_INTERVAL + random.uniform(0, 2))
    except KeyboardInterrupt:
        # Caught around the whole loop rather than inside it: the polling sleep, and the
        # longer ones a rate limit and a payment error wait out, all sat outside the old
        # handler — and an idle worker spends almost every second of its life in one of
        # them, so Ctrl+C answered with a traceback more often than not.
        #
        # The job in hand, if any, has already released itself on the way out. The counts
        # of everything analysed before it are still worth printing: an interrupted run is
        # the normal way to stop one, and discarding them left no record of what it did.
        closing = f"{_analysis_done_lines(searches)}\n" if was_analyzing else ""
        _finish(f"{closing}[+] Worker stopped")
        _audit_stopped("stopped")
    except Exception:
        # The audit is closed on the way out rather than left to look like a run that
        # died: a crash is a fact about this run, and it is the one the store cannot
        # infer for itself. Re-raised untouched — the traceback is the point.
        _audit_stopped("crashed")
        raise


if __name__ == "__main__":
    main()
