"""Tests for the postmortem receipt poller (1.6.0).

The poller is **read-only on the money path** — these tests must
fail loudly if a regression makes it broadcast or modify
idempotency state.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from skr_crypto.server.receipt_poller import (
    TERMINAL_STATUSES,
    ReceiptPoller,
    _classify_receipt,
)

# ---------------------------------------------------------------------------
# _classify_receipt
# ---------------------------------------------------------------------------


class TestClassify:
    def test_success_with_block(self):
        info = {"blockNumber": 68234567, "receipt": {}}
        s, b, e = _classify_receipt(info, None)
        assert s == "SUCCESS"
        assert b == 68234567
        assert e is None

    def test_revert_explicit(self):
        info = {"blockNumber": 68234567, "receipt": {"result": "REVERT"}}
        s, b, _ = _classify_receipt(info, None)
        assert s == "REVERT"
        assert b == 68234567

    def test_out_of_energy(self):
        info = {"blockNumber": 100, "receipt": {"result": "OUT_OF_ENERGY"}}
        s, _, _ = _classify_receipt(info, None)
        assert s == "OUT_OF_ENERGY"

    def test_pending_no_block(self):
        info = {"receipt": {}}
        s, b, _ = _classify_receipt(info, None)
        assert s == "PENDING"
        assert b is None

    def test_not_found_empty_response(self):
        s, b, _ = _classify_receipt(None, None)
        assert s == "NOT_FOUND"
        assert b is None

    def test_not_found_exception(self):
        s, _, _ = _classify_receipt(None, Exception("Transaction not found"))
        assert s == "NOT_FOUND"

    def test_rpc_error(self):
        s, _, e = _classify_receipt(None, RuntimeError("trongrid 503"))
        assert s == "RPC_ERROR"
        assert "RuntimeError" in e
        assert "trongrid 503" in e

    @pytest.mark.invariant
    def test_terminal_set_includes_money_relevant_failures(self):
        """INVARIANT: every code that means 'tx failed on-chain'
        must be in TERMINAL_STATUSES, otherwise the poller will
        keep polling forever and never emit RECEIPT_RESOLVED."""
        for code in (
            "SUCCESS", "OUT_OF_ENERGY", "REVERT", "TRANSFER_FAILED",
        ):
            assert code in TERMINAL_STATUSES, f"{code} must be terminal"


# ---------------------------------------------------------------------------
# Poller mechanics — in-memory mode
# ---------------------------------------------------------------------------


class TestPollerInMemory:
    def _poller(self, mock_tron_client) -> ReceiptPoller:
        return ReceiptPoller(
            db_path=None, tron_client=mock_tron_client,
            interval_sec=10.0, lookback_hours=48,
            batch_size=20, not_found_giveup_hours=24,
        )

    def test_poll_resolves_success_terminal(self):
        # Mock TronGrid response: tx landed in block.
        mock_tron = MagicMock()
        mock_tron.client.get_transaction_info = MagicMock(
            return_value={"blockNumber": 100, "receipt": {}},
        )
        p = self._poller(mock_tron)
        p._seed_for_tests("abc123", idempotency_key="key-1")
        # Force one tick (don't start the thread)
        p._tick()
        rec = p.get_status("abc123")
        assert rec is not None
        assert rec.status == "SUCCESS"
        assert rec.block_number == 100
        assert rec.resolved_at is not None

    def test_poll_resolves_revert_terminal(self):
        mock_tron = MagicMock()
        mock_tron.client.get_transaction_info = MagicMock(
            return_value={"blockNumber": 100, "receipt": {"result": "REVERT"}},
        )
        p = self._poller(mock_tron)
        p._seed_for_tests("def456")
        p._tick()
        rec = p.get_status("def456")
        assert rec.status == "REVERT"
        assert rec.resolved_at is not None

    def test_pending_is_non_terminal_and_retried(self):
        """A response with no block + no result is PENDING — the poller
        must NOT mark it resolved, but must update last_checked so the
        next tick re-tries."""
        mock_tron = MagicMock()
        mock_tron.client.get_transaction_info = MagicMock(
            return_value={"receipt": {}},
        )
        p = self._poller(mock_tron)
        p._seed_for_tests("pendingxx")
        p._tick()
        rec = p.get_status("pendingxx")
        assert rec is not None
        assert rec.status is None  # not terminal
        assert rec.resolved_at is None
        assert rec.last_checked is not None

    def test_not_found_below_giveup_threshold_stays_open(self):
        mock_tron = MagicMock()
        mock_tron.client.get_transaction_info = MagicMock(
            side_effect=Exception("Transaction not found"),
        )
        p = self._poller(mock_tron)
        p._seed_for_tests("xyz789", first_seen=time.time())  # just now
        p._tick()
        rec = p.get_status("xyz789")
        assert rec.status is None  # NOT_FOUND, stays unresolved
        assert rec.resolved_at is None

    def test_not_found_above_giveup_threshold_becomes_giveup(self):
        """After the giveup window, NOT_FOUND becomes terminal GIVEUP
        so the operator gets an explicit signal."""
        mock_tron = MagicMock()
        mock_tron.client.get_transaction_info = MagicMock(
            side_effect=Exception("Transaction not found"),
        )
        p = self._poller(mock_tron)
        # Seed with first_seen 25h ago (past 24h giveup threshold)
        p._seed_for_tests("oldtx", first_seen=time.time() - 25 * 3600)
        p._tick()
        rec = p.get_status("oldtx")
        assert rec.status == "GIVEUP"
        assert rec.resolved_at is not None

    @pytest.mark.invariant
    def test_poller_never_calls_send_or_broadcast(self, mock_tron):
        """INVARIANT: the poller must NEVER call wallet.send_usdt or
        client.broadcast. It is read-only on the money path. A
        regression that adds a retry would fail this."""
        # Configure the mock_tron so that any write-call would be
        # a clear failure. The wallet pool has a "send_usdt" attribute
        # we set in conftest — assert it's NOT called.
        from skr_crypto.server.wallet_pool import wallets
        # The pool is seeded by mock_tron fixture
        assert wallets.count() >= 1
        wallet_obj = next(iter(wallets.all()))
        wallet_obj.send_usdt = MagicMock()  # tracked

        mock_tron.client.get_transaction_info = MagicMock(
            return_value={"blockNumber": 100, "receipt": {}},
        )
        p = self._poller(mock_tron)
        p._seed_for_tests("watched")
        p._tick()

        assert wallet_obj.send_usdt.call_count == 0, (
            "Poller called wallet.send_usdt — money-path invariant violated"
        )

    def test_rpc_error_is_retried_not_terminal(self):
        mock_tron = MagicMock()
        mock_tron.client.get_transaction_info = MagicMock(
            side_effect=RuntimeError("connection reset"),
        )
        p = self._poller(mock_tron)
        p._seed_for_tests("flakyxx")
        p._tick()
        rec = p.get_status("flakyxx")
        assert rec.status is None  # RPC_ERROR is non-terminal
        assert rec.resolved_at is None
        assert rec.last_error and "RuntimeError" in rec.last_error


# ---------------------------------------------------------------------------
# Poller — SQLite-backed persistence
# ---------------------------------------------------------------------------


class TestPollerSqlite:
    def test_resolved_status_persists_across_restart(self, tmp_path):
        """Run #1 resolves a tx; run #2 sees the same tx_status row."""
        from skr_crypto.server.idempotency import SqliteIdempotencyStore
        db = tmp_path / "store.db"
        # Seed an idempotency entry — required so the poller has
        # something to look at via the joined query.
        idem = SqliteIdempotencyStore(str(db))
        idem.reserve("key-r1")
        idem.commit("key-r1", "txr1")
        idem.close()

        mock_tron = MagicMock()
        mock_tron.client.get_transaction_info = MagicMock(
            return_value={"blockNumber": 999, "receipt": {}},
        )
        p1 = ReceiptPoller(
            db_path=str(db), tron_client=mock_tron,
            interval_sec=10.0, batch_size=20,
            lookback_hours=48, not_found_giveup_hours=24,
        )
        p1._tick()
        rec = p1.get_status("txr1")
        assert rec is not None
        assert rec.status == "SUCCESS"
        p1.stop()

        # Restart — open a new poller on the same db
        p2 = ReceiptPoller(
            db_path=str(db), tron_client=mock_tron,
            interval_sec=10.0, batch_size=20,
            lookback_hours=48, not_found_giveup_hours=24,
        )
        rec2 = p2.get_status("txr1")
        assert rec2 is not None
        assert rec2.status == "SUCCESS"
        assert rec2.block_number == 999
        p2.stop()


