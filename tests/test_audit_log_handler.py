"""Tests for audit/log_handler.py — what the worker's warnings and errors leave behind."""

import logging
import threading

import pytest

from audit import recorder
from audit.log_handler import AuditLogHandler
from audit.outbox import Outbox

_JOB = {"id": "lead-1", "business_name": "Ejemplo SL", "website": "https://ejemplo.es"}
_SEARCH = {"id": "search-1", "profession": "fontaneros", "city": "Elche"}


def emit(name: str, level: int = logging.WARNING, message: str = "algo", exc_info=None) -> None:
    """Log one record through the real logging machinery, whatever the levels are set to.

    ``handle`` is used rather than ``warning`` so the test does not depend on the level
    the logger happens to have inherited, and does not have to change it for everyone.
    """
    logger = logging.getLogger(name)
    logger.handle(logger.makeRecord(name, level, "test.py", 1, message, (), exc_info))


def entries(box: Outbox) -> list[dict]:
    return [record.fields for record in box.pending(1000) if record.target == "log_entries"]


def run_id(box: Outbox) -> str:
    return next(record.record_id for record in box.pending(1000) if record.target == "runs")


@pytest.fixture
def started(tmp_path):
    box = Outbox(tmp_path / "log-outbox.sqlite")
    recorder.start(outbox=box)
    yield box
    recorder.stop("stopped")


# ── What gets filed ───────────────────────────────────────────────────────────

class TestWhatGetsFiled:
    def test_a_warning_becomes_a_row(self, started):
        emit("scraper.maps_scraper", message="Could not check whether ejemplo.es is known")

        entry = entries(started)[0]
        assert entry["kind"] == "log"
        assert entry["level"] == "WARNING"
        assert entry["logger"] == "scraper.maps_scraper"
        assert entry["message"] == "Could not check whether ejemplo.es is known"
        assert entry["run_id"] == run_id(started)
        assert entry["logged_at"].endswith("+00:00")

    def test_the_message_is_rendered_with_its_arguments(self, started):
        logger = logging.getLogger("worker")
        logger.handle(logger.makeRecord(
            "worker", logging.WARNING, "test.py", 1, "Dropping %s: %d characters",
            ("email", 300), None))

        assert entries(started)[0]["message"] == "Dropping email: 300 characters"

    def test_an_info_record_is_not_filed(self, started):
        # The level is the point: everything the worker logs at INFO is routine, and the
        # audit is for what somebody has to look into.
        emit("worker", level=logging.INFO)
        assert entries(started) == []

    def test_an_error_keeps_its_class_and_its_traceback(self, started):
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            emit("worker", logging.ERROR, "Analysis job failed",
                 (type(exc), exc, exc.__traceback__))

        entry = entries(started)[0]
        assert entry["level"] == "ERROR"
        assert entry["exception_type"] == "RuntimeError"
        assert "Traceback" in entry["exception_text"]
        assert "RuntimeError: boom" in entry["exception_text"]

    def test_a_record_without_an_exception_carries_no_traceback_fields(self, started):
        emit("worker")
        assert not {"exception_type", "exception_text"} & set(entries(started)[0])

    @pytest.mark.parametrize("name", ["audit", "audit.outbox", "audit.recorder"])
    def test_the_audits_own_complaints_are_not_filed(self, started, name):
        # Otherwise a broken outbox feeds on itself: a failed write logs a warning, which
        # is a record, which is another failed write.
        emit(name, message="Audit fragment could not be stored")
        assert entries(started) == []

    def test_nothing_is_filed_once_the_run_is_closed(self, started):
        recorder.stop("stopped")
        emit("worker")
        assert entries(started) == []


# ── Which work a record belongs to ────────────────────────────────────────────

class TestWhatARecordIsFiledUnder:
    def test_a_warning_during_an_analysis_is_filed_under_it(self, started):
        recorder.search_started(_SEARCH)
        recorder.analysis_started(_JOB)
        emit("scraper.web_analyzer", message="Certificate verify failed")

        entry = entries(started)[0]
        assert entry["search_attempt_id"] == recorder.current_search_attempt_id()
        assert entry["analysis_id"] == recorder.current_analysis_id()

    def test_a_warning_between_jobs_is_filed_under_the_run_alone(self, started):
        emit("api.client", message="API connection error, retrying")

        entry = entries(started)[0]
        assert entry["search_attempt_id"] is None
        assert entry["analysis_id"] is None

    def test_a_warning_from_another_thread_names_no_work(self, started):
        recorder.analysis_started(_JOB)
        thread = threading.Thread(target=emit, args=("scraper.compliance.rendering",))
        thread.start()
        thread.join()

        entry = entries(started)[0]
        # A thread begins with an empty context, so the analysis the main thread has open
        # is not visible from it. Filed under the run, which is module state and visible
        # everywhere: a record attached to the wrong lead would be worse than one
        # attached to none.
        assert entry["run_id"] == run_id(started)
        assert entry["analysis_id"] is None


# ── The handler cannot misbehave ──────────────────────────────────────────────

class TestEmitCannotMisbehave:
    def test_it_only_takes_warnings_and_worse(self):
        assert AuditLogHandler(lambda record: None).level == logging.WARNING

    def test_a_sink_that_explodes_neither_raises_nor_prints(self, capsys):
        def explode(record):
            raise RuntimeError("the outbox is gone")

        logger = logging.getLogger("worker")
        record = logger.makeRecord("worker", logging.ERROR, "test.py", 1, "x", (), None)

        AuditLogHandler(explode).emit(record)  # must not raise

        printed = capsys.readouterr()
        # logging's own handleError() would put this on stderr, which is where the
        # operator is reading the run.
        assert printed.out == "" and printed.err == ""

    def test_a_broken_outbox_costs_nothing_but_the_row(self, started, monkeypatch):
        monkeypatch.setattr(started, "put", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
        emit("worker")  # must not raise


# ── Installation ──────────────────────────────────────────────────────────────

class TestInstallation:
    def _installed(self) -> list[AuditLogHandler]:
        return [h for h in logging.getLogger().handlers if isinstance(h, AuditLogHandler)]

    def test_start_installs_it_once_and_stop_takes_it_away(self, tmp_path):
        before = list(logging.getLogger().handlers)
        box = Outbox(tmp_path / "log-outbox.sqlite")

        recorder.start(outbox=box)
        recorder.start(outbox=box)
        assert len(self._installed()) == 1

        recorder.stop("stopped")
        assert self._installed() == []
        assert logging.getLogger().handlers == before

    def test_it_adds_a_destination_and_replaces_none(self, tmp_path):
        # The worker's console output is the thing an operator watches; the audit is one
        # more place the same record goes.
        seen = []
        console = logging.Handler()
        console.emit = seen.append
        logging.getLogger().addHandler(console)
        box = Outbox(tmp_path / "log-outbox.sqlite")
        try:
            recorder.start(outbox=box)
            emit("worker", message="Rate limited")
            assert [record.getMessage() for record in seen] == ["Rate limited"]
            assert len(entries(box)) == 1
        finally:
            logging.getLogger().removeHandler(console)
            recorder.stop("stopped")
