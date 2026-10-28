"""Drains the local audit outbox into the shared Neon store, from a background thread.

Runs inside the worker rather than the panel, so a run started from the terminal is
audited all the same. With no AUDIT_DATABASE_URL it does nothing and the outbox simply
keeps everything locally.

Each fragment first UPDATEs the columns it carries and, only if no row was there yet,
INSERTs them. Not a bare INSERT ... ON CONFLICT DO UPDATE: Postgres checks NOT NULL on
the row it is asked to insert before it looks for a conflict, so a fragment completing
a row (an outcome, an end time) was refused for lacking the columns its first fragment
had already written. Fragments go up in the order they were written, one batch per
transaction.

Two kinds of failure, handled differently:
  - transient (network down, Neon waking up, connection dropped): nothing is marked, the
    connection is thrown away and the whole batch is retried after a growing pause;
  - refusal (a column that does not exist, a value Neon cannot take): the batch is
    replayed one fragment at a time under savepoints, so every good fragment goes up
    and only the refused one is marked failed, with its error, out of the queue.

Never lets an exception reach the worker: the thread logs and waits, and stop() gives
the outbox a last few seconds to drain, leaving the rest for the next run.
"""

import logging
import re
import threading
import time
from collections.abc import Callable

import psycopg
from psycopg.types.json import Jsonb

from audit.outbox import TARGETS, Outbox, PendingRecord

logger = logging.getLogger(__name__)

# Failures that say nothing about the fragments: retry the batch later, mark nothing.
_TRANSIENT = (psycopg.OperationalError, psycopg.InterfaceError)

# Column names come from our own code, never from scraped data, but they are spliced
# into SQL, so anything that is not a plain lowercase identifier is refused outright.
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

_MAX_BACKOFF = 300.0


def build_statements(record: PendingRecord) -> tuple[tuple[str, list] | None, tuple[str, list]]:
    """The UPDATE to try first (None for a fragment with no columns) and the INSERT fallback."""
    if record.target not in TARGETS:
        raise ValueError(f"Unknown audit table {record.target!r}")
    columns = [c for c in record.fields if c != "id"]
    for column in columns:
        if not _IDENTIFIER.match(column):
            raise ValueError(f"Invalid audit column name {column!r}")

    values = [_adapt(record.fields[c]) for c in columns]
    table = f'"{record.target}"'

    update = None
    if columns:
        assignments = ", ".join(f'"{c}" = %s' for c in columns)
        update = (f'UPDATE {table} SET {assignments} WHERE "id" = %s', [*values, record.record_id])

    names = ["id", *columns]
    quoted = ", ".join(f'"{n}"' for n in names)
    placeholders = ", ".join(["%s"] * len(names))
    statement = f"INSERT INTO {table} ({quoted}) VALUES ({placeholders}) ON CONFLICT (id) DO "
    if columns:
        statement += "UPDATE SET " + ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in columns)
    else:
        statement += "NOTHING"
    insert = (statement, [record.record_id, *values])

    return update, insert


def _apply(conn, record: PendingRecord) -> None:
    """Write one fragment: update the row if it is there, insert it if not."""
    update, insert = build_statements(record)
    if update is not None and conn.execute(*update).rowcount:
        return
    # ON CONFLICT is only a backstop here: the row was not there a moment ago.
    conn.execute(*insert)


def _adapt(value):
    # Dicts and lists go to jsonb columns; everything else (text, numbers, booleans,
    # ISO timestamps as text) is cast by Postgres from the column's own type.
    if isinstance(value, (dict, list)):
        return Jsonb(value)
    return value


def _connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(
        dsn,
        connect_timeout=10,
        application_name="local-leads-automation",
        # A statement that hangs must not hold stop() past its deadline.
        options="-c statement_timeout=30s",
    )


