"""Multi-wallet routing tests — the wire format added in 1.4.0.

Covers:

  - /api/v1/send with explicit `wallet` field (named lookup)
  - /api/v1/send with omitted `wallet` (auto-pick by max USDT balance)
  - /api/v1/balance with ?wallet= query parameter
  - /api/v1/wallets listing endpoint
  - 404 on unknown wallet name
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from skr_crypto.server.wallet import Wallet
from skr_crypto.server.wallet_pool import wallets as pool

VALID_TO = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"


def _proxy_send_factory(tron):
    """Each test wallet shares a send_usdt that delegates to ``tron``'s
    legacy mock, mirroring what conftest.mock_tron does."""
    def send(_client, _contract, to_addr, amount, *, fee_limit_sun=None):
        return tron.send_usdt(to_addr, amount, fee_limit_sun=fee_limit_sun)
    return send


def _override_pool_two(tron):
    """Replace conftest's single-wallet pool with two named wallets."""
    a = Wallet(
        name="hot",
        address="THotxxxxxxxxxxxxxxxxxxxxxxxxxxxx00",
        priv_key=MagicMock(),
    )
    b = Wallet(
        name="cold",
        address="TColdxxxxxxxxxxxxxxxxxxxxxxxxxxx00",
        priv_key=MagicMock(),
    )
    a.send_usdt = _proxy_send_factory(tron)
    b.send_usdt = _proxy_send_factory(tron)
    pool._override_for_tests([a, b])
    return a, b


# ---------------------------------------------------------------------------
# /send named wallet
# ---------------------------------------------------------------------------


class TestSendWithExplicitWallet:
    def test_send_with_named_wallet_uses_that_wallets_address(
        self, client, auth_headers, mock_tron,
    ):
        _a, b = _override_pool_two(mock_tron)
        # Both wallets have plenty of balance.
        mock_tron.get_usdt_balance_for = MagicMock(return_value=Decimal("9999"))
        mock_tron.get_trx_balance_for = MagicMock(return_value=Decimal("100"))

        resp = client.post("/api/v1/send", json={
            "to_address": VALID_TO,
            "amount": "10",
            "idempotency_key": "test-named-wallet-1",
            "wallet": "cold",
        }, headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["wallet"] == "cold"
        assert data["from_address"] == b.address

    def test_send_with_unknown_wallet_returns_404(
        self, client, auth_headers, mock_tron,
    ):
        _override_pool_two(mock_tron)
        resp = client.post("/api/v1/send", json={
            "to_address": VALID_TO,
            "amount": "10",
            "idempotency_key": "test-unknown-1",
            "wallet": "ghost",
        }, headers=auth_headers)
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "WALLET_NOT_FOUND"
        assert body["wallet"] == "ghost"
        assert set(body["available"]) == {"hot", "cold"}


# ---------------------------------------------------------------------------
# /send auto-pick
# ---------------------------------------------------------------------------


class TestSendAutoPick:
    def test_omitted_wallet_picks_max_usdt(
        self, client, auth_headers, mock_tron,
    ):
        a, b = _override_pool_two(mock_tron)
        # cold has more than hot — auto-pick must choose cold.
        balances = {a.address: Decimal("10"), b.address: Decimal("9000")}
        mock_tron.get_usdt_balance_for = MagicMock(
            side_effect=lambda addr: balances.get(addr, Decimal("0")),
        )
        mock_tron.get_trx_balance_for = MagicMock(return_value=Decimal("100"))

        resp = client.post("/api/v1/send", json={
            "to_address": VALID_TO,
            "amount": "5",
            "idempotency_key": "test-autopick-1",
        }, headers=auth_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["wallet"] == "cold"

    def test_omitted_wallet_with_one_wallet_picks_it(
        self, client, auth_headers, mock_tron,
    ):
        # conftest already seeds a single wallet — exercise the
        # one-wallet shortcut where /send works with no wallet field.
        resp = client.post("/api/v1/send", json={
            "to_address": VALID_TO,
            "amount": "1",
            "idempotency_key": "test-single-shortcut-1",
        }, headers=auth_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["wallet"] == "default"


# ---------------------------------------------------------------------------
# /balance ?wallet=
# ---------------------------------------------------------------------------


class TestBalanceWithWalletParam:
    def test_balance_picks_named_wallet(self, client, auth_headers, mock_tron):
        a, b = _override_pool_two(mock_tron)
        balances_usdt = {a.address: Decimal("11"), b.address: Decimal("22")}
        balances_trx = {a.address: Decimal("1"), b.address: Decimal("2")}
        mock_tron.get_usdt_balance_for = MagicMock(
            side_effect=lambda addr: balances_usdt[addr],
        )
        mock_tron.get_trx_balance_for = MagicMock(
            side_effect=lambda addr: balances_trx[addr],
        )

        resp = client.get(
            "/api/v1/balance?wallet=hot",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["wallet"] == "hot"
        assert data["address"] == a.address
        assert data["usdt"] == "11"
        assert data["trx"] == "1"

    def test_balance_unknown_wallet_returns_404(
        self, client, auth_headers, mock_tron,
    ):
        _override_pool_two(mock_tron)
        resp = client.get(
            "/api/v1/balance?wallet=nonexistent",
            headers=auth_headers,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /wallets listing
# ---------------------------------------------------------------------------


class TestWalletsListing:
    def test_lists_all_wallets_with_balances(
        self, client, auth_headers, mock_tron,
    ):
        a, b = _override_pool_two(mock_tron)
        usdt = {a.address: Decimal("5"), b.address: Decimal("100")}
        mock_tron.get_usdt_balance_for = MagicMock(
            side_effect=lambda addr: usdt[addr],
        )
        resp = client.get("/api/v1/wallets", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert {w["wallet"] for w in body["wallets"]} == {"hot", "cold"}
        assert body["auto_pick"] == "cold"  # max USDT

    def test_failing_balance_marked_as_error_not_500(
        self, client, auth_headers, mock_tron,
    ):
        """A flaky RPC for one wallet must not nuke the whole listing —
        the surviving wallets should still show up."""
        _a, b = _override_pool_two(mock_tron)
        good = b.address

        def lookup(addr):
            if addr == good:
                return Decimal("42")
            raise RuntimeError("trongrid down")

        mock_tron.get_usdt_balance_for = MagicMock(side_effect=lookup)
        mock_tron.get_trx_balance_for = MagicMock(side_effect=lookup)

        resp = client.get("/api/v1/wallets", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        rows = {w["wallet"]: w for w in body["wallets"]}
        assert rows["cold"]["usdt"] == "42"
        assert rows["hot"]["usdt"] == "error"
        # The healthy wallet wins auto-pick.
        assert body["auto_pick"] == "cold"
