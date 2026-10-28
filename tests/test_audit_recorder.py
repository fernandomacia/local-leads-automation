"""Tests for audit/recorder.py — a real outbox in tmp_path, no DSN, no network."""

import logging
import re
import socket
from datetime import timezone
from pathlib import Path

import pytest

from audit import recorder
from audit.outbox import Outbox
from config import APP_VERSION

_MIGRATION = Path(__file__).resolve().parent.parent / "audit" / "migrations" / "001_initial.sql"

_JOB = {"id": "lead-1", "lead_search_id": "search-1", "business_name": "Ejemplo SL",
        "website": "https://ejemplo.es"}
_SEARCH = {"id": "search-1", "profession": "fontaneros", "city": "Elche", "max_results": 5}


def not_null_columns(table: str) -> set[str]:
    """The columns the store refuses an INSERT without, read from the migration itself.

    Read rather than restated: the point of the check is to fail when the schema gains a
    NOT NULL column that the recorder's first fragment does not carry. ``id`` is left out
    because it travels as the fragment's record_id, not among its fields.
    """
    body = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", _MIGRATION.read_text(), re.S).group(1)
    columns = {
        line.split()[0] for raw in body.splitlines()
        if (line := raw.strip()) and not line.startswith("--") and "NOT NULL" in line
    }
    return columns - {"id"}


def fragments(box: Outbox, target: str) -> list[dict]:
    """Every fragment written for a table, in the order it was written."""
    return [record.fields for record in box.pending(1000) if record.target == target]


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def box(tmp_path):
    # Named apart from the machine outbox the autouse fixture points at tmp_path, so a
    # test can tell "the recorder wrote here" from "the recorder opened its own".
    return Outbox(tmp_path / "recorder-outbox.sqlite")


@pytest.fixture
def started(box):
    """A run open on a temporary outbox, uploading nowhere."""
    recorder.start(outbox=box, dsn="")
    yield box
    recorder.stop("stopped")


# ── Nothing happens without a run ─────────────────────────────────────────────

class TestNoOpWithoutStart:
    """The audit must be invisible to anything that never started it."""

    def test_every_function_is_a_no_op(self, tmp_path):
        recorder.search_started(_SEARCH)
        recorder.search_finished("completed", cards_seen=3)
        recorder.analysis_started(_JOB)
        recorder.analysis_finished("analysed")
        recorder.pause(recorder.PAUSE_PAYMENT, 120)
        recorder.heartbeat()

        assert recorder.stop("stopped") == 0
        assert recorder.current_search_attempt_id() is None
        assert recorder.current_analysis_id() is None
        # Not even the machine's own outbox is opened: an unaudited run touches no disk.
        assert not (tmp_path / "outbox.sqlite").exists()


# ── The first fragment is the INSERT ──────────────────────────────────────────

class TestEveryRowCanBeInserted:
    """A first fragment short of one NOT NULL column is a row Neon refuses outright.

    Checked against the migration, so a column added there without being added here
    fails in the suite rather than as a refused fragment on a machine somewhere.
    """

    def _assert_complete(self, box: Outbox, table: str) -> None:
        missing = not_null_columns(table) - set(fragments(box, table)[0])
        assert not missing, f"the first fragment of {table} lacks {sorted(missing)}"

    def test_a_run(self, started):
        self._assert_complete(started, "runs")

    def test_a_search_attempt(self, started):
        recorder.search_started(_SEARCH)
        self._assert_complete(started, "search_attempts")

    def test_an_analysis(self, started):
        recorder.analysis_started(_JOB)
        self._assert_complete(started, "analyses")

    def test_a_pause(self, started):
        recorder.pause(recorder.PAUSE_RATE_LIMIT, 60)
        self._assert_complete(started, "log_entries")


class TestWhatTheRunSays:
    def test_it_names_the_machine_the_environment_and_the_version(self, started):
        run = fragments(started, "runs")[0]
        assert run["machine"] == socket.gethostname()
        # The column's CHECK accepts exactly these two; anything else is a refused row.
        assert run["environment"] in ("local", "production")
        assert run["worker_version"] == APP_VERSION

    def test_timestamps_are_utc(self, started):
        # An aware datetime goes in, and the outbox stores it as ISO text — so what
        # reaches Neon is the offset. Without it the store would read every timestamp
        # in its own timezone, which is not the one the worker ran in.
        assert recorder._utcnow().tzinfo is timezone.utc
        assert fragments(started, "runs")[0]["started_at"].endswith("+00:00")

    def test_stopping_says_how_it_ended(self, started):
        recorder.stop("crashed")
        assert fragments(started, "runs")[-1] == {
            "ended_at": fragments(started, "runs")[-1]["ended_at"], "end_reason": "crashed",
        }