class Syncer:
    def __init__(
        self,
        outbox: Outbox,
        dsn: str,
        *,
        batch_size: int = 200,
        idle_interval: float = 5.0,
        connect: Callable[[str], psycopg.Connection] = _connect,
    ) -> None:
        self.outbox = outbox
        self.dsn = dsn
        self.batch_size = batch_size
        self.idle_interval = idle_interval
        self._connect_fn = connect
        self._conn = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failing = False

    @property
    def enabled(self) -> bool:
        return bool(self.dsn)

    # ── Connection ────────────────────────────────────────────────────────────

    def _connection(self):
        if self._conn is None:
            self._conn = self._connect_fn(self.dsn)
        return self._conn

    def _discard_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # ── One batch ─────────────────────────────────────────────────────────────

    def sync_once(self) -> int:
        """Send the oldest pending batch. Returns how many fragments it settled.

        Raises on a transient failure, with nothing marked; the caller waits and retries.
        """
        batch = self.outbox.pending(self.batch_size)
        if not batch:
            return 0

        conn = self._connection()
        try:
            for record in batch:
                _apply(conn, record)
            conn.commit()
        except _TRANSIENT:
            self._discard_connection()
            raise
        except Exception:
            try:
                conn.rollback()
            except _TRANSIENT:
                self._discard_connection()
                raise
            return self._settle_one_by_one(batch)

        self.outbox.mark_synced([r.seq for r in batch])
        return len(batch)

    def _settle_one_by_one(self, batch: list[PendingRecord]) -> int:
        """Replay a refused batch under savepoints, to set aside only what Neon refuses."""
        conn = self._connection()
        synced: list[int] = []
        refused: list[tuple[PendingRecord, Exception]] = []
        try:
            for record in batch:
                conn.execute("SAVEPOINT fragment")
                try:
                    _apply(conn, record)
                except _TRANSIENT:
                    raise
                except Exception as exc:
                    conn.execute("ROLLBACK TO SAVEPOINT fragment")
                    refused.append((record, exc))
                else:
                    conn.execute("RELEASE SAVEPOINT fragment")
                    synced.append(record.seq)
            conn.commit()
        except _TRANSIENT:
            self._discard_connection()
            raise

        self.outbox.mark_synced(synced)
        for record, exc in refused:
            logger.warning(
                "Audit fragment %s for %s %s refused by the store: %s",
                record.seq, record.target, record.record_id, exc,
            )
            self.outbox.mark_failed(record.seq, f"{type(exc).__name__}: {exc}")
        return len(batch)

    # ── Background thread ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Start draining in the background; does nothing without a DSN."""
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="audit-sync", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self.outbox.prune()
        except Exception:
            logger.warning("Could not prune the audit outbox", exc_info=True)

        backoff = 0.0
        while not self._stop.is_set():
            try:
                sent = self.sync_once()
            except Exception as exc:
                # Logged once per outage, not once per retry.
                if not self._failing:
                    logger.warning("Audit store unreachable, keeping records locally: %s", exc)
                    self._failing = True
                backoff = min(max(backoff * 2, 2.0), _MAX_BACKOFF)
                self._stop.wait(backoff)
                continue

            if self._failing:
                logger.info("Audit store reachable again")
                self._failing = False
            backoff = 0.0
            if sent < self.batch_size:
                self._stop.wait(self.idle_interval)

    def stop(self, timeout: float = 10.0) -> int:
        """Stop the thread, try to drain what is left, and return how many stay pending."""
        deadline = time.monotonic() + timeout
        self._stop.set()
        if self._thread is not None:
            self._thread.join(max(0.0, deadline - time.monotonic()))

        # Drain only once the thread is gone: two threads must never share a connection.
        if self.enabled and (self._thread is None or not self._thread.is_alive()):
            while time.monotonic() < deadline:
                try:
                    if self.sync_once() == 0:
                        break
                except Exception:
                    break
            self._discard_connection()

        self._thread = None
        try:
            return self.outbox.pending_count()
        except Exception:
            return -1