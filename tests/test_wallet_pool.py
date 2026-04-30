"""Tests for WalletPool — resolution, auto-pick, multi-wallet routing."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from skr_crypto.server.wallet import Wallet
from skr_crypto.server.wallet_pool import (
    WalletAutoPickFailed,
    WalletNotFound,
    WalletPool,
    WalletPoolEmpty,
    WalletPoolError,
)


def _make_wallet(name: str, address: str | None = None) -> Wallet:
    return Wallet(
        name=name,
        address=address or f"T{name}{'X' * (33 - len(name))}",
        priv_key=MagicMock(),
    )


class TestEmpty:
    def test_uninitialised_resolve_raises(self):
        pool = WalletPool()
        with pytest.raises(WalletPoolEmpty):
            pool.resolve(None)

    def test_count_zero(self):
        assert WalletPool().count() == 0


class TestSingleWallet:
    def test_resolve_no_name_returns_only_wallet(self):
        pool = WalletPool()
        w = _make_wallet("solo")
        pool._override_for_tests([w])
        assert pool.resolve(None) is w

    def test_resolve_explicit_name_returns_it(self):
        pool = WalletPool()
        w = _make_wallet("solo")
        pool._override_for_tests([w])
        assert pool.resolve("solo") is w

    def test_resolve_unknown_name_raises(self):
        pool = WalletPool()
        pool._override_for_tests([_make_wallet("solo")])
        with pytest.raises(WalletNotFound) as exc_info:
            pool.resolve("ghost")
        assert exc_info.value.name == "ghost"
        assert exc_info.value.available == ["solo"]


class TestMultiWalletAutoPick:
    def _pool_two(self, balances):
        """Pool with two wallets a/b and a tron_client whose
        get_usdt_balance_for honours the ``balances`` dict."""
        pool = WalletPool()
        a = _make_wallet("a")
        b = _make_wallet("b")
        pool._override_for_tests([a, b])
        tron = MagicMock()
        tron.get_usdt_balance_for = MagicMock(
            side_effect=lambda addr: balances[addr],
        )
        return pool, a, b, tron

    def test_auto_pick_returns_max_usdt(self):
        pool, _a, _b, tron = self._pool_two({
            a_addr: Decimal("100") for a_addr in []  # placeholder
        })
        # Reseed with real addresses
        a = pool.get("a")
        b = pool.get("b")
        balances = {a.address: Decimal("100"), b.address: Decimal("500")}
        tron.get_usdt_balance_for = MagicMock(
            side_effect=lambda addr: balances[addr],
        )
        chosen = pool.resolve(None, tron_client=tron)
        assert chosen is b

    def test_auto_pick_explicit_name_skips_balance_lookup(self):
        pool, a, _b, tron = self._pool_two({})
        # Even with no balances, explicit name resolves cleanly.
        chosen = pool.resolve("a", tron_client=tron)
        assert chosen is a
        assert tron.get_usdt_balance_for.call_count == 0

    def test_auto_pick_no_tron_client_raises(self):
        pool, _a, _b, _ = self._pool_two({})
        with pytest.raises(WalletPoolError, match="auto-pick"):
            pool.resolve(None)  # no tron_client

    def test_auto_pick_skips_failing_lookup_picks_other(self):
        pool, a, b, tron = self._pool_two({})
        balances = {b.address: Decimal("50")}

        def lookup(addr):
            if addr == a.address:
                raise RuntimeError("trongrid 503")
            return balances[addr]

        tron.get_usdt_balance_for = MagicMock(side_effect=lookup)
        chosen = pool.resolve(None, tron_client=tron)
        assert chosen is b

    def test_auto_pick_all_failures_raises(self):
        pool, _a, _b, tron = self._pool_two({})
        tron.get_usdt_balance_for = MagicMock(
            side_effect=RuntimeError("everything down"),
        )
        with pytest.raises(WalletAutoPickFailed):
            pool.resolve(None, tron_client=tron)

    def test_auto_pick_unknown_policy_raises(self):
        pool, _a, _b, tron = self._pool_two({})
        with pytest.raises(WalletPoolError, match="unknown auto-pick policy"):
            pool.resolve(None, tron_client=tron, policy="round-robin")


class TestPoolListing:
    def test_names_returns_sorted(self):
        pool = WalletPool()
        pool._override_for_tests([
            _make_wallet("zebra"),
            _make_wallet("alpha"),
            _make_wallet("mike"),
        ])
        assert pool.names() == ["alpha", "mike", "zebra"]
