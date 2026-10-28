"""Tests for audit/outbox.py — the local buffer the sync thread drains to Neon."""

import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from audit.outbox import Outbox, default_path


@pytest.fixture
def outbox(tmp_path) -> Outbox:
    return Outbox(tmp_path / "outbox.sqlite")


class TestPut:
    def test_fragments_come_back_in_the_order_they_were_written(self, outbox):
        outbox.put("runs", "r1", {"machine": "debian"})
        outbox.put("analyses", "a1", {"run_id": "r1", "api_lead_id": "42"})
        outbox.put("analyses", "a1", {"outcome": "analysed"})

        pending = outbox.pending()

        assert [(p.target, p.record_id) for p in pending] == [
            ("runs", "r1"), ("analyses", "a1"), ("analyses", "a1"),
        ]
        assert pending[2].fields == {"outcome": "analysed"}

    def test_dates_and_uuids_are_stored_as_text(self, outbox):
        when = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
        rid = uuid.uuid4()
        outbox.put("runs", str(rid), {"id_copy": rid, "started_at": when})

        fields = outbox.pending()[0].fields

        assert fields == {"id_copy": str(rid), "started_at": "2026-10-28T12:00:00+00:00"}

    def test_accented_text_survives(self, outbox):
        outbox.put("cards", "c1", {"business_name": "Fontanería Martínez"})
        assert outbox.pending()[0].fields["business_name"] == "Fontanería Martínez"

    def test_an_unknown_table_is_dropped(self, outbox):
        assert outbox.put("leads", "x", {}) is False
        assert outbox.pending_count() == 0

    def test_a_store_that_cannot_be_written_never_raises(self, tmp_path):
        # The parent "directory" is a file, so the outbox cannot be created.
        blocker = tmp_path / "blocker"
        blocker.write_text("")
        broken = Outbox(blocker / "outbox.sqlite")

        assert broken.put("runs", "r1", {"machine": "debian"}) is False

    def test_two_threads_writing_at_once_lose_nothing(self, outbox):
        def write(prefix):
            for i in range(100):
                outbox.put("log_entries", f"{prefix}{i}", {"level": "WARNING"})

        threads = [threading.Thread(target=write, args=(p,)) for p in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert outbox.pending_count() == 200


class TestSyncBookkeeping:
    def test_synced_fragments_leave_the_pending_list(self, outbox):
        outbox.put("runs", "r1", {"machine": "debian"})
        outbox.put("runs", "r1", {"last_seen_at": "2026-10-28T12:00:00+00:00"})

        first = outbox.pending()[0]
        outbox.mark_synced([first.seq])

        assert [p.seq for p in outbox.pending()] == [first.seq + 1]
        assert outbox.pending_count() == 1

    def test_prune_drops_only_old_synced_fragments(self, outbox):
        for rid in ("old-synced", "new-synced", "old-unsynced"):
            outbox.put("runs", rid, {"machine": "debian"})
        seqs = {p.record_id: p.seq for p in outbox.pending()}
        outbox.mark_synced([seqs["old-synced"], seqs["new-synced"]])

        with sqlite3.connect(outbox.path) as conn:
            conn.execute("UPDATE records SET synced_at = '2020-01-01T00:00:00+00:00' "
                         "WHERE record_id = 'old-synced'")
            conn.execute("UPDATE records SET created_at = '2020-01-01T00:00:00+00:00' "
                         "WHERE record_id = 'old-unsynced'")

        assert outbox.prune() == 1

        with sqlite3.connect(outbox.path) as conn:
            left = {r[0] for r in conn.execute("SELECT record_id FROM records")}
        assert left == {"new-synced", "old-unsynced"}


class TestDefaultPath:
    def test_the_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUDIT_OUTBOX_PATH", str(tmp_path / "x.sqlite"))
        assert default_path() == tmp_path / "x.sqlite"

    def test_linux_follows_xdg(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AUDIT_OUTBOX_PATH", raising=False)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert default_path() == tmp_path / "local-leads-automation" / "outbox.sqlite"

    def test_windows_uses_local_app_data(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AUDIT_OUTBOX_PATH", raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert default_path() == tmp_path / "local-leads-automation" / "outbox.sqlite"


class TestFailedFragments:
    def test_a_failed_fragment_leaves_the_queue_and_keeps_its_error(self, outbox):
        outbox.put("runs", "bad", {"machine": "debian"})
        outbox.put("runs", "good", {"machine": "debian"})
        bad = outbox.pending()[0]

        outbox.mark_failed(bad.seq, "UndefinedColumn: machne")

        assert [p.record_id for p in outbox.pending()] == ["good"]
        assert outbox.pending_count() == 1
        assert outbox.failed_count() == 1
        with sqlite3.connect(outbox.path) as conn:
            error = conn.execute("SELECT error FROM records WHERE seq = ?", (bad.seq,)).fetchone()[0]
        assert error == "UndefinedColumn: machne"

    def test_failed_fragments_outlive_synced_ones(self, outbox):
        for rid in ("synced-20d", "failed-20d", "failed-40d"):
            outbox.put("runs", rid, {"machine": "debian"})
        seqs = {p.record_id: p.seq for p in outbox.pending()}
        outbox.mark_synced([seqs["synced-20d"]])
        outbox.mark_failed(seqs["failed-20d"], "x")
        outbox.mark_failed(seqs["failed-40d"], "x")

        twenty = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        forty = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        with sqlite3.connect(outbox.path) as conn:
            conn.execute("UPDATE records SET synced_at = ? WHERE record_id = 'synced-20d'", (twenty,))
            conn.execute("UPDATE records SET failed_at = ? WHERE record_id = 'failed-20d'", (twenty,))
            conn.execute("UPDATE records SET failed_at = ? WHERE record_id = 'failed-40d'", (forty,))

        assert outbox.prune() == 2

        with sqlite3.connect(outbox.path) as conn:
            left = {r[0] for r in conn.execute("SELECT record_id FROM records")}
        assert left == {"failed-20d"}