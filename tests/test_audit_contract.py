"""The API's audit contract, encoded here, against every fragment the recorder emits.

**This is the worker's copy of a contract that lives in SegurSEO-API**, where the
`audit_*` tables are defined and `AuditTable` is their single definition. The worker cannot
read that, so the tables, the columns, their types, the values their CHECKs allow and what
each row cannot be created without are written out below by hand — and a column the API
adds, renames or retypes has to be changed here by hand too.

Why it is worth the duplication: the API answers a fragment it cannot accept with a
`rejected` inside a 200, which `audit/upload.py` records in the local outbox and nothing
else reads. So a recorder that emits `"3"` where an integer goes loses that row silently,
hours after the run that produced it, with the reason in a file on the developer's laptop.
This test is the only thing on this side that can see it before the fragment is sent.

What it checks, for every fragment of every recorder function:

- the table is one of the six;
- no column the contract does not name (`id` travels beside `fields`, and
  `content_purged_at` is the API's own record of what it purged — both are unknown here on
  purpose);
- every value is of its column's type, strictly: `"3"` is not an integer, `1` is not a
  boolean, and a timestamp carries its offset;
- a value a CHECK constrains is one of the values it allows;
- and the **first** fragment of each row carries every column that row cannot be created
  without, with nothing null among them.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from audit import recorder
from audit.outbox import Outbox

# ── The contract ──────────────────────────────────────────────────────────────


class _Type:
    """A column type, as the API checks it."""

    name = "?"

    def accepts(self, value) -> bool:
        raise NotImplementedError


class _Text(_Type):
    name = "text"

    def accepts(self, value) -> bool:
        return isinstance(value, str)


class _Uuid(_Type):
    name = "uuid"

    def accepts(self, value) -> bool:
        if not isinstance(value, str):
            return False
        try:
            uuid.UUID(value)
        except ValueError:
            return False
        return True


class _Timestamp(_Type):
    """ISO 8601 **with an offset** — the API refuses one without."""

    name = "timestamp"

    def accepts(self, value) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return False
        return parsed.tzinfo is not None


class _Integer(_Type):
    name = "integer"

    def accepts(self, value) -> bool:
        # bool is a subclass of int in Python and is not an integer to the API.
        return isinstance(value, int) and not isinstance(value, bool)


class _Numeric(_Type):
    name = "numeric"

    def accepts(self, value) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)


class _Boolean(_Type):
    name = "boolean"

    def accepts(self, value) -> bool:
        return isinstance(value, bool)


class _Json(_Type):
    name = "json"

    def accepts(self, value) -> bool:
        return isinstance(value, (dict, list))


@dataclass(frozen=True)
class _Enum(_Type):
    """Text constrained by a CHECK to a named set."""

    allowed: frozenset
    name: str = "text"

    def accepts(self, value) -> bool:
        return isinstance(value, str) and value in self.allowed


TEXT, UUID, TIMESTAMP = _Text(), _Uuid(), _Timestamp()
INTEGER, NUMERIC, BOOLEAN, JSON = _Integer(), _Numeric(), _Boolean(), _Json()


@dataclass(frozen=True)
class _Table:
    columns: dict
    required: frozenset = field(default_factory=frozenset)


CONTRACT = {
    "runs": _Table(
        columns={
            "machine": TEXT,
            "worker_version": TEXT,
            "started_at": TIMESTAMP,
            "last_seen_at": TIMESTAMP,
            "ended_at": TIMESTAMP,
            "end_reason": _Enum(frozenset({"stopped", "crashed"})),
        },
        required=frozenset({"machine", "worker_version", "started_at", "last_seen_at"}),
    ),
    "search_attempts": _Table(
        columns={
            "run_id": UUID,
            "lead_search_id": UUID,
            "profession": TEXT,
            "city": TEXT,
            "started_at": TIMESTAMP,
            "max_results": INTEGER,
            "ended_at": TIMESTAMP,
            "error": TEXT,
            "outcome": _Enum(frozenset({
                "completed", "failed", "released_interrupted", "released_api_unreachable",
            })),
            "cards_seen": INTEGER,
            "cards_skipped_known": INTEGER,
            "leads_reported": INTEGER,
            "leads_inserted": INTEGER,
        },
        # lead_search_id is nullable in the API's own schema — it has to survive the search
        # being deleted — and still mandatory when the row is created: an attempt nothing
        # can tie back to its job records nothing.
        required=frozenset({"run_id", "lead_search_id", "profession", "city", "started_at"}),
    ),
    "cards": _Table(
        columns={
            "search_attempt_id": UUID,
            "seen_at": TIMESTAMP,
            "outcome": _Enum(frozenset({"reported", "skipped_known_domain"})),
            "maps_issues": JSON,
            "domain": TEXT,
            "business_name": TEXT,
            "website": TEXT,
            "phone": TEXT,
            "address": TEXT,
            "maps_url": TEXT,
            "raw": JSON,
        },
        required=frozenset({"search_attempt_id", "seen_at", "outcome"}),
    ),
    "analyses": _Table(
        columns={
            "run_id": UUID,
            "lead_id": UUID,
            "lead_search_id": UUID,
            "started_at": TIMESTAMP,
            "ended_at": TIMESTAMP,
            "duration_ms": INTEGER,
            "error_type": TEXT,
            "outcome": _Enum(frozenset({
                "analysed", "failed", "report_rejected",
                "released_rate_limit", "released_payment", "released_interrupted",
            })),
            "error_message": TEXT,
            "business_name": TEXT,
            "website": TEXT,
            "result": JSON,
        },
        required=frozenset({"run_id", "lead_id", "started_at"}),
    ),
    "llm_calls": _Table(
        columns={
            "analysis_id": UUID,
            "started_at": TIMESTAMP,
            "model": TEXT,
            "temperature": NUMERIC,
            "cost_usd": NUMERIC,
            "max_tokens": INTEGER,
            "final_status": INTEGER,
            "latency_ms": INTEGER,
            "prompt_tokens": INTEGER,
            "completion_tokens": INTEGER,
            "parsed_ok": BOOLEAN,
            "system_prompt_sha256": TEXT,
            "attempts": JSON,
            "system_prompt": TEXT,
            "user_prompt": TEXT,
            "response_raw": TEXT,
            "response_parsed": JSON,
        },
        required=frozenset({"analysis_id", "started_at", "model"}),
    ),
    "log_entries": _Table(
        columns={
            "run_id": UUID,
            "logged_at": TIMESTAMP,
            "level": TEXT,
            "logger": TEXT,
            "kind": _Enum(frozenset({"log", "pause_rate_limit", "pause_payment"})),
            "search_attempt_id": UUID,
            "analysis_id": UUID,
            "exception_type": TEXT,
            "message": TEXT,
            "exception_text": TEXT,
        },
        required=frozenset({"run_id", "logged_at", "level", "logger"}),
    ),
}

# Never describable, and the API answers "columna desconocida" to either: the id travels
# beside `fields`, and the purge stamp is the API's own record of what it emptied — a
# worker able to write it could claim an erasure that never happened.
NEVER_SENT = frozenset({"id", "content_purged_at"})

_SEARCH_ID = "11111111-1111-4111-8111-111111111111"
_LEAD_ID = "22222222-2222-4222-8222-222222222222"

_SEARCH = {"id": _SEARCH_ID, "profession": "fontaneros", "city": "Elche", "max_results": 5}
_JOB = {"id": _LEAD_ID, "lead_search_id": _SEARCH_ID, "business_name": "Ejemplo SL",
        "website": "https://ejemplo.es"}
_CARD = {"lead": "Abogados García", "website": "https://www.Ejemplo.es/contacto",
         "phone": "965123456", "address": "Calle Mayor 1", "city": "Elche",
         "maps_url": "https://maps.google.com/place/abogados-garcia"}


# ── Checking one fragment ─────────────────────────────────────────────────────

def assert_fragment_is_valid(target: str, fields: dict, *, first: bool) -> None:
    """Hold one fragment against the contract, the way the API would."""
    assert target in CONTRACT, f"unknown table {target!r}"
    table = CONTRACT[target]

    unknown = set(fields) - set(table.columns)
    assert not unknown, f"{target}: columns the API does not know: {sorted(unknown)}"
    assert not (set(fields) & NEVER_SENT), f"{target}: sent a column the API always refuses"

    for column, value in fields.items():
        if value is None:
            continue
        column_type = table.columns[column]
        assert column_type.accepts(value), (
            f"{target}.{column} is {type(value).__name__} {value!r}, "
            f"not {column_type.name}"
        )

    if first:
        missing = table.required - set(fields)
        assert not missing, f"{target}: the first fragment lacks {sorted(missing)}"
        nulls = [c for c in table.required if fields.get(c) is None]
        assert not nulls, f"{target}: the first fragment has null in {sorted(nulls)}"


def assert_outbox_satisfies_contract(box: Outbox) -> None:
    """Every fragment in the outbox, with the first of each row checked as an insert."""
    records = box.pending(1000)
    assert records, "nothing was recorded, so nothing was checked"

    seen: set[tuple[str, str]] = set()
    for record in records:
        key = (record.target, record.record_id)
        assert_fragment_is_valid(record.target, record.fields, first=key not in seen)
        seen.add(key)


@pytest.fixture
def box(tmp_path):
    return Outbox(tmp_path / "contract-outbox.sqlite")


@pytest.fixture
def started(box):
    """A run open on a temporary outbox, uploading nowhere: no URL, so no uploader."""
    recorder.start(outbox=box)
    yield box
    recorder.stop("stopped")


# ── Every recorder function ───────────────────────────────────────────────────

class TestEveryFragmentSatisfiesTheContract:
    """Driven through the recorder's own API, and read back through the outbox.

    Read back rather than intercepted, because the outbox serialises to JSON on the way in:
    a `datetime` is an ISO string by the time it is sent, and that string — offset and all —
    is what the API actually judges. Checking the Python object would check something the
    API never sees.
    """

    def test_a_run_from_start_to_stop(self, box):
        recorder.start(outbox=box)
        recorder.heartbeat()
        recorder._last_heartbeat = 0.0  # so the next one is not suppressed
        recorder.heartbeat()
        recorder.stop("stopped")

        assert_outbox_satisfies_contract(box)

    def test_a_crashed_run(self, box):
        recorder.start(outbox=box)
        recorder.stop("crashed")

        assert_outbox_satisfies_contract(box)

    def test_a_search_attempt_that_completed(self, started):
        recorder.search_started(_SEARCH)
        recorder.search_finished(
            "completed", cards_seen=12, cards_skipped_known=3,
            leads_reported=9, leads_inserted=7,
        )

        assert_outbox_satisfies_contract(started)

    @pytest.mark.parametrize("outcome", sorted(
        CONTRACT["search_attempts"].columns["outcome"].allowed,
    ))
    def test_every_search_outcome(self, started, outcome):
        recorder.search_started(_SEARCH)
        recorder.search_finished(outcome, error=RuntimeError("boom"))

        assert_outbox_satisfies_contract(started)

    def test_a_search_with_no_max_results(self, started):
        recorder.search_started({**_SEARCH, "max_results": None})
        recorder.search_finished("completed")

        assert_outbox_satisfies_contract(started)

    # The API sends max_results as JSON, and JSON has one number type: a float where an
    # integer goes would cost the whole fragment.
    @pytest.mark.parametrize("max_results", [5, 5.0, "5", None, "", "many", 2.5])
    def test_whatever_the_api_calls_max_results(self, started, max_results):
        recorder.search_started({**_SEARCH, "max_results": max_results})

        assert_outbox_satisfies_contract(started)

    def test_a_reported_card_and_a_dropped_one(self, started):
        recorder.search_started(_SEARCH)
        recorder.card_reported(_CARD)
        recorder.card_skipped_known("ejemplo.es")

        assert_outbox_satisfies_contract(started)

    def test_a_card_with_nothing_but_a_name(self, started):
        recorder.search_started(_SEARCH)
        recorder.card_reported({"lead": "Solo el nombre"})

        assert_outbox_satisfies_contract(started)

    @pytest.mark.parametrize("outcome", sorted(
        CONTRACT["analyses"].columns["outcome"].allowed,
    ))
    def test_every_analysis_outcome(self, started, outcome):
        recorder.analysis_started(_JOB)
        recorder.analysis_finished(
            outcome, error=ValueError("no"), result={"phone_script": "Buenos días"},
        )

        assert_outbox_satisfies_contract(started)

    def test_an_analysis_of_a_lead_with_no_search(self, started):
        recorder.analysis_started({**_JOB, "lead_search_id": None})
        recorder.analysis_finished("analysed")

        assert_outbox_satisfies_contract(started)

    def test_an_llm_call_end_to_end(self, started):
        recorder.analysis_started(_JOB)
        recorder.llm_call_started(
            model="anthropic/claude", system_prompt="s", user_prompt="u",
            temperature=0.6, max_tokens=2000,
        )
        recorder.llm_call_finished(
            attempts=[{"status": 429, "latency_ms": 10, "retry_in_s": 2}, {"status": 200}],
            final_status=200, latency_ms=1234, prompt_tokens=900,
            completion_tokens=120, cost_usd=0.0042, response_raw='{"phone_script":"hola"}',
        )
        recorder.llm_call_parsed({"phone_script": "hola"}, parsed_ok=True)
        recorder.analysis_finished("analysed")

        assert_outbox_satisfies_contract(started)

    # OpenRouter's usage block is a third party's JSON: the counts arrive as numbers but
    # not reliably as integers, and `cost` has been seen as a string.
    @pytest.mark.parametrize("usage", [
        {"prompt_tokens": 900, "completion_tokens": 120, "cost": 0.0042},
        {"prompt_tokens": 900.0, "completion_tokens": 120.0, "cost": "0.0042"},
        {"prompt_tokens": "900", "completion_tokens": None, "cost": None},
        {"prompt_tokens": None, "completion_tokens": "n/a", "cost": "free"},
    ])
    def test_whatever_openrouter_calls_its_usage(self, started, usage):
        recorder.analysis_started(_JOB)
        recorder.llm_call_started(model="m", system_prompt="s", user_prompt="u")
        recorder.llm_call_finished(
            attempts=[], final_status=200, latency_ms=10,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            cost_usd=usage["cost"],
        )

        assert_outbox_satisfies_contract(started)

    def test_a_call_whose_answer_could_not_be_read(self, started):
        recorder.analysis_started(_JOB)
        recorder.llm_call_started(model="m", system_prompt="s", user_prompt="u")
        recorder.llm_call_parsed(None, parsed_ok=False)

        assert_outbox_satisfies_contract(started)

    @pytest.mark.parametrize("kind", [recorder.PAUSE_RATE_LIMIT, recorder.PAUSE_PAYMENT])
    def test_both_pauses(self, started, kind):
        recorder.pause(kind, 120)

        assert_outbox_satisfies_contract(started)

    def test_a_logged_warning_and_a_logged_exception(self, started):
        logging.getLogger("scraper.test").warning("Algo pasó con %s", "un lead")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logging.getLogger("scraper.test").exception("Y algo falló")

        assert_outbox_satisfies_contract(started)

    def test_a_warning_logged_inside_an_analysis(self, started):
        recorder.search_started(_SEARCH)
        recorder.analysis_started(_JOB)
        logging.getLogger("api.client").error("La API respondió 500")

        assert_outbox_satisfies_contract(started)

    # The whole shape of a run, in one go: every table, every ordering, one outbox.
    def test_a_whole_run(self, box):
        recorder.start(outbox=box)
        recorder.search_started(_SEARCH)
        recorder.card_reported(_CARD)
        recorder.card_skipped_known("otro.es")
        logging.getLogger("scraper.maps").warning("Una tarjeta no cargó")
        recorder.search_finished("completed", cards_seen=2, leads_reported=1, leads_inserted=1)
        recorder.analysis_started(_JOB)
        recorder.llm_call_started(model="m", system_prompt="s", user_prompt="u", temperature=0.6)
        recorder.llm_call_finished(attempts=[{"status": 200}], final_status=200, latency_ms=5)
        recorder.llm_call_parsed({"phone_script": "x"}, parsed_ok=True)
        recorder.analysis_finished("analysed", result={"phone_script": "x"})
        recorder.pause(recorder.PAUSE_RATE_LIMIT, 60)
        recorder.stop("stopped")

        assert_outbox_satisfies_contract(box)
        assert {record.target for record in box.pending(1000)} == set(CONTRACT)


class TestWhatTheRecorderRefusesToRecord:
    """A row the API could only reject is better not written at all.

    `search_attempts.lead_search_id` and `analyses.lead_id` are resolved by the API against
    its own rows, so a job whose id is not a uuid has no recordable attempt: queueing one
    would fill the outbox with fragments that come back rejected, one request at a time.
    """

    @pytest.mark.parametrize("bad_id", ["search-1", "", None, 7])
    def test_a_search_job_with_no_usable_id(self, started, bad_id):
        recorder.search_started({**_SEARCH, "id": bad_id})

        assert [r for r in started.pending(1000) if r.target == "search_attempts"] == []
        # And nothing hangs off an attempt that was never opened.
        assert recorder.current_search_attempt_id() is None

    @pytest.mark.parametrize("bad_id", ["lead-1", "", None, 7])
    def test_an_analysis_job_with_no_usable_id(self, started, bad_id):
        recorder.analysis_started({**_JOB, "id": bad_id})

        assert [r for r in started.pending(1000) if r.target == "analyses"] == []
        assert recorder.current_analysis_id() is None

    # A lead_search_id that is not a uuid is a nullable column, so it is dropped rather
    # than taking the analysis with it: the row is still worth having.
    def test_an_unusable_search_id_does_not_cost_the_analysis(self, started):
        recorder.analysis_started({**_JOB, "lead_search_id": "search-1"})

        analyses = [r for r in started.pending(1000) if r.target == "analyses"]
        assert len(analyses) == 1
        assert analyses[0].fields["lead_search_id"] is None
        assert_outbox_satisfies_contract(started)


class TestTheContractItself:
    """Guards on the copy, so it cannot drift into something that checks nothing."""

    def test_every_required_column_is_a_column(self):
        for name, table in CONTRACT.items():
            assert table.required <= set(table.columns), name

    def test_no_table_describes_a_column_the_api_always_refuses(self):
        for name, table in CONTRACT.items():
            assert not (set(table.columns) & NEVER_SENT), name

    def test_a_timestamp_without_an_offset_is_refused(self):
        assert TIMESTAMP.accepts("2026-10-31T12:00:00+01:00")
        assert TIMESTAMP.accepts("2026-10-31T12:00:00+00:00")
        assert not TIMESTAMP.accepts("2026-10-31T12:00:00")
        assert not TIMESTAMP.accepts("2026-10-31")
        assert not TIMESTAMP.accepts("ayer")

    def test_the_strict_types_are_strict(self):
        assert INTEGER.accepts(3) and not INTEGER.accepts("3")
        assert not INTEGER.accepts(3.5) and not INTEGER.accepts(True)
        assert BOOLEAN.accepts(False) and not BOOLEAN.accepts(1)
        assert NUMERIC.accepts(1) and NUMERIC.accepts(0.5) and not NUMERIC.accepts("0.5")
        assert UUID.accepts(_LEAD_ID) and not UUID.accepts("lead-1")
        assert JSON.accepts({}) and JSON.accepts([]) and not JSON.accepts("{}")
