from __future__ import annotations

import threading
import time

import pytest

from skr_crypto.server.idempotency import IdempotencyConflict, IdempotencyStore


class TestIdempotencyStoreBasic:
    def test_reserve_returns_none_for_new_key(self):
        store = IdempotencyStore()
        assert store.reserve("k1") is None

    def test_reserve_then_commit_then_reserve_returns_txid(self):
        store = IdempotencyStore()
        assert store.reserve("k1") is None
        store.commit("k1", "txid1")
        # Second reserve returns committed txid as duplicate, never blocks.
        assert store.reserve("k1") == "txid1"

    def test_release_frees_slot(self):
        store = IdempotencyStore()
        assert store.reserve("k1") is None
        store.release("k1")
        # Slot is free again — a new reserve succeeds.
        assert store.reserve("k1") is None

    def test_release_does_not_overwrite_committed(self):
        store = IdempotencyStore()
        assert store.reserve("k1") is None
        store.commit("k1", "txid1")
        store.release("k1")  # must be a no-op
        assert store.reserve("k1") == "txid1"

    def test_commit_rejects_empty_txid(self):
        store = IdempotencyStore()
        store.reserve("k1")
        with pytest.raises(ValueError):
            store.commit("k1", "")

    def test_commit_rejects_pending_sentinel(self):
        store = IdempotencyStore()
        store.reserve("k1")
        with pytest.raises(ValueError):
            store.commit("k1", "__PENDING__")

    def test_different_keys_independent(self):
        store = IdempotencyStore()
        store.reserve("k1")
        store.commit("k1", "tx1")
        store.reserve("k2")
        store.commit("k2", "tx2")
        assert store.reserve("k1") == "tx1"
        assert store.reserve("k2") == "tx2"

    def test_count(self):
        store = IdempotencyStore()
        assert store.count() == 0
        store.reserve("k1")
        store.commit("k1", "t1")
        store.reserve("k2")
        store.commit("k2", "t2")
        assert store.count() == 2

    def test_peek_does_not_reserve(self):
        store = IdempotencyStore()
        assert store.peek("k1") is None
        # peek didn't take the slot, so we can still reserve normally.
        assert store.reserve("k1") is None
        # peek on PENDING returns None too (caller can't see the sentinel).
        assert store.peek("k1") is None
        store.commit("k1", "tx1")
        assert store.peek("k1") == "tx1"


class TestIdempotencyConcurrency:
    """Real concurrency tests — directly hit IdempotencyStore from threads.

    These tests do NOT go through TestClient (which serializes requests under
    the hood); they create a real race condition with threading.Barrier and
    verify that exactly one reservation is granted.
    """

    def test_concurrent_reserve_same_key_only_one_winner(self):
        """N threads race on reserve(); exactly one gets the slot, rest get the
        committed txid back."""
        store = IdempotencyStore()
        N = 20
        barrier = threading.Barrier(N)
        results: list[tuple[str, str | None]] = []
        results_lock = threading.Lock()

        def worker(idx: int):
            barrier.wait()
            r = store.reserve("shared-key")
            if r is None:
                # We won — simulate a slow broadcast then commit.
                time.sleep(0.05)
                store.commit("shared-key", "the-only-txid")
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
        assert len(winners) == 1, f"expected exactly 1 winner, got {len(winners)}"
        assert len(losers) == N - 1
        # Every loser must see the committed txid (not None, not PENDING).
        for _kind, txid in losers:
            assert txid == "the-only-txid", f"loser saw unexpected txid: {txid}"

    def test_release_after_reserve_lets_next_thread_in(self):
        """If holder releases (e.g. validation failed), a waiting thread must
        get a fresh reservation — not see a stale value."""
        store = IdempotencyStore()
        first_reserved = threading.Event()
        first_done = threading.Event()
        second_result: list = []

        def first():
            assert store.reserve("k") is None
            first_reserved.set()
            # Let second thread enter wait.
            time.sleep(0.05)
            store.release("k")
            first_done.set()

        def second():
            first_reserved.wait()
            # This will block on PENDING until first calls release().
            r = store.reserve("k")
            second_result.append(r)
            # We must now own the reservation — commit so the test cleans up.
            if r is None:
                store.commit("k", "second-tx")

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=second)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert first_done.is_set()
        # The second thread must have gotten a fresh reservation (None).
        assert second_result == [None]
        assert store.peek("k") == "second-tx"

    def test_reserve_wait_timeout_raises_conflict(self, monkeypatch):
        """If holder never commits or releases, waiters eventually raise."""
        # Patch the wait timeout to something tiny so the test is fast.
        import skr_crypto.server.idempotency as idem_mod
        monkeypatch.setattr(idem_mod, "_RESERVE_WAIT_TIMEOUT", 0.1)

        store = IdempotencyStore()
        assert store.reserve("k") is None  # holder never releases

        with pytest.raises(IdempotencyConflict):
            store.reserve("k")


class TestIdempotencyIntegration:
    """Test idempotency via the API — duplicate request returns same txid."""

    VALID_ADDRESS = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"

    def test_duplicate_returns_cached_txid(self, client, auth_headers, mock_tron):
        payload = {
            "to_address": self.VALID_ADDRESS,
            "amount": "50",
            "idempotency_key": "dup-test-key",
        }
        resp1 = client.post("/api/v1/send", json=payload, headers=auth_headers)
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "broadcast"

        resp2 = client.post("/api/v1/send", json=payload, headers=auth_headers)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "duplicate"
        assert resp2.json()["txid"] == resp1.json()["txid"]

        # send_usdt should only be called once
        assert mock_tron.send_usdt.call_count == 1

    def test_failed_send_releases_slot_so_retry_works(self, client, auth_headers, mock_tron):
        """If broadcast fails, the idempotency slot must be released so the
        client can retry with the same key and actually broadcast."""
        from unittest.mock import MagicMock

        payload = {
            "to_address": self.VALID_ADDRESS,
            "amount": "10",
            "idempotency_key": "release-test-key",
        }

        mock_tron.send_usdt = MagicMock(side_effect=RuntimeError("transient node error"))
        resp1 = client.post("/api/v1/send", json=payload, headers=auth_headers)
        assert resp1.status_code == 500

        # Now flip mock to success and retry — must NOT be reported as duplicate.
        mock_tron.send_usdt = MagicMock(return_value="retry-txid")
        resp2 = client.post("/api/v1/send", json=payload, headers=auth_headers)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "broadcast"
        assert resp2.json()["txid"] == "retry-txid"
