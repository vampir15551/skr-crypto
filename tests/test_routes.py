from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

# Valid TRON testnet address (base58check valid, starts with T, prefix 0x41)
# Using a known valid Nile testnet address format
VALID_ADDRESS = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"


class TestSendEndpoint:
    def _send(self, client, auth_headers, **overrides):
        payload = {
            "to_address": VALID_ADDRESS,
            "amount": "100.50",
            "idempotency_key": "test-key-001",
        }
        payload.update(overrides)
        return client.post("/api/v1/send", json=payload, headers=auth_headers)

    def test_send_success(self, client, auth_headers, mock_tron):
        resp = self._send(client, auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["txid"] == "abc123txid"
        assert data["status"] == "broadcast"
        assert data["amount"] == "100.50"
        assert data["to_address"] == VALID_ADDRESS

    def test_send_returns_request_id(self, client, auth_headers):
        resp = self._send(client, auth_headers)
        assert "x-request-id" in resp.headers

    def test_send_custom_request_id(self, client, auth_headers):
        headers = {**auth_headers, "X-Request-ID": "my-req-123"}
        resp = self._send(client, headers)
        assert resp.headers["x-request-id"] == "my-req-123"

    def test_send_no_auth(self, client):
        resp = client.post("/api/v1/send", json={
            "to_address": VALID_ADDRESS,
            "amount": "100",
            "idempotency_key": "key",
        })
        assert resp.status_code == 401

    def test_send_insufficient_usdt(self, client, auth_headers, mock_tron):
        mock_tron.get_usdt_balance = MagicMock(return_value=Decimal("10"))
        resp = self._send(client, auth_headers, amount="1000")
        assert resp.status_code == 400
        assert "INSUFFICIENT_BALANCE" in resp.json()["code"]

    def test_send_low_trx(self, client, auth_headers, mock_tron):
        mock_tron.get_trx_balance = MagicMock(return_value=Decimal("1"))
        resp = self._send(client, auth_headers)
        assert resp.status_code == 400
        assert "INSUFFICIENT_BALANCE" in resp.json()["code"]

    def test_send_invalid_address_not_T(self, client, auth_headers):
        resp = self._send(client, auth_headers, to_address="A" * 34)
        assert resp.status_code == 400
        assert "INVALID_ADDRESS" in resp.json()["code"]

    def test_send_invalid_address_bad_checksum(self, client, auth_headers):
        resp = self._send(client, auth_headers, to_address="T" + "1" * 33)
        assert resp.status_code == 400
        assert "INVALID_ADDRESS" in resp.json()["code"]

    def test_send_tx_failure(self, client, auth_headers, mock_tron):
        mock_tron.send_usdt = MagicMock(side_effect=RuntimeError("node down"))
        resp = self._send(client, auth_headers)
        assert resp.status_code == 500
        assert resp.json()["code"] == "TX_FAILED"

    def test_send_validation_error_amount_zero(self, client, auth_headers):
        resp = self._send(client, auth_headers, amount="0")
        assert resp.status_code == 422

    def test_send_validation_error_amount_negative(self, client, auth_headers):
        resp = self._send(client, auth_headers, amount="-5")
        assert resp.status_code == 422


class TestBalanceEndpoint:
    def test_balance_success(self, client, auth_headers, mock_tron):
        resp = client.get("/api/v1/balance", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["trx"] == "100"
        assert data["usdt"] == "5000"
        assert data["address"] == mock_tron.address

    def test_balance_no_auth(self, client):
        resp = client.get("/api/v1/balance")
        assert resp.status_code == 401


class TestHealthEndpoint:
    def test_health_success(self, client, auth_headers, mock_tron):
        resp = client.get("/api/v1/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["network"] == "nile"
        assert data["node_connected"] is True
        assert "uptime_seconds" in data
        assert "shutdown_in_seconds" in data

    def test_health_node_disconnected(self, client, auth_headers, mock_tron):
        mock_tron.check_connection = MagicMock(return_value=False)
        resp = client.get("/api/v1/health", headers=auth_headers)
        data = resp.json()
        assert data["node_connected"] is False


class TestVersionEndpoint:
    """The /version endpoint is unauthenticated by design — dashboards
    poll it to know which build is deployed without carrying API keys."""

    def test_version_unauthenticated(self, client):
        resp = client.get("/api/v1/version")
        assert resp.status_code == 200
        data = resp.json()
        assert "git_sha" in data
        assert "started_at" in data
        assert "network" in data
        assert isinstance(data["started_at"], (int, float))
        assert data["network"] == "nile"

    def test_version_git_sha_is_string(self, client):
        resp = client.get("/api/v1/version")
        sha = resp.json()["git_sha"]
        # Either a 12-char short sha or "unknown" — never empty.
        assert isinstance(sha, str) and len(sha) > 0
