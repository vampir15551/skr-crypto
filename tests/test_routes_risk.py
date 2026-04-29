"""Integration tests for risk-flow on the HTTP layer.

  - GET /api/v1/risk/{address} — happy + invalid + external
  - POST /api/v1/send — refuses HIGH, allows LOW, audits SEND_REJECTED
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

VALID = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"
NULL_TRON = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"


def _setup_low_risk(mock_tron):
    """Make all risk checks pass cleanly so /send can proceed."""
    mock_tron.client.get_account = MagicMock(return_value={
        "create_time": 1700000000_000,
    })
    mock_tron.client.get_contract = MagicMock(side_effect=Exception("no contract"))
    mock_tron._get_usdt_contract().functions.isBlackListed = MagicMock(return_value=False)
    mock_tron.get_destination_info = MagicMock(return_value={
        "exists": True,
        "trx_balance": Decimal("1"),
        "usdt_balance": Decimal("100"),
    })


# ---------------------------------------------------------------------------
# GET /api/v1/risk/{address}
# ---------------------------------------------------------------------------


class TestRiskEndpoint:
    def test_low_risk_address(self, client, auth_headers, mock_tron):
        _setup_low_risk(mock_tron)
        resp = client.get(f"/api/v1/risk/{VALID}", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["address"] == VALID
        assert body["level"] == "low"
        assert isinstance(body["checks"], list)

    def test_invalid_address_returns_invalid(self, client, auth_headers):
        resp = client.get("/api/v1/risk/notanaddress", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["level"] == "invalid"

    def test_blacklisted_returns_high(self, client, auth_headers, mock_tron):
        _setup_low_risk(mock_tron)
        mock_tron._get_usdt_contract().functions.isBlackListed = MagicMock(return_value=True)
        resp = client.get(f"/api/v1/risk/{VALID}", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["level"] == "high"
        bl = next(c for c in body["checks"] if c["name"] == "usdt_blacklist")
        assert bl["status"] == "fail"

    def test_burn_address_returns_high(self, client, auth_headers, mock_tron):
        _setup_low_risk(mock_tron)
        resp = client.get(f"/api/v1/risk/{NULL_TRON}", headers=auth_headers)
        body = resp.json()
        assert body["level"] == "high"
        burn = next(c for c in body["checks"] if c["name"] == "burn_address")
        assert burn["status"] == "fail"

    def test_unauthenticated_rejected(self, client, mock_tron):
        resp = client.get(f"/api/v1/risk/{VALID}")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/send — preflight rejection on HIGH risk
# ---------------------------------------------------------------------------


class TestSendRiskPreflight:
    def _post(self, client, headers, *, to_address=VALID, key="key-risk-1"):
        return client.post(
            "/api/v1/send",
            headers=headers,
            json={
                "to_address": to_address,
                "amount": "10.5",
                "idempotency_key": key,
            },
        )

    def test_low_risk_send_proceeds(self, client, auth_headers, mock_tron):
        _setup_low_risk(mock_tron)
        # Plus the existing /send mocks (already in mock_tron defaults).
        resp = self._post(client, auth_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "broadcast"

    def test_high_risk_send_blocked(self, client, auth_headers, mock_tron):
        _setup_low_risk(mock_tron)
        mock_tron._get_usdt_contract().functions.isBlackListed = MagicMock(return_value=True)
        resp = self._post(client, auth_headers)
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "RISK_TOO_HIGH"
        assert body["level"] == "high"
        # Full report attached so client can show the operator without a
        # second call.
        assert "report" in body
        names = [c["name"] for c in body["report"]["checks"]]
        assert "usdt_blacklist" in names

    def test_burn_address_send_blocked(self, client, auth_headers, mock_tron):
        _setup_low_risk(mock_tron)
        resp = self._post(client, auth_headers, to_address=NULL_TRON)
        assert resp.status_code == 400
        assert resp.json()["code"] == "RISK_TOO_HIGH"

    def test_high_risk_does_not_burn_idempotency_slot(
        self, client, auth_headers, mock_tron,
    ):
        """A blocked /send must NOT poison the idempotency store —
        otherwise the operator can't retry with the same key after
        switching the destination."""
        from skr_crypto.server.idempotency import idempotency
        _setup_low_risk(mock_tron)
        mock_tron._get_usdt_contract().functions.isBlackListed = MagicMock(return_value=True)
        resp = self._post(client, auth_headers, key="key-not-poisoned")
        assert resp.status_code == 400
        # peek() should return None — slot was never reserved (we block
        # BEFORE idempotency.reserve()).
        assert idempotency.peek("key-not-poisoned") is None


# ---------------------------------------------------------------------------
# RISK_BLOCK_LEVEL=none — opt out
# ---------------------------------------------------------------------------


class TestRiskBlockLevelNone:
    def test_opt_out_lets_high_through(
        self, client, auth_headers, mock_tron, monkeypatch,
    ):
        """Operator can disable preflight blocking entirely (e.g. for
        a deliberate burn). The risk report still computes; we just
        don't refuse."""
        import skr_crypto.server.routes as routes_mod
        monkeypatch.setattr(routes_mod, "RISK_BLOCK_LEVEL", "none")
        _setup_low_risk(mock_tron)
        mock_tron._get_usdt_contract().functions.isBlackListed = MagicMock(return_value=True)
        resp = client.post(
            "/api/v1/send",
            headers=auth_headers,
            json={
                "to_address": VALID, "amount": "1.0",
                "idempotency_key": "key-opt-out",
            },
        )
        # We bypassed the gate — broadcast happens.
        assert resp.status_code == 200, resp.text
