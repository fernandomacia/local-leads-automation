"""Local outbox for audit records: written by the worker, drained to Neon by audit.sync.

One SQLite file per machine, outside the repository. Each put() is one fragment of a row
in the shared audit store, holding the columns known at that moment, and fragments are
drained in the order they were written: a row's first fragment, which carries its
required columns, always reaches Neon before the later ones that complete it.

SQLite suits this file and would not suit the shared store: here one process on one
machine writes it, and nothing syncs the file itself.

put() never raises. The worker calls it from inside a search and an analysis, and an
audit failure must never turn into a lead reported as failed. The read and bookkeeping
methods may raise; their only caller is the sync thread, which handles it.

Failures are logged under the "audit" logger hierarchy, which the audit log handler
leaves out so a broken outbox cannot feed on its own warnings.
"""

import json
import logging
import os
import sqlite3
import sys
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger(__name__)

# The tables of the shared store (audit/migrations). A fragment for anything else is a
# programming error, dropped with a warning rather than left to fail at every sync.
TARGETS = frozenset({
    "runs", "search_attempts", "cards", "analyses", "llm_calls", "log_entries",
})

# Synced records stay this long locally; they hold the same personal data as Neon.
SYNCED_RETENTION_DAYS = 7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    target      TEXT    NOT NULL,
    record_id   TEXT    NOT NULL,
    fields      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    synced_at   TEXT
);
CREATE INDEX IF NOT EXISTS records_pending_idx ON records (seq) WHERE synced_at IS NULL;
CREATE INDEX IF NOT EXISTS records_record_idx  ON records (target, record_id);
"""


def default_path() -> Path:
    """Where this machine keeps its outbox; AUDIT_OUTBOX_PATH overrides it."""
    if override := os.environ.get("AUDIT_OUTBOX_PATH"):
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "local-leads-automation" / "outbox.sqlite"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode(value):
    """JSON fallback for the values audit fields carry beyond plain JSON types."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, Decimal, Path)):
        return str(value)
    return str(value)


@dataclass(frozen=True)
class PendingRecord:
    seq: int
    target: str
    record_id: str
    fields: dict


class Outbox:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_path()
        self._ready = False
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        # A connection per operation: the worker thread and the sync thread both use the
        # outbox, and sqlite3 connections must not cross threads.
        return sqlite3.connect(self.path, timeout=5)

    def _ensure_schema(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as conn:
                # WAL lets the sync thread read while the worker writes.
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
            self._ready = True

    def put(self, target: str, record_id: str, fields: dict) -> bool:
        """Store one fragment of a row. Returns whether it was stored; never raises."""
        if target not in TARGETS:
            logger.warning("Audit fragment for unknown table %r dropped", target)
            return False
        try:
            payload = json.dumps(fields, ensure_ascii=False, default=_encode)
            self._ensure_schema()
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO records (target, record_id, fields, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (target, str(record_id), payload, _now()),
                )
            return True
        except Exception:
            logger.warning(
                "Audit fragment for %s %s could not be stored", target, record_id, exc_info=True,
            )
            return False

    def pending(self, limit: int = 200) -> list[PendingRecord]:
        """The oldest fragments not yet in Neon, in the order they were written."""
        self._ensure_schema()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT seq, target, record_id, fields FROM records "
                "WHERE synced_at IS NULL ORDER BY seq LIMIT ?",
                (limit,),
            ).fetchall()
        return [PendingRecord(seq, target, rid, json.loads(f)) for seq, target, rid, f in rows]

    def pending_count(self) -> int:
        self._ensure_schema()
        with closing(self._connect()) as conn:
            return conn.execute("SELECT count(*) FROM records WHERE synced_at IS NULL").fetchone()[0]

    def mark_synced(self, seqs: list[int]) -> None:
        if not seqs:
            return
        self._ensure_schema()
        stamp = _now()
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "UPDATE records SET synced_at = ? WHERE seq = ?", [(stamp, s) for s in seqs],
            )

    def prune(self, keep_days: int = SYNCED_RETENTION_DAYS) -> int:
        """Delete fragments synced more than keep_days ago. Unsynced ones are never touched."""
        self._ensure_schema()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "DELETE FROM records WHERE synced_at IS NOT NULL AND synced_at < ?", (cutoff,),
            )
            return cur.rowcount