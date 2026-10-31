"""Tests for audit/outbox.py — the local buffer everything audited waits in."""

import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from audit.outbox import api_slug, Outbox, default_path


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
    """Where the outbox lives, and why there is one of them per API.

    The file is per machine *and* per API. Fragments written against one API can only be
    sent to that one — a search attempt names a `lead_search_id` the other has never heard
    of — and the failure the separation really prevents is the asymmetric one: a local run
    left with fragments pending, uploaded into production the next time the worker is
    started against it.
    """

    def test_the_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUDIT_OUTBOX_PATH", str(tmp_path / "x.sqlite"))
        # Even with an API named, which is what makes it an override rather than a default.
        assert default_path("https://platform.segurseo.com") == tmp_path / "x.sqlite"

    def test_linux_follows_xdg(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AUDIT_OUTBOX_PATH", raising=False)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert default_path("http://127.0.0.1:8000") == (
            tmp_path / "local-leads-automation" / "outbox-127.0.0.1-8000.sqlite"
        )

    def test_windows_uses_local_app_data(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AUDIT_OUTBOX_PATH", raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert default_path("http://127.0.0.1:8000") == (
            tmp_path / "local-leads-automation" / "outbox-127.0.0.1-8000.sqlite"
        )

    def test_the_local_api_and_production_never_share_a_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AUDIT_OUTBOX_PATH", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

        local = default_path("http://127.0.0.1:8000")
        production = default_path("https://platform.segurseo.com")

        assert local != production
        assert local.name == "outbox-127.0.0.1-8000.sqlite"
        assert production.name == "outbox-platform.segurseo.com.sqlite"
        # Same directory as always: what changed is the file, not where it lives.
        assert local.parent == production.parent

    # Two local APIs on one machine are two stores, so the port has to be in the name.
    def test_the_port_distinguishes_two_local_apis(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AUDIT_OUTBOX_PATH", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

        assert default_path("http://127.0.0.1:8000") != default_path("http://127.0.0.1:8001")

    def test_the_old_single_file_is_simply_not_used(self, monkeypatch, tmp_path):
        """It is neither migrated nor deleted: nothing can say which API it was written for.

        That is the very reason the name changed, so guessing would be the mistake this
        prevents — and deleting it would throw away fragments somebody may still want to
        read.
        """
        monkeypatch.delenv("AUDIT_OUTBOX_PATH", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        legacy = tmp_path / "local-leads-automation" / "outbox.sqlite"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"old")

        assert default_path("http://127.0.0.1:8000") != legacy

        Outbox(default_path("http://127.0.0.1:8000")).put("runs", "r1", {"machine": "m"})

        assert legacy.read_bytes() == b"old"

    @pytest.mark.parametrize("url, expected", [
        ("http://127.0.0.1:8000", "127.0.0.1-8000"),
        ("https://platform.segurseo.com", "platform.segurseo.com"),
        ("https://platform.segurseo.com/", "platform.segurseo.com"),
        ("https://PLATFORM.Segurseo.COM", "platform.segurseo.com"),
        ("http://localhost:8000", "localhost-8000"),
        # config.py admits ::1 as a local host, so this is reachable — and the colons would
        # otherwise be stripped down to a bare "1".
        ("http://[::1]:8000", "ipv6--1-8000"),
        # Never a crash and never an empty name: an outbox that cannot be named still has
        # to be writable, because the alternative is a run that refuses to start.
        ("", "unknown"),
        ("not a url", "unknown"),
        ("http://", "unknown"),
    ])
    def test_the_name_is_safe_and_readable(self, url, expected):
        assert api_slug(url) == expected
        # Nothing that could escape the directory or need quoting in a shell.
        assert "/" not in expected and " " not in expected


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