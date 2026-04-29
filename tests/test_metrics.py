"""Tests for the Prometheus /metrics endpoint and counter/gauge wiring."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

VALID_ADDRESS = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"


def _metric_value(body: str, name: str, **labels) -> float | None:
    """Parse the Prometheus text output and return the value of a metric.

    Metric lines look like:
        payouts_tx_broadcast_total{result="success"} 2.0
        payouts_uptime_seconds 12.0
    """
    for line in body.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if labels:
            # Match any order of labels — Prometheus doesn't guarantee order.
            if not line.startswith(name):
                continue
            open_brace = line.find("{")
            close_brace = line.find("}")
            if open_brace < 0 or close_brace < 0:
                continue
            actual_labels = line[open_brace + 1:close_brace]
            actual = dict(
                p.split("=", 1) for p in actual_labels.split(",") if "=" in p
            )
            actual = {k: v.strip('"') for k, v in actual.items()}
            if all(actual.get(k) == str(v) for k, v in labels.items()):
                return float(line.rsplit(" ", 1)[1])
        else:
            head = line.split(" ", 1)[0]
            # Exact match (no labels).
            if head == name:
                return float(line.rsplit(" ", 1)[1])
    return None


class TestMetricsEndpoint:
    def test_metrics_endpoint_requires_auth(self, client):
        resp = client.get("/api/v1/metrics")
        assert resp.status_code == 401

    def test_metrics_endpoint_returns_prometheus_text(self, client, auth_headers):
        resp = client.get("/api/v1/metrics", headers=auth_headers)
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        body = resp.text
        # Spot-check that our custom metrics are registered (even if zero).
        assert "payouts_tx_broadcast_total" in body
        assert "payouts_uptime_seconds" in body
        assert "payouts_idempotency_store_size" in body

    def test_metrics_endpoint_bypasses_rate_limit(self, client, auth_headers):
        from skr_crypto.server.server import _limiter
        for _ in range(200):
            _limiter.check("testclient")
        resp = client.get("/api/v1/metrics", headers=auth_headers)
        assert resp.status_code == 200


class TestCounterWiring:
    def test_success_increments_broadcast_total(self, client, auth_headers, mock_tron):
        client.post("/api/v1/send", json={
            "to_address": VALID_ADDRESS,
            "amount": "10",
            "idempotency_key": "m-success",
        }, headers=auth_headers)

        body = client.get("/api/v1/metrics", headers=auth_headers).text
        assert _metric_value(body, "payouts_tx_broadcast_total", result="success") == 1.0

    def test_failure_increments_failed(self, client, auth_headers, mock_tron):
        mock_tron.send_usdt = MagicMock(side_effect=RuntimeError("node dead"))
        client.post("/api/v1/send", json={
            "to_address": VALID_ADDRESS,
            "amount": "10",
            "idempotency_key": "m-fail",
        }, headers=auth_headers)

        body = client.get("/api/v1/metrics", headers=auth_headers).text
        assert _metric_value(body, "payouts_tx_broadcast_total", result="failed") == 1.0

    def test_duplicate_increments_duplicate_total(self, client, auth_headers, mock_tron):
        payload = {
            "to_address": VALID_ADDRESS,
            "amount": "10",
            "idempotency_key": "m-dup",
        }
        client.post("/api/v1/send", json=payload, headers=auth_headers)
        client.post("/api/v1/send", json=payload, headers=auth_headers)

        body = client.get("/api/v1/metrics", headers=auth_headers).text
        assert _metric_value(body, "payouts_tx_duplicate_total") == 1.0

    def test_insufficient_usdt_increments_rejected(
        self, client, auth_headers, mock_tron,
    ):
        mock_tron.get_usdt_balance = MagicMock(return_value=Decimal("1"))
        client.post("/api/v1/send", json={
            "to_address": VALID_ADDRESS,
            "amount": "1000",
            "idempotency_key": "m-reject-usdt",
        }, headers=auth_headers)

        body = client.get("/api/v1/metrics", headers=auth_headers).text
        assert _metric_value(
            body, "payouts_tx_rejected_total", reason="insufficient_usdt",
        ) == 1.0

    def test_invalid_address_increments_rejected(self, client, auth_headers):
        client.post("/api/v1/send", json={
            "to_address": "A" * 34,
            "amount": "1",
            "idempotency_key": "m-reject-addr",
        }, headers=auth_headers)

        body = client.get("/api/v1/metrics", headers=auth_headers).text
        assert _metric_value(
            body, "payouts_tx_rejected_total", reason="invalid_address",
        ) == 1.0


class TestGaugesFromBalance:
    def test_balance_endpoint_updates_gauges(self, client, auth_headers, mock_tron):
        mock_tron.get_resource_summary = MagicMock(return_value={
            "energy_available": 7777,
            "energy_limit": 10_000,
            "bandwidth_free_available": 321,
            "bandwidth_paid_available": 42,
            "tron_power": 99,
        })
        mock_tron.get_trx_balance = MagicMock(return_value=Decimal("555"))
        mock_tron.get_usdt_balance = MagicMock(return_value=Decimal("12345.6"))

        client.get("/api/v1/balance", headers=auth_headers)

        body = client.get("/api/v1/metrics", headers=auth_headers).text
        assert _metric_value(body, "payouts_trx_balance") == 555.0
        assert _metric_value(body, "payouts_usdt_balance") == 12345.6
        assert _metric_value(body, "payouts_energy_available") == 7777.0
        assert _metric_value(body, "payouts_bandwidth_free_available") == 321.0
        assert _metric_value(body, "payouts_tron_power_staked") == 99.0


class TestRateLimitMetric:
    def test_rate_limit_drops_counter_increments(self, client, auth_headers, mock_tron):
        from skr_crypto.server.server import _limiter
        # Exhaust the limiter for this IP so the next /send gets 429.
        for _ in range(200):
            _limiter.check("testclient")

        resp = client.post("/api/v1/send", json={
            "to_address": VALID_ADDRESS,
            "amount": "1",
            "idempotency_key": "rl",
        }, headers=auth_headers)
        assert resp.status_code == 429

        body = client.get("/api/v1/metrics", headers=auth_headers).text
        assert _metric_value(body, "payouts_rate_limit_drops_total") == 1.0
