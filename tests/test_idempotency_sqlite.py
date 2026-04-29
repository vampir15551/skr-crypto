"""Tests for the SQLite-backed idempotency store.

These verify durability across process restart, orphan recovery,
UnresolvedIdempotency on retry, and correct concurrency behavior on the
file-backed implementation.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from skr_crypto.server.idempotency import (
    SqliteIdempotencyStore,
    UnresolvedIdempotency,
)


@pytest.fixture()
def sqlite_path(tmp_path):
    return str(tmp_path / "idem.db")


class TestSqliteBasic:
    def test_reserve_then_commit_then_retry(self, sqlite_path):
        s = SqliteIdempotencyStore(sqlite_path)
        assert s.reserve("k1") is None
        s.commit("k1", "txid-1")
        assert s.reserve("k1") == "txid-1"
        assert s.peek("k1") == "txid-1"
        s.close()

    def test_release_frees_slot(self, sqlite_path):
        s = SqliteIdempotencyStore(sqlite_path)
        assert s.reserve("k") is None
        s.release("k")
        # After release, we can reserve again — like it never happened.
        assert s.reserve("k") is None
        s.close()

    def test_release_does_not_overwrite_committed(self, sqlite_path):
        s = SqliteIdempotencyStore(sqlite_path)
        s.reserve("k")
        s.commit("k", "real-tx")
        s.release("k")  # must be a no-op
        assert s.peek("k") == "real-tx"
        s.close()

    def test_commit_rejects_empty_txid(self, sqlite_path):
        s = SqliteIdempotencyStore(sqlite_path)
        s.reserve("k")
        with pytest.raises(ValueError):
            s.commit("k", "")
        s.close()

    def test_count(self, sqlite_path):
        s = SqliteIdempotencyStore(sqlite_path)
        assert s.count() == 0
        s.reserve("k1")
        s.commit("k1", "t1")
        s.reserve("k2")
        assert s.count() == 2
        s.close()


class TestDurability:
    """State survives close()/reopen — that's the whole point of SQLite."""

    def test_committed_keys_persist_across_reopen(self, sqlite_path):
        s1 = SqliteIdempotencyStore(sqlite_path)
        s1.reserve("k")
        s1.commit("k", "persistent-txid")
        s1.close()

        s2 = SqliteIdempotencyStore(sqlite_path)
        # Clean reopen — retry returns the same txid as duplicate.
        assert s2.reserve("k") == "persistent-txid"
        s2.close()


class TestOrphanRecovery:
    """Key invariant: if we crash mid-broadcast, a retry MUST NOT silently
    re-broadcast (risk of double-send). Orphan rows become 'unknown' and
    cause UnresolvedIdempotency."""

    def test_pending_rows_become_unknown_on_reopen(self, sqlite_path):
        s1 = SqliteIdempotencyStore(sqlite_path)
        s1.reserve("orphan-key")
        # Simulate crash: close without commit OR release.
        s1.close()

        # Fresh process.
        s2 = SqliteIdempotencyStore(sqlite_path)
        with pytest.raises(UnresolvedIdempotency) as exc_info:
            s2.reserve("orphan-key")
        assert exc_info.value.key == "orphan-key"
        assert exc_info.value.created_at > 0
        s2.close()

    def test_multiple_orphans_all_recovered(self, sqlite_path):
        s1 = SqliteIdempotencyStore(sqlite_path)
        for i in range(5):
            s1.reserve(f"orphan-{i}")
        # Plus one that was committed — should not be touched.
        s1.reserve("committed")
        s1.commit("committed", "good-txid")
        s1.close()

        s2 = SqliteIdempotencyStore(sqlite_path)
        # Committed key still works as duplicate.
        assert s2.reserve("committed") == "good-txid"
        # All orphans are now UNKNOWN.
        for i in range(5):
            with pytest.raises(UnresolvedIdempotency):
                s2.reserve(f"orphan-{i}")
        s2.close()

    def test_unknown_persists_across_further_reopens(self, sqlite_path):
        """An 'unknown' row stays 'unknown' — operator must reconcile
        manually (in practice, they'd DELETE the row from the DB once
        they've verified on-chain state)."""
        s1 = SqliteIdempotencyStore(sqlite_path)
        s1.reserve("k")
        s1.close()
        s2 = SqliteIdempotencyStore(sqlite_path)
        s2.close()  # promotes to unknown
        s3 = SqliteIdempotencyStore(sqlite_path)
        with pytest.raises(UnresolvedIdempotency):
            s3.reserve("k")
        s3.close()


