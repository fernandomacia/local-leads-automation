"""Sends the local outbox on to the API the fragments were written for.

One daemon thread, started with the run and stopped with it. It takes the oldest pending
fragments in `seq` order, posts them in batches, and marks each one by what the API said
about it — accepted, or refused with the reason. Nothing else drains the outbox, so the
fragments a run could not send are simply still pending when the next run starts.

Three rules shape all of it:

- **It never raises at the worker and never delays it.** The thread owns every failure it
  can have; `start()` and `stop()` answer a broken outbox or an unreachable API the way the
  rest of `audit/` does, with a warning under the "audit" hierarchy — which the audit log
  handler skips, so an uploader that cannot upload does not file records about it.
- **Nothing is marked unless the API said something about that fragment.** A 401, a 422, a
  429, a 500, a timeout and a result the response left out are all "ask again later": the
  fragments stay pending and travel with the next run. The only two things that take a
  fragment out of the queue are an `accepted` and a `rejected`, both of which are answers.
- **A fragment is never sent to an API it was not written for.** That is the outbox's doing
  rather than this module's — one file per API, see `outbox.default_path()` — and it is why
  this takes the base URL and the token as arguments instead of reading `config`: `audit/`
  knows nothing about where the worker is pointed, and `recorder.start()` is handed both.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass

import requests

from audit.outbox import Outbox, PendingRecord

logger = logging.getLogger(__name__)

PATH = "/api/scraper/audit/fragments"

# The API's own caps are 200 fragments and 512 KB. The byte figure here is deliberately
# well under it: what is measured locally is the fragment list, and the request that
# arrives also carries the envelope, the headers and whatever the JSON encoder spends on
# escaping — so sending right up to the limit would earn a 413 for a batch that looked
# fine from here.
MAX_FRAGMENTS = 200
MAX_BYTES = 450_000

# A fragment this big on its own can never be sent, whatever it is batched with. It is
# marked failed locally rather than retried for ever: an `llm_calls` row carrying a
# pathological model answer is the realistic case, and the row's metadata — what it cost,
# how long it took — is already in the fragments around it.
_TOO_BIG = f"local: over the {MAX_BYTES} byte limit on its own"

# Nothing pending: long enough not to spin, short enough that a run which finishes its
# work still empties the outbox before it is asked to stop.
IDLE_SECONDS = 5.0

# A 401 or a 422 needs a person, not a retry: the token is wrong, or this worker is
# sending something the API cannot read. Waiting five minutes keeps the thread alive —
# so the fragments go up as soon as it is fixed — without hammering an endpoint that has
# already said what it thinks.
LONG_WAIT_SECONDS = 300.0

# Everything that says nothing about the request — a 500, a timeout, a refused connection
# — backs off from five seconds to five minutes. The API being restarted is the common
# case and the first retry usually lands.
BACKOFF_START_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 300.0

# Short to connect, generous to read: the API applies a batch inside one transaction with
# a savepoint per fragment, so a full 200 is not instant. A worker whose network is gone
# finds out in five seconds rather than thirty.
TIMEOUT = (5, 30)


@dataclass(frozen=True)
class _Outcome:
    """What one attempt achieved, and how long to wait before the next.

    `progressed` means something was marked, so there may be more to do at once.
    `wait` is how long this particular answer earns; the caller applies it, which is what
    lets the final flush at `stop()` ignore every wait and still share this code.
    """

    progressed: bool
    wait: float = 0.0


class Uploader:
    """Drains one outbox to one API, on a thread of its own."""

    def __init__(
        self,
        outbox: Outbox,
        base_url: str,
        token: str,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self._outbox = outbox
        self._url = f"{base_url.rstrip('/')}{PATH}"
        self._session = session or requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._backoff = BACKOFF_START_SECONDS
        # One line per streak rather than one per attempt: an API that has been down for an
        # hour would otherwise write 720 identical warnings, and the audit's own logs are
        # the ones nobody is watching.
        self._reported: set[str] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin draining, on a daemon thread. Never raises."""
        if self._thread is not None:
            return
        try:
            self._thread = threading.Thread(
                target=self._loop, name="audit-upload", daemon=True,
            )
            self._thread.start()
        except Exception:
            logger.warning("Could not start the audit uploader", exc_info=True)
            self._thread = None

    def stop(self, timeout: float = 10.0) -> int:
        """Stop draining, try to empty what is left, and say how much still is.

        The thread is asked to stop and joined first, and the last flush runs on *this*
        thread only once it is gone. Two threads draining at once would have each one
        marking fragments the other had in flight — the outbox opens a connection per
        operation, so it is not a shared-connection problem, it is a double-send.

        A thread still alive after the join is one inside a request it is entitled to
        finish. Nothing is flushed from here in that case, and the fragments travel with
        the next run, which is what pending has always meant.

        Returns:
            Fragments still waiting, or -1 when the outbox could not be counted.
        """
        self._stop.set()
        thread, self._thread = self._thread, None

        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                logger.warning("The audit uploader is still sending; leaving the rest pending")
                return self._pending_count()

        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline and self._cycle().progressed:
                pass
        except Exception:
            logger.warning("Could not flush the audit outbox before stopping", exc_info=True)

        return self._pending_count()

    # ── The thread ────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        """Drain until asked to stop. Owns every failure; nothing escapes to the worker."""
        try:
            self._outbox.prune()
        except Exception:
            logger.warning("Could not prune the audit outbox", exc_info=True)

        while not self._stop.is_set():
            try:
                outcome = self._cycle()
            except Exception:
                # A bug here must not take the thread down: the run would then record
                # everything and send none of it, with nothing saying so.
                logger.warning("The audit uploader hit an unexpected failure", exc_info=True)
                outcome = _Outcome(False, self._next_backoff())

            if outcome.wait:
                self._stop.wait(outcome.wait)
            elif not outcome.progressed:
                # Nothing pending. While there is work it sends without pausing.
                self._stop.wait(IDLE_SECONDS)

    def _cycle(self) -> _Outcome:
        """Send one batch, or report that there was nothing to send."""
        pending = self._outbox.pending(MAX_FRAGMENTS)
        if not pending:
            return _Outcome(False)

        batch, oversized = self._fit(pending)

        for record in oversized:
            logger.warning(
                "Audit fragment for %s is too large to send on its own; marked failed",
                record.target,
            )
            self._outbox.mark_failed(record.seq, _TOO_BIG)

        if not batch:
            # Every candidate was oversized: progress, since the queue is shorter.
            return _Outcome(bool(oversized))

        return self._send(batch)

    def _fit(self, pending: list[PendingRecord]) -> tuple[list[PendingRecord], list[PendingRecord]]:
        """Split the queue head into one batch and the fragments too large to ever send.

        Walked in `seq` order and stopped at the first fragment that does not fit in what
        is left, rather than skipping it: it goes in the next batch. Only one that exceeds
        the whole limit by itself is taken out, because no batch will ever hold it.
        """
        batch: list[PendingRecord] = []
        oversized: list[PendingRecord] = []
        total = 0

        for record in pending:
            size = len(json.dumps(_fragment(record), ensure_ascii=False).encode("utf-8"))
            if size > MAX_BYTES:
                oversized.append(record)
                continue
            if total + size > MAX_BYTES:
                break
            batch.append(record)
            total += size

        return batch, oversized

    # ── One request ───────────────────────────────────────────────────────────

    def _send(self, batch: list[PendingRecord]) -> _Outcome:
        """Post one batch and act on the answer. Marks nothing it was not told about."""
        payload = {"fragments": [_fragment(record) for record in batch]}

        try:
            response = self._session.post(self._url, json=payload, timeout=TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as exc:
            return self._retry_later("unreachable", f"The audit API is out of reach ({exc.__class__.__name__})")
        except requests.RequestException:
            logger.warning("The audit request could not be sent", exc_info=True)
            return _Outcome(False, self._next_backoff())

        status = response.status_code

        if status == 200:
            return self._apply(batch, response)

        if status == 401:
            # Not retried in a tight loop: the token is wrong and only a person fixes it.
            self._report("401", logging.WARNING, "The audit API refused the worker token (401)")
            return _Outcome(False, LONG_WAIT_SECONDS)

        if status == 413:
            return self._split(batch)

        if status == 422:
            # This worker is sending something the API cannot read, which is a bug here.
            # Nothing is marked, so no data is lost while it is being fixed.
            self._report(
                "422", logging.ERROR,
                "The audit API rejected the batch's structure (422); fragments kept pending",
            )
            return _Outcome(False, LONG_WAIT_SECONDS)

        if status == 429:
            return _Outcome(False, _retry_after(response) or self._next_backoff())

        if status >= 500:
            return self._retry_later("5xx", f"The audit API answered {status}")

        self._report(str(status), logging.WARNING, f"The audit API answered an unexpected {status}")
        return _Outcome(False, self._next_backoff())

    def _apply(self, batch: list[PendingRecord], response: requests.Response) -> _Outcome:
        """Mark each fragment by what the response said about it, and only that.

        A `seq` the response leaves out is left pending and reported: the API answers one
        result per fragment, so a gap is either a contract that changed or a response that
        was truncated, and guessing "accepted" would lose the fragment silently — which is
        the one outcome this whole module exists to avoid.
        """
        try:
            results = response.json()["data"]["results"]
            if not isinstance(results, list):
                # Not "every answer is missing": the body is not the shape the contract
                # describes, and treating it as missing answers would spin on it with no
                # wait at all.
                raise TypeError("results is not a list")
            by_seq = {
                result["seq"]: result
                for result in results
                if isinstance(result, dict) and isinstance(result.get("seq"), int)
            }
        except (ValueError, KeyError, TypeError):
            logger.warning("The audit API answered 200 with a body this worker cannot read")
            return _Outcome(False, self._next_backoff())

        self._reported.clear()
        self._backoff = BACKOFF_START_SECONDS

        synced: list[int] = []
        rejected: list[tuple[int, str]] = []
        missing: list[int] = []

        for record in batch:
            result = by_seq.get(record.seq)
            if result is None:
                missing.append(record.seq)
                continue
            status = result.get("status")
            if status == "accepted":
                synced.append(record.seq)
            elif status == "rejected":
                rejected.append((record.seq, str(result.get("reason") or "rejected")))
            else:
                # An unknown status is not an answer either.
                missing.append(record.seq)

        self._outbox.mark_synced(synced)
        for seq, reason in rejected:
            self._outbox.mark_failed(seq, reason)

        if rejected:
            logger.warning(
                "The audit API refused %d of %d fragments; the reasons are in the outbox",
                len(rejected), len(batch),
            )
        if missing:
            logger.warning(
                "The audit API answered about %d of %d fragments; the rest stay pending",
                len(batch) - len(missing), len(batch),
            )

        return _Outcome(bool(synced or rejected))

    def _split(self, batch: list[PendingRecord]) -> _Outcome:
        """Answer a 413 by halving the batch, down to the single fragment that earned it."""
        if len(batch) == 1:
            logger.warning(
                "The audit API refused one %s fragment for its size; marked failed", batch[0].target,
            )
            self._outbox.mark_failed(batch[0].seq, "413: the API refused this fragment on its own")
            return _Outcome(True)

        logger.warning("The audit API refused a batch of %d for its size; splitting", len(batch))

        return self._send(batch[: len(batch) // 2])

    # ── Waiting and saying so ─────────────────────────────────────────────────

    def _retry_later(self, kind: str, message: str) -> _Outcome:
        self._report(kind, logging.WARNING, message)
        return _Outcome(False, self._next_backoff())

    def _report(self, kind: str, level: int, message: str) -> None:
        """Log once per streak of the same answer; cleared by the next successful send."""
        if kind in self._reported:
            return
        self._reported.add(kind)
        logger.log(level, "%s; retrying later", message)

    def _next_backoff(self) -> float:
        wait = self._backoff
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_SECONDS)
        return wait

    def _pending_count(self) -> int:
        try:
            return self._outbox.pending_count()
        except Exception:
            logger.warning("Could not count what the audit has left to send", exc_info=True)
            return -1


def _fragment(record: PendingRecord) -> dict:
    """One record as the contract's fragment: the id travels beside `fields`, not inside."""
    return {
        "seq": record.seq,
        "table": record.target,
        "id": record.record_id,
        "fields": record.fields,
    }


def _retry_after(response: requests.Response) -> float | None:
    """The `Retry-After` seconds, when the header carries a number we can use."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except (TypeError, ValueError):
        # An HTTP-date is legal in the header and is not worth parsing: the caller falls
        # back to its own backoff, which is the same kind of answer.
        return None
    return max(0.0, min(seconds, BACKOFF_MAX_SECONDS))