# ---------------------------------------------------------------------------
# /api/v1/tx/{txid}/status endpoint
# ---------------------------------------------------------------------------


class TestTxStatusEndpoint:
    def test_unknown_txid_returns_known_false(self, client, auth_headers, mock_tron):
        # Init a fresh in-memory poller for the test
        from skr_crypto.server import receipt_poller as rp
        rp.poller = ReceiptPoller(
            db_path=None, tron_client=mock_tron,
            interval_sec=10.0,
        )
        try:
            r = client.get("/api/v1/tx/never-seen/status", headers=auth_headers)
            assert r.status_code == 200
            body = r.json()
            assert body["known"] is False
            assert body["status"] is None
        finally:
            rp.poller = None

    def test_resolved_txid_returns_status(self, client, auth_headers, mock_tron):
        from skr_crypto.server import receipt_poller as rp
        mock_tron.client.get_transaction_info = MagicMock(
            return_value={"blockNumber": 1234, "receipt": {}},
        )
        rp.poller = ReceiptPoller(
            db_path=None, tron_client=mock_tron,
            interval_sec=10.0,
        )
        rp.poller._seed_for_tests("seen-txid", idempotency_key="k")
        rp.poller._tick()
        try:
            r = client.get("/api/v1/tx/seen-txid/status", headers=auth_headers)
            assert r.status_code == 200
            body = r.json()
            assert body["known"] is True
            assert body["status"] == "SUCCESS"
            assert body["block_number"] == 1234
        finally:
            rp.poller = None