# ── Liveness ──────────────────────────────────────────────────────────────────

class TestHeartbeat:
    def test_it_is_written_at_most_once_every_thirty_seconds(self, started, monkeypatch):
        clock = _Clock(recorder._last_heartbeat)
        monkeypatch.setattr(recorder, "time", clock)

        clock.now += recorder.HEARTBEAT_INTERVAL - 1
        recorder.heartbeat()
        # Still only the fragment that opened the run: a beat per poll would be one row
        # every ten seconds for ever, which is traffic rather than information.
        assert len(fragments(started, "runs")) == 1

        clock.now += 2
        recorder.heartbeat()
        beats = fragments(started, "runs")
        assert len(beats) == 2
        assert set(beats[1]) == {"last_seen_at"}


# ── How long a lead took ──────────────────────────────────────────────────────

class TestDuration:
    def test_duration_ms_is_measured_from_the_start(self, started, monkeypatch):
        clock = _Clock()
        monkeypatch.setattr(recorder, "time", clock)

        recorder.analysis_started(_JOB)
        clock.now += 1.25
        recorder.analysis_finished("analysed")

        assert fragments(started, "analyses")[1]["duration_ms"] == 1250

    def test_an_error_is_split_into_its_class_and_its_message(self, started):
        recorder.analysis_started(_JOB)
        recorder.analysis_finished("failed", error=RuntimeError("boom"))

        closing = fragments(started, "analyses")[1]
        assert closing["error_type"] == "RuntimeError"
        assert closing["error_message"] == "boom"


# ── The work in progress ──────────────────────────────────────────────────────

class TestCurrentWork:
    """What the log handler and the llm_calls task will read to file their own rows."""

    def test_a_pause_is_filed_under_whatever_is_open(self, started):
        recorder.search_started(_SEARCH)
        recorder.analysis_started(_JOB)
        recorder.pause(recorder.PAUSE_RATE_LIMIT, 60)

        entry = fragments(started, "log_entries")[0]
        assert entry["search_attempt_id"] == recorder.current_search_attempt_id()
        assert entry["analysis_id"] == recorder.current_analysis_id()
        assert "60" in entry["message"]

    def test_they_are_cleared_when_the_work_ends(self, started):
        recorder.search_started(_SEARCH)
        recorder.analysis_started(_JOB)
        recorder.analysis_finished("analysed")
        recorder.search_finished("completed")

        assert recorder.current_analysis_id() is None
        assert recorder.current_search_attempt_id() is None

    def test_closing_what_was_never_opened_records_nothing(self, started):
        recorder.analysis_finished("analysed")
        recorder.search_finished("completed")
        assert fragments(started, "analyses") == []
        assert fragments(started, "search_attempts") == []


# ── A failed audit is not a failed lead ───────────────────────────────────────

class TestNothingReachesTheCaller:
    """The rule the whole package exists under: an audit failure costs nothing else."""

    @pytest.fixture
    def broken(self, started, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("the outbox is gone")

        monkeypatch.setattr(started, "put", explode)
        return started

    def test_an_outbox_that_cannot_store_anything_only_warns(self, broken, caplog):
        with caplog.at_level(logging.WARNING, logger="audit"):
            recorder.search_started(_SEARCH)
            recorder.analysis_started(_JOB)
            recorder.analysis_finished("analysed", error=RuntimeError("boom"))
            recorder.search_finished("failed", error=RuntimeError("boom"))
            recorder.pause(recorder.PAUSE_PAYMENT, 120)
            recorder.heartbeat()

        assert caplog.records
        # Under "audit", which is the hierarchy the log handler leaves out: a broken
        # outbox must not be able to feed on its own warnings.
        assert all(record.name.startswith("audit") for record in caplog.records)

    def test_stopping_says_it_could_not_count_rather_than_raising(self, broken):
        assert recorder.stop("stopped") == -1

    def test_a_run_that_cannot_be_opened_leaves_everything_a_no_op(self, box, monkeypatch, caplog):
        def explode(*args, **kwargs):
            raise RuntimeError("the outbox is gone")

        monkeypatch.setattr(box, "put", explode)
        with caplog.at_level(logging.WARNING, logger="audit"):
            assert recorder.start(outbox=box, dsn="") is None

        recorder.search_started(_SEARCH)  # must not raise
        assert recorder.current_search_attempt_id() is None