class TestSqliteConcurrency:
    def test_concurrent_reserve_same_key_only_one_winner(self, sqlite_path):
        s = SqliteIdempotencyStore(sqlite_path)
        N = 10
        barrier = threading.Barrier(N)
        results: list[tuple[str, str | None]] = []
        results_lock = threading.Lock()

        def worker(idx: int):
            barrier.wait()
            r = s.reserve("shared")
            if r is None:
                time.sleep(0.02)
                s.commit("shared", "the-only-txid")
                with results_lock:
                    results.append(("winner", None))
            else:
                with results_lock:
                    results.append(("loser", r))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive(), "thread deadlocked"

        winners = [r for r in results if r[0] == "winner"]
        losers = [r for r in results if r[0] == "loser"]
        assert len(winners) == 1
        assert all(txid == "the-only-txid" for _, txid in losers)
        s.close()


class TestFactoryFallback:
    """If SQLite fails to open, the factory must fall back to in-memory.

    NOTE: we call _build_store() directly instead of reloading modules —
    reloading would create a new idempotency singleton, but app.routes has
    already captured the old reference via `from ... import idempotency`,
    which would desynchronize the two and break later tests.
    """

    def test_fallback_on_bad_path(self, monkeypatch, tmp_path):
        bad_path = tmp_path / "nonexistent_parent" / "idem.db"

        import skr_crypto.server.idempotency as idem_mod
        monkeypatch.setattr(idem_mod, "IDEMPOTENCY_DB_PATH", str(bad_path))

        store = idem_mod._build_store()
        assert isinstance(store, idem_mod.IdempotencyStore)
        # Verify it actually works
        assert store.reserve("k") is None
        store.commit("k", "tx")
        assert store.peek("k") == "tx"

    def test_empty_path_uses_in_memory(self, monkeypatch):
        import skr_crypto.server.idempotency as idem_mod
        monkeypatch.setattr(idem_mod, "IDEMPOTENCY_DB_PATH", "")
        store = idem_mod._build_store()
        assert isinstance(store, idem_mod.IdempotencyStore)

    def test_valid_path_uses_sqlite(self, monkeypatch, sqlite_path):
        import skr_crypto.server.idempotency as idem_mod
        monkeypatch.setattr(idem_mod, "IDEMPOTENCY_DB_PATH", sqlite_path)
        store = idem_mod._build_store()
        try:
            assert isinstance(store, idem_mod.SqliteIdempotencyStore)
        finally:
            store.close()


class TestSchemaIntegrity:
    """Spot-check raw DB contents to catch accidental schema regressions."""

    def test_schema_has_required_columns(self, sqlite_path):
        s = SqliteIdempotencyStore(sqlite_path)
        s.reserve("k")
        s.close()
        conn = sqlite3.connect(sqlite_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(idempotency)")}
        assert {"key", "txid", "status", "process_id", "created_at", "updated_at"} <= cols
        conn.close()

    def test_wal_mode_enabled(self, sqlite_path):
        s = SqliteIdempotencyStore(sqlite_path)
        conn = sqlite3.connect(sqlite_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
        # Depending on the platform it's "wal" or "memory" under some tmpfs —
        # but on a real file it must be wal.
        assert mode == "wal"
        conn.close()
        s.close()
