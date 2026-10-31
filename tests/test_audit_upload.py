"""Tests for audit/upload.py — a fake transport, a real outbox in tmp_path, no network.

Every test drives `_cycle()` by hand rather than letting the thread run: what each answer
earns is returned as an `_Outcome`, so the wait can be asserted instead of slept through.
The one test that does start the thread is the one about stopping, which is the only
behaviour the thread itself owns.

Nothing here touches a real outbox — the paths are all under `tmp_path` — and nothing
reaches the network: `requests` is never given the chance, because the session is injected.
"""

import json
import logging

import pytest
import requests

from audit.outbox import Outbox
from audit.upload import (
    BACKOFF_START_SECONDS, IDLE_SECONDS, LONG_WAIT_SECONDS, MAX_BYTES, MAX_FRAGMENTS, PATH,
    Uploader,
)

_BASE_URL = "http://127.0.0.1:8000"
_TOKEN = "test-worker-token"


class _Response:
    """Just enough of requests.Response for the uploader to read."""

    def __init__(self, status_code: int, body=None, headers: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def accepted(*seqs: int) -> _Response:
    return _Response(200, {"data": {"results": [
        {"seq": seq, "status": "accepted", "reason": None} for seq in seqs
    ]}})


class _FakeSession:
    """Records what was posted and answers from a scripted list."""

    def __init__(self, *responses) -> None:
        self.headers: dict = {}
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if not self.responses:
            raise AssertionError("the uploader sent more requests than the test scripted")
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def sent_seqs(self) -> list[list[int]]:
        """The seqs of each request, in the order they were sent."""
        return [[f["seq"] for f in call["json"]["fragments"]] for call in self.calls]


@pytest.fixture
def outbox(tmp_path):
    return Outbox(tmp_path / "upload-outbox.sqlite")


def fill(outbox: Outbox, count: int, *, size: int = 0) -> list[int]:
    """Put `count` fragments in the outbox and return their seqs."""
    for index in range(count):
        fields = {"machine": "m" + "x" * size, "worker_version": "2.10.0"}
        outbox.put("runs", f"00000000-0000-4000-8000-{index:012d}", fields)
    return [record.seq for record in outbox.pending(1000)]


def uploader(outbox: Outbox, *responses) -> tuple[Uploader, _FakeSession]:
    session = _FakeSession(*responses)
    return Uploader(outbox, _BASE_URL, _TOKEN, session=session), session


def states(outbox: Outbox) -> dict:
    """What the outbox says about every fragment: seq → (synced, failed, error)."""
    import sqlite3
    with sqlite3.connect(outbox.path) as conn:
        rows = conn.execute(
            "SELECT seq, synced_at IS NOT NULL, failed_at IS NOT NULL, error FROM records"
        ).fetchall()
    return {seq: (bool(synced), bool(failed), error) for seq, synced, failed, error in rows}


# ── The request itself ────────────────────────────────────────────────────────

class TestTheRequest:
    def test_it_posts_to_the_contract_url_with_the_worker_token(self, outbox):
        fill(outbox, 1)
        up, session = uploader(outbox, accepted(1))

        up._cycle()

        assert session.calls[0]["url"] == f"{_BASE_URL}{PATH}"
        assert session.headers["Authorization"] == f"Bearer {_TOKEN}"
        assert session.headers["Content-Type"] == "application/json"
        # Short to connect, generous to read.
        assert session.calls[0]["timeout"] == (5, 30)

    def test_a_fragment_carries_its_id_beside_its_fields(self, outbox):
        outbox.put("runs", "33333333-3333-4333-8333-333333333333", {"machine": "m"})
        up, session = uploader(outbox, accepted(1))

        up._cycle()

        fragment = session.calls[0]["json"]["fragments"][0]
        assert fragment == {
            "seq": 1,
            "table": "runs",
            "id": "33333333-3333-4333-8333-333333333333",
            "fields": {"machine": "m"},
        }

    def test_fragments_go_in_seq_order(self, outbox):
        fill(outbox, 5)
        up, session = uploader(outbox, accepted(1, 2, 3, 4, 5))

        up._cycle()

        assert session.sent_seqs == [[1, 2, 3, 4, 5]]


# ── 200 ───────────────────────────────────────────────────────────────────────

class TestAnAcceptedBatch:
    def test_every_fragment_is_marked_sent(self, outbox):
        fill(outbox, 3)
        up, _ = uploader(outbox, accepted(1, 2, 3))

        outcome = up._cycle()

        assert outcome.progressed
        assert outbox.pending_count() == 0
        assert all(synced and not failed for synced, failed, _ in states(outbox).values())

    def test_an_empty_outbox_is_not_a_request(self, outbox):
        up, session = uploader(outbox)

        outcome = up._cycle()

        assert not outcome.progressed
        assert session.calls == []


class TestAMixedBatch:
    """A rejected fragment leaves the queue with its reason; the others are sent."""

    def test_accepted_and_rejected_are_marked_apart(self, outbox):
        fill(outbox, 3)
        up, _ = uploader(outbox, _Response(200, {"data": {"results": [
            {"seq": 1, "status": "accepted", "reason": None},
            {"seq": 2, "status": "rejected", "reason": "columna desconocida: hostname"},
            {"seq": 3, "status": "accepted", "reason": None},
        ]}}))

        assert up._cycle().progressed

        marks = states(outbox)
        assert marks[1][0] and marks[3][0]
        assert marks[2][1] and marks[2][2] == "columna desconocida: hostname"
        # And the queue is empty: a rejection is an answer, so it does not hold up the rest.
        assert outbox.pending_count() == 0

    def test_a_rejection_with_no_reason_still_records_one(self, outbox):
        fill(outbox, 1)
        up, _ = uploader(outbox, _Response(200, {"data": {"results": [
            {"seq": 1, "status": "rejected", "reason": None},
        ]}}))

        up._cycle()

        assert states(outbox)[1][2] == "rejected"


class TestAResultThatIsMissing:
    """A seq the response left out is not an answer, so the fragment stays pending.

    Guessing "accepted" would lose it silently, which is the one outcome the whole module
    exists to avoid; guessing "rejected" would throw away a fragment the API may well have
    taken. Asking again later costs one request.
    """

    def test_the_missing_one_stays_pending_and_the_rest_are_marked(self, outbox, caplog):
        fill(outbox, 3)
        up, _ = uploader(outbox, accepted(1, 3))

        with caplog.at_level(logging.WARNING, logger="audit.upload"):
            assert up._cycle().progressed

        marks = states(outbox)
        assert marks[1][0] and marks[3][0]
        assert marks[2] == (False, False, None)
        assert outbox.pending_count() == 1
        assert "stay pending" in caplog.text

    def test_a_status_the_worker_does_not_know_is_not_an_answer_either(self, outbox):
        fill(outbox, 1)
        up, _ = uploader(outbox, _Response(200, {"data": {"results": [
            {"seq": 1, "status": "maybe", "reason": None},
        ]}}))

        up._cycle()

        assert outbox.pending_count() == 1

    @pytest.mark.parametrize("body", [
        {}, {"data": {}}, {"data": {"results": "nope"}}, None,
    ])
    def test_a_200_the_worker_cannot_read_marks_nothing(self, outbox, body):
        fill(outbox, 2)
        up, _ = uploader(outbox, _Response(200, body))

        outcome = up._cycle()

        assert not outcome.progressed
        assert outcome.wait == BACKOFF_START_SECONDS
        assert outbox.pending_count() == 2


# ── The failures that mark nothing ────────────────────────────────────────────

class TestNothingIsMarkedOnAFailedRequest:
    """401, 422, 429, 5xx, a timeout and a refused connection are all "ask again later"."""

    def test_401_waits_long_and_warns_once_per_streak(self, outbox, caplog):
        fill(outbox, 2)
        up, _ = uploader(outbox, _Response(401), _Response(401))

        with caplog.at_level(logging.WARNING, logger="audit.upload"):
            first = up._cycle()
            second = up._cycle()

        assert first.wait == LONG_WAIT_SECONDS and not first.progressed
        assert second.wait == LONG_WAIT_SECONDS
        assert outbox.pending_count() == 2
        # One line for the streak, not one per attempt.
        assert caplog.text.count("refused the worker token") == 1

    def test_422_is_an_error_here_and_keeps_everything(self, outbox, caplog):
        fill(outbox, 2)
        up, _ = uploader(outbox, _Response(422), _Response(422))

        with caplog.at_level(logging.ERROR, logger="audit.upload"):
            outcome = up._cycle()
            up._cycle()

        assert outcome.wait == LONG_WAIT_SECONDS and not outcome.progressed
        assert outbox.pending_count() == 2
        assert caplog.text.count("rejected the batch's structure") == 1
        # An error rather than a warning: this one is a bug in the worker.
        assert [r.levelno for r in caplog.records] == [logging.ERROR]

    def test_429_honours_retry_after(self, outbox):
        fill(outbox, 1)
        up, _ = uploader(outbox, _Response(429, headers={"Retry-After": "42"}))

        outcome = up._cycle()

        assert outcome.wait == 42.0
        assert outbox.pending_count() == 1

    @pytest.mark.parametrize("header", [None, "", "later", "Wed, 21 Oct 2026 07:28:00 GMT"])
    def test_429_without_a_number_falls_back_to_the_backoff(self, outbox, header):
        fill(outbox, 1)
        headers = {"Retry-After": header} if header is not None else {}
        up, _ = uploader(outbox, _Response(429, headers=headers))

        assert up._cycle().wait == BACKOFF_START_SECONDS

    def test_5xx_backs_off_and_doubles(self, outbox):
        fill(outbox, 1)
        up, _ = uploader(outbox, _Response(500), _Response(503), _Response(502))

        waits = [up._cycle().wait for _ in range(3)]

        assert waits == [BACKOFF_START_SECONDS, BACKOFF_START_SECONDS * 2, BACKOFF_START_SECONDS * 4]
        assert outbox.pending_count() == 1

    @pytest.mark.parametrize("error", [
        requests.Timeout("read timed out"), requests.ConnectionError("refused"),
    ])
    def test_a_timeout_or_a_lost_connection_keeps_everything(self, outbox, error):
        fill(outbox, 2)
        up, _ = uploader(outbox, error)

        outcome = up._cycle()

        assert not outcome.progressed and outcome.wait == BACKOFF_START_SECONDS
        assert outbox.pending_count() == 2

    # An API older than 13.4.0 has no such endpoint. The worker degrades rather than
    # failing: it backs off, marks nothing, and the fragments go up when the API catches up.
    def test_an_api_without_the_endpoint_keeps_everything(self, outbox, caplog):
        fill(outbox, 2)
        up, _ = uploader(outbox, _Response(404), _Response(404))

        with caplog.at_level(logging.WARNING, logger="audit.upload"):
            outcome = up._cycle()
            up._cycle()

        assert not outcome.progressed and outcome.wait == BACKOFF_START_SECONDS
        assert outbox.pending_count() == 2
        assert caplog.text.count("unexpected 404") == 1

    def test_an_unexpected_status_keeps_everything_too(self, outbox):
        fill(outbox, 1)
        up, _ = uploader(outbox, _Response(418))

        assert up._cycle().wait == BACKOFF_START_SECONDS
        assert outbox.pending_count() == 1

    def test_a_successful_send_clears_the_backoff_and_the_streak(self, outbox, caplog):
        fill(outbox, 2)
        up, _ = uploader(outbox, _Response(500), accepted(1, 2), _Response(500))

        with caplog.at_level(logging.WARNING, logger="audit.upload"):
            up._cycle()
            up._cycle()
            fill(outbox, 1)
            third = up._cycle()

        assert third.wait == BACKOFF_START_SECONDS
        # Said again because the streak was broken by the send in between.
        assert caplog.text.count("answered 500") == 2


# ── Size ──────────────────────────────────────────────────────────────────────

class TestSize:
    def test_a_batch_is_cut_at_the_byte_limit(self, outbox):
        # Four fragments of ~140 KB: three fit under the 450 KB cut, the fourth waits.
        fill(outbox, 4, size=140_000)
        up, session = uploader(outbox, accepted(1, 2, 3), accepted(4))

        up._cycle()
        up._cycle()

        assert session.sent_seqs == [[1, 2, 3], [4]]
        assert outbox.pending_count() == 0

    def test_a_batch_is_cut_at_the_fragment_count(self, outbox):
        fill(outbox, MAX_FRAGMENTS + 5)
        up, session = uploader(
            outbox,
            accepted(*range(1, MAX_FRAGMENTS + 1)),
            accepted(*range(MAX_FRAGMENTS + 1, MAX_FRAGMENTS + 6)),
        )

        up._cycle()
        up._cycle()

        assert [len(seqs) for seqs in session.sent_seqs] == [MAX_FRAGMENTS, 5]

    def test_a_fragment_too_big_on_its_own_is_failed_locally(self, outbox, caplog):
        outbox.put("runs", "44444444-4444-4444-8444-444444444444", {"machine": "x" * MAX_BYTES})
        fill(outbox, 1)
        up, session = uploader(outbox, accepted(2))

        with caplog.at_level(logging.WARNING, logger="audit.upload"):
            up._cycle()

        marks = states(outbox)
        assert marks[1][1] and "byte limit" in marks[1][2]
        # And it did not hold up the one behind it.
        assert session.sent_seqs == [[2]]
        assert outbox.pending_count() == 0
        assert "too large to send" in caplog.text

    def test_only_oversized_fragments_is_progress_without_a_request(self, outbox):
        outbox.put("runs", "55555555-5555-4555-8555-555555555555", {"machine": "x" * MAX_BYTES})
        up, session = uploader(outbox)

        outcome = up._cycle()

        assert outcome.progressed
        assert session.calls == []
        assert outbox.pending_count() == 0

    def test_a_fragment_that_does_not_fit_now_goes_in_the_next_batch(self, outbox):
        """Not skipped: it is under the limit, so some batch can carry it."""
        fill(outbox, 1, size=300_000)
        fill(outbox, 1, size=300_000)
        up, session = uploader(outbox, accepted(1), accepted(2))

        up._cycle()
        up._cycle()

        assert session.sent_seqs == [[1], [2]]
        assert outbox.pending_count() == 0


class TestA413:
    """The API refused the request for its size, so the batch is halved and retried."""

    def test_a_batch_is_split(self, outbox):
        fill(outbox, 4)
        up, session = uploader(outbox, _Response(413), accepted(1, 2))

        outcome = up._cycle()

        assert outcome.progressed
        assert session.sent_seqs == [[1, 2, 3, 4], [1, 2]]
        assert outbox.pending_count() == 2

    def test_it_splits_down_to_the_one_fragment_that_earned_it(self, outbox):
        fill(outbox, 4)
        up, session = uploader(outbox, _Response(413), _Response(413), _Response(413))

        outcome = up._cycle()

        assert session.sent_seqs == [[1, 2, 3, 4], [1, 2], [1]]
        assert outcome.progressed
        marks = states(outbox)
        assert marks[1][1] and "413" in marks[1][2]
        # The three behind it are untouched and go out next time.
        assert outbox.pending_count() == 3


# ── Stopping ──────────────────────────────────────────────────────────────────

class TestStopping:
    def test_stop_flushes_what_is_left_and_reports_nothing_pending(self, outbox):
        fill(outbox, 3)
        # No thread started, so stop() takes the flush itself — which is the arrangement
        # that matters: two threads draining at once would double-send.
        up, session = uploader(outbox, accepted(1, 2, 3))

        assert up.stop() == 0
        assert session.sent_seqs == [[1, 2, 3]]

    def test_stop_reports_what_it_could_not_send(self, outbox):
        fill(outbox, 3)
        up, _ = uploader(outbox, _Response(500))

        assert up.stop() == 3

    def test_stop_drains_several_batches(self, outbox):
        fill(outbox, MAX_FRAGMENTS + 1)
        up, session = uploader(
            outbox,
            accepted(*range(1, MAX_FRAGMENTS + 1)),
            accepted(MAX_FRAGMENTS + 1),
        )

        assert up.stop() == 0
        assert len(session.sent_seqs) == 2

    def test_the_thread_drains_and_stops(self, outbox):
        fill(outbox, 2)
        up, session = uploader(outbox, accepted(1, 2))

        up.start()
        try:
            assert up.stop() == 0
        finally:
            up.stop()

        assert session.sent_seqs == [[1, 2]]

    def test_stopping_twice_is_harmless(self, outbox):
        up, _ = uploader(outbox)

        assert up.stop() == 0
        assert up.stop() == 0

    def test_a_broken_outbox_is_reported_rather_than_raised(self, outbox, monkeypatch):
        def boom(*_args, **_kwargs):
            raise sqlite_error()

        def sqlite_error():
            import sqlite3
            return sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(outbox, "pending", boom)
        monkeypatch.setattr(outbox, "pending_count", boom)
        up, _ = uploader(outbox)

        assert up.stop() == -1


# ── The loop's own guards ─────────────────────────────────────────────────────

class TestTheLoop:
    def test_it_prunes_once_on_the_way_in(self, outbox, monkeypatch):
        pruned = []
        monkeypatch.setattr(outbox, "prune", lambda *a, **k: pruned.append(True) or 0)
        up, _ = uploader(outbox)

        up.start()
        up.stop()

        assert pruned == [True]

    def test_an_unexpected_failure_does_not_take_the_thread_down(self, outbox, monkeypatch):
        """A bug here must not leave a run recording everything and sending none of it."""
        calls = []

        def explode():
            calls.append(True)
            raise RuntimeError("bug")

        up, _ = uploader(outbox)
        monkeypatch.setattr(up, "_cycle", explode)
        monkeypatch.setattr(up, "_pending_count", lambda: 0)

        up.start()
        # The loop catches it and waits rather than dying; stop() ends the wait.
        up.stop()

        assert calls, "the loop never ran"

    def test_idle_is_a_short_wait_rather_than_a_spin(self):
        assert 1.0 <= IDLE_SECONDS <= 30.0

    def test_nothing_is_sent_without_a_session_being_asked(self, outbox):
        """A guard on the tests themselves: no test may reach the network."""
        up, session = uploader(outbox)

        up._cycle()

        assert session.calls == []


class TestTheSuiteItselfNeverUploads:
    """A guard on the tests, pinning what `conftest.py` does — and it is not theoretical.

    `config.py` loads the developer's real `.env` with `override=True`, so `API_BASE_URL`
    and `API_TOKEN` inside the suite are the local API and a token it accepts. `main()`
    hands both to `recorder.start()`, and before the conftest guard existed a plain
    `pytest -q` posted every fragment the suite produced to a running API — seventy-one
    rows in its `audit_runs`, all from test runs. Redirecting `AUDIT_OUTBOX_PATH` does not
    help: the fragments were written to `tmp_path` and sent from there perfectly well.
    """

    def test_start_with_a_real_url_and_token_starts_nothing(self, tmp_path):
        from audit import recorder

        box = Outbox(tmp_path / "guarded-outbox.sqlite")
        try:
            recorder.start(
                outbox=box, api_base_url="http://127.0.0.1:8000", api_token="a-real-token",
            )

            assert recorder._uploader is None, "the suite started a real uploader"
        finally:
            recorder.stop("stopped")

        # And the fragments are still local, which is where a test's audit belongs.
        assert box.pending_count() > 0


def test_the_payload_is_json_serialisable(outbox):
    """What `_fit` measures has to be what `requests` will send."""
    outbox.put("cards", "66666666-6666-4666-8666-666666666666", {
        "search_attempt_id": "77777777-7777-4777-8777-777777777777",
        "maps_issues": {"no_phone": "Sin teléfono"},
        "raw": {"lead": "Ñandú S.L.", "website": None},
    })
    up, session = uploader(outbox, accepted(1))

    up._cycle()

    # Round-trips, accents and nulls included.
    body = session.calls[0]["json"]
    assert json.loads(json.dumps(body, ensure_ascii=False)) == body
