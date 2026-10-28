"""Tests for audit/sync.py — run against a fake connection, never the real store."""

import psycopg
import pytest
from psycopg.types.json import Jsonb

from audit.outbox import Outbox, PendingRecord
from audit.sync import Syncer, build_statements


class FakeCursor:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class FakeConnection:
    """A store that knows which rows exist and checks NOT NULL the way Postgres does.

    Postgres checks NOT NULL on the row an INSERT proposes before ON CONFLICT is
    considered, so an INSERT for a fragment that completes a row is refused even when
    the row is already there. That is the behaviour that broke the first version.
    """

    REQUIRED = {"runs": "machine"}

    def __init__(self, refuse=(), down=False, existing=()):
        self.refuse = set(refuse)
        self.down = down
        self.rows = set(existing)
        self.written: list[tuple[str, str]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, statement, params=None):
        if self.down:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        if not params:
            return FakeCursor(0)  # SAVEPOINT / RELEASE / ROLLBACK TO

        is_update = statement.startswith("UPDATE")
        rid = params[-1] if is_update else params[0]
        if rid in self.refuse:
            raise psycopg.errors.UndefinedColumn('column "machne" does not exist')

        if is_update:
            if rid not in self.rows:
                return FakeCursor(0)
            self.written.append(("update", rid))
            return FakeCursor(1)

        table = statement.split('"')[1]
        required = self.REQUIRED.get(table)
        if required and f'"{required}"' not in statement:
            raise psycopg.errors.NotNullViolation(f'null value in column "{required}"')
        self.rows.add(rid)
        self.written.append(("insert", rid))
        return FakeCursor(1)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture
def outbox(tmp_path) -> Outbox:
    return Outbox(tmp_path / "outbox.sqlite")


def _syncer(outbox, *connections) -> tuple[Syncer, list]:
    made = []
    pool = list(connections)

    def connect(dsn):
        conn = pool.pop(0)
        made.append(conn)
        return conn

    return Syncer(outbox, "postgresql://test", connect=connect, idle_interval=60), made


class TestBuildStatements:
    def test_a_fragment_updates_its_own_columns_or_inserts_them(self):
        update, insert = build_statements(
            PendingRecord(1, "runs", "r1", {"machine": "debian", "environment": "local"}))

        assert update == (
            'UPDATE "runs" SET "machine" = %s, "environment" = %s WHERE "id" = %s',
            ["debian", "local", "r1"],
        )
        assert insert == (
            'INSERT INTO "runs" ("id", "machine", "environment") VALUES (%s, %s, %s) '
            'ON CONFLICT (id) DO UPDATE SET "machine" = EXCLUDED."machine", '
            '"environment" = EXCLUDED."environment"',
            ["r1", "debian", "local"],
        )

    def test_a_fragment_with_no_columns_only_makes_sure_the_row_exists(self):
        update, (statement, params) = build_statements(PendingRecord(1, "runs", "r1", {}))
        assert update is None
        assert statement.endswith("ON CONFLICT (id) DO NOTHING")
        assert params == ["r1"]

    def test_dicts_and_lists_go_as_jsonb(self):
        update, _ = build_statements(
            PendingRecord(1, "analyses", "a1", {"result": {"cms": "wordpress"}}))
        assert isinstance(update[1][0], Jsonb)

    def test_a_column_name_that_is_not_an_identifier_is_refused(self):
        with pytest.raises(ValueError):
            build_statements(PendingRecord(1, "runs", "r1", {'machine"; DROP TABLE runs; --': 1}))

    def test_an_unknown_table_is_refused(self):
        with pytest.raises(ValueError):
            build_statements(PendingRecord(1, "leads", "x", {"a": 1}))


class TestSyncOnce:
    def test_a_batch_goes_up_in_order_in_one_transaction(self, outbox):
        for rid in ("r1", "r2", "r3"):
            outbox.put("runs", rid, {"machine": "debian"})
        conn = FakeConnection()
        syncer, _ = _syncer(outbox, conn)

        assert syncer.sync_once() == 3

        assert conn.written == [("insert", "r1"), ("insert", "r2"), ("insert", "r3")]
        assert conn.commits == 1
        assert outbox.pending_count() == 0

    def test_a_fragment_completing_a_row_in_the_same_batch_is_an_update(self, outbox):
        # The case the first version got wrong against the real store.
        outbox.put("runs", "r1", {"machine": "debian"})
        outbox.put("runs", "r1", {"end_reason": "stopped"})
        conn = FakeConnection()
        syncer, _ = _syncer(outbox, conn)

        assert syncer.sync_once() == 2

        assert conn.written == [("insert", "r1"), ("update", "r1")]
        assert outbox.failed_count() == 0

    def test_a_fragment_completing_a_row_already_in_the_store_is_an_update(self, outbox):
        outbox.put("runs", "r1", {"end_reason": "stopped"})
        conn = FakeConnection(existing={"r1"})
        syncer, _ = _syncer(outbox, conn)

        assert syncer.sync_once() == 1

        assert conn.written == [("update", "r1")]
        assert outbox.failed_count() == 0

    def test_nothing_pending_opens_no_connection(self, outbox):
        syncer, made = _syncer(outbox)
        assert syncer.sync_once() == 0
        assert made == []

    def test_a_refused_fragment_is_set_aside_and_the_rest_go_up(self, outbox):
        for rid in ("ok1", "bad", "ok2"):
            outbox.put("runs", rid, {"machine": "debian"})
        conn = FakeConnection(refuse={"bad"})
        syncer, _ = _syncer(outbox, conn)

        assert syncer.sync_once() == 3

        assert conn.rollbacks == 1
        assert ("insert", "ok1") in conn.written and ("insert", "ok2") in conn.written
        assert outbox.pending_count() == 0
        assert outbox.failed_count() == 1

    def test_a_dropped_connection_marks_nothing_and_reconnects_next_time(self, outbox):
        for rid in ("r1", "r2"):
            outbox.put("runs", rid, {"machine": "debian"})
        down, up = FakeConnection(down=True), FakeConnection()
        syncer, made = _syncer(outbox, down, up)

        with pytest.raises(psycopg.OperationalError):
            syncer.sync_once()
        assert down.closed
        assert outbox.pending_count() == 2
        assert outbox.failed_count() == 0

        assert syncer.sync_once() == 2
        assert made == [down, up]
        assert outbox.pending_count() == 0


class TestLifecycle:
    def test_without_a_dsn_nothing_starts_and_records_stay_local(self, outbox):
        outbox.put("runs", "r1", {"machine": "debian"})
        syncer = Syncer(outbox, "")

        syncer.start()

        assert syncer._thread is None
        assert syncer.stop(timeout=0.1) == 1

    def test_stop_drains_what_is_left(self, outbox):
        conn = FakeConnection()
        syncer, _ = _syncer(outbox, conn)
        syncer.start()
        for rid in ("r1", "r2"):
            outbox.put("runs", rid, {"machine": "debian"})

        assert syncer.stop(timeout=5) == 0
        assert {rid for _, rid in conn.written} == {"r1", "r2"}
        assert conn.closed